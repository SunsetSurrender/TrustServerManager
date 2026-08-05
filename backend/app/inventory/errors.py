"""Errori del repository dell'inventario.

Tipizzati e con codice stabile: chi li intercetta (l'endpoint, quando arriverà)
deve poter tradurli in stati HTTP senza leggere messaggi.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class InventoryError(Exception):
    """Base. `code` è un contratto, `details` è leggibile dalla macchina."""

    code = "inventory_error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details if details is not None else []

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotBootstrappedError(InventoryError):
    """Nessuna testa: il repository non è stato inizializzato."""
    code = "not_bootstrapped"


class AlreadyBootstrappedError(InventoryError):
    """Il bootstrap è una-volta-sola e la testa esiste già."""
    code = "already_bootstrapped"


class VersionConflictError(InventoryError):
    """`baseVersion` non è più la testa E il contenuto è diverso (§8.11, §8.18).

    Porta `currentSha256` perché il client possa decidere senza un secondo giro:
    confrontando l'hash con quello del documento che ha in mano capisce se la
    versione in testa è già quella che voleva scrivere.
    """
    code = "version_conflict"

    def __init__(self, base_version: Any, current_version: Any,
                 current_sha256: str | None = None):
        super().__init__(
            f"baseVersion {base_version} non è più la versione corrente ({current_version})",
            {"baseVersion": base_version, "currentVersion": current_version,
             "currentSha256": current_sha256})
        self.base_version = base_version
        self.current_version = current_version
        self.current_sha256 = current_sha256


class DocumentRejectedError(InventoryError):
    """Lo schema congelato del documento non è rispettato (§8.16)."""
    code = "document_rejected"


class IdentityRejectedError(InventoryError):
    """La transizione di identità non è ammissibile (§8.4)."""
    code = "identity_rejected"


class NotAuthorizedError(InventoryError):
    """La politica ha respinto l'insieme di eventi (§8.15)."""
    code = "not_authorized"
