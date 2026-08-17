"""L'invariante del magazzino, contro PostgreSQL vero.

    Ogni documento accettato dal `PUT` normale deve essere rappresentabile senza
    perdite dal magazzino delle istantanee, secondo la semantica del digest
    canonico del repository.

Era falsa. `inventory_versions.doc` è JSONB, JSONB tiene i numeri in `numeric`, e
`numeric` non ha il segno dello zero né la notazione esponenziale: `-0.0` tornava
`0.0` e `1e+20` tornava `100000000000000000000` (intero). Il digest REGISTRATO al
salvataggio non corrispondeva più al documento riletto, e da lì il no-op canonico
(§8.18) smetteva di riconoscere un documento identico.

Questa suite fa tre cose diverse, e la prima è la più importante:

  1. **è l'ORACOLO della regola pura.** Per ogni valore di un corpus confronta la
     previsione di `json_numbers` con ciò che PostgreSQL fa davvero. Se i due
     dissentono su un solo valore, questo file è rosso — la regola non è
     un'approssimazione scritta a mano, è una previsione verificata;
  2. prova che il rifiuto arriva PRIMA di qualunque stato nel database, lock
     compreso;
  3. prova l'invariante sul verso positivo: per ogni documento valido, il digest
     registrato è uguale a quello ricalcolato dal documento RILETTO.

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import (
    Actor,
    DocumentRejectedError,
    InventoryRepository,
    canonical_sha256,
)
from app.inventory.document import NUMBER_NOT_ROUNDTRIPPABLE, strip_legacy_fields
from app.inventory.json_numbers import is_representable
from app.inventory.representable import walk_scalars

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")

LOC = "aaaaaaaa-0000-4000-8000-0000000000f1"
ROOM = "bbbbbbbb-0000-4000-8000-0000000000f1"
RACK = "cccccccc-0000-4000-8000-0000000000f1"
DEV = "dddddddd-0000-4000-8000-0000000000f1"

#: Il corpus dell'oracolo. Gli stessi valori della sonda, più i confini.
CORPUS = [
    # interi
    0, 1, -1, 42, -42, 2**31, 2**53, 2**63, 2**64, 10**30, -10**30, 10**100,
    # float ordinari
    0.0, 0.1, 0.4, -0.5, 10.0, -10.0, 3.141592653589793,
    0.30000000000000004, 123456789.12345679,
    # esponente negativo: la scala resta
    1e-7, 1e-9, 2.5e-05, 1e-100, 5e-324,
    # il confine di `repr`
    1000000000000000.0, 1234567890123456.0, 1e15, 1e16,
    # esponente positivo
    1e20, -1e20, 1.5e300,
    # il segno dello zero
    -0.0,
    # non finiti: `json.loads` li accetta, quindi ci si arriva
    float("inf"), float("-inf"), float("nan"),
]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational_num", "fixtures/relational/build.py")
build_expiry = _load("tsm_fixture_expiry_num",
                     "fixtures/expiry/build.py").build_inventory


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
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM photos"))
        # Le fixture referenziano due foto, e il salvataggio pretende che esistano
        # (§8.5): senza queste righe metà dei documenti verrebbe rifiutata per un
        # motivo che non c'entra con i numeri.
        for n, photo_id in enumerate((relbuild.FOTO_A, relbuild.FOTO_B)):
            payload = b"\x89PNG\r\n\x1a\n" + bytes([n])
            c.execute(text("""
                INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
                VALUES (:i, 'image/png', :b, :s, :n)
            """), {"i": photo_id, "b": payload, "n": len(payload),
                   "s": hashlib.sha256(payload).hexdigest()})
    yield engine


def document(**rack_over) -> dict:
    rack = {"_uid": RACK, "id": "R01", "name": "R01", "u": 45,
            "x": 0.5, "y": 1.25, "w": 0.6, "h": 0.65,
            "devices": [{"_uid": DEV, "id": "srv", "name": "srv", "u": 1}]}
    rack.update(rack_over)
    return {
        "schemaVersion": 1,
        "locations": [{"_uid": LOC, "id": "sito", "nome": "Sito", "sale": [
            {"_uid": ROOM, "id": "sala", "nome": "Sala", "w": 8.5, "h": 6.25,
             "vani": [], "racks": [rack]}]}],
    }


def state(engine) -> tuple[int, int, int | None]:
    """(versioni, righe di audit, versione in testa)."""
    with engine.begin() as c:
        return (
            c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one(),
            c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            c.execute(text("SELECT version FROM inventory_head "
                           "WHERE id IS TRUE")).scalar(),
        )


# ==================================================================
# 1. L'ORACOLO: la regola pura contro il database
# ==================================================================

def jsonb_round_trip(conn, value):
    """Il giro vero: json.dumps → jsonb → testo → json.loads.

    Restituisce `(sopravvive, com_è_tornato)`. Il confronto è sulla
    SERIALIZZAZIONE e non sul valore, perché è la serializzazione che entra nel
    digest: `-0.0 == 0.0` è vero, e `json.dumps` scrive due cose diverse.
    """
    payload = json.dumps({"x": value})
    try:
        got = conn.execute(text("SELECT CAST(:d AS jsonb)"), {"d": payload}).scalar_one()
    except Exception:
        conn.rollback()
        return False, "RIFIUTATO da PostgreSQL"
    back = got["x"] if isinstance(got, dict) else json.loads(got)["x"]
    return json.dumps(back) == json.dumps(value), repr(back)


@pytest.mark.parametrize("value", CORPUS, ids=repr)
def test_the_pure_rule_agrees_with_postgresql(db, engine, value):
    """⚠ Il test che rende la regola una previsione invece di un'ipotesi.

    La regola deve essere pura — gira prima di qualunque accesso al database — ma
    non deve essere indovinata. Qui il database è l'oracolo: se `json_numbers` dice
    «rappresentabile» e PostgreSQL non lo conserva, o viceversa, questo test è rosso.
    """
    with engine.connect() as c:
        survives, came_back = jsonb_round_trip(c, value)
        c.rollback()
    assert is_representable(value) is survives, (
        f"la regola dice {is_representable(value)}, PostgreSQL {survives}: "
        f"{value!r} torna come {came_back}")


def test_the_measured_failure_that_started_this(db, engine):
    """⚠ Regressione sul guasto esatto che ha esposto il problema.

    Trovato dal confronto dei digest della fase 2B: la ricostruzione della
    proiezione abortiva con `digest_della_versione_incoerente` su un documento con
    `1e+20` e `-0.0`, perché il digest registrato non corrispondeva più al documento
    riletto. Qui si riproduce il meccanismo dal basso — senza la proiezione — e poi
    si prova che oggi quel documento non entra più.
    """
    doc = document(x=1e20, y=-0.0)

    # Il meccanismo: com'era, e perché il digest cambiava.
    with engine.connect() as c:
        riletto = c.execute(text("SELECT CAST(:d AS jsonb)"),
                            {"d": json.dumps(doc)}).scalar_one()
        c.rollback()
    rack = riletto["locations"][0]["sale"][0]["racks"][0]
    assert rack["x"] == 100000000000000000000 and isinstance(rack["x"], int)
    assert json.dumps(rack["y"]) == "0.0"
    assert canonical_sha256(riletto) != canonical_sha256(doc), (
        "se JSONB avesse conservato questi numeri, questo commit non avrebbe "
        "ragione di esistere")

    # E oggi il documento viene rifiutato prima di diventare una versione.
    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError) as err:
            InventoryRepository(c).bootstrap(doc, ADMIN)
    codes = {d["code"] for d in err.value.details}
    assert codes == {NUMBER_NOT_ROUNDTRIPPABLE}
    paths = {d["path"] for d in err.value.details}
    assert paths == {"locations[0].sale[0].racks[0].x",
                     "locations[0].sale[0].racks[0].y"}
    assert state(engine) == (0, 0, None)


# ==================================================================
# 2. il rifiuto non lascia niente, e precede il lock
# ==================================================================

@pytest.mark.parametrize("value", [-0.0, 1e20, 1e16, 1.5e300,
                                   float("inf"), float("nan")], ids=repr)
def test_a_save_with_an_offending_number_leaves_no_state(db, engine, value):
    """Né versione, né audit, né testa spostata. Il documento in testa resta quello
    di prima, byte per byte."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    prima = state(engine)

    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError) as err:
            InventoryRepository(c).save(version, document(x=value), ADMIN)
    assert NUMBER_NOT_ROUNDTRIPPABLE in {d["code"] for d in err.value.details}
    assert state(engine) == prima

    with engine.begin() as c:
        assert canonical_sha256(InventoryRepository(c).get_current().doc) == \
            canonical_sha256(document())


