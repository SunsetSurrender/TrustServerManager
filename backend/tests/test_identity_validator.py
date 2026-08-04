"""Validatore di identità — guidato dalle fixture condivise + casi mirati."""
from __future__ import annotations

import pytest

from app.identity import (
    AMBIGUOUS_REPLACEMENT,
    BUSINESS_KEY_REUSE,
    DUPLICATE_UID,
    IDENTITY_REPLACEMENT,
    MALFORMED_UID,
    MISSING_UID,
    is_uid,
    validate_against_base,
    validate_document,
    walk,
)


# --------------------------------------------------------------- dalle fixture

def test_fixture_validity(fixture_any):
    """Ogni fixture dichiara se `after` è ammissibile rispetto a `before`."""
    errors = validate_against_base(fixture_any["before"], fixture_any["after"])
    assert bool(errors) == (not fixture_any["expectedValid"]), (
        f"{fixture_any['name']}: attesa validità={fixture_any['expectedValid']}, "
        f"errori={[e.as_dict() for e in errors]}"
    )


def test_fixture_error_codes(fixture_invalid):
    """I codici di errore sono un contratto stabile fra frontend e backend."""
    errors = validate_against_base(fixture_invalid["before"], fixture_invalid["after"])
    got = sorted({e.code for e in errors})
    expected = sorted(set(fixture_invalid["expectedErrorCodes"]))
    assert set(expected).issubset(set(got)), (
        f"{fixture_invalid['name']}: attesi {expected}, ottenuti {got}"
    )


def test_valid_fixtures_have_no_errors(fixture_valid):
    assert validate_against_base(fixture_valid["before"], fixture_valid["after"]) == []


# ------------------------------------------------------------- casi mirati

def test_is_uid_accepts_v4_only():
    assert is_uid("aaaaaaaa-0000-4000-8000-000000000001")
    assert not is_uid("11111111-1111-1111-1111-111111111111")   # non v4
    assert not is_uid("aaaaaaaa-0000-4000-c000-000000000001")   # variant errata
    assert not is_uid("non-un-uuid")
    assert not is_uid("")
    assert not is_uid(None)
    assert not is_uid(12345)


def test_empty_document_is_valid():
    assert validate_document({}) == []
    assert validate_document(None) == []


def test_vani_are_not_entities(fixture_valid):
    """I vani non hanno identità: non devono mai comparire fra le entità."""
    kinds = {e.kind for e in walk(fixture_valid["after"])}
    assert "vano" not in kinds


def test_missing_uid_stops_differential_check():
    """Con un documento internamente incoerente si riportano quegli errori e si
    smette: proseguire produrrebbe rumore su un albero di cui non ci si fida."""
    base = {"locations": [{"_uid": "aaaaaaaa-0000-4000-8000-000000000001",
                           "id": "s", "nome": "S", "sale": []}]}
    nxt = {"locations": [{"id": "s", "nome": "S", "sale": []}]}
    errors = validate_against_base(base, nxt)
    assert [e.code for e in errors] == [MISSING_UID]


def test_duplicate_uid_reported_once_per_collision():
    uid = "dddddddd-0000-4000-8000-00000000000a"
    doc = {"locations": [{"_uid": "aaaaaaaa-0000-4000-8000-000000000001", "id": "s",
                          "nome": "S", "sale": [
        {"_uid": "bbbbbbbb-0000-4000-8000-000000000001", "id": "r", "nome": "R", "racks": [
            {"_uid": "cccccccc-0000-4000-8000-00000000000a", "id": "R1", "name": "R1",
             "devices": [{"_uid": uid, "id": "d1", "name": "d1"},
                         {"_uid": uid, "id": "d2", "name": "d2"}]}]}]}]}
    codes = [e.code for e in validate_document(doc)]
    assert codes == [DUPLICATE_UID]


