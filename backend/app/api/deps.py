"""Dipendenze delle rotte: connessione e ATTORE AUTENTICATO OBBLIGATORIO.

Non esiste un ripiego di sviluppo che conceda `admin` senza autenticazione.
Il motivo è che quel ripiego, per definizione, funziona: sopravvive ai
refactoring, non fa fallire nessun test, e il giorno in cui una variabile
d'ambiente è impostata male diventa un accesso amministrativo anonimo. Il
prototipo aveva già un difetto della stessa forma — `_doLogin` concedeva `admin`
quando l'elenco utenze era vuoto — e va rimosso, non riprodotto sul server.

`require_actor` in esecuzione richiede una sessione valida e risponde 401
altrimenti. Nei test si sostituisce con `app.dependency_overrides`, che è
esplicito, locale al test e impossibile da attivare per errore in produzione.

Riferimento: BACKEND-PLAN.md §8.20.
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.engine import Connection

from app.auth.service import (
    AuthenticatedUser,
    NotAuthenticated,
    resolve_session,
)
from app.db import get_engine
from app.inventory import Actor
from app.util import safe_ip

SESSION_COOKIE = "tsm_session"


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
    token = request.cookies.get(SESSION_COOKIE)
    try:
        return resolve_session(conn, token)
    except NotAuthenticated as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "autenticazione richiesta"},
            headers={"Cache-Control": "no-store"},
        ) from None


def require_actor(request: Request,
                  user: AuthenticatedUser = Depends(current_user)) -> Actor:
    """L'attore per le scritture. 401 se non autenticato.

    Se l'utenza ha una password provvisoria si risponde 403 con un codice
    dedicato: la sessione è valida ma non deve poter fare nulla prima del cambio
    password (§8.1). Il client sa già gestire quel passaggio.
    """
    if user.must_change_pw:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "password_change_required",
                    "message": "cambiare la password provvisoria prima di procedere"},
            headers={"Cache-Control": "no-store"},
        )
    client = request.client
    return user.to_actor(ip=safe_ip(client.host if client else None))
