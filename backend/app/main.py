"""Trust Server Manager — API.

SCHELETRO. Espone solo /api/health e /api/ready.
Non ci sono, deliberatamente e in commit separati (BACKEND-PLAN.md §9):
  - autenticazione e sessioni (§8.1, §8.6)
  - motore di diff identity-aware (§8.10)
  - persistenza dell'inventario e commit atomico (§8.11)
"""
import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(
    title="Trust Server Manager API",
    version=get_settings().app_version,
    # La UI di OpenAPI resta accessibile solo perché non c'è ancora niente da proteggere.
    # Da chiudere (o mettere dietro auth) nello stesso commit in cui arrivano gli endpoint
    # reali: elenca l'intera superficie dell'API a chiunque la raggiunga.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.include_router(health_router, prefix="/api", tags=["operations"])
