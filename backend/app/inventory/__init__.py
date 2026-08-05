"""Inventario: schema congelato del documento e repository atomico.

Nessun endpoint HTTP, nessuna autenticazione: le rotte e le sessioni sono commit
successivi (BACKEND-PLAN.md §9, punti 5-6).
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
