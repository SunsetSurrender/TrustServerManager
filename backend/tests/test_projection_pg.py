"""Fase 2B: costruire la proiezione, rileggerla da SQL, e non fidarsi.

PostgreSQL reale, e qui non è una preferenza: ciò che si verifica è precisamente
quello che il database fa ai valori. Il giro attraverso una colonna `numeric`, il
limite di un `integer`, un `uuid` che torna come oggetto invece che come stringa,
un lock che fa aspettare: nessuna di queste cose si può provare con un doppio,
perché il doppio le farebbe come me le immagino io.

Quattro affermazioni che questa suite deve dimostrare, in ordine di importanza:

  1. il documento riassemblato **da SQL** è la versione che dice di essere, sul seed
     di produzione e su ogni fixture — digest per digest;
  2. quando NON lo è, la ricostruzione aborta e nel database non cambia niente;
  3. nessuno consuma la proiezione: `GET` continua a servire l'istantanea JSON
     anche con la proiezione corrotta, e la readiness non la guarda;
  4. una proiezione vecchia si VEDE, perché la fase 2C non esiste ancora e un `PUT`
     la lascia indietro per progetto.

Riferimento: BACKEND-PLAN.md §8.42.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import Actor, InventoryRepository, canonical_sha256
from app.inventory import projection
from app.inventory.document import strip_legacy_fields
from app.inventory.projection import ProjectionAborted
from app.inventory.relational import MAPPER_VERSION, assemble
from app.inventory.relational_validate import errors, validate_model

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")

PROJECTION_TABLES = ("inventory_locations", "inventory_rooms", "inventory_racks",
                     "inventory_devices", "inventory_manual_entries")


def _load(name: str, relative: str):
    """Come in `test_relational_mapper.py`: per PERCORSO, con un nome proprio.

    I generatori si chiamano tutti `build.py`, e `sys.modules` è condiviso da tutta
    la sessione di pytest: `import build` restituirebbe quello che un altro file ha
    già caricato. Il risultato non sarebbe un errore ma una fixture sbagliata, cioè
    un test che passa provando qualcos'altro.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relbuild = _load("tsm_fixture_relational_pg", "fixtures/relational/build.py")
build_expiry = _load("tsm_fixture_expiry_proj",
                     "fixtures/expiry/build.py").build_inventory

TODAY = date(2026, 8, 10)

