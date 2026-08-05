"""Rotte dell'inventario. Contratto congelato (§8.22).

    GET  /api/inventory  → { version, schemaVersion, sha256, doc }
    PUT  /api/inventory  ← { baseVersion, doc, action? }
                         → { version, schemaVersion, sha256, changed }
                           409 → { code, currentVersion, currentSha256 }

Il bootstrap NON è qui: è una CLI (§8.17). L'API non ha nemmeno il privilegio di
inserire la riga di testa (§8.19).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import get_connection, require_actor
from app.api.errors import NO_STORE, http_error_for
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import (
    Actor,
    InventoryError,
    InventoryRepository,
    MAX_CLIENT_HINT_CHARS,
    canonical_sha256,
)

router = APIRouter()


class InventoryOut(BaseModel):
    version: int
    schemaVersion: int
    sha256: str
    doc: dict


class SaveIn(BaseModel):
    baseVersion: int
    doc: dict
    # Etichetta di comodo, non attendibile e limitata in lunghezza: descrive
    # l'intenzione per la lettura del registro, non ciò che è cambiato — quello
    # lo calcola il server (§8.9).
    action: str | None = Field(default=None, max_length=MAX_CLIENT_HINT_CHARS)


class SaveOut(BaseModel):
    version: int
    schemaVersion: int
    sha256: str
    changed: bool


@router.get("/inventory", response_model=InventoryOut,
            summary="Documento corrente dell'inventario")
def get_inventory(response: Response,
                  conn: Connection = Depends(get_connection),
                  actor: Actor = Depends(require_actor)) -> InventoryOut:
    response.headers.update(NO_STORE)
    try:
        snapshot = InventoryRepository(conn).get_current()
    except Exception as exc:
        raise http_error_for(exc) from None
    doc = snapshot.doc or {}
    return InventoryOut(
        version=snapshot.version,
        schemaVersion=doc.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        sha256=canonical_sha256(doc),
        doc=doc,
    )


@router.put("/inventory", response_model=SaveOut,
            summary="Salva il documento (lock ottimistico)")
def put_inventory(payload: SaveIn, response: Response,
                  conn: Connection = Depends(get_connection),
                  actor: Actor = Depends(require_actor)) -> SaveOut:
    response.headers.update(NO_STORE)
    repo = InventoryRepository(conn)
    try:
        result = repo.save(payload.baseVersion, payload.doc, actor,
                           client_hint=payload.action)
        # La versione risultante è quella da cui il client ripartirà: si rilegge
        # dal repository invece di ricostruirla, così la risposta descrive ciò
        # che è nel database e non ciò che si suppone.
        snapshot = repo.get_version(result.version)
    except Exception as exc:
        raise http_error_for(exc) from None

    doc = snapshot.doc or {}
    return SaveOut(
        version=result.version,
        schemaVersion=doc.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        sha256=canonical_sha256(doc),
        changed=result.created,
    )
