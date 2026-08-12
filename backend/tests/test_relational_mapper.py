"""La mappa relazionale: giro completo senza perdite, e cosa non torna.

PURO: nessun database. Ciò che questa suite deve dimostrare è che la mappa non
perde niente, e un database non c'entra — anzi, provarla contro un database
significherebbe provarla insieme a un database, cioè non sapere quale dei due ha
sbagliato quando il test è rosso.

L'invariante è uno:

    canonicalise(assemble(normalise(doc))) == canonicalise(doc)

e da esso discendono il digest uguale e zero eventi di dominio. Le tre asserzioni
non sono ridondanti: la prima confronta strutture, la seconda la serializzazione
canonica (l'ordine delle chiavi e la forma dei numeri), la terza il significato
secondo il motore di diff — che è ciò che l'utente vedrebbe nel registro.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    """Carica un generatore di fixture per PERCORSO, con un nome di modulo proprio.

    ⚠ I generatori si chiamano tutti `build.py` — `fixtures/expiry/build.py`,
    `fixtures/relational/build.py` — e con `sys.path.insert` + `import build`
    vincerebbe quello inserito per ultimo, oppure quello che un ALTRO file di test
    ha già importato: `sys.modules` è condiviso da tutta la sessione di pytest.
    Il risultato non è un errore ma una fixture sbagliata, cioè un test che passa
    provando qualcos'altro.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational", "fixtures/relational/build.py")
build_inventory = _load("tsm_fixture_expiry",
                        "fixtures/expiry/build.py").build_inventory

from app.identity import canonicalise, diff_as_dicts         # noqa: E402
from app.inventory import canonical_sha256                   # noqa: E402
from app.inventory.document import strip_legacy_fields       # noqa: E402
from app.inventory.relational import (                       # noqa: E402
    FIELD_MAP,
    DeviceRow,
    LocationRow,
    ManualRow,
    RackRow,
    RelationalModel,
    RoomRow,
    assemble,
    normalise,
    round_trip,
)
from app.inventory.relational_validate import (              # noqa: E402
    CARRIED_VERBATIM,
    DUPLICATE_DEVICE_CODE,
    DUPLICATE_ORDINAL,
    DUPLICATE_SCOPED_CODE,
    DUPLICATE_UID,
    ERROR,
    EXTRA_SHADOWS_COLUMN,
    INVALID_DATE,
    INVALID_ENUM,
    INVALID_ORDINAL,
    MALFORMED_ROW,
    MALFORMED_UID,
    MISSING_SCHEMA_VERSION,
    MISSING_UID,
    NON_CONTIGUOUS_ORDINAL,
    PHOTO_NOT_FOUND,
    UNKNOWN_PARENT,
    WARNING,
    codes,
    errors,
    validate_model,
    warnings,
)

# ==================================================================
# i documenti sotto esame
# ==================================================================

FIXTURES = relbuild.documents()


def _seed() -> dict:
    with (ROOT / "fixtures" / "seed.json").open(encoding="utf-8") as fh:
        return json.load(fh)


#: Il seed di PRODUZIONE in due forme:
#:
#:  - `seed-legacy`  come sta nel repository, con le radici estratte (`utenti`,
#:    `versione`) che solo la migrazione può consumare (§8.16). Non è un documento
#:    che il salvataggio normale accetterebbe, ed è qui di proposito: la mappa deve
#:    essere TOTALE, e un documento che non capisce deve attraversarla intatto;
#:  - `seed`  come sta davvero nel database dopo il bootstrap `--from-legacy`.
FIXTURES["seed-legacy"] = _seed()
FIXTURES["seed"] = strip_legacy_fields(_seed())[0]

#: L'inventario delle scadenze, con date relative: contiene nomi ostili, date
#: rotte, campi assenti e due dispositivi con lo stesso identificativo.
FIXTURES["expiry"] = build_inventory(date(2026, 8, 10))

NAMES = sorted(FIXTURES)


@pytest.fixture(params=NAMES, ids=NAMES)
def document(request) -> dict:
    return FIXTURES[request.param]


