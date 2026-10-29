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
        if d[4]:
            used = 100 - round(100 * (d[5] or 0) / d[4])
            st = "ok" if used < DISK_WARN else ("warn" if used < DISK_CRIT else "crit")
            v.append(_voce("Backup", "Disco dati (/srv/tsm-data)",
                           f"{used}% occupato · {_fmt_bytes(d[5])} liberi di {_fmt_bytes(d[4])}"
                           f" (misurato {_eta(d[0], now)})", st))

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
