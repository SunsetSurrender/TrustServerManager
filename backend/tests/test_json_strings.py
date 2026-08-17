"""La regola sul TESTO: che cosa rifiuta, che cosa non deve rifiutare, e le chiavi.

PURO. Come per i numeri, la regola è una PREVISIONE e l'oracolo è il database:
`test_snapshot_strings_pg.py` la confronta con PostgreSQL su un corpus di stringhe
provate sia come valore sia come chiave, e se i due dissentono su una sola quel file è
rosso. I due vanno letti insieme.

⚠ Ogni carattere invisibile è scritto con una SEQUENZA DI ESCAPE (`"a\\u0000b"`), mai
digitato. Nella sonda che ha misurato questo comportamento la prima versione del corpus
li aveva digitati direttamente e non erano sopravvissuti alla scrittura del file:
conteneva `"ab"` dove doveva esserci un carattere di controllo, cioè non provava
niente e sembrava verde.

Qui si fissano le cose che l'oracolo non copre:

  1. la regola è TOTALE e non modifica il testo — non normalizza, non ripulisce;
  2. le CHIAVI sono dati dell'utente come i valori;
  3. il valore rifiutato non compare mai nell'errore, e nemmeno nel percorso;
  4. la visita arriva in ogni angolo del documento.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from app.inventory.document import (
    STRING_NOT_ROUNDTRIPPABLE,
    strip_legacy_fields,
    validate_normal_document,
)
from app.inventory.json_strings import (
    JSON_STRING_NOT_ROUNDTRIPPABLE,
    NUL,
    is_representable_text,
    is_text,
    unrepresentable_text_reason,
)
from app.inventory.representable import (
    MAX_REPORTED,
    key_segment,
    unrepresentable_items,
    walk_scalars,
)

ROOT = Path(__file__).resolve().parents[2]

#: Il corpus misurato contro PostgreSQL. Sopravvive tutto tranne NUL e i surrogati
#: spaiati — compresi i caratteri di controllo, i noncaratteri e il piano 16.
RAPPRESENTABILI = [
    ("ascii", "R01-SALA"),
    ("italiano accentato", "Località è già più però"),
    ("maiuscole accentate", "ÀÈÌÒÙ ÇÑ"),
    ("simboli", "53.13 m² × 8.50 — «virgolette»"),
    ("greco/cirillico/CJK", "Ωμέγα При 日本語"),
    ("arabo RTL", "العربية"),
    ("emoji BMP", "☎ ✓ ⚠"),
    ("emoji non-BMP", "\U0001f680 \U0001f4be"),
    ("emoji con selettore", "❤️"),
    ("combinante NFD", "é à"),
    ("precomposto NFC", "é à"),
    ("newline", "prima\nseconda"),
    ("ritorno a capo", "prima\rseconda"),
    ("CRLF", "prima\r\nseconda"),
    ("tab", "col1\tcol2"),
    ("controllo U+0001", "a\u0001b"),
    ("controllo U+001F", "a\u001fb"),
    ("DEL U+007F", "a\u007fb"),
    ("separatore di riga U+2028", "a\u2028b"),
    ("BOM U+FEFF", "\ufeffprima"),
    ("noncarattere U+FFFE", "a\ufffeb"),
    ("noncarattere U+FFFF", "a\uffffb"),
    ("noncarattere U+FDD0", "a\ufdd0b"),
    ("coppia surrogata valida", "\U0001f600"),
    ("piano 16", "\U0010fffd"),
    ("stringa vuota", ""),
    ("solo spazi", "   "),
    ("backslash e virgolette", "a\\b\"c"),
]

NON_RAPPRESENTABILI = [
    ("NUL in mezzo", "a\u0000b"),
    ("NUL da solo", "\u0000"),
    ("NUL in coda", "ab\u0000"),
    ("NUL in testa", "\u0000ab"),
    ("surrogato alto spaiato", "a\ud800b"),
    ("surrogato basso spaiato", "a\udc00b"),
    ("surrogato alto da solo", "\udbff"),
    ("surrogato in coda a testo valido", "Sala 1\udfff"),
]


# ==================================================================
# 1. la regola
# ==================================================================

@pytest.mark.parametrize("etichetta,value", RAPPRESENTABILI,
                         ids=[e for e, _v in RAPPRESENTABILI])
def test_a_representable_string_is_accepted(etichetta, value):
    assert is_representable_text(value), unrepresentable_text_reason(value)


@pytest.mark.parametrize("etichetta,value", NON_RAPPRESENTABILI,
                         ids=[e for e, _v in NON_RAPPRESENTABILI])
def test_an_unrepresentable_string_is_refused_with_a_reason(etichetta, value):
    assert not is_representable_text(value)
    reason = unrepresentable_text_reason(value)
    assert reason and isinstance(reason, str)
    # ⚠ Il motivo dice dove e quanto, MAI cosa: è un errore, non un'eco.
    assert value not in reason
    assert "posizione" in reason and "caratteri" in reason


def test_the_two_families_are_refused_for_different_reasons():
    """Il NUL lo rifiuta il DATABASE, il surrogato spaiato la CODIFICA prima di
    arrivarci. Per chi salva è la stessa cosa, ma il motivo va detto giusto."""
    assert "NUL" in unrepresentable_text_reason("a\u0000b")
    assert "surrogato" in unrepresentable_text_reason("a\ud800b")


def test_the_rule_is_total_and_never_raises():
    """Gira su ogni stringa di ogni documento: non può sollevare. Nemmeno su un
    surrogato, che è il valore su cui `encode` esplode."""
    for value in ("a\ud800b", "\u0000", "", "x" * 100000, "\U0010ffff"):
        assert isinstance(is_representable_text(value), bool)
    for value in (None, 42, 3.5, True, [], {}, b"byte", object()):
        assert unrepresentable_text_reason(value) is None
        assert not is_text(value)


def test_the_rule_does_not_touch_the_text():
    """⚠ Non normalizza, non ripulisce, non sostituisce.

    Ripulire vorrebbe dire salvare un documento diverso da quello inviato (§8.16), e
    per il testo sarebbe peggio che per i numeri: cambiare un nome è una modifica che
    l'utente vede nel registro attribuita a sé.
    """
    nfd = "é"
    nfc = "é"
    assert nfd != nfc and unicodedata.normalize("NFC", nfd) == nfc
    # Entrambe passano, ED È GIUSTO: sono due stringhe diverse e PostgreSQL le
    # conserva entrambe come sono. Una validazione che normalizzasse le renderebbe
    # uguali, cioè cambierebbe il documento.
    assert is_representable_text(nfd) and is_representable_text(nfc)

    doc = {"schemaVersion": 1, "locations": [], "manuale": [{"titolo": nfd}]}
    prima = json.dumps(doc, ensure_ascii=True, sort_keys=True)
    validate_normal_document(doc)
    assert json.dumps(doc, ensure_ascii=True, sort_keys=True) == prima


def test_control_characters_and_noncharacters_are_not_refused():
    """PostgreSQL li conserva, quindi non sono affari nostri.

    È il lato «non correggere troppo» della regola: un tab in una nota o un BOM
    incollato da Excel sono testo dell'utente, non un errore.
    """
    for _etichetta, value in RAPPRESENTABILI:
        assert is_representable_text(value)
    assert is_representable_text("a\u0001\u001f\u007f\u2028\ufeff\uffffb")


# ==================================================================
# 2. le chiavi sono dati dell'utente
# ==================================================================

def test_a_broken_key_does_not_slip_through():
    """⚠ Il caso dell'enunciato: campi ignoti e chiavi ignote sopravvivono al
    salvataggio (finiranno in `extra`, §8.42), quindi una chiave rotta fa fallire
    l'inserimento esattamente come un valore rotto."""
    doc = {"_uid": "x", "normalField": "va bene", "bad\u0000key": "anche questo va bene"}
    found, remaining = unrepresentable_items(doc)
    assert remaining == 0
    assert len(found) == 1
    assert found[0].code == JSON_STRING_NOT_ROUNDTRIPPABLE
    assert found[0].is_key