# ==================================================================
# 1. l'invariante
# ==================================================================

def test_the_round_trip_preserves_the_canonical_document(document):
    assert canonicalise(round_trip(document)) == canonicalise(document)


def test_the_round_trip_preserves_the_repository_digest(document):
    """Il digest è ciò che decide se una richiesta è già stata soddisfatta (§8.18).
    Se cambiasse per un giro nella mappa, il primo salvataggio dopo la fase 2D
    sembrerebbe una modifica reale a un documento identico."""
    assert canonical_sha256(round_trip(document)) == canonical_sha256(document)


def test_the_round_trip_produces_no_domain_events(document):
    """Zero eventi: è la versione della stessa affermazione nel linguaggio che
    l'utente vede nel registro. Un `reorder` o un `update` qui sarebbe una riga di
    audit per una modifica che nessuno ha fatto (§8.9)."""
    assert diff_as_dicts(canonicalise(document),
                         canonicalise(round_trip(document))) == []


def test_the_round_trip_is_idempotent(document):
    once = round_trip(document)
    assert round_trip(once) == once


def test_the_model_of_the_round_trip_equals_the_model_of_the_original(document):
    """Non solo il documento: anche il MODELLO deve tornare identico. Se il giro
    perdesse un valore in una colonna e lo ritrovasse in `extra`, i due documenti
    resterebbero uguali e le tabelle no — cioè il difetto sarebbe invisibile
    esattamente dove conta, che è la parte interrogabile."""
    assert normalise(round_trip(document)) == normalise(document)


def test_the_assembled_document_is_byte_stable(document):
    """L'ordine delle chiavi è deterministico. Non serve all'uguaglianza fra
    dizionari, serve a rendere confrontabili due dump e leggibile un `diff` fatto a
    mano durante la migrazione."""
    first = json.dumps(round_trip(document), ensure_ascii=False, sort_keys=False)
    second = json.dumps(round_trip(document), ensure_ascii=False, sort_keys=False)
    assert first == second


def test_the_invariant_is_capable_of_failing():
    """⚠ Controprova del metodo, non del codice.

    Una mappa che buttasse `extra` deve far CADERE l'invariante. Senza questo test,
    un invariante scritto male — che confronta la cosa sbagliata, o che confronta
    due volte lo stesso oggetto — sarebbe indistinguibile da un invariante
    soddisfatto, e tutta la suite sopra passerebbe senza dimostrare niente.
    """
    from dataclasses import replace

    doc = FIXTURES["unknown-fields"]
    model = normalise(doc)
    spogliato = RelationalModel(
        schema_version=model.schema_version,
        has_manual=model.has_manual,
        locations=tuple(replace(r, extra={}) for r in model.locations),
        rooms=tuple(replace(r, extra={}) for r in model.rooms),
        racks=tuple(replace(r, extra={}) for r in model.racks),
        devices=tuple(replace(r, extra={}) for r in model.devices),
        manual=tuple(replace(r, extra={}) for r in model.manual),
        root_extra={},
    )
    assert canonicalise(assemble(spogliato)) != canonicalise(doc)
    assert canonical_sha256(assemble(spogliato)) != canonical_sha256(doc)
    assert diff_as_dicts(canonicalise(doc),
                         canonicalise(assemble(spogliato))) != []


def test_no_errors_on_any_fixture(document):
    """Ogni fixture è un documento LEGITTIMO: può avere avvisi (date scritte a
    mano, tipi fuori vocabolario, valori non tipizzabili) ma nessun errore."""
    found = validate_model(normalise(document))
    assert errors(found) == [], [f.as_dict() for f in errors(found)]


# ==================================================================
# 2. ciò che la mappa deve conservare, campo per campo
# ==================================================================

def test_the_hierarchy_is_reproduced_exactly():
    model = normalise(FIXTURES["base"])
    counts = model.counts()
    assert counts == {"locations": 2, "rooms": 3, "racks": 4, "devices": 5,
                      "manual": 2}
    # Ogni figlio conosce il proprio genitore, e nessun genitore è inventato.
    assert {r.location_uid for r in model.rooms} == {relbuild.L1, relbuild.L2}
    assert {d.rack_uid for d in model.devices} <= {r.uid for r in model.racks}


