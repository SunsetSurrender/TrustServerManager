"""Identità e diff dell'inventario — logica pura.

Deliberatamente NON collegato a FastAPI né a PostgreSQL: qui non ci sono
endpoint, sessioni, modelli SQLAlchemy o transazioni. L'aggancio all'API
(autorizzazione per ambito, commit atomico, audit) è un commit successivo,
punti 5 e 6 di BACKEND-PLAN.md §9.

Il contratto con il frontend (handoff/identity.js) è verificato dalle fixture
condivise in fixtures/identity/, consumate da entrambe le suite di test.
"""
from .canonical import (
    ENTITY_DEFAULTS,
    SETTINGS_DEFAULTS,
    canonical_sort,
    canonicalise,
    strip_uids,
)
from .diff import Event, diff_as_dicts, diff_documents, scopes_touched
from .model import KINDS, SCOPE_BY_KIND, Entity, is_uid, walk
from .schema import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION_INVALID,
    SCHEMA_VERSION_MISSING,
    SCHEMA_VERSION_TOO_NEW,
    SCHEMA_VERSION_TOO_OLD,
    SchemaError,
    check_schema_version,
    is_migratable,
)
from .validator import (
    AMBIGUOUS_REPLACEMENT,
    BUSINESS_KEY_REUSE,
    DUPLICATE_UID,
    IDENTITY_REPLACEMENT,
    MALFORMED_UID,
    MISSING_UID,
    IdentityError,
    validate_against_base,
    validate_document,
)

__all__ = [
    "Entity", "Event", "IdentityError", "SchemaError",
    "KINDS", "SCOPE_BY_KIND",
    "is_uid", "walk",
    "validate_document", "validate_against_base",
    "diff_documents", "diff_as_dicts", "scopes_touched",
    "canonicalise", "canonical_sort", "strip_uids",
    "ENTITY_DEFAULTS", "SETTINGS_DEFAULTS",
    "check_schema_version", "is_migratable", "CURRENT_SCHEMA_VERSION",
    "MISSING_UID", "MALFORMED_UID", "DUPLICATE_UID",
    "IDENTITY_REPLACEMENT", "BUSINESS_KEY_REUSE", "AMBIGUOUS_REPLACEMENT",
    "SCHEMA_VERSION_MISSING", "SCHEMA_VERSION_TOO_OLD",
    "SCHEMA_VERSION_TOO_NEW", "SCHEMA_VERSION_INVALID",
]
