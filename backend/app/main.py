"""Trust Server Manager — API.

Rotte attive: autenticazione, inventario (lettura e salvataggio), operative.

NON esposto via HTTP, di proposito:
  - il bootstrap dell'inventario, che è una CLI e non ha nemmeno il privilegio di
    database per inserire la riga di testa (§8.17, §8.19);
  - la gestione delle utenze da parte degli admin, commit successivo.

Nessun accesso anonimo: ogni rotta dell'inventario dipende da `require_actor`,
che risponde 401 senza una sessione valida. Non esiste un ripiego di sviluppo che
conceda `admin` (§8.20).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.inventory import router as inventory_router
from app.config import get_settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(
    title="Trust Server Manager API",
    version=settings.app_version,
    # La UI di OpenAPI descrive l'intera superficie dell'API: dietro il proxy la
    # raggiunge solo chi è in rete interna, ma va comunque chiusa in produzione.
    docs_url="/api/docs" if settings.expose_docs else None,
    openapi_url="/api/openapi.json" if settings.expose_docs else None,
)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Limite di dimensione a livello applicativo.

    Nginx ha il suo `client_max_body_size`, ma quello vale solo per chi passa dal
    proxy. Questo vale sempre, anche per una richiesta che arriva direttamente
    all'API — che è esattamente lo scenario in cui il primo livello non aiuta.

    Si guarda `Content-Length` per rifiutare prima di leggere il corpo. Una
    richiesta senza `Content-Length` (chunked) viene lasciata passare qui e la
    fermano i limiti a valle: il documento oltre soglia è comunque respinto dalla
    validazione (§8.16), che risponde 413.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"code": "request_too_large",
                             "message": "richiesta troppo grande"},
                    headers={"Cache-Control": "no-store"})
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"code": "bad_content_length",
                         "message": "Content-Length non valido"},
                headers={"Cache-Control": "no-store"})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Ultima rete: nessun traceback e nessun dettaglio SQL nella risposta.

    Un errore di psycopg contiene frammenti di query e nomi di colonna; una
    traccia contiene percorsi del filesystem. Nei log servono, nella risposta no.
    """
    log.exception("errore non gestito su %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"code": "unavailable",
                 "message": "servizio temporaneamente non disponibile"},
        headers={"Cache-Control": "no-store"})


app.include_router(health_router, prefix="/api", tags=["operations"])
app.include_router(auth_router, prefix="/api", tags=["auth"])
app.include_router(inventory_router, prefix="/api", tags=["inventory"])