def test_ordering_is_carried_by_ordinal_not_by_row_order():
    """⚠ Il punto per cui esiste la colonna `ordinal`.

    Si costruisce un modello con le righe in ordine INVERSO e si verifica che il
    documento esca nell'ordine giusto. L'ordine fisico delle righe di PostgreSQL
    non è definito, e affidarsi a quello produrrebbe eventi `reorder` fantasma al
    primo `VACUUM` — su un documento che nessuno ha toccato.
    """
    model = normalise(FIXTURES["base"])
    rovesciato = RelationalModel(
        schema_version=model.schema_version,
        has_manual=model.has_manual,
        locations=tuple(reversed(model.locations)),
        rooms=tuple(reversed(model.rooms)),
        racks=tuple(reversed(model.racks)),
        devices=tuple(reversed(model.devices)),
        manual=tuple(reversed(model.manual)),
        root_extra=model.root_extra,
    )
    assert assemble(rovesciato) == assemble(model)


def test_a_reordered_document_is_not_the_same_document():
    """Controprova: se l'ordine non fosse conservato, questo test passerebbe per il
    motivo sbagliato — cioè la fixture «riordinata» sarebbe indistinguibile dalla
    base e i test sull'ordine non proverebbero niente."""
    base = canonicalise(FIXTURES["base"])
    reordered = canonicalise(FIXTURES["reordered"])
    assert base != reordered
    eventi = [e["event"] for e in diff_as_dicts(base, reordered)]
    assert "reorder" in eventi, eventi


def test_explicit_and_implicit_defaults_converge():
    """Due documenti che l'applicazione considera identici devono dare lo stesso
    modello: è la ragione per cui la mappa canonicalizza in ingresso (§8.14)."""
    a = normalise(FIXTURES["implicit-defaults"])
    b = normalise(FIXTURES["explicit-defaults"])
    assert a == b


def test_empty_strings_zeroes_and_false_survive():
    """Sono valori dell'utente, non assenze: la canonicalizzazione sostituisce solo
    `None`. Trattarli come vuoti li rimpiazzerebbe con i default, e la differenza
    comparirebbe nel registro come una modifica mai fatta."""
    doc = FIXTURES["empty-zero-false"]
    model = normalise(doc)
    room = model.rooms[0]
    rack = model.racks[0]
    device = model.devices[0]
    assert room.w == 0 and room.h == 0
    assert room.segnaposto is False
    assert rack.x == 0 and rack.y == 0 and rack.u == 0
    assert rack.name == ""
    assert device.u == 0 and device.h == 0 and device.note == ""
    out = assemble(model)
    r = out["locations"][0]["sale"][0]
    assert r["w"] == 0 and r["segnaposto"] is False
    assert r["racks"][0]["u"] == 0 and r["racks"][0]["name"] == ""


def test_a_missing_manual_root_stays_missing():
    """`manuale` assente e `manuale: []` sono documenti diversi, e la
    canonicalizzazione conserva la differenza. Inventare la radice farebbe comparire
    nell'audit una modifica che nessuno ha fatto."""
    assert "manuale" not in round_trip(FIXTURES["no-manual"])
    assert round_trip(FIXTURES["empty-manual"])["manuale"] == []
    assert normalise(FIXTURES["no-manual"]).has_manual is False
    assert normalise(FIXTURES["empty-manual"]).has_manual is True


