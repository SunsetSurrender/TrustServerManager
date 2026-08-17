"""L'invariante del magazzino per il TESTO, contro PostgreSQL vero.

    Ogni VALORE e ogni CHIAVE accettati dal `PUT` normale devono essere
    rappresentabili senza perdite da PostgreSQL JSONB, secondo la semantica del
    digest canonico del repository.

Gemella di `test_snapshot_numbers_pg.py`, e il guasto NON è lo stesso: con i numeri
PostgreSQL cambiava il valore in silenzio (fedeltà), con il testo lo RIFIUTA
(l'`INSERT` non riesce). Senza validazione l'errore arriva dal database a metà del
salvataggio, cioè un **500** invece di un 422 — l'utente vede «errore del server» per
un carattere in un nome che potrebbe correggere.

Quattro cose, e la prima è la più importante:

  1. **è l'ORACOLO della regola pura.** Ogni stringa del corpus si prova sia come
     VALORE sia come CHIAVE e si confronta la previsione con ciò che PostgreSQL fa
     davvero. Se dissentono su una sola, questo file è rosso;
  2. il rifiuto precede il lock della testa e non lascia niente nel database;
  3. i documenti Unicode validi si salvano, si rileggono identici e il loro digest
     registrato regge;
  4. jsonb riordina le chiavi e collassa i duplicati, e **non è un problema**: il
     digest canonico ordina le chiavi. Distinguere «torna diverso» da «torna con un
     significato diverso» è parte dell'invariante.

⚠ Ogni carattere invisibile è scritto con una sequenza di ESCAPE, mai digitato: nella
prima versione della sonda il corpus conteneva `"ab"` dove doveva esserci un carattere
di controllo, cioè non provava niente e sembrava verde.

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import (
    Actor,
    DocumentRejectedError,
    InventoryRepository,
    canonical_sha256,
)
from app.inventory.document import STRING_NOT_ROUNDTRIPPABLE, strip_legacy_fields
from app.inventory.json_strings import is_representable_text
from app.inventory.representable import key_segment, walk_scalars

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")

LOC = "aaaaaaaa-0000-4000-8000-0000000000e1"
ROOM = "bbbbbbbb-0000-4000-8000-0000000000e1"
RACK = "cccccccc-0000-4000-8000-0000000000e1"
DEV = "dddddddd-0000-4000-8000-0000000000e1"
MAN = "eeeeeeee-0000-4000-8000-0000000000e1"

#: Il corpus. Gli stessi casi della suite pura, qui verificati contro il database.
CORPUS = [
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
    ("NUL in mezzo", "a\u0000b"),
    ("NUL da solo", "\u0000"),
    ("NUL in coda", "ab\u0000"),
    ("surrogato alto spaiato", "a\ud800b"),
    ("surrogato basso spaiato", "a\udc00b"),
    ("surrogato alto da solo", "\udbff"),
]

ROTTI = [("NUL", "a\u0000b"), ("surrogato", "a\ud800b")]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational_txt", "fixtures/relational/build.py")


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
        for n, photo_id in enumerate((relbuild.FOTO_A, relbuild.FOTO_B)):
            payload = b"\x89PNG\r\n\x1a\n" + bytes([n])
            c.execute(text("""
                INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
                VALUES (:i, 'image/png', :b, :s, :n)
            """), {"i": photo_id, "b": payload, "n": len(payload),
                   "s": hashlib.sha256(payload).hexdigest()})
    yield engine


def document(**over) -> dict:
    """Documento valido con testo ITALIANO realistico, non ASCII."""
    doc = {
        "schemaVersion": 1,
        "locations": [{
            "_uid": LOC, "id": "pomezia", "nome": "Pomezia G0 — Città",
            "sale": [{
                "_uid": ROOM, "id": "sala-1", "nome": "Sala 1 (già CED)",
                "w": 8.5, "h": 6.25, "area": "53.13 m²",
                "vani": [{"x": 0, "y": 0, "w": 4.25, "h": 6.25,
                          "porta": {"lato": "bottom", "x": 0.35, "w": 0.84},
                          "porta2": {"lato": "top", "x": 2.0, "w": 1.1}}],
                "racks": [{
                    "_uid": RACK, "id": "R01", "name": "Rack perimetrale",
                    "u": 45, "x": 0.5, "y": 1.25, "w": 0.6, "h": 0.65,
                    "seriali": ["2006004084", "SN-À-01"],
                    "devices": [{
                        "_uid": DEV, "id": "srv-01",
                        "name": "srv-01 «produzione»",
                        "note": "Verificare l'alimentazione — cavo già sostituito",
                    }],
                }],
            }],
        }],
        "manuale": [{
            "_uid": MAN, "id": "procedura", "titolo": "Procedura di spegnimento",
            "blocchi": [{"testo": "Sequenza: UPS → rack → climatizzazione"}],
        }],
    }
    doc["locations"][0]["sale"][0]["racks"][0].update(over)
    return doc


def state(engine) -> tuple[int, int, int | None]:
    with engine.begin() as c:
        return (
            c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one(),
            c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            c.execute(text("SELECT version FROM inventory_head "
                           "WHERE id IS TRUE")).scalar(),
        )


# ==================================================================
# 1. L'ORACOLO: la regola pura contro il database, valori E chiavi
# ==================================================================

def jsonb_round_trip(conn, payload_obj):
    """Il giro vero, sul percorso dell'applicazione: `json.dumps(ensure_ascii=False)`
    poi legatura come `str` (psycopg codifica in UTF-8), poi rilettura.

    Restituisce `(sopravvive, perché no)`. Il confronto è sulla SERIALIZZAZIONE.
    """
    try:
        payload = json.dumps(payload_obj, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return False, f"json.dumps: {type(exc).__name__}"
    try:
        got = conn.execute(text("SELECT CAST(:d AS jsonb)"),
                           {"d": payload}).scalar_one()
    except Exception as exc:
        conn.rollback()
        return False, type(exc).__name__
    back = got if isinstance(got, (dict, list)) else json.loads(got)
    same = (json.dumps(back, ensure_ascii=False, sort_keys=True)
            == json.dumps(payload_obj, ensure_ascii=False, sort_keys=True))
    return same, "" if same else f"tornato diverso: {back!r}"[:60]


@pytest.mark.parametrize("etichetta,value", CORPUS, ids=[e for e, _v in CORPUS])
@pytest.mark.parametrize("posizione", ["valore", "chiave"])
def test_the_pure_rule_agrees_with_postgresql(db, engine, etichetta, value,
                                              posizione):
    """⚠ Il test che rende la regola una previsione invece di un'ipotesi.

    Le CHIAVI si provano come i valori: sono dati dell'utente, il modello è aperto
    (§8.42) e una chiave ignota sopravvive al salvataggio.
    """
    payload = {"k": value} if posizione == "valore" else {value: "v"}
    with engine.connect() as c:
        survives, perche = jsonb_round_trip(c, payload)
        c.rollback()
    assert is_representable_text(value) is survives, (
        f"{etichetta} come {posizione}: la regola dice "
        f"{is_representable_text(value)}, PostgreSQL {survives} ({perche})")


def test_postgresql_does_not_normalise_unicode(db, engine):
    """⚠ Se normalizzasse, sarebbe una modifica silenziosa del documento — la stessa
    classe di guasto dei numeri, e la regola non basterebbe a coprirla.

    Una `e` più un accento combinante deve tornare in DUE code point, non precomposta.
    """
    nfd = "é"
    assert len(nfd) == 2
    with engine.connect() as c:
        got = c.execute(text("SELECT CAST(:d AS jsonb)"),
                        {"d": json.dumps({"k": nfd}, ensure_ascii=False)}).scalar_one()
        c.rollback()
    tornato = (got if isinstance(got, dict) else json.loads(got))["k"]
    assert tornato == nfd and len(tornato) == 2


def test_reordered_keys_are_not_a_fidelity_problem(db, engine):
    """jsonb riordina le chiavi e collassa i duplicati. Non rompe l'invariante,
    perché il digest canonico ordina le chiavi (§8.14) — e un oggetto JSON con chiavi
    duplicate è già collassato dal parser di Python prima di arrivare qui.

    Vale la pena fissarlo: «il documento torna diverso» e «il documento torna con un
    significato diverso» sono due cose distinte, e solo la seconda è un guasto.
    """
    doc = document()
    version = None
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(doc, ADMIN).version
    with engine.begin() as c:
        row = c.execute(text("SELECT doc, canonical_sha256 FROM inventory_versions "
                             "WHERE version = :v"), {"v": version}).one()
    riletto, registrato = row[0], row[1]
    assert canonical_sha256(riletto) == registrato
    # Le chiavi possono tornare in un ordine diverso...
    assert list(riletto["locations"][0]) != []
    # ...e il documento resta lo stesso documento.
    assert canonical_sha256(riletto) == canonical_sha256(doc)


# ==================================================================
# 2. il rifiuto non lascia niente, e precede il lock
# ==================================================================

@pytest.mark.parametrize("etichetta,rotto", ROTTI, ids=[e for e, _v in ROTTI])
def test_a_save_with_broken_text_leaves_no_state(db, engine, etichetta, rotto):
    """Né versione, né audit, né testa spostata, né istantanea modificata."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    prima = state(engine)

    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError) as err:
            InventoryRepository(c).save(version, document(name=rotto), ADMIN)
    assert STRING_NOT_ROUNDTRIPPABLE in {d["code"] for d in err.value.details}
    assert state(engine) == prima

    # L'istantanea esistente non è stata toccata.
    with engine.begin() as c:
        assert canonical_sha256(InventoryRepository(c).get_current().doc) == \
            canonical_sha256(document())
    # E niente è finito nella proiezione (che questo commit non tocca).
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_racks")).scalar_one() == 0


