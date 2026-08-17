"""La regola sui numeri JSON: che cosa rifiuta, che cosa NON deve rifiutare.

PURO. La regola gira nel percorso della richiesta, prima di qualunque accesso al
database, quindi deve essere pura — ma non è un'approssimazione scritta a mano:
`test_snapshot_numbers_pg.py` la confronta con PostgreSQL vero su un corpus, e se i
due dissentono su un solo valore quel test è rosso. **La regola è una previsione,
l'oracolo è il database**, e i due file vanno letti insieme.

Qui si fissano tre cose che l'oracolo non copre:

  1. la regola è TOTALE — nessun valore Python la fa sollevare;
  2. i booleani non sono numeri, anche se `bool` è sottoclasse di `int`;
  3. la ricorsione arriva davvero in ogni angolo del documento, comprese le liste
     di scalari, `extra`, la geometria delle sale e i blocchi del manuale.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.inventory.document import (
    NUMBER_NOT_ROUNDTRIPPABLE,
    validate_normal_document,
)
from app.inventory.json_numbers import (
    JSON_NUMBER_NOT_ROUNDTRIPPABLE,
    describe,
    is_number,
    is_representable,
    unrepresentable_reason,
)
# La VISITA del documento e il limite di quanti problemi si elencano sono in comune
# con la regola sul testo: stanno in `representable.py`, e una visita sola copre
# entrambe (§8.16).
from app.inventory.representable import (
    MAX_REPORTED,
    unrepresentable_items,
    walk_scalars,
)

ROOT = Path(__file__).resolve().parents[2]

#: Gli stessi valori che la sonda ha misurato contro PostgreSQL. Qui si asserisce
#: la PREVISIONE; il file su PG asserisce che la previsione è giusta.
RAPPRESENTABILI = [
    0, 1, -1, 42, -42, 2**31, 2**53, 2**63, 2**64, 10**30, -10**30, 10**100,
    0.0, 0.1, 0.4, -0.5, 10.0, -10.0, 3.141592653589793,
    0.30000000000000004, 123456789.12345679,
    1e-7, 1e-9, 2.5e-05, 1e-100, 5e-324,
    1e15, 1000000000000000.0, 1234567890123456.0,
]

NON_RAPPRESENTABILI = [
    1e16, 1e20, 1.5e300, -1e20, -0.0,
    float("inf"), float("-inf"), float("nan"),
]


# ==================================================================
# 1. la regola
# ==================================================================

@pytest.mark.parametrize("value", RAPPRESENTABILI, ids=repr)
def test_a_representable_number_is_accepted(value):
    assert is_representable(value), unrepresentable_reason(value)


@pytest.mark.parametrize("value", NON_RAPPRESENTABILI, ids=repr)
def test_an_unrepresentable_number_is_refused_with_a_reason(value):
    assert not is_representable(value)
    reason = unrepresentable_reason(value)
    assert reason and isinstance(reason, str), "un rifiuto senza motivo non è azionabile"


def test_the_zero_that_is_refused_is_only_the_negative_one():
    """⚠ La distinzione che un confronto con `==` non vede.

    `-0.0 == 0.0` è vero in Python: una verifica scritta sui valori invece che sulla
    serializzazione avrebbe dichiarato fedele proprio il caso che non lo è. È
    successo, alla prima versione della sonda.
    """
    assert -0.0 == 0.0
    assert json.dumps(-0.0) != json.dumps(0.0)
    assert is_representable(0.0)
    assert not is_representable(-0.0)
    assert is_representable(0)


def test_the_boundary_is_where_repr_switches_to_an_exponent():
    """Il confine non è una soglia scelta a mano: è il punto in cui `repr` passa
    alla notazione esponenziale, cioè in cui PostgreSQL scriverebbe un intero."""
    assert repr(1234567890123456.0) == "1234567890123456.0"
    assert is_representable(1234567890123456.0)
    assert repr(1e16) == "1e+16"
    assert not is_representable(1e16)
    # Esponente NEGATIVO: la scala resta, e il valore torna. Non va rifiutato.
    assert repr(1e-9) == "1e-09"
    assert is_representable(1e-9)


@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_not_numbers(value):
    """`isinstance(True, int)` è vero in Python. Senza questa distinzione un
    `segnaposto: false` sarebbe un numero da esaminare, con l'esito assurdo di un
    booleano rifiutato come «non rappresentabile» — e JSONB i booleani li conserva."""
    assert not is_number(value)
    assert unrepresentable_reason(value) is None
    assert is_representable(value)


@pytest.mark.parametrize("value", ["10", None, [], {}, "abc", b"x"])
def test_what_is_not_a_number_is_not_this_module_business(value):
    assert not is_number(value)
    assert unrepresentable_reason(value) is None


def test_the_rule_is_total_and_never_raises():
    """⚠ La regola gira su ogni valore di ogni documento: non può sollevare.

    Il caso che l'ha resa non totale la prima volta: `len(str(abs(v)))` su un intero
    enorme solleva `ValueError`, perché da Python 3.11 convertire in stringa un
    intero di più di 4300 cifre è limitato. La regola crollava invece di rispondere,
    e il confronto si fa con una potenza di dieci.
    """
    mostro = 10 ** 140000
    assert unrepresentable_reason(mostro) is not None      # non ci sta in `numeric`
    assert not is_representable(mostro)
    assert "cifre" in describe(mostro)                    # e si descrive senza stampare

    # Il limite di CPython c'è davvero, e `describe` non ci inciampa.
    with pytest.raises(ValueError):
        str(mostro)
    assert sys.get_int_max_str_digits() == 4300


def test_a_number_too_long_to_serialise_cannot_even_be_a_document():
    """Il limite pratico non è `numeric`, è il parser JSON: un letterale di più di
    4300 cifre non diventa nemmeno un valore Python. Il controllo sulla misura
    esiste perché la regola deve essere totale, non perché quel caso arrivi."""
    with pytest.raises(ValueError):
        json.loads("1" + "0" * 4400)


# ==================================================================
# 2. la ricorsione arriva in ogni angolo
# ==================================================================

def test_the_walk_visits_numbers_inside_lists_of_scalars():
    """⚠ Il caso che `_walk_raw` di `document.py` NON vede.

    Quella funzione produce coppie (chiave, valore) e non scende negli elementi di
    una lista di scalari: `seriali: [1e20]` le sfuggirebbe. È la ragione per cui la
    ricorsione è scritta a parte.
    """
    found = {p: v for p, kind, v in walk_scalars({"seriali": ["ok", 1e20, 3]})
             if kind == "value"}
    assert found == {"seriali[0]": "ok", "seriali[1]": 1e20, "seriali[2]": 3}


def test_the_walk_reports_a_readable_path():
    doc = {"locations": [{"sale": [{"racks": [{"x": -0.0}]}]}]}
    paths = [p for p, kind, v in walk_scalars(doc)
             if kind == "value" and isinstance(v, float)]
    assert paths == ["locations[0].sale[0].racks[0].x"]


@pytest.mark.parametrize("where,doc", [
    ("campo di entità", {"locations": [{"sale": [{"racks": [{"x": 1e20}]}]}]}),
    ("campo ignoto (finirebbe in `extra`)",
     {"locations": [{"sale": [{"racks": [{"campoNuovo": 1e20}]}]}]}),
    ("geometria della sala", {"locations": [{"sale": [{"w": 1e20}]}]}),
    ("vano", {"locations": [{"sale": [{"vani": [{"x": 0, "w": 1e20}]}]}]}),
    ("porta di un vano",
     {"locations": [{"sale": [{"vani": [{"porta": {"x": -0.0}}]}]}]}),
    ("voce di manuale", {"manuale": [{"blocchi": [{"altezza": 1e20}]}]}),
    ("lista di scalari", {"locations": [{"sale": [{"racks": [
        {"seriali": ["a", 1e20]}]}]}]}),
    ("annidamento profondo",
     {"manuale": [{"blocchi": [{"tabella": [[1, [2, {"z": -0.0}]]]}]}]}),
])
def test_an_offending_number_is_found_wherever_it_hides(where, doc):
    """Il modello delle entità è APERTO (§8.42): un valore ignoto può stare
    ovunque, e validare solo le colonne note lascerebbe fuori proprio i campi che
    non conosciamo ancora."""
    found, extra = unrepresentable_items(doc)
    assert len(found) == 1 and extra == 0, where
    assert found[0].code == JSON_NUMBER_NOT_ROUNDTRIPPABLE
    assert found[0].message, "il motivo va detto"


def test_a_clean_document_reports_nothing():
    """Controprova: se `unrepresentable_numbers` segnalasse sempre qualcosa, tutti i
    test sopra passerebbero per il motivo sbagliato."""
    doc = {"locations": [{"sale": [{"w": 8.5, "h": 6.25, "segnaposto": False,
                                    "vani": [{"x": 0, "porta": {"w": 0.84}}],
                                    "racks": [{"u": 45, "x": 0.5,
                                               "seriali": ["2006004084"]}]}]}]}
    assert unrepresentable_items(doc) == ([], 0)


def test_many_offenders_are_reported_but_not_dumped():
    """L'errore indica i campi, non ristampa il documento inviato."""
    doc = {"locations": [{"sale": [{"racks": [{"x": -0.0} for _ in range(50)]}]}]}
    found, extra = unrepresentable_items(doc)
    assert len(found) == MAX_REPORTED
    assert extra == 50 - MAX_REPORTED


