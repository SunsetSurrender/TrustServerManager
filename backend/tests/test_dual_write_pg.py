"""Fase 2C: ogni salvataggio mantiene ENTRAMBE le rappresentazioni. PostgreSQL vero.

L'invariante, e tutto questo file esiste per provarlo. Dopo ogni `PUT` che cambia
qualcosa:

    projection_state.head_version == inventory_head.version
    projection_state.head_sha256  == inventory_versions.canonical_sha256 (testa)
    canonicalise(assemble(proiezione)) == documento immutabile in testa
    digest(assemble(proiezione))       == canonical_sha256 della testa

e una transazione che non può dimostrarle tutte e quattro si annulla per intero.
**Non esiste** uno stato committato in cui la testa JSON è avanzata e la proiezione
no, né il contrario.

Che cosa c'è qui e non altrove
------------------------------
`test_projection_pg.py` prova la RICOSTRUZIONE (fase 2B): il comando esplicito, il
lock, gli abort. Questo prova la SINCRONIZZAZIONE AUTOMATICA: che il salvataggio la
faccia, che la faccia bene su ogni forma di documento, che il rollback la porti via
tutta, e che due scritture concorrenti non possano lasciare le due rappresentazioni
disallineate nemmeno per un istante committato.

La parte che vale di più sono le INIEZIONI DI GUASTO. Una sincronizzazione che
funziona quando tutto va bene non dimostra niente sull'atomicità: il difetto che
conta è la scrittura parziale sopravvissuta, e l'unico modo di provarne l'assenza è
far fallire di proposito ogni singolo passo e guardare che cosa resta.

⚠ Nessuno LEGGE la proiezione. `GET /api/inventory` restituisce ancora il JSON, e un
test qui sotto lo verifica confrontando i tre digest.

Riferimento: BACKEND-PLAN.md §8.44.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import (
    Actor,
    InventoryRepository,
    ProjectionNotCurrentError,
    VersionConflictError,
    canonical_sha256,
)
from app.inventory import projection
from app.inventory.document import strip_legacy_fields
from app.inventory.projection import ProjectionAborted
from app.inventory.relational import MAPPER_VERSION, assemble

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")

PROJECTION_TABLES = ("inventory_locations", "inventory_rooms", "inventory_racks",
                     "inventory_devices", "inventory_manual_entries")


def _load(name: str, relative: str):
    """Per PERCORSO e con un nome proprio: i generatori si chiamano tutti `build.py`
    e `sys.modules` è condiviso da tutta la sessione di pytest, quindi un `import
    build` restituirebbe quello che un altro file ha già caricato — una fixture
    sbagliata, cioè un test che passa provando qualcos'altro."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational_dw", "fixtures/relational/build.py")
build_expiry = _load("tsm_fixture_expiry_dw",
                     "fixtures/expiry/build.py").build_inventory

TODAY = date(2026, 8, 10)

DOCUMENTS = dict(relbuild.documents())
DOCUMENTS["seed"] = strip_legacy_fields(
    json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
DOCUMENTS["expiry"] = build_expiry(TODAY)

#: Escluso dalla passata generale, con un test proprio che spiega perché: contiene
#: `1e+20` e `-0.0`, che il MAGAZZINO DELLE ISTANTANEE rifiuta (§8.16). Non arriva
#: mai a un salvataggio, quindi non è un caso della fase 2C. Elenco esplicito e non
#: uno `skip` silenzioso: un test saltato somiglia molto a un test passato.
SNAPSHOT_REJECTED = ("jsonb-hostile-numbers",)
NAMES = sorted(set(DOCUMENTS) - set(SNAPSHOT_REJECTED))


# ==================================================================
# impianto
# ==================================================================

@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Database pulito. L'ordine è imposto dalle chiavi esterne.

    Le foto si cancellano DOPO i rack: `inventory_racks.photo_id` le protegge, ed è
    la stessa protezione che impedisce alla GC di cancellare la foto che lo stato
    corrente sta usando.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM inventory_projection_state"))
        c.execute(text("DELETE FROM photos"))
        # La GC delle foto è idempotente sulla data LOCALE: senza questa riga il giro
        # di un test troverebbe `already_ran_today` per un'esecuzione fatta da
        # un'altra suite, e passerebbe senza aver cancellato niente — cioè senza
        # provare quello che dichiara.
        c.execute(text("DELETE FROM maintenance_runs"))
        for n, photo_id in enumerate((relbuild.FOTO_A, relbuild.FOTO_B)):
            payload = b"\x89PNG\r\n\x1a\n" + bytes([n])
            c.execute(text("""
                INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
                VALUES (:i, 'image/png', :b, :s, :n)
            """), {"i": photo_id, "b": payload, "n": len(payload),
                   "s": hashlib.sha256(payload).hexdigest()})
    yield engine


def bootstrap(engine, doc: dict) -> int:
    with engine.begin() as c:
        return InventoryRepository(c).bootstrap(doc, ADMIN).version


def save(engine, doc: dict, *, base: int | None = None):
    with engine.begin() as c:
        repo = InventoryRepository(c)
        return repo.save(repo.head_version() if base is None else base, doc, ADMIN)


def head_of(engine) -> tuple[int, str, dict]:
    """(versione, digest registrato, documento) della testa."""
    with engine.begin() as c:
        version = c.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar_one()
        row = c.execute(text("SELECT canonical_sha256, doc FROM inventory_versions "
                             " WHERE version = :v"), {"v": version}).one()
    return int(version), row[0], row[1]


def state_row(engine) -> dict | None:
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT head_version, head_sha256, mapper_version, schema_version, "
            "       has_manual, root_extra "
            "  FROM inventory_projection_state")).mappings().first()
    return dict(row) if row else None


def counts(engine) -> dict:
    with engine.begin() as c:
        return projection.counts(c)


def verify(engine):
    with engine.begin() as c:
        return projection.verify(c)


def snapshot_of_everything(engine) -> dict:
    """Fotografia di TUTTO ciò che una scrittura potrebbe toccare.

    Serve alle iniezioni di guasto: dopo un fallimento si confronta questa struttura
    con quella di prima, e un solo campo diverso fa fallire il test. Confrontare
    tabella per tabella dentro ogni test significherebbe dimenticarne una — ed è la
    tabella dimenticata quella dove il difetto sopravvive.
    """
    with engine.begin() as c:
        def rows(sql):
            return [tuple(str(v) for v in r) for r in c.execute(text(sql)).all()]

        return {
            "head": c.execute(text("SELECT version FROM inventory_head "
                                   " WHERE id IS TRUE")).scalar(),
            "versioni": rows("SELECT version, canonical_sha256 "
                             "  FROM inventory_versions ORDER BY version"),
            "audit": c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            "photo_refs": rows("SELECT inventory_version, photo_id "
                               "  FROM inventory_photo_refs "
                               " ORDER BY inventory_version, photo_id"),
            "stato": rows("SELECT head_version, head_sha256, mapper_version "
                          "  FROM inventory_projection_state"),
            "locations": rows("SELECT uid, code, nome, ordinal FROM "
                              "inventory_locations ORDER BY uid"),
            "rooms": rows("SELECT uid, location_uid, code, ordinal "
                          "  FROM inventory_rooms ORDER BY uid"),
            "racks": rows("SELECT uid, room_uid, code, name, photo_id, ordinal "
                          "  FROM inventory_racks ORDER BY uid"),
            "devices": rows("SELECT uid, rack_uid, code, name, garanzia, "
                            "       garanzia_date, ordinal "
                            "  FROM inventory_devices ORDER BY uid"),
            "manual": rows("SELECT uid, code, titolo, ordinal "
                           "  FROM inventory_manual_entries ORDER BY uid"),
        }