def test_the_current_photo_is_a_column_and_absence_is_absence():
    model = normalise(FIXTURES["base"])
    # ⚠ Si indicizza per `uid`, non per `code`: nella base ci sono DUE rack «R01»,
    # in due sale diverse, ed è il caso normale. Un dizionario per codice ne
    # terrebbe uno solo e il test parlerebbe del rack sbagliato.
    by_uid = {r.uid: r for r in model.racks}
    assert by_uid[relbuild.K1].photo_id == relbuild.FOTO_A
    assert by_uid[relbuild.K2].photo_id is None
    assert by_uid[relbuild.K3].photo_id == relbuild.FOTO_B
    # Il rack senza foto non deve acquistare una chiave `foto`.
    out = assemble(model)
    racks = out["locations"][0]["sale"][0]["racks"]
    r02 = [r for r in racks if r["_uid"] == relbuild.K2][0]
    assert "foto" not in r02


def test_an_explicit_null_photo_is_not_the_same_as_no_photo():
    """`foto: null` esplicito non è rappresentabile da una colonna `uuid`, quindi
    viaggia in `extra` — ed è esattamente il caso che dimostra perché la regola
    «colonna NULL ⇔ chiave in extra» deve valere in entrambi i versi."""
    doc = FIXTURES["explicit-null-photo"]
    model = normalise(doc)
    rack = [r for r in model.racks if r.code == "R02"][0]
    assert rack.photo_id is None
    assert rack.extra["foto"] is None
    out = round_trip(doc)
    r02 = [r for r in out["locations"][0]["sale"][0]["racks"] if r["id"] == "R02"][0]
    assert "foto" in r02 and r02["foto"] is None


def test_serial_arrays_keep_their_order():
    model = normalise(FIXTURES["base"])
    rack = [r for r in model.racks if r.code == "R01"][0]
    assert rack.seriali == ["2006004084", "2006004085"]


def test_room_geometry_and_vani_stay_structured():
    """I vani restano un value object della sala (§8.12): nessuna identità
    visibile, nessun CRUD indipendente, nessuno spostamento, nessuna interrogazione
    globale. Una tabella `vani` più una tabella `porte` darebbero due join per
    disegnare una pianta e zero garanzie in più."""
    model = normalise(FIXTURES["deep-room-geometry"])
    room = [r for r in model.rooms if r.code == "sala-1"][0]
    assert isinstance(room.vani, list) and len(room.vani) == 4
    assert room.vani[3]["porta2"]["lato"] == "bottom"
    assert room.vani[2].get("porta") is None
    # E la sala senza vani ha una lista vuota, non NULL.
    vuota = [r for r in model.rooms if r.code == "sala-2"][0]
    assert vuota.vani == []


def test_manual_blocks_stay_structured():
    model = normalise(FIXTURES["base"])
    voce = model.manual[0]
    assert voce.titolo == "Avvio"
    assert voce.blocchi[0]["paragrafi"][1] == "Secondo paragrafo."
    # `custom` non ha una colonna: è un campo dell'interfaccia, e viaggia in extra.
    assert model.manual[1].extra["custom"] is True


def test_unknown_fields_are_carried_at_every_level():
    """Il documento è APERTO: lo schema congelato vincola le chiavi di radice, non
    i campi delle entità. Una mappa che elencasse le colonne e buttasse il resto
    sarebbe lossy per costruzione, e lo si scoprirebbe in produzione."""
    model = normalise(FIXTURES["unknown-fields"])
    assert model.locations[0].extra["etichettaFutura"] == "qualcosa"
    assert [r for r in model.rooms if r.code == "sala-1"][0].extra["temperaturaMax"] == 27.5
    assert [r for r in model.racks if r.code == "R01"][0].extra["cablaggio"] == {
        "patch": 24, "fibra": 4}
    assert model.devices[0].extra["tagArbitrari"] == ["a", "b"]
    assert model.manual[0].extra["revisione"] == 3


