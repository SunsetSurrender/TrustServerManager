#!/usr/bin/env python3
"""Migrazioni + provisioning dei ruoli di runtime. Eseguito dal servizio `migrate`.

Gira come PROPRIETARIO dello schema. Due passi:

  1. `alembic upgrade head`
  2. imposta la password dei ruoli di runtime leggendola dai secret:
       `tsm_api`    → API      (/run/secrets/api_db_password)
       `tsm_worker` → worker   (/run/secrets/worker_db_password)

Il passo 2 non sta in una migrazione perché una migrazione finisce nel
repository e nell'immagine, e una password non deve stare in nessuno dei due. La
migrazione crea i ruoli senza password; qui gliela si dà, a ogni avvio, così una
rotazione è solo la sostituzione del file del secret più un riavvio.

Perché DUE ruoli e non uno
--------------------------
Il worker ha bisogno di `DELETE` su `photos` per la garbage collection delle foto
orfane (§8.5); l'API no. Con un ruolo unico quel privilegio finirebbe anche a chi
serve richieste HTTP, e un difetto in una rotta potrebbe cancellare byte che una
versione storica dell'inventario referenzia. Nessuno dei due riceve la password
del proprietario dello schema.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command                     # noqa: E402
from alembic.config import Config               # noqa: E402
from sqlalchemy import create_engine, text      # noqa: E402

from app.config import get_settings             # noqa: E402

#: (ruolo, file del secret, a cosa serve). L'ordine è quello in cui si stampa.
RUNTIME_ROLES = (
    ("tsm_api", Path("/run/secrets/api_db_password"), "l'API"),
    ("tsm_worker", Path("/run/secrets/worker_db_password"), "il worker"),
)


def _read_secret(path: Path, role: str, what: str) -> str | None:
    if not path.exists():
        print(f"secret {path} assente: password del ruolo {role} non impostata "
              f"({what} non potrà collegarsi)", file=sys.stderr)
        return None
    password = path.read_text(encoding="utf-8").strip()
    if not password:
        print(f"secret {path} vuoto: password del ruolo {role} non impostata",
              file=sys.stderr)
        return None
    return password


def main() -> int:
    print("applico le migrazioni ...", flush=True)
    command.upgrade(Config("alembic.ini"), "head")

    passwords: list[tuple[str, str]] = []
    for role, secret, what in RUNTIME_ROLES:
        password = _read_secret(secret, role, what)
        if password is None:
            # Si fallisce CHIUSO: un servizio che non può collegarsi al database
            # deve fermare il deployment, non partire e riavviarsi in ciclo con un
            # errore di autenticazione che sembra un problema di rete.
            return 1
        passwords.append((role, password))

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        with engine.begin() as conn:
            for role, password in passwords:
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
                                       '{role}', current_setting('tsm.newpw'));
                    END
                    $$;
                """))
                print(f"password del ruolo {role} impostata", flush=True)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
