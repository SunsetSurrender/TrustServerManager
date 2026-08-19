"""Inventario: schema congelato del documento, repository atomico, mappa relazionale.

Nessun endpoint HTTP, nessuna autenticazione: le rotte e le sessioni stanno in
`app/api/` (BACKEND-PLAN.md §8.22).

    document.py             schema congelato del percorso normale (§8.16)
    digest.py               il digest canonico di un documento (§8.11)
    repository.py           scritture atomiche e serializzate (§8.11, §8.44)
    relational.py           mappa pura documento ↔ modello relazionale (§8.42)
    relational_validate.py  coerenza del modello, errori e avvisi
    projection.py           sincronizzazione, rilettura e verifica della proiezione

⚠ Dalla fase 2C (§8.44) `repository.py` SCRIVE due rappresentazioni nella stessa
transazione: l'istantanea JSONB immutabile con la sua storia, e la proiezione
relazionale dello stato corrente. Non esiste uno stato committato in cui una è
avanzata e l'altra no.

⚠ Ma nessuno la LEGGE ancora, e questa metà non è cambiata: `GET /api/inventory`
restituisce il JSON, lo scheduler delle notifiche legge il documento, il frontend
non sa che la proiezione esista. Il passaggio della lettura è la fase 2D, e avviene
solo dopo che il confronto è stato verde ripetutamente su dati veri. L'unica lettura
nuova è la readiness, che guarda lo STATO della proiezione — versione, digest,
versione della mappa — non le righe.

⚠ `projection` NON è riesportata qui di proposito. Questo pacchetto lo importa
`app/api/inventory.py`: riesportarla renderebbe la proiezione raggiungibile dal
percorso delle richieste con un `from app.inventory import projection` scritto per
sbaglio. Chi le serve la importa per nome — `repository.py` e la readiness — e un
controllo statico verifica che le rotte dell'inventario e il worker non lo facciano.
"""
from .document import (
    ALLOWED_ROOT_KEYS,
    DOCUMENT_TOO_LARGE,
    EMBEDDED_PASSWORD,
    EMBEDDED_PHOTO_DATA,
    EXTRACTED_ROOT_KEYS,
    FORBIDDEN_ROOT_KEY,
    INVALID_PHOTO_REFERENCE,
    MAX_DOCUMENT_BYTES,
    NUMBER_NOT_ROUNDTRIPPABLE,
    SCHEMA_VERSION_CHANGED,
    UNKNOWN_ROOT_KEY,
    DocumentError,
    strip_legacy_fields,
    validate_normal_document,
)
from .errors import (
    AlreadyBootstrappedError,
    DocumentRejectedError,
    IdentityRejectedError,
    InventoryError,
    NotAuthorizedError,
    NotBootstrappedError,
    ProjectionNotCurrentError,
    VersionConflictError,
)
from .repository import (
    MAX_CLIENT_HINT_CHARS,
    Actor,
    InventoryRepository,
    InventorySnapshot,
    SaveResult,
    canonical_sha256,
)

__all__ = [
    "Actor", "InventoryRepository", "InventorySnapshot", "SaveResult",
    "canonical_sha256", "MAX_CLIENT_HINT_CHARS",
    "validate_normal_document", "strip_legacy_fields", "DocumentError",
    "ALLOWED_ROOT_KEYS", "EXTRACTED_ROOT_KEYS", "MAX_DOCUMENT_BYTES",
    "FORBIDDEN_ROOT_KEY", "UNKNOWN_ROOT_KEY", "EMBEDDED_PASSWORD",
    "EMBEDDED_PHOTO_DATA", "INVALID_PHOTO_REFERENCE", "SCHEMA_VERSION_CHANGED",
    "DOCUMENT_TOO_LARGE", "NUMBER_NOT_ROUNDTRIPPABLE",
    "InventoryError", "NotBootstrappedError", "AlreadyBootstrappedError",
    "VersionConflictError", "DocumentRejectedError", "IdentityRejectedError",
    "NotAuthorizedError", "ProjectionNotCurrentError",
]
