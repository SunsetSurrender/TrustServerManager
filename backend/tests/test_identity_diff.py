"""Motore di diff identity-aware — guidato dalle fixture + proprietà."""
from __future__ import annotations

import json

import pytest

from app.identity import diff_as_dicts, diff_documents, scopes_touched


def _normalise(events: list[dict]) -> list[dict]:
    """Confronto sui soli campi dichiarati nelle fixture.

    `path` è informativo (dipende dai codici al momento del diff) e non fa parte
    del contratto; i campi non dichiarati da una fixture non vengono confrontati,
    così una fixture può essere precisa quanto serve senza dover elencare tutto.
    """
    return events


def _matches(expected: dict, got: dict) -> bool:
    for k, v in expected.items():
        if k not in got:
            return False
        if got[k] != v:
            return False
    return True


# ------------------------------------------------------------- dalle fixture

def test_fixture_events(fixture_with_events):
    """Ogni fixture valida dichiara gli eventi di dominio attesi."""
    expected = fixture_with_events["expectedEvents"]
    got = diff_as_dicts(fixture_with_events["before"], fixture_with_events["after"])

    assert len(got) == len(expected), (
        f"{fixture_with_events['name']}: attesi {len(expected)} eventi, "
        f"ottenuti {len(got)}\nattesi: {json.dumps(expected, indent=2)}\n"
        f"ottenuti: {json.dumps(got, indent=2, default=str)}"
    )
    for i, (exp, actual) in enumerate(zip(expected, got)):
        assert _matches(exp, actual), (
            f"{fixture_with_events['name']}: evento {i} non corrisponde\n"
            f"atteso:   {json.dumps(exp, indent=2)}\n"
            f"ottenuto: {json.dumps(actual, indent=2, default=str)}"
        )


def test_diff_is_deterministic(fixture_with_events):
    """Stessi input → stesso output, ripetutamente. L'audit deve essere
    riproducibile."""
    a = diff_as_dicts(fixture_with_events["before"], fixture_with_events["after"])
    b = diff_as_dicts(fixture_with_events["before"], fixture_with_events["after"])
    assert json.dumps(a, default=str) == json.dumps(b, default=str)


def test_no_change_produces_no_events():
    from tests.conftest import ALL_FIXTURES
    for f in ALL_FIXTURES:
        assert diff_documents(f["before"], f["before"]) == [], f["name"]


def test_change_sets_are_sorted(fixture_with_events):
    """Le chiavi dei dizionari di modifiche sono ordinate: senza questo il JSON
    dell'audit cambierebbe a parità di modifica."""
    for ev in diff_as_dicts(fixture_with_events["before"], fixture_with_events["after"]):
        if "changes" in ev:
            keys = list(ev["changes"])
            assert keys == sorted(keys), ev


# --------------------------------------------------------------- proprietà

def test_scopes_touched_is_sorted_and_unique():
    from tests.conftest import ALL_FIXTURES
    for f in ALL_FIXTURES:
        if not f["expectedValid"]:
            continue
        s = scopes_touched(diff_documents(f["before"], f["after"]))
        assert s == sorted(set(s))


def test_device_move_is_devices_scope_not_structure():
    """Il caso che giustifica il diff identity-aware: spostare un dispositivo
    tocca due sottoalberi di rack, ma l'ambito è `devices` (gli operatori devono
    poterlo fare). Un diff per percorso direbbe `structure`."""
    from tests.conftest import ALL_FIXTURES
    fx = next(f for f in ALL_FIXTURES if f["name"] == "move-device-between-racks")
    events = diff_documents(fx["before"], fx["after"])
    assert [e.event for e in events] == ["move"]
    assert scopes_touched(events) == ["devices"]


def test_insert_at_head_does_not_touch_siblings():
    """Inserire un rack in testa NON deve far sembrare modificati i fratelli:
    è il difetto tipico del confronto per indice di array."""
    from tests.conftest import ALL_FIXTURES
    fx = next(f for f in ALL_FIXTURES if f["name"] == "reorder-suppressed-by-add")
    events = diff_documents(fx["before"], fx["after"])
    assert [e.event for e in events] == ["add"]


def test_reorder_suppressed_when_membership_changes():
    """Con add o delete fra i fratelli il reorder non si emette."""
    from tests.conftest import ALL_FIXTURES
    for name in ("reorder-suppressed-by-add", "reorder-suppressed-by-delete"):
        fx = next(f for f in ALL_FIXTURES if f["name"] == name)
        events = diff_documents(fx["before"], fx["after"])
        assert not any(e.event == "reorder" for e in events), name


def test_rename_and_move_are_separate_events():
    """Non vanno fusi: gli ambiti potrebbero differire e l'autorizzazione lavora
    per evento."""
    from tests.conftest import ALL_FIXTURES
    fx = next(f for f in ALL_FIXTURES if f["name"] == "rename-and-move")
    events = diff_documents(fx["before"], fx["after"])
    assert [e.event for e in events] == ["rename", "move"]
    assert len({e.uid for e in events}) == 1     # stessa entità


def test_height_is_update_not_move():
    """L'altezza in U è una dimensione, non una posizione."""
    from tests.conftest import ALL_FIXTURES
    fx = next(f for f in ALL_FIXTURES if f["name"] == "update-device-height")
    events = diff_documents(fx["before"], fx["after"])
    assert [e.event for e in events] == ["update"]


@pytest.mark.parametrize("event_name", [
    "add", "delete", "update", "move", "rename", "reorder",
])
def test_every_event_type_is_exercised(event_name):
    """Nessun tipo di evento senza fixture."""
    from tests.conftest import EVENT_FIXTURES
    seen = {ev["event"] for f in EVENT_FIXTURES for ev in f["expectedEvents"]}
    assert event_name in seen, f"nessuna fixture produce un evento {event_name}"


def test_events_stable_under_key_reordering():
    """Il diff non deve dipendere dall'ordine delle chiavi nei dict di input:
    JSON e psycopg non garantiscono un ordine, e l'output deve essere lo stesso."""
    from tests.conftest import ALL_FIXTURES

    def shuffle_keys(v):
        if isinstance(v, list):
            return [shuffle_keys(x) for x in v]
        if isinstance(v, dict):
            return {k: shuffle_keys(v[k]) for k in reversed(list(v))}
        return v

    for f in ALL_FIXTURES:
        if not f["expectedValid"]:
            continue
        a = diff_as_dicts(f["before"], f["after"])
        b = diff_as_dicts(shuffle_keys(f["before"]), shuffle_keys(f["after"]))
        assert json.dumps(a, default=str) == json.dumps(b, default=str), f["name"]
