"""Il foglio «Seriali rack» → correzioni (tools/apply-rack-serials.py).

Le regole di una riga sono il contratto con chi compila: vuoto = invariato,
NESSUNO = nessun seriale, altrimenti i seriali scritti. E una riga che non si
riconosce con certezza (uid alterato, codice che non combacia) ferma tutto
invece di finire sul rack sbagliato.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tsm_apply_rack_serials", ROOT / "tools" / "apply-rack-serials.py")
AP = importlib.util.module_from_spec(_spec)
sys.modules["tsm_apply_rack_serials"] = AP
try:
    _spec.loader.exec_module(AP)
except ModuleNotFoundError as exc:      # openpyxl: strumento di sviluppo, non dell'immagine
    pytest.skip(f"apply-rack-serials non importabile: {exc}", allow_module_level=True)

U1 = "a227d36e-20d5-42b9-9aea-8fed156b9118"
U2 = "eafd9d2b-130c-4ec6-aa15-1647bdd98bc1"
RACK = {U1: {"rack": "R01", "sala": "backend", "seriali": []},
        U2: {"rack": "R02", "sala": "backend", "seriali": ["OLD"]}}


def riga(uid=U1, rack="R01", s1="", s2="", n=2):
    return {"riga": n, "_uid": uid, "rack": rack, "s1": s1, "s2": s2}


def test_vuoto_non_cambia_niente():
    nuove, errori = AP.correzioni_da([riga(), riga(U2, "R02")], RACK)
    assert (nuove, errori) == ({}, [])


def test_uno_e_due_seriali():
    nuove, errori = AP.correzioni_da([riga(s1="A"), riga(U2, "R02", "B", "C", 3)], RACK)
    assert errori == []
    assert nuove[U1]["seriali"] == ["A"] and nuove[U2]["seriali"] == ["B", "C"]


def test_NESSUNO_svuota():
    nuove, errori = AP.correzioni_da([riga(U2, "R02", "nessuno")], RACK)
    assert errori == [] and nuove[U2]["seriali"] == []


@pytest.mark.parametrize("r", [
    riga(uid="", s1="A"),                       # uid cancellato
    riga(uid=U2, rack="R01", s1="A"),           # righe spostate: codice ≠ uid
    riga(uid="11111111-1111-4111-8111-111111111111", s1="A"),
    riga(s2="B"),                               # 2 senza 1
    riga(s1="A", s2="A"),                       # duplicato
    riga(s1="NESSUNO", s2="B"),
])
def test_righe_non_valide_fermano(r):
    _nuove, errori = AP.correzioni_da([r], RACK)
    assert errori


def test_lo_stesso_seriale_su_due_rack_ferma():
    _n, errori = AP.correzioni_da([riga(s1="X"), riga(U2, "R02", "X", n=3)], RACK)
    assert errori


def test_un_numero_letto_come_float_torna_intero():
    assert AP._testo(2020047937.0) == "2020047937"
    assert AP._testo("  a   b ") == "a b"
