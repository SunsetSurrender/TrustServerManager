"""Errori del dominio foto. Codici stabili, messaggi per chi guarda lo schermo.

Nessun messaggio riporta il nome del file caricato: è testo scelto dal chiamante
e non deve tornare in una risposta né in un'intestazione (§8.5).
"""
from __future__ import annotations


class PhotoError(Exception):
    code = "photo_error"

    def __init__(self, message: str, code: str | None = None, **extra):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra


class PhotoRejected(PhotoError):
    """Il contenuto caricato non è un'immagine accettabile."""
    code = "photo_rejected"


class PhotoTooLarge(PhotoError):
    code = "photo_too_large"


class PhotoNotFound(PhotoError):
    """L'UUID non corrisponde a nessuna foto.

    Serve in due posti con lo stesso significato e due stati HTTP diversi:
    `GET /api/photos/{id}` risponde 404, un documento di inventario che referenzia
    una foto inesistente risponde 422 — là non è una risorsa mancante, è un
    documento non valido.
    """
    code = "photo_not_found"
