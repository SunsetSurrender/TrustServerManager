"""Identità e diff dell'inventario — logica pura.

Deliberatamente NON collegato a FastAPI né a PostgreSQL: qui non ci sono
endpoint, sessioni, modelli SQLAlchemy o transazioni. L'aggancio all'API
(autorizzazione per ambito, commit atomico, audit) è un commit successivo,
punti 5 e 6 di BACKEND-PLAN.md §9.

Il contratto con il frontend (handoff/identity.js) è verificato dalle fixture
condivise in fixtures/identity/, consumate da entrambe le suite di test.
"""
from .diff import Event, diff_as_dicts, diff_documents, scopes_touched
from .model import KINDS, SCOPE_BY_KIND, Entity, is_uid, walk
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
    "Entity", "Event", "IdentityError",
    "KINDS", "SCOPE_BY_KIND",
    "is_uid", "walk",
    "validate_document", "validate_against_base",
    "diff_documents", "diff_as_dicts", "scopes_touched",
    "MISSING_UID", "MALFORMED_UID", "DUPLICATE_UID",
    "IDENTITY_REPLACEMENT", "BUSINESS_KEY_REUSE", "AMBIGUOUS_REPLACEMENT",
]
