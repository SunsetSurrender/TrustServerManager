"""Garbage collection delle foto orfane. L'UNICO posto che cancella byte.

Gira nel worker, con un ruolo di database che ha `DELETE` su `photos` e che l'API
non ha (migrazione 0009). Non esiste `DELETE /api/photos/{id}`, e non è una
dimenticanza: un amministratore che «rimuove una foto» dal rack sta cambiando un
riferimento in una versione nuova dell'inventario, e le versioni vecchie
continuano a puntare ai byte. Dare a una rotta HTTP il potere di cancellarli
significherebbe che una persona può rompere il rollback di un'altra (§8.5).

Due condizioni, entrambe necessarie
-----------------------------------
    nessuna riga in `inventory_photo_refs`
  E `created_at` più vecchio della finestra di grazia

La seconda copre la finestra in cui una foto è legittimamente orfana: caricata,
non ancora referenziata. Succede a ogni conflitto sul salvataggio, a ogni modulo
chiuso senza salvare, a ogni sessione interrotta. Ventiquattro ore sono
abbondanti per qualunque di questi casi e brevi rispetto al ritmo con cui si
carica una foto di armadio.

⚠ La prima condizione NON guarda l'inventario corrente. Una foto referenziata
soltanto da una versione vecchia è **viva**: vedi la nota in testa a
`app/photos/refs.py`.

Perché non basta la query
-------------------------
La chiave esterna `inventory_photo_refs.photo_id → photos.id` è senza
`ON DELETE`, quindi il database RIFIUTA di cancellare una foto referenziata. Se
la query qui sotto venisse riscritta male, la GC otterrebbe un errore invece di
perdere dei byte. Il vincolo copre anche l'intreccio con una scrittura
concorrente: sotto READ COMMITTED la sottoquery potrebbe non vedere un
riferimento appena inserito e non ancora committato, e in quel caso è il vincolo
a far fallire la cancellazione. Un giro di GC fallito si ripete domani; dei byte
cancellati non tornano.

Riferimento: BACKEND-PLAN.md §8.5.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

#: Nome del lavoro in `maintenance_runs`. Elenco chiuso imposto da un vincolo
#: della migrazione: un lavoro periodico nuovo è una decisione, non una stringa.
GC_JOB = "photo_gc"

#: Finestra di grazia per gli upload non ancora referenziati.
GRACE = timedelta(hours=24)

#: Ora locale del giro. Sua, non quella degli avvisi: la GC è un lavoro
#: indipendente e deve poterlo restare anche nell'orario.
GC_HOUR = 3
GC_MINUTE = 30

#: Quanti identificativi si riportano nell'audit. Sono UUID generati dal server,
#: quindi innocui, ma una riga di registro non deve poter diventare grande a
#: piacere: il conteggio è sempre esatto, l'elenco è troncato e lo dichiara.
MAX_AUDIT_IDS = 50

GC_AUDIT_ACTION = "photos.gc.collected"

#: Attore delle righe di audit. Distinto da quello del worker delle notifiche: chi
#: legge il registro deve poter distinguere «ha mandato una email» da «ha
#: cancellato dei byte», anche se il processo è lo stesso.
GC_ACTOR = "(worker manutenzione)"


@dataclass
class GcResult:
    ran: bool = False
    reason: str = ""
    examined: int = 0
    deleted: int = 0
    run_date: date | None = None
    deleted_ids: list[str] = field(default_factory=list)


# ==================================================================
# identità durevole dell'esecuzione: propria, non quella degli avvisi
# ==================================================================

def claim_run(conn: Connection, run_date: date, tz_name: str) -> bool:
    """Prenota il giro di questa data locale. False se già fatto.

    Stessa forma di `scheduler_runs` (§8.41) e tabella diversa. Condividere la
    riga con gli avvisi di scadenza sembrerebbe un risparmio e sarebbe un
    accoppiamento: spegnere le notifiche fermerebbe anche la liberazione dello
    spazio, e un giro di avvisi già registrato per oggi impedirebbe alla GC di
    recuperare il suo.

    Il conflitto riprende una riga rimasta NON conclusa, per lo stesso motivo di
    `scheduler_runs`: un giro interrotto a metà non deve far saltare la giornata.
    """
    row = conn.execute(text("""
        INSERT INTO maintenance_runs (job, run_date, timezone)
        VALUES (:job, :d, :tz)
        ON CONFLICT (job, run_date) DO UPDATE
           SET started_at = now(), timezone = :tz
         WHERE maintenance_runs.finished_at IS NULL
     RETURNING run_date
    """), {"job": GC_JOB, "d": run_date, "tz": tz_name}).first()
    return row is not None


def finish_run(conn: Connection, run_date: date, *, examined: int, deleted: int,
               outcome: str) -> None:
    conn.execute(text("""
        UPDATE maintenance_runs
           SET finished_at = now(), examined_count = :e, deleted_count = :d,
               outcome = :o
         WHERE job = :job AND run_date = :rd
    """), {"job": GC_JOB, "rd": run_date, "e": examined, "d": deleted,
           "o": outcome[:200]})


def last_run(conn: Connection) -> dict | None:
    """Ultimo giro concluso, per il monitoraggio."""
    row = conn.execute(text("""
        SELECT run_date, finished_at, examined_count, deleted_count, outcome
          FROM maintenance_runs
         WHERE job = :job AND finished_at IS NOT NULL
         ORDER BY run_date DESC LIMIT 1
    """), {"job": GC_JOB}).mappings().first()
    return dict(row) if row else None


# ==================================================================
# la cancellazione
# ==================================================================

def candidates(conn: Connection, cutoff: datetime) -> int:
    """Quante foto sono abbastanza vecchie da poter essere esaminate.

    Si registra insieme al numero di cancellate: «guardate 40, cancellate 0» e
    «guardate 0» sono due situazioni diverse, e con il solo conteggio delle
    cancellazioni sembrerebbero la stessa.
    """
    return int(conn.execute(
        text("SELECT count(*) FROM photos WHERE created_at < :c"),
        {"c": cutoff}).scalar_one())


def collect(conn: Connection, cutoff: datetime) -> list[str]:
    """Cancella le orfane più vecchie del limite. Restituisce gli id cancellati."""
    rows = conn.execute(text("""
        DELETE FROM photos p
         WHERE p.created_at < :cutoff
           AND NOT EXISTS (SELECT 1 FROM inventory_photo_refs r
                            WHERE r.photo_id = p.id)
     RETURNING p.id
    """), {"cutoff": cutoff}).all()
    return sorted(str(r[0]) for r in rows)


# ==================================================================
# un giro
# ==================================================================

def _timezone_for_gc(engine: Engine) -> str:
    """Fuso in cui calcolare la data locale del giro.

    Si legge dalle impostazioni per coerenza con il resto (un amministratore che
    configura `Europe/Rome` si aspetta che «giornaliero» voglia dire quello), ma
    **un guasto nel leggerle non ferma la GC**: si ricade su UTC. Le notifiche
    spente o le impostazioni illeggibili sono problemi delle notifiche; lo spazio
    su disco va liberato comunque.
    """
    try:
        from app.settings import repository as settings_repo
        with engine.connect() as conn:
            with conn.begin():
                data = settings_repo.load(conn).data
        tz = (data.get("notifications") or {}).get("timezone") or "UTC"
        ZoneInfo(tz)                     # se non è valido si usa UTC
        return tz
    except Exception:
        log.warning("fuso per la GC non leggibile: si usa UTC")
        return "UTC"


def run_once(engine: Engine, *, now_utc: datetime, force: bool = False,
             grace: timedelta = GRACE) -> GcResult:
    """Un giro di GC. Indipendente dal worker delle notifiche.

    Non guarda `notifications.enabled`: con gli avvisi spenti la GC deve girare
    comunque, altrimenti spegnere le email riempirebbe lentamente il disco.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    tz_name = _timezone_for_gc(engine)
    local = now_utc.astimezone(ZoneInfo(tz_name))
    if not force and (local.hour * 60 + local.minute) < GC_HOUR * 60 + GC_MINUTE:
        return GcResult(reason="not_yet_scheduled")
    today = local.date()

    with engine.connect() as conn:
        with conn.begin():
            if not claim_run(conn, today, tz_name):
                return GcResult(reason="already_ran_today")

    cutoff = now_utc - grace
    with engine.connect() as conn:
        with conn.begin():
            examined = candidates(conn, cutoff)
            deleted = collect(conn, cutoff)
            _audit(conn, deleted, examined=examined, grace=grace)
            finish_run(conn, today, examined=examined, deleted=len(deleted),
                       outcome="collected" if deleted else "nothing_to_collect")

    if deleted:
        log.info("GC foto: %d orfane cancellate su %d esaminate",
                 len(deleted), examined)
    return GcResult(ran=True, reason="collected" if deleted else "nothing_to_collect",
                    examined=examined, deleted=len(deleted), run_date=today,
                    deleted_ids=deleted)


