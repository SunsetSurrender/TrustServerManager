"""Rotte di autenticazione. Adattatore HTTP su `app.auth.service`.

Sequenza di avvio del client (§8.1): `GET /api/auth/me` prima dell'inventario. Un
401 lì significa schermata di login e NESSUNA chiamata a /api/inventory.

Con una password provvisoria l'accesso **riesce** (200, `authenticated=true`,
`mustChangePassword=true`) e la sessione è ristretta a queste tre rotte: §8.26.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import (NO_STORE, SESSION_COOKIE, current_user, get_connection,
                           require_actor)
from app.inventory import Actor
from sqlalchemy import text
from app.api.errors import http_error_for
from app.api.request_context import client_ip
from app.auth.service import (
    SESSION_TTL,
    AuthenticatedUser,
    change_own_password,
    login,
    logout,
)
from app.auth import reset as reset_svc
from app.auth.service import _out_of_band
from app.config import get_settings

log = logging.getLogger(__name__)

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
    # Profilo (0.4.2): li legge e li modifica la persona stessa dal Profilo.
    nome: str | None = None
    cognome: str | None = None
    telefono: str | None = None
    team: str | None = None
    email: str | None = None
    #: Minuti di inattività dopo cui la sessione si chiude (0 = mai). Il client lo
    #: usa per avvisare e uscire da solo; la regola vera la applica il server.
    idleTimeoutMinutes: int = 0


class ProfileIn(BaseModel):
    """Dati che una persona può cambiare da sé. Ruolo, utenza e team no: li decide
    un amministratore."""
    nome: str | None = Field(default=None, max_length=200)
    cognome: str | None = Field(default=None, max_length=200)
    telefono: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=254)


def _session_out(conn: Connection, username: str, role: str,
                 must_change: bool) -> "SessionOut":
    from app.auth import lockout
    row = conn.execute(text(
        "SELECT nome, cognome, telefono, team, email FROM users WHERE username = :u"),
        {"u": username}).first()
    nome, cognome, telefono, team, email = row if row else (None,) * 5
    return SessionOut(authenticated=True, username=username, role=role,
                      mustChangePassword=must_change, nome=nome, cognome=cognome,
                      telefono=telefono, team=team, email=email,
                      idleTimeoutMinutes=lockout.get_policy(conn).idle_timeout_minutes)


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
    return _session_out(conn, user.username, user.role, user.must_change_pw)


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
def me(response: Response, conn: Connection = Depends(get_connection),
       user: AuthenticatedUser = Depends(current_user)) -> SessionOut:
    # Volutamente NON usa require_actor: con una password provvisoria l'utente
    # deve poter leggere il proprio stato per sapere che deve cambiarla (§8.26).
    # I campi sono riletti dal database a ogni richiesta, non dalla sessione.
    response.headers.update(NO_STORE)
    return _session_out(conn, user.username, user.role, user.must_change_pw)


@router.patch("/auth/me", response_model=SessionOut,
              summary="Aggiorna i propri dati personali")
def update_me(payload: ProfileIn, request: Request, response: Response,
              conn: Connection = Depends(get_connection),
              actor: Actor = Depends(require_actor)) -> SessionOut:
    """Solo nome, cognome, telefono ed email, e solo i propri. Stessa validazione
    e stessa riga di audit della modifica fatta da un amministratore."""
    from app.auth import users as users_svc
    response.headers.update(NO_STORE)
    fields = payload.model_dump(exclude_unset=True)
    try:
        users_svc.update_user(conn, actor.user_id, actor=actor, role=None, **fields)
    except Exception as exc:
        raise http_error_for(exc) from None
    return _session_out(conn, actor.username, actor.role, False)


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


# ------------------------------------------------------------ recupero password
#
# Tre rotte PUBBLICHE (nessuna sessione): la schermata di accesso le usa prima
# di avere un utente. Regole in `app.auth.reset`.

class ResetAvailableOut(BaseModel):
    enabled: bool


class ResetRequestIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)


class ResetConfirmIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    newPassword: str = Field(max_length=1024)


RESET_ACCEPTED_MESSAGE = (
    "Se l'utenza esiste e ha un indirizzo email, riceverai a breve un codice "
    "valido 30 minuti. Se non arriva, rivolgiti a un amministratore.")


def _send_reset_mail(mail: "reset_svc.ResetMail") -> None:
    """Spedizione in sottofondo, DOPO la risposta.

    Due ragioni: il tempo di risposta non deve dire se una mail è partita (cioè
    se l'utenza esiste), e un relay lento non deve tenere appesa la schermata di
    accesso. Un errore finisce nei log; la persona, non ricevendo niente, chiede
    a un amministratore — come dice il messaggio.
    """
    from app.notifications.smtp import build_reset_message, deliver
    s = get_settings()
    origins = s.allowed_origins()
    link = None
    if origins:
        link = origins[0] + "/#reset?" + urlencode({"u": mail.username, "c": mail.code})
    try:
        msg = build_reset_message(s.smtp_sender, mail.to, username=mail.username,
                                  code=mail.code, expires_minutes=mail.expires_minutes,
                                  link=link)
        deliver(msg, what="recupero password")
    except Exception:
        log.exception("recupero password: invio della mail non riuscito")


@router.get("/auth/password-reset", response_model=ResetAvailableOut,
            summary="Il recupero password via email è disponibile?")
def reset_available(response: Response,
                    conn: Connection = Depends(get_connection)) -> ResetAvailableOut:
    response.headers.update(NO_STORE)
    return ResetAvailableOut(enabled=reset_svc.is_available(conn))


@router.post("/auth/password-reset/request", status_code=status.HTTP_202_ACCEPTED,
             summary="Chiede un codice di recupero (risposta sempre identica)")
def reset_request(payload: ResetRequestIn, request: Request,
                  background: BackgroundTasks,
                  conn: Connection = Depends(get_connection)) -> dict:
    mail = reset_svc.request_reset(conn, payload.username, ip=client_ip(request))
    if mail is not None:
        background.add_task(_send_reset_mail, mail)
    return {"message": RESET_ACCEPTED_MESSAGE}


@router.post("/auth/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT,
             summary="Imposta una nuova password con utenza e codice")
def reset_confirm(payload: ResetConfirmIn, request: Request,
                  conn: Connection = Depends(get_connection)) -> Response:
    try:
        reset_svc.confirm_reset(conn, payload.username, payload.code,
                                payload.newPassword, ip=client_ip(request),
                                out_of_band=_out_of_band)
    except Exception as exc:
        raise http_error_for(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=NO_STORE)
