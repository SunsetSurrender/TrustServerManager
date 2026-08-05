"""Traduzione degli errori del repository in risposte HTTP.

Un solo posto per la mappa, così una rotta nuova non inventa i propri stati.

Nessuna risposta espone: traceback, testo di errori SQL, o il contenuto del
documento rifiutato. Il documento può contenere l'inventario di un cliente, e i
codici di errore bastano al client per spiegarsi con l'utente; il dettaglio
completo va nei log del server, dove serve a chi opera e non a chi sonda.

Riferimento: BACKEND-PLAN.md §8.21.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status

from app.auth.service import AuthError, InvalidCredentials, NotAuthenticated
from app.inventory import (
    AlreadyBootstrappedError,
    DocumentRejectedError,
    IdentityRejectedError,
    InventoryError,
    NotAuthorizedError,
    NotBootstrappedError,
    VersionConflictError,
)

log = logging.getLogger(__name__)

NO_STORE = {"Cache-Control": "no-store"}

#: Chiavi dei dettagli che è sicuro restituire. Tutto il resto resta nei log: i
#: messaggi degli errori di documento citano nomi di campo e valori, che possono
#: venire dall'inventario.
_SAFE_DETAIL_KEYS = ("code", "path", "entity", "event", "scope", "requiredRole",
                     "uid", "kind", "found", "expected")


def _sanitise(details: Any) -> list[dict]:
    """Solo campi strutturali: codice e posizione, non contenuti."""
    if not isinstance(details, list):
        return []
    out = []
    for d in details:
        if isinstance(d, dict):
            out.append({k: d[k] for k in _SAFE_DETAIL_KEYS if k in d})
    return out


def http_error_for(exc: Exception) -> HTTPException:
    """Mappa un errore di dominio su una HTTPException. Non solleva."""

    # --- 401 ---
    if isinstance(exc, (NotAuthenticated, InvalidCredentials)):
        return HTTPException(status.HTTP_401_UNAUTHORIZED,
                             detail={"code": exc.code,
                                     "message": "autenticazione richiesta"},
                             headers=NO_STORE)

    # --- 403 ---
    if isinstance(exc, NotAuthorizedError):
        return HTTPException(status.HTTP_403_FORBIDDEN,
                             detail={"code": exc.code,
                                     "message": "modifica non consentita per il ruolo",
                                     "violations": _sanitise(exc.details)},
                             headers=NO_STORE)

    # --- 409 ---
    if isinstance(exc, VersionConflictError):
        return HTTPException(status.HTTP_409_CONFLICT,
                             detail={"code": exc.code,
                                     "message": "l'inventario è stato modificato "
                                                "da un'altra sessione",
                                     "currentVersion": exc.current_version,
                                     "currentSha256": exc.current_sha256},
                             headers=NO_STORE)
    if isinstance(exc, AlreadyBootstrappedError):
        return HTTPException(status.HTTP_409_CONFLICT,
                             detail={"code": exc.code,
                                     "message": "inventario già inizializzato"},
                             headers=NO_STORE)

    # --- 413 / 422 ---
    if isinstance(exc, DocumentRejectedError):
        codes = {d.get("code") for d in exc.details if isinstance(d, dict)}
        if "document_too_large" in codes:
            return HTTPException(413,
                                 detail={"code": "document_too_large",
                                         "message": "documento troppo grande"},
                                 headers=NO_STORE)
        return HTTPException(422,
                             detail={"code": exc.code,
                                     "message": "documento non accettabile",
                                     "problems": _sanitise(exc.details)},
                             headers=NO_STORE)
    if isinstance(exc, IdentityRejectedError):
        return HTTPException(422,
                             detail={"code": exc.code,
                                     "message": "transizione di identità non accettabile",
                                     "problems": _sanitise(exc.details)},
                             headers=NO_STORE)

    # --- 503 ---
    if isinstance(exc, NotBootstrappedError):
        # Non è colpa del client: il servizio non è pronto a servire l'inventario.
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                             detail={"code": exc.code,
                                     "message": "inventario non inizializzato"},
                             headers=NO_STORE)

    if isinstance(exc, AuthError):
        return HTTPException(status.HTTP_403_FORBIDDEN,
                             detail={"code": exc.code, "message": exc.message},
                             headers=NO_STORE)

    if isinstance(exc, InventoryError):
        log.exception("errore di inventario non mappato: %s", exc.code)
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                             detail={"code": "unavailable",
                                     "message": "servizio temporaneamente non disponibile"},
                             headers=NO_STORE)

    # Qualunque altra cosa: il dettaglio va nei log, non nella risposta. Un errore
    # di psycopg contiene frammenti di query e nomi di colonna.
    log.exception("errore non gestito")
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                         detail={"code": "unavailable",
                                 "message": "servizio temporaneamente non disponibile"},
                         headers=NO_STORE)