#: Tutti i documenti della fase 2A più il seed di produzione e l'inventario delle
#: scadenze: gli stessi che la suite pura verifica in memoria, qui fatti passare
#: dal database vero. È il passaggio che la suite pura non può coprire.
DOCUMENTS = dict(relbuild.documents())
DOCUMENTS["seed"] = strip_legacy_fields(
    json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
DOCUMENTS["expiry"] = build_expiry(TODAY)

#: ⚠ Esclusi dalla passata generale, con un test proprio che spiega perché.
#:
#: `jsonb-hostile-numbers` contiene `1e+20` e `-0.0`, che JSONB non conserva: il
#: confine è il MAGAZZINO DELLE ISTANTANEE, non la proiezione, e pretendere che il
#: giro torni sarebbe pretendere che la fase 2B ripari un difetto della fase 1. Un
#: elenco esplicito e non uno `skip` silenzioso: un test saltato somiglia molto a un
#: test passato.
JSONB_LOSSY = ("jsonb-hostile-numbers",)
NAMES = sorted(set(DOCUMENTS) - set(JSONB_LOSSY))


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

    `TRUNCATE inventory_versions CASCADE` porta via anche i riferimenti alle foto e
    la riga di stato della proiezione (che referenzia una versione). Le foto vanno
    cancellate DOPO i rack, perché `inventory_racks.photo_id` le protegge.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM photos"))
        # La GC delle foto è idempotente sulla data locale: senza questa riga il
        # giro del test troverebbe `already_ran_today` per un'esecuzione fatta da
        # un'altra suite, e passerebbe senza aver cancellato niente.
        c.execute(text("DELETE FROM maintenance_runs"))
        # Le foto che le fixture referenziano devono ESISTERE: il salvataggio
        # pretende già che ci siano (§8.5), quindi un documento con `foto` non
        # arriverebbe mai in testa senza di loro. Si inseriscono con l'id atteso,
        # che è l'unico modo di riprodurre lo stato reale.
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


def save(engine, doc: dict) -> int:
    with engine.begin() as c:
        repo = InventoryRepository(c)
        return repo.save(repo.head_version(), doc, ADMIN).version


def rebuild(engine):
    with engine.begin() as c:
        return projection.rebuild(c)


def status(engine):
    with engine.begin() as c:
        return projection.status(c)


def verify(engine):
    with engine.begin() as c:
        return projection.verify(c)


def recorded_sha(engine, version: int) -> str:
    with engine.begin() as c:
        return c.execute(text("SELECT canonical_sha256 FROM inventory_versions "
                              "WHERE version = :v"), {"v": version}).scalar_one()


# ==================================================================
# 1. il documento riassemblato da SQL è la versione che dice di essere
# ==================================================================

@pytest.mark.parametrize("name", NAMES, ids=NAMES)
def test_every_document_survives_the_projection(db, engine, name):
    """⚠ Il test centrale della fase 2B, su ventiquattro documenti.

    La suite pura prova la mappa in memoria. Questo prova il giro COMPLETO: il
    documento passa da JSONB, dalle colonne tipizzate, dagli array di testo, dai
    `numeric` e dagli `uuid`, e torna indietro. È il passaggio in cui un numero
    cambia forma e un `uuid` cambia tipo, e nessun doppio lo riprodurrebbe.

    Il confronto è col digest REGISTRATO nella versione: non con uno ricalcolato
    adesso, che sarebbe confrontare il codice con sé stesso.
    """
    version = bootstrap(engine, DOCUMENTS[name])
    report = rebuild(engine)

    assert report.version == version
    assert report.sha256 == recorded_sha(engine, version)
    assert verify(engine).ok
    assert status(engine).fresh


def test_the_production_seed_projects_completely(db, engine):
    """Il seed vero: 3 siti, 6 sale, 102 rack, 86 dispositivi. Niente in `extra`,
    nessun avviso, e il digest che il database ha già registrato."""
    version = bootstrap(engine, DOCUMENTS["seed"])
    report = rebuild(engine)

    assert report.counts == {"locations": 3, "rooms": 6, "racks": 102,
                             "devices": 86, "manual": 0}
    assert report.rows_written == 3 + 6 + 102 + 86
    assert report.warnings == [], "sui dati veri la normalizzazione è completa"
    assert report.sha256 == recorded_sha(engine, version)

    with engine.begin() as c:
        for table in PROJECTION_TABLES:
            vuote = c.execute(text(f"SELECT count(*) FROM {table} "
                                   f"WHERE extra <> '{{}}'::jsonb")).scalar_one()
            assert vuote == 0, f"{table}: qualcosa viaggia in `extra`"


def test_the_rebuild_is_idempotent(db, engine):
    """Ricostruire due volte deve dare la stessa proiezione. Un comando che si può
    rilanciare è un comando che si rilancia — dopo un dubbio, dopo un ripristino — e
    non deve accumulare righe né cambiare digest."""
    bootstrap(engine, DOCUMENTS["base"])
    primo = rebuild(engine)
    secondo = rebuild(engine)
    assert (primo.version, primo.sha256, primo.counts) == \
           (secondo.version, secondo.sha256, secondo.counts)
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_projection_state"
                              )).scalar_one() == 1
        assert projection.counts(c) == secondo.counts


def test_the_uuids_come_back_as_strings(db, engine):
    """⚠ Il difetto che si sarebbe manifestato come «il digest non torna».

    Una colonna `uuid` letta con una query testuale torna come oggetto `uuid.UUID`.
    `assemble` lo metterebbe nel campo `_uid`, dove non è serializzabile in JSON e
    non è uguale alla stringa a cui il digest deve corrispondere — un difetto che
    non fa pensare a un tipo.
    """
    bootstrap(engine, DOCUMENTS["base"])
    rebuild(engine)
    with engine.begin() as c:
        model = projection.read_model(c)
    for row in model.locations + model.rooms + model.racks + model.devices:
        assert isinstance(row.uid, str), type(row.uid)
    for rack in model.racks:
        assert rack.photo_id is None or isinstance(rack.photo_id, str)
    # E il documento riassemblato è serializzabile: se gli `_uid` fossero oggetti
    # `UUID`, questa riga solleverebbe.
    json.dumps(assemble(model))


def test_the_hostile_numbers_come_back_bit_for_bit(db, engine):
    """⚠ Il contratto di legatura delle colonne `numeric`, contro il database vero.

    `relational.py` promette che `10.0` torna `10.0` e non `10`, e che
    `0.30000000000000004` non diventa `0.3`. È una promessa su ciò che PostgreSQL
    fa, quindi va provata contro PostgreSQL: legando il float invece del `Decimal`
    entrambe cadevano, e cadevano in silenzio.
    """
    version = bootstrap(engine, DOCUMENTS["hostile-numbers"])
    report = rebuild(engine)
    assert report.sha256 == recorded_sha(engine, version)

    with engine.begin() as c:
        model = projection.read_model(c)
    room = [r for r in model.rooms if r.uid == relbuild.R1][0]
    assert json.dumps(room.w) == "10.0", "la scala di `numeric` è il dato"
    assert room.h == 0.30000000000000004
    rack = [r for r in model.racks if r.uid == relbuild.K1][0]
    assert rack.w == 1e-9


