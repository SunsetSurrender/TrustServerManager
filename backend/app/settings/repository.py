"""Lettura e scrittura delle impostazioni, con revisione monotona.

NB sul nome: questo modulo tratta le impostazioni *dell'applicazione*, quelle che
un amministratore modifica dall'interfaccia e che vivono nel database. La
configurazione *del deployment* — host del database, secret, origini — sta in
`app/config.py` e non passa mai da qui. Sono due cose diverse che in italiano si
chiamano allo stesso modo.

Concorrenza ottimistica, non ultimo-che-scrive-vince
----------------------------------------------------
Due amministratori aprono la stessa schermata. Il primo aggiunge un
destinatario, il secondo cambia l'orario. Senza controllo, il secondo
salvataggio riscrive il documento intero con quello che il secondo aveva sotto
gli occhi e il destinatario appena aggiunto sparisce — senza errori, senza
tracce, e nessuno se ne accorge finché l'avviso non arriva. La revisione rende
il caso visibile: il secondo riceve un conflitto e ricarica.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth.audit import RESULT_SUCCESS, record_auth_event
from app.settings.schema import canonicalise, default_document

SETTINGS_UPDATED = "settings.updated"


class SettingsError(Exception):
    code = "settings_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SettingsVersionConflict(SettingsError):
    """La revisione fornita non è quella corrente."""

    code = "settings_version_conflict"

    def __init__(self, current_version: int):
        super().__init__("le impostazioni sono state modificate da un'altra sessione")
        self.current_version = current_version


class SettingsMissing(SettingsError):
    """La riga unica non c'è. Non è un caso da gestire con un default silenzioso.

    La riga nasce nella migrazione 0007 e il ruolo di runtime non ha il
    privilegio di crearne una: se manca, il database non è al livello atteso.
    Inventare qui un documento di default nasconderebbe una migrazione non
    applicata, e il servizio girerebbe con impostazioni che nessuno ha salvato.
    """

    code = "settings_unavailable"

    def __init__(self) -> None:
        super().__init__("impostazioni non inizializzate")


class SettingsCorrupted(SettingsError):
    """La colonna `data` non contiene un oggetto JSON.

    Non è una condizione da correggere al volo: significa che qualcosa ha scritto
    un valore della forma sbagliata, e continuare come se niente fosse
    propagherebbe il problema a chiunque legga la colonna con gli operatori jsonb.
    """

    code = "settings_unavailable"

    def __init__(self, found: str):
        super().__init__("documento delle impostazioni non valido")
        self.found = found


@dataclass(frozen=True)
class SettingsRow:
    version: int
    data: dict
    updated_at: Any
    updated_by: Any

    @property
    def etag(self) -> str:
        """ETag forte, la revisione fra virgolette. È il valore che il client
        rimanda in `If-Match`, non un valore che deve saper costruire."""
        return f'"{self.version}"'


def load(conn: Connection, *, for_update: bool = False) -> SettingsRow:
    row = conn.execute(text(
        "SELECT version, data, updated_at, updated_by FROM settings WHERE id = 1"
        + (" FOR UPDATE" if for_update else "")
    )).first()
    if row is None:
        raise SettingsMissing()
    # Il driver restituisce un `dict` per un oggetto jsonb. Qualunque altra cosa
    # significa che nella colonna è finito un valore jsonb che non è un oggetto —
    # tipicamente una *stringa* JSON, per una doppia serializzazione.
    #
    # Qui NON si accomoda con un `json.loads`: aprire la stringa farebbe
    # funzionare l'API e lascerebbe rotti gli operatori jsonb (`data -> …`), che
    # su una stringa restituiscono NULL. Il guasto si manifesterebbe altrove,
    # molto più tardi, come «lo scheduler non trova destinatari». Meglio adesso.
    if not isinstance(row[1], dict):
        raise SettingsCorrupted(type(row[1]).__name__)
    return SettingsRow(version=int(row[0]), data=row[1],
                       updated_at=row[2], updated_by=row[3])


def save(conn: Connection, *, payload: Any, expected_version: int,
         actor) -> tuple[SettingsRow, bool]:
    """Salva se il documento cambia davvero. Restituisce (riga, modificato).

    Tutto in UNA transazione, quella della richiesta: la riga di audit e la
    modifica stanno o cadono insieme. Se l'audit fallisce, la modifica non
    resta — un cambiamento di configurazione senza traccia di chi l'ha fatto è
    esattamente ciò che l'audit esiste per impedire.

    Il blocco `FOR UPDATE` è preso PRIMA di confrontare la revisione: leggere,
    confrontare e poi scrivere senza blocco lascerebbe una finestra in cui due
    richieste con la stessa revisione attesa passano entrambe il controllo, e la
    seconda sovrascriverebbe la prima con la benedizione del meccanismo che
    dovrebbe impedirlo.
    """
    current = load(conn, for_update=True)
    candidate = canonicalise(payload)

    if current.version != expected_version:
        raise SettingsVersionConflict(current.version)

    if candidate == current.data:
        # Nessun cambiamento reale: la revisione NON sale. Se salisse, aprire e
        # salvare senza toccare niente farebbe fallire il salvataggio di un
        # collega che aveva la schermata aperta — un conflitto inventato, che
        # insegna a ignorare i conflitti veri.
        return current, False

    changed_fields = _changed_fields(current.data, candidate)

    # `CAST(:d AS jsonb)` esplicito: senza, si dipende dal cast implicito che
    # PostgreSQL applica in assegnazione, e basta cambiare il contesto della query
    # perché nella colonna finisca una *stringa* JSON invece di un oggetto (vedi
    # la migrazione 0007, dove è successo).
    row = conn.execute(text("""
        UPDATE settings
           SET data = CAST(:d AS jsonb), version = version + 1,
               updated_at = now(), updated_by = :by
         WHERE id = 1
     RETURNING version, data, updated_at, updated_by
    """), {"d": json.dumps(candidate), "by": actor.user_id}).first()

    # Nel dettaglio finiscono i NOMI dei campi cambiati, non i valori. I
    # destinatari sono indirizzi di persone e non c'è ragione di duplicarli in
    # ogni riga di registro; il documento corrente si legge con una GET.
    record_auth_event(conn, SETTINGS_UPDATED, username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      result=RESULT_SUCCESS,
                      detail={"fromVersion": current.version,
                              "toVersion": int(row[0]),
                              "changedFields": changed_fields})

    if not isinstance(row[1], dict):                        # pragma: no cover
        raise SettingsCorrupted(type(row[1]).__name__)
    return SettingsRow(version=int(row[0]), data=row[1],
                       updated_at=row[2], updated_by=row[3]), True


def _changed_fields(before: dict, after: dict) -> list[str]:
    """Percorsi dei campi diversi, in ordine. Solo i nomi."""
    out: list[str] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key), b.get(key), f"{path}.{key}" if path else key)
        elif a != b:
            out.append(path)

    walk(before, after, "")
    return out


def to_response(row: SettingsRow, *, smtp_configured: bool) -> dict:
    """Corpo della risposta: documento tipizzato più i campi derivati.

    `smtp` contiene UNA cosa sola, ed è un booleano. Non l'host, non l'utenza,
    non il percorso del secret: tutto ciò che riguarda il trasporto è gestito
    dall'operations e non ha motivo di attraversare l'API. Un oggetto `smtp` con
    dentro dei parametri è anche l'oggetto in cui, un giorno, qualcuno
    aggiungerà `password` senza pensarci — e la forma migliore per impedirlo è
    che non ci sia nessun posto dove metterla.
    """
    return {
        "version": row.version,
        "notifications": copy_notifications(row.data),
        "smtp": {"configured": bool(smtp_configured)},
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def copy_notifications(data: dict) -> dict:
    """Blocco `notifications` con i campi previsti, e solo quelli.

    Si ricostruisce campo per campo invece di restituire ciò che c'è nel
    `jsonb`: se una riga scritta da una versione futura (o da una mano umana sul
    database) contenesse una chiave in più, restituirla significherebbe farla
    uscire senza che nessuno l'abbia mai validata.
    """
    notif = data.get("notifications") if isinstance(data, dict) else None
    if not isinstance(notif, dict):
        notif = default_document()["notifications"]
    fallback = default_document()["notifications"]
    schedule = notif.get("schedule")
    if not isinstance(schedule, dict):
        schedule = fallback["schedule"]
    return {
        "enabled": bool(notif.get("enabled", fallback["enabled"])),
        "timezone": str(notif.get("timezone", fallback["timezone"])),
        "warningDays": [int(d) for d in notif.get("warningDays", []) or []],
        "recipients": [str(r) for r in notif.get("recipients", []) or []],
        "schedule": {"hour": int(schedule.get("hour", 0)),
                     "minute": int(schedule.get("minute", 0))},
    }