@pytest.mark.parametrize("where,mutate", [
    ("campo ignoto", lambda d: d["locations"][0]["sale"][0]["racks"][0]
     .update({"campoNuovo": 1e20})),
    ("dentro una lista di scalari", lambda d: d["locations"][0]["sale"][0]
     ["racks"][0].update({"seriali": ["2006004084", -0.0]})),
    ("geometria della sala", lambda d: d["locations"][0]["sale"][0]
     .update({"w": 1e20})),
    ("vano", lambda d: d["locations"][0]["sale"][0]
     .update({"vani": [{"x": 0, "y": 0, "w": 1e20, "h": 3.0}]})),
    ("porta di un vano", lambda d: d["locations"][0]["sale"][0]
     .update({"vani": [{"x": 0, "y": 0, "w": 4.0, "h": 3.0,
                        "porta": {"lato": "top", "x": -0.0, "w": 0.9}}]})),
    ("dispositivo", lambda d: d["locations"][0]["sale"][0]["racks"][0]
     ["devices"][0].update({"pesoKg": 1e20})),
    ("voce di manuale", lambda d: d.update(
        {"manuale": [{"_uid": "eeeeeeee-0000-4000-8000-0000000000f1",
                      "id": "voce", "titolo": "Voce",
                      "blocchi": [{"testo": "x", "altezza": -0.0}]}]})),
])
def test_an_offending_number_is_refused_wherever_it_hides(db, engine, where, mutate):
    """Il modello delle entità è APERTO (§8.42): un valore ignoto può stare in
    qualunque ramo, compresi quelli che finirebbero in `extra`. Validare solo le
    colonne note lascerebbe fuori proprio i campi che non conosciamo ancora."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    prima = state(engine)

    doc = document()
    mutate(doc)
    assert any(not is_representable(v) for _p, _k, v in walk_scalars(doc)), (
        f"la mutazione «{where}» non ha inserito nessun numero offensivo: "
        "il test non proverebbe niente")

    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError):
            InventoryRepository(c).save(version, doc, ADMIN)
    assert state(engine) == prima


def test_the_rejection_does_not_even_wait_for_the_head_lock(db, engine):
    """⚠ La prova che la validazione precede il LOCK, non solo la scrittura.

    Si tiene bloccata la riga di testa da un'altra connessione e si prova a salvare
    un documento offensivo con `lock_timeout` molto breve. Se la validazione
    arrivasse dopo il lock, si otterrebbe un errore di lock scaduto; deve invece
    arrivare il rifiuto del documento, immediato.

    Serve perché «un documento rifiutato non lascia stato» e «un documento rifiutato
    non prende un lock» sono due affermazioni diverse: la prima è vera anche se la
    validazione arriva dopo, grazie al rollback.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version

    with engine.connect() as blocca:
        with blocca.begin():
            blocca.execute(text("SELECT version FROM inventory_head "
                                "WHERE id IS TRUE FOR UPDATE"))

            with engine.connect() as scrittore:
                with scrittore.begin():
                    scrittore.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    with pytest.raises(DocumentRejectedError) as err:
                        InventoryRepository(scrittore).save(
                            version, document(x=-0.0), ADMIN)
            assert NUMBER_NOT_ROUNDTRIPPABLE in {d["code"]
                                                 for d in err.value.details}

    # Controprova: con un documento VALIDO, lo stesso codice aspetta e scade. Se non
    # fosse così, il test sopra passerebbe anche senza che il lock sia mai stato preso.
    with engine.connect() as blocca:
        with blocca.begin():
            blocca.execute(text("SELECT version FROM inventory_head "
                                "WHERE id IS TRUE FOR UPDATE"))
            with engine.connect() as scrittore:
                with scrittore.begin():
                    scrittore.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    with pytest.raises(Exception) as err:
                        InventoryRepository(scrittore).save(
                            version, document(x=0.75), ADMIN)
            assert "lock" in str(err.value).lower()


