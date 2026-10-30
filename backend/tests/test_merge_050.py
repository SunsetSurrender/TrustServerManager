"""0.5.0: unione a tre vie dei salvataggi concorrenti. Senza database."""
from __future__ import annotations

import json

import pytest

from app.identity.canonical import canonicalise
from app.inventory.merge import MergeConflict, merge, touched_racks

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RA = "cccccccc-0000-4000-8000-00000000000a"
RB = "cccccccc-0000-4000-8000-00000000000b"
DA = "dddddddd-0000-4000-8000-00000000000a"
DB = "dddddddd-0000-4000-8000-00000000000b"
DN = "dddddddd-0000-4000-8000-00000000000e"
DM = "dddddddd-0000-4000-8000-00000000000f"


def base():
    return canonicalise({
        "schemaVersion": 1,
        "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
            {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [], "racks": [
                {"_uid": RA, "id": "R01", "name": "R01", "u": 45, "x": 0.5, "y": 0.5,
                 "w": 0.6, "h": 0.8, "devices": [
                     {"_uid": DA, "id": "srv-01", "name": "srv-01", "u": 10},
                     {"_uid": DB, "id": "srv-02", "name": "srv-02", "u": 20}]},
                {"_uid": RB, "id": "R02", "name": "R02", "u": 45, "x": 1.5, "y": 0.5,
                 "w": 0.6, "h": 0.8, "devices": []}]}]}],
    })


def c(d):
    return json.loads(json.dumps(d))


def rack(doc, uid):
    return next(k for k in doc["locations"][0]["sale"][0]["racks"] if k["_uid"] == uid)


def test_different_racks_merge():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RB)["devices"].append({"_uid": DN, "id": "fw-01", "name": "fw-01", "u": 40})
    rack(theirs, RA)["devices"][0]["model"] = "DL380"
    m = merge(b, canonicalise(mine), canonicalise(theirs))
    assert rack(m, RA)["devices"][0]["model"] == "DL380"
    assert [d["id"] for d in rack(m, RB)["devices"]] == ["fw-01"]


def test_same_device_different_values_conflicts():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RA)["devices"][0]["model"] = "A"
    rack(theirs, RA)["devices"][0]["model"] = "B"
    with pytest.raises(MergeConflict):
        merge(b, mine, theirs)


def test_same_change_on_both_sides_is_not_a_conflict():
    b = base()
    mine, theirs = c(b), c(b)
    for d in (mine, theirs):
        rack(d, RA)["devices"][0]["model"] = "A"
    rack(mine, RB)["x"] = 3
    m = merge(b, mine, theirs)
    assert rack(m, RA)["devices"][0]["model"] == "A" and rack(m, RB)["x"] == 3


def test_adding_devices_to_the_same_rack_conflicts():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RB)["devices"].append({"_uid": DN, "id": "a", "name": "a", "u": 1})
    rack(theirs, RB)["devices"].append({"_uid": DM, "id": "b", "name": "b", "u": 5})
    with pytest.raises(MergeConflict):
        merge(b, mine, theirs)


def test_editing_a_device_in_a_rack_the_other_deleted_conflicts():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RA)["devices"][1]["model"] = "X"
    theirs["locations"][0]["sale"][0]["racks"] = [rack(theirs, RB)]
    with pytest.raises(MergeConflict):
        merge(b, mine, theirs)


def test_adding_to_an_empty_rack_the_other_deleted_conflicts():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RB)["devices"].append({"_uid": DN, "id": "a", "name": "a", "u": 1})
    theirs["locations"][0]["sale"][0]["racks"] = [rack(theirs, RA)]
    with pytest.raises(MergeConflict):
        merge(b, mine, theirs)


def test_moving_rack_and_editing_its_neighbour_merge():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RA)["x"] = 2.5
    rack(theirs, RB)["name"] = "R02-nuovo"
    m = merge(b, mine, theirs)
    assert rack(m, RA)["x"] == 2.5 and rack(m, RB)["name"] == "R02-nuovo"


def test_order_is_kept():
    b = base()
    mine, theirs = c(b), c(b)
    rack(mine, RA)["devices"].reverse()
    rack(theirs, RB)["u"] = 42
    m = merge(b, mine, theirs)
    assert [d["_uid"] for d in rack(m, RA)["devices"]] == [DB, DA]


def test_touched_racks():
    b = base()
    n = c(b)
    rack(n, RA)["devices"][0]["model"] = "X"
    assert touched_racks(b, n) == {RA}
    n = c(b)
    dev = rack(n, RA)["devices"].pop()
    rack(n, RB)["devices"].append(dev)
    assert touched_racks(b, n) == {RA, RB}
    n = c(b)
    n["locations"][0]["nome"] = "Altro"
    assert touched_racks(b, n) == set()