def test_a_version_stored_before_the_numeric_fix_aborts_the_rebuild(db, engine):
    """⚠ Dati SCRITTI PRIMA della correzione: la proiezione li diagnostica.

    `1e+20` e `-0.0` non sopravvivono a JSONB (misurato: diventano
    `100000000000000000000` e `0.0`), e `inventory_versions.doc` è JSONB. Il digest
    registrato al salvataggio non corrisponde più al documento riletto.

    Oggi un documento così **non entra più**: lo schema congelato lo rifiuta con
    `json_number_not_roundtrippable` (§8.16), e `test_snapshot_numbers_pg.py` lo
    prova. Ma le versioni sono immutabili e per sempre: una scritta prima della
    correzione resta lì. Per riprodurla si inserisce la riga DIRETTAMENTE, come
    proprietario — che è esattamente il modo in cui è nata.

    La proiezione non ha un riferimento di cui fidarsi e ABORTA dicendo questo,
    invece di ricalcolare il digest in silenzio — che sarebbe coprire il caso in cui
    un'istantanea immutabile non corrisponde al suo digest.
    """
    doc = DOCUMENTS["jsonb-hostile-numbers"]

    # La conferma che oggi quel documento non passerebbe più dal percorso normale.
    from app.inventory import validate_normal_document
    problemi = validate_normal_document(doc)
    assert {e.code for e in problemi} == {"json_number_not_roundtrippable"}

    # Lo stato di allora, scritto come lo scriveva allora: il digest calcolato sul
    # candidato, il documento come JSONB lo conserva.
    with engine.begin() as c:
        version = c.execute(text("""
            INSERT INTO inventory_versions
                   (doc, canonical_sha256, actor_username, actor_role)
            VALUES (CAST(:doc AS jsonb), :sha, 'capo', 'admin')
         RETURNING version
        """), {"doc": json.dumps(doc, ensure_ascii=False),
               "sha": canonical_sha256(doc)}).scalar_one()
        c.execute(text("INSERT INTO inventory_head (id, version) VALUES (TRUE, :v)"),
                  {"v": version})

    with engine.begin() as c:
        riletto = c.execute(text("SELECT doc FROM inventory_versions "
                                 "WHERE version = :v"), {"v": version}).scalar_one()
    assert canonical_sha256(riletto) != canonical_sha256(doc), (
        "se JSONB conservasse questi numeri, questo test non avrebbe più ragione di "
        "esistere e andrebbe cancellato invece di adattato")

    with pytest.raises(ProjectionAborted) as err:
        rebuild(engine)
    assert err.value.reason == "digest_della_versione_incoerente"
    with engine.begin() as c:
        assert projection.counts(c)["racks"] == 0


def test_an_oversized_integer_does_not_break_the_insert(db, engine):
    """`u` e `h` sono `integer`. Senza il limite nel predicato, questo documento
    fermerebbe il popolamento con «integer out of range» a metà — cioè una
    migrazione che aborta per un dato che la fase 1 ha sempre accettato."""
    version = bootstrap(engine, DOCUMENTS["oversized-integers"])
    report = rebuild(engine)
    assert report.sha256 == recorded_sha(engine, version)
    with engine.begin() as c:
        row = c.execute(text("SELECT u, extra->'u' FROM inventory_racks "
                             "WHERE uid = :u"), {"u": relbuild.K1}).one()
    assert row[0] is None and row[1] == 3_000_000_000


def test_a_document_with_a_nul_byte_never_becomes_a_version(db, engine):
    """Perché il proiettore non trova mai un byte NUL in una versione.

    PostgreSQL non accetta `\\u0000` né in `text` né in `jsonb`, e
    `inventory_versions.doc` È jsonb: un documento con un NUL non diventa una versione,
    quindi non arriva mai alla proiezione.

    ⚠ Il CONFINE si è spostato, e il test lo segue. Prima il rifiuto arrivava dal
    database a metà del salvataggio (un 500); adesso lo dà la validazione dello schema
    congelato, con `json_string_not_roundtrippable` e il percorso del campo (§8.16).
    La conseguenza per la proiezione non cambia — nessuna versione, niente da
    rispecchiare — ma chi salva riceve un errore che può correggere.
    """
    from app.inventory import DocumentRejectedError
    from app.inventory.document import STRING_NOT_ROUNDTRIPPABLE

    doc = relbuild.base()
    doc["locations"][0]["nome"] = "Pomezia\x00G0"
    with pytest.raises(DocumentRejectedError) as err:
        bootstrap(engine, doc)
    problemi = [d for d in err.value.details
                if d["code"] == STRING_NOT_ROUNDTRIPPABLE]
    assert problemi and problemi[0]["path"] == "locations[0].nome"
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")
                         ).scalar_one() == 0


