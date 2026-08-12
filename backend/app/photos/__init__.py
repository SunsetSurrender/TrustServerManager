"""Foto dei rack: oggetti binari immutabili, fuori dal documento versionato.

    validate.py    che cosa può diventare una foto (formati, limiti, metadati)
    repository.py  conservazione indirizzata dal contenuto, deduplicata
    refs.py        quale versione dell'inventario usa quale foto
    gc.py          l'unico posto che cancella byte, e gira nel worker

Riferimento: BACKEND-PLAN.md §8.5.
"""
from app.photos.errors import PhotoError, PhotoNotFound, PhotoRejected, PhotoTooLarge
from app.photos.validate import (
    ALLOWED_FORMATS,
    MAX_PIXELS,
    MAX_UPLOAD_BYTES,
    NormalisedImage,
    normalise,
)

__all__ = [
    "ALLOWED_FORMATS",
    "MAX_PIXELS",
    "MAX_UPLOAD_BYTES",
    "NormalisedImage",
    "PhotoError",
    "PhotoNotFound",
    "PhotoRejected",
    "PhotoTooLarge",
    "normalise",
]
