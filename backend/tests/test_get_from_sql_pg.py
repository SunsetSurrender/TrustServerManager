"""Fase 2D: `GET /api/inventory` restituisce ciò che le TABELLE contengono.

PostgreSQL vero, e qui non è nemmeno discutibile: metà di ciò che questo file deve
dimostrare è comportamento del database — l'isolamento `REPEATABLE READ`, il rifiuto
di una scrittura in `READ ONLY`, la coerenza di uno snapshot mentre un'altra
transazione committa. Nessun doppio proverebbe niente di tutto questo.

L'invariante di OGNI `GET` riuscito, ed è il motivo per cui questo file esiste:

    digest(risposta.doc) == risposta.sha256
                         == projection_state.head_sha256
                         == inventory_versions.canonical_sha256 (testa)

    risposta.version     == inventory_head.version
                         == projection_state.head_version

tutto osservato dentro **un solo** istante del database.

Che cosa c'è qui e non altrove
------------------------------
`test_relational_mapper.py` prova la mappa pura, `test_projection_pg.py` la
ricostruzione, `test_dual_write_pg.py` che ogni scrittura mantiene le due
rappresentazioni. Questo prova la LETTURA: che il documento servito venga dalle
tabelle e non dall'istantanea, che una proiezione non attuale o incoerente faccia
rifiutare la risposta invece di ripiegare sul JSON, e che un `PUT` concorrente non
possa far uscire un documento fatto di due versioni.

Le due famiglie che valgono di più
----------------------------------
1. **La corruzione manuale.** Si modificano le tabelle come farebbe un DBA con le
   mani in pasta, e si pretende che il `GET` se ne accorga. Compreso il caso a cui
   il digest è CIECO: una colonna data derivata sbagliata lascia il documento
   identico byte per byte, e solo `validate_model` la vede. È il punto cieco trovato
   in fase 2B, e questo è il posto dove si chiude anche in lettura.

2. **La concorrenza.** Un `GET` che legge la testa e poi le righe è una lettura in
   sette pezzi. Sotto READ COMMITTED sarebbe corretta quasi sempre e sbaglierebbe
   ogni volta che un `PUT` committa nel mezzo — cioè il modo peggiore di sbagliare.
   Il test che ferma il `GET` a metà e fa committare un altro utente è, di fatto, la
   mutazione dell'isolamento: se l'opzione non avesse effetto, quel test è rosso.

Riferimento: BACKEND-PLAN.md §8.45.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.api.deps import get_connection, require_actor, snapshot_connection
from app.identity import canonicalise
from app.inventory import Actor, InventoryRepository, canonical_sha256
from app.inventory import projection
from app.inventory.document import strip_legacy_fields
from app.inventory.relational import MAPPER_VERSION, assemble, derived_names
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402  (client HTTPS: vedi conftest)

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")

#: Digest sintatticamente plausibile e inventato: 64 esadecimali che non sono il
#: digest di nulla. Serve a distinguere «il confronto non torna» da «il confronto non
#: viene fatto», e un valore non plausibile lo confonderebbe con un errore di tipo.
DIGEST_FINTO = "0" * 64


def _load(name: str, relative: str):
    """Per PERCORSO e con un nome proprio: i generatori si chiamano tutti `build.py`
    e `sys.modules` è condiviso da tutta la sessione di pytest, quindi un `import
    build` restituirebbe quello che un altro file ha già caricato."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational_get", "fixtures/relational/build.py")
build_expiry = _load("tsm_fixture_expiry_get",
                     "fixtures/expiry/build.py").build_inventory

TODAY = date(2026, 8, 10)

DOCUMENTS = dict(relbuild.documents())
DOCUMENTS["seed"] = strip_legacy_fields(
    json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
DOCUMENTS["expiry"] = build_expiry(TODAY)

#: Escluso, con un test proprio che spiega perché: contiene `1e+20` e `-0.0`, che il
#: magazzino delle istantanee rifiuta (§8.16), quindi non arriva mai a essere una
#: versione da servire. Elenco esplicito e non uno `skip` silenzioso: un test saltato
#: somiglia troppo a un test passato.
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
    """Database pulito, e le foto che le fixture referenziano.

    Le foto si cancellano DOPO i rack: `inventory_racks.photo_id` le protegge, ed è
    la stessa chiave esterna che impedisce alla GC di portare via la foto che lo
    stato corrente sta usando.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM inventory_projection_state"))
        c.execute(text("DELETE FROM photos"))
        c.execute(text("DELETE FROM maintenance_runs"))
        for n, photo_id in enumerate((relbuild.FOTO_A, relbuild.FOTO_B)):
            payload = b"\x89PNG\r\n\x1a\n" + bytes([n])
            c.execute(text("""
                INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
                VALUES (:i, 'image/png', :b, :s, :n)
            """), {"i": photo_id, "b": payload, "n": len(payload),
                   "s": hashlib.sha256(payload).hexdigest()})
    yield engine


@pytest.fixture
def client(engine):
    """Client HTTPS con un attore admin.

    ⚠ `get_snapshot_reader` NON viene sostituito, di proposito. È la fabbrica dello
    snapshot vero, quindi ogni `GET` di questo file gira davvero in
    `REPEATABLE READ, READ ONLY` su una connessione presa dall'engine
    dell'applicazione. Sostituirlo con la connessione del test renderebbe verdi i
    test di concorrenza senza che l'isolamento sia mai stato esercitato — cioè li
    trasformerebbe in test che passano per il motivo sbagliato.

    Perché funziona: `app.db.get_engine()` legge `TSM_DB_URL`, che è la stessa che
    usa questa suite. Le fixture committano, quindi lo snapshot le vede.
    """
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = lambda: ADMIN
    with api_client(app) as c:
        yield c
    app.dependency_overrides.clear()


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


def declared(engine) -> dict | None:
    with engine.begin() as c:
        row = c.execute(text("SELECT head_version, head_sha256, mapper_version "
                             "  FROM inventory_projection_state")).mappings().first()
    return dict(row) if row else None


def sql(engine, statement: str, **params) -> None:
    """Una scrittura da PROPRIETARIO dello schema: è così che si simula il DBA.

    L'API non potrebbe fare parecchie di queste cose (non ha `UPDATE` su
    `inventory_versions`, per esempio), e va bene: la corruzione che questi test
    provano a scoprire non arriva dall'API — se ci arrivasse, la fase 2C avrebbe già
    fallito. Arriva da fuori.
    """
    with engine.begin() as c:
        c.execute(text(statement), params)


# ==================================================================
# l'invariante, in un posto solo
# ==================================================================

def assert_get_invariant(engine, response) -> dict:
    """Tutto ciò che deve valere per un `GET` riuscito. Restituisce il corpo.

    In una funzione sola perché è ciò che rende impossibile scrivere un test nuovo
    che verifica tre quarti dell'invariante e dimentica il quarto — e il quarto
    dimenticato è sempre quello che avrebbe trovato il difetto.
    """
    assert response.status_code == 200, response.text
    body = response.json()

    # --- il contratto HTTP, invariato (§8.22) ---
    assert set(body) == {"version", "schemaVersion", "sha256", "doc"}, \
        "la fase 2D non deve aggiungere né togliere una chiave"
    assert response.headers["cache-control"] == "no-store"

    version, recorded, immutable = head_of(engine)
    state = declared(engine)

    # --- i quattro digest sono lo stesso digest ---
    assert body["version"] == version
    assert body["sha256"] == recorded
    assert canonical_sha256(body["doc"]) == recorded, \
        "il documento servito non ha il digest della versione in testa"
    assert state is not None and state["head_sha256"] == recorded
    assert state["head_version"] == version
    assert state["mapper_version"] == MAPPER_VERSION

    # --- e il documento è, byte per byte, quello dell'istantanea ---
    #
    # L'istantanea non è la FONTE (il test qui sotto lo dimostra corrompendola), ma
    # resta il giudice: se il riassemblaggio da SQL divergesse, la fase 2 non avrebbe
    # più senso.
    assert body["doc"] == canonicalise(immutable)

    # --- nessuna colonna DERIVATA è uscita nel documento ---
    #
    # `garanzia_date` e `supporto_date` esistono per interrogare, non per essere
    # restituite: sono una derivata del testo, e il frontend non le ha mai viste.
    # L'elenco si legge da `DERIVED`, non si riscrive a mano, altrimenti una colonna
    # derivata aggiunta domani sfuggirebbe a questo controllo.
    derivate = set()
    for kind in ("location", "room", "rack", "device", "manual"):
        derivate.update(derived_names(kind))
    assert derivate, "nessuna colonna derivata: il controllo sarebbe vacuo"
    trovate = [k for k in _all_keys(body["doc"]) if k in derivate]
    assert not trovate, f"colonne derivate uscite nel documento: {trovate}"

    return body


def _all_keys(value) -> set[str]:
    """Ogni chiave, a ogni profondità."""
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            out |= _all_keys(v)
    elif isinstance(value, list):
        for v in value:
            out |= _all_keys(v)
    return out


# ==================================================================
# 1. il meccanismo: lo snapshot è quello che diciamo che è
# ==================================================================
#
# Prima di ogni test di comportamento, la domanda al database. Se `isolation_level`
# o `postgresql_readonly` fossero ignorati dal dialetto psycopg3 — sono opzioni di
# SQLAlchemy, e psycopg3 è un dialetto diverso da psycopg2 — tutto il resto di questo
# file passerebbe quasi sempre e sbaglierebbe sotto carico.

def test_the_read_snapshot_is_repeatable_read_and_read_only(db, engine):
    """Non si assume: si CHIEDE a PostgreSQL, che è l'unica autorità."""
    from app import db as app_db
    app_db.get_engine()      # forza la creazione dell'engine dell'applicazione

    with snapshot_connection() as conn:
        assert conn.execute(text("SHOW transaction_isolation")).scalar_one() \
            == "repeatable read"
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"