def test_a_broken_key_is_located_without_being_reproduced():
    """Percorso del genitore + posizione della chiave, MAI la chiave."""
    doc = {"locations": [{"sale": [{"nome": "Sala", "ch\u0000ave": 1}]}]}
    found, _r = unrepresentable_items(doc)
    assert len(found) == 1
    problema = found[0]
    assert problema.is_key
    assert problema.path == f"locations[0].sale[0].{key_segment(1)}"
    assert "chiave" in problema.message
    assert "\u0000" not in problema.path and "\u0000" not in problema.message
    assert "ch" not in problema.path.replace("chiave", "")


def test_a_value_under_a_broken_key_does_not_leak_the_key_either():
    """Il percorso di un valore innocente non deve finire per riprodurre la chiave
    che non si può scrivere."""
    doc = {"racks": [{"b\u0000d": {"x": 1e20}}]}
    found, _r = unrepresentable_items(doc)
    percorsi = [f.path for f in found]
    assert all("\u0000" not in p for p in percorsi)
    assert f"racks[0].{key_segment(0)}.x" in percorsi


def test_the_walk_yields_keys_and_values():
    doc = {"a": "uno", "b": ["due", {"c": "tre"}]}
    visti = [(p, kind, v) for p, kind, v in walk_scalars(doc)]
    assert ("a", "key", "a") in visti
    assert ("a", "value", "uno") in visti
    assert ("b[0]", "value", "due") in visti
    assert ("b[1].c", "key", "c") in visti
    assert ("b[1].c", "value", "tre") in visti


