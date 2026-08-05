"""Liveness e readiness. Sono due cose diverse e vanno tenute separate.

/api/health   liveness  — il processo è vivo e risponde. NON tocca il DB.
                          Se restituisse 503 a DB giù, l'orchestratore riavvierebbe
                          l'API per un guasto che non è dell'API.
/api/ready    readiness — il servizio può servire richieste. Tocca il DB.
                          È questo che il reverse proxy deve guardare.

Da quando esistono le rotte dell'inventario, «pronto» vuol dire tre cose insieme
(§8.23): database raggiungibile, migrazioni al livello atteso, e testa
dell'inventario presente. Un'istanza che risponde 200 con lo schema vecchio o
senza inventario inizializzato manderebbe in errore ogni richiesta, e sarebbe un
guasto molto più difficile da diagnosticare di un 503 sincero.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)
router = APIRouter()

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def expected_head_revision() -> str | None:
    """Revisione attesa, ricavata dai file della migrazione.

    Si legge dal filesystem e non da una costante scritta a mano: una costante si
    dimentica di aggiornare, e allora la readiness direbbe «pronto» con lo schema
    sbagliato — cioè esattamente il caso che deve segnalare.
    """
    files = sorted(_MIGRATIONS.glob("[0-9]*.py"))
    if not files:
        return None
    for line in files[-1].read_text(encoding="utf-8").splitlines():
        if line.startswith("revision:"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


@router.get("/health", summary="Liveness: il processo risponde")
def health() -> dict:
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/ready", summary="Readiness: DB, migrazioni e inventario")
def ready(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    checks = {"database": False, "migrations": False, "inventory": False}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = True

            expected = expected_head_revision()
            applied = conn.execute(
                text("SELECT version_num FROM alembic_version")).scalar()
            checks["migrations"] = bool(expected) and applied == expected

            head = conn.execute(
                text("SELECT version FROM inventory_head WHERE id IS TRUE")).scalar()
            checks["inventory"] = head is not None
    except Exception as exc:
        # Il dettaglio nei log: la stringa di connessione e la topologia interna
        # non escono dall'API.
        log.warning("readiness fallita: %s", exc)

    if all(checks.values()):
        return {"status": "ready", **{k: "ok" for k in checks}}

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "unavailable",
            **{k: ("ok" if v else "not-ready") for k, v in checks.items()}}