def test_genuine_add_is_allowed_even_with_unrelated_delete():
    """Un delete e un add non correlati sono due eventi legittimi, non una
    sostituzione: il validatore non deve confonderli."""
    loc = "aaaaaaaa-0000-4000-8000-000000000001"
    room = "bbbbbbbb-0000-4000-8000-000000000001"
    rack = "cccccccc-0000-4000-8000-00000000000a"

    def doc(devices):
        return {"locations": [{"_uid": loc, "id": "s", "nome": "S", "sale": [
            {"_uid": room, "id": "r", "nome": "R", "racks": [
                {"_uid": rack, "id": "R1", "name": "R1", "devices": devices}]}]}]}

    a = {"_uid": "dddddddd-0000-4000-8000-00000000000a", "id": "srv-a", "name": "srv-a"}
    b = {"_uid": "dddddddd-0000-4000-8000-00000000000b", "id": "srv-b", "name": "srv-b"}
    assert validate_against_base(doc([a]), doc([b])) == []


@pytest.mark.parametrize("code", [
    MISSING_UID, MALFORMED_UID, DUPLICATE_UID,
    IDENTITY_REPLACEMENT, BUSINESS_KEY_REUSE, AMBIGUOUS_REPLACEMENT,
])
def test_every_rejection_code_is_exercised_by_a_fixture(code):
    """Nessun codice di rifiuto deve restare senza fixture: un codice non
    esercitato è un ramo non testato."""
    from tests.conftest import INVALID_FIXTURES
    covered = {c for f in INVALID_FIXTURES for c in f["expectedErrorCodes"]}
    assert code in covered, f"nessuna fixture copre {code}"


def test_same_parent_match_wins_over_ambiguity():
    """Se nello stesso genitore esiste un'entità con quel codice, la diagnosi è
    la più specifica — identity_replacement — anche se altrove ce ne sono altre
    con lo stesso codice. Sapere QUALE entità è stata sostituita è più utile che
    dichiarare l'ambiguità."""
    loc = "aaaaaaaa-0000-4000-8000-000000000001"
    room = "bbbbbbbb-0000-4000-8000-000000000001"
    ra, rb = "cccccccc-0000-4000-8000-00000000000a", "cccccccc-0000-4000-8000-00000000000b"

    def rack(uid, devices):
        return {"_uid": uid, "id": "R" + uid[-1], "name": "R", "devices": devices}

    def dev(uid):
        return {"_uid": uid, "id": "srv-x", "name": "srv-x"}

    def doc(a_devs, b_devs):
        return {"locations": [{"_uid": loc, "id": "s", "nome": "S", "sale": [
            {"_uid": room, "id": "r", "nome": "R",
             "racks": [rack(ra, a_devs), rack(rb, b_devs)]}]}]}

    base = doc([dev("dddddddd-0000-4000-8000-00000000000a")],
               [dev("dddddddd-0000-4000-8000-00000000000b")])
    nxt = doc([], [dev("ffffffff-0000-4000-8000-00000000000f")])
    errors = validate_against_base(base, nxt)
    assert {e.code for e in errors} == {IDENTITY_REPLACEMENT}
    # e indica precisamente quale identità è stata rimpiazzata
    assert errors[0].replaced_uid == "dddddddd-0000-4000-8000-00000000000b"


def test_ambiguous_replacement_when_no_same_parent_candidate():
    """Ambiguità vera: il codice non esisteva nel genitore di destinazione e DUE
    entità con quel codice sono scomparse altrove. Non si indovina."""
    loc = "aaaaaaaa-0000-4000-8000-000000000001"
    room = "bbbbbbbb-0000-4000-8000-000000000001"
    ra, rb, rc = ("cccccccc-0000-4000-8000-00000000000a",
                  "cccccccc-0000-4000-8000-00000000000b",
                  "cccccccc-0000-4000-8000-00000000000c")

    def rack(uid, devices):
        return {"_uid": uid, "id": "R" + uid[-1], "name": "R", "devices": devices}

    def dev(uid):
        return {"_uid": uid, "id": "srv-x", "name": "srv-x"}

    def doc(a, b, c):
        return {"locations": [{"_uid": loc, "id": "s", "nome": "S", "sale": [
            {"_uid": room, "id": "r", "nome": "R",
             "racks": [rack(ra, a), rack(rb, b), rack(rc, c)]}]}]}

    base = doc([dev("dddddddd-0000-4000-8000-00000000000a")],
               [dev("dddddddd-0000-4000-8000-00000000000b")], [])
    nxt = doc([], [], [dev("ffffffff-0000-4000-8000-00000000000f")])
    codes = {e.code for e in validate_against_base(base, nxt)}
    assert AMBIGUOUS_REPLACEMENT in codes