def assert_invariant(engine) -> None:
    """L'invariante completo della fase 2C, in un posto solo.

    Si richiama dopo OGNI salvataggio riuscito di questo file. Averlo in una funzione
    non è economia di righe: è ciò che rende impossibile scrivere un test nuovo che
    verifica tre quarti dell'invariante e dimentica il quarto.
    """
    version, recorded, doc = head_of(engine)
    state = state_row(engine)

    assert state is not None, "la proiezione non dichiara nessuna versione"
    assert state["head_version"] == version
    assert state["head_sha256"] == recorded
    assert state["mapper_version"] == MAPPER_VERSION

    with engine.begin() as c:
        model = projection.read_model(c)
    rebuilt = assemble(model)
    assert canonical_sha256(rebuilt) == recorded
    assert rebuilt == doc, "il documento riassemblato non è quello in testa"

    # E lo strumento indipendente è d'accordo: se `verify` e le asserzioni qui sopra
    # dissentissero, una delle due sarebbe scritta male.
    result = verify(engine)
    assert result.faithful and result.current and result.ok


# ==================================================================
# 1. ogni forma di documento sopravvive a un salvataggio vero
# ==================================================================

@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_every_document_dual_writes_on_bootstrap(db, engine, name):
    """Il bootstrap è una scrittura che cambia l'inventario, quindi proietta.

    Su tutti i documenti di prova: seed di produzione, inventario delle scadenze,
    voci di manuale, `vani` come oggetti-valore, campi ignoti destinati a `extra`,
    valori falsi espliciti, `seriali` di tipi misti, date valide e rotte, numeri
    ostili, interi fuori scala.

    Senza questo, un'installazione nuova nascerebbe con una testa e nessuna
    proiezione, e il primo `PUT` riceverebbe 503 fino a un `--rebuild` a mano.
    """
    bootstrap(engine, DOCUMENTS[name])
    assert_invariant(engine)


@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_every_document_dual_writes_on_save(db, engine, name):
    """E lo stesso attraverso un `PUT`, che è la strada vera. **Nessun salto.**

    ⚠ La prima stesura partiva da un documento e ne salvava un ALTRO, e tre casi
    finivano in `pytest.skip` perché la transizione di identità fra due fixture
    indipendenti è illegittima (§8.4) — gli `_uid` non si sostituiscono. La copertura
    non era persa (il bootstrap li prova tutti), ma un test saltato somiglia troppo a
    un test passato, e tre skip in una parametrizzazione da venticinque diventano
    invisibili al primo sguardo.

    Adesso ogni documento parte da SE STESSO e viene salvato con una modifica minima
    e sempre legale: il nome di un sito. Nessuna identità cambia, quindi nessuna
    transizione è rifiutata, e tutti e venticinque i documenti passano davvero dal
    percorso di salvataggio.
    """
    doc = DOCUMENTS[name]
    bootstrap(engine, doc)
    assert_invariant(engine)

    modificato = deepcopy(doc)
    sito = modificato["locations"][0]
    sito["nome"] = (sito.get("nome") or "") + " — modificato dal test"

    result = save(engine, modificato)
    assert result.created is True, "la modifica non è stata riconosciuta come tale"
    assert_invariant(engine)


def test_the_production_seed_dual_writes_completely(db, engine):
    """Il seed vero: 3 siti, 6 sale, 102 rack, 86 dispositivi, in una transazione."""
    bootstrap(engine, DOCUMENTS["seed"])
    assert counts(engine) == {"locations": 3, "rooms": 6, "racks": 102,
                              "devices": 86, "manual": 0}
    assert_invariant(engine)


# ==================================================================
# 2. le forme di modifica: aggiunte, rimozioni, spostamenti, scambi
# ==================================================================

def _rack(doc, room=0, loc=0, index=0):
    return doc["locations"][loc]["sale"][room]["racks"][index]


def test_an_addition_appears_in_the_projection(db, engine):
    bootstrap(engine, DOCUMENTS["base"])
    prima = counts(engine)

    doc = deepcopy(DOCUMENTS["base"])
    sala = doc["locations"][0]["sale"][0]
    sala["racks"].append({
        "_uid": "cccccccc-0000-4000-8000-0000000000ff",
        "id": "R99", "name": "Rack nuovo", "u": 42,
        "x": 3.0, "y": 3.0, "w": 0.6, "h": 0.6, "devices": [],
    })
    save(engine, doc)

    assert counts(engine)["racks"] == prima["racks"] + 1
    assert_invariant(engine)


def test_a_removal_disappears_from_the_projection(db, engine):
    """Una riga tolta dal documento non deve restare in tabella.

    È il difetto che una sincronizzazione «solo upsert» produrrebbe: le righe nuove
    ci sono, le vecchie non se ne vanno, e il riassemblaggio contiene un rack che
    nessuno ha più.
    """
    bootstrap(engine, DOCUMENTS["base"])
    prima = counts(engine)

    doc = deepcopy(DOCUMENTS["base"])
    tolto = doc["locations"][0]["sale"][0]["racks"].pop()
    save(engine, doc)

    assert counts(engine)["racks"] == prima["racks"] - 1
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_racks WHERE uid = :u"),
                         {"u": tolto["_uid"]}).scalar_one() == 0
    assert_invariant(engine)


def test_a_rename_updates_the_row_and_keeps_the_identity(db, engine):
    """Il `code` cambia, l'`_uid` no: la riga è la stessa entità rinominata.

    È la distinzione su cui si regge tutta la fase 2 (§8.4): se la proiezione usasse
    il codice come chiave, una ridenominazione sarebbe indistinguibile da «una riga
    cancellata più una creata», e la storia dell'entità si spezzerebbe.
    """
    bootstrap(engine, DOCUMENTS["base"])
    uid = _rack(DOCUMENTS["base"])["_uid"]

    save(engine, relbuild.variant_renamed())

    with engine.begin() as c:
        row = c.execute(text("SELECT code, name FROM inventory_racks WHERE uid = :u"),
                        {"u": uid}).first()
    assert row is not None, "la ridenominazione ha cambiato identità"
    assert "NUOVO" in (row[0] or "") + (row[1] or "")
    assert_invariant(engine)