# ==================================================================
# 2. le date derivate: la ragione per cui la proiezione esiste
# ==================================================================

def test_the_expiry_scan_becomes_a_query(db, engine):
    """⚠ Il pagamento di tutto il commit.

    «Quali dispositivi scadono entro trenta giorni» oggi è una scansione dell'intero
    documento in Python (§8.41). Con le colonne derivate è una query — e deve
    restituire ESATTAMENTE lo stesso insieme, altrimenti la proiezione risponde una
    cosa diversa dal worker e il giorno in cui la vista Scadenze passerà a SQL gli
    avvisi cambieranno senza che nessuno abbia cambiato niente.
    """
    from app.notifications.expiry import due_items

    doc = DOCUMENTS["expiry"]
    bootstrap(engine, doc)
    rebuild(engine)

    atteso = {(i.entity_uid, i.kind, i.expiry)
              for i in due_items(doc, today=TODAY, warning_days=[30])}
    assert atteso, "la fixture deve contenere scadenze in finestra"

    with engine.begin() as c:
        rows = c.execute(text("""
            SELECT uid, garanzia_date, supporto_date
              FROM inventory_devices
             WHERE garanzia_date BETWEEN :oggi AND :limite
                OR supporto_date BETWEEN :oggi AND :limite
        """), {"oggi": TODAY, "limite": date(2026, 9, 9)}).all()

    trovato = set()
    for uid, garanzia, supporto in rows:
        for kind, value in (("garanzia", garanzia), ("supporto", supporto)):
            if value is not None and TODAY <= value <= date(2026, 9, 9):
                trovato.add((str(uid), kind, value))
    assert trovato == atteso


def test_the_derived_dates_in_sql_agree_with_the_scanner_row_by_row(db, engine):
    """Su ogni riga della proiezione, non solo su quelle in finestra. La colonna
    deve significare «la data che il worker userà», sempre."""
    from app.notifications.expiry import parse_expiry

    bootstrap(engine, DOCUMENTS["expiry"])
    rebuild(engine)
    with engine.begin() as c:
        rows = c.execute(text("SELECT uid, garanzia, garanzia_date, supporto, "
                              "supporto_date FROM inventory_devices")).all()
    assert rows
    for uid, garanzia, garanzia_date, supporto, supporto_date in rows:
        assert garanzia_date == parse_expiry(garanzia), uid
        assert supporto_date == parse_expiry(supporto), uid


def test_a_hand_edited_derived_date_is_invisible_to_the_digest(db, engine):
    """⚠ Il difetto che l'invariante NON può vedere, e chi lo vede.

    `garanzia_date` non torna nel documento: cambiarla a mano lascia il digest
    identico. La verifica quindi non può guardare solo i digest — se lo facesse,
    l'unico difetto che l'invariante non copre sarebbe anche l'unico che lo
    strumento fatto per coprirlo non guarda.
    """
    bootstrap(engine, DOCUMENTS["dated-devices"])
    rebuild(engine)
    assert verify(engine).ok

    with engine.begin() as c:
        cambiate = c.execute(text("""
            UPDATE inventory_devices SET garanzia_date = '1999-01-01'
             WHERE garanzia_date IS NOT NULL RETURNING uid
        """)).all()
    assert cambiate, "la fixture deve avere almeno una data interpretata"

    with engine.begin() as c:
        model = projection.read_model(c)
        # Il documento è ancora identico: il digest non se ne accorge.
        assert canonical_sha256(assemble(model)) == \
            recorded_sha(engine, status(engine).projected_version)
        # La validazione sì.
        found = errors(validate_model(model))
    assert [f.code for f in found] == ["derived_mismatch"] * len(cambiate)

    esito = verify(engine)
    assert not esito.ok and esito.reason == "modello_incoerente"


# ==================================================================
# 3. quando non torna, aborta — e non resta niente
# ==================================================================

def test_a_rebuild_that_does_not_round_trip_aborts(db, engine, monkeypatch):
    """⚠ La controprova del commit: si rompe la mappa e si pretende l'abort.

    Senza questo test, un confronto scritto male — che confronta il modello con sé
    stesso, o due volte lo stesso digest — sarebbe indistinguibile da un confronto
    soddisfatto, e tutta la suite passerebbe senza dimostrare niente.
    """
    bootstrap(engine, DOCUMENTS["base"])

    def assemble_bacato(model):
        doc = assemble(model)
        # Si butta un rack: il documento riassemblato non è più la versione.
        doc["locations"][0]["sale"][0]["racks"].pop()
        return doc

    prima = status(engine)
    assert prima.fresh, "il bootstrap deve aver già scritto la proiezione"

    monkeypatch.setattr(projection, "assemble", assemble_bacato)
    with pytest.raises(ProjectionAborted) as err:
        rebuild(engine)
    assert err.value.reason == "digest_diverso"
    assert err.value.details

    # ⚠ E nel database non è cambiato NIENTE. L'asserzione è cambiata con la fase
    # 2C: prima si pretendeva una proiezione vuota, perché il bootstrap non ne
    # scriveva una. Adesso quella del bootstrap esiste, e «niente è cambiato»
    # significa che è ancora lì e ancora fedele — che è un'asserzione più forte,
    # perché un rollback incompleto la lascerebbe a metà invece che assente.
    monkeypatch.undo()
    dopo = status(engine)
    assert dopo.projected_version == prima.projected_version
    assert dopo.projected_sha256 == prima.projected_sha256
    assert dopo.counts == prima.counts
    assert verify(engine).ok


