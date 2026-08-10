"""Impostazioni dell'applicazione. Solo amministratori.

    GET /api/settings   →  documento tipizzato + `ETag: "<revisione>"`
    PUT /api/settings   →  richiede `If-Match: "<revisione>"`

Non esiste `PATCH`: la `PUT` sostituisce il blocco `notifications` per intero.
Con una modifica parziale «campo assente» e «campo falso» diventano
indistinguibili, e un client che dimentica `enabled` spegnerebbe le notifiche
senza volerlo (§8.38).

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.config import get_settings as get_config
from app.inventory import Actor
from app.settings import (
    MAX_SETTINGS_BYTES,
    SettingsCorrupted,
    SettingsMissing,
    SettingsValidationError,
    SettingsVersionConflict,
)
from app.settings import repository as repo

router = APIRouter()

IF_MATCH_REQUIRED = "if_match_required"
IF_MATCH_MALFORMED = "if_match_malformed"
SETTINGS_TOO_LARGE = "settings_too_large"

#: `"4"` oppure `W/"4"`. Quello che conta è la REVISIONE dentro le virgolette.
#:
#: La prima versione accettava solo la forma forte, per il motivo giusto sulla
#: carta: la RFC 9110 impone il confronto forte per `If-Match`. In pratica non
#: funzionava, e l'ha scoperto il test nel browser vero — non quello sull'API.
#:
#: Il modulo gzip di nginx INDEBOLISCE l'ETag quando comprime la risposta: il
#: server manda `"4"`, il browser (che dichiara `Accept-Encoding: gzip`) riceve
#: `W/"4"`, e può solo rimandare quello che ha ricevuto. Ogni salvataggio dal
#: browser riceveva 422 mentre le stesse chiamate da uno script — che non chiede
#: la compressione — funzionavano.
#:
#: Accettare la forma debole è corretto per QUESTO validatore: la distinzione
#: forte/debole riguarda l'identità byte per byte della rappresentazione, e serve
#: a cose come le richieste di intervallo. Qui il validatore è un numero di
#: revisione, e la compressione non cambia la revisione: `W/"4"` identifica la
#: revisione 4 senza ambiguità. Si sarebbe potuto disattivare gzip su /api/, ma
#: sarebbe una configurazione da ricordare per sempre, e il prossimo proxy che
#: comprime romperebbe di nuovo la concorrenza.
#:
#: `*` resta rifiutato: quello significa davvero «qualunque versione vada bene»,
#: cioè l'ultimo-che-scrive-vince con un'intestazione davanti.
_ETAG_RE = re.compile(r'^(?:W/)?"(\d{1,19})"$')


def _fail(code: str, message: str, http: int = 422, **extra) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(http, detail=detail, headers=NO_STORE)


def parse_if_match(raw: str | None) -> int:
    """Revisione attesa dall'intestazione `If-Match`. Solleva con codice stabile."""
    if raw is None or not raw.strip():
        raise _fail(IF_MATCH_REQUIRED,
                    "intestazione If-Match obbligatoria: rileggere le "
                    "impostazioni e rimandare l'ETag ricevuto")
    match = _ETAG_RE.match(raw.strip())
    if not match:
        raise _fail(IF_MATCH_MALFORMED,
                    'If-Match deve essere l\'ETag ricevuto dalla GET, '
                    'per esempio "4" oppure W/"4"')
    return int(match.group(1))


@router.get("/settings", summary="Impostazioni (solo admin)")
def read_settings(response: Response,
                  conn: Connection = Depends(get_connection),
                  actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)
    try:
        row = repo.load(conn)
    except (SettingsMissing, SettingsCorrupted) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None
    # L'ETag viaggia nell'intestazione E la revisione nel corpo. Non è
    # duplicazione inutile: l'intestazione è ciò che il client rimanda in
    # `If-Match`, il campo nel corpo è ciò che può mostrare e registrare.
    response.headers["ETag"] = row.etag
    return repo.to_response(row, smtp_configured=get_config().smtp_configured())


@router.put("/settings", summary="Salva le impostazioni (solo admin)")
def write_settings(request: Request, response: Response,
                   payload: Any = Body(default=None),
                   conn: Connection = Depends(get_connection),
                   actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)

    # Tetto specifico, molto più stretto di quello globale (§8.22): il limite
    # generale deve lasciar passare l'inventario, questo no.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_SETTINGS_BYTES:
        raise _fail(SETTINGS_TOO_LARGE, "corpo della richiesta troppo grande",
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    expected = parse_if_match(request.headers.get("if-match"))

    try:
        row, changed = repo.save(conn, payload=payload,
                                 expected_version=expected, actor=actor)
    except SettingsValidationError as exc:
        # Il campo si nomina, il valore no: un valore rifiutato può essere
        # proprio il segreto che si sta cercando di tenere fuori (§8.38).
        raise _fail(exc.code, exc.message, 422, field=exc.field) from None
    except SettingsVersionConflict as exc:
        # L'ETag corrente viaggia anche qui: il client può ricaricare e riprovare
        # senza una seconda GET, ma NON deve rimandare gli stessi dati in
        # automatico — il conflitto esiste perché quei dati sono vecchi.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": exc.message,
                    "currentVersion": exc.current_version},
            headers={**NO_STORE, "ETag": f'"{exc.current_version}"'}) from None
    except (SettingsMissing, SettingsCorrupted) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None

    response.headers["ETag"] = row.etag
    body = repo.to_response(row, smtp_configured=get_config().smtp_configured())
    # `changed: false` per un salvataggio che non cambia nulla. La revisione NON
    # sale (§8.38) e il client deve poterlo distinguere da un salvataggio vero,
    # se non altro per non dire «salvato» quando non c'era niente da salvare.
    body["changed"] = changed
    return body