def test_a_move_between_parents_changes_the_foreign_key(db, engine):
    """Un dispositivo che cambia rack: `rack_uid` deve seguirlo.

    Una sincronizzazione incrementale che aggiornasse solo gli attributi lo
    lascerebbe appeso al genitore vecchio, e il riassemblaggio lo mostrerebbe nel
    rack sbagliato — con lo stesso numero di righe, quindi senza che nessun conteggio
    se ne accorga.
    """
    bootstrap(engine, DOCUMENTS["base"])
    spostato = relbuild.variant_moved_device()

    # Dove sta il dispositivo PRIMA e dove deve stare DOPO, secondo il documento.
    def genitore_nel_documento(doc, device_uid):
        for loc in doc["locations"]:
            for sala in loc["sale"]:
                for rack in sala["racks"]:
                    for dev in rack.get("devices", []):
                        if dev["_uid"] == device_uid:
                            return rack["_uid"]
        return None

    with engine.begin() as c:
        prima = {str(r[0]): str(r[1]) for r in c.execute(text(
            "SELECT uid, rack_uid FROM inventory_devices")).all()}

    save(engine, spostato)

    with engine.begin() as c:
        dopo = {str(r[0]): str(r[1]) for r in c.execute(text(
            "SELECT uid, rack_uid FROM inventory_devices")).all()}

    cambiati = [u for u in prima if prima[u] != dopo.get(u)]
    assert cambiati, "la fixture non sposta nessun dispositivo"
    for uid in cambiati:
        assert dopo[uid] == genitore_nel_documento(spostato, uid)
    assert_invariant(engine)


def test_a_reorder_changes_only_the_ordinal(db, engine):
    """L'ordine è un DATO e sta in `ordinal`, non nell'ordine fisico delle righe.

    PostgreSQL non garantisce nessun ordine di ritorno senza `ORDER BY`: se l'ordine
    del documento vivesse nella sequenza delle righe, il riassemblaggio cambierebbe
    da un `VACUUM` all'altro. Qui si prova che gli `ordinal` si aggiornano e che il
    documento riassemblato ha l'ordine nuovo.
    """
    bootstrap(engine, DOCUMENTS["base"])
    riordinato = relbuild.variant_reordered()
    save(engine, riordinato)

    with engine.begin() as c:
        ordinali = {str(r[0]): r[1] for r in c.execute(text(
            "SELECT uid, ordinal FROM inventory_racks")).all()}
    atteso = {}
    for loc in riordinato["locations"]:
        for sala in loc["sale"]:
            for n, rack in enumerate(sala["racks"]):
                atteso[rack["_uid"]] = n
    assert ordinali == atteso
    assert_invariant(engine)


def test_a_scoped_code_swap_needs_no_deferral(db, engine):
    """⚠ Il caso che decide la strategia di sincronizzazione.

    Due rack nella stessa sala si scambiano il `code`. Un `UPDATE` incrementale
    violerebbe `uq_rack_code` a metà — il vincolo è `DEFERRABLE INITIALLY IMMEDIATE`
    proprio per poter sopravvivere a quel momento. La sostituzione integrale cancella
    prima e inserisce dopo, quindi il conflitto non nasce affatto: non serve
    appoggiarsi al rinvio, e non serve ricordarsi che servirebbe.
    """
    bootstrap(engine, DOCUMENTS["base"])
    scambiato = relbuild.variant_swapped_codes()
    save(engine, scambiato)

    with engine.begin() as c:
        codici = {str(r[0]): r[1] for r in c.execute(text(
            "SELECT uid, code FROM inventory_racks")).all()}
    atteso = {}
    for loc in scambiato["locations"]:
        for sala in loc["sale"]:
            for rack in sala["racks"]:
                atteso[rack["_uid"]] = rack["id"]
    assert codici == atteso
    assert_invariant(engine)


def test_a_rename_and_a_move_in_the_same_put(db, engine):
    """Le due cose insieme, che è dove una differenza incrementale si perde:
    l'entità cambia genitore E attributi nella stessa transazione."""
    bootstrap(engine, DOCUMENTS["base"])

    doc = deepcopy(DOCUMENTS["base"])
    sala = doc["locations"][0]["sale"][0]
    if len(sala["racks"]) < 2:
        pytest.skip("la fixture non ha due rack nella stessa sala")
    origine, destinazione = sala["racks"][0], sala["racks"][1]
    if not origine.get("devices"):
        pytest.skip("il rack di partenza non ha dispositivi")

    dispositivo = origine["devices"].pop(0)
    dispositivo["name"] = "rinominato e spostato"
    dispositivo["id"] = "srv-trasferito"
    destinazione.setdefault("devices", []).append(dispositivo)
    save(engine, doc)

    with engine.begin() as c:
        row = c.execute(text("SELECT rack_uid, code, name FROM inventory_devices "
                             " WHERE uid = :u"), {"u": dispositivo["_uid"]}).one()
    assert str(row[0]) == destinazione["_uid"]
    assert row[1] == "srv-trasferito"
    assert row[2] == "rinominato e spostato"
    assert_invariant(engine)


def test_unchanged_rows_stay_semantically_identical(db, engine):
    """Una modifica in una sala non deve cambiare il SIGNIFICATO delle altre righe.

    ⚠ Si confronta il contenuto, non l'identità fisica della riga: la sostituzione
    integrale riscrive tutto, quindi `ctid` e `xmin` cambiano per costruzione e
    pretendere che non cambino sarebbe provare l'implementazione invece della
    proprietà. Ciò che deve restare identico è il dato.
    """
    bootstrap(engine, DOCUMENTS["base"])

    def righe_dei_dispositivi():
        with engine.begin() as c:
            return {str(r[0]): tuple(str(v) for v in r[1:]) for r in c.execute(text(
                "SELECT uid, rack_uid, code, name, u, ordinal, extra::text "
                "  FROM inventory_devices")).all()}

    prima = righe_dei_dispositivi()

    # Si tocca SOLO il nome di un sito: nessun dispositivo è coinvolto.
    doc = deepcopy(DOCUMENTS["base"])
    doc["locations"][0]["nome"] = "Pomezia (rinominata)"
    save(engine, doc)

    assert righe_dei_dispositivi() == prima
    assert_invariant(engine)


