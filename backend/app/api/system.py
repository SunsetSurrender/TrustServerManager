"""«Stato del sistema» per gli amministratori (0.4.2).

    GET /api/system/status

Raccoglie in una risposta quello che oggi si vede solo da terminale o da Nagios:
versione, database e migrazioni, worker, giri di manutenzione, ultimo dump,
spazio sul disco dati (misurato dal servizio di backup), posta, certificato,
utenze. Sola lettura, nessun parametro.

Ogni voce ha uno `stato` fra `ok`, `warn`, `crit`, `info`, deciso QUI e non nel
browser: le soglie sono le stesse di `ops/nagios/check_tsm.sh`, e due posti che
giudicano la stessa cosa in modo diverso sono il modo per non fidarsi di nessuno.
"""
from __future__ import annotations

import ssl
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.api.health import expected_head_revision
from app.config import get_settings
from app.inventory import Actor

router = APIRouter()

#: Certificato servito da nginx, montato in sola lettura nel container dell'API
#: (solo la catena pubblica, mai la chiave).
CERT_PATH = Path("/run/tls/fullchain.pem")

DISK_WARN, DISK_CRIT = 75, 90          # come check_tsm.sh storage
SHRINK_RATIO = 0.5                     # dump più piccolo della metà del precedente
RESTORE_MAX_AGE_DAYS = 8               # la prova di ripristino è settimanale
FAILED_LOGINS_WARN = 30                # accessi falliti in 24 ore

#: Esiti del giro degli avvisi di scadenza: (parole, stato).
ESITI_SCADENZE = {
    "sent": ("avviso inviato", "ok"),
    "nothing_due": ("niente in scadenza", "ok"),
    "no_recipients_configured": ("nessun destinatario configurato", "warn"),
    "notifications_disabled": ("avvisi disattivati", "warn"),
    "disabled": ("avvisi disattivati", "warn"),
    "inventory_not_bootstrapped": ("inventario non inizializzato", "crit"),
    "failed": ("INVIO NON RIUSCITO", "crit"),
}
CERT_WARN_DAYS, CERT_CRIT_DAYS = 30, 7  # come check_tsm.sh cert


def _voce(gruppo: str, nome: str, valore: str, stato: str = "ok") -> dict:
    return {"gruppo": gruppo, "nome": nome, "valore": valore, "stato": stato}


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    v = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or u == "TB":
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= 1024
    return str(n)


#: Esiti dei giri di manutenzione, in parole.
ESITI = {"ok": "eseguito", "collected": "foto rimosse", "nothing_to_collect": "niente da pulire",
         "purged": "dati vecchi rimossi", "nothing_to_purge": "niente da pulire",
         "errore": "ERRORE"}


