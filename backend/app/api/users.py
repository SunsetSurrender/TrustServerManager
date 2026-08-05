"""Rotte di gestione delle utenze. Solo amministratori.

    GET   /api/users?includeDisabled=       elenco
    POST  /api/users                        crea con password provvisoria
    PATCH /api/users/{id}                   ruolo e profilo
    POST  /api/users/{id}/disable           disattivazione logica + revoca sessioni
    POST  /api/users/{id}/enable            riattivazione
    POST  /api/users/{id}/reset-password    password provvisoria + revoca sessioni

**Non esiste `DELETE`**, e non è una dimenticanza: `audit.actor_user_id` punta a
`users`, quindi cancellare un utente romperebbe la tracciabilità (§8.6). Il ruolo
di runtime non ha nemmeno il privilegio (§8.19), perciò anche una rotta scritta
per errore in futuro non riuscirebbe a cancellare niente.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.api.errors import http_error_for
from app.auth import users as svc
from app.inventory import Actor

router = APIRouter()

ROLE_PATTERN = r"^(view|edit|admin)$"


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    mustChangePassword: bool
    disabled: bool
    nome: str | None = None
    cognome: str | None = None
    telefono: str | None = None
    team: str | None = None
    lastLoginAt: str | None = None
    createdAt: str | None = None


class CreatedUserOut(BaseModel):
    user: UserOut
    #: Password provvisoria, restituita UNA SOLA VOLTA e mai registrata (§8.25).
    temporaryPassword: str


class CreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern=ROLE_PATTERN)
    nome: str | None = Field(default=None, max_length=200)
    cognome: str | None = Field(default=None, max_length=200)
    telefono: str | None = Field(default=None, max_length=50)
    team: str | None = Field(default=None, max_length=200)


class UpdateIn(BaseModel):
    role: str | None = Field(default=None, pattern=ROLE_PATTERN)
    nome: str | None = Field(default=None, max_length=200)
    cognome: str | None = Field(default=None, max_length=200)
    telefono: str | None = Field(default=None, max_length=50)
    team: str | None = Field(default=None, max_length=200)


@router.get("/users", response_model=list[UserOut], summary="Elenco utenze")
def list_users(response: Response,
               includeDisabled: bool = Query(default=False),
               conn: Connection = Depends(get_connection),
               actor: Actor = Depends(require_admin)) -> list[UserOut]:
    response.headers.update(NO_STORE)
    try:
        rows = svc.list_users(conn, include_disabled=includeDisabled)
    except Exception as exc:
        raise http_error_for(exc) from None
    return [UserOut(**r.as_dict()) for r in rows]


@router.post("/users", response_model=CreatedUserOut,
             status_code=status.HTTP_201_CREATED,
             summary="Crea un'utenza con password provvisoria")
def create_user(payload: CreateIn, response: Response,
                conn: Connection = Depends(get_connection),
                actor: Actor = Depends(require_admin)) -> CreatedUserOut:
    response.headers.update(NO_STORE)
    try:
        row, temp = svc.create_user(
            conn, username=payload.username, role=payload.role, actor=actor,
            nome=payload.nome, cognome=payload.cognome,
            telefono=payload.telefono, team=payload.team)
    except Exception as exc:
        raise http_error_for(exc) from None
    return CreatedUserOut(user=UserOut(**row.as_dict()), temporaryPassword=temp)


@router.patch("/users/{user_id}", response_model=UserOut,
              summary="Cambia ruolo e profilo")
def update_user(payload: UpdateIn, response: Response,
                user_id: uuid.UUID = Path(...),
                conn: Connection = Depends(get_connection),
                actor: Actor = Depends(require_admin)) -> UserOut:
    response.headers.update(NO_STORE)
    # `exclude_unset` per distinguere «campo non inviato» da «campo svuotato»:
    # senza, una PATCH che cambia solo il ruolo azzererebbe tutto il profilo.
    fields = payload.model_dump(exclude_unset=True)
    role = fields.pop("role", None)
    try:
        row = svc.update_user(conn, user_id, actor=actor, role=role, **fields)
    except Exception as exc:
        raise http_error_for(exc) from None
    return UserOut(**row.as_dict())


@router.post("/users/{user_id}/disable", response_model=UserOut,
             summary="Disattivazione logica (mai DELETE)")
def disable_user(response: Response, user_id: uuid.UUID = Path(...),
                 conn: Connection = Depends(get_connection),
                 actor: Actor = Depends(require_admin)) -> UserOut:
    response.headers.update(NO_STORE)
    try:
        row = svc.set_disabled(conn, user_id, True, actor=actor)
    except Exception as exc:
        raise http_error_for(exc) from None
    return UserOut(**row.as_dict())


@router.post("/users/{user_id}/enable", response_model=UserOut,
             summary="Riattivazione")
def enable_user(response: Response, user_id: uuid.UUID = Path(...),
                conn: Connection = Depends(get_connection),
                actor: Actor = Depends(require_admin)) -> UserOut:
    response.headers.update(NO_STORE)
    try:
        row = svc.set_disabled(conn, user_id, False, actor=actor)
    except Exception as exc:
        raise http_error_for(exc) from None
    return UserOut(**row.as_dict())


@router.post("/users/{user_id}/reset-password", response_model=CreatedUserOut,
             summary="Password provvisoria + revoca sessioni")
def reset_password(response: Response, user_id: uuid.UUID = Path(...),
                   conn: Connection = Depends(get_connection),
                   actor: Actor = Depends(require_admin)) -> CreatedUserOut:
    response.headers.update(NO_STORE)
    try:
        row, temp = svc.reset_password(conn, user_id, actor=actor)
    except Exception as exc:
        raise http_error_for(exc) from None
    return CreatedUserOut(user=UserOut(**row.as_dict()), temporaryPassword=temp)