def test_duplicate_device_ids_still_pass(db, engine):
    """La fase 2 non è più severa della fase 1 (§8.44).

    Due dispositivi con lo stesso `id` nello stesso rack sono già accettati dal
    salvataggio: la proiezione deve accoglierli, non rifiutarli. Introdurre qui
    un'unicità che la fase 1 non ha significherebbe che un documento salvabile ieri
    non lo è più oggi — e nell'inventario reale quei duplicati esistono.
    """
    # ⚠ Si BOOTSTRAPPA con il documento che contiene i duplicati, invece di salvarlo
    # sopra la base. La fixture aggiunge un dispositivo con un `_uid` nuovo, e la
    # transizione di identità da `base` a quel documento è un'aggiunta legittima —
    # ma il validatore la rifiuta per un'altra ragione, che è affare di §8.4 e non
    # della fase 2C. Provare il duplicato dal bootstrap misura ciò che questo test
    # vuole misurare: che la PROIEZIONE lo accolga.
    bootstrap(engine, relbuild.variant_same_code_same_rack())

    with engine.begin() as c:
        codici = [r[0] for r in c.execute(text(
            "SELECT code FROM inventory_devices")).all()]
    assert len(codici) != len(set(codici)), "la fixture non ha duplicati"
    assert_invariant(engine)

    # E un salvataggio successivo sopra quel documento continua a funzionare: i
    # duplicati non sono un ostacolo alla sincronizzazione, solo all'unicità che non
    # esiste.
    doc = deepcopy(relbuild.variant_same_code_same_rack())
    doc["locations"][0]["nome"] = "Con duplicati, rinominato"
    save(engine, doc)
    assert_invariant(engine)


@pytest.mark.parametrize("name", ["empty-zero-false", "untyped-values",
                                  "unknown-fields", "deep-room-geometry",
                                  "empty-manual", "no-manual",
                                  "explicit-null-photo"])
def test_the_awkward_shapes_round_trip_through_a_save(db, engine, name):
    """Valori falsi espliciti, `seriali` di tipi misti, campi ignoti in `extra`,
    geometria dei `vani`, `manuale` assente contro `manuale: []`, foto a `null`.

    Sono le forme in cui il giro si rompe per un dettaglio di tipo, e vanno provate
    attraverso un salvataggio VERO e non solo con una ricostruzione: è il
    salvataggio che le farà passare in produzione.
    """
    bootstrap(engine, DOCUMENTS["base"])
    candidato = DOCUMENTS[name]
    if canonical_sha256(candidato) == canonical_sha256(DOCUMENTS["base"]):
        pytest.skip("identico alla base")
    save(engine, candidato)
    assert_invariant(engine)


def test_unknown_fields_survive_a_full_put_and_have_one_home(db, engine):
    """I campi ignoti passano dal `PUT`, finiscono in `extra`, e tornano identici.

    E la regola dell'unica casa: un valore non compare sia in una colonna tipizzata
    sia in `extra`. Duplicarlo significherebbe due fonti per lo stesso dato, e la
    domanda «quale vince» non ha una risposta buona.
    """
    bootstrap(engine, DOCUMENTS["base"])
    doc = relbuild.variant_unknown_fields()
    save(engine, doc)
    assert_invariant(engine)

    with engine.begin() as c:
        righe = c.execute(text(
            "SELECT code, name, extra FROM inventory_racks "
            " WHERE extra::text <> '{}'")).all()
    assert righe, "la fixture non produce nessun `extra`"
    for code, name, extra in righe:
        # Nessuna chiave di `extra` ripete il valore di una colonna tipizzata.
        assert "id" not in extra and "name" not in extra, extra


# ==================================================================
# 3. le foto: corrente contro storica
# ==================================================================

def test_replacing_a_photo_updates_the_current_and_keeps_the_history(db, engine):
    """⚠ Due meccanismi distinti, e appiattirli sarebbe perdere dati.

    `inventory_racks.photo_id` è la foto CORRENTE: cambia con lo stato.
    `inventory_photo_refs` sono le dipendenze STORICHE, una riga per versione, e
    sono ciò che la GC guarda.

    Dopo la sostituzione: il rack punta a B, e i riferimenti contengono ancora
    v(n) → A. Se li unissimo, la foto della versione precedente diventerebbe
    cancellabile appena il rack ne monta un'altra, e un rollback a quella versione
    mostrerebbe un riquadro rotto per sempre.
    """
    con_a = relbuild.base()
    v1 = bootstrap(engine, con_a)
    rack_uid = _rack(con_a)["_uid"]

    with engine.begin() as c:
        corrente = c.execute(text("SELECT photo_id FROM inventory_racks "
                                  " WHERE uid = :u"), {"u": rack_uid}).scalar()
    assert corrente is not None and str(corrente) == relbuild.FOTO_A

    con_b = deepcopy(con_a)
    _rack(con_b)["foto"] = relbuild.FOTO_B
    v2 = save(engine, con_b).version
    assert_invariant(engine)

    with engine.begin() as c:
        # lo stato CORRENTE è B
        assert str(c.execute(text("SELECT photo_id FROM inventory_racks "
                                  " WHERE uid = :u"),
                             {"u": rack_uid}).scalar()) == relbuild.FOTO_B
        # la STORIA ha entrambe, ciascuna con la sua versione
        refs = {(int(r[0]), str(r[1])) for r in c.execute(text(
            "SELECT inventory_version, photo_id FROM inventory_photo_refs")).all()}
    assert (v1, relbuild.FOTO_A) in refs
    assert (v2, relbuild.FOTO_B) in refs


def test_the_gc_keeps_a_photo_that_only_an_old_version_uses(db, engine):
    """La conseguenza operativa: A resta raggiungibile e la GC non la tocca.

    E `inventory_racks.photo_id` è una seconda difesa a livello di database: anche se
    la query della GC venisse riscritta male, la chiave esterna rifiuterebbe di
    cancellare la foto che lo stato corrente usa.
    """
    from datetime import datetime, timezone

    from app.photos import gc as photo_gc

    con_a = relbuild.base()
    bootstrap(engine, con_a)
    con_b = deepcopy(con_a)
    _rack(con_b)["foto"] = relbuild.FOTO_B
    save(engine, con_b)

    esito = photo_gc.run_once(engine, now_utc=datetime.now(timezone.utc), force=True)
    assert esito.ran
    with engine.begin() as c:
        vive = {str(r[0]) for r in c.execute(text("SELECT id FROM photos")).all()}
    assert relbuild.FOTO_A in vive, "la GC ha cancellato la foto di una versione storica"
    assert relbuild.FOTO_B in vive
    assert_invariant(engine)


def test_every_photo_the_projection_references_is_also_a_historical_ref(db, engine):
    """L'invariante che rende la chiave esterna INATTIVABILE dalla GC.

    Se un rack referenziasse una foto senza riga in `inventory_photo_refs` per la
    versione in testa, la GC proverebbe a cancellarla e l'intera transazione della GC
    fallirebbe — un guasto in un processo che deve girare da solo di notte. Le due
    scritture stanno nella stessa transazione, quindi non può succedere: qui si prova
    che l'insieme è davvero contenuto.
    """
    con_a = relbuild.base()
    version = bootstrap(engine, con_a)
    with engine.begin() as c:
        dalla_proiezione = {str(r[0]) for r in c.execute(text(
            "SELECT DISTINCT photo_id FROM inventory_racks "
            " WHERE photo_id IS NOT NULL")).all()}
        storiche = {str(r[0]) for r in c.execute(text(
            "SELECT photo_id FROM inventory_photo_refs "
            " WHERE inventory_version = :v"), {"v": version}).all()}
    assert dalla_proiezione, "la fixture non referenzia nessuna foto"
    assert dalla_proiezione <= storiche