@pytest.mark.parametrize("etichetta,rotto", ROTTI, ids=[e for e, _v in ROTTI])
def test_a_broken_object_key_is_refused_too(db, engine, etichetta, rotto):
    """Una chiave ignota sopravvive al salvataggio e finirebbe in `extra` (§8.42):
    se non è scrivibile, il documento non è salvabile."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    prima = state(engine)

    doc = document()
    doc["locations"][0]["sale"][0]["racks"][0][rotto] = "valore innocuo"
    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError) as err:
            InventoryRepository(c).save(version, doc, ADMIN)

    problemi = [d for d in err.value.details
                if d["code"] == STRING_NOT_ROUNDTRIPPABLE]
    assert problemi, err.value.details
    # Il percorso indica il genitore e la POSIZIONE della chiave, non la chiave.
    assert problemi[0]["path"].startswith("locations[0].sale[0].racks[0].<chiave n.")
    assert rotto not in json.dumps(problemi, ensure_ascii=False)
    assert state(engine) == prima


def test_the_rejection_does_not_even_wait_for_the_head_lock(db, engine):
    """⚠ La validazione precede il LOCK, non solo la scrittura.

    Si tiene bloccata la riga di testa da un'altra connessione e si salva un documento
    con testo rotto sotto `lock_timeout` breve: deve arrivare il rifiuto, immediato. Se
    la validazione stesse dopo il lock si otterrebbe un errore di lock scaduto.

    «Non lascia stato» e «non prende un lock» sono due affermazioni diverse: la prima è
    vera anche se la validazione arriva dopo, grazie al rollback.
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
                            version, document(name="a\u0000b"), ADMIN)
            assert STRING_NOT_ROUNDTRIPPABLE in {d["code"]
                                                 for d in err.value.details}

    # Controprova: con un documento VALIDO lo stesso codice aspetta il lock e scade.
    # Senza di essa il test sopra passerebbe anche se il lock non fosse mai stato preso.
    with engine.connect() as blocca:
        with blocca.begin():
            blocca.execute(text("SELECT version FROM inventory_head "
                                "WHERE id IS TRUE FOR UPDATE"))
            with engine.connect() as scrittore:
                with scrittore.begin():
                    scrittore.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    with pytest.raises(Exception) as err:
                        InventoryRepository(scrittore).save(
                            version, document(name="Rack rinominato — già in uso"),
                            ADMIN)
            assert "lock" in str(err.value).lower()


