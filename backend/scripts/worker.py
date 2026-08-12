#!/usr/bin/env python3
"""Punto d'ingresso del worker delle notifiche.

    python scripts/worker.py

Non è una rotta HTTP e non lo diventerà: un endpoint che «fa partire le
notifiche» sarebbe un modo per mandare posta a comando, e la posta parte a
un'ora, non su richiesta. L'invio manuale di VERIFICA esiste già, è
`POST /api/notifications/test`, ed è limitato di proposito (§8.38).

Riferimento: BACKEND-PLAN.md §8.41.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.notifications.worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