def test_the_read_snapshot_cannot_write_even_by_mistake(db, engine):
    """`READ ONLY` non è decorativo: è il database a rifiutare.

    Un difetto futuro che provasse a scrivere mentre serve una lettura verrebbe
    fermato qui, non dalle buone intenzioni di chi scrive il codice.
    """
    import psycopg

    with snapshot_connection() as conn:
        with pytest.raises(Exception) as caught:
            conn.execute(text("UPDATE inventory_head SET version = version"))
    assert isinstance(caught.value.orig, psycopg.errors.ReadOnlySqlTransaction), \
        f"rifiutata, ma per il motivo sbagliato: {caught.value!r}"


def test_the_read_pool_is_at_least_as_large_as_the_request_pool(db, engine):
    """L'invariante che impedisce lo stallo, in una riga.

    Un `GET` tiene due connessioni insieme. I portatori della prima sono al massimo
    quanti la capienza del pool delle richieste; se il pool di lettura ne avesse
    meno, qualcuno di loro non otterrebbe la seconda e resterebbe in attesa di
    qualcuno che è già in attesa.

    ⚠ `_max_overflow` è privato in SQLAlchemy e non c'è un modo pubblico di leggerlo.
    Si accetta la fragilità: un `AttributeError` qui è un test da aggiornare, mentre
    un pool di lettura rimpicciolito «perché legge poco» sarebbe uno stallo in
    produzione sotto carico, dove non si riproduce a mano.
    """
    from app.db import get_engine, get_read_engine

    def capienza(e):
        return e.pool.size() + getattr(e.pool, "_max_overflow", 0)

    assert get_read_engine() is not get_engine(), \
        "un pool solo: un GET aspetterebbe sé stesso"
    assert capienza(get_read_engine()) >= capienza(get_engine()), \
        f"lettura {capienza(get_read_engine())} < richieste {capienza(get_engine())}"


def test_a_single_pool_would_deadlock_a_get(db, engine, monkeypatch):
    """⚠ Lo stallo si DIMOSTRA, non si argomenta.

    Si riducono entrambi i pool a **una** connessione. Con due pool distinti un `GET`
    riesce: prende la prima per autenticarsi e la seconda per lo snapshot. Con un pool
    solo — facendo puntare il pool di lettura allo stesso engine — lo stesso `GET` non
    può finire: la connessione che gli serve è quella che sta già tenendo.

    Basta **un** `GET` per farlo vedere, perché il rapporto che conta è «due
    connessioni per richiesta contro la capienza del pool», non il numero di utenti. In
    produzione la stessa cosa succede a quindici `GET` simultanei con la capienza
    predefinita (5 + 10), e a quel punto si presenta come trenta secondi di silenzio
    sotto carico — cioè il guasto che non si riproduce a mano.

    ⚠ Qui serve una SESSIONE VERA, e la prima stesura non l'aveva: con
    `require_actor` sostituito da una lambda, `get_connection` non viene risolta
    affatto — FastAPI risolve solo le dipendenze che servono davvero — quindi il `GET`
    teneva UNA connessione e non due, e il test riusciva con un pool solo dichiarando
    che lo stallo non c'era. È la catena `require_actor → current_user →
    get_connection` a tenere la prima connessione, ed è quella che va esercitata.

    `pool_timeout=3` per non aspettare il valore predefinito di trenta secondi.
    """
    from sqlalchemy import create_engine as crea
    from app import db as app_db
    from app.auth.service import create_user

    PW = "collaudo del pool di lettura"

    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sessions"))
        conn.execute(text("DELETE FROM users WHERE username = 'lettore'"))
        create_user(conn, username="lettore", password=PW, role="admin",
                    must_change_pw=False)

    def minuscolo():
        return crea(DSN, pool_size=1, max_overflow=0, pool_timeout=3)

    richieste, letture = minuscolo(), minuscolo()

    def _dep():
        with app_db.get_engine().connect() as conn:
            with conn.begin():
                yield conn

    monkeypatch.setattr(app_db, "_engine", richieste)
    monkeypatch.setattr(app_db, "_read_engine", letture)
    # ⚠ NESSUNA sostituzione di `require_actor`: è la catena vera che tiene la prima
    # connessione, e sostituirla farebbe passare il test senza esercitare lo stallo.
    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as c:
            acceso = c.post("/api/auth/login", headers=ORIGIN,
                            json={"username": "lettore", "password": PW})
            assert acceso.status_code == 200, acceso.text
            due_pool = c.get("/api/inventory")
            # E ora il progetto sbagliato: il pool di lettura È quello delle richieste.
            monkeypatch.setattr(app_db, "_read_engine", richieste)
            un_pool = c.get("/api/inventory")
    finally:
        app.dependency_overrides.clear()
        richieste.dispose()
        letture.dispose()

    assert due_pool.status_code == 200, due_pool.text
    assert un_pool.status_code == 503, \
        f"con un pool solo il GET è riuscito ({un_pool.status_code}): il test non " \
        "sta dimostrando niente, e l'invariante del pool non è verificata"
    # 503 generico e non un codice della proiezione: è un esaurimento di risorse, non
    # un problema dei dati. La distinzione conta per chi legge i log.
    assert un_pool.json()["detail"]["code"] == "unavailable"


