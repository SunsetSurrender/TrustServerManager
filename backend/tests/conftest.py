"""Caricamento delle fixture di identità condivise con la suite JavaScript.

`fixtures/identity/*.json` è il contratto neutro rispetto al linguaggio: le
stesse fixture sono consumate da tools/identity-tests.mjs (validità e codici) e
da questa suite (validità, codici ED eventi di dominio).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FIXTURE_DIR = FIXTURES / "identity"
POLICY_DIR = FIXTURES / "policy"


def _load(directory: Path, generator: str) -> list[dict]:
    if not directory.is_dir():
        raise RuntimeError(f"fixture non trovate in {directory}. Generarle con `node {generator}`.")
    out = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        data["_file"] = path.name
        out.append(data)
    if not out:
        raise RuntimeError(f"nessuna fixture in {directory}")
    return out


def load_fixtures() -> list[dict]:
    return _load(FIXTURE_DIR, "tools/make-identity-fixtures.mjs")


ALL_FIXTURES = load_fixtures()
IDENTITY_BY_NAME = {f["name"]: f for f in ALL_FIXTURES}
POLICY_FIXTURES = _load(POLICY_DIR, "tools/make-policy-fixtures.mjs")
VALID_FIXTURES = [f for f in ALL_FIXTURES if f["expectedValid"]]
INVALID_FIXTURES = [f for f in ALL_FIXTURES if not f["expectedValid"]]
EVENT_FIXTURES = [f for f in ALL_FIXTURES if f.get("expectedEvents") is not None]


def _ids(fixtures):
    return [f["name"] for f in fixtures]


@pytest.fixture(params=ALL_FIXTURES, ids=_ids(ALL_FIXTURES))
def fixture_any(request):
    return request.param


@pytest.fixture(params=VALID_FIXTURES, ids=_ids(VALID_FIXTURES))
def fixture_valid(request):
    return request.param


@pytest.fixture(params=INVALID_FIXTURES, ids=_ids(INVALID_FIXTURES))
def fixture_invalid(request):
    return request.param


@pytest.fixture(params=EVENT_FIXTURES, ids=_ids(EVENT_FIXTURES))
def fixture_with_events(request):
    return request.param
