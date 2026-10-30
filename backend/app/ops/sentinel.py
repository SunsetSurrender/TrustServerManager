"""La sentinella: controlla lo stato ogni pochi minuti e avvisa (0.5.0).

Gira DENTRO l'API, in un thread, e non nel worker. Tre ragioni:

  - deve accorgersi anche del worker fermo: un controllo che vive nel processo
    che dovrebbe controllare tace proprio quando serve;
  - l'API ha già tutto quello che lo Stato del sistema legge (privilegi sulle
    tabelle, certificato TLS montato, uscita verso il relay);
  - il giro è breve (una manciata di query) e non tocca l'inventario.

Più processi dell'API possono avere la sentinella accesa: un lock consultivo di
PostgreSQL fa sì che un solo giro sia attivo alla volta, e lo stato vive nel
database (`system_alerts`), non in memoria. Un riavvio non fa ripartire da zero
né mandare doppioni.

Ogni giro fa anche la fotografia del giorno (`status_snapshots`) per lo storico.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import get_settings

log = logging.getLogger(__name__)

LOCK_KEY = 0x7473_6D73_656E        # "tsmsen"
TZ = ZoneInfo("Europe/Rome")
FIRST_RUN_DELAY = 60

_thread: threading.Thread | None = None
_stop = threading.Event()


def snapshot(conn, items: list[dict], now: datetime) -> None:
    """Una riga al giorno; i giri successivi la aggiornano con l'ultimo valore."""
    giorno = now.astimezone(TZ).date()
    d = conn.execute(text("""
        SELECT disk_total_bytes, disk_free_bytes FROM db_dumps
         WHERE finished_at IS NOT NULL AND disk_total_bytes > 0
         ORDER BY started_at DESC LIMIT 1""")).first()
    pct = (100 - round(100 * (d[1] or 0) / d[0])) if d else None
    conn.execute(text("""
        INSERT INTO status_snapshots (day, taken_at, db_bytes, disk_used_pct, disk_free_bytes,
                                      dump_bytes, devices, racks, active_users, failed_logins)
        VALUES (:day, :now, pg_database_size(current_database()), :pct, :free,
                (SELECT bytes FROM db_dumps WHERE ok ORDER BY started_at DESC LIMIT 1),
                (SELECT count(*) FROM inventory_devices),
                (SELECT count(*) FROM inventory_racks),
                (SELECT count(*) FROM users WHERE disabled_at IS NULL),
                (SELECT count(*) FROM login_attempts WHERE NOT success
                    AND ts > now() - interval '24 hours'))
        ON CONFLICT (day) DO UPDATE SET
            taken_at = EXCLUDED.taken_at, db_bytes = EXCLUDED.db_bytes,
            disk_used_pct = EXCLUDED.disk_used_pct, disk_free_bytes = EXCLUDED.disk_free_bytes,
            dump_bytes = EXCLUDED.dump_bytes, devices = EXCLUDED.devices,
            racks = EXCLUDED.racks, active_users = EXCLUDED.active_users,
            failed_logins = GREATEST(status_snapshots.failed_logins, EXCLUDED.failed_logins)
    """), {"day": giorno, "now": now, "pct": pct, "free": d[1] if d else None})


def run_once(engine: Engine, *, now: datetime | None = None, deliver=None) -> dict:
    """Un giro. Restituisce un riassunto (per i log e per i test)."""
    from app.api.system import _cert_expiry, collect
    from app.auth.audit import record_auth_event
    from app.notifications.smtp import SmtpSendFailed, deliver as smtp_deliver
    from app.ops import alerts as A

    deliver = deliver or smtp_deliver
    now = now or datetime.now(timezone.utc)
    s = get_settings()
    out = {"ran": False, "sent": False, "new": 0, "repeat": 0, "resolved": 0,
           "reminders": 0, "reason": ""}

    with engine.connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                 {"k": LOCK_KEY}).scalar():
            out["reason"] = "giro già in corso altrove"
            return out
        try:
            with engine.begin() as conn:
                items = collect(conn, now)
                snapshot(conn, items, now)
                cfg = A.load_config(conn)
                plan = A.evaluate(conn, items, now=now, cfg=cfg, cert_expiry=_cert_expiry())
                conn.execute(text("UPDATE system_alert_config SET last_run_at = :now "
                                  " WHERE id = 1"), {"now": now})
                dest = A.recipients(conn, cfg)
            out.update(ran=True, new=len(plan.new), repeat=len(plan.repeat),
                       resolved=len(plan.resolved), reminders=len(plan.reminders))

            if plan.empty():
                out["reason"] = "niente da segnalare"
                return out
            if not cfg["enabled"]:
                out["reason"] = "avvisi disattivati"
                return out
            if not dest:
                out["reason"] = "nessun destinatario"
                return out
            if not s.smtp_configured():
                out["reason"] = "invio email non configurato"
                return out
            ult, ok = cfg.get("lastSendAttemptAt"), cfg.get("lastSendOkAt")
            if ult and (ok is None or ult > ok) and now - ult < A.RETRY_AFTER:
                out["reason"] = "ultimo invio fallito da poco: si riprova più tardi"
                return out

            msg = A.build_message(s.smtp_sender, dest, plan, now=now,
                                  public_origin=s.public_origin)
            errore = None
            try:
                deliver(msg)
            except SmtpSendFailed as exc:
                errore = f"{exc.reason}"
            except Exception as exc:                     # noqa: BLE001
                errore = type(exc).__name__
            with engine.begin() as conn:
                if errore is None:
                    A.mark_sent(conn, plan, now=now)
                    conn.execute(text("""
                        UPDATE system_alert_config SET last_send_attempt_at = :now,
                               last_send_ok_at = :now, last_send_error = NULL WHERE id = 1"""),
                                 {"now": now})
                    record_auth_event(conn, A.SENT, username=A.ACTOR, detail={
                        "new": len(plan.new), "repeat": len(plan.repeat),
                        "resolved": len(plan.resolved), "reminders": len(plan.reminders),
                        "recipients": len(dest)})
                    out["sent"] = True
                else:
                    conn.execute(text("""
                        UPDATE system_alert_config SET last_send_attempt_at = :now,
                               last_send_error = :e WHERE id = 1"""),
                                 {"now": now, "e": errore[:200]})
                    record_auth_event(conn, A.FAILED, username=A.ACTOR, result="failure",
                                      detail={"reason": errore[:200]})
                    out["reason"] = f"invio non riuscito: {errore}"
            return out
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            lock_conn.commit()


def _loop(interval: int) -> None:                     # pragma: no cover
    from app.db import get_engine
    if _stop.wait(FIRST_RUN_DELAY):
        return
    while not _stop.is_set():
        try:
            r = run_once(get_engine())
            if r.get("sent") or (r.get("reason") or "").startswith("invio non"):
                log.info("sentinella: %s", r)
        except Exception:
            log.exception("giro della sentinella fallito")
        _stop.wait(interval)


def start() -> None:                                  # pragma: no cover
    global _thread
    s = get_settings()
    if not s.sentinel_enabled or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(s.sentinel_interval_seconds,),
                               name="tsm-sentinella", daemon=True)
    _thread.start()
    log.info("sentinella avviata (giro ogni %ds)", s.sentinel_interval_seconds)


def stop() -> None:                                   # pragma: no cover
    _stop.set()
