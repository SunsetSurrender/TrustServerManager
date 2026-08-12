#!/usr/bin/env python3
"""Stato del worker delle notifiche, per l'healthcheck e per il monitoraggio.

    python scripts/worker_health.py [--max-age-seconds N] [--json]

Esce 0 se il worker ha battuto di recente ed è in uno stato sano, 1 altrimenti.

Perché NON sta in `/api/ready`
------------------------------
`/api/ready` risponde alla domanda «l'API può servire richieste?». Il worker non
c'entra: con il worker fermo l'applicazione resta perfettamente usabile — non
partono gli avvisi, che è un guasto diverso e va segnalato diversamente. Legarli
significherebbe che un worker fermo fa togliere l'API dal bilanciatore, cioè
trasformare un problema di notifiche in un'interruzione di servizio (§8.41).

Il battito passa dal DATABASE e non da un file: così il monitoraggio può
guardarlo da fuori dal container, e leggerlo prova anche che il worker vede il
database.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text                                  # noqa: E402

from app.db import get_engine                                # noqa: E402

#: Il worker batte ogni 300 s (`TICK_SECONDS`). La soglia è più del doppio, così
#: un giro lento — una valutazione dell'inventario, un timeout SMTP — non fa
#: dichiarare morto un worker che sta lavorando.
DEFAULT_MAX_AGE = 900

#: Stati in cui il worker non sta facendo il suo lavoro.
UNHEALTHY_STATES = ("refused", "error", "stopped")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    gc_run = None
    try:
        with get_engine().connect() as conn:
            row = conn.execute(text("""
                SELECT last_tick_at, last_run_date, state, detail
                  FROM worker_heartbeat WHERE id IS TRUE
            """)).mappings().first()
            # Ultimo giro di GC delle foto (§8.5). Si RIPORTA ma non decide la
            # salute del container: la GC è un lavoro di manutenzione e un suo
            # ritardo non rende il worker inutilizzabile. Farla pesare qui vorrebbe
            # dire che un problema di spazio riavvia il processo che manda gli
            # avvisi — cioè trasformare un guasto in due.
            from app.photos.gc import last_run                # noqa: PLC0415
            gc_run = last_run(conn)
    except Exception as exc:
        print(f"battito non leggibile: {type(exc).__name__}", file=sys.stderr)
        return 1

    if row is None:
        print("riga del battito assente: migrazioni non applicate?", file=sys.stderr)
        return 1

    age = (datetime.now(timezone.utc) - row["last_tick_at"]).total_seconds()
    healthy = age <= args.max_age_seconds and row["state"] not in UNHEALTHY_STATES

    if args.json:
        print(json.dumps({
            "healthy": healthy,
            "state": row["state"],
            "ageSeconds": round(age, 1),
            "lastRunDate": row["last_run_date"].isoformat()
                           if row["last_run_date"] else None,
            "detail": row["detail"],
            "photoGc": {
                "lastRunDate": gc_run["run_date"].isoformat(),
                "examined": gc_run["examined_count"],
                "deleted": gc_run["deleted_count"],
                "outcome": gc_run["outcome"],
            } if gc_run else None,
        }))
    else:
        print(f"stato={row['state']} eta={age:.0f}s "
              f"ultimaEsecuzione={row['last_run_date']} "
              f"gcFoto={gc_run['run_date'] if gc_run else 'mai'} "
              f"{'sano' if healthy else 'NON SANO'}")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