# ==================================================================
# 3. copertura di tutto il documento
# ==================================================================

def base_document() -> dict:
    """Documento valido con testo italiano realistico, non ASCII."""
    return {
        "schemaVersion": 1,
        "locations": [{
            "_uid": "aaaaaaaa-0000-4000-8000-000000000001",
            "id": "pomezia", "nome": "Pomezia G0 — Città",
            "sale": [{
                "_uid": "bbbbbbbb-0000-4000-8000-000000000001",
                "id": "sala-1", "nome": "Sala 1 (già CED)",
                "w": 8.5, "h": 6.25, "area": "53.13 m²",
                "vani": [{"x": 0, "y": 0, "w": 4.25, "h": 6.25,
                          "porta": {"lato": "bottom", "x": 0.35, "w": 0.84},
                          "porta2": {"lato": "top", "x": 2.0, "w": 1.1}}],
                "racks": [{
                    "_uid": "cccccccc-0000-4000-8000-000000000001",
                    "id": "R01", "name": "Rack perimetrale", "u": 45,
                    "x": 0.5, "y": 1.25, "w": 0.6, "h": 0.65,
                    "seriali": ["2006004084", "SN-À-01"],
                    "devices": [{
                        "_uid": "dddddddd-0000-4000-8000-000000000001",
                        "id": "srv-01", "name": "srv-01 «produzione»",
                        "note": "Verificare l'alimentazione — cavo già sostituito",
                    }],
                }],
            }],
        }],
        "manuale": [{
            "_uid": "eeeeeeee-0000-4000-8000-000000000001",
            "id": "procedura", "titolo": "Procedura di spegnimento",
            "blocchi": [{"testo": "Sequenza: UPS → rack → climatizzazione"}],
        }],
    }


def test_a_realistic_italian_document_is_accepted():
    """La prima cosa da provare: non aver rotto il caso normale, che non è ASCII."""
    assert validate_normal_document(base_document()) == []


#: Ogni posto in cui una stringa può nascondersi. Il modello è APERTO: i campi ignoti
#: e le chiavi ignote sopravvivono, quindi vanno coperti come quelli noti.
POSIZIONI = [
    ("nome del sito", lambda d, s: d["locations"][0].update({"nome": s})),
    ("chiave ignota del sito", lambda d, s: d["locations"][0].update({s: "x"})),
    ("nome della sala",
     lambda d, s: d["locations"][0]["sale"][0].update({"nome": s})),
    ("geometria: `area` della sala",
     lambda d, s: d["locations"][0]["sale"][0].update({"area": s})),
    ("vano, campo ignoto",
     lambda d, s: d["locations"][0]["sale"][0]["vani"][0].update({"etichetta": s})),
    ("porta di un vano",
     lambda d, s: d["locations"][0]["sale"][0]["vani"][0]["porta"].update({"lato": s})),
    ("porta2 di un vano",
     lambda d, s: d["locations"][0]["sale"][0]["vani"][0]["porta2"].update({"lato": s})),
    ("chiave del vano",
     lambda d, s: d["locations"][0]["sale"][0]["vani"][0].update({s: 1})),
    ("nome del rack",
     lambda d, s: d["locations"][0]["sale"][0]["racks"][0].update({"name": s})),
    ("campo ignoto del rack",
     lambda d, s: d["locations"][0]["sale"][0]["racks"][0].update({"reparto": s})),
    ("dentro `seriali` (lista di scalari)",
     lambda d, s: d["locations"][0]["sale"][0]["racks"][0]["seriali"].append(s)),
    ("nota di un dispositivo",
     lambda d, s: d["locations"][0]["sale"][0]["racks"][0]["devices"][0]
     .update({"note": s})),
    ("chiave di un dispositivo",
     lambda d, s: d["locations"][0]["sale"][0]["racks"][0]["devices"][0]
     .update({s: "x"})),
    ("titolo di una voce di manuale", lambda d, s: d["manuale"][0].update({"titolo": s})),
    ("testo di un blocco",
     lambda d, s: d["manuale"][0]["blocchi"][0].update({"testo": s})),
    ("struttura arbitraria annidata in un blocco",
     lambda d, s: d["manuale"][0]["blocchi"][0].update({"tabella": [["a", [s]]]})),
]


