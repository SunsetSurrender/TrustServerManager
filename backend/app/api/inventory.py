"""Rotte dell'inventario. Contratto congelato (§8.22).

    GET  /api/inventory  → { version, schemaVersion, sha256, doc }
    PUT  /api/inventory  ← { baseVersion, doc, action? }
                         → { version, schemaVersion, sha256, changed }
                           409 → { code, currentVersion, currentSha256 }

Il bootstrap NON è qui: è una CLI (§8.17). L'API non ha nemmeno il privilegio di
inserire la riga di testa (§8.19).

⚠ Dalla fase 2D il `GET` RICOSTRUISCE il documento dalle tabelle normalizzate
(§8.45). Il contratto HTTP non cambia di una virgola — stesse quattro chiavi, stesso
`Cache-Control`, stesse restrizioni di autenticazione, nessuna modifica al frontend —
ma la fonte sì: `inventory_versions.doc` non è più ciò che si restituisce.

Il modello che ne risulta:

    tabelle normalizzate    stato operativo CORRENTE, autorevole
    inventory_versions.doc  storia immutabile, e GIUDICE della coerenza
    inventory_head          puntatore alla revisione corrente
    projection_state        dichiarazione di quale testa le tabelle rappresentano

Questa rotta legge di `inventory_versions` soltanto il `canonical_sha256` della
versione in testa, che è metadato e serve come riferimento. Non deserializza il
documento immutabile e non lo restituisce mai: se lo facesse potrebbe ripiegarci
sopra in caso di dubbio, e quel ripiego nasconderebbe precisamente il difetto che la
fase 2 esiste per scoprire. Un controllo statico in `tools/storage-config-test.py`
lo verifica, perché è una proprietà che un test di comportamento non sa provare.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, ContextManager

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import get_connection, get_snapshot_reader, require_actor
from app.api.errors import NO_STORE, http_error_for
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import (
    Actor,
    InventoryError,
    InventoryRepository,
    MAX_CLIENT_HINT_CHARS,
    canonical_sha256,
)
# ⚠ Importata per nome e non dal pacchetto: `app.inventory` NON riesporta
# `projection`, di proposito (§8.42). Fino alla fase 2C questa rotta non doveva
# nemmeno poterla raggiungere; dalla 2D l'import è VOLUTO ed è il senso della fase.
# Il divieto non è stato tolto, si è STRETTO: un controllo statico verifica che
# questa rotta legga la proiezione e NON `inventory_versions.doc`.
from app.inventory import projection

log = logging.getLogger(__name__)
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
                  actor: Actor = Depends(require_actor),
                  reader: Callable[[], ContextManager[Connection]] =
                      Depends(get_snapshot_reader)) -> InventoryOut:
    """Il documento corrente, RIASSEMBLATO dalle tabelle normalizzate (§8.45).

    ⚠ Non dipende da `get_connection`. Lo snapshot si apre qui, nel corpo, e per due
    ragioni:

      - l'autenticazione è già stata risolta quando il corpo comincia, quindi una
        richiesta anonima o con password provvisoria non apre nessuna transazione sul
        database — costa un 401 o un 403 e niente altro. Con un `Depends` che
        restituisce la connessione, l'ordine dipenderebbe dall'ordine dei parametri;
      - la connessione della richiesta non sarebbe utilizzabile comunque: è già in
        transazione, e l'isolamento si dichiara prima del primo statement. Vedi
        `snapshot_connection` per il perché completo.

    `version` e `sha256` vengono dallo STESSO snapshot che ha prodotto `doc`: la
    risposta descrive un solo istante del database, non tre letture ravvicinate.
    """
    response.headers.update(NO_STORE)
    try:
        with reader() as snapshot_conn:
            current = projection.current_document(snapshot_conn)
    except Exception as exc:
        raise http_error_for(exc) from None

    doc = current.doc
    if current.warnings:
        # Nei log e non nella risposta: il contratto del frontend è il documento
        # (§8.22), e un avviso non è un guasto — una data scritta a mano o un enum
        # fuori vocabolario non devono rendere l'inventario indisponibile (§8.42).
        # Nei log servono, perché sono l'unico modo di sapere che quel campo, per
        # quella riga, non risponderà a una query.
        log.info("inventario servito con %d avvisi di proiezione (versione %d): %s",
                 len(current.warnings), current.version, current.warnings[:10])
    return InventoryOut(
        version=current.version,
        schemaVersion=doc.get("schemaVersion", CURRENT_SCHEMA_VERSION),
        sha256=current.sha256,
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
