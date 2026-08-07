"""Rotta di lettura del registro di audit. Solo amministratori.

Sola lettura, e non solo per convenzione: il ruolo di runtime del database non ha
i privilegi `UPDATE`/`DELETE` sulla tabella (§8.19, migrazione 0006). Una rotta
di modifica scritta per errore in futuro non riuscirebbe comunque a scrivere.

Il registro NON passa dal documento dell'inventario: è una tabella con la sua
rotta, e i client non ne ricostruiscono voci per conto proprio (§8.9).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.audit import (
    AuditQueryError,
    Cursor,
    parse_filters,
    parse_page_size,
    query_audit,
)
from app.inventory import Actor

router = APIRouter()


@router.get("/audit", summary="Registro delle modifiche (solo admin)")
def read_audit(
    response: Response,
    # I parametri arrivano come STRINGHE e li valida il nostro parser. Se li
    # dichiarassimo tipizzati o con vincoli stretti, sarebbe FastAPI a rifiutarli
    # per primo, con una forma di errore diversa dalla nostra: il client
    # riceverebbe due formati a seconda di quale controllo scatta. Il limite
    # generoso qui è solo un tetto grezzo; i limiti veri stanno nel parser, che
    # risponde sempre con un codice stabile.
    cursor: str | None = Query(default=None, max_length=1024),
    pageSize: str | None = Query(default=None, max_length=32),
    frm: str | None = Query(default=None, alias="from", max_length=64),
    to: str | None = Query(default=None, max_length=64),
    username: str | None = Query(default=None, max_length=1024),
    event: str | None = Query(default=None, max_length=1024),
    result: str | None = Query(default=None, max_length=64),
    conn: Connection = Depends(get_connection),
    actor: Actor = Depends(require_admin),
) -> dict:
    response.headers.update(NO_STORE)
    try:
        size = parse_page_size(pageSize)
        filters = parse_filters(frm=frm, to=to, username=username,
                                event=event, result=result)
        cur = Cursor.decode(cursor) if cursor else None
    except AuditQueryError as exc:
        # 422 con un codice stabile: un cursore manomesso deve dare un errore
        # riconoscibile, non una pagina «quasi giusta» che salta righe.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": exc.message},
            headers=NO_STORE) from None

    return query_audit(conn, filters=filters, cursor=cur, page_size=size)
