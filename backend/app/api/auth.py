"""Rotte di autenticazione. Adattatore HTTP su `app.auth.service`.

Sequenza di avvio del client (§8.1): `GET /api/auth/me` prima dell'inventario.
Un 401 lì significa schermata di login e NESSUNA chiamata a /api/inventory.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import SESSION_COOKIE, current_user, get_connection
from app.api.errors import NO_STORE, http_error_for
from app.auth.service import (
    AuthenticatedUser,
    SESSION_TTL,
    change_own_password,
    login,
    logout,
)
from app.config import get_settings
from app.util import safe_ip

router = APIRouter()


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class MeOut(BaseModel):
    username: str
    role: str
    mustChangePassword: bool


class PasswordIn(BaseModel):
    currentPassword: str = Field(max_length=1024)
    newPassword: str = Field(min_length=10, max_length=1024)


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,          # non leggibile da JavaScript
        secure=s.cookie_secure,  # in produzione obbligatorio: senza HTTPS non si entra
        samesite="strict",      # niente invio da altri siti
        path="/",
    )


@router.post("/auth/login", response_model=MeOut, summary="Apre una sessione")
def do_login(payload: LoginIn, request: Request, response: Response,
             conn: Connection = Depends(get_connection)) -> MeOut:
    response.headers.update(NO_STORE)
    client = request.client
    try:
        token, user = login(conn, payload.username, payload.password,
                            ip=safe_ip(client.host if client else None),
                            user_agent=request.headers.get("user-agent"))
    except Exception as exc:
        raise http_error_for(exc) from None
    _set_session_cookie(response, token)
    return MeOut(username=user.username, role=user.role,
                 mustChangePassword=user.must_change_pw)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT,
             summary="Chiude la sessione")
def do_logout(request: Request, response: Response,
              conn: Connection = Depends(get_connection)) -> Response:
    logout(conn, request.cookies.get(SESSION_COOKIE))
    response.headers.update(NO_STORE)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT,
                    headers={**NO_STORE,
                             "set-cookie": response.headers.get("set-cookie", "")})


@router.get("/auth/me", response_model=MeOut, summary="Sessione corrente")
def me(response: Response,
       user: AuthenticatedUser = Depends(current_user)) -> MeOut:
    # Volutamente NON usa require_actor: un utente con password provvisoria deve
    # poter leggere il proprio stato per sapere che deve cambiarla (§8.1).
    response.headers.update(NO_STORE)
    return MeOut(username=user.username, role=user.role,
                 mustChangePassword=user.must_change_pw)


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Cambio della propria password")
def change_password(payload: PasswordIn, response: Response,
                    conn: Connection = Depends(get_connection),
                    user: AuthenticatedUser = Depends(current_user)) -> Response:
    try:
        change_own_password(conn, user.id, payload.currentPassword, payload.newPassword)
    except Exception as exc:
        raise http_error_for(exc) from None
    # Il cambio password revoca tutte le sessioni, compresa questa: il cookie va
    # rimosso, altrimenti il client crede di essere ancora autenticato.
    r = Response(status_code=status.HTTP_204_NO_CONTENT, headers=NO_STORE)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r