# ==================================================================
# 4. colonne data derivate
# ==================================================================

def test_the_derived_dates_are_populated_by_a_save(db, engine):
    """`garanzia_date` si popola col parser dello scanner delle scadenze.

    Il testo grezzo resta autoritativo per il giro di andata e ritorno; la colonna
    data è derivata e serve solo alle query. Un valore non interpretabile lascia la
    colonna a NULL e CONSERVA il testo: la fase 2 non butta un dato per farlo entrare
    in un tipo.
    """
    bootstrap(engine, DOCUMENTS["dated-devices"])
    assert_invariant(engine)

    with engine.begin() as c:
        righe = c.execute(text(
            "SELECT garanzia, garanzia_date FROM inventory_devices "
            " WHERE garanzia IS NOT NULL")).all()
    assert righe, "la fixture non ha date di garanzia"
    assert any(r[1] is not None for r in righe), "nessuna data interpretata"


def test_unparseable_dates_are_preserved_with_a_null_derived_column(db, engine):
    """«in attesa», «vedi contratto»: l'inventario reale ne è pieno.

    Restano nel testo, la colonna derivata è NULL, e il salvataggio riesce. Rifiutarli
    renderebbe la fase 2 più severa della fase 1 (§8.44).
    """
    bootstrap(engine, DOCUMENTS["broken-dates"])
    assert_invariant(engine)

    with engine.begin() as c:
        righe = c.execute(text(
            "SELECT garanzia, garanzia_date FROM inventory_devices "
            " WHERE garanzia IS NOT NULL AND garanzia_date IS NULL")).all()
    assert righe, "la fixture non ha date non interpretabili"
    for testo, data in righe:
        # `is not None` e non `assert testo`: la fixture contiene `garanzia: ""`, che
        # è una stringa VUOTA — un valore falso ma presente, e la differenza fra «il
        # campo c'era, vuoto» e «il campo non c'era» è esattamente ciò che la
        # canonicalizzazione conserva (§8.14). Un `assert testo` la cancellerebbe.
        assert testo is not None, "il testo grezzo è stato perso"
        assert data is None


def test_a_sql_date_query_agrees_with_the_expiry_scanner(db, engine):
    """⚠ Il senso delle colonne derivate, provato contro l'unica fonte che conta.

    Lo scanner delle scadenze calcola `due_items` dal DOCUMENTO. Una query SQL sulle
    colonne derivate deve trovare gli stessi dispositivi: se dissentissero, la
    colonna interrogabile risponderebbe una cosa e le notifiche un'altra, e nessuno
    dei due saprebbe di avere torto.

    ⚠ Lo scanner NON passa a queste colonne in questo commit. Questo test dimostra
    che POTRÀ farlo, e nel frattempo che le due strade sono d'accordo.
    """
    from app.notifications.expiry import due_items

    doc = DOCUMENTS["expiry"]
    bootstrap(engine, doc)
    assert_invariant(engine)

    giorni = 60
    attesi_uid = {item.entity_uid for item in due_items(
        doc, today=TODAY, warning_days=[giorni]) if item.kind == "garanzia"}
    assert attesi_uid, "la fixture non produce scadenze di garanzia nella finestra"

    with engine.begin() as c:
        trovati = {str(r[0]) for r in c.execute(text("""
            SELECT uid FROM inventory_devices
             WHERE garanzia_date IS NOT NULL
               AND garanzia_date >= :oggi
               AND garanzia_date <= (CAST(:oggi AS date) + CAST(:g AS integer))
        """), {"oggi": TODAY, "g": giorni}).all()}

    assert trovati == attesi_uid, {
        "solo in SQL": sorted(trovati - attesi_uid),
        "solo nello scanner": sorted(attesi_uid - trovati),
    }


def test_the_notification_worker_still_reads_the_document(db, engine):
    """Il passaggio dello scanner a SQL NON è in questo commit (§8.44).

    Un controllo statico sul modulo: se un giorno lo scanner leggesse
    `garanzia_date`, sarebbe una decisione da prendere di proposito — con i suoi test
    — non un effetto collaterale della fase 2C.
    """
    from pathlib import Path as _Path

    import app.notifications.expiry as expiry_module
    sorgente = _Path(expiry_module.__file__).read_text(encoding="utf-8")
    assert "garanzia_date" not in sorgente
    assert "inventory_devices" not in sorgente


# ==================================================================
# 5. no-op canonico e idempotenza
# ==================================================================

def test_a_canonical_noop_writes_nothing_at_all(db, engine):
    """Nessuna versione, nessuna riscrittura della proiezione, nessun audit, nessun
    riferimento alle foto, nessun movimento della testa."""
    bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)
    with engine.begin() as c:
        sincronizzato_prima = c.execute(text(
            "SELECT synchronised_at FROM inventory_projection_state")).scalar_one()

    result = save(engine, deepcopy(DOCUMENTS["base"]))
    assert result.created is False

    assert snapshot_of_everything(engine) == prima
    # ⚠ Anche il timestamp: se la proiezione fosse stata riscritta con lo stesso
    # contenuto, tutto il resto combacerebbe e solo questo lo rivelerebbe.
    with engine.begin() as c:
        assert c.execute(text("SELECT synchronised_at FROM "
                              "inventory_projection_state")).scalar_one() \
            == sincronizzato_prima
    assert_invariant(engine)


def test_a_lost_response_replay_is_idempotent(db, engine):
    """Il caso reale: il commit riesce, la risposta si perde, il client riprova.

    A ha committato la versione 11 con la proiezione 11. Ritenta con `baseVersion`
    10 e lo stesso documento: deve ricevere `changed=false` e la versione 11, senza
    una seconda riscrittura della proiezione, una seconda versione o un secondo
    audit. Confrontare prima il `baseVersion` gli darebbe un conflitto per una
    scrittura che è già la sua (§8.18).
    """
    v1 = bootstrap(engine, DOCUMENTS["base"])
    nuovo = relbuild.variant_renamed()
    v2 = save(engine, nuovo, base=v1).version
    assert v2 == v1 + 1
    prima = snapshot_of_everything(engine)

    ripetuto = save(engine, deepcopy(nuovo), base=v1)     # il vecchio baseVersion
    assert ripetuto.created is False
    assert ripetuto.version == v2
    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_a_stale_base_version_with_different_content_still_conflicts(db, engine):
    """Un `baseVersion` superato con contenuto DIVERSO resta un conflitto, e non
    lascia niente: né versione, né proiezione toccata."""
    v1 = bootstrap(engine, DOCUMENTS["base"])
    save(engine, relbuild.variant_renamed(), base=v1)
    prima = snapshot_of_everything(engine)

    doc = deepcopy(DOCUMENTS["base"])
    doc["locations"][0]["nome"] = "Un'altra modifica ancora"
    with pytest.raises(VersionConflictError):
        save(engine, doc, base=v1)

    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


