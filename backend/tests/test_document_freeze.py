"""Schema congelato del documento (§8.16) ed eventi non supportati (§8.15)."""
from __future__ import annotations

import pytest

from app.authz import (
    FORBIDDEN_FOR_ROLE,
    UNSUPPORTED_DOMAIN_EVENT,
    authorize_events,
)
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import (
    ALLOWED_ROOT_KEYS,
    DOCUMENT_TOO_LARGE,
    EMBEDDED_PASSWORD,
    EMBEDDED_PHOTO_DATA,
    EXTRACTED_ROOT_KEYS,
    FORBIDDEN_ROOT_KEY,
    INVALID_PHOTO_REFERENCE,
    SCHEMA_VERSION_CHANGED,
    UNKNOWN_ROOT_KEY,
    strip_legacy_fields,
    validate_normal_document,
)

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RACK = "cccccccc-0000-4000-8000-00000000000a"
DEV = "dddddddd-0000-4000-8000-00000000000a"


def doc(**extra) -> dict:
    d = {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
            {"_uid": ROOM, "id": "r", "nome": "R", "w": 5, "h": 4, "vani": [], "racks": [
                {"_uid": RACK, "id": "R1", "name": "R1", "u": 45,
                 "x": 0, "y": 0, "w": 0.6, "h": 0.8, "devices": [
                    {"_uid": DEV, "id": "d1", "name": "d1", "u": 10}]}]}]}],
    }
    d.update(extra)
    return d


def codes(errors) -> set[str]:
    return {e.code for e in errors}


# ------------------------------------------------------------ forma accettata

def test_minimal_valid_document_accepted():
    assert validate_normal_document(doc()) == []


def test_manuale_is_allowed():
    d = doc(manuale=[{"_uid": "eeeeeeee-0000-4000-8000-000000000001",
                      "id": "m1", "titolo": "T", "blocchi": []}])
    assert validate_normal_document(d) == []


def test_allowlist_is_the_frozen_shape():
    assert ALLOWED_ROOT_KEYS == {"schemaVersion", "locations", "manuale"}


# ------------------------------------------------- radici estratte e legacy

@pytest.mark.parametrize("key", sorted(EXTRACTED_ROOT_KEYS))
def test_every_extracted_root_is_rejected(key):
    """utenti, registro, impostazioni, smtp, notifiche, versione: nessuno di
    questi vive più nel documento, e vanno rifiutati, non ripuliti in silenzio."""
    errors = validate_normal_document(doc(**{key: {} if key != "versione" else 3}))
    assert FORBIDDEN_ROOT_KEY in codes(errors), f"{key}: {codes(errors)}"


def test_extracted_roots_cover_the_named_fields():
    for k in ("users", "utenti", "audit", "registro", "settings", "notifiche", "smtp"):
        assert k in EXTRACTED_ROOT_KEYS


def test_unknown_root_key_rejected_separately():
    """Una chiave ignota non è la stessa cosa di una estratta: messaggi diversi
    perché le cause sono diverse (client sperimentale vs migrazione dimenticata)."""
    errors = validate_normal_document(doc(qualcosa_di_nuovo=1))
    assert UNKNOWN_ROOT_KEY in codes(errors)
    assert FORBIDDEN_ROOT_KEY not in codes(errors)


def test_multiple_forbidden_roots_all_reported():
    errors = validate_normal_document(doc(utenti=[], registro=[], smtp={}))
    assert len([e for e in errors if e.code == FORBIDDEN_ROOT_KEY]) == 3


# ---------------------------------------------------------------- password

def test_embedded_password_rejected_at_root():
    errors = validate_normal_document(doc(password="x"))
    assert EMBEDDED_PASSWORD in codes(errors)


def test_embedded_password_rejected_when_nested_deeply():
    """Una credenziale va trovata anche se nascosta in un ramo che lo schema non
    prevede: il controllo non si fida della struttura."""
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["devices"][0]["password"] = "segreto"
    assert EMBEDDED_PASSWORD in codes(validate_normal_document(d))


def test_password_like_key_names_rejected():
    d = doc()
    d["locations"][0]["smtpPassword"] = "x"
    assert EMBEDDED_PASSWORD in codes(validate_normal_document(d))


# ------------------------------------------------------------------- foto

def test_base64_photo_rejected():
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["foto"] = \
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    assert EMBEDDED_PHOTO_DATA in codes(validate_normal_document(d))


def test_photo_uuid_reference_accepted():
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["foto"] = \
        "ffffffff-0000-4000-8000-00000000000f"
    assert validate_normal_document(d) == []


def test_photo_absent_accepted():
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["foto"] = None
    assert validate_normal_document(d) == []


def test_photo_garbage_reference_rejected():
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["foto"] = {"bytes": [1, 2, 3]}
    assert INVALID_PHOTO_REFERENCE in codes(validate_normal_document(d))


# --------------------------------------------------------- schemaVersion

def test_missing_uids_rejected():
    d = doc()
    del d["locations"][0]["sale"][0]["racks"][0]["devices"][0]["_uid"]
    assert "missing_uid" in codes(validate_normal_document(d))


