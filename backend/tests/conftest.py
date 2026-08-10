"""Caricamento delle fixture di identità condivise con la suite JavaScript, e
il client HTTP di prova.

`fixtures/identity/*.json` è il contratto neutro rispetto al linguaggio: le
stesse fixture sono consumate da tools/identity-tests.mjs (validità e codici) e
da questa suite (validità, codici ED eventi di dominio).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ==================================================================
# client di prova: HTTPS, e non è un dettaglio estetico
# ==================================================================
#
# Il cookie di sessione è `Secure` per default (§8.29), e un cookie `Secure` NON
# viene inviato su `http://` — è la politica di `http.cookiejar`, non una scelta
# nostra. Con il valore predefinito del TestClient (`http://testserver`) succede
# questo: l'accesso riesce, `Set-Cookie` arriva, il cookie viene perfino
# memorizzato, e poi ogni richiesta successiva parte SENZA cookie. Il risultato
# sono 401 al posto dei 403 attesi, e soprattutto:
#
#   la validazione di origine (§8.27) scatta solo sulle richieste CHE PORTANO il
#   cookie. Senza cookie non veniva mai esercitata: i test che la riguardano
#   passavano per il motivo sbagliato, cioè perché il controllo non avveniva.
#
# Un test che passa senza eseguire ciò che dichiara di verificare è peggio di un
# test rosso. Parlare in `https://` risolve entrambe le cose ed è anche più
# fedele: in produzione il servizio si raggiunge solo in HTTPS (§8.31).
#
# L'alternativa — far girare la suite con `TSM_COOKIE_SECURE=false` — è stata
# scartata: renderebbe verde il test cambiando la configurazione
# dell'applicazione, e la configurazione di produzione non verrebbe più provata
# da nessuno.
BASE_URL = "https://testserver"

#: Origine da mandare sulle richieste che modificano stato. DEVE combaciare con
#: `BASE_URL`: è esattamente il confronto che fa il server (§8.27).
ORIGIN = {"Origin": BASE_URL}


def api_client(app, **kwargs) -> TestClient:
    """TestClient che parla `https://`, così il cookie `Secure` viaggia."""
    return TestClient(app, base_url=BASE_URL, **kwargs)

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