def put_raw(engine, version: int, literal: str):
    """Un `PUT` con la stringa scritta A MANO nel corpo JSON.

    Serve il corpo grezzo: `"\\u0000"` e `"\\ud800"` sono letterali che un client può
    mandare e che `json.dumps` non produrrebbe mai da un valore già arrivato.

    ⚠ `Content-Type` esplicito: con `content=` httpx non lo imposta, il corpo non viene
    interpretato come JSON e la risposta è 422 `invalid_body` — lo stesso codice di
    stato del rifiuto che si sta cercando. Da qui il caso di controllo qui sotto.
    """
    from app.api.deps import get_connection, require_actor
    from app.main import app
    from conftest import ORIGIN, api_client

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    corpo = json.dumps(document(), ensure_ascii=True).replace(
        '"name": "Rack perimetrale"', '"name": "%s"' % literal)
    assert literal in corpo, "la sostituzione nel corpo non ha funzionato"
    body = '{"baseVersion": %d, "doc": %s}' % (version, corpo)

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = \
        lambda: Actor(username="capo", role="admin")
    try:
        with api_client(app) as client:
            return client.put(
                "/api/inventory",
                headers={**ORIGIN, "Content-Type": "application/json"},
                content=body.encode("utf-8"))
    finally:
        app.dependency_overrides.clear()


