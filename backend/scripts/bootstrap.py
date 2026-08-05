#!/usr/bin/env python3
"""Bootstrap dell'inventario e del primo amministratore. FUORI da HTTP.

Non è una rotta e non lo diventerà: l'API non ha nemmeno il privilegio di
database per inserire la riga di testa (§8.19). La differenza fra «popolo un
database vuoto» e «accetto una scrittura» non va affidata a un parametro di una
richiesta.

Gira come PROPRIETARIO dello schema, non come ruolo di runtime.

Uso:
    python scripts/bootstrap.py --seed ../fixtures/seed.json --admin admin
    python scripts/bootstrap.py --seed <file> --admin <utente> --from-legacy

La password del primo amministratore si legge da TSM_BOOTSTRAP_PASSWORD, oppure
viene generata e stampata una volta sola. In entrambi i casi è provvisoria:
`must_change_pw` è impostato e il primo accesso obbliga a cambiarla (§8.1).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text          # noqa: E402

from app.auth.service import count_active_admins, create_user   # noqa: E402
from app.config import get_settings                             # noqa: E402
from app.inventory import (                                     # noqa: E402
    Actor,
    AlreadyBootstrappedError,
    InventoryRepository,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", required=True,
                    help="documento JSON iniziale dell'inventario")
    ap.add_argument("--admin", default="admin",
                    help="username del primo amministratore")
    ap.add_argument("--from-legacy", action="store_true",
                    help="consuma e rimuove le radici legacy (utenti, registro, "
                         "notifiche, smtp, versione): §8.16")
    args = ap.parse_args()

    doc = json.loads(Path(args.seed).read_text(encoding="utf-8"))

    password = os.environ.get("TSM_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(18)
    generated = "TSM_BOOTSTRAP_PASSWORD" not in os.environ

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        with engine.begin() as conn:
            actor = Actor(username=args.admin, role="admin")

            if count_active_admins(conn) == 0:
                user_id = create_user(conn, args.admin, password, "admin",
                                      must_change_pw=True)
                print(f"amministratore creato: {args.admin}")
                if generated:
                    print(f"password provvisoria: {password}")
                    print("  → va cambiata al primo accesso, e non verrà mostrata di nuovo")
                actor = Actor(username=args.admin, role="admin", user_id=user_id)
            else:
                print("amministratori già presenti: nessuna utenza creata")
                row = conn.execute(text(
                    "SELECT id, username FROM users WHERE role='admin' "
                    "AND disabled_at IS NULL ORDER BY created_at LIMIT 1")).one()
                actor = Actor(username=str(row[1]), role="admin", user_id=row[0])

            repo = InventoryRepository(conn)
            try:
                result = repo.bootstrap(doc, actor, from_legacy=args.from_legacy)
                print(f"inventario inizializzato alla versione {result.version}")
            except AlreadyBootstrappedError:
                print(f"inventario già inizializzato (versione {repo.head_version()}): "
                      "nessuna modifica")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
