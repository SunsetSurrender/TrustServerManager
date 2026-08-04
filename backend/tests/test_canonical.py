"""Canonicalizzazione: purezza, idempotenza e assenza di eventi spuri."""
from __future__ import annotations

import copy
import json

import pytest

from app.identity import (
    CURRENT_SCHEMA_VERSION,
    ENTITY_DEFAULTS,
    canonical_sort,
    canonicalise,
    diff_documents,
    strip_uids,
    walk,
)

UID = "dddddddd-0000-4000-8000-00000000000a"


def _doc(device: dict) -> dict:
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{"_uid": "aaaaaaaa-0000-4000-8000-000000000001", "id": "s", "nome": "S",
                       "sale": [{"_uid": "bbbbbbbb-0000-4000-8000-000000000001", "id": "r",
                                 "nome": "R", "w": 5, "h": 4, "vani": [], "racks": [
            {"_uid": "cccccccc-0000-4000-8000-00000000000a", "id": "R1", "name": "R1",
             "u": 45, "x": 0, "y": 0, "w": 0.6, "h": 0.8, "devices": [device]}]}]}],
    }


# ------------------------------------------------------------------ proprietà

def test_is_pure_does_not_mutate_input(fixture_any):
    before = copy.deepcopy(fixture_any["after"])
    canonicalise(fixture_any["after"])
    assert fixture_any["after"] == before, "canonicalise ha modificato l'input"


def test_is_idempotent(fixture_any):
    once = canonicalise(fixture_any["after"])
    twice = canonicalise(once)
    assert json.dumps(canonical_sort(once), default=str) == \
           json.dumps(canonical_sort(twice), default=str)


def test_never_invents_uids():
    """Il backfill è solo dello script di migrazione (§8.4): canonicalizzare non
    deve fabbricare identità."""
    doc = _doc({"id": "d1", "name": "d1", "u": 10})       # dispositivo senza _uid
    out = canonicalise(doc)
    devices = [e for e in walk(out) if e.kind == "device"]
    assert devices[0].uid is None


def test_never_invents_schema_version():
    """Un documento senza schemaVersion va rifiutato, non aggiornato in silenzio."""
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    del doc["schemaVersion"]
    assert "schemaVersion" not in canonicalise(doc)


def test_never_invents_settings_objects():
    """Inventare `notifiche` o `smtp` in un documento che non li ha mai avuti
    farebbe apparire modifiche che l'utente non ha fatto."""
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    out = canonicalise(doc)
    assert "notifiche" not in out
    assert "smtp" not in out


def test_settings_subfields_filled_when_object_exists():
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    doc["notifiche"] = {"attive": True}
    out = canonicalise(doc)
    assert out["notifiche"] == {"email": "", "giorni": 30, "attive": True}


def test_smtp_password_is_never_materialised():
    """La password SMTP non vive nel documento (§8.7): materializzarla come
    stringa vuota la reintrodurrebbe nello schema."""
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    doc["smtp"] = {"host": "mail.example"}
    out = canonicalise(doc)
    assert "password" not in out["smtp"]
    assert out["smtp"]["porta"] == 587


# ------------------------------------------------------- default documentati

@pytest.mark.parametrize("field,expected", [("stato", "attivo"), ("h", 1), ("type", "altro"),
                                            ("model", ""), ("note", "")])
def test_device_defaults(field, expected):
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    out = canonicalise(doc)
    device = out["locations"][0]["sale"][0]["racks"][0]["devices"][0]
    assert device[field] == expected


def test_explicit_falsy_values_are_preserved():
    """Una stringa vuota o uno zero espliciti sono valori dell'utente, non
    assenze: non vanno sostituiti."""
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10, "note": "", "h": 3})
    device = canonicalise(doc)["locations"][0]["sale"][0]["racks"][0]["devices"][0]
    assert device["note"] == ""
    assert device["h"] == 3


def test_false_is_preserved_not_defaulted():
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    doc["locations"][0]["sale"][0]["segnaposto"] = False
    room = canonicalise(doc)["locations"][0]["sale"][0]
    assert room["segnaposto"] is False


# ---------------------------------------------- nessun evento spurio nel diff

def test_missing_to_default_produces_no_events():
    """Il motivo per cui la canonicalizzazione esiste: un import che scrive
    esplicitamente i default non deve generare un update per ogni dispositivo."""
    sparse = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    explicit = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10,
                     "stato": "attivo", "h": 1, "type": "altro", "model": "",
                     "ip": "", "serial": "", "owner": "", "garanzia": "",
                     "supporto": "", "note": ""})
    assert diff_documents(sparse, explicit) == []
    assert diff_documents(explicit, sparse) == []


def test_real_change_still_detected_after_canonicalisation():
    """La canonicalizzazione non deve nascondere le modifiche vere."""
    a = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    b = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10, "stato": "manutenzione"})
    events = diff_documents(a, b)
    assert [e.event for e in events] == ["update"]
    assert events[0].changes == {"stato": ["attivo", "manutenzione"]}


def test_default_to_missing_also_produces_no_events():
    """Simmetrico: rimuovere un campo che valeva il default non è una modifica."""
    explicit = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10, "h": 1})
    sparse = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    assert diff_documents(explicit, sparse) == []


def test_rack_defaults_do_not_diff():
    a = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    b = copy.deepcopy(a)
    b["locations"][0]["sale"][0]["racks"][0]["seriali"] = []
    b["locations"][0]["sale"][0]["racks"][0]["row"] = ""
    assert diff_documents(a, b) == []


def test_strip_uids_removes_all_of_them():
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    assert "_uid" not in json.dumps(strip_uids(doc))


def test_canonical_sort_is_stable_under_key_shuffle():
    doc = _doc({"_uid": UID, "id": "d1", "name": "d1", "u": 10})
    shuffled = json.loads(json.dumps(doc))
    a = json.dumps(canonical_sort(canonicalise(doc)), default=str)
    b = json.dumps(canonical_sort(canonicalise(shuffled)), default=str)
    assert a == b


def test_defaults_table_matches_documented_kinds():
    assert set(ENTITY_DEFAULTS) == {"location", "room", "rack", "device", "manual"}
    assert "vano" not in ENTITY_DEFAULTS      # i vani non sono entità (§8.12)