def put_raw(engine, version: int, literal: str):
    """Un `PUT` con il numero scritto A MANO nel corpo JSON.

    Serve il corpo grezzo e non `json=`: `1e400` e `NaN` sono letterali che un
    client può mandare e che `json.dumps` non produrrebbe mai da un valore Python
    già arrivato.

    ⚠ `Content-Type` esplicito. Con `content=` httpx non lo imposta, il corpo non
    viene interpretato come JSON e la risposta è **422 `invalid_body`** — cioè lo
    stesso codice di stato del rifiuto che si sta cercando. La prima versione di
    questo test passava per quel motivo: asseriva 422 e stava provando che avevo
    dimenticato un'intestazione. Da qui il caso di CONTROLLO qui sotto.
    """
    from app.api.deps import get_connection, require_actor
    from app.main import app
    from conftest import ORIGIN, api_client

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    body = ('{"baseVersion": %d, "doc": %s}'
            % (version, json.dumps(document()).replace('"x": 0.5',
                                                       '"x": ' + literal)))
    assert literal in body, "la sostituzione nel corpo non ha funzionato"

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = \
        lambda: Actor(username="capo", role="admin")
    try:
        with api_client(app) as client:
            return client.put(
                "/api/inventory",
                headers={**ORIGIN, "Content-Type": "application/json"},
                content=body)
    finally:
        app.dependency_overrides.clear()