# ==================================================================
# 3. il rifiuto passa dallo schema congelato
# ==================================================================

def base_document() -> dict:
    return {
        "schemaVersion": 1,
        "locations": [{
            "_uid": "aaaaaaaa-0000-4000-8000-000000000001",
            "id": "sito", "nome": "Sito",
            "sale": [{
                "_uid": "bbbbbbbb-0000-4000-8000-000000000001",
                "id": "sala", "nome": "Sala", "w": 8.5, "h": 6.25,
                "vani": [], "racks": [{
                    "_uid": "cccccccc-0000-4000-8000-000000000001",
                    "id": "R01", "name": "R01", "u": 45,
                    "x": 0.5, "y": 1.25, "w": 0.6, "h": 0.65, "devices": [],
                }],
            }],
        }],
    }


def test_a_valid_document_is_still_accepted():
    """La prima cosa da provare, prima di ogni rifiuto: non aver rotto il caso
    normale."""
    assert validate_normal_document(base_document()) == []


@pytest.mark.parametrize("value", NON_RAPPRESENTABILI, ids=repr)
def test_the_frozen_schema_refuses_the_document(value):
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["x"] = value
    errors = validate_normal_document(doc)
    codes = {e.code for e in errors}
    assert codes == {NUMBER_NOT_ROUNDTRIPPABLE}, [e.as_dict() for e in errors]
    assert NUMBER_NOT_ROUNDTRIPPABLE == JSON_NUMBER_NOT_ROUNDTRIPPABLE
    problem = errors[0].as_dict()
    assert problem["path"] == "locations[0].sale[0].racks[0].x"