def test_a_read_only_connection_does_not_poison_the_pool(db, engine):
    """La connessione torna nel pool: chi la riusa deve poter scrivere.

    Se SQLAlchemy non ripristinasse gli attributi al rientro, il pool servirebbe una
    connessione di sola lettura a un `PUT`, e il salvataggio fallirebbe a
    intermittenza in produzione con un errore che non nomina la causa. È il genere di
    guasto che si diagnostica in tre giorni.
    """
    from app.db import get_engine

    with snapshot_connection() as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"

    with get_engine().connect() as conn:
        with conn.begin():
            assert conn.execute(
                text("SHOW transaction_read_only")).scalar_one() == "off"
            assert conn.execute(
                text("SHOW transaction_isolation")).scalar_one() == "read committed"
            # E scrive per davvero, non solo «dichiara di poterlo fare».
            conn.execute(text("CREATE TEMP TABLE prova_di_scrittura (x int)"))


# ==================================================================
# 2. ogni forma di documento si riassembla e si serve
# ==================================================================

@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_every_document_is_served_from_sql(db, engine, client, name):
    """Tutte le fixture della mappa, più il seed di produzione e le scadenze.

    Voci di manuale, `vani` come oggetti-valore con e senza porta, campi ignoti
    portati in `extra`, valori falsi espliciti (`""`, `0`, `False`), `foto: null`
    esplicito, `seriali` di tipi misti, date rotte, enum fuori vocabolario, due
    dispositivi con lo stesso identificativo nello stesso rack, codici scambiati,
    riordini, interi fuori scala, numeri ostili.

    Ognuna deve tornare IDENTICA al documento canonico: `assert_get_invariant`
    confronta il documento servito con l'istantanea byte per byte, quindi un solo
    campo perso, aggiunto o di tipo diverso fa fallire il test.
    """
    bootstrap(engine, DOCUMENTS[name])
    assert_get_invariant(engine, client.get("/api/inventory"))


