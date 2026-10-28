#!/usr/bin/env python3
"""Sblocco di un'utenza dalla riga di comando del server. FUORI da HTTP.

Serve per un caso solo: l'unico amministratore è bloccato dopo troppi accessi
falliti, e nessuno può sbloccarlo dall'interfaccia. Negli altri casi lo sblocco
si fa da Profilo, «Utenti e privilegi».

Gira come PROPRIETARIO dello schema, dal servizio `migrate`:

    docker compose run --rm migrate python scripts/unlock_user.py <utenza>

Non cambia la password. Se il blocco nasce da tentativi di qualcun altro, dopo lo
sblocco conviene cambiarla.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text          # noqa: E402

from app.auth import lockout                        # noqa: E402
from app.config import get_settings                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Sblocca un'utenza bloccata")
    ap.add_argument("username")
    args = ap.parse_args()

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT id, locked_at, disabled_at FROM users WHERE username = :u"),
                {"u": args.username}).first()
            if row is None:
                print(f"utenza inesistente: {args.username}", file=sys.stderr)
                return 1
            if row[2] is not None:
                print(f"l'utenza {args.username} è DISATTIVATA, non bloccata: "
                      "la riattivazione si fa dall'interfaccia", file=sys.stderr)
                return 1
            if not lockout.unlock(conn, row[0], actor_username="(riga di comando)"):
                print(f"l'utenza {args.username} non era bloccata")
                return 0
    finally:
        engine.dispose()
    print(f"utenza {args.username} sbloccata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
