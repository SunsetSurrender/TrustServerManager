"""Impostazioni dell'applicazione: schema tipizzato e persistenza con revisione.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from app.settings.repository import (
    SETTINGS_UPDATED,
    SettingsCorrupted,
    SettingsError,
    SettingsMissing,
    SettingsRow,
    SettingsVersionConflict,
    copy_notifications,
    load,
    save,
    to_response,
)
from app.settings.schema import (
    DEFAULTS,
    MAX_RECIPIENTS,
    MAX_SETTINGS_BYTES,
    SettingsValidationError,
    canonicalise,
    default_document,
)

__all__ = [
    "DEFAULTS", "MAX_RECIPIENTS", "MAX_SETTINGS_BYTES", "SETTINGS_UPDATED",
    "SettingsCorrupted", "SettingsError", "SettingsMissing", "SettingsRow",
    "SettingsValidationError", "SettingsVersionConflict",
    "canonicalise", "copy_notifications", "default_document", "load", "save",
    "to_response",
]
