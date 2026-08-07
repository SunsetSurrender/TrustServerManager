"""Registro di audit: scrittura degli eventi e lettura paginata.

La scrittura vive in `app.auth.audit` (gli eventi di autenticazione) e nel
repository dell'inventario; qui c'è la lettura, che è di soli amministratori e
non passa mai dal documento dell'inventario.
"""
from .query import (
    DEFAULT_PAGE_SIZE,
    INVALID_CURSOR,
    INVALID_FILTER,
    INVALID_PAGE_SIZE,
    MAX_PAGE_SIZE,
    RESULTS,
    AuditQueryError,
    Cursor,
    Filters,
    parse_filters,
    parse_page_size,
    query_audit,
)
from .sanitize import REDACTED, contains_secret, sanitize

__all__ = [
    "query_audit", "Cursor", "Filters", "AuditQueryError",
    "parse_filters", "parse_page_size",
    "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "RESULTS",
    "INVALID_CURSOR", "INVALID_PAGE_SIZE", "INVALID_FILTER",
    "sanitize", "contains_secret", "REDACTED",
]