def test_untyped_values_go_to_extra_and_are_reported():
    doc = FIXTURES["untyped-values"]
    model = normalise(doc)
    rack = [r for r in model.racks if r.uid == relbuild.K1][0]
    assert rack.u is None and rack.extra["u"] == "45"
    # ⚠ `seriali` è un `text[]`, non un JSONB: una lista con un numero dentro
    # diventerebbe `{"ok","12345"}` e il numero tornerebbe indietro come stringa.
    # Il difetto era nella mappa, non nel test: `isinstance(v, list)` diceva
    # «rappresentabile» per una colonna che non poteva contenerlo.
    assert rack.seriali is None and rack.extra["seriali"] == ["ok", 12345]
    device = [d for d in model.devices if d.uid == relbuild.D1][0]
    assert device.h is None and device.extra["h"] == 1.5

    found = validate_model(model)
    assert errors(found) == []
    carried = [f for f in found if f.code == CARRIED_VERBATIM]
    assert {f.field for f in carried} >= {"u", "seriali", "h"}


def test_a_serial_array_of_strings_does_reach_the_column():
    """La controprova: se `seriali` finisse SEMPRE in `extra` il test precedente
    passerebbe senza dimostrare niente, e la colonna non servirebbe a nessuno."""
    model = normalise(FIXTURES["base"])
    rack = [r for r in model.racks if r.uid == relbuild.K1][0]
    assert rack.seriali == ["2006004084", "2006004085"]
    assert "seriali" not in rack.extra


def test_a_boolean_never_lands_in_an_integer_column():
    """⚠ In Python `True` è un `int`: `isinstance(True, int)` è vero.

    Senza la cura specifica, `u: True` finirebbe in una colonna intera come 1 e
    tornerebbe indietro come `1` invece di `True` — una differenza che il diff
    riporterebbe come una modifica dell'utente.
    """
    doc = relbuild.base()
    rack = relbuild._find_rack(doc, relbuild.K1)
    rack["u"] = True
    model = normalise(doc)
    row = [r for r in model.racks if r.uid == relbuild.K1][0]
    assert row.u is None
    assert row.extra["u"] is True
    assert round_trip(doc)["locations"][0]["sale"][0]["racks"][0]["u"] is True


def test_the_root_keys_the_schema_does_not_know_are_carried():
    """Il seed nel repository ha ancora `utenti` e `versione`, che solo la
    migrazione può consumare (§8.16). La mappa non li capisce e non li perde."""
    model = normalise(FIXTURES["seed-legacy"])
    assert set(model.root_extra) == {"utenti", "versione"}
    assert "utenti" in round_trip(FIXTURES["seed-legacy"])
    # E il documento che il database contiene DAVVERO non ne ha nessuno.
    assert normalise(FIXTURES["seed"]).root_extra == {}


def test_the_production_seed_is_fully_normalised():
    """Nessun campo del seed reale finisce in `extra`: se ce ne fosse uno, la
    tabella non lo potrebbe interrogare, e vale la pena saperlo adesso e non nella
    fase 2D."""
    model = normalise(FIXTURES["seed"])
    carried = {
        f"{kind}.{f.field}"
        for kind, rows in (("location", model.locations), ("room", model.rooms),
                           ("rack", model.racks), ("device", model.devices),
                           ("manual", model.manual))
        for f in validate_model(model) if f.code == CARRIED_VERBATIM
    }
    assert carried == set(), carried
    assert model.counts() == {"locations": 3, "rooms": 6, "racks": 102,
                              "devices": 86, "manual": 0}


def test_two_devices_with_the_same_business_id_in_different_racks():
    """Caso normale con gli inventari importati da fogli di calcolo: l'identità è
    l'`_uid`, il codice è un attributo mutabile."""
    model = normalise(FIXTURES["base"])
    same = [d for d in model.devices if d.code == "srv-web"]
    assert len(same) == 2
    assert len({d.uid for d in same}) == 2
    assert len({d.rack_uid for d in same}) == 2, "devono stare in rack diversi"
    assert errors(validate_model(model)) == []


def test_two_devices_with_the_same_business_id_in_the_same_rack_are_a_warning():
    """Ammesso, non un errore: il validatore di identità lo tollera da sempre e
    l'import tabellare lo produce. Un vincolo qui farebbe rifiutare alla fase 2C
    documenti che la fase 1 accetta."""
    found = validate_model(normalise(FIXTURES["same-code-same-rack"]))
    assert errors(found) == []
    assert DUPLICATE_DEVICE_CODE in codes(warnings(found))