def test_the_production_seed_is_served_from_sql(db, engine, client):
    """Il seed vero: 3 siti, 6 sale, 102 rack, 86 dispositivi, riassemblati."""
    bootstrap(engine, DOCUMENTS["seed"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    racks = [r for loc in body["doc"]["locations"] for room in loc["sale"]
             for r in room["racks"]]
    assert len(body["doc"]["locations"]) == 3
    assert len(racks) == 102
    assert sum(len(r["devices"]) for r in racks) == 86

    # E i conteggi delle TABELLE combaciano con quelli del documento: è la prova
    # diretta che le righe sono la fonte, non un caso fortunato.
    with engine.begin() as c:
        assert projection.counts(c)["racks"] == 102


def test_the_document_comes_from_the_tables_and_not_from_the_snapshot(db, engine,
                                                                     client):
    """⚠ La prova diretta della fase 2D, e il senso di tutto il commit (§16).

    Si MANOMETTE `inventory_versions.doc` della testa lasciando intatto il suo
    `canonical_sha256` — cioè si crea uno stato in cui l'istantanea è corrotta e la
    proiezione è ancora perfettamente fedele al digest registrato.

    Se `GET` leggesse l'istantanea, restituirebbe il sito rinominato dalla
    manomissione. Restituisce invece il nome vero, quello che sta nelle tabelle: il
    documento immutabile non è la fonte, e non c'è nessun ripiego che possa
    diventarlo di nascosto.

    ⚠ E lo strumento del proprietario se ne accorge. `--verify` confronta la
    proiezione col digest REGISTRATO: se non verificasse anche che quel digest
    descrive ancora il documento che gli sta accanto, una manomissione come questa
    passerebbe la verifica — l'imputato sarebbe assolto perché il giudice è stato
    corrotto. È il buco chiuso in questa fase.
    """
    bootstrap(engine, DOCUMENTS["base"])
    version, recorded, immutable = head_of(engine)
    vero = immutable["locations"][0]["nome"]

    manomesso = deepcopy(immutable)
    manomesso["locations"][0]["nome"] = "MANOMESSO NELL'ISTANTANEA"
    sql(engine, "UPDATE inventory_versions SET doc = CAST(:d AS jsonb) "
                " WHERE version = :v",
        d=json.dumps(manomesso, ensure_ascii=False), v=version)

    # Il digest REGISTRATO è rimasto quello di prima: la proiezione lo rispecchia
    # ancora, quindi la precondizione di attualità non ha niente da eccepire.
    assert head_of(engine)[1] == recorded
    assert declared(engine)["head_sha256"] == recorded

    r = client.get("/api/inventory")
    assert r.status_code == 200, r.text
    servito = r.json()["doc"]["locations"][0]["nome"]
    assert servito == vero, "GET sta leggendo l'istantanea, non le tabelle"
    assert "MANOMESSO" not in json.dumps(r.json(), ensure_ascii=False)

    with engine.begin() as c:
        result = projection.verify(c)
    assert not result.ok and result.reason == "digest_della_versione_incoerente", \
        f"`--verify` non ha visto l'oracolo corrotto: {result.reason}"


def test_photo_references_are_uuids_and_no_bytes_travel(db, engine, client):
    """Le foto restano identità, non contenuto (§8.5, §10).

    L'inventario porta l'UUID del rack; i byte si chiedono a `GET /api/photos/{id}`.
    Inserirli qui vorrebbe dire trasferire decine di megabyte per disegnare una
    pianta, e la fase 2D non deve cambiare questo per comodità di un `JOIN`.
    """
    bootstrap(engine, DOCUMENTS["base"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    racks = [r for loc in body["doc"]["locations"] for room in loc["sale"]
             for r in room["racks"]]
    con_foto = [r for r in racks if r.get("foto")]
    assert con_foto, "la fixture deve avere almeno un rack con foto"
    assert {r["foto"] for r in con_foto} <= {relbuild.FOTO_A, relbuild.FOTO_B}

    testo = json.dumps(body, ensure_ascii=False)
    assert "data:image" not in testo and "base64" not in testo
    assert "\\x89PNG" not in testo and "iVBORw0KGgo" not in testo
    # Nessun campo del documento è grande come un'immagine: un binario travestito da
    # stringa si riconoscerebbe dalla lunghezza.
    assert max((len(v) for v in _all_strings(body["doc"])), default=0) < 4096


def _all_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _all_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _all_strings(v)]
    return []


def test_malformed_but_preserved_values_are_served_faithfully(db, engine, client):
    """Date illeggibili ed enum fuori vocabolario NON rendono il `GET` indisponibile.

    È la semantica stabilita in fase 2B (§8.42) e la fase 2D non la irrigidisce: un
    inventario reale è pieno di caselle scritte a mano, e `supporto: "in attesa"` è
    un dato, non un guasto. La colonna derivata resta `NULL`, il testo torna com'è, e
    solo un ERRORE del modello rende l'inventario non servibile.
    """
    bootstrap(engine, DOCUMENTS["broken-dates"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    testi = [d.get("garanzia") for loc in body["doc"]["locations"]
             for room in loc["sale"] for r in room["racks"] for d in r["devices"]]
    testi += [d.get("supporto") for loc in body["doc"]["locations"]
              for room in loc["sale"] for r in room["racks"] for d in r["devices"]]
    assert any(t not in (None, "") for t in testi), \
        "la fixture deve contenere almeno una data scritta a mano"

    with engine.begin() as c:
        illeggibili = c.execute(text(
            "SELECT count(*) FROM inventory_devices "
            " WHERE garanzia IS NOT NULL AND garanzia <> '' "
            "   AND garanzia_date IS NULL")).scalar_one()
    assert illeggibili > 0, "nessuna data illeggibile: il test sarebbe vacuo"


def test_explicit_falsy_values_survive_the_round_trip(db, engine, client):
    """`""`, `0` e `False` ESPLICITI non diventano assenza.

    È la differenza che la canonicalizzazione conserva di proposito, e quella che un
    riassemblaggio distratto perde per primo: `if value:` invece di
    `if value is not None`.
    """
    bootstrap(engine, DOCUMENTS["empty-zero-false"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    trovati = {"vuota": False, "zero": False, "falso": False}
    for value in _all_scalars(body["doc"]):
        if value == "":
            trovati["vuota"] = True
        elif value == 0 and not isinstance(value, bool):
            trovati["zero"] = True
        elif value is False:
            trovati["falso"] = True
    assert all(trovati.values()), f"valori falsi espliciti persi: {trovati}"


def _all_scalars(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _all_scalars(v)
    elif isinstance(value, list):
        for v in value:
            yield from _all_scalars(v)
    else:
        yield value


def test_ordinals_and_not_row_order_decide_the_document_order(db, engine, client):
    """L'ordine del documento viene da `ordinal`, non dall'ordine fisico delle righe.

    Si riscrive la proiezione con lo STESSO modello ma inserendo le righe in ordine
    ROVESCIATO dentro ogni livello: gli `ordinal` non cambiano, l'ordine fisico sì.
    Il documento servito deve restare identico byte per byte.

    PostgreSQL non promette nessun ordine di ritorno senza `ORDER BY`, e un riordino
    fantasma sarebbe un evento di dominio che nessuno ha causato (§8.10): il client lo
    rimanderebbe con un `PUT` e diventerebbe una versione nuova.

    ⚠ Rovesciare i livelli e non l'elenco intero: `write_model` inserisce per livello
    (siti, poi sale, poi rack…) e quell'ordine è imposto dalle chiavi esterne.
    """
    import dataclasses

    bootstrap(engine, DOCUMENTS["reordered"])
    primo = assert_get_invariant(engine, client.get("/api/inventory"))
    version, recorded, _doc = head_of(engine)

    with engine.begin() as c:
        model = projection.read_model(c)
        rovesciato = dataclasses.replace(
            model,
            locations=tuple(reversed(model.locations)),
            rooms=tuple(reversed(model.rooms)),
            racks=tuple(reversed(model.racks)),
            devices=tuple(reversed(model.devices)),
            manual=tuple(reversed(model.manual)))
        assert [r.uid for r in rovesciato.racks] != [r.uid for r in model.racks], \
            "il modello non è stato rovesciato: il test sarebbe vacuo"
        # `synchronise` rilegge e pretende il digest: se l'ordine fisico contasse,
        # ABORTIREBBE qui — la prova arriva prima ancora del `GET`.
        projection.synchronise(c, rovesciato, version=version, sha256=recorded)

    secondo = assert_get_invariant(engine, client.get("/api/inventory"))
    assert json.dumps(primo["doc"], ensure_ascii=False) == \
        json.dumps(secondo["doc"], ensure_ascii=False)


def test_the_same_device_id_twice_in_a_rack_is_still_served(db, engine, client):
    """Due dispositivi con lo stesso identificativo nello stesso rack: un AVVISO.

    Arriva dall'import tabellare ed è un caso reale. La fase 2 non deve diventare più
    severa della fase 1: se questo fosse un errore, la lettura di un inventario
    importato da un foglio di calcolo diventerebbe 503.
    """
    bootstrap(engine, DOCUMENTS["same-code-same-rack"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    codici = [d["id"] for loc in body["doc"]["locations"] for room in loc["sale"]
              for r in room["racks"] for d in r["devices"] if "id" in d]
    assert len(codici) != len(set(codici)), "la fixture non ha più il duplicato"


def test_room_geometry_and_doors_survive_as_value_objects(db, engine, client):
    """`vani` e porte sono JSONB posseduto dalla sala, e tornano identici."""
    bootstrap(engine, DOCUMENTS["deep-room-geometry"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    sale = [room for loc in body["doc"]["locations"] for room in loc["sale"]]
    vani = [v for room in sale for v in room.get("vani", [])]
    assert vani, "la fixture deve avere dei vani"
    assert any("porta" in v for v in vani), "nessuna porta: il test sarebbe vacuo"


def test_unknown_fields_come_back_from_extra(db, engine, client):
    """I campi che nessuna colonna conosce tornano nel documento, da `extra`.

    E hanno UNA sola casa: se un valore stesse sia in colonna sia in `extra`,
    `validate_model` lo chiamerebbe `extra_shadows_column` e il `GET` sarebbe 503 —
    perché due verità sullo stesso campo significano che il riassemblaggio ne sceglie
    una in silenzio.
    """
    bootstrap(engine, DOCUMENTS["unknown-fields"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    with engine.begin() as c:
        popolati = c.execute(text(
            "SELECT count(*) FROM inventory_racks WHERE extra <> '{}'::jsonb")
        ).scalar_one()
    assert popolati > 0, "nessun campo ignoto è finito in `extra`: test vacuo"
    assert body["doc"] == canonicalise(head_of(engine)[2])


# ==================================================================
# 3. la precondizione: non attuale ⇒ non si serve
# ==================================================================

def assert_refused(response, code: str) -> None:
    """503 col codice giusto, e NIENTE che descriva la topologia interna."""
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["detail"]["code"] == code, body
    assert "doc" not in body, "un documento è uscito da una risposta di rifiuto"

    testo = json.dumps(body, ensure_ascii=False)
    for vietato in ("inventory_", "projection_state", "SELECT", "psycopg",
                    "Traceback", "rebuild", "sha256", "mapper"):
        assert vietato not in testo, f"la risposta espone «{vietato}»: {testo}"


def test_a_missing_projection_state_refuses_the_read(db, engine, client):
    """Nessuna dichiarazione: non si sa quale versione le tabelle rappresentino.

    Le righe potrebbero anche essere giuste. «Probabilmente giuste» non è una
    risposta che si può servire.
    """
    bootstrap(engine, DOCUMENTS["base"])
    assert client.get("/api/inventory").status_code == 200

    sql(engine, "DELETE FROM inventory_projection_state")
    assert_refused(client.get("/api/inventory"), "projection_not_current")


def test_a_projection_stale_by_version_refuses_the_read(db, engine, client):
    """La proiezione dichiara una versione che non è la testa."""
    bootstrap(engine, DOCUMENTS["base"])
    modificato = deepcopy(DOCUMENTS["base"])
    modificato["locations"][0]["nome"] = "Pomezia G0 bis"
    save(engine, modificato)
    version, recorded, _doc = head_of(engine)

    sql(engine, "UPDATE inventory_projection_state SET head_version = :v",
        v=version - 1)
    assert_refused(client.get("/api/inventory"), "projection_not_current")


def test_a_projection_stale_by_hash_refuses_the_read(db, engine, client):
    """Il numero di versione combacia, il digest no.

    È il caso che un confronto solo sul numero non vedrebbe — e la ragione per cui lo
    stato registra entrambi.
    """
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_projection_state SET head_sha256 = :s",
        s=DIGEST_FINTO)
    assert_refused(client.get("/api/inventory"), "projection_not_current")


def test_an_unsupported_mapper_version_refuses_the_read(db, engine, client):
    """Una mappa che non gira più: le righe potrebbero avere i dati in posti diversi.

    È il caso a cui il digest è STRUTTURALMENTE cieco. Se un campo passasse da
    `extra` a una colonna tipizzata, le righe scritte dalla mappa vecchia
    riassemblerebbero lo stesso documento — stesso digest, nessun allarme — con i
    dati nel posto sbagliato per ogni query futura.
    """
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_projection_state SET mapper_version = 99")
    assert_refused(client.get("/api/inventory"), "projection_not_current")


def test_a_phase_2b_projection_declaring_no_mapper_refuses_the_read(db, engine,
                                                                   client):
    """`mapper_version = NULL`, cioè una proiezione della fase 2B.

    Non sappiamo quale mappa l'ha scritta: lo sappiamo solo per deduzione («ce n'è
    stata una sola»), e la deduzione non è un dato. `NULL` è la verità, e fallisce
    chiuso — che è esattamente il passo di attivazione documentato.
    """
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_projection_state SET mapper_version = NULL")
    assert_refused(client.get("/api/inventory"), "projection_not_current")


def test_a_never_bootstrapped_inventory_answers_not_bootstrapped(db, engine, client):
    """Nessuna testa: è un altro guasto, e ha il suo codice di sempre.

    Appiattirlo su `projection_not_current` manderebbe chi opera a eseguire
    `--rebuild` su un database che non ha ancora un inventario.
    """
    r = client.get("/api/inventory")
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "not_bootstrapped"


def test_the_remedy_restores_the_read(db, engine, client):
    """Il rimedio documentato funziona, e senza riavviare niente."""
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "DELETE FROM inventory_projection_state")
    assert client.get("/api/inventory").status_code == 503

    with engine.begin() as c:
        projection.rebuild(c)

    assert_get_invariant(engine, client.get("/api/inventory"))


# ==================================================================
# 4. la corruzione manuale: la dichiarazione è falsa
# ==================================================================
#
# Qui lo stato dichiara la versione e il digest GIUSTI — la precondizione di
# attualità non ha niente da eccepire — e le tabelle contengono qualcos'altro. È
# l'unica famiglia di guasti che nessun percorso dell'applicazione può produrre, e
# quindi l'unica la cui causa è per definizione esterna.

def test_a_corrupted_typed_column_refuses_the_read(db, engine, client):
    """Una colonna tipizzata riscritta a mano: il documento cambia, il digest no.

    Si aggiorna `name` di UNA riga: non `code`, che violerebbe `uq_rack_code` e
    farebbe fallire il test per il motivo sbagliato — un vincolo che scatta non è la
    stessa cosa di una verifica che si accorge.
    """
    bootstrap(engine, DOCUMENTS["base"])
    assert client.get("/api/inventory").status_code == 200

    sql(engine, "UPDATE inventory_racks SET name = 'CORROTTO' "
                " WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)")
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_corrupted_extra_refuses_the_read(db, engine, client):
    """Una chiave aggiunta a `extra`: il documento acquista un campo che nessuno ha
    scritto. Il digest lo vede, perché `assemble` emette le chiavi di `extra`."""
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_devices "
                "   SET extra = extra || '{\"inventato\": \"dal DBA\"}'::jsonb "
                " WHERE uid = (SELECT uid FROM inventory_devices "
                "               ORDER BY uid LIMIT 1)")
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_corrupted_ordinal_refuses_the_read(db, engine, client):
    """Due rack che si scambiano l'ordinale: l'ORDINE del documento cambia.

    Lo scambio è un `UPDATE` unico perché `uq_rack_ordinal` è
    `DEFERRABLE INITIALLY IMMEDIATE`, cioè verificato a fine statement: due `UPDATE`
    separati collideriebbero a metà, e il test fallirebbe per il vincolo invece che
    per la verifica.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        sala = c.execute(text(
            "SELECT room_uid FROM inventory_racks GROUP BY room_uid "
            " HAVING count(*) > 1 LIMIT 1")).scalar()
    assert sala is not None, "serve una sala con almeno due rack"

    sql(engine, "UPDATE inventory_racks SET ordinal = 1 - ordinal "
                " WHERE room_uid = :s AND ordinal IN (0, 1)", s=sala)
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_corrupted_parent_refuses_the_read(db, engine, client):
    """Un rack spostato sotto un'altra sala: la struttura cambia, e la chiave esterna
    lo permette perché la sala esiste. Il documento non è più quello della versione."""
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        altra = c.execute(text("""
            SELECT r.uid FROM inventory_rooms r
             WHERE r.uid <> (SELECT room_uid FROM inventory_racks
                              ORDER BY uid LIMIT 1)
               AND NOT EXISTS (SELECT 1 FROM inventory_racks k
                                WHERE k.room_uid = r.uid AND k.ordinal = 0)
             LIMIT 1""")).scalar()
    assert altra is not None, "serve una sala di destinazione senza ordinale 0"

    sql(engine, "UPDATE inventory_racks SET room_uid = :r "
                " WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)",
        r=altra)
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_corrupted_root_metadata_refuses_the_read(db, engine, client):
    """La riga di stato porta anche il livello di RADICE del documento.

    `schema_version`, `has_manual` e `root_extra` non stanno in nessuna delle cinque
    tabelle: stanno lì. Corromperli cambia il documento come corrompere una colonna,
    e `mai aggiornare projection_state indipendentemente` (§8.44) vale anche in
    lettura — chi lo facesse a mano se lo sentirebbe dire dal `GET`.
    """
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_projection_state SET schema_version = 99")
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_corrupted_root_extra_refuses_the_read(db, engine, client):
    bootstrap(engine, DOCUMENTS["base"])
    sql(engine, "UPDATE inventory_projection_state "
                "   SET root_extra = '{\"radice\": \"inventata\"}'::jsonb")
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_dropped_row_refuses_the_read(db, engine, client):
    """Una voce di manuale cancellata: il documento perde un pezzo.

    Un `GET` che servisse questo restituirebbe un inventario *plausibile* e
    incompleto, il client lo rimanderebbe con un `PUT`, e la cancellazione
    diventerebbe una versione nuova firmata da un utente che non ha cancellato
    niente. È il caso peggiore, e la ragione per cui non si ripiega mai.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        assert c.execute(text(
            "SELECT count(*) FROM inventory_manual_entries")).scalar_one() > 0

    sql(engine, "DELETE FROM inventory_manual_entries "
                " WHERE uid = (SELECT uid FROM inventory_manual_entries "
                "               ORDER BY uid LIMIT 1)")
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_corrupted_derived_date_refuses_the_read_although_the_digest_agrees(
        db, engine, client):
    """⚠ Il punto cieco della fase 2B, chiuso anche in lettura.

    `garanzia_date` è una derivata del testo e **non torna nel documento**. Quindi
    scriverla sbagliata lascia il documento identico byte per byte e il digest
    uguale: l'invariante del giro completo — quella su cui poggia tutto il resto di
    questo file — non la vede nemmeno.

    Il test lo DIMOSTRA invece di affermarlo: prima riassembla e verifica che il
    digest sia ancora quello della testa, cioè che il controllo dei digest sia
    davvero cieco a questa corruzione; e solo dopo pretende il 503. Senza il primo
    passo, il test potrebbe passare perché la corruzione ha cambiato il documento —
    cioè provando qualcos'altro.

    La conseguenza pratica è concre: una data derivata sbagliata significa un
    promemoria di scadenza mandato al momento sbagliato, e nessun modo di accorgersene
    guardando l'inventario.
    """
    bootstrap(engine, DOCUMENTS["dated-devices"])
    assert client.get("/api/inventory").status_code == 200
    _version, recorded, _doc = head_of(engine)

    with engine.begin() as c:
        toccate = c.execute(text("""
            UPDATE inventory_devices SET garanzia_date = DATE '2001-01-01'
             WHERE garanzia_date IS NOT NULL
               AND garanzia_date <> DATE '2001-01-01'
        """)).rowcount
    assert toccate > 0, "nessuna data derivata da corrompere: il test sarebbe vacuo"

    # 1. il digest è ANCORA quello della testa: la corruzione è invisibile al giro.
    with engine.begin() as c:
        riassemblato = assemble(projection.read_model(c))
    assert canonical_sha256(riassemblato) == recorded, \
        "la corruzione ha cambiato il documento: questo test non prova il punto cieco"

    # 2. e il `GET` rifiuta comunque, perché non guarda solo il digest.
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_derived_date_invented_where_the_text_is_unreadable_refuses_the_read(
        db, engine, client):
    """`supporto = "in attesa"` con `supporto_date` valorizzata: due verità.

    Il testo è la verità e la colonna è per interrogare. Se divergono, la colonna
    mente — e mente a un worker che manderà una notifica.
    """
    bootstrap(engine, DOCUMENTS["broken-dates"])
    assert client.get("/api/inventory").status_code == 200

    with engine.begin() as c:
        toccate = c.execute(text("""
            UPDATE inventory_devices SET supporto_date = DATE '2030-01-01'
             WHERE supporto IS NOT NULL AND supporto <> '' AND supporto_date IS NULL
        """)).rowcount
    assert toccate > 0, "nessun testo illeggibile: il test sarebbe vacuo"

    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_a_completely_different_projection_refuses_the_read(db, engine, client):
    """La proiezione di un ALTRO documento, con la dichiarazione di questo.

    Si scrive il modello della variante `renamed` e si lascia la riga di stato a
    dichiarare la versione e il digest di `base`. Non è un campo storto: è uno stato
    interamente incoerente, e serve a provare che il rifiuto non dipende dalla
    piccolezza della differenza.
    """
    bootstrap(engine, DOCUMENTS["base"])
    _version, recorded, _doc = head_of(engine)

    from app.inventory.relational import normalise
    altro = normalise(canonicalise(DOCUMENTS["renamed"]))
    with engine.begin() as c:
        stato = c.execute(text("SELECT head_version, head_sha256, mapper_version "
                               "  FROM inventory_projection_state")).mappings().one()
        projection.clear(c)
        projection.write_model(c, altro)
        c.execute(text(f"""
            INSERT INTO {projection.STATE_TABLE}
                   (id, head_version, head_sha256, mapper_version,
                    schema_version, has_manual, root_extra)
            VALUES (TRUE, :v, :s, :m, :sv, :hm, '{{}}'::jsonb)
        """), {"v": stato["head_version"], "s": stato["head_sha256"],
               "m": stato["mapper_version"], "sv": altro.schema_version,
               "hm": altro.has_manual})

    assert declared(engine)["head_sha256"] == recorded, \
        "la dichiarazione deve essere rimasta quella della testa"
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")


def test_the_two_refusals_have_different_codes(db, engine, client):
    """`not_current` e `inconsistent` non si confondono, e non è pedanteria.

    Il primo si rimedia con `--rebuild`; il secondo NO — una ricostruzione
    cancellerebbe le prove di una corruzione di cui non si conosce ancora la causa.
    Un codice solo direbbe a chi opera «esegui --rebuild» in entrambi i casi.
    """
    bootstrap(engine, DOCUMENTS["base"])

    sql(engine, "DELETE FROM inventory_projection_state")
    non_attuale = client.get("/api/inventory").json()["detail"]["code"]

    with engine.begin() as c:
        projection.rebuild(c)
    sql(engine, "UPDATE inventory_racks SET name = 'CORROTTO' "
                " WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)")
    incoerente = client.get("/api/inventory").json()["detail"]["code"]

    assert non_attuale == "projection_not_current"
    assert incoerente == "projection_inconsistent"
    assert non_attuale != incoerente


# ==================================================================
# 5. concorrenza: uno snapshot, non sette letture
# ==================================================================

def test_a_put_committing_mid_read_cannot_split_the_document(db, engine, client,
                                                             monkeypatch):
    """⚠ Il test più importante del file, e la mutazione dell'isolamento.

    Si ferma il `GET` fra la lettura della testa/stato e la lettura delle tabelle, e
    in quel punto un ALTRO utente committa la versione N+1 con la sua proiezione.

    Atteso: il `GET` restituisce un documento della versione N, completo e coerente,
    dal suo snapshot. Non deve:

      - mescolare la testa N con la proiezione N+1 (né il contrario);
      - restituire un 503 spurio a fronte di attività perfettamente normale;
      - avere avuto bisogno di bloccare il `PUT` — che infatti committa dentro la
        pausa, e se il `GET` lo bloccasse questo test andrebbe in stallo.

    ⚠ Perché è la mutazione: sotto READ COMMITTED — cioè se `isolation_level` non
    avesse effetto — le tabelle lette dopo la pausa sarebbero quelle di N+1, il
    digest non tornerebbe con la testa N, e la risposta sarebbe
    `projection_inconsistent`. Questo test è rosso appena l'isolamento smette di
    esserci, e non c'è modo di farlo passare per caso.
    """
    bootstrap(engine, DOCUMENTS["base"])
    version, recorded, immutable = head_of(engine)
    nome_originale = immutable["locations"][0]["nome"]

    originale = projection.read_model
    fatto: list[int] = []

    def legge_dopo_un_commit_concorrente(conn):
        if not fatto:
            fatto.append(1)
            modificato = deepcopy(DOCUMENTS["base"])
            modificato["locations"][0]["nome"] = "SCRITTO DA UN ALTRO UTENTE"
            result = save(engine, modificato)      # su un'ALTRA connessione, e COMMITTA
            assert result.created and result.version == version + 1
        return originale(conn)

    monkeypatch.setattr(projection, "read_model", legge_dopo_un_commit_concorrente)
    r = client.get("/api/inventory")
    monkeypatch.undo()

    assert fatto, "la pausa non è stata eseguita: il test non prova niente"
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == version, "la risposta ha cambiato versione a metà"
    assert body["sha256"] == recorded
    assert canonical_sha256(body["doc"]) == recorded
    assert body["doc"]["locations"][0]["nome"] == nome_originale
    assert "SCRITTO DA UN ALTRO UTENTE" not in json.dumps(body, ensure_ascii=False)

    # E la richiesta successiva, con uno snapshot nuovo, vede N+1: lo snapshot è per
    # transazione, non una cache.
    dopo = assert_get_invariant(engine, client.get("/api/inventory"))
    assert dopo["version"] == version + 1
    assert dopo["doc"]["locations"][0]["nome"] == "SCRITTO DA UN ALTRO UTENTE"


def test_a_read_does_not_block_a_write(db, engine, client, monkeypatch):
    """Il `GET` non prende lock sulla testa: un `PUT` non lo aspetta.

    Si misura invece di dedurlo: la scrittura concorrente parte da un thread mentre
    il `GET` è fermo a metà, e deve committare DENTRO la pausa. Se il `GET` prendesse
    un `FOR UPDATE` sulla riga di testa, il thread resterebbe appeso e il test
    scadrebbe.
    """
    bootstrap(engine, DOCUMENTS["base"])
    version, _recorded, _doc = head_of(engine)

    originale = projection.read_model
    committato = threading.Event()
    guasto: list[BaseException] = []
    prima_volta: list[int] = []

    def scrittore():
        try:
            modificato = deepcopy(DOCUMENTS["base"])
            modificato["locations"][0]["nome"] = "in parallelo"
            save(engine, modificato)
            committato.set()
        except BaseException as exc:      # noqa: BLE001 — va riportato al test
            guasto.append(exc)
            committato.set()

    def legge_dopo_aver_aspettato(conn):
        # ⚠ La guardia NON è difensiva, è necessaria, e la prima stesura non l'aveva.
        #
        # `save` chiama `synchronise`, che chiama `read_model` — cioè QUESTA funzione,
        # perché la sostituzione è sul modulo. Senza guardia il primo scrittore ne
        # avviava un secondo, che ne avviava un terzo, e il secondo restava in attesa
        # del `FOR UPDATE` che il primo teneva: stallo, `wait` scaduto, e un 503
        # generico che accusava il codice invece del test. Il test ha trovato un
        # difetto suo — e vale la pena lasciarlo scritto qui.
        if prima_volta:
            return originale(conn)
        prima_volta.append(1)
        t = threading.Thread(target=scrittore, daemon=True)
        t.start()
        assert committato.wait(timeout=20), \
            "il PUT non ha committato entro 20s: il GET lo sta bloccando"
        t.join(timeout=5)
        return originale(conn)

    monkeypatch.setattr(projection, "read_model", legge_dopo_aver_aspettato)
    r = client.get("/api/inventory")
    monkeypatch.undo()

    assert not guasto, f"il PUT concorrente è fallito: {guasto[0]!r}"
    assert r.status_code == 200, r.text
    assert r.json()["version"] == version, "il GET ha visto la scrittura nuova"


def test_repeated_reads_during_sustained_writes_are_always_coherent(db, engine,
                                                                    client):
    """Letture ripetute sotto scritture continue: ognuna coerente con SE STESSA.

    Non si pretende che le letture vedano una versione precisa — è concorrenza, e
    quale versione veda una richiesta è un dettaglio di tempistica. Si pretende che
    OGNI risposta sia internamente coerente: il digest del documento servito deve
    essere quello registrato per la versione che la risposta dichiara. Un documento
    fatto di due versioni fallirebbe qui, e fallirebbe in modo intermittente — la
    ragione per cui questo test esiste invece di fidarsi del ragionamento.
    """
    bootstrap(engine, DOCUMENTS["base"])
    fermati = threading.Event()
    scritture: list[int] = []
    guasto: list[BaseException] = []

    def scrittore():
        n = 0
        try:
            while not fermati.is_set() and n < 40:
                n += 1
                modificato = deepcopy(DOCUMENTS["base"])
                modificato["locations"][0]["nome"] = f"giro {n}"
                save(engine, modificato)
                scritture.append(n)
        except BaseException as exc:      # noqa: BLE001
            guasto.append(exc)

    t = threading.Thread(target=scrittore, daemon=True)
    t.start()
    try:
        letture = 0
        incoerenti = []
        scaduto = time.monotonic() + 25
        while letture < 25 and time.monotonic() < scaduto:
            r = client.get("/api/inventory")
            letture += 1
            if r.status_code != 200:
                incoerenti.append(("stato", r.status_code, r.text[:200]))
                continue
            body = r.json()
            with engine.begin() as c:
                registrato = c.execute(text(
                    "SELECT canonical_sha256 FROM inventory_versions "
                    " WHERE version = :v"), {"v": body["version"]}).scalar()
            if not (canonical_sha256(body["doc"]) == body["sha256"] == registrato):
                incoerenti.append(("digest", body["version"], body["sha256"][:12]))
    finally:
        fermati.set()
        t.join(timeout=15)

    assert not guasto, f"lo scrittore è fallito: {guasto[0]!r}"
    assert len(scritture) >= 3, f"troppe poche scritture concorrenti: {len(scritture)}"
    assert letture >= 10, f"troppe poche letture: {letture}"
    assert not incoerenti, f"risposte non coerenti con sé stesse: {incoerenti[:5]}"


# ==================================================================
# 6. ripristino di una versione precedente
# ==================================================================

def test_a_rollback_goes_through_the_normal_save_path(db, engine, client):
    """Tornare a una versione vecchia crea una versione NUOVA, e la proiezione segue.

    Non esiste un percorso di ripristino: si rilegge il documento storico e si salva.
    Un solo percorso di codice, quindi il ripristino eredita gratuitamente
    l'invariante, la validazione dell'identità, l'audit e la scrittura doppia — e non
    esiste una seconda implementazione che possa restare indietro.

    Lo scenario chiesto: v1 con FOTO_A, v2 con FOTO_B, v3 un'altra modifica,
    ripristino a v1 → nasce v4.

    ⚠ Le righe storiche non si toccano MAI, e la foto della v2 resta protetta dai suoi
    riferimenti storici anche quando lo stato corrente non la monta più: confondere la
    foto CORRENTE (`inventory_racks.photo_id`) con la raggiungibilità storica
    (`inventory_photo_refs`) farebbe cancellare la foto di una versione passata appena
    il rack ne monta un'altra (§8.5).
    """
    con_a = deepcopy(DOCUMENTS["base"])
    primo_rack = con_a["locations"][0]["sale"][0]["racks"][0]
    primo_rack["foto"] = relbuild.FOTO_A
    v1 = bootstrap(engine, con_a)

    con_b = deepcopy(con_a)
    con_b["locations"][0]["sale"][0]["racks"][0]["foto"] = relbuild.FOTO_B
    v2 = save(engine, con_b).version

    terzo = deepcopy(con_b)
    terzo["locations"][0]["nome"] = "un'altra modifica ancora"
    v3 = save(engine, terzo).version
    assert (v1, v2, v3) == (1, 2, 3)

    prima_del_ripristino = {v: head_doc(engine, v) for v in (v1, v2, v3)}

    # --- il ripristino: si rilegge la v1 e si salva ---
    v4 = save(engine, prima_del_ripristino[v1]).version
    assert v4 == 4, "un ripristino è una versione nuova, non una riscrittura"

    body = assert_get_invariant(engine, client.get("/api/inventory"))
    assert body["version"] == v4
    assert body["doc"] == canonicalise(prima_del_ripristino[v1]), \
        "il documento servito non è lo stato della v1"
    assert body["doc"]["locations"][0]["sale"][0]["racks"][0]["foto"] \
        == relbuild.FOTO_A

    # --- la proiezione porta la foto della v1, non quella della v2 ---
    with engine.begin() as c:
        foto_corrente = c.execute(text(
            "SELECT photo_id FROM inventory_racks WHERE uid = :u"),
            {"u": primo_rack["_uid"]}).scalar()
    assert str(foto_corrente) == relbuild.FOTO_A

    # --- la storia è intatta, e FOTO_B è ancora protetta ---
    for v, doc in prima_del_ripristino.items():
        assert head_doc(engine, v) == doc, f"la versione storica {v} è cambiata"
    with engine.begin() as c:
        rif = {str(r[0]) for r in c.execute(text(
            "SELECT photo_id FROM inventory_photo_refs")).all()}
        esiste = c.execute(text("SELECT count(*) FROM photos WHERE id = :i"),
                           {"i": relbuild.FOTO_B}).scalar_one()
    assert relbuild.FOTO_B in rif, \
        "la foto della v2 non è più raggiungibile: la GC la cancellerebbe"
    assert esiste == 1


def head_doc(engine, version: int) -> dict:
    with engine.begin() as c:
        return c.execute(text("SELECT doc FROM inventory_versions WHERE version = :v"),
                         {"v": version}).scalar_one()


# ==================================================================
# 7. il contratto HTTP, che non è cambiato
# ==================================================================

def test_the_read_still_requires_authentication(db, engine):
    """401 e non 503: la fase 2D non ha aperto una porta.

    ⚠ Con l'inventario NON inizializzato, di proposito. Se l'ordine fosse invertito —
    prima si tenta la lettura, poi si controlla l'attore — questo test risponderebbe
    503 `not_bootstrapped` e rivelerebbe a chi non è autenticato lo stato del
    servizio. Ed è anche la prova che una richiesta anonima non apre nessuna
    transazione sul database.
    """
    def _dep():
        from app.db import get_engine
        with get_engine().connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as c:
            r = c.get("/api/inventory")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "not_authenticated"


def test_a_temporary_password_session_still_cannot_read(db, engine):
    """Sessione valida ma RISTRETTA: 403 finché la password provvisoria non è cambiata.

    La restrizione è strutturale — passa da `require_actor` (§8.26) — e la fase 2D non
    doveva poterla aggirare cambiando la fonte dei dati. Si esercita la dipendenza
    vera, non un doppio: un `require_actor` sostituito proverebbe soltanto che il
    sostituto funziona.
    """
    from app.auth.service import create_user

    #: Conforme alla politica (§8.43): 15 punti di codice almeno, non in blocklist.
    #: Serve solo ad accedere — `must_change_pw` resta `True`, che è il punto.
    PROVVISORIA = "collaudo della lettura relazionale"

    bootstrap(engine, DOCUMENTS["base"])

    def _dep():
        from app.db import get_engine
        with get_engine().connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as c:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM sessions"))
                conn.execute(text("DELETE FROM users WHERE username = 'nuovo'"))
                create_user(conn, username="nuovo", password=PROVVISORIA,
                            role="edit")
            r = c.post("/api/auth/login", headers=ORIGIN,
                       json={"username": "nuovo", "password": PROVVISORIA})
            assert r.status_code == 200, r.text
            assert r.json()["mustChangePassword"] is True

            r = c.get("/api/inventory")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


def test_the_response_shape_is_byte_for_byte_the_old_contract(db, engine, client):
    """Le chiavi, i tipi, l'header. Il frontend non deve cambiare di una riga."""
    bootstrap(engine, DOCUMENTS["base"])
    r = client.get("/api/inventory")

    assert r.status_code == 200
    body = r.json()
    assert list(body) == ["version", "schemaVersion", "sha256", "doc"]
    assert isinstance(body["version"], int)
    assert isinstance(body["schemaVersion"], int)
    assert isinstance(body["sha256"], str) and len(body["sha256"]) == 64
    assert isinstance(body["doc"], dict)
    assert r.headers["cache-control"] == "no-store"
    assert body["schemaVersion"] == body["doc"]["schemaVersion"]


def test_a_get_and_a_put_of_the_same_document_is_a_no_op(db, engine, client):
    """⚠ Il giro completo del client vero, e il difetto che rovinerebbe tutto.

    Il frontend legge, l'utente non tocca niente, il frontend risalva. Se il `GET`
    assemblasse anche solo un campo in modo diverso da come la mappa lo normalizza,
    quel `PUT` non sarebbe un no-op: creerebbe una versione nuova con un contenuto
    che nessuno ha scritto, firmata da un utente che non ha modificato niente. Ed è
    il difetto che si accumulerebbe in silenzio, una versione per apertura di pagina.

    Su TUTTE le fixture, perché è la forma del documento a determinare se il giro
    torna.
    """
    for name in NAMES:
        with engine.begin() as c:
            c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                           "RESTART IDENTITY CASCADE"))
            c.execute(text("DELETE FROM inventory_locations"))
            c.execute(text("DELETE FROM inventory_manual_entries"))
            c.execute(text("DELETE FROM inventory_projection_state"))
        bootstrap(engine, DOCUMENTS[name])

        body = assert_get_invariant(engine, client.get("/api/inventory"))
        r = client.put("/api/inventory", headers=ORIGIN,
                       json={"baseVersion": body["version"], "doc": body["doc"]})
        assert r.status_code == 200, f"{name}: {r.text}"
        assert r.json()["changed"] is False, \
            f"{name}: rimandare il documento letto ha creato una versione nuova"
        assert r.json()["version"] == body["version"]
        assert r.json()["sha256"] == body["sha256"]


# ==================================================================
# 8. gli strumenti restano indipendenti, e la readiness leggera
# ==================================================================

def test_the_operational_invariant_holds_all_three_ways(db, engine, client):
    """`GET` riesce, `--verify` riesce, e i due parlano dello stesso digest (§11).

    Tre strade indipendenti verso la stessa risposta: la rotta HTTP, lo strumento del
    proprietario, e la testa nel database. Se una divergesse dalle altre, sapremmo
    subito quale — ed è il motivo per cui `--verify` non è «chiama il GET».
    """
    bootstrap(engine, DOCUMENTS["seed"])
    body = assert_get_invariant(engine, client.get("/api/inventory"))

    with engine.begin() as c:
        result = projection.verify(c)
    assert result.ok and result.faithful and result.current
    assert body["sha256"] == head_of(engine)[1] == result.status.head_sha256


def test_readiness_stays_cheap_and_does_not_reassemble(db, engine, client):
    """La readiness resta il confronto fra tre valori registrati (§12).

    La separazione è voluta: la sonda gira ogni pochi secondi per sempre, il `GET`
    una volta per richiesta. La prova che la readiness NON riassembla è la sua
    reazione alla corruzione di una colonna: lo stato dichiara ancora la versione e
    il digest giusti, quindi resta «pronta» — mentre il `GET`, che riassembla,
    rifiuta. Se la readiness diventasse 503 anche qui, avrebbe cominciato a fare il
    lavoro del `GET` a ogni sonda.
    """
    bootstrap(engine, DOCUMENTS["base"])
    assert client.get("/api/ready").json()["projection"] == "ok"

    sql(engine, "UPDATE inventory_racks SET name = 'CORROTTO' "
                " WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)")

    pronta = client.get("/api/ready")
    assert pronta.json()["projection"] == "ok", \
        "la readiness ha cominciato a riassemblare: costerebbe un --verify a ogni sonda"
    assert_refused(client.get("/api/inventory"), "projection_inconsistent")

    # E la readiness vede invece ciò che è SUO: una proiezione non attuale.
    sql(engine, "DELETE FROM inventory_projection_state")
    non_pronta = client.get("/api/ready")
    assert non_pronta.status_code == 503
    assert non_pronta.json()["projection"] == "not-ready"


def test_the_notification_worker_still_reads_the_document(db, engine):
    """Lo scanner delle scadenze NON è passato alle colonne derivate (§17).

    Fuori dallo scopo della 2D, e va provato invece di dichiarato: si corrompono le
    colonne derivate e si verifica che lo scanner trovi ancora le stesse scadenze,
    perché legge il testo del documento. Il giorno in cui passasse alle colonne,
    questo test diventerebbe rosso e la decisione sarebbe presa di proposito.
    """
    from app.notifications.expiry import due_items

    #: Le soglie di preavviso predefinite: la regola e'
    #: `0 <= giorni_rimanenti <= N` per almeno una N (§8.41).
    PREAVVISI = [60, 30, 7]

    bootstrap(engine, DOCUMENTS["expiry"])
    _version, _recorded, doc = head_of(engine)
    prima = {i.key for i in due_items(doc, today=TODAY, warning_days=PREAVVISI)}
    assert prima, "la fixture delle scadenze non produce nessuna scadenza"

    sql(engine, "UPDATE inventory_devices SET garanzia_date = NULL, "
                "                             supporto_date = NULL")
    dopo = {i.key
            for i in due_items(head_of(engine)[2], today=TODAY, warning_days=PREAVVISI)}
    assert dopo == prima, "lo scanner dipende dalle colonne derivate: non deve"
