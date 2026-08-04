"""Autorizzazione — logica pura, separata dall'identità.

Consuma gli eventi di dominio di `app.identity` e decide. Nessun endpoint,
nessuna sessione, nessun accesso al database: l'aggancio è il punto 6 di
BACKEND-PLAN.md §9.
"""
from .policy import (
    EDIT_DEVICE_EVENTS,
    FORBIDDEN_FOR_ROLE,
    ROLES,
    ROLLBACK_FORBIDDEN,
    UNKNOWN_ROLE,
    Decision,
    Violation,
    authorize_events,
    authorize_rollback,
)

__all__ = [
    "ROLES", "EDIT_DEVICE_EVENTS",
    "Decision", "Violation",
    "authorize_events", "authorize_rollback",
    "FORBIDDEN_FOR_ROLE", "ROLLBACK_FORBIDDEN", "UNKNOWN_ROLE",
]