def test_renaming_and_moving_keep_the_identity():
    renamed = normalise(FIXTURES["renamed"])
    base = normalise(FIXTURES["base"])
    assert {r.uid for r in renamed.racks} == {r.uid for r in base.racks}
    assert [r for r in renamed.racks if r.uid == relbuild.K1][0].code == "R01-NUOVO"

    moved = normalise(FIXTURES["moved-device"])
    assert {d.uid for d in moved.devices} == {d.uid for d in base.devices}
    prima = [d for d in base.devices if d.uid == relbuild.D1][0]
    dopo = [d for d in moved.devices if d.uid == relbuild.D1][0]
    assert prima.rack_uid != dopo.rack_uid


def test_swapped_codes_are_valid_in_the_final_state():
    """Il motivo per cui i vincoli di unicità con ambito devono essere
    `DEFERRABLE`: a metà transazione i due codici collidono, alla fine no."""
    found = validate_model(normalise(FIXTURES["swapped-codes"]))
    assert errors(found) == []


def test_broken_dates_and_unknown_enums_are_warnings_not_errors():
    for name, expected in (("broken-dates", INVALID_DATE),
                           ("unknown-enums", INVALID_ENUM)):
        found = validate_model(normalise(FIXTURES[name]))
        assert errors(found) == [], name
        assert expected in codes(warnings(found)), name


def test_the_date_warning_agrees_with_the_expiry_scanner():
    """L'avviso significa esattamente «lo scanner delle scadenze ignorerà questa
    data»: usa il suo parser, non un secondo controllo scritto a parte. Due idee di
    «data valida» in due moduli divergono, e divergerebbero sui casi limite."""
    from app.notifications.expiry import parse_expiry
    found = validate_model(normalise(FIXTURES["expiry"]))
    segnalate = {f.uid for f in found if f.code == INVALID_DATE}
    model = normalise(FIXTURES["expiry"])
    attese = {d.uid for d in model.devices
              for v in (d.garanzia, d.supporto)
              if v not in (None, "") and parse_expiry(v) is None}
    assert segnalate == attese
    assert attese, "la fixture deve contenere almeno una data rotta"


# ==================================================================
# 3. modelli malformati: che cosa la validazione deve dire
# ==================================================================

def _model(**over) -> RelationalModel:
    """Un modello minimo e valido, da rompere un pezzo per volta."""
    defaults = dict(
        schema_version=1,
        has_manual=False,
        locations=(LocationRow(uid=relbuild.L1, ordinal=0, code="sito",
                               nome="Sito"),),
        rooms=(RoomRow(uid=relbuild.R1, location_uid=relbuild.L1, ordinal=0,
                       code="sala", nome="Sala"),),
        racks=(RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0,
                       code="R01", name="R01"),),
        devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1, ordinal=0,
                           code="srv", name="srv"),),
    )
    defaults.update(over)
    return RelationalModel(**defaults)


def test_a_valid_hand_built_model_has_no_findings():
    """La controprova che rende utili tutti i test negativi: se il modello di
    partenza avesse già dei rilievi, ogni test successivo passerebbe senza
    dimostrare la propria causa."""
    assert validate_model(_model()) == []


def test_duplicate_uid_is_an_error():
    model = _model(devices=(
        DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1, ordinal=0, code="a", name="a"),
        DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1, ordinal=1, code="b", name="b"),
    ))
    found = validate_model(model)
    assert DUPLICATE_UID in codes(errors(found))


def test_a_duplicate_uid_across_kinds_is_an_error():
    """L'`_uid` è la chiave primaria di TUTTE le entità: un rack e un dispositivo
    non possono condividerlo, anche se stanno in tabelle diverse."""
    model = _model(devices=(DeviceRow(uid=relbuild.K1, rack_uid=relbuild.K1,
                                      ordinal=0, code="a", name="a"),))
    assert DUPLICATE_UID in codes(errors(validate_model(model)))


