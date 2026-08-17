"""Rotte di autenticazione. Adattatore HTTP su `app.auth.service`.

Sequenza di avvio del client (§8.1): `GET /api/auth/me` prima dell'inventario. Un
401 lì significa schermata di login e NESSUNA chiamata a /api/inventory.

Con una password provvisoria l'accesso **riesce** (200, `authenticated=true`,
`mustChangePassword=true`) e la sessione è ristretta a queste tre rotte: §8.26.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, SESSION_COOKIE, current_user, get_connection
from app.api.errors import http_error_for
from app.api.request_context import client_ip
from app.auth.service import (
    SESSION_TTL,
    AuthenticatedUser,
    change_own_password,
    login,
    logout,
)
from app.config import get_settings

router = APIRouter()


class LoginIn(BaseModel):
    # Dimensioni limitate: un input non attendibile non deve poter diventare un
    # costo. Argon2 su una password enorme è lavoro regalato a chi la invia.
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class SessionOut(BaseModel):
    """Forma unica per login e /auth/me: il client legge sempre gli stessi campi.

    `authenticated` è esplicito e non implicito nello stato HTTP: un accesso con
    password provvisoria riesce, quindi «200» da solo non direbbe al client se
    può procedere o se deve prima cambiare la password.
    """
    authenticated: bool
    username: str
    role: str
    mustChangePassword: bool


class PasswordIn(BaseModel):
    """Nessun `min_length` qui, e non è una svista.

    La lunghezza minima è una regola di POLITICA, e vive in `app.auth.passwords`
    con tutte le altre. Duplicarla qui darebbe due comportamenti per lo stesso
    rifiuto: pydantic risponde con la propria forma (`detail` come lista di errori
    di validazione), mentre la politica risponde con un codice stabile e un
    messaggio utile — e il client non può spiegare all'utente un errore che arriva
    in due formati diversi a seconda di quanto è corta la password. Peggio: i due
    numeri divergerebbero, e il 10 rimasto qui dal contratto precedente ne è la
    prova.

    `max_length` resta, ed è un'altra cosa: non è politica, è un limite di
    dimensione su un input non attendibile, molto sopra il massimo consentito (128)
    perché a rifiutare deve essere la politica, con il suo codice.
    """
    currentPassword: str = Field(max_length=1024)
    newPassword: str = Field(max_length=1024)


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,           # non leggibile da JavaScript
        secure=s.cookie_secure,  # in produzione obbligatorio (l'avvio lo impone)
        samesite="strict",       # niente invio da altri siti
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True,
                           secure=s.cookie_secure, samesite="strict")


@router.post("/auth/login", response_model=SessionOut, summary="Apre una sessione")
def do_login(payload: LoginIn, request: Request, response: Response,
             conn: Connection = Depends(get_connection)) -> SessionOut:
    response.headers.update(NO_STORE)
    try:
        token, user = login(conn, payload.username, payload.password,
                            ip=client_ip(request),
                            user_agent=request.headers.get("user-agent"))
    except Exception as exc:
        raise http_error_for(exc) from None
    _set_session_cookie(response, token)
    return SessionOut(authenticated=True, username=user.username, role=user.role,
                      mustChangePassword=user.must_change_pw)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT,
             summary="Chiude la sessione")
def do_logout(request: Request,
              conn: Connection = Depends(get_connection)) -> Response:
    # Volutamente senza `current_user`: chiudere una sessione già scaduta o
    # inesistente deve riuscire, non dare 401. Il logout è idempotente.
    logout(conn, request.cookies.get(SESSION_COOKIE), ip=client_ip(request))
    r = Response(status_code=status.HTTP_204_NO_CONTENT, headers=NO_STORE)
    _clear_session_cookie(r)
    return r


@router.get("/auth/me", response_model=SessionOut, summary="Sessione corrente")
def me(response: Response,
       user: AuthenticatedUser = Depends(current_user)) -> SessionOut:
    # Volutamente NON usa require_actor: con una password provvisoria l'utente
    # deve poter leggere il proprio stato per sapere che deve cambiarla (§8.26).
    # I campi sono riletti dal database a ogni richiesta, non dalla sessione.
    response.headers.update(NO_STORE)
    return SessionOut(authenticated=True, username=user.username, role=user.role,
                      mustChangePassword=user.must_change_pw)


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Cambio della propria password")
def change_password(payload: PasswordIn, request: Request,
                    conn: Connection = Depends(get_connection),
                    user: AuthenticatedUser = Depends(current_user)) -> Response:
    # Anche questa usa `current_user` e non `require_actor`: è una delle tre cose
    # che una sessione con password provvisoria deve poter fare.
    try:
        change_own_password(conn, user.id, payload.currentPassword,
                            payload.newPassword)
    except Exception as exc:
        raise http_error_for(exc) from None
    # Il cambio revoca TUTTE le sessioni, compresa questa: il cookie va rimosso e
    # serve un accesso nuovo. Altrimenti il client crederebbe di essere ancora
    # autenticato e riceverebbe 401 alla richiesta successiva, senza capire perché.
    r = Response(status_code=status.HTTP_204_NO_CONTENT, headers=NO_STORE)
    _clear_session_cookie(r)
    return r