def test_the_raw_body_control_case_is_accepted(db, engine):
    """Il controllo che rende leggibile il test successivo: lo stesso corpo, con testo
    normale, deve essere ACCETTATO."""
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version
    r = put_raw(engine, version, "Rack perimetrale")
    assert r.status_code == 200, r.json()
    assert r.json()["changed"] is False


@pytest.mark.parametrize("literal", ["a\\u0000b", "a\\ud800b", "\\udbff",
                                     "Sala\\u0000 1"])
def test_the_route_answers_422_with_the_stable_code(db, engine, literal):
    """Dal lato del client: i letterali di escape che solo un corpo grezzo produce.

    `json.loads` li accetta senza protestare — un surrogato spaiato diventa una `str`
    Python che non è codificabile in UTF-8 — quindi ci si arriva davvero, e prima di
    questo commit il rifiuto arrivava dal database come un 500.
    """
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(document(), ADMIN).version

    r = put_raw(engine, version, literal)

    assert r.status_code == 422, r.json()
    body = r.json()["detail"]
    assert body["code"] == "document_rejected"
    problemi = body["problems"]
    assert {p["code"] for p in problemi} == {STRING_NOT_ROUNDTRIPPABLE}
    assert problemi[0]["path"] == "locations[0].sale[0].racks[0].name"
    # Il valore non torna indietro, e nemmeno il resto del documento.
    testo = json.dumps(body, ensure_ascii=False)
    assert "Rack" not in testo and "Pomezia" not in testo
    assert state(engine) == (1, 1, version)


# ==================================================================
# 3. i documenti Unicode validi funzionano
# ==================================================================

@pytest.mark.parametrize("etichetta,value",
                         [(e, v) for e, v in CORPUS if is_representable_text(v)],
                         ids=[e for e, v in CORPUS if is_representable_text(v)])
def test_a_representable_string_survives_a_real_save(db, engine, etichetta, value):
    """Non over-correggere: ogni stringa che la regola ammette deve continuare a
    funzionare, come valore E come chiave, e il digest registrato deve valere anche
    dopo la rilettura."""
    doc = document(name=value)
    doc["locations"][0]["sale"][0]["racks"][0][value or "vuota"] = value

    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(doc, ADMIN).version
    with engine.begin() as c:
        row = c.execute(text("SELECT doc, canonical_sha256 FROM inventory_versions "
                             "WHERE version = :v"), {"v": version}).one()
    riletto, registrato = row[0], row[1]

    assert canonical_sha256(riletto) == registrato
    assert riletto["locations"][0]["sale"][0]["racks"][0]["name"] == value
    assert canonical_sha256(riletto) == canonical_sha256(doc)


