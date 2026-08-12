"""Inventario: schema congelato del documento, repository atomico, mappa relazionale.

Nessun endpoint HTTP, nessuna autenticazione: le rotte e le sessioni stanno in
`app/api/` (BACKEND-PLAN.md §8.22).

    document.py             schema congelato del percorso normale (§8.16)
    repository.py           scritture atomiche e serializzate (§8.11)
    relational.py           mappa pura documento ↔ modello relazionale (§8.42)
    relational_validate.py  coerenza del modello, errori e avvisi

⚠ `relational*` NON è agganciato a niente: la fase 2A crea lo schema e la mappa,
la sincronizzazione è la fase 2C. `repository.py` continua a essere l'unico
scrittore, e scrive soltanto istantanee JSON, audit e riferimenti alle foto.
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
    "DOCUMENT_TOO_LARGE",
    "InventoryError", "NotBootstrappedError", "AlreadyBootstrappedError",
    "VersionConflictError", "DocumentRejectedError", "IdentityRejectedError",
    "NotAuthorizedError",
]