def test_an_abort_leaves_the_previous_projection_untouched(db, engine, monkeypatch):
    """Un tentativo fallito non deve peggiorare la situazione: la proiezione
    precedente resta buona, e resta VERIFICABILE."""
    bootstrap(engine, DOCUMENTS["base"])
    buona = rebuild(engine)

    def assemble_bacato(model):
        doc = assemble(model)
        doc["locations"][0]["nome"] = "un altro nome"
        return doc

    monkeypatch.setattr(projection, "assemble", assemble_bacato)
    with pytest.raises(ProjectionAborted):
        rebuild(engine)
    monkeypatch.undo()

    dopo = status(engine)
    assert dopo.projected_version == buona.version
    assert dopo.projected_sha256 == buona.sha256
    assert verify(engine).ok


def test_an_inconsistent_model_aborts_before_writing_anything(db, engine):
    """Il documento in testa punta a una foto che non esiste: si aborta al passo 4,
    prima di svuotare la proiezione.

    La foto si cancella dal database DOPO il bootstrap, perché il salvataggio
    pretende già che esista (§8.5). È lo stato che resterebbe se qualcuno cancellasse
    i byte fuori dall'API — e la proiezione deve rifiutarsi di rappresentarlo, non
    scrivere un rack con un riferimento rotto.
    """
    import hashlib
    payload = b"\x89PNG\r\n\x1a\n finti byte"
    doc = relbuild.base()
    with engine.begin() as c:
        photo_id = c.execute(text("""
            INSERT INTO photos (mime, bytes, sha256, size_bytes)
            VALUES ('image/png', :b, :s, :n) RETURNING id
        """), {"b": payload, "s": hashlib.sha256(payload).hexdigest(),
               "n": len(payload)}).scalar_one()
    doc["locations"][0]["sale"][0]["racks"][0]["foto"] = str(photo_id)
    bootstrap(engine, doc)
    rebuild(engine)                      # la prima volta funziona: la foto c'è

    with engine.begin() as c:
        c.execute(text("UPDATE inventory_racks SET photo_id = NULL"))
        c.execute(text("DELETE FROM inventory_photo_refs"))
        c.execute(text("DELETE FROM photos WHERE id = :p"), {"p": photo_id})

    with pytest.raises(ProjectionAborted) as err:
        rebuild(engine)
    assert err.value.reason == "modello_incoerente"
    assert err.value.details[0]["code"] == "photo_not_found"


def test_a_snapshot_whose_recorded_digest_lies_aborts(db, engine):
    """Un difetto più grave della proiezione, e da non coprire.

    Se il digest registrato in una versione non corrisponde al suo contenuto, non c'è
    un riferimento di cui fidarsi: ricalcolarlo in silenzio e proseguire vorrebbe
    dire coprire proprio il caso in cui un'istantanea immutabile è stata alterata.
    """
    version = bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_versions SET canonical_sha256 = "
                       "repeat('a', 64) WHERE version = :v"), {"v": version})
    with pytest.raises(ProjectionAborted) as err:
        rebuild(engine)
    assert err.value.reason == "digest_della_versione_incoerente"


def test_a_rebuild_without_an_inventory_refuses(db, engine):
    from app.inventory.errors import NotBootstrappedError
    with pytest.raises(NotBootstrappedError):
        rebuild(engine)
    assert status(engine).describe().startswith("nessuna versione in testa")


# ==================================================================
# 4. una proiezione vecchia si vede
# ==================================================================

