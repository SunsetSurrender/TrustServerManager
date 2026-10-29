"""Pulizia giornaliera dei dati di accesso (migrazione 0016).

Cancella, oltre la soglia di conservazione (`TSM_AUTH_RETENTION_DAYS`, 90 giorni
predefiniti, mai meno di 30):

- `login_attempts` più vecchi della soglia;
- `sessions` scadute o revocate da più della soglia (le sessioni vive no, mai);
- `recovery_codes` creati da più della soglia.

Stessa forma della GC delle foto: una riga in `maintenance_runs` per data locale
(job `auth_purge`), ora propria (03:45), una riga di audit per giro con i soli
conteggi, e nessun legame con le notifiche — spegnere gli avvisi non deve far
crescere le tabelle.

Perché non si tocca l'audit: è la storia delle modifiche e degli accessi, e chi
la legge deve poterci contare. Le righe potate qui sono dati di LAVORO del
limitatore e delle sessioni; ciò che conta di loro (accessi falliti, blocchi,
recuperi) è già nell'audit.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

JOB = "auth_purge"
PURGE_HOUR = 3
PURGE_MINUTE = 45
DEFAULT_RETENTION_DAYS = 90
MIN_RETENTION_DAYS = 30
AUDIT_ACTION = "maintenance.auth_purge"
ACTOR = "(worker manutenzione)"


@dataclass
class PurgeResult:
    ran: bool = False
    reason: str = ""
    run_date: date | None = None
    login_attempts: int = 0
    sessions: int = 0
    recovery_codes: int = 0

    @property
    def total(self) -> int:
        return self.login_attempts + self.sessions + self.recovery_codes


def retention_days() -> int:
    """Soglia da ambiente, con un minimo: un errore di battitura (`TSM_AUTH_RETENTION_DAYS=0`)
    non deve poter svuotare le tabelle in una notte."""
    raw = os.environ.get("TSM_AUTH_RETENTION_DAYS", "").strip()
    try:
        n = int(raw) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        log.warning("TSM_AUTH_RETENTION_DAYS non numerico (%r): uso %d",
                    raw, DEFAULT_RETENTION_DAYS)
        n = DEFAULT_RETENTION_DAYS
    return max(n, MIN_RETENTION_DAYS)


def run_once(engine: Engine, *, now_utc: datetime, tz_name: str = "UTC",
             force: bool = False, days: int | None = None) -> PurgeResult:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        tz_name = "UTC"
        local = now_utc
    if not force and (local.hour * 60 + local.minute) < PURGE_HOUR * 60 + PURGE_MINUTE:
        return PurgeResult(reason="not_yet_scheduled")
    today = local.date()

    with engine.connect() as conn:
        with conn.begin():
            row = conn.execute(text("""
                INSERT INTO maintenance_runs (job, run_date, timezone)
                VALUES (:job, :d, :tz)
                ON CONFLICT (job, run_date) DO UPDATE
                   SET started_at = now(), timezone = :tz
                 WHERE maintenance_runs.finished_at IS NULL
             RETURNING run_date
            """), {"job": JOB, "d": today, "tz": tz_name}).first()
            if row is None:
                return PurgeResult(reason="already_ran_today")

    keep = days if days is not None else retention_days()
    cutoff = now_utc - timedelta(days=keep)
    res = PurgeResult(ran=True, run_date=today)
    with engine.connect() as conn:
        with conn.begin():
            res.login_attempts = conn.execute(text(
                "DELETE FROM login_attempts WHERE ts < :c"), {"c": cutoff}).rowcount
            res.sessions = conn.execute(text("""
                DELETE FROM sessions
                 WHERE (revoked_at IS NOT NULL AND revoked_at < :c)
                    OR (revoked_at IS NULL AND expires_at < :c)
            """), {"c": cutoff}).rowcount
            res.recovery_codes = conn.execute(text(
                "DELETE FROM recovery_codes WHERE created_at < :c"), {"c": cutoff}).rowcount
            res.reason = "purged" if res.total else "nothing_to_purge"
            if res.total:
                from app.auth.audit import RESULT_SUCCESS, record_auth_event
                record_auth_event(conn, AUDIT_ACTION, username=ACTOR, role=None, ip=None,
                                  result=RESULT_SUCCESS,
                                  detail={"retentionDays": keep,
                                          "loginAttempts": res.login_attempts,
                                          "sessions": res.sessions,
                                          "recoveryCodes": res.recovery_codes})
            conn.execute(text("""
                UPDATE maintenance_runs
                   SET finished_at = now(), examined_count = :n, deleted_count = :n,
                       outcome = :o
                 WHERE job = :job AND run_date = :d
            """), {"job": JOB, "d": today, "n": res.total, "o": res.reason})
    return res
