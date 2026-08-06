"""Dipendenze delle rotte: connessione e ATTORE AUTENTICATO OBBLIGATORIO.

Non esiste un ripiego di sviluppo che conceda `admin` senza autenticazione. Quel
ripiego è pericoloso proprio perché funziona: sopravvive ai refactoring, non fa
fallire nessun test, e il giorno in cui una variabile d'ambiente è impostata male
diventa un accesso amministrativo anonimo. Il prototipo aveva già un difetto della
stessa forma — `_doLogin` concedeva `admin` quando l'elenco utenze era vuoto — e va
rimosso, non riprodotto sul server.

`require_actor` in esecuzione richiede una sessione valida e risponde 401
altrimenti. Nei test si sostituisce con `app.dependency_overrides`, che è
esplicito, locale al test e impossibile da attivare per errore in produzione.

Riferimento: BACKEND-PLAN.md §8.20, §8.26.
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.engine import Connection

from app.api.request_context import client_ip
from app.auth.service import (
    AuthenticatedUser,
    NotAuthenticated,
    resolve_session,
)
from app.db import get_engine
from app.inventory import Actor

SESSION_COOKIE = "tsm_session"

NO_STORE = {"Cache-Control": "no-store"}

#: Codice sul filo. Tutti i codici di errore sono snake_case minuscolo:
#: un vocabolario con due convenzioni si sbaglia a scrivere (§8.21).
PASSWORD_CHANGE_REQUIRED = "password_change_required"


def get_connection() -> Iterator[Connection]:
    """Una connessione con una transazione per richiesta.

    La transazione è della richiesta: se l'handler solleva, non resta scritto
    niente. È ciò che rende atomico il salvataggio dell'inventario senza che il
    repository debba gestire il commit.
    """
    engine = get_engine()
    with engine.connect() as conn:
        with conn.begin():
            yield conn


def current_user(request: Request,
                 conn: Connection = Depends(get_connection)) -> AuthenticatedUser:
    """Utente della sessione, con lo stato RILETTO dal database (§8.26).

    `resolve_session` non si fida di nulla che sia stato memorizzato al login:
    ruolo, disattivazione e obbligo di cambio password vengono riletti a ogni
    richiesta. La sessione dice *chi* è; non dice cosa può fare.
    """
    token = request.cookies.get(SESSION_COOKIE)
    try:
        return resolve_session(conn, token)
    except NotAuthenticated as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "autenticazione richiesta"},
            headers=NO_STORE,
        ) from None


def require_actor(request: Request,
                  user: AuthenticatedUser = Depends(current_user)) -> Actor:
    """L'attore per le operazioni normali. 401 se non autenticato.

    Con una password provvisoria la sessione è **valida ma ristretta** (§8.26):
    esiste, `/auth/me` la vede, e serve solo a cambiare la password. Qualunque
    altro endpoint risponde 403 `PASSWORD_CHANGE_REQUIRED`.

    La restrizione è strutturale, non un elenco di percorsi: gli unici tre
    endpoint raggiungibili sono quelli che NON dipendono da `require_actor`
    (`/auth/me`, `/auth/password`, `/auth/logout`). Un endpoint nuovo è ristretto
    per costruzione, perché per fare qualcosa gli serve un attore.
    """
    if user.must_change_pw:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": PASSWORD_CHANGE_REQUIRED,
                    "message": "cambiare la password provvisoria prima di procedere"},
            headers=NO_STORE,
        )
    return user.to_actor(ip=client_ip(request))


def require_admin(actor: Actor = Depends(require_actor)) -> Actor:
    """Attore con ruolo admin. Il ruolo è quello riletto adesso, non quello del
    login: una revoca di privilegi ha effetto dalla richiesta successiva."""
    if actor.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden_for_role",
                    "message": "operazione riservata agli amministratori",
                    "requiredRole": "admin"},
            headers=NO_STORE,
        )
    return actor