def test_the_raw_body_control_case_is_accepted(db, engine):
    """⚠ Il controllo che rende leggibile il test successivo.

    Lo stesso corpo, con un numero normale, deve essere ACCETTATO. Senza questo, un
    422 dovuto al corpo malformato sarebbe indistinguibile dal 422 del rifiuto.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    r = put_raw(engine, version, "0.5")
    assert r.status_code == 200, r.json()
    assert r.json()["changed"] is False


@pytest.mark.parametrize("literal", ["-0.0", "1e20", "1e400", "NaN", "-Infinity"])
def test_the_route_answers_422_with_the_stable_code(db, engine, literal):
    """Dal lato del client, compresi i letterali che solo un corpo grezzo produce.

    `1e400` e `NaN` **arrivano davvero** fino al validatore: `json.loads` li accetta
    e li converte in `inf`/`nan` invece di rifiutarli — misurato, non supposto.
    Prima di questo commit quel documento passava la validazione e faceva fallire
    l'`INSERT`, cioè un 500 al momento della scrittura invece di un errore di
    validazione.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version

    r = put_raw(engine, version, literal)

    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "document_rejected"
    problems = body["problems"]
    assert {p["code"] for p in problems} == {NUMBER_NOT_ROUNDTRIPPABLE}
    assert problems[0]["path"] == "locations[0].sale[0].racks[0].x"
    # Il documento inviato non torna indietro: solo codice e percorso.
    testo = json.dumps(body, ensure_ascii=False)
    assert "R01" not in testo and "Sala" not in testo and literal not in testo
    assert state(engine) == (1, 1, version)


# ==================================================================
# 3. l'invariante sul verso positivo
# ==================================================================

@pytest.mark.parametrize("value", [v for v in CORPUS if is_representable(v)],
                         ids=repr)
def test_a_representable_number_survives_a_real_save(db, engine, value):
    """Non over-correggere: ogni valore che la regola ammette deve continuare a
    funzionare, e il digest registrato deve valere anche dopo la rilettura."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(x=value), ADMIN).version
    with engine.begin() as c:
        row = c.execute(text("SELECT doc, canonical_sha256 FROM inventory_versions "
                             "WHERE version = :v"), {"v": version}).one()
    riletto, registrato = row[0], row[1]
    assert canonical_sha256(riletto) == registrato
    assert json.dumps(riletto["locations"][0]["sale"][0]["racks"][0]["x"]) == \
        json.dumps(value)


#: Le fixture della fase 2A più il seed di produzione e le scadenze. Manca
#: `jsonb-hostile-numbers`, che questo commit ha reso non salvabile: è un documento
#: che descrive lo stato *precedente*, e resta a documentare la storia.
DOCUMENTS = {k: v for k, v in relbuild.documents().items()
             if k != "jsonb-hostile-numbers"}
DOCUMENTS["seed"] = strip_legacy_fields(
    json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
DOCUMENTS["expiry"] = build_expiry(date(2026, 8, 10))
NAMES = sorted(DOCUMENTS)


@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_the_recorded_digest_survives_the_snapshot_store(db, engine, name):
    """⚠ L'invariante del magazzino, su ventitré documenti.

        digest registrato al salvataggio == digest ricalcolato dal documento riletto

    È l'affermazione che il repository dava per vera e che non lo era. Vale per il
    documento intero, non solo per i numeri: se JSONB cambiasse qualunque cosa —
    l'ordine delle chiavi non conta per il digest, ma un tipo sì — questo test lo
    vedrebbe.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(DOCUMENTS[name], ADMIN).version
    with engine.begin() as c:
        row = c.execute(text("SELECT doc, canonical_sha256 FROM inventory_versions "
                             "WHERE version = :v"), {"v": version}).one()
    assert canonical_sha256(row[0]) == row[1]


@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_saving_the_document_read_back_is_a_no_op(db, engine, name):
    """La conseguenza pratica dell'invariante, e il motivo per cui conta (§8.18).

    Si rilegge il documento dal database e si prova a salvarlo così com'è: deve
    essere riconosciuto come no-op canonico. Con un digest che non sopravvive alla
    rilettura, questo salvataggio creerebbe una versione nuova con un contenuto che
    nessuno ha cambiato — e il registro attribuirebbe all'utente una modifica fatta
    da PostgreSQL.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(DOCUMENTS[name], ADMIN).version
    with engine.begin() as c:
        repo = InventoryRepository(c)
        riletto = repo.get_current().doc
        result = repo.save(version, riletto, ADMIN)
    assert result.created is False, "un documento riletto non è una modifica"
    assert result.version == version
    assert state(engine)[0] == 1