def _eta(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "mai"
    s = int((now - ts).total_seconds())
    if s < 120:
        return f"{s} s fa"
    if s < 7200:
        return f"{s // 60} min fa"
    if s < 172800:
        return f"{s // 3600} h fa"
    return f"{s // 86400} giorni fa"


def _cert_expiry() -> datetime | None:
    if not CERT_PATH.is_file():
        return None
    try:
        info = ssl._ssl._test_decode_cert(str(CERT_PATH))        # noqa: SLF001
        return datetime.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


def collect(conn: Connection, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    s = get_settings()
    v: list[dict] = [_voce("Applicazione", "Versione", s.app_version, "info")]

    # ---- database ----
    atteso = expected_head_revision()
    applicata = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    v.append(_voce("Database", "Migrazione", applicata or "—",
                   "ok" if applicata == atteso else "crit"))
    size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar()
    v.append(_voce("Database", "Dimensione", _fmt_bytes(size), "info"))
    inv = conn.execute(text(
        "SELECT h.version, v.created_at FROM inventory_head h "
        "  JOIN inventory_versions v ON v.version = h.version WHERE h.id IS TRUE")).first()
    v.append(_voce("Database", "Inventario",
                   f"revisione {inv[0]} · ultima modifica {_eta(inv[1], now)}" if inv else "assente",
                   "ok" if inv else "crit"))

    # ---- worker ----
    hb = conn.execute(text(
        "SELECT last_tick_at, state, detail FROM worker_heartbeat LIMIT 1")).first()
    if hb is None or hb[0] is None:
        v.append(_voce("Worker", "Battito", "mai ricevuto", "crit"))
    else:
        age = (now - hb[0]).total_seconds()
        st = "ok" if age < 600 and hb[1] != "error" else ("warn" if age < 1800 else "crit")
        v.append(_voce("Worker", "Battito", f"{_eta(hb[0], now)} · {hb[1]}", st))

    # ---- manutenzione notturna ----
    nomi = {"photo_gc": "Pulizia foto orfane", "auth_purge": "Pulizia dati di accesso",
            "db_dump": "Dump del database"}
    for job, nome in nomi.items():
        r = conn.execute(text(
            "SELECT run_date, finished_at, outcome FROM maintenance_runs "
            " WHERE job = :j AND finished_at IS NOT NULL "
            " ORDER BY run_date DESC LIMIT 1"), {"j": job}).first()
        if r is None:
            v.append(_voce("Manutenzione", nome, "mai eseguita", "warn"))
        else:
            giorni = (now.date() - r[0]).days
            v.append(_voce("Manutenzione", nome,
                           f"{r[0].isoformat()} · {ESITI.get(r[2] or '', r[2] or '—')}",
                           "ok" if giorni <= 1 else "warn"))

    # ---- backup e disco dati ----
    d = conn.execute(text(
        "SELECT finished_at, ok, file, bytes, disk_total_bytes, disk_free_bytes, kept, message "
        "  FROM db_dumps WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1")).first()
    if d is None:
        v.append(_voce("Backup", "Ultimo dump", "nessuno ancora", "warn"))
    else:
        ok_age = (now - d[0]).total_seconds() < 36 * 3600
        v.append(_voce("Backup", "Ultimo dump",
                       f"{_eta(d[0], now)} · {_fmt_bytes(d[3])} · {d[2] or '—'}"
                       + ("" if d[1] else f" · ERRORE: {d[7] or ''}"),
                       "ok" if d[1] and ok_age else ("warn" if d[1] else "crit")))
        v.append(_voce("Backup", "Dump conservati", str(d[6] or 0), "info"))
        # 0.5.0: un dump molto più piccolo del precedente è il primo indizio di
        # dati spariti (o di un dump troncato che pg_restore legge comunque).
        prev = conn.execute(text(
            "SELECT bytes FROM db_dumps WHERE ok AND bytes IS NOT NULL "
            " ORDER BY started_at DESC OFFSET 1 LIMIT 1")).scalar()
        if d[1] and d[3] and prev and d[3] < prev * SHRINK_RATIO:
            v.append(_voce("Backup", "Dimensione del dump",
                           f"{_fmt_bytes(d[3])}, il precedente era {_fmt_bytes(prev)}: "
                           f"controllare che non manchino dati", "warn"))
        if d[4]:
            used = 100 - round(100 * (d[5] or 0) / d[4])
            st = "ok" if used < DISK_WARN else ("warn" if used < DISK_CRIT else "crit")
            v.append(_voce("Backup", "Disco dati (/srv/tsm-data)",
                           f"{used}% occupato · {_fmt_bytes(d[5])} liberi di {_fmt_bytes(d[4])}"
                           f" (misurato {_eta(d[0], now)})", st))
    rt = conn.execute(text(
        "SELECT restore_checked_at, restore_ok, restore_rows, restore_message "
        "  FROM db_dumps WHERE restore_checked_at IS NOT NULL "
        " ORDER BY restore_checked_at DESC LIMIT 1")).first()
    if rt is None:
        v.append(_voce("Backup", "Prova di ripristino", "non ancora eseguita", "info"))
    else:
        vecchia = (now - rt[0]).total_seconds() > RESTORE_MAX_AGE_DAYS * 86400
        v.append(_voce("Backup", "Prova di ripristino",
                       (f"{_eta(rt[0], now)} · riuscita, {rt[2] or 0} "
                        f"{'versione' if rt[2] == 1 else 'versioni'} dell'inventario"
                        if rt[1] else f"{_eta(rt[0], now)} · FALLITA: {rt[3] or ''}"),
                       "crit" if not rt[1] else ("warn" if vecchia else "ok")))

    # ---- posta e certificato ----
    v.append(_voce("Servizi", "Invio email (SMTP)",
                   "configurato" if s.smtp_configured() else "non configurato",
                   "ok" if s.smtp_configured() else "warn"))
    exp = _cert_expiry()
    if exp is None:
        v.append(_voce("Servizi", "Certificato TLS", "non leggibile dall'API", "info"))
    else:
        giorni = (exp - now).days
        st = "ok" if giorni > CERT_WARN_DAYS else ("warn" if giorni > CERT_CRIT_DAYS else "crit")
        v.append(_voce("Servizi", "Certificato TLS",
                       f"scade il {exp.date().isoformat()} ({giorni} giorni)", st))

    # ---- avvisi (0.5.0) ----
    run = conn.execute(text(
        "SELECT run_date, outcome FROM scheduler_runs WHERE finished_at IS NOT NULL "
        " ORDER BY run_date DESC LIMIT 1")).first()
    if run is None:
        v.append(_voce("Avvisi", "Scadenze: ultimo giro", "mai eseguito", "info"))
    else:
        esito = run[1] or ""
        parole, st = ESITI_SCADENZE.get(esito.split(":")[0], (esito or "—", "info"))
        v.append(_voce("Avvisi", "Scadenze: ultimo giro",
                       f"{run[0].isoformat()} · {parole}", st))
    cfg = conn.execute(text(
        "SELECT enabled, include_admins, cardinality(extra_recipients), last_send_error, "
        "       last_send_attempt_at, last_send_ok_at FROM system_alert_config WHERE id = 1")).first()
    if cfg is not None:
        admins = conn.execute(text(
            "SELECT count(*) FROM users WHERE role = 'admin' AND disabled_at IS NULL "
            "   AND coalesce(email, '') <> ''")).scalar() if cfg[1] else 0
        dest = (admins or 0) + (cfg[2] or 0)
        if not cfg[0]:
            v.append(_voce("Avvisi", "Avvisi di sistema", "disattivati", "warn"))
        elif dest == 0:
            v.append(_voce("Avvisi", "Avvisi di sistema",
                           "nessun destinatario: nessuno verrebbe avvisato", "warn"))
        elif cfg[3] and (cfg[5] is None or cfg[4] > cfg[5]):
            v.append(_voce("Avvisi", "Avvisi di sistema",
                           f"ultimo invio non riuscito: {cfg[3]}", "warn"))
        else:
            v.append(_voce("Avvisi", "Avvisi di sistema",
                           f"attivi · {dest} {'destinatario' if dest == 1 else 'destinatari'}", "ok"))

    # ---- accessi (0.5.0) ----
    fail = conn.execute(text(
        "SELECT count(*) FROM login_attempts WHERE NOT success "
        "   AND ts > now() - interval '24 hours'")).scalar() or 0
    v.append(_voce("Accessi", "Accessi falliti (24 h)", str(fail),
                   "warn" if fail >= FAILED_LOGINS_WARN else "info"))

    # ---- utenze ----
    u = conn.execute(text("""
        SELECT count(*) FILTER (WHERE disabled_at IS NULL),
               count(*) FILTER (WHERE disabled_at IS NULL AND role = 'admin'),
               count(*) FILTER (WHERE disabled_at IS NULL AND locked_at IS NOT NULL),
               count(*) FILTER (WHERE disabled_at IS NULL AND must_change_pw),
               count(*) FILTER (WHERE disabled_at IS NULL AND (email IS NULL OR email = ''))
          FROM users""")).first()
    v.append(_voce("Utenze", "Attive",
                   f"{u[0]} (di cui {u[1]} {'amministratore' if u[1] == 1 else 'amministratori'})",
                   "ok" if u[1] >= 2 else "warn"))
    v.append(_voce("Utenze", "Bloccate", str(u[2]), "warn" if u[2] else "ok"))
    v.append(_voce("Utenze", "Con password provvisoria", str(u[3]), "info"))
    v.append(_voce("Utenze", "Senza email (niente recupero)", str(u[4]), "warn" if u[4] else "ok"))
    sess = conn.execute(text(
        "SELECT count(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > now()")).scalar()
    v.append(_voce("Utenze", "Sessioni aperte", str(sess), "info"))
    online = conn.execute(text(
        "SELECT count(DISTINCT user_id) FROM sessions WHERE revoked_at IS NULL "
        "   AND expires_at > now() AND presence_at > now() - interval '2 minutes'")).scalar()
    v.append(_voce("Utenze", "Collegati adesso", str(online or 0), "info"))
    locks = conn.execute(text(
        "SELECT count(*) FROM edit_locks WHERE expires_at > now()")).scalar()
    v.append(_voce("Utenze", "Rack in modifica", str(locks or 0), "info"))
    return v


@router.get("/system/status", summary="Stato del sistema (solo amministratori)")
def system_status(response: Response, conn: Connection = Depends(get_connection),
                  actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)
    now = datetime.now(timezone.utc)
    voci = collect(conn, now)
    peggiore = ("crit" if any(x["stato"] == "crit" for x in voci)
                else "warn" if any(x["stato"] == "warn" for x in voci) else "ok")
    return {"generatedAt": now.isoformat(), "overall": peggiore, "items": voci}


# ================================================================ 0.5.0
#
#   GET  /api/system/alerts        configurazione degli avvisi + destinatari effettivi
#   PUT  /api/system/alerts        salva la configurazione
#   POST /api/system/alerts/test   manda una prova ai destinatari
#   GET  /api/system/diagnostics   storico, dump, avvisi, accessi, blocchi

TEST_MIN_INTERVAL_SECONDS = 60


def _cfg_out(conn: Connection) -> dict:
    from app.ops import alerts as A
    c = A.load_config(conn)
    dest = A.recipients(conn, c)
    iso = lambda t: t.isoformat() if t else None      # noqa: E731
    return {"enabled": c["enabled"], "includeAdmins": c["includeAdmins"],
            "extraRecipients": c["extraRecipients"], "minLevel": c["minLevel"],
            "repeatHours": c["repeatHours"], "recipients": dest,
            "smtpConfigured": get_settings().smtp_configured(),
            "lastRunAt": iso(c.get("lastRunAt")), "lastSendOkAt": iso(c.get("lastSendOkAt")),
            "lastSendAttemptAt": iso(c.get("lastSendAttemptAt")),
            "lastSendError": c.get("lastSendError"),
            "updatedAt": iso(c.get("updatedAt")), "updatedBy": c.get("updatedBy")}


@router.get("/system/alerts", summary="Avvisi di sistema: configurazione (amministratori)")
def get_alerts(response: Response, conn: Connection = Depends(get_connection),
               actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)
    return _cfg_out(conn)


@router.put("/system/alerts", summary="Avvisi di sistema: salva (amministratori)")
def put_alerts(payload: dict, response: Response, conn: Connection = Depends(get_connection),
               actor: Actor = Depends(require_admin)) -> dict:
    from fastapi import HTTPException

    from app.auth.audit import record_auth_event
    from app.ops import alerts as A
    response.headers.update(NO_STORE)
    try:
        A.save_config(conn, payload or {}, by=actor.username)
    except A.ConfigRejected as exc:
        raise HTTPException(422, detail={"code": "alerts_config_rejected",
                                         "field": exc.field, "message": str(exc)},
                            headers=NO_STORE) from None
    c = A.load_config(conn)
    record_auth_event(conn, "system.alerts.config_changed", username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"enabled": c["enabled"], "includeAdmins": c["includeAdmins"],
                              "extra": len(c["extraRecipients"]), "minLevel": c["minLevel"],
                              "repeatHours": c["repeatHours"]})
    return _cfg_out(conn)


@router.post("/system/alerts/test", summary="Avvisi di sistema: prova (amministratori)")
def test_alerts(response: Response, conn: Connection = Depends(get_connection),
                actor: Actor = Depends(require_admin)) -> dict:
    from fastapi import HTTPException

    from app.auth.audit import record_auth_event
    from app.notifications.smtp import SmtpSendFailed, deliver
    from app.ops import alerts as A
    response.headers.update(NO_STORE)
    s = get_settings()

    def no(code: str, msg: str, st: int = 409):
        return HTTPException(st, detail={"code": code, "message": msg}, headers=NO_STORE)

    if not s.smtp_configured():
        raise no("smtp_not_configured", "l'invio email non è configurato sul server")
    c = A.load_config(conn)
    dest = A.recipients(conn, c)
    if not dest:
        raise no("no_recipients", "nessun destinatario: aggiungi un indirizzo o un'email "
                                  "agli amministratori")
    ok = conn.execute(text("""
        UPDATE system_alert_config SET last_test_at = now()
         WHERE id = 1 AND (last_test_at IS NULL
               OR last_test_at < now() - make_interval(secs => :s))
        RETURNING 1"""), {"s": TEST_MIN_INTERVAL_SECONDS}).first()
    if ok is None:
        raise no("test_too_soon", "una prova al minuto: riprova fra poco", 429)
    aperti = conn.execute(text("""
        SELECT id, key, level, title, detail FROM system_alerts
         WHERE kind = 'problem' AND resolved_at IS NULL ORDER BY first_seen_at""")).all()
    now = datetime.now(timezone.utc)
    msg = A.build_message(s.smtp_sender, dest, A.Plan(), now=now, test=True,
                          public_origin=s.public_origin, open_problems=aperti)
    try:
        deliver(msg)
    except SmtpSendFailed as exc:
        record_auth_event(conn, "system.alerts.test", username=actor.username,
                          user_id=actor.user_id, role=actor.role, ip=actor.ip,
                          result="failure", detail={"reason": exc.reason})
        raise no("send_failed", f"invio non riuscito ({exc.reason})", 502)
    record_auth_event(conn, "system.alerts.test", username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"recipients": len(dest)})
    return {"sent": True, "recipients": dest}


