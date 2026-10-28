"""Politica di accesso. Solo amministratori.

    GET  /api/security-policy     soglia di accessi falliti prima del blocco
    PUT  /api/security-policy     {"maxFailedLogins": 3..20, "passwordResetEnabled": bool}
                                  (uno o entrambi)

Le utenze bloccate si sbloccano da `POST /api/users/{id}/unlock`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.api.errors import http_error_for
from app.auth import lockout
from app.inventory import Actor

router = APIRouter()


class PolicyOut(BaseModel):
    maxFailedLogins: int
    minMaxFailedLogins: int
    maxMaxFailedLogins: int
    updatedAt: str | None = None
    updatedBy: str | None = None
    passwordResetEnabled: bool = True


class PolicyIn(BaseModel):
    # Senza vincoli di tipo qui: il controllo vive in `lockout.set_policy`, così
    # i messaggi e i limiti stanno in un posto solo.
    maxFailedLogins: object = None
    passwordResetEnabled: object = None


@router.get("/security-policy", response_model=PolicyOut,
            summary="Politica di accesso")
def get_policy(response: Response, conn: Connection = Depends(get_connection),
               actor: Actor = Depends(require_admin)) -> PolicyOut:
    response.headers.update(NO_STORE)
    return PolicyOut(**lockout.get_policy(conn).as_dict())


@router.put("/security-policy", response_model=PolicyOut,
            summary="Cambia la soglia di blocco delle utenze")
def put_policy(payload: PolicyIn, response: Response,
               conn: Connection = Depends(get_connection),
               actor: Actor = Depends(require_admin)) -> PolicyOut:
    response.headers.update(NO_STORE)
    try:
        sent = payload.model_dump(exclude_unset=True)
        kwargs = {}
        if "maxFailedLogins" in sent:
            kwargs["max_failed_logins"] = sent["maxFailedLogins"]
        if "passwordResetEnabled" in sent:
            kwargs["password_reset_enabled"] = sent["passwordResetEnabled"]
        policy = lockout.set_policy(conn, actor=actor, **kwargs)
    except Exception as exc:
        raise http_error_for(exc) from None
    return PolicyOut(**policy.as_dict())