@pytest.mark.parametrize("where,mutate", POSIZIONI, ids=[w for w, _m in POSIZIONI])
@pytest.mark.parametrize("bad", ["a\u0000b", "a\ud800b"], ids=["NUL", "surrogato"])
def test_an_offending_string_is_found_wherever_it_hides(where, mutate, bad):
    doc = base_document()
    mutate(doc, bad)
    errors = validate_normal_document(doc)
    codes = {e.code for e in errors}
    assert STRING_NOT_ROUNDTRIPPABLE in codes, (where, [e.as_dict() for e in errors])


@pytest.mark.parametrize("where,mutate", POSIZIONI, ids=[w for w, _m in POSIZIONI])
def test_the_same_places_accept_ordinary_italian_text(where, mutate):
    """Controprova di ogni caso sopra: se la mutazione producesse un documento
    invalido di suo, il test precedente passerebbe per il motivo sbagliato."""
    doc = base_document()
    mutate(doc, "Località già verificata — n° 3")
    assert validate_normal_document(doc) == [], where


def test_the_root_is_covered_too_even_though_no_unknown_root_key_is_legal():
    """⚠ La radice sta fuori da `POSIZIONI`, e la ragione l'ha trovata la controprova.

    Alla radice lo schema congelato ammette solo tre chiavi (§8.16): qualunque campo
    ignoto è già rifiutato come `unknown_root_key`, con o senza caratteri rotti. Il
    controllo positivo su quella posizione falliva sempre — non per il testo, per la
    chiave — e teneva insieme due affermazioni diverse.

    La rappresentabilità della radice si prova quindi così: il codice sul testo
    compare **insieme** a quello sulla chiave, non al suo posto.
    """
    from app.inventory.document import UNKNOWN_ROOT_KEY
    doc = base_document()
    doc["noteGlobali"] = "a\u0000b"
    codes = {e.code for e in validate_normal_document(doc)}
    assert codes == {UNKNOWN_ROOT_KEY, STRING_NOT_ROUNDTRIPPABLE}

    # E una CHIAVE di radice rotta: due errori, e nessuno dei due riproduce la chiave.
    doc = base_document()
    doc["ch\u0000ave"] = "x"
    errors = validate_normal_document(doc)
    assert {e.code for e in errors} == {UNKNOWN_ROOT_KEY, STRING_NOT_ROUNDTRIPPABLE}
    nostro = [e for e in errors if e.code == STRING_NOT_ROUNDTRIPPABLE][0]
    assert nostro.path == key_segment(3), "radice: quarta chiave del documento"
    assert "\u0000" not in json.dumps(nostro.as_dict(), ensure_ascii=False)


def test_the_error_never_echoes_the_string():
    """Requisito esplicito: il percorso identifica il campo, il valore non torna."""
    segreto = "NOME-CHE-NON-DEVE-COMPARIRE\u0000x"
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["name"] = segreto
    errors = validate_normal_document(doc)
    assert [e.code for e in errors] == [STRING_NOT_ROUNDTRIPPABLE]
    testo = json.dumps([e.as_dict() for e in errors], ensure_ascii=False)
    assert "NOME-CHE-NON-DEVE-COMPARIRE" not in testo
    assert "\u0000" not in testo
    assert errors[0].path == "locations[0].sale[0].racks[0].name"