@router.get("/system/diagnostics", summary="Diagnostica (amministratori)")
def diagnostics(response: Response, conn: Connection = Depends(get_connection),
                actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)
    iso = lambda t: t.isoformat() if t else None      # noqa: E731

    storia = [{"day": r[0].isoformat(), "dbBytes": r[1], "diskUsedPct": r[2],
               "diskFreeBytes": r[3], "dumpBytes": r[4], "devices": r[5], "racks": r[6],
               "activeUsers": r[7], "failedLogins": r[8]}
              for r in conn.execute(text("""
                  SELECT day, db_bytes, disk_used_pct, disk_free_bytes, dump_bytes, devices,
                         racks, active_users, failed_logins
                    FROM status_snapshots WHERE day > current_date - 90 ORDER BY day""")).all()]

    dumps = [{"startedAt": iso(r[0]), "ok": r[1], "file": r[2], "bytes": r[3],
              "tocEntries": r[4], "restoreOk": r[5], "restoreRows": r[6],
              "restoreMessage": r[7], "message": r[8]}
             for r in conn.execute(text("""
                 SELECT started_at, ok, file, bytes, toc_entries, restore_ok, restore_rows,
                        restore_message, message
                   FROM db_dumps WHERE finished_at IS NOT NULL
                  ORDER BY started_at DESC LIMIT 14""")).all()]

    avvisi = [{"key": r[0], "kind": r[1], "level": r[2], "title": r[3], "detail": r[4],
               "firstSeenAt": iso(r[5]), "lastSeenAt": iso(r[6]), "notifiedAt": iso(r[7]),
               "resolvedAt": iso(r[8]), "open": r[8] is None and r[1] == "problem"}
              for r in conn.execute(text("""
                  SELECT key, kind, level, title, detail, first_seen_at, last_seen_at,
                         notified_at, resolved_at
                    FROM system_alerts
                   ORDER BY (resolved_at IS NULL AND kind = 'problem') DESC,
                            last_seen_at DESC LIMIT 30""")).all()]

    def conta(where: str, h: int) -> int:
        return conn.execute(text(
            f"SELECT count(*) FROM login_attempts WHERE {where} "
            f"   AND ts > now() - make_interval(hours => :h)"), {"h": h}).scalar() or 0

    top_u = [{"username": r[0], "count": r[1]} for r in conn.execute(text("""
        SELECT username::text, count(*) FROM login_attempts
         WHERE NOT success AND ts > now() - interval '7 days'
         GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5""")).all()]
    top_ip = [{"ip": str(r[0]) if r[0] else "—", "count": r[1]} for r in conn.execute(text("""
        SELECT host(ip), count(*) FROM login_attempts
         WHERE NOT success AND ts > now() - interval '7 days'
         GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5""")).all()]
    blocchi = conn.execute(text("""
        SELECT count(*) FROM audit WHERE action = 'auth.login.blocked'
           AND ts > now() - interval '7 days'""")).scalar()

    giri = [{"runDate": r[0].isoformat(), "outcome": r[1], "due": r[2], "sent": r[3],
             "finishedAt": iso(r[4])}
            for r in conn.execute(text("""
                SELECT run_date, outcome, due_count, sent_count, finished_at
                  FROM scheduler_runs ORDER BY run_date DESC LIMIT 10""")).all()]

    from app.api.presence import state as presence_state
    pres = presence_state(conn, user_id=actor.user_id)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "history": storia, "dumps": dumps, "alerts": avvisi,
        "access": {"failed24h": conta("NOT success", 24), "ok24h": conta("success", 24),
                   "failed7d": conta("NOT success", 168), "ok7d": conta("success", 168),
                   "blocked7d": blocchi, "topUsers": top_u, "topIps": top_ip},
        "expiryRuns": giri, "online": pres["online"], "locks": pres["locks"],
    }