def test_the_error_names_the_field_and_not_the_document():
    """Il percorso identifica il campo; il documento inviato non torna indietro.

    Il valore incriminato sta nel MESSAGGIO — dire quale numero è il problema è ciò
    che rende l'errore azionabile — e `app/api/errors.py` manda al client solo
    `code` e `path`, non il messaggio.
    """
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["y"] = 1e20
    doc["locations"][0]["sale"][0]["nome"] = "Nome che non deve comparire"
    errors = validate_normal_document(doc)
    assert len(errors) == 1
    testo = json.dumps(errors[0].as_dict(), ensure_ascii=False)
    assert "1e+20" in testo
    assert "Nome che non deve comparire" not in testo
    assert "R01" not in testo


@pytest.mark.parametrize("value", [0, 0.0, False, "", 45, -3, 8.5, 0.30000000000000004])
def test_falsy_and_ordinary_values_still_pass(value):
    """Non correggere troppo: gli zeri espliciti, i `False` e i decimali normali
    sono valori dell'utente e devono continuare a funzionare (§8.14)."""
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["x"] = value
    assert validate_normal_document(doc) == []


def test_the_production_seed_has_no_unrepresentable_numbers():
    """Il documento vero: se il rifiuto lo colpisse, questo commit avrebbe reso
    non salvabile l'inventario del cliente."""
    from app.inventory.document import strip_legacy_fields
    with (ROOT / "fixtures" / "seed.json").open(encoding="utf-8") as fh:
        seed = strip_legacy_fields(json.load(fh))[0]
    assert unrepresentable_items(seed) == ([], 0)
    assert validate_normal_document(seed) == []


def test_the_relational_mapper_uses_the_same_rule():
    """⚠ Una regola sola.

    La domanda «questo numero sopravvive a un giro attraverso `numeric`?» è la stessa
    per una colonna `numeric` e per JSONB, perché JSONB i numeri li tiene in
    `numeric`. Due implementazioni divergerebbero sui casi limite, cioè proprio dove
    la regola serve. Quello che cambia fra i due usi è la CONSEGUENZA: nella
    proiezione il valore va in `extra`, nell'istantanea il documento si rifiuta.
    """
    from app.inventory.relational import _is_num
    for value in RAPPRESENTABILI:
        assert _is_num(value) is True, value
    for value in NON_RAPPRESENTABILI:
        assert _is_num(value) is False, value
    for value in (True, False, "10", None):
        assert _is_num(value) is False, value