def test_client_cannot_change_schema_version():
    """Un salvataggio non fa evolvere lo schema: serve una migrazione (§8.13)."""
    d = doc(schemaVersion=CURRENT_SCHEMA_VERSION)
    errors = validate_normal_document(d, current_schema_version=CURRENT_SCHEMA_VERSION)
    assert errors == []

    # il client dichiara una versione diversa da quella in testa
    d2 = doc(schemaVersion=CURRENT_SCHEMA_VERSION + 1)
    errors = validate_normal_document(d2, current_schema_version=CURRENT_SCHEMA_VERSION)
    assert SCHEMA_VERSION_CHANGED in codes(errors)


def test_missing_schema_version_rejected():
    d = doc()
    del d["schemaVersion"]
    assert "schema_version_missing" in codes(validate_normal_document(d))


# ------------------------------------------------------------- dimensione

def test_oversized_document_rejected():
    d = doc()
    d["locations"][0]["sale"][0]["racks"][0]["devices"][0]["note"] = "x" * 5000
    errors = validate_normal_document(d, max_bytes=1000)
    assert DOCUMENT_TOO_LARGE in codes(errors)


def test_non_object_rejected():
    for bad in ([], "stringa", 3, None):
        assert validate_normal_document(bad), repr(bad)


# --------------------------------------------- migrazione: consuma e toglie

def test_strip_legacy_fields_removes_extracted_roots():
    legacy = doc(utenti=[{"email": "admin"}], registro=[], notifiche={},
                 smtp={"password": "p"}, versione=3)
    cleaned, removed = strip_legacy_fields(legacy)
    assert set(cleaned) <= ALLOWED_ROOT_KEYS
    for k in ("utenti", "registro", "notifiche", "smtp", "versione"):
        assert k in removed
    assert validate_normal_document(cleaned) == []


def test_strip_legacy_sets_current_schema_version():
    legacy = doc()
    del legacy["schemaVersion"]
    cleaned, _ = strip_legacy_fields(legacy)
    assert cleaned["schemaVersion"] == CURRENT_SCHEMA_VERSION


def test_strip_legacy_is_the_only_path_that_removes():
    """Il repository normale non ripulisce: rifiuta. Se ripulisse, salverebbe un
    documento diverso da quello inviato e il client crederebbe altro."""
    legacy = doc(utenti=[])
    assert validate_normal_document(legacy)          # rifiutato
    cleaned, _ = strip_legacy_fields(legacy)
    assert validate_normal_document(cleaned) == []   # solo dopo la migrazione


# ================================================================
# Eventi non supportati (§8.15): nessun ruolo li rende accettabili
# ================================================================

@pytest.mark.parametrize("bad_event", [
    {"entity": "galassia", "event": "update", "scope": "?"},          # entità ignota
    {"entity": "device", "event": "teleport", "scope": "devices"},    # evento ignoto
    {"entity": "", "event": "update", "scope": "devices"},            # entità vuota
    {"entity": "device", "event": "", "scope": "devices"},            # evento vuoto
    {"event": "update", "scope": "devices"},                          # entità assente
    {"entity": "device", "scope": "devices"},                         # evento assente
    {"entity": 42, "event": "update"},                                # tipi sbagliati
    {"entity": "device", "event": ["update"]},
    "non-un-evento",
    None,
    123,
])
def test_unsupported_event_denied_for_every_role(bad_event):
    for role in ("view", "edit", "admin"):
        d = authorize_events(role, [bad_event])
        assert not d.allowed, f"{role} ha accettato {bad_event!r}"
        assert d.violations[0].code == UNSUPPORTED_DOMAIN_EVENT, \
            f"{role} {bad_event!r} → {d.violations[0].code}"


def test_unsupported_event_has_no_required_role():
    """`requiredRole` vuoto significa: nessun privilegio aiuta. Il problema non è
    il permesso ma il significato."""
    d = authorize_events("admin", [{"entity": "galassia", "event": "update"}])
    assert d.violations[0].required_role == ""
    assert d.violations[0].as_dict()["requiredRole"] == ""


def test_known_but_restricted_is_distinct_from_unsupported():
    """La distinzione che questa correzione introduce: un rack update è NOTO ma
    ristretto (admin passa), un'entità ignota non è interpretabile (admin non
    passa)."""
    restricted = [{"entity": "rack", "event": "update", "scope": "structure"}]
    assert authorize_events("edit", restricted).violations[0].code == FORBIDDEN_FOR_ROLE
    assert authorize_events("admin", restricted).allowed

    unsupported = [{"entity": "galassia", "event": "update", "scope": "?"}]
    assert authorize_events("edit", unsupported).violations[0].code == UNSUPPORTED_DOMAIN_EVENT
    assert not authorize_events("admin", unsupported).allowed


def test_mixed_supported_and_unsupported_rejects_whole_set():
    events = [{"entity": "device", "event": "update", "scope": "devices", "uid": "d1"},
              {"entity": "galassia", "event": "update", "scope": "?"}]
    d = authorize_events("admin", events)
    assert not d.allowed
    assert [v.code for v in d.violations] == [UNSUPPORTED_DOMAIN_EVENT]


def test_real_engine_events_are_all_supported(fixture_valid):
    """Nessun evento prodotto dal motore reale deve risultare non supportato:
    il vocabolario chiuso e il motore devono restare allineati."""
    from app.identity import diff_as_dicts
    events = diff_as_dicts(fixture_valid["before"], fixture_valid["after"])
    d = authorize_events("admin", events)
    unsupported = [v for v in d.violations if v.code == UNSUPPORTED_DOMAIN_EVENT]
    assert not unsupported, f"{fixture_valid['name']}: {[v.as_dict() for v in unsupported]}"