def test_a_save_now_carries_the_projection_with_it(db, engine):
    """⚠ Il rovescio esatto del test che stava qui, e la fase 2C è quel rovescio.

    Fino alla 2B un `PUT` lasciava la proiezione indietro e l'unico requisito era che
    la cosa fosse VISIBILE. Adesso il salvataggio la porta con sé nella stessa
    transazione: la versione dichiarata, il digest dichiarato e il contenuto delle
    righe si muovono insieme alla testa.

    Si conserva la parte che continua a valere — il rack rinominato — ma con
    l'aspettativa invertita: dopo il salvataggio è la proiezione a dover contenere il
    nome NUOVO, senza che nessuno abbia eseguito `--rebuild`.
    """
    prima = bootstrap(engine, DOCUMENTS["base"])
    # ⚠ Nessun `rebuild` qui, e l'assenza è il punto: il bootstrap ha già scritto la
    # proiezione. Un database appena inizializzato è utilizzabile subito.
    assert status(engine).fresh

    dopo = save(engine, relbuild.variant_renamed())
    assert dopo > prima

    state = status(engine)
    assert state.fresh
    assert state.projected_version == dopo == state.head_version
    assert state.projected_sha256 == state.head_sha256
    assert state.behind == 0
    assert "aggiornata alla versione" in state.describe()

    # Le righe sono quelle nuove: il codice rinominato c'è.
    #
    # ⚠ Non si asserisce che «R01» sia sparito: l'unicità dei codici è AMBITO alla
    # sala, e la fixture ne ha più di una — un altro rack in un'altra sala si chiama
    # ancora R01, legittimamente. La prima stesura di questo test lo pretendeva e
    # falliva su un fatto del documento, non su un difetto della proiezione.
    with engine.begin() as c:
        codes = [r[0] for r in c.execute(text("SELECT code FROM inventory_racks")).all()]
    assert "R01-NUOVO" in codes

    # Fedele E attuale: in fase 2C `ok` pretende entrambe.
    result = verify(engine)
    assert result.faithful and result.current and result.ok


