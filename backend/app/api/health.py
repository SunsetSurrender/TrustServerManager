"""Liveness e readiness. Sono due cose diverse e vanno tenute separate.

/api/health   liveness  — il processo è vivo e risponde. NON tocca il DB.
                          Se restituisse 503 a DB giù, l'orchestratore riavvierebbe
                          l'API per un guasto che non è dell'API.
/api/ready    readiness — le dipendenze sono utilizzabili. Tocca il DB.
                          È questo che il reverse proxy deve guardare per decidere
                          se mandare traffico.
"""
import logging

from fastapi import APIRouter, Response, status

from app.config import get_settings
from app.db import check_database

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", summary="Liveness: il processo risponde")
def health() -> dict:
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/ready", summary="Readiness: il database è raggiungibile")
def ready(response: Response) -> dict:
    try:
        check_database()
    except Exception as exc:
        # Il dettaglio va nei log, non nella risposta: la stringa di connessione
        # e la topologia interna non escono dall'API.
        log.warning("readiness fallita: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ready", "database": "ok"}