def test_a_missing_uid_is_an_error():
    model = _model(devices=(DeviceRow(uid=None, rack_uid=relbuild.K1, ordinal=0,
                                      code="a", name="a"),))
    assert MISSING_UID in codes(errors(validate_model(model)))


def test_a_malformed_uid_is_an_error():
    model = _model(devices=(DeviceRow(uid="non-un-uuid", rack_uid=relbuild.K1,
                                      ordinal=0, code="a", name="a"),))
    assert MALFORMED_UID in codes(errors(validate_model(model)))


def test_an_unknown_parent_is_an_error():
    model = _model(devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K4,
                                      ordinal=0, code="a", name="a"),))
    assert UNKNOWN_PARENT in codes(errors(validate_model(model)))


def test_a_row_with_an_unknown_parent_is_omitted_not_reattached():
    """La riga orfana non viene attaccata altrove: un documento plausibile e falso
    è peggio di un documento a cui manca un pezzo, perché nessuno lo va a
    controllare."""
    model = _model(devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K4,
                                      ordinal=0, code="a", name="a"),))
    out = assemble(model)
    assert out["locations"][0]["sale"][0]["racks"][0]["devices"] == []


def test_a_duplicate_scoped_code_is_an_error_for_structure():
    model = _model(racks=(
        RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0, code="R01",
                name="a"),
        RackRow(uid=relbuild.K2, room_uid=relbuild.R1, ordinal=1, code="R01",
                name="b"),
    ), devices=())
    assert DUPLICATE_SCOPED_CODE in codes(errors(validate_model(model)))


def test_the_same_code_in_two_different_rooms_is_fine():
    """L'unicità ha un AMBITO: due sale possono avere entrambe un rack «R01», e
    infatti il seed di produzione ne è pieno."""
    model = _model(
        rooms=(RoomRow(uid=relbuild.R1, location_uid=relbuild.L1, ordinal=0,
                       code="s1", nome="S1"),
               RoomRow(uid=relbuild.R2, location_uid=relbuild.L1, ordinal=1,
                       code="s2", nome="S2")),
        racks=(RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0,
                       code="R01", name="a"),
               RackRow(uid=relbuild.K2, room_uid=relbuild.R2, ordinal=0,
                       code="R01", name="b")),
        devices=())
    assert validate_model(model) == []


def test_a_duplicate_ordinal_is_an_error():
    model = _model(racks=(
        RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0, code="a", name="a"),
        RackRow(uid=relbuild.K2, room_uid=relbuild.R1, ordinal=0, code="b", name="b"),
    ), devices=())
    assert DUPLICATE_ORDINAL in codes(errors(validate_model(model)))


def test_assembly_stays_deterministic_even_with_duplicate_ordinals():
    """Due righe nella stessa posizione sono un difetto, ma il riassemblaggio deve
    restare deterministico: altrimenti lo stesso modello darebbe due documenti
    diversi e il confronto dei digest della fase 2B fallirebbe a intermittenza,
    che è il modo peggiore di fallire."""
    model = _model(racks=(
        RackRow(uid=relbuild.K2, room_uid=relbuild.R1, ordinal=0, code="b", name="b"),
        RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0, code="a", name="a"),
    ), devices=())
    assert assemble(model) == assemble(model)
    ordine = [r["id"] for r in assemble(model)["locations"][0]["sale"][0]["racks"]]
    assert ordine == ["a", "b"], "lo spareggio è l'uid, non l'ordine di arrivo"


def test_a_non_integer_ordinal_is_an_error():
    model = _model(devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1,
                                      ordinal="primo", code="a", name="a"),))
    assert INVALID_ORDINAL in codes(errors(validate_model(model)))


def test_a_gap_in_the_ordinals_is_only_a_warning():
    model = _model(devices=(
        DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1, ordinal=0, code="a", name="a"),
        DeviceRow(uid=relbuild.D2, rack_uid=relbuild.K1, ordinal=7, code="b", name="b"),
    ))
    found = validate_model(model)
    assert errors(found) == []
    assert NON_CONTIGUOUS_ORDINAL in codes(warnings(found))
    # E il documento esce comunque nell'ordine giusto: si ordina, non si indicizza.
    devices = assemble(model)["locations"][0]["sale"][0]["racks"][0]["devices"]
    assert [d["id"] for d in devices] == ["a", "b"]