def test_a_surrogate_is_not_reported_as_a_non_object():
    """⚠ L'ordine dei controlli, provato.

    Il calcolo della dimensione serializza in UTF-8, e un surrogato spaiato non è
    codificabile: quel `try` cattura l'`UnicodeEncodeError` (che è una `ValueError`) e
    direbbe `not_an_object` — «il documento non è un oggetto» per un documento che è un
    oggetto. La rappresentabilità si controlla PRIMA, e il motivo vero sopravvive.
    """
    doc = base_document()
    doc["locations"][0]["nome"] = "Pomezia\ud800"
    codes = [e.code for e in validate_normal_document(doc)]
    assert STRING_NOT_ROUNDTRIPPABLE in codes
    assert codes[0] == STRING_NOT_ROUNDTRIPPABLE, codes


def test_many_offenders_are_reported_but_not_dumped():
    doc = base_document()
    doc["manuale"][0]["blocchi"] = [{"testo": f"x{NUL}"} for _ in range(50)]
    found, remaining = unrepresentable_items(doc)
    assert len(found) == MAX_REPORTED and remaining == 50 - MAX_REPORTED
    errors = validate_normal_document(doc)
    assert len({e.code for e in errors}) == 1
    assert len(errors) == MAX_REPORTED + 1          # + il riepilogo


def test_numbers_and_strings_are_reported_together():
    """Una visita sola, due regole: un documento con entrambi i problemi li elenca
    entrambi invece di fermarsi alla prima famiglia."""
    from app.inventory.document import NUMBER_NOT_ROUNDTRIPPABLE
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["x"] = -0.0
    doc["locations"][0]["sale"][0]["racks"][0]["name"] = "a\u0000b"
    codes = {e.code for e in validate_normal_document(doc)}
    assert codes == {NUMBER_NOT_ROUNDTRIPPABLE, STRING_NOT_ROUNDTRIPPABLE}


def test_the_production_seed_has_no_unrepresentable_text():
    """Il documento vero, pieno di accenti: se il rifiuto lo colpisse, questo commit
    avrebbe reso non salvabile l'inventario del cliente."""
    with (ROOT / "fixtures" / "seed.json").open(encoding="utf-8") as fh:
        seed = strip_legacy_fields(json.load(fh))[0]
    assert unrepresentable_items(seed) == ([], 0)
    assert validate_normal_document(seed) == []


# ==================================================================
# 4. una regola sola, condivisa con la mappa relazionale
# ==================================================================

def test_the_relational_mapper_uses_the_same_capability():
    """⚠ «PostgreSQL conserva questa stringa?» ha una risposta sola.

    La mappa non può avere un'idea propria di stringa rappresentabile: divergerebbe
    sui casi limite, cioè proprio dove la regola serve. Quello che cambia è la
    conseguenza — e per il testo è più stretta che per i numeri, perché una stringa
    rifiutata non entra nemmeno in `extra`.
    """
    from app.inventory.relational import _is_str
    for _etichetta, value in RAPPRESENTABILI:
        assert _is_str(value) is True, value
    for _etichetta, value in NON_RAPPRESENTABILI:
        assert _is_str(value) is False
    for value in (None, 42, True, []):
        assert _is_str(value) is False


def test_the_model_of_a_document_with_broken_text_is_an_error_not_a_warning():
    """Una stringa che PostgreSQL rifiuta non entra da nessuna parte: né in colonna,
    né in `extra`. Chiamarla `carried_verbatim` — «integra, ma non interrogabile» —
    sarebbe falso, quindi è un ERRORE."""
    from app.inventory.relational import normalise
    from app.inventory.relational_validate import (TEXT_NOT_REPRESENTABLE, codes,
                                                   errors, validate_model)
    doc = base_document()
    doc["locations"][0]["sale"][0]["racks"][0]["name"] = "a\u0000b"
    trovati = validate_model(normalise(doc))
    assert TEXT_NOT_REPRESENTABLE in codes(errors(trovati))
    # E il documento torna comunque identico: la MAPPA resta totale e lossless.
    from app.inventory.relational import round_trip
    from app.inventory import canonical_sha256
    assert canonical_sha256(round_trip(doc)) == canonical_sha256(doc)