def test_the_projection_state_records_the_mapper_version(db, engine):
    """La ricevuta dice anche CON QUALE MAPPA è stata scritta.

    Serve perché il digest è cieco alla distribuzione dei dati: se un campo passasse
    da `extra` a una colonna tipizzata, le righe vecchie riassemblerebbero lo stesso
    documento — stesso digest, nessun allarme — e le query per cui la colonna esiste
    non troverebbero niente.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.begin() as c:
        recorded = c.execute(text(
            "SELECT mapper_version FROM inventory_projection_state")).scalar_one()
    assert recorded == MAPPER_VERSION
    assert status(engine).currency.mapper_supported

    save(engine, relbuild.variant_renamed())
    with engine.begin() as c:
        assert c.execute(text(
            "SELECT mapper_version FROM inventory_projection_state"
        )).scalar_one() == MAPPER_VERSION


def test_the_rebuild_holds_the_head_lock(db, engine):
    """⚠ «Atomicamente sotto la testa bloccata» deve significare qualcosa.

    Si apre la ricostruzione in una transazione e si prova a salvare da un'ALTRA
    connessione: il salvataggio deve aspettare. Con `lock_timeout` si trasforma
    l'attesa in un errore osservabile invece di un test che si blocca — e se il lock
    non ci fosse, il salvataggio passerebbe e questo test diventerebbe rosso.
    """
    bootstrap(engine, DOCUMENTS["base"])

    with engine.connect() as costruttore:
        with costruttore.begin():
            projection.rebuild(costruttore)          # lock preso, non ancora committato

            with engine.connect() as scrittore:
                with scrittore.begin():
                    scrittore.execute(text("SET LOCAL lock_timeout = '400ms'"))
                    repo = InventoryRepository(scrittore)
                    with pytest.raises(Exception) as err:
                        repo.save(1, relbuild.variant_renamed(), ADMIN)
            assert "lock" in str(err.value).lower()

    # Fuori dalla transazione il salvataggio riesce: era attesa, non un guasto.
    save(engine, relbuild.variant_renamed())


def test_a_concurrent_save_makes_the_rebuild_wait_its_turn(db, engine):
    """La controprova simmetrica: chi tiene il lock è un salvataggio, e la
    ricostruzione è quella che aspetta. Senza il lock la proiezione potrebbe
    rispecchiare una testa che è cambiata sotto di lei."""
    bootstrap(engine, DOCUMENTS["base"])
    with engine.connect() as scrittore:
        with scrittore.begin():
            repo = InventoryRepository(scrittore)
            repo.save(repo.head_version(), relbuild.variant_renamed(), ADMIN)

            with engine.connect() as costruttore:
                with costruttore.begin():
                    costruttore.execute(text("SET LOCAL lock_timeout = '400ms'"))
                    with pytest.raises(Exception) as err:
                        projection.rebuild(costruttore)
            assert "lock" in str(err.value).lower()


# ==================================================================
# 5. nessuno consuma la proiezione
# ==================================================================

def test_the_api_still_serves_the_json_snapshot(db, engine):
    """⚠ La prova più forte che `GET` non è cambiato: si CORROMPE la proiezione.

    Si cancella un sito dalle tabelle e si chiede l'inventario. Se la risposta fosse
    ancora completa per caso — perché la proiezione non è mai stata popolata, per
    esempio — il test non proverebbe niente: perciò prima si ricostruisce, si
    verifica che sia piena, e solo dopo la si rompe.
    """
    from app.api.deps import get_connection, require_actor
    from app.main import app
    from conftest import api_client

    bootstrap(engine, DOCUMENTS["seed"])
    rebuild(engine)
    with engine.begin() as c:
        assert projection.counts(c)["racks"] == 102

    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_locations"))
        assert projection.counts(c)["racks"] == 0

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    app.dependency_overrides[require_actor] = \
        lambda: Actor(username="lettore", role="view")
    try:
        with api_client(app) as client:
            r = client.get("/api/inventory")
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 200
    body = r.json()
    racks = [rack for loc in body["doc"]["locations"] for room in loc["sale"]
             for rack in room["racks"]]
    assert len(racks) == 102, "GET deve servire l'istantanea, non la proiezione"


def test_readiness_now_requires_a_current_projection(db, engine):
    """⚠ L'opposto di quello che valeva in fase 2B, e per una ragione precisa.

    In 2B la proiezione non era una condizione di readiness: farla diventare tale
    avrebbe impedito al servizio di partire perché una rappresentazione che nessuno
    legge non era aggiornata.

    Dalla 2C l'API PROMETTE di mantenerla a ogni salvataggio. Se non rispecchia la
    testa, quella promessa non è mantenibile e ogni `PUT` verrà rifiutato: un backend
    che risponde «pronto» e poi rifiuta tutte le scritture mente al reverse proxy.
    `GET` funzionerebbe ancora — legge il JSON — ed è proprio questo che renderebbe
    il guasto difficile da vedere: l'applicazione sembra viva e non si può salvare.
    """
    from app.main import app
    from conftest import api_client

    bootstrap(engine, DOCUMENTS["base"])
    with api_client(app) as client:
        # Il bootstrap ha già scritto la proiezione: pronto subito.
        assert status(engine).fresh
        r = client.get("/api/ready")
        assert r.status_code == 200 and r.json()["status"] == "ready"
        assert r.json()["projection"] == "ok"

        # Si svuota la proiezione da SOTTO, come farebbe un ripristino parziale.
        with engine.begin() as c:
            projection.clear(c)
        r = client.get("/api/ready")
        assert r.status_code == 503, r.text
        assert r.json()["projection"] == "not-ready"
        # Le altre tre condizioni restano OK: il rapporto dice QUALE manca.
        assert r.json()["database"] == "ok" and r.json()["inventory"] == "ok"

        # E torna pronta dopo la ricostruzione, senza riavviare niente.
        rebuild(engine)
        assert client.get("/api/ready").status_code == 200


def test_readiness_rejects_a_projection_written_by_another_mapper(db, engine):
    """Una mappa diversa è un guasto che il digest non vede.

    Le righe riassemblerebbero lo stesso documento e starebbero nelle colonne
    sbagliate. La readiness lo vede perché confronta un numero registrato, non il
    contenuto.
    """
    from app.main import app
    from conftest import api_client

    bootstrap(engine, DOCUMENTS["base"])
    with api_client(app) as client:
        assert client.get("/api/ready").status_code == 200
        with engine.begin() as c:
            c.execute(text("UPDATE inventory_projection_state "
                           "   SET mapper_version = mapper_version + 1"))
        r = client.get("/api/ready")
        assert r.status_code == 503
        assert r.json()["projection"] == "not-ready"


def test_readiness_does_not_reassemble_the_inventory(db, engine):
    """⚠ La readiness fa confronti STRUTTURALI, non un giro completo.

    Il rischio non è teorico: riassemblare l'inventario da SQL a ogni sonda
    costerebbe quanto un `--verify`, ripetuto ogni pochi secondi per sempre.

    Si prova dal COMPORTAMENTO e non leggendo il codice: si corrompe una RIGA della
    proiezione lasciando intatto lo stato (versione, digest, mappa). Chi riassembla se
    ne accorge; chi confronta i numeri registrati no — e deve restare pronto, perché
    la fedeltà non è la domanda della readiness.
    """
    from app.main import app
    from conftest import api_client

    bootstrap(engine, DOCUMENTS["base"])
    # Si corrompe `name` e una riga SOLA: `code` è sotto un vincolo di unicità
    # ambito alla sala, e riscriverlo su tutte le righe fallirebbe per il vincolo
    # invece di produrre lo stato che serve al test.
    with engine.begin() as c:
        c.execute(text("""
            UPDATE inventory_racks SET name = 'CORROTTO'
             WHERE uid = (SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)
        """))

    with api_client(app) as client:
        r = client.get("/api/ready")
        assert r.status_code == 200, "la readiness ha riassemblato: costa troppo"
        assert r.json()["projection"] == "ok"

    # E la fedeltà, quella, il guasto lo vede: è il mestiere di `--verify`.
    result = verify(engine)
    assert result.current and not result.faithful


def test_the_photo_gc_still_runs_with_a_populated_projection(db, engine):
    """`inventory_racks.photo_id` è una seconda chiave esterna su `photos`, senza
    `ON DELETE`. Una GC che tentasse di cancellare una foto ancora riferita dalla
    proiezione fallirebbe l'intera transazione: qui si prova che il giro normale
    resta verde con le tabelle piene, cioè che le due difese non si pestano i piedi.
    """
    from app.photos import gc as photo_gc

    payload = b"\x89PNG\r\n\x1a\n orfana"
    doc = relbuild.base()
    with engine.begin() as c:
        usata = c.execute(text("""
            INSERT INTO photos (mime, bytes, sha256, size_bytes)
            VALUES ('image/png', :b, :s, :n) RETURNING id
        """), {"b": payload, "s": hashlib.sha256(payload).hexdigest(),
               "n": len(payload)}).scalar_one()
        orfana = c.execute(text("""
            INSERT INTO photos (mime, bytes, sha256, size_bytes, created_at)
            VALUES ('image/png', :b, :s, :n, now() - interval '48 hours')
         RETURNING id
        """), {"b": payload + b"!", "s": hashlib.sha256(payload + b"!").hexdigest(),
               "n": len(payload) + 1}).scalar_one()

    doc["locations"][0]["sale"][0]["racks"][0]["foto"] = str(usata)
    bootstrap(engine, doc)
    rebuild(engine)

    esito = photo_gc.run_once(engine, now_utc=datetime.now(timezone.utc), force=True)
    assert esito.ran and esito.deleted == 1
    assert str(orfana) in esito.deleted_ids
    assert str(usata) not in esito.deleted_ids
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM photos WHERE id = :p"),
                         {"p": usata}).scalar_one() == 1
    assert verify(engine).ok


def test_the_worker_role_cannot_build_the_projection(db, engine):
    """Il worker non scrive la proiezione, e non può nemmeno provandoci.

    ⚠ Questo test valeva per ENTRAMBI i ruoli di runtime fino alla fase 2B. Adesso
    `tsm_api` la scrive per mestiere — è il senso della 2C — e la parametrizzazione è
    stata sciolta invece di essere allargata: due ruoli con la stessa aspettativa
    erano un test solo, due ruoli con aspettative opposte sono due test, e uno che
    dicesse «l'API può, il worker no» in una parametrizzazione booleana nasconderebbe
    quale dei due sta davvero verificando.

    Il worker resta in sola lettura perché le colonne data derivate esistono per le
    query e il passaggio dello scanner è una decisione successiva (§8.44).
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.connect() as c:
        with c.begin():
            c.execute(text("SET ROLE tsm_worker"))
            with pytest.raises(Exception) as err:
                projection.rebuild(c)
    assert "permission denied" in str(err.value).lower()

    # E la proiezione del bootstrap è INTATTA: il tentativo non ha rotto niente.
    assert verify(engine).ok