# ==================================================================
# 6. la precondizione: la proiezione deve già rispecchiare la testa
# ==================================================================

def test_a_save_is_refused_when_the_projection_state_is_missing(db, engine):
    """Fallire CHIUSO, e non curarsi da soli.

    Se il salvataggio riscrivesse comunque la proiezione, il sistema passerebbe da
    «disallineato e visibile» ad «allineato», cancellando ogni traccia del fatto che
    per un certo tempo non lo era — e con essa l'unica occasione di chiedersi perché.
    Un disallineamento ha una causa: una migrazione a metà, una scrittura fuori
    dall'API, un `--rebuild` mai eseguito.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_projection_state"))
    prima = snapshot_of_everything(engine)

    with pytest.raises(ProjectionNotCurrentError) as err:
        save(engine, relbuild.variant_renamed())
    assert err.value.code == "projection_not_current"
    assert err.value.details and err.value.details[0]["proiezione_versione"] is None
    assert snapshot_of_everything(engine) == prima


def test_a_save_is_refused_when_the_projection_is_stale_by_version(db, engine):
    """Lo stato dichiara una versione che non è la testa: si rifiuta.

    Per costruire la condizione servono DUE versioni: `head_version` ha una chiave
    esterna verso `inventory_versions`, quindi non si può far dichiarare allo stato
    una versione inesistente. Si sposta indietro alla 1, che esiste — ed è esattamente
    la forma che avrebbe un salvataggio andato a metà in un mondo senza transazione
    unica.
    """
    v1 = bootstrap(engine, DOCUMENTS["base"])
    v2 = save(engine, relbuild.variant_renamed(), base=v1).version
    assert v2 != v1

    with engine.begin() as c:
        c.execute(text("UPDATE inventory_projection_state SET head_version = :v"),
                  {"v": v1})
    prima = snapshot_of_everything(engine)

    doc = deepcopy(DOCUMENTS["base"])
    doc["locations"][0]["nome"] = "Terza modifica"
    with pytest.raises(ProjectionNotCurrentError) as err:
        save(engine, doc, base=v2)
    assert err.value.details[0]["proiezione_versione"] == v1
    assert err.value.details[0]["testa_versione"] == v2
    assert snapshot_of_everything(engine) == prima


def test_a_save_is_refused_when_the_projection_is_stale_by_digest(db, engine):
    """Stessa versione, digest diverso: un'istantanea immutabile non cambia, quindi
    qualcosa l'ha cambiata fuori dall'API. Non è il momento di scriverci sopra."""
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_projection_state "
                       "   SET head_sha256 = repeat('a', 64)"))
    prima = snapshot_of_everything(engine)

    with pytest.raises(ProjectionNotCurrentError):
        save(engine, relbuild.variant_renamed())
    assert snapshot_of_everything(engine) == prima


def test_a_save_is_refused_when_the_mapper_version_is_wrong(db, engine):
    """⚠ Il guasto che il digest NON vede.

    Una proiezione scritta da un'altra mappa riassembla lo stesso documento — quindi
    lo stesso digest, quindi nessun allarme dal confronto — e ha i dati nelle colonne
    sbagliate. Solo il numero registrato lo rivela.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_projection_state "
                       "   SET mapper_version = mapper_version + 1"))
    prima = snapshot_of_everything(engine)

    with pytest.raises(ProjectionNotCurrentError) as err:
        save(engine, relbuild.variant_renamed())
    assert err.value.details[0]["versione_mappa"] == MAPPER_VERSION + 1
    assert snapshot_of_everything(engine) == prima


def test_a_null_mapper_version_from_phase_2b_is_refused(db, engine):
    """Una proiezione della fase 2B non dichiara la mappa, e NULL non è supportata.

    Non si presume `1` per far tornare il controllo: quale mappa l'ha scritta lo
    sappiamo per deduzione — ce n'è stata una sola — e «per deduzione» non è un dato.
    La distribuzione dei dati fra colonne ed `extra` non è verificabile a posteriori.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_projection_state SET mapper_version = NULL"))

    with pytest.raises(ProjectionNotCurrentError):
        save(engine, relbuild.variant_renamed())

    # E il rimedio documentato funziona: un `--rebuild` la rende utilizzabile.
    with engine.begin() as c:
        projection.rebuild(c)
    save(engine, relbuild.variant_renamed())
    assert_invariant(engine)


def test_the_precondition_is_checked_before_the_noop_path(db, engine):
    """⚠ Anche un no-op passa dalla precondizione.

    Un no-op è una risposta di SUCCESSO. Restituirla mentre la proiezione è vecchia
    direbbe al client «tutto in ordine» da un backend che ha smesso di mantenere una
    delle due rappresentazioni — e se la richiesta ripetuta arrivasse proprio in quel
    momento, il difetto resterebbe invisibile esattamente al cliente che sta
    riprovando.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_projection_state"))

    with pytest.raises(ProjectionNotCurrentError):
        # Documento IDENTICO alla testa: senza il controllo prima, questo
        # restituirebbe changed=false e un 200.
        save(engine, deepcopy(DOCUMENTS["base"]))