def _audit(conn: Connection, deleted: list[str], *, examined: int,
           grace: timedelta) -> None:
    """Una riga per giro, non una per foto.

    Un giro che libera cinquecento orfane scriverebbe cinquecento righe che dicono
    la stessa cosa, e il registro serve a essere letto. Il conteggio è esatto,
    l'elenco degli identificativi è troncato e la riga dichiara di esserlo.

    ⚠ Nessun `try/except` qui, a differenza dell'audit del worker delle notifiche.
    Là ingoiare un guasto del registro è la scelta giusta: la posta è già partita e
    fingere il contrario farebbe rimandare il messaggio. Qui la cancellazione non è
    ancora avvenuta — siamo nella stessa transazione — e cancellare byte senza
    lasciarne traccia è esattamente ciò che un registro esiste per impedire. Se
    l'audit non si scrive, la cancellazione non avviene: la foto resta orfana un
    giorno in più e il giro riprende, perché la riga di `maintenance_runs` è stata
    committata a parte e resta non conclusa.
    """
    if not deleted:
        return
    from app.audit.sanitize import sanitize
    from app.auth.audit import RESULT_SUCCESS, record_auth_event

    detail = {
        "deleted": len(deleted),
        "examined": examined,
        "graceHours": int(grace.total_seconds() // 3600),
        "ids": deleted[:MAX_AUDIT_IDS],
    }
    if len(deleted) > MAX_AUDIT_IDS:
        detail["idsTruncated"] = True
    record_auth_event(conn, GC_AUDIT_ACTION, username=GC_ACTOR, role=None,
                      ip=None, result=RESULT_SUCCESS, detail=sanitize(detail))
