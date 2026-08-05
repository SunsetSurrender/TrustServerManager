#!/usr/bin/env python3
"""Migrazioni + provisioning del ruolo di runtime. Eseguito dal servizio `migrate`.

Gira come PROPRIETARIO dello schema. Due passi:

  1. `alembic upgrade head`
  2. imposta la password del ruolo di runtime (`tsm_api`) leggendola dal secret

Il passo 2 non sta in una migrazione perché una migrazione finisce nel
repository e nell'immagine, e una password non deve stare in nessuno dei due. La
migrazione crea il ruolo senza password; qui gliela si dà, a ogni avvio, così una
rotazione è solo la sostituzione del file del secret più un riavvio.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command                     # noqa: E402
from alembic.config import Config               # noqa: E402
from sqlalchemy import create_engine, text      # noqa: E402

from app.config import get_settings             # noqa: E402

RUNTIME_ROLE = "tsm_api"
RUNTIME_SECRET = Path("/run/secrets/api_db_password")


def main() -> int:
    print("applico le migrazioni ...", flush=True)
    command.upgrade(Config("alembic.ini"), "head")

    if not RUNTIME_SECRET.exists():
        print(f"secret {RUNTIME_SECRET} assente: password del ruolo di runtime "
              "non impostata (l'API non potrà collegarsi)", file=sys.stderr)
        return 1

    password = RUNTIME_SECRET.read_text(encoding="utf-8").strip()
    if not password:
        print(f"secret {RUNTIME_SECRET} vuoto", file=sys.stderr)
        return 1

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        with engine.begin() as conn:
            # `ALTER ROLE ... PASSWORD` è un comando di utilità e NON accetta
            # parametri associati. Concatenare la password nel testo SQL sarebbe
            # l'alternativa ovvia e sbagliata: finirebbe nei log delle query e
            # sarebbe esposta a un valore malformato.
            #
            # `set_config` è una funzione, quindi il parametro funziona: si mette
            # la password in una variabile locale alla transazione e la si cita
            # con format('%L'), che è la quotatura di Postgres.
            conn.execute(text("SELECT set_config('tsm.newpw', :pw, true)"),
                         {"pw": password})
            conn.execute(text(f"""
                DO $$
                BEGIN
                    EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L',
                                   '{RUNTIME_ROLE}', current_setting('tsm.newpw'));
                END
                $$;
            """))
        print(f"password del ruolo {RUNTIME_ROLE} impostata", flush=True)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