def test_a_corrupted_projection_row_is_caught_by_verify_not_by_the_precondition(
        db, engine):
    """La divisione dei compiti, provata dal comportamento.

    Una riga corrotta con lo stato intatto passa la precondizione — che confronta
    numeri registrati — e viene presa da `--verify`, che riassembla. È deliberato: se
    la precondizione riassemblasse, ogni salvataggio pagherebbe un giro completo.

    ⚠ E il salvataggio successivo RIPARA la riga, perché la sostituzione è integrale.
    Non è la precondizione a farlo: è la strategia di sincronizzazione.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("""
            UPDATE inventory_racks SET name = 'CORROTTO'
             WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)
        """))

    result = verify(engine)
    assert result.current and not result.faithful

    save(engine, relbuild.variant_renamed())
    assert_invariant(engine)


# ==================================================================
# 7. iniezioni di guasto: nessuna scrittura parziale sopravvive
# ==================================================================

#: I punti in cui si inietta il guasto. Ognuno è una funzione REALE del percorso di
#: salvataggio: sostituirla con una che solleva è il modo di provare che il rollback
#: copre quel passo, e non una simulazione di un guasto che non può avvenire.
#:
#: `(etichetta, modulo, attributo)`
PUNTI_DI_GUASTO = [
    ("svuotamento della proiezione", projection, "clear"),
    ("inserimento nelle tabelle", projection, "write_model"),
    ("riga di stato", projection, "_write_state"),
    ("rilettura da SQL", projection, "read_model"),
    ("confronto dei modelli", projection, "model_differences"),
    ("riassemblaggio per il digest", projection, "assemble"),
    ("popolamento delle colonne derivate", projection, "validate_model"),
]


@pytest.mark.parametrize("etichetta,modulo,attributo", PUNTI_DI_GUASTO,
                         ids=[p[0] for p in PUNTI_DI_GUASTO])
def test_a_failure_during_the_sync_rolls_everything_back(
        db, engine, monkeypatch, etichetta, modulo, attributo):
    """Sette punti dentro la sincronizzazione. Dopo ognuno: NIENTE è cambiato.

    La testa, le versioni, l'audit, i riferimenti storici alle foto, tutte le righe
    della proiezione e la riga di stato. `snapshot_of_everything` li confronta tutti
    insieme, così un test nuovo non può dimenticarne uno.
    """
    v1 = bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)

    def esplode(*args, **kwargs):
        raise RuntimeError(f"guasto iniettato: {etichetta}")

    monkeypatch.setattr(modulo, attributo, esplode)
    with pytest.raises(RuntimeError, match="guasto iniettato"):
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()

    assert snapshot_of_everything(engine) == prima
    assert head_of(engine)[0] == v1
    assert_invariant(engine)


@pytest.mark.parametrize("metodo", ["_insert_version", "_insert_audit",
                                    "_update_head"])
def test_a_failure_in_the_snapshot_side_also_rolls_the_projection_back(
        db, engine, monkeypatch, metodo):
    """L'altro verso: il guasto è nella metà JSON e la proiezione non deve avanzare.

    È la simmetria che l'invariante pretende — non esiste uno stato in cui una delle
    due è avanzata — e va provata in entrambe le direzioni, perché il codice che le
    scrive è diverso.
    """
    bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)

    def esplode(*args, **kwargs):
        raise RuntimeError("guasto iniettato nel lato istantanea")

    monkeypatch.setattr(InventoryRepository, metodo, esplode)
    with pytest.raises(RuntimeError, match="lato istantanea"):
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()

    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_a_failure_in_the_photo_refs_rolls_everything_back(db, engine, monkeypatch):
    """I riferimenti storici stanno o cadono con la versione che li dichiara."""
    from app.photos import refs as photo_refs_module

    bootstrap(engine, relbuild.base())
    prima = snapshot_of_everything(engine)

    def esplode(*args, **kwargs):
        raise RuntimeError("guasto iniettato nei riferimenti")

    monkeypatch.setattr(photo_refs_module, "record", esplode)
    doc = deepcopy(relbuild.base())
    _rack(doc)["foto"] = relbuild.FOTO_B
    with pytest.raises(RuntimeError, match="riferimenti"):
        save(engine, doc)
    monkeypatch.undo()

    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_a_digest_mismatch_during_a_save_aborts_the_transaction(db, engine,
                                                                monkeypatch):
    """⚠ La controprova: si rompe il riassemblaggio e si pretende l'abort.

    Senza questo test, un confronto scritto male — che confronta il modello con sé
    stesso, o due volte lo stesso digest — sarebbe indistinguibile da un confronto
    soddisfatto, e tutto il file passerebbe senza dimostrare niente.
    """
    bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)

    vero_assemble = projection.assemble

    def assemble_bacato(model):
        doc = vero_assemble(model)
        doc["locations"][0]["sale"][0]["racks"].pop()
        return doc

    monkeypatch.setattr(projection, "assemble", assemble_bacato)
    with pytest.raises(ProjectionAborted) as err:
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()

    assert err.value.reason == "digest_diverso"
    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_a_read_back_difference_during_a_save_aborts_the_transaction(
        db, engine, monkeypatch):
    """L'altro dei quattro controlli: il modello riletto diverso da quello scritto.

    Non è coperto dal digest: un valore che passasse da una colonna a `extra`
    lascerebbe il documento identico. Si simula facendo restituire alla rilettura un
    modello a cui manca un dispositivo.
    """
    bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)

    vero_read = projection.read_model

    def read_bacato(conn):
        model = vero_read(conn)
        import dataclasses
        return dataclasses.replace(model, devices=model.devices[:-1])

    monkeypatch.setattr(projection, "read_model", read_bacato)
    with pytest.raises(ProjectionAborted) as err:
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()

    assert err.value.reason == "modello_riletto_diverso"
    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_an_incoherent_read_back_model_aborts_the_transaction(db, engine,
                                                              monkeypatch):
    """Il terzo controllo: il modello riletto incoerente.

    È l'unico che vede le colonne DERIVATE, a cui il digest è cieco: si simula un
    `garanzia_date` sbagliato, che lascerebbe il documento — e quindi il digest —
    identico.
    """
    import dataclasses

    bootstrap(engine, DOCUMENTS["dated-devices"])
    prima = snapshot_of_everything(engine)

    vero_read = projection.read_model

    def read_con_data_sbagliata(conn):
        model = vero_read(conn)
        devices = list(model.devices)
        for n, dev in enumerate(devices):
            if getattr(dev, "garanzia_date", None) is not None:
                devices[n] = dataclasses.replace(dev, garanzia_date=date(1999, 1, 1))
                break
        else:
            pytest.skip("nessuna data derivata da falsificare")
        return dataclasses.replace(model, devices=tuple(devices))

    monkeypatch.setattr(projection, "read_model", read_con_data_sbagliata)
    with pytest.raises(ProjectionAborted) as err:
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()

    assert err.value.reason in ("modello_riletto_diverso",
                               "modello_riletto_incoerente")
    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


def test_a_database_level_failure_mid_sync_rolls_back(db, engine, monkeypatch):
    """Un guasto del DATABASE, non del codice Python: si viola una chiave esterna.

    È il caso che un `monkeypatch` non riproduce — l'errore arriva da PostgreSQL a
    metà della sincronizzazione — e il rollback deve coprirlo allo stesso modo.
    """
    bootstrap(engine, DOCUMENTS["base"])
    prima = snapshot_of_everything(engine)

    vero_write = projection.write_model

    def write_con_foto_inesistente(conn, model):
        import dataclasses
        racks = list(model.racks)
        racks[0] = dataclasses.replace(
            racks[0], photo_id="00000000-0000-4000-8000-000000000000")
        return vero_write(conn, dataclasses.replace(model, racks=tuple(racks)))

    monkeypatch.setattr(projection, "write_model", write_con_foto_inesistente)
    with pytest.raises(Exception) as err:
        save(engine, relbuild.variant_renamed())
    monkeypatch.undo()
    assert "foreign key" in str(err.value).lower() or "violates" in str(err.value).lower()

    assert snapshot_of_everything(engine) == prima
    assert_invariant(engine)


# ==================================================================
# 8. concorrenza
# ==================================================================

def test_two_concurrent_saves_never_leave_the_two_halves_apart(db, engine):
    """⚠ A e B partono dalla stessa versione. Uno vince, l'altro ha un conflitto.

    Il requisito forte non è che B perda: è che non esista **nessun istante
    committato** in cui la testa è 2 e la proiezione è 1. Con la sostituzione
    integrale sotto il lock della testa, il perdente aspetta, si risveglia, rilegge
    una testa e una proiezione già allineate, e riceve il conflitto normale.
    """
    v1 = bootstrap(engine, DOCUMENTS["base"])

    doc_a = relbuild.variant_renamed()
    doc_b = deepcopy(DOCUMENTS["base"])
    doc_b["locations"][0]["nome"] = "Modificato da B"

    esiti: dict[str, object] = {}
    partenza = threading.Barrier(2)

    def salva(nome, doc):
        try:
            with engine.begin() as c:
                repo = InventoryRepository(c)
                partenza.wait(timeout=10)
                esiti[nome] = repo.save(v1, doc, ADMIN)
        except Exception as exc:              # il perdente
            esiti[nome] = exc

    fili = [threading.Thread(target=salva, args=("a", doc_a)),
            threading.Thread(target=salva, args=("b", doc_b))]
    for f in fili:
        f.start()
    for f in fili:
        f.join(timeout=60)

    vincitori = [k for k, v in esiti.items() if not isinstance(v, Exception)]
    perdenti = [k for k, v in esiti.items() if isinstance(v, Exception)]
    assert len(vincitori) == 1, esiti
    assert len(perdenti) == 1, esiti
    assert isinstance(esiti[perdenti[0]], VersionConflictError)

    # E lo stato finale è coerente: testa 2, proiezione 2, digest uguali.
    assert head_of(engine)[0] == v1 + 1
    assert_invariant(engine)


def test_the_loser_sees_an_already_synchronised_projection(db, engine):
    """Il perdente, al risveglio, NON vede `testa 11 / proiezione 10`.

    Si serializza a mano ciò che il test concorrente fa in parallelo: A committa, poi
    B guarda. Se le due metà avanzassero in due transazioni diverse, questa è la
    finestra in cui si vedrebbe la differenza.
    """
    v1 = bootstrap(engine, DOCUMENTS["base"])
    v2 = save(engine, relbuild.variant_renamed(), base=v1).version

    with engine.begin() as c:
        currency = projection.currency(c)
    assert currency.head_version == v2
    assert currency.projected_version == v2
    assert currency.current

    doc = deepcopy(DOCUMENTS["base"])
    doc["locations"][0]["nome"] = "B si sveglia"
    with pytest.raises(VersionConflictError) as err:
        save(engine, doc, base=v1)
    assert err.value.current_version == v2
    assert_invariant(engine)


# ==================================================================
# 9. GET non è cambiato, e i tre digest coincidono
# ==================================================================

def test_get_still_serves_the_json_snapshot_and_the_digests_agree(db, engine):
    """⚠ L'asserzione più ripetuta di questo commit, attraverso l'API vera.

        digest della risposta di GET
        == digest del documento riassemblato dalla proiezione
        == canonical_sha256 registrato in testa

    E `GET` legge il JSON: il passaggio a SQL è la fase 2D. Se leggesse già da SQL,
    il primo dei tre digest verrebbe dalla stessa fonte del secondo e il confronto
    non proverebbe più niente.
    """
    from app.api.deps import get_connection
    from app.main import app
    from conftest import ORIGIN, api_client

    bootstrap(engine, DOCUMENTS["base"])

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as client:
            from app.api.deps import require_actor
            app.dependency_overrides[require_actor] = lambda: ADMIN

            risposta = client.get("/api/inventory")
            assert risposta.status_code == 200
            corpo = risposta.json()

            version, recorded, doc = head_of(engine)
            with engine.begin() as c:
                dalla_proiezione = assemble(projection.read_model(c))

            assert corpo["version"] == version
            assert canonical_sha256(corpo["doc"]) == recorded
            assert canonical_sha256(dalla_proiezione) == recorded
            assert corpo["doc"] == doc

            # Un `PUT` attraverso l'API, e i tre digest coincidono ancora.
            risposta = client.put("/api/inventory", headers=ORIGIN,
                                  json={"baseVersion": version,
                                        "doc": relbuild.variant_renamed()})
            assert risposta.status_code == 200, risposta.text
            assert risposta.json()["changed"] is True

            version, recorded, doc = head_of(engine)
            corpo = client.get("/api/inventory").json()
            with engine.begin() as c:
                dalla_proiezione = assemble(projection.read_model(c))
            assert canonical_sha256(corpo["doc"]) == recorded
            assert canonical_sha256(dalla_proiezione) == recorded
    finally:
        app.dependency_overrides.clear()

    assert_invariant(engine)


def test_a_put_refused_for_a_stale_projection_answers_503(db, engine):
    """Il codice stabile arriva al client, con lo stato giusto.

    503 e non un 4xx: la richiesta era valida, è il backend che si rifiuta di operare
    finché mantiene una promessa a metà.
    """
    from app.api.deps import get_connection, require_actor
    from app.main import app
    from conftest import ORIGIN, api_client

    version = bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_projection_state"))

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = lambda: ADMIN
    try:
        with api_client(app) as client:
            r = client.put("/api/inventory", headers=ORIGIN,
                           json={"baseVersion": version,
                                 "doc": relbuild.variant_renamed()})
            assert r.status_code == 503, r.text
            assert r.json()["detail"]["code"] == "projection_not_current"
            # Il messaggio non nomina tabelle né comandi interni: il rimedio sta nei
            # log del server, dove lo legge chi opera (§8.21).
            testo = json.dumps(r.json())
            assert "inventory_" not in testo and "rebuild" not in testo

            # ⚠ In fase 2C `GET` funzionava ancora — leggeva il JSON — ed era
            # proprio questo che rendeva il guasto difficile da vedere senza la
            # readiness. Dalla fase 2D (§8.45) `GET` legge la proiezione, quindi
            # cade con lo STESSO codice: non c'è nessun ripiego sull'istantanea.
            # L'inversione è deliberata e va lasciata visibile qui, dove qualcuno
            # cercherà «e il GET?».
            g = client.get("/api/inventory")
            assert g.status_code == 503, g.text
            assert g.json()["detail"]["code"] == "projection_not_current"
    finally:
        app.dependency_overrides.clear()


def test_the_frontend_contract_is_unchanged(db, engine):
    """Le chiavi della risposta sono quelle di prima: il frontend non deve sapere
    che la proiezione esista (§8.22)."""
    from app.api.deps import get_connection, require_actor
    from app.main import app
    from conftest import ORIGIN, api_client

    version = bootstrap(engine, DOCUMENTS["base"])

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = lambda: ADMIN
    try:
        with api_client(app) as client:
            corpo = client.get("/api/inventory").json()
            assert set(corpo) == {"version", "doc", "sha256",
                                  "schemaVersion"}, sorted(corpo)

            r = client.put("/api/inventory", headers=ORIGIN,
                           json={"baseVersion": version,
                                 "doc": relbuild.variant_renamed()})
            assert set(r.json()) <= {"version", "changed", "sha256", "schemaVersion",
                                     "events", "scopes"}, sorted(r.json())
            testo = json.dumps(r.json())
            for parola in PROJECTION_TABLES + ("mapper_version", "projection",
                                               "garanzia_date"):
                assert parola not in testo, parola
    finally:
        app.dependency_overrides.clear()