def test_the_api_role_can_maintain_the_projection_but_not_truncate_it(db, engine):
    """L'altra metà: l'API la scrive, e resta senza `TRUNCATE`.

    La 0010 aveva negato la scrittura scrivendo «i privilegi li concede la fase 2C,
    con il codice che li usa». Questo è quel codice, e questo è il test che la
    concessione sia esattamente quanto serve — non `ALL`.
    """
    bootstrap(engine, DOCUMENTS["base"])
    with engine.connect() as c:
        with c.begin():
            c.execute(text("SET ROLE tsm_api"))
            # Scrivere: si può. Si prova per davvero, non con
            # `has_table_privilege`: il privilegio dichiarato e il comportamento sono
            # due cose, e la seconda è quella che conta.
            c.execute(text("UPDATE inventory_racks SET code = code"))
            c.execute(text("DELETE FROM inventory_manual_entries"))
            c.execute(text("UPDATE inventory_projection_state "
                           "   SET synchronised_at = now()"))
        c.rollback()

    with engine.connect() as c:
        with c.begin():
            c.execute(text("SET ROLE tsm_api"))
            with pytest.raises(Exception) as err:
                c.execute(text("TRUNCATE inventory_locations"))
        c.rollback()
    assert "permission denied" in str(err.value).lower()

    # E l'istantanea immutabile resta immutabile anche adesso che l'API scrive la
    # proiezione: sono due tabelle e due contratti diversi.
    for statement in ("UPDATE inventory_versions SET canonical_sha256 = 'x'",
                      "DELETE FROM inventory_versions"):
        with engine.connect() as c:
            with c.begin():
                c.execute(text("SET ROLE tsm_api"))
                with pytest.raises(Exception) as err:
                    c.execute(text(statement))
            c.rollback()
        assert "permission denied" in str(err.value).lower(), statement
