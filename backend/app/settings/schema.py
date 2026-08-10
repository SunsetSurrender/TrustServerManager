"""Schema TIPIZZATO delle impostazioni: validazione e forma canonica.

Puro: niente database, niente HTTP. Prende quello che è arrivato e restituisce
il documento canonico, oppure solleva con un codice stabile.

Perché uno schema chiuso e non una tabella chiave/valore
--------------------------------------------------------
Un editor di coppie chiave/valore è comodo per chi lo scrive e indifendibile per
chi lo mantiene: nulla impedisce che un giorno qualcuno ci metta dentro
`smtp.password`, e da quel momento la password è in un campo che l'API
restituisce a chiunque possa leggere le impostazioni. Qui i campi ammessi sono
un elenco finito, tutto il resto viene rifiutato, e le chiavi che *somigliano* a
un segreto vengono rifiutate a qualunque profondità — anche se un giorno
qualcuno aggiungerà un sotto-oggetto a cui nessuno ha ancora pensato.

Sostituzione, non modifica parziale
-----------------------------------
`PUT` pretende l'oggetto `notifications` COMPLETO. Non è pignoleria: con i campi
facoltativi «assente» e «falso» diventano indistinguibili, e un client che
dimentica `enabled` spegnerebbe le notifiche senza volerlo. Con tutti i campi
obbligatori il caso non esiste, e `enabled: false` è un valore esplicito che
attraversa la canonicalizzazione intatto.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.audit.sanitize import SENSITIVE_KEY_PARTS

# ------------------------------------------------------------------ limiti
#: Tetto sul corpo della richiesta. Il limite globale (§8.22) è di 5 MiB perché
#: deve lasciar passare l'inventario; una configurazione di notifica che arriva a
#: 16 KiB è già un abuso.
MAX_SETTINGS_BYTES = 16 * 1024

MAX_RECIPIENTS = 20
MAX_RECIPIENT_CHARS = 254          # RFC 5321: lunghezza massima di un percorso
MAX_LOCAL_PART_CHARS = 64
MAX_WARNING_DAYS = 10
MAX_WARNING_DAY_VALUE = 3650       # dieci anni: oltre non è un preavviso
MAX_TIMEZONE_CHARS = 64

#: Profondità e numero di chiavi del documento GREZZO, controllati prima di
#: guardarne il contenuto: la scansione ricorsiva alla ricerca di chiavi segrete
#: non deve poter essere trasformata in un carico con un annidamento profondo.
MAX_DEPTH = 8
MAX_KEYS = 64

# ------------------------------------------------------------------ codici
UNKNOWN_FIELD = "unknown_field"
READ_ONLY_FIELD = "read_only_field"
MISSING_FIELD = "missing_field"
SECRET_FIELD_REJECTED = "secret_field_rejected"
INVALID_TYPE = "invalid_type"
INVALID_RECIPIENT = "invalid_recipient"
DUPLICATE_RECIPIENT = "duplicate_recipient"
TOO_MANY_RECIPIENTS = "too_many_recipients"
INVALID_WARNING_DAY = "invalid_warning_day"
TOO_MANY_WARNING_DAYS = "too_many_warning_days"
INVALID_TIMEZONE = "invalid_timezone"
INVALID_SCHEDULE = "invalid_schedule"
DOCUMENT_TOO_COMPLEX = "document_too_complex"

#: Campi che l'API produce ma non accetta. Rifiutarli con un codice PROPRIO
#: invece che come «sconosciuti» dice al client la cosa giusta: non ha inventato
#: un campo, ne ha rimandato indietro uno di sola lettura.
READ_ONLY_TOP_LEVEL = ("version", "smtp", "updatedAt", "updatedBy")

TOP_LEVEL_FIELDS = ("notifications",)
NOTIFICATION_FIELDS = ("enabled", "timezone", "warningDays", "recipients",
                       "schedule")
SCHEDULE_FIELDS = ("hour", "minute")

#: Indirizzo di posta. Deliberatamente conservativo: si pretende almeno un punto
#: nel dominio. In rete chiusa `utente@mailhost` sarebbe tecnicamente valido, ma
#: il caso reale è sempre un FQDN e il dominio senza punto è quasi sempre un
#: dominio dimenticato a metà — che si scopre solo quando la posta non arriva.
_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
EMAIL_RE = re.compile(rf"^{_LOCAL}@{_LABEL}(?:\.{_LABEL})+$")

#: Documento iniziale, e forma canonica di un'installazione appena creata.
#: `enabled` è falso: un servizio nuovo non deve cominciare a mandare posta
#: perché qualcuno ha acceso l'interruttore e nessuno ha ancora messo un
#: destinatario. La migrazione 0007 inserisce esattamente questo, e un test lo
#: verifica — se divergessero, la prima GET restituirebbe un documento che la
#: PUT successiva considera modificato.
DEFAULTS: dict[str, Any] = {
    "notifications": {
        "enabled": False,
        "timezone": "Europe/Rome",
        "warningDays": [30],
        "recipients": [],
        "schedule": {"hour": 8, "minute": 0},
    },
}


class SettingsValidationError(Exception):
    """Impostazioni non accettabili. `code` è stabile e va sul filo.

    `field` è il PERCORSO del campo, mai il suo valore: un messaggio d'errore che
    cita il valore rifiutato lo copierebbe nei log e nelle risposte, e per un
    campo che non doveva esserci quel valore potrebbe essere proprio il segreto
    che si sta cercando di tenere fuori.
    """

    code = "settings_invalid"

    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def default_document() -> dict:
    return copy.deepcopy(DEFAULTS)


# ==================================================================
# 1. scansione del documento grezzo: segreti e complessità
# ==================================================================

def reject_secret_like_keys(value: Any, path: str = "", depth: int = 0) -> None:
    """Rifiuta a QUALUNQUE profondità una chiave che somigli a un segreto.

    Gira sul documento grezzo, prima della validazione strutturale, e usa lo
    stesso vocabolario della ripulitura dell'audit (`SENSITIVE_KEY_PARTS`): una
    sola definizione di «somiglia a un segreto» per tutta l'applicazione, perché
    due elenchi diversi divergono e quello dimenticato è sempre quello che
    conta.

    Sarebbe tecnicamente ridondante — un campo sconosciuto viene rifiutato
    comunque dalla validazione strutturale — ma la ridondanza è il punto: se un
    giorno lo schema crescerà di un sotto-oggetto, quel sotto-oggetto nascerà già
    protetto invece di dipendere dall'attenzione di chi lo aggiunge.
    """
    if depth > MAX_DEPTH:
        raise SettingsValidationError(
            DOCUMENT_TOO_COMPLEX, "documento troppo annidato", path or "(radice)")

    if isinstance(value, dict):
        if len(value) > MAX_KEYS:
            raise SettingsValidationError(
                DOCUMENT_TOO_COMPLEX, "troppe chiavi", path or "(radice)")
        for key, sub in value.items():
            k = str(key)
            here = f"{path}.{k}" if path else k
            if any(part in k.lower() for part in SENSITIVE_KEY_PARTS):
                raise SettingsValidationError(
                    SECRET_FIELD_REJECTED,
                    "le impostazioni non contengono segreti: "
                    "la password SMTP è gestita dall'operations tramite secret",
                    here)
            reject_secret_like_keys(sub, here, depth + 1)

    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_KEYS:
            raise SettingsValidationError(
                DOCUMENT_TOO_COMPLEX, "elenco troppo lungo", path or "(radice)")
        for i, sub in enumerate(value):
            reject_secret_like_keys(sub, f"{path}[{i}]", depth + 1)


# ==================================================================
# 2. validatori dei singoli campi
# ==================================================================

def _require_keys(obj: dict, allowed: tuple[str, ...], where: str) -> None:
    for key in obj:
        if key not in allowed:
            raise SettingsValidationError(
                UNKNOWN_FIELD, f"campo non previsto: {key!r}",
                f"{where}.{key}" if where else str(key))
    for key in allowed:
        if key not in obj:
            raise SettingsValidationError(
                MISSING_FIELD,
                f"campo obbligatorio mancante: {key!r} "
                "(PUT sostituisce, non modifica parzialmente)",
                f"{where}.{key}" if where else str(key))


def _as_bool(value: Any, field: str) -> bool:
    # `isinstance(True, int)` è vero in Python: senza il controllo esplicito
    # sul tipo bool, un `1` passerebbe per `true` e la validazione «tipizzata»
    # non lo sarebbe.
    if not isinstance(value, bool):
        raise SettingsValidationError(
            INVALID_TYPE, "atteso un booleano (true/false)", field)
    return value


def _as_int(value: Any, field: str, code: str = INVALID_TYPE) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsValidationError(code, "atteso un intero", field)
    return value


def canonical_recipient(raw: Any, index: int) -> str:
    """Un indirizzo, ripulito. Solleva se non è accettabile.

    Normalizzazione: spazi via, dominio in minuscolo. La parte locale resta
    com'è, perché per l'RFC è sensibile alle maiuscole e cambiarla significa
    inviare a un indirizzo diverso da quello che l'amministratore ha scritto. La
    ricerca dei duplicati, invece, confronta tutto in minuscolo: nessun server
    reale tratta `Mario@x.it` e `mario@x.it` come due persone, e accettarli
    entrambi manderebbe due copie dello stesso avviso.
    """
    field = f"notifications.recipients[{index}]"
    if not isinstance(raw, str):
        raise SettingsValidationError(INVALID_TYPE, "atteso un indirizzo", field)

    value = raw.strip()
    if not value:
        raise SettingsValidationError(INVALID_RECIPIENT, "indirizzo vuoto", field)
    if len(value) > MAX_RECIPIENT_CHARS:
        raise SettingsValidationError(
            INVALID_RECIPIENT,
            f"indirizzo oltre {MAX_RECIPIENT_CHARS} caratteri", field)
    # Un indirizzo con spazi interni, virgole o a capo è quasi sempre un elenco
    # incollato in un campo solo: dirlo è più utile di «non valido».
    if any(c.isspace() or c == "," or c == ";" for c in value):
        raise SettingsValidationError(
            INVALID_RECIPIENT,
            "un indirizzo per riga: spazi, virgole e punti e virgola non sono "
            "ammessi dentro un indirizzo", field)
    if value.count("@") != 1:
        raise SettingsValidationError(
            INVALID_RECIPIENT, "indirizzo non valido", field)

    local, domain = value.split("@")
    if len(local) > MAX_LOCAL_PART_CHARS:
        raise SettingsValidationError(
            INVALID_RECIPIENT,
            f"parte locale oltre {MAX_LOCAL_PART_CHARS} caratteri", field)

    value = f"{local}@{domain.lower()}"
    if not EMAIL_RE.match(value):
        raise SettingsValidationError(
            INVALID_RECIPIENT, "indirizzo non valido", field)
    return value


def canonical_recipients(raw: Any) -> list[str]:
    field = "notifications.recipients"
    if not isinstance(raw, list):
        raise SettingsValidationError(INVALID_TYPE, "atteso un elenco", field)
    if len(raw) > MAX_RECIPIENTS:
        raise SettingsValidationError(
            TOO_MANY_RECIPIENTS,
            f"al massimo {MAX_RECIPIENTS} destinatari", field)

    out: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        address = canonical_recipient(item, i)
        key = address.lower()
        if key in seen:
            # Dopo la normalizzazione: due scritture diverse dello stesso
            # indirizzo sono un duplicato, e riceverebbero due copie di ogni
            # avviso.
            raise SettingsValidationError(
                DUPLICATE_RECIPIENT, "destinatario ripetuto",
                f"{field}[{i}]")
        seen.add(key)
        out.append(address)
    # NIENTE ordinamento: l'ordine è quello scritto dall'amministratore, ed è
    # informazione sua. La canonicalizzazione serve a rendere confrontabili due
    # documenti, non a riorganizzarli.
    return out


def canonical_warning_days(raw: Any) -> list[int]:
    """Giorni di preavviso: interi positivi, senza ripetizioni, ordinati.

    Qui l'ordinamento CI VUOLE, al contrario dei destinatari: `[30, 7]` e
    `[7, 30]` sono la stessa configurazione — un insieme di finestre — e senza un
    ordine deterministico due salvataggi equivalenti sembrerebbero diversi e
    farebbero salire la revisione a vuoto.
    """
    field = "notifications.warningDays"
    if not isinstance(raw, list):
        raise SettingsValidationError(INVALID_TYPE, "atteso un elenco", field)
    if len(raw) > MAX_WARNING_DAYS:
        raise SettingsValidationError(
            TOO_MANY_WARNING_DAYS,
            f"al massimo {MAX_WARNING_DAYS} finestre di preavviso", field)

    values: set[int] = set()
    for i, item in enumerate(raw):
        day = _as_int(item, f"{field}[{i}]", INVALID_WARNING_DAY)
        if day <= 0:
            raise SettingsValidationError(
                INVALID_WARNING_DAY, "il preavviso deve essere positivo",
                f"{field}[{i}]")
        if day > MAX_WARNING_DAY_VALUE:
            raise SettingsValidationError(
                INVALID_WARNING_DAY,
                f"preavviso oltre {MAX_WARNING_DAY_VALUE} giorni",
                f"{field}[{i}]")
        values.add(day)
    return sorted(values)


def canonical_timezone(raw: Any) -> str:
    """Fuso IANA. Validato costruendo davvero uno `ZoneInfo`.

    Un elenco di nomi ammessi scritto a mano invecchierebbe; `ZoneInfo` consulta
    il database dei fusi, che è la stessa fonte che userà lo scheduler. Se il
    nome non esiste è meglio saperlo adesso che alle 8 del mattino, quando
    l'invio non parte.
    """
    field = "notifications.timezone"
    if not isinstance(raw, str):
        raise SettingsValidationError(INVALID_TYPE, "atteso un fuso orario", field)
    value = raw.strip()
    if not value or len(value) > MAX_TIMEZONE_CHARS:
        raise SettingsValidationError(
            INVALID_TIMEZONE, "nome di fuso orario non valido", field)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # ValueError copre anche i percorsi assoluti e i `..`, che ZoneInfo
        # rifiuta per conto suo: il nome non può diventare una lettura di file.
        raise SettingsValidationError(
            INVALID_TIMEZONE,
            "fuso orario non riconosciuto (atteso un nome IANA, "
            "es. 'Europe/Rome')", field) from None
    return value


def canonical_schedule(raw: Any) -> dict:
    field = "notifications.schedule"
    if not isinstance(raw, dict):
        raise SettingsValidationError(INVALID_TYPE, "atteso un oggetto", field)
    _require_keys(raw, SCHEDULE_FIELDS, field)

    hour = _as_int(raw["hour"], f"{field}.hour", INVALID_SCHEDULE)
    minute = _as_int(raw["minute"], f"{field}.minute", INVALID_SCHEDULE)
    if not 0 <= hour <= 23:
        raise SettingsValidationError(
            INVALID_SCHEDULE, "ora fuori intervallo (0-23)", f"{field}.hour")
    if not 0 <= minute <= 59:
        raise SettingsValidationError(
            INVALID_SCHEDULE, "minuto fuori intervallo (0-59)", f"{field}.minute")
    return {"hour": hour, "minute": minute}


# ==================================================================
# 3. documento completo
# ==================================================================

def canonicalise(payload: Any) -> dict:
    """Documento canonico a partire dal corpo di una `PUT`. Solleva se non va.

    L'ordine dei controlli non è casuale: prima i segreti, poi la struttura. Un
    corpo che contiene `password` deve sentirsi dire *quello*, non «campo
    sconosciuto» — che è vero ma non spiega perché quel campo non esisterà mai.
    """
    if not isinstance(payload, dict):
        raise SettingsValidationError(
            INVALID_TYPE, "atteso un oggetto JSON", "(radice)")

    reject_secret_like_keys(payload)

    for key in payload:
        if key in READ_ONLY_TOP_LEVEL:
            raise SettingsValidationError(
                READ_ONLY_FIELD,
                f"{key!r} è di sola lettura: la concorrenza si gestisce con "
                "l'intestazione If-Match, non con un campo nel corpo",
                str(key))
    _require_keys(payload, TOP_LEVEL_FIELDS, "")

    notif = payload["notifications"]
    if not isinstance(notif, dict):
        raise SettingsValidationError(
            INVALID_TYPE, "atteso un oggetto", "notifications")
    _require_keys(notif, NOTIFICATION_FIELDS, "notifications")

    return {
        "notifications": {
            # `_as_bool` e non `bool(...)`: la conversione accetterebbe
            # qualunque cosa, e `enabled: "false"` diventerebbe `True`. Il valore
            # esplicito `False` attraversa intatto — è il caso che conta.
            "enabled": _as_bool(notif["enabled"], "notifications.enabled"),
            "timezone": canonical_timezone(notif["timezone"]),
            "warningDays": canonical_warning_days(notif["warningDays"]),
            "recipients": canonical_recipients(notif["recipients"]),
            "schedule": canonical_schedule(notif["schedule"]),
        },
    }
