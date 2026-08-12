"""Trust Server Manager — API.

Rotte attive: autenticazione, inventario (lettura e salvataggio), operative.

NON esposto via HTTP, di proposito:
  - il bootstrap dell'inventario, che è una CLI e non ha nemmeno il privilegio di
    database per inserire la riga di testa (§8.17, §8.19);
  - la cancellazione fisica delle utenze: non esiste (§8.6, §8.30);
  - la cancellazione fisica delle foto: la fa la garbage collection nel worker,
    con un ruolo di database che l'API non ha (§8.5).

Nessun accesso anonimo: ogni rotta dell'inventario dipende da `require_actor`,
che risponde 401 senza una sessione valida. Non esiste un ripiego di sviluppo che
conceda `admin` (§8.20).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.inventory import router as inventory_router
from app.api.notifications import router as notifications_router
from app.api.photos import router as photos_router
from app.api.request_context import origin_is_acceptable
from app.api.settings import router as settings_router
from app.api.users import router as users_router
from app.config import get_settings
from app.photos import MAX_UPLOAD_BYTES

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# get_settings() valida la configurazione e SOLLEVA se in produzione i cookie di
# sessione non sono `Secure` (§8.29). L'errore avviene qui, all'import: il
# processo non parte, invece di partire in modo insicuro.
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
async def validate_origin(request: Request, call_next):
    """Origine stessa per le richieste che modificano stato e portano il cookie.

    Non si abilita CORS con credenziali: non esiste un caso d'uso in cui un altro
    sito debba chiamare questa API col cookie dell'utente, e abilitarlo smonterebbe
    da solo `SameSite=strict`. Vedi §8.27.
    """
    ok, reason = origin_is_acceptable(request)
    if not ok:
        log.warning("origine rifiutata su %s %s: %s",
                    request.method, request.url.path, reason)
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"code": "origin_not_allowed",
                     "message": "origine della richiesta non consentita"},
            headers={"Cache-Control": "no-store"})
    return await call_next(request)


#: Spazio per l'involucro multipart: delimitatore, intestazioni della parte,
#: chiusura. Poche centinaia di byte in pratica; il margine è largo perché
#: sbagliarlo per difetto farebbe rifiutare un'immagine esattamente al limite, con
#: un errore che parla di dimensione mentre il file è dentro il limite dichiarato.
MULTIPART_OVERHEAD = 64 * 1024

#: Percorsi con un limite PROPRIO, più alto di quello generale.
#:
#: Il limite generale (5 MB) esiste per i documenti JSON, e un'immagine da 10 MB
#: non è un documento gonfio: è il caso previsto (§8.5). Alzare il limite generale
#: a 10 MB per far passare le foto allargherebbe la soglia anche per l'inventario,
#: dove i 4 MB di `MAX_DOCUMENT_BYTES` (§8.16) sono una decisione a sé. Meglio una
#: deroga per un percorso, dichiarata qui e verificabile, che una soglia unica che
#: nessuno sa più perché è quel numero.
LARGER_LIMITS = {"/api/photos": MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD}


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    """Limite di dimensione a livello applicativo.

    Nginx ha il suo `client_max_body_size`, ma quello vale solo per chi passa dal
    proxy. Questo vale sempre, anche per una richiesta che arriva direttamente
    all'API — che è esattamente lo scenario in cui il primo livello non aiuta.

    Si guarda `Content-Length` per rifiutare prima di leggere il corpo. Una
    richiesta senza `Content-Length` (chunked) viene lasciata passare qui e la
    fermano i limiti a valle: il documento oltre soglia è comunque respinto dalla
    validazione (§8.16), che risponde 413, e il caricamento di una foto legge il
    corpo con un tetto proprio invece di fidarsi dell'intestazione (§8.5).
    """
    limit = LARGER_LIMITS.get(request.url.path.rstrip("/"),
                              settings.max_request_bytes)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
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


@app.exception_handler(RequestValidationError)
async def malformed_body(request: Request,
                         exc: RequestValidationError) -> JSONResponse:
    """UN solo formato di errore su tutta l'API.

    Senza questo, un corpo JSON malformato riceve la forma di FastAPI
    (`detail` come elenco di oggetti con `loc`/`type`) mentre tutto il resto
    riceve la nostra (`{code, message}`): il client dovrebbe saper leggere due
    formati e indovinare quale aspettarsi a seconda di quale controllo scatta
    per primo. È lo stesso motivo per cui i parametri di `/api/audit` sono
    dichiarati come stringhe e validati dal nostro parser (§8.36).

    I dettagli di FastAPI non si riportano: `loc` e `input` contengono frammenti
    del corpo rifiutato, che è esattamente ciò che non si restituisce (§8.21).
    """
    log.info("corpo non valido su %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"code": "invalid_body",
                 "message": "corpo della richiesta non valido"},
        headers={"Cache-Control": "no-store"})


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
app.include_router(users_router, prefix="/api", tags=["users"])
app.include_router(audit_router, prefix="/api", tags=["audit"])
app.include_router(settings_router, prefix="/api", tags=["settings"])
app.include_router(notifications_router, prefix="/api", tags=["notifications"])
app.include_router(photos_router, prefix="/api", tags=["photos"])