def test_a_realistic_italian_document_is_a_no_op_when_saved_back(db, engine):
    """La conseguenza pratica dell'invariante (§8.18): il documento riletto dal
    database non è una modifica. Con accenti, emoji e trattini tipografici."""
    doc = document(name="Rack «perimetrale» — 2ª fila 🚀")
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(doc, ADMIN).version
    with engine.begin() as c:
        repo = InventoryRepository(c)
        riletto = repo.get_current().doc
        result = repo.save(version, riletto, ADMIN)
    assert result.created is False
    assert result.version == version
    assert state(engine)[0] == 1


def test_the_production_seed_still_saves(db, engine):
    """Il documento vero, pieno di accenti: se il rifiuto lo colpisse, questo commit
    avrebbe reso non salvabile l'inventario del cliente."""
    seed = strip_legacy_fields(json.loads(
        (ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(seed, ADMIN).version
    with engine.begin() as c:
        row = c.execute(text("SELECT doc, canonical_sha256 FROM inventory_versions "
                             "WHERE version = :v"), {"v": version}).one()
    assert canonical_sha256(row[0]) == row[1]


# ==================================================================
# 4. non esistono dati storici con testo rotto, e si può dimostrare
# ==================================================================

@pytest.mark.parametrize("etichetta,rotto", ROTTI, ids=[e for e, _v in ROTTI])
def test_broken_text_could_never_have_been_stored_at_all(db, engine, etichetta,
                                                         rotto):
    """⚠ La differenza con i numeri, provata.

    I numeri non rappresentabili **sono** potuti entrare prima della correzione:
    PostgreSQL li accettava cambiandoli, e le versioni scritte allora restano (la
    ricostruzione della proiezione le diagnostica). Per il testo no: PostgreSQL
    rifiuta, quindi non esiste e non può esistere una versione storica con una stringa
    così — nemmeno inserendola direttamente come proprietario dello schema.

    Ne segue che il controllo sul modello relazionale non è raggiungibile dalla testa,
    ed è la ragione per cui esiste comunque: `validate_model` gira anche su modelli che
    non vengono dal database.
    """
    doc = document(name=rotto)
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text("""
                INSERT INTO inventory_versions
                       (doc, canonical_sha256, actor_username, actor_role)
                VALUES (CAST(:doc AS jsonb), :sha, 'capo', 'admin')
            """), {"doc": json.dumps(doc, ensure_ascii=False),
                   "sha": canonical_sha256(doc)})
        c.rollback()
    motivo = str(err.value).lower()
    assert ("unsupported unicode" in motivo or "codec can't encode" in motivo
            or "surrogates not allowed" in motivo), motivo
    assert state(engine) == (0, 0, None)


def test_the_walk_and_the_paths_agree_with_what_the_repository_reports(db, engine):
    """La visita pura e ciò che il repository riporta devono indicare lo stesso campo:
    un percorso che non corrisponde a niente è un errore che non si può usare."""
    doc = document()
    doc["locations"][0]["sale"][0]["vani"][0]["porta2"]["lato"] = "a\u0000b"

    with engine.begin() as c:
        with pytest.raises(DocumentRejectedError) as err:
            InventoryRepository(c).bootstrap(doc, ADMIN)
    percorso = [d["path"] for d in err.value.details
                if d["code"] == STRING_NOT_ROUNDTRIPPABLE][0]
    assert percorso == "locations[0].sale[0].vani[0].porta2.lato"

    # E lo stesso percorso si ritrova nella visita, sullo stesso valore.
    trovati = {p: v for p, kind, v in walk_scalars(doc) if kind == "value"}
    assert trovati[percorso] == "a\u0000b"
    assert key_segment(0) == "<chiave n.1>"
