"""Politica di autorizzazione — guidata dalle fixture di fixtures/policy/."""
from __future__ import annotations

import json

import pytest

from app.authz import (
    EDIT_DEVICE_EVENTS,
    FORBIDDEN_FOR_ROLE,
    ROLLBACK_FORBIDDEN,
    UNKNOWN_ROLE,
    authorize_events,
    authorize_rollback,
)
from app.identity import diff_as_dicts

from tests.conftest import IDENTITY_BY_NAME, POLICY_FIXTURES


def _events_of(fx: dict) -> list[dict]:
    """Eventi della fixture: espliciti, oppure calcolati dal motore di diff reale
    a partire da una fixture di identità."""
    if "fromIdentityFixture" in fx:
        src = IDENTITY_BY_NAME[fx["fromIdentityFixture"]]
        return diff_as_dicts(src["before"], src["after"])
    return fx["events"]


def _matches(expected: dict, got: dict) -> bool:
    return all(k in got and got[k] == v for k, v in expected.items())


@pytest.mark.parametrize("fx", POLICY_FIXTURES, ids=[f["name"] for f in POLICY_FIXTURES])
def test_policy_fixture(fx):
    if fx.get("operation") == "rollback":
        decision = authorize_rollback(fx["role"])
    else:
        decision = authorize_events(fx["role"], _events_of(fx))

    assert decision.allowed == fx["expectedAllowed"], (
        f"{fx['name']}: atteso allowed={fx['expectedAllowed']}, "
        f"violazioni={[v.as_dict() for v in decision.violations]}"
    )

    expected = fx["expectedViolations"]
    got = [v.as_dict() for v in decision.violations]
    assert len(got) == len(expected), (
        f"{fx['name']}: attese {len(expected)} violazioni, ottenute {len(got)}\n"
        f"attese: {json.dumps(expected, indent=2)}\nottenute: {json.dumps(got, indent=2)}"
    )
    for i, (exp, actual) in enumerate(zip(expected, got)):
        assert _matches(exp, actual), (
            f"{fx['name']}: violazione {i}\natteso:   {json.dumps(exp, indent=2)}\n"
            f"ottenuto: {json.dumps(actual, indent=2)}"
        )


# ------------------------------------------------------------------ proprietà

def test_empty_event_set_allowed_for_every_role():
    for role in ("view", "edit", "admin"):
        assert authorize_events(role, []).allowed, role


def test_view_forbids_every_event_in_every_fixture():
    """Nessuna combinazione presente nelle fixture deve passare con `view`."""
    for fx in POLICY_FIXTURES:
        if fx.get("operation") == "rollback":
            continue
        events = _events_of(fx)
        if not events:
            continue
        assert not authorize_events("view", events).allowed, fx["name"]


def test_admin_allows_every_event_set_in_every_fixture():
    for fx in POLICY_FIXTURES:
        if fx.get("operation") == "rollback":
            continue
        assert authorize_events("admin", _events_of(fx)).allowed, fx["name"]


def test_edit_allows_exactly_the_documented_device_events():
    assert EDIT_DEVICE_EVENTS == {"add", "update", "rename", "move", "delete"}
    for ev in EDIT_DEVICE_EVENTS:
        d = authorize_events("edit", [{"entity": "device", "event": ev, "scope": "devices"}])
        assert d.allowed, ev
    # reorder è escluso di proposito
    d = authorize_events("edit", [{"entity": "device", "event": "reorder", "scope": "devices"}])
    assert not d.allowed


def test_whole_change_rejected_if_any_event_forbidden():
    """Tutto o niente: la presenza di un evento vietato respinge l'insieme, per
    quanti eventi legittimi ci siano."""
    events = [{"entity": "device", "event": "update", "scope": "devices", "uid": f"d{i}"}
              for i in range(50)]
    assert authorize_events("edit", events).allowed
    events.append({"entity": "rack", "event": "delete", "scope": "structure", "uid": "r1"})
    d = authorize_events("edit", events)
    assert not d.allowed
    assert len(d.violations) == 1
    assert d.violations[0].entity == "rack"


def test_violations_are_deterministic():
    events = [{"entity": "room", "event": "update", "scope": "structure", "uid": "b"},
              {"entity": "rack", "event": "add", "scope": "structure", "uid": "a"},
              {"entity": "manual", "event": "delete", "scope": "manuale", "uid": "m"}]
    a = [v.as_dict() for v in authorize_events("edit", events).violations]
    b = [v.as_dict() for v in authorize_events("edit", list(reversed(events))).violations]
    assert a == b


def test_unknown_entity_defaults_to_admin():
    """Un tipo di entità non previsto nasce ristretto: si concede solo ciò che è
    esplicitamente permesso, così una struttura nuova non diventa scrivibile per
    distrazione."""
    ev = [{"entity": "qualcosa_di_nuovo", "event": "update", "scope": "?"}]
    assert not authorize_events("edit", ev).allowed
    assert not authorize_events("view", ev).allowed
    assert authorize_events("admin", ev).allowed


@pytest.mark.parametrize("role", ["", "Admin", "ADMIN", "superuser", None, "editor"])
def test_unknown_role_fails_closed(role):
    d = authorize_events(role, [])
    assert not d.allowed
    assert d.violations[0].code == UNKNOWN_ROLE
    r = authorize_rollback(role)
    assert not r.allowed
    assert r.violations[0].code == UNKNOWN_ROLE


def test_rollback_is_admin_only():
    assert authorize_rollback("admin").allowed
    for role in ("view", "edit"):
        d = authorize_rollback(role)
        assert not d.allowed
        assert d.violations[0].code == ROLLBACK_FORBIDDEN
        assert d.violations[0].required_role == "admin"


def test_accepts_event_objects_and_dicts_alike():
    """La politica deve funzionare sia sugli Event del motore sia su eventi
    deserializzati da JSON."""
    from tests.conftest import IDENTITY_BY_NAME
    from app.identity import diff_documents

    src = IDENTITY_BY_NAME["move-device-between-racks"]
    objs = diff_documents(src["before"], src["after"])
    dicts = diff_as_dicts(src["before"], src["after"])
    assert authorize_events("edit", objs).allowed
    assert authorize_events("edit", dicts).allowed


def test_violation_payload_is_machine_readable():
    d = authorize_events("edit", [{"entity": "rack", "event": "delete",
                                   "scope": "structure", "uid": "r1"}])
    v = d.violations[0].as_dict()
    assert set(v) >= {"code", "role", "entity", "event", "scope", "requiredRole", "uid"}
    assert v["code"] == FORBIDDEN_FOR_ROLE
    assert v["requiredRole"] == "admin"
    assert json.dumps(v)          # serializzabile senza adattatori


def test_every_violation_code_is_covered_by_a_fixture():
    codes = {v["code"] for f in POLICY_FIXTURES for v in f["expectedViolations"]}
    assert {FORBIDDEN_FOR_ROLE, ROLLBACK_FORBIDDEN, UNKNOWN_ROLE} <= codes
