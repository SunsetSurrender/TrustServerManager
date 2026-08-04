"""Versione di schema: distinta dalla revisione, legacy rifiutata."""
from __future__ import annotations

import pytest

from app.identity import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION_INVALID,
    SCHEMA_VERSION_MISSING,
    SCHEMA_VERSION_TOO_NEW,
    SCHEMA_VERSION_TOO_OLD,
    check_schema_version,
    is_migratable,
)


def test_current_version_accepted():
    assert check_schema_version({"schemaVersion": CURRENT_SCHEMA_VERSION}) == []


def test_missing_version_is_legacy():
    errors = check_schema_version({"locations": []})
    assert [e.code for e in errors] == [SCHEMA_VERSION_MISSING]
    assert "migrat" in errors[0].message.lower()


def test_none_document_is_legacy():
    assert [e.code for e in check_schema_version(None)] == [SCHEMA_VERSION_MISSING]


def test_older_version_requires_migration():
    errors = check_schema_version({"schemaVersion": CURRENT_SCHEMA_VERSION - 1})
    assert [e.code for e in errors] == [SCHEMA_VERSION_TOO_OLD]


def test_newer_version_rejected():
    """Un client più nuovo del server: non si può indovinare cosa comprende."""
    errors = check_schema_version({"schemaVersion": CURRENT_SCHEMA_VERSION + 1})
    assert [e.code for e in errors] == [SCHEMA_VERSION_TOO_NEW]


@pytest.mark.parametrize("bad", ["1", 1.5, True, [], {}, "v1"])
def test_non_integer_version_rejected(bad):
    """`True` è un int in Python: se passasse, un booleano diventerebbe la
    versione 1. Va rifiutato esplicitamente."""
    errors = check_schema_version({"schemaVersion": bad})
    assert [e.code for e in errors] == [SCHEMA_VERSION_INVALID], f"{bad!r}"


def test_legacy_documents_are_migratable():
    assert is_migratable({"schemaVersion": None}) is True
    assert is_migratable({}) is True
    assert is_migratable({"schemaVersion": CURRENT_SCHEMA_VERSION}) is True


def test_future_versions_are_not_migratable():
    assert is_migratable({"schemaVersion": CURRENT_SCHEMA_VERSION + 1}) is False


def test_schema_version_is_independent_of_revision():
    """La revisione ottimistica conta le modifiche ai dati; la versione di schema
    descrive la forma. Un documento alla revisione 900 è ancora schema 1."""
    doc = {"schemaVersion": CURRENT_SCHEMA_VERSION, "versione": 3, "locations": []}
    assert check_schema_version(doc) == []
    # il campo `versione` del prototipo non ha alcun ruolo
    doc["versione"] = 99
    assert check_schema_version(doc) == []


def test_all_fixtures_declare_current_schema_version(fixture_any):
    for side in ("before", "after"):
        assert check_schema_version(fixture_any[side]) == [], f"{fixture_any['name']}/{side}"