def test_a_malformed_extra_is_an_error():
    model = _model(devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1,
                                      ordinal=0, code="a", name="a",
                                      extra="non un oggetto"),))
    assert MALFORMED_ROW in codes(errors(validate_model(model)))


def test_extra_shadowing_a_column_is_an_error():
    """La regola del modulo: la colonna vale NULL ⇔ la chiave è in `extra`. Se
    valgono entrambe, esistono due verità sullo stesso campo e il riassemblaggio ne
    scegliebbe una in silenzio."""
    model = _model(devices=(DeviceRow(uid=relbuild.D1, rack_uid=relbuild.K1,
                                      ordinal=0, code="a", name="a",
                                      extra={"name": "un altro nome"}),))
    assert EXTRA_SHADOWS_COLUMN in codes(errors(validate_model(model)))


def test_a_missing_referenced_photo_is_an_error_only_when_checked():
    model = _model(racks=(RackRow(uid=relbuild.K1, room_uid=relbuild.R1, ordinal=0,
                                  code="R01", name="R01",
                                  photo_id=relbuild.FOTO_A),), devices=())
    # Senza l'insieme delle foto note il controllo NON avviene: un controllo
    # saltato per distrazione somiglia molto a un controllo passato, quindi la
    # differenza fra «non controllare» e «nessuna foto esiste» è esplicita.
    assert validate_model(model) == []
    assert PHOTO_NOT_FOUND in codes(errors(validate_model(model, known_photo_ids=[])))
    assert validate_model(model, known_photo_ids=[relbuild.FOTO_A]) == []


def test_a_model_without_schema_version_is_an_error():
    """Un documento senza versione di schema va rifiutato, non aggiornato in
    silenzio (§8.13)."""
    assert MISSING_SCHEMA_VERSION in codes(errors(validate_model(_model(
        schema_version=None))))


# ==================================================================
# 4. proprietà della mappa
# ==================================================================

def test_normalise_does_not_mutate_its_input():
    doc = relbuild.base()
    prima = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    normalise(doc)
    assert json.dumps(doc, ensure_ascii=False, sort_keys=True) == prima


def test_normalise_survives_a_document_without_identity():
    """Un documento senza `_uid` non deve far cadere la mappa: la mappa descrive,
    la validazione giudica. Sollevare qui trasformerebbe ogni controllo in «la prima
    cosa che è andata storta», e la migrazione ha bisogno dell'elenco completo."""
    doc = {"schemaVersion": 1, "locations": [{"id": "x", "nome": "X", "sale": []}]}
    model = normalise(doc)
    assert model.locations[0].uid is None
    assert MISSING_UID in codes(errors(validate_model(model)))


@pytest.mark.parametrize("junk", [None, [], "testo", 42])
def test_normalise_survives_junk(junk):
    model = normalise(junk)
    assert model.counts() == {"locations": 0, "rooms": 0, "racks": 0,
                              "devices": 0, "manual": 0}


def test_every_mapped_column_has_a_document_key_and_vice_versa():
    """La corrispondenza è dichiarata in un posto solo (`FIELD_MAP`), e questo test
    la controlla contro i campi delle dataclass: due elenchi in due punti divergono,
    se nessuno li confronta."""
    from app.inventory.relational import ROW_CLASS, column_names
    for kind, mapping in FIELD_MAP.items():
        colonne = set(column_names(kind))
        dichiarate = {name for name, _key, _t in mapping}
        assert dichiarate <= colonne, (kind, dichiarate - colonne)
        nostre = colonne - dichiarate
        assert nostre <= {"uid", "ordinal", "extra", "location_uid", "room_uid",
                          "rack_uid"}, (kind, nostre)
        assert ROW_CLASS[kind].__name__.lower().startswith(kind[:4])
