"""Fase 2F: il worker prende le scadenze dalla PROIEZIONE, e nient'altro è cambiato.

Due affermazioni, e questo file esiste per provarle entrambe.

1. **PARITÀ.** Per lo stesso documento canonico, le stesse impostazioni e la stessa
   data di riferimento, la nuova sorgente SQL produce le stesse voci dovute che
   produceva `expiry.due_items(doc)`. L'oracolo è quella funzione, che è rimasta pura
   e non conosce il database (un test lo pretende in `test_get_from_sql_pg.py`):
   quindi il confronto è fra due implementazioni indipendenti, non fra
   un'implementazione e sé stessa. Sedici corpora per cinque insiemi di finestre.

2. **NIENTE ALTRO È CAMBIATO.** Questa metà non si prova qui: si prova col fatto che
   `test_worker_pg.py` — cinquantatré test su precedenza, recupero, soglie superate,
   idempotenza, `Message-ID`, cooldown, cinque tentativi, destinatari, cambio d'ora,
   audit, lock consultivo — è passato **senza una modifica**. Se avessi riscritto quei
   test per accomodare la migrazione, avrei perso l'unica prova che la consegna non è
   cambiata. Il posto in cui verificarlo è il `git diff` di quel file: vuoto.

Che cosa NON è l'endpoint
-------------------------
`GET /api/inventory/expiries` riproduce la vista Scadenze. Il worker segue
`due_items`. Sono diversi, e la §11 della fase 2F chiede di provare che lo restano:
c'è una famiglia di test che mette i due risultati uno accanto all'altro e pretende
che divergano dove sappiamo che divergono. Un test che li vedesse uguali significherebbe
che qualcuno ha cambiato la semantica di uno dei due.

PostgreSQL vero, sempre. Metà di ciò che c'è da dimostrare è comportamento del
database — l'isolamento dello snapshot, i privilegi del ruolo, l'uso degli indici
parziali, il rifiuto di una scrittura in `READ ONLY` — e nessun doppio proverebbe
niente di tutto questo.

Riferimento: BACKEND-PLAN.md §8.47, §8.48.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db import read_snapshot
from app.inventory import Actor, InventoryRepository
from app.inventory.errors import NotBootstrappedError, ProjectionNotCurrentError
from app.notifications import candidates
from app.notifications import worker as wk
from app.notifications.expiry import due_items
from app.settings.schema import DEFAULTS

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
ADMIN = Actor(username="capo", role="admin")
TODAY = date(2026, 8, 10)
WINDOWS = [90, 30, 7]
RECIPIENTS = ["uno@example.internal", "due@example.internal"]


def _load(name: str, relative: str):
    """Per PERCORSO e con un nome proprio: i generatori delle fixture si chiamano
    tutti `build.py` e `sys.modules` è condiviso da tutta la sessione di pytest,
    quindi un `import build` restituirebbe quello che un altro file ha già caricato."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_parity = _load("tsm_fixture_expiry_parity", "fixtures/expiry/parity.py")
CORPORA = _parity.corpora(TODAY)
WINDOW_SETS = _parity.WINDOW_SETS
NAMES = sorted(CORPORA)

#: Il seed di produzione: la forma e la scala che contano davvero. Un corpus costruito
#: a mano non ha né la profondità dell'albero né la distribuzione dei campi vuoti di un
#: inventario reale.
from app.inventory.document import strip_legacy_fields  # noqa: E402

SEED = strip_legacy_fields(
    json.loads((ROOT / "fixtures" / "seed.json").read_text(encoding="utf-8")))[0]
CORPORA["seed"] = SEED


def _seed_with_dates(reference: date) -> dict:
    """Il seed, con le scadenze aggiunte in modo DETERMINISTICO.

    ⚠ Perché serve, ed è una scoperta di questo commit: **il seed di produzione non ha
    nessuna scadenza**. Ottantasei dispositivi, e nessuno con `garanzia` o `supporto` —
    è il buco dei dati di seed che il piano registra da tempo (§7), e la conseguenza
    qui è che il worker non era mai stato messo alla prova su dati di forma reale: sul
    seed, «niente è dovuto» è l'unica risposta possibile, e un test di parità su quel
    corpus confronta due elenchi vuoti.

    Il corpus `seed` resta nel giro della parità — «nessuna data da nessuna delle due
    parti» è un caso vero e vale verificarlo alla scala di produzione — e questo lo
    accompagna con le date, distribuite come cadono in un CED reale: qualcuna scaduta,
    qualcuna dentro le finestre, la maggior parte lontana.

    Deterministico e non casuale: una fixture che cambia a ogni esecuzione dà test che
    passano o cadono per motivi che nessuno può ricostruire.
    """
    from copy import deepcopy

    doc = deepcopy(SEED)
    n = 0
    for L in doc.get("locations") or []:
        for R in L.get("sale") or []:
            for K in R.get("racks") or []:
                for V in K.get("devices") or []:
                    n += 1
                    # −40 … +160 giorni: copre scaduti, oggi, le tre finestre e il
                    # fuori-finestra, senza che nessun caso domini gli altri.
                    if n % 3 == 0:
                        V["garanzia"] = (reference + timedelta(
                            days=(n * 17) % 200 - 40)).isoformat()
                    if n % 5 == 0:
                        V["supporto"] = (reference + timedelta(
                            days=(n * 23) % 200 - 40)).isoformat()
                    # Qualche valore non interpretabile, che nell'inventario vero è la
                    # norma e non l'eccezione.
                    if n % 11 == 0:
                        V["garanzia"] = "da verificare"
    return doc


CORPORA["seed-dated"] = _seed_with_dates(TODAY)
NAMES = sorted(CORPORA)


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
    """Database pulito. Le tabelle della proiezione NON hanno una chiave esterna
    verso `inventory_versions`, quindi il `TRUNCATE ... CASCADE` non le porta via: si
    svuotano a mano, altrimenti resterebbero righe di un test precedente e la
    proiezione sembrerebbe attuale mentre descrive un altro inventario."""
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions, "
                       "reminders, reminder_deliveries, scheduler_runs "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM inventory_projection_state"))
        c.execute(text("DELETE FROM maintenance_runs"))
    set_settings(engine)
    yield engine


def set_settings(engine, *, enabled=True, recipients=None, windows=None,
                 tz="Europe/Rome", hour=8, minute=0):
    data = json.loads(json.dumps(DEFAULTS))
    data["notifications"].update({
        "enabled": enabled,
        "recipients": RECIPIENTS if recipients is None else recipients,
        "warningDays": WINDOWS if windows is None else windows,
        "timezone": tz,
        "schedule": {"hour": hour, "minute": minute},
    })
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb), "
                       "version = version + 1 WHERE id = 1"),
                  {"d": json.dumps(data)})


def bootstrap(engine, doc: dict) -> int:
    with engine.begin() as c:
        return InventoryRepository(c).bootstrap(doc, ADMIN).version


def save(engine, doc: dict):
    with engine.begin() as c:
        repo = InventoryRepository(c)
        return repo.save(repo.head_version(), doc, ADMIN)


def stored_document(engine) -> dict:
    """Il documento CANONICO in testa: è quello che il frontend vede e quello da cui
    la proiezione è stata costruita. La parità va misurata su questo, non sul
    documento della fixture: la canonicalizzazione RIEMPIE i campi (§8.14), e
    confrontare lo SQL con un JavaScript che ha girato su un documento che in
    produzione non esiste è il difetto del banco di prova che la fase 2E ha già
    pagato una volta."""
    with engine.begin() as c:
        return c.execute(text(
            "SELECT v.doc FROM inventory_head h "
            "  JOIN inventory_versions v ON v.version = h.version "
            " WHERE h.id IS TRUE")).scalar_one()


def sql(engine, statement: str, **params) -> None:
    """Una scrittura da PROPRIETARIO dello schema: è così che si simula il DBA con le
    mani in pasta. Il worker non potrebbe farne nessuna — la matrice dei privilegi in
    `test_photos_api_pg.py` lo pretende — e la corruzione che questi test scoprono
    arriva da fuori dall'applicazione."""
    with engine.begin() as c:
        c.execute(text(statement), params)


def from_projection(*, today=TODAY, windows=None) -> candidates.Candidates:
    with read_snapshot() as snap:
        return candidates.due_items_from_projection(
            snap, today=today, warning_days=WINDOWS if windows is None else windows)


def identity(items) -> list[tuple]:
    """Identità immutabile + ciclo di vita della scadenza. È il confronto che la §9
    della fase 2F chiede: `_uid`, tipo, data, giorni. NON l'etichetta, che è
    presentazione, e NON l'ordine in cui il documento è scritto."""
    return [(i.entity_uid, i.kind, i.expiry, i.days_remaining) for i in items]


def labels(items) -> dict:
    return {(i.entity_uid, i.kind): (i.device, i.rack, i.room, i.location)
            for i in items}


# ==================================================================
# 1. parità con `due_items`: il test che porta il peso
# ==================================================================

@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("windows", WINDOW_SETS, ids=lambda w: f"finestre{w}")
def test_the_sql_source_matches_the_document_scanner(db, engine, name, windows):
    """L'affermazione centrale della fase 2F, per ogni corpus e ogni insieme di soglie.

    Si confronta l'IDENTITÀ e il ciclo di vita: `_uid`, tipo di scadenza, data,
    giorni rimanenti. È la definizione di «stessa voce dovuta» — due voci con lo
    stesso `_uid` e la stessa data sono la stessa cosa anche se il nome del
    dispositivo è cambiato, e due voci con lo stesso nome e `_uid` diversi non lo
    sono mai.

    L'ordine si confronta anche lui, ed è più di quanto la §9 chieda: `due_items`
    ordina su una chiave totale, e due giri sullo stesso inventario devono produrre
    lo stesso digest riga per riga. Se un giorno l'ordine divergesse, il digest
    cambierebbe senza che l'inventario sia cambiato.
    """
    bootstrap(engine, CORPORA[name])
    atteso = due_items(stored_document(engine), today=TODAY,
                       warning_days=list(windows))
    ottenuto = from_projection(windows=list(windows)).items
    assert identity(ottenuto) == identity(atteso), (
        f"corpus {name}, finestre {windows}: "
        f"solo oracolo {sorted(set(identity(atteso)) - set(identity(ottenuto)))}, "
        f"solo SQL {sorted(set(identity(ottenuto)) - set(identity(atteso)))}")


@pytest.mark.parametrize("name", [n for n in NAMES if n != "context"])
def test_the_structural_context_matches_too(db, engine, name):
    """Nome del dispositivo, rack, sala e sito: identici all'oracolo.

    Il contesto non fa parte del confronto che la §9 pretende — è presentazione, non
    identità — ma è ciò che finisce nel corpo dell'avviso, e una posizione sbagliata
    in un digest è un avviso che manda una persona nella sala sbagliata.

    Il corpus `context` è escluso e ha un test suo: contiene l'unica divergenza
    voluta della fase 2F, e un `parametrize` che salta silenziosamente il caso
    interessante è un test che passa per il motivo sbagliato.
    """
    bootstrap(engine, CORPORA[name])
    atteso = labels(due_items(stored_document(engine), today=TODAY,
                              warning_days=WINDOWS))
    ottenuto = labels(from_projection().items)
    assert ottenuto == atteso


def test_a_numeric_device_name_survives_because_extra_is_read(db, engine):
    """`name: 42` non è una stringa: sta in `extra`, non nella colonna.

    Guardare solo `inventory_devices.name` avrebbe fatto sparire quel nome e mostrato
    l'id al suo posto. È una divergenza invisibile in ogni inventario ben formato e
    visibile in quelli importati da un foglio di calcolo — cioè quasi tutti. Il test
    fissa il valore invece di limitarsi al confronto con l'oracolo, così se un domani
    `extra` smettesse di essere letto si vedrebbe da qui che cosa si perde.
    """
    bootstrap(engine, CORPORA["labels"])
    with engine.begin() as c:
        riga = c.execute(text(
            "SELECT name, code, extra -> 'name' AS extra_name FROM inventory_devices "
            " WHERE code = 'numerico'")).mappings().one()
    assert riga["name"] is None, "la colonna deve essere NULL: 42 non è testo"
    assert riga["extra_name"] == 42

    per_uid = {i.entity_uid: i.device for i in from_projection().items}
    numerico = [d for u, d in per_uid.items() if d == "42"]
    assert numerico == ["42"], f"il nome numerico non è arrivato: {sorted(per_uid.values())}"


@pytest.mark.parametrize("code,atteso", [
    ("con-nome", "Il Nome"),
    ("solo-id", "solo-id"),          # `name` assente → l'id
    ("nome-vuoto", "nome-vuoto"),    # `name` vuoto → l'id (la stringa vuota è falsa)
    ("numerico", "42"),
    ("zero", "zero"),                # `name: 0` è falso → l'id
    ("falso", "falso"),              # `name: False` è falso → l'id
    ("lista", "['a', 'b']"),
    ("dizionario", "{'x': 1}"),
])
def test_the_device_label_chain_branch_by_branch(db, engine, code, atteso):
    """`obj.get("name") or obj.get("id") or "(senza nome)"`, ramo per ramo.

    I valori sono fissati a mano, non presi dall'oracolo: qui il punto è che la
    catena resti QUELLA, e un confronto con l'oracolo passerebbe anche se entrambi
    cambiassero nello stesso modo.
    """
    bootstrap(engine, CORPORA["labels"])
    per_codice = {}
    with engine.begin() as c:
        for r in c.execute(text("SELECT uid, code FROM inventory_devices")).all():
            per_codice[r[1]] = str(r[0])
    uid = per_codice[code]
    trovati = {i.entity_uid: i.device for i in from_projection().items}
    assert trovati[uid] == atteso


def test_a_device_without_name_and_without_id_is_unnamed(db, engine):
    """Ultimo anello della catena: «(senza nome)». Due dispositivi lo raggiungono per
    strade diverse — nessuna delle due chiavi, e un id vuoto — e finiscono entrambi
    con la stessa etichetta. Non è un problema: l'identità è l'`_uid`, e due avvisi
    su «(senza nome)» restano due entità distinte."""
    bootstrap(engine, CORPORA["labels"])
    senza = [i for i in from_projection().items if i.device == candidates.NO_NAME]
    assert len(senza) == 2
    assert len({i.entity_uid for i in senza}) == 2


# ==================================================================
# 2. l'unica divergenza voluta: gli id con uno `/` dentro
# ==================================================================

def test_a_slash_in_a_code_is_no_longer_truncated(db, engine):
    """⚠ LA divergenza deliberata della fase 2F, con entrambi i valori fissati.

    `walk` componeva il contesto come UNA stringa —
    `f"{L['id']} / {R['id']} / {K['id']} / {V['id']}"` — e `_context` la rispezzava su
    ogni `/`. Un id che contiene uno `/` rompeva quel giro:

      - un rack `10.0.0.0/24` arrivava nel digest come `10.0.0.0`;
      - uno `/` nel SITO spostava tutto di un posto, e il campo «rack» dell'avviso
        finiva per contenere il nome della SALA.

    La JOIN ha il valore intero e lo restituisce intero. Riprodurre il troncamento
    avrebbe voluto dire scrivere codice nuovo il cui unico scopo è corrompere un
    valore che il database ha già giusto.

    Questo test fissa ENTRAMBI i valori — quello che l'oracolo produce e quello che
    la proiezione produce — così la differenza sta in un file di test invece che in
    una frase di un rapporto. Non cambia MAI quali scadenze sono dovute: la
    §1 lo verifica separatamente, corpus per corpus.
    """
    bootstrap(engine, CORPORA["context"])
    vecchio = labels(due_items(stored_document(engine), today=TODAY,
                               warning_days=WINDOWS))
    nuovo = labels(from_projection().items)

    # Le voci sono le stesse: la divergenza è solo nel testo della posizione.
    assert set(vecchio) == set(nuovo)

    per_nome_vecchio = {v[0]: v for v in vecchio.values()}
    per_nome_nuovo = {v[0]: v for v in nuovo.values()}

    # Rack con uno `/`: troncato prima, intero adesso.
    assert per_nome_vecchio["rack-con-slash"][1] == "10.0.0.0"
    assert per_nome_nuovo["rack-con-slash"][1] == "10.0.0.0/24"

    # Sito con uno `/`: tutte le parti scalate di uno. Il campo «rack» conteneva la
    # SALA, e il campo «sala» conteneva la seconda metà del codice del sito.
    assert per_nome_vecchio["sito-con-slash"][1:] == ("sala-5", "b", "a")
    assert per_nome_nuovo["sito-con-slash"][1:] == ("R05", "sala-5", "a/b")

    # Tutto il resto combacia: la divergenza è confinata agli id che CONTENGONO
    # uno `/`. Il filtro guarda il valore NUOVO, non il vecchio: il vecchio è quello
    # troncato, quindi non contiene più lo `/` e si escluderebbe da solo — un filtro
    # sul vecchio avrebbe reso il ciclo vacuo proprio sui casi interessanti.
    intatti = {k for k, v in nuovo.items() if "/" not in "".join(map(str, v))}
    assert len(intatti) == 3, f"il filtro esclude troppo: {sorted(intatti)}"
    for k in intatti:
        assert vecchio[k] == nuovo[k]


def test_a_missing_code_is_still_the_string_none(db, engine):
    """`id` assente → l'etichetta è la stringa «None», come prima.

    `id` non è obbligatorio nello schema del documento, e `walk` lo interpolava in una
    f-string: un rack senza id mostrava «None» nell'avviso. Non è un difetto che la
    fase 2F corregge — correggerlo cambierebbe il testo di un avviso reale senza che
    nessuno l'abbia chiesto — ma è un difetto, e sta nel registro (§8.48).
    """
    bootstrap(engine, CORPORA["context"])
    nuovo = {v[0]: v for v in labels(from_projection().items).values()}
    assert nuovo["rack-senza-id"][1] == "None"
    assert nuovo["sala-senza-id"][2] == "None"
    # Un id numerico invece diventa il suo `str()`, in entrambe le implementazioni.
    assert nuovo["sala-senza-id"][3] == "3"


# ==================================================================
# 3. la sorgente è davvero SQL
# ==================================================================

def test_corrupting_the_derived_columns_changes_what_the_worker_finds(db, engine):
    """La prova che la sorgente è cambiata davvero, fatta sul COMPORTAMENTO.

    Si azzerano le colonne data lasciando il documento intatto. Prima della fase 2F il
    worker non se ne accorgeva — leggeva il testo — e un test statico su `expiry.py`
    faceva da allarme. Adesso deve accorgersene: nessuna candidata.

    ⚠ E qui c'è un limite dichiarato. Il controllo di attualità confronta versione,
    digest e versione della mappa: nessuno dei tre cambia se si corrompe una colonna
    DERIVATA, perché quelle colonne non entrano nel documento riassemblato e quindi
    non entrano nel digest. È lo stesso punto cieco trovato in fase 2B. Chi lo vede è
    `GET /api/inventory` (che valida il modello, e risponde 503) e `project.py
    --verify`; il worker no, di proposito: validare il modello vuol dire leggere
    l'intera proiezione, cioè ricreare la scansione completa che la §3 della fase 2F
    chiede di NON ricreare. La conseguenza operativa è nel registro (§8.48).
    """
    bootstrap(engine, CORPORA["windows"])
    assert from_projection().items, "la fixture non produce nessuna candidata"

    sql(engine, "UPDATE inventory_devices SET garanzia_date = NULL, "
                "                             supporto_date = NULL")
    assert from_projection().items == [], \
        "il worker non legge le colonne derivate: la fase 2F non è avvenuta"

    # L'oracolo, che legge il testo, continua a trovarle: è la dimostrazione che le
    # due implementazioni guardano posti diversi.
    assert due_items(stored_document(engine), today=TODAY, warning_days=WINDOWS)


def test_the_worker_never_reads_the_immutable_snapshot(db, engine):
    """Nessun percorso del worker legge `inventory_versions.doc`.

    Controllo statico sui due moduli, e serve perché il ripiego sarebbe la reazione
    istintiva il giorno in cui la proiezione dà un problema in produzione: si
    leggerebbe il documento «solo per stavolta», e il difetto di coerenza che la fase 2
    esiste per scoprire tornerebbe invisibile (§8.45).

    `canonical_sha256` della versione in testa è un'altra cosa e resta ammesso: è un
    METADATO, ed è il giudice del confronto. Chi ha in mano il digest può verificare;
    chi ha in mano il documento può restituirlo, e allora prima o poi lo restituirà.
    """
    for modulo in (wk, candidates):
        codice = _executable_source(modulo)
        assert "get_current" not in codice, f"{modulo.__name__} legge l'istantanea"
        assert "InventoryRepository" not in codice, \
            f"{modulo.__name__} usa il repository del documento"
        assert "inventory_versions" not in codice, \
            f"{modulo.__name__} nomina la tabella delle istantanee"


def test_the_worker_does_not_call_the_http_endpoint(db, engine):
    """Il worker non parla HTTP con sé stesso (§8 della fase 2F).

    L'endpoint `/api/inventory/expiries` riproduce la vista Scadenze, che non è la
    semantica del worker; e comunque un processo del backend che si collega al proprio
    server HTTP si porterebbe dietro autenticazione, rete e stati di errore per leggere
    da un database su cui è già collegato.
    """
    for modulo in (wk, candidates):
        codice = _executable_source(modulo)
        for vietato in ("httpx", "requests", "urllib", "http://", "https://",
                        "app.inventory.queries", "inventory/expiries",
                        "queries.expiries"):
            assert vietato not in codice, \
                f"{modulo.__name__} sembra chiamare un endpoint: {vietato!r}"


def test_the_query_asks_only_for_the_candidate_window(db, engine):
    """Non si legge tutta la proiezione per rifare la scansione del documento (§3).

    Si verifica sul PIANO, non sul codice: il piano di una finestra da 7 giorni deve
    scartare le righe fuori finestra nel database. La forma che lo dimostra è il
    filtro sulla colonna data — con un `OR` fra le due colonne PostgreSQL non potrebbe
    usare nessuno dei due indici parziali, e questo test diventerebbe rosso.
    """
    bootstrap(engine, CORPORA["windows"])
    piano = _explain(engine, windows=[7])
    assert "garanzia_date" in piano and "supporto_date" in piano
    # Nessuna scansione dell'istantanea, e nessuna delle tabelle che non servono.
    assert "inventory_versions" not in piano
    assert "inventory_manual_entries" not in piano


def _explain(engine, *, windows=None, today=TODAY, analyze=False) -> str:
    giorni = WINDOWS if windows is None else windows
    verbo = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN"
    with read_snapshot() as snap:
        righe = snap.execute(
            text(f"{verbo} {candidates._CANDIDATES_SQL}"),
            {"today": today, "until": today + timedelta(days=max(giorni))}).all()
    return "\n".join(r[0] for r in righe)


def test_the_partial_date_indexes_are_used_when_they_help(db, engine):
    """Gli indici parziali della 0011 sono quelli giusti, e si usano quando conviene.

    ⚠ Una tabella grande NON basta: serve una finestra SELETTIVA. La prima stesura
    duplicava i dispositivi conservandone le date, quindi la finestra da 7 giorni
    continuava a prendere un terzo delle righe e il pianificatore restava — con
    ragione — sulla scansione sequenziale. La misura diceva «l'indice non serve», che
    era vero per quella distribuzione e falso in generale. Qui le copie hanno date
    sparse su una decina d'anni, che è come cadono le scadenze in un inventario reale.

    Serve a rispondere alla §12 con una misura invece che con una dichiarazione:
    nessun indice NUOVO è stato aggiunto, perché quelli che servono esistono dalla
    0011 e questi due test dimostrano che il pianificatore li trova.
    """
    bootstrap(engine, CORPORA["windows"])
    _gonfia(engine, copie=60)
    sql(engine, "ANALYZE inventory_devices")
    piano = _explain(engine, windows=[7])
    assert "ix_device_garanzia_date" in piano, piano
    assert "ix_device_supporto_date" in piano, piano


def test_the_partial_indexes_are_used_at_production_scale_too(db, engine):
    """Sul seed di produzione il pianificatore usa già gli indici parziali.

    ⚠ Avevo scritto il contrario, e la misura mi ha corretto. Il ragionamento
    sbagliato era: «duecento righe stanno in una pagina, quindi la scansione
    sequenziale è più economica». Vale per un indice sull'INTERA tabella; questi sono
    **parziali** (`WHERE garanzia_date IS NOT NULL`, migrazione 0011), e nel seed la
    grande maggioranza dei dispositivi non ha date. L'indice contiene quindi una
    manciata di voci, e leggerlo costa meno che leggere tutte le righe — anche a
    duecento righe.

    È il motivo per cui la §12 chiede una misura e non un'intuizione, e il motivo per
    cui la 0011 aveva fatto la scelta giusta: `postgresql_where` non è un dettaglio di
    forma, è ciò che rende questi indici convenienti alla scala che abbiamo davvero.
    """
    bootstrap(engine, CORPORA["seed"])
    sql(engine, "ANALYZE inventory_devices")
    piano = _explain(engine)
    assert "ix_device_garanzia_date" in piano or \
           "ix_device_supporto_date" in piano, piano
    assert "inventory_versions" not in piano


def _gonfia(engine, *, copie: int) -> None:
    """Duplica i dispositivi con `_uid` nuovi e date SPARSE, nella proiezione.

    Si scrive nelle tabelle e non nel documento di proposito: serve una tabella grande
    per il pianificatore, non un inventario grande da salvare — e passare per il
    salvataggio costerebbe minuti. La proiezione risulta incoerente col documento, e
    va bene: questi test misurano il PIANO della query, non la coerenza, che ha i suoi.

    ⚠ Le date delle copie si spostano di `n × 40` giorni. Conservarle avrebbe prodotto
    una tabella grande e una finestra NON selettiva — un terzo delle righe dentro i
    sette giorni — e il pianificatore avrebbe (giustamente) continuato a scandire
    tutto. Sparpagliarle su una decina d'anni riproduce la distribuzione di un
    inventario reale, dove le scadenze non cadono tutte nella stessa settimana.
    """
    # UNA sola istruzione, e non un ciclo: la `SELECT` di un `INSERT ... SELECT` vede
    # lo stato PRIMA dell'istruzione, quindi non copia le proprie copie. Il ciclo
    # invece ripescava le righe già inserite e collideva su `uq_device_ordinal` — cioè
    # il test cadeva per un difetto del test.
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO inventory_devices
                (uid, rack_uid, ordinal, code, name, garanzia, supporto,
                 garanzia_date, supporto_date, extra)
            SELECT gen_random_uuid(), d.rack_uid, d.ordinal + g.n * 1000,
                   d.code || '-' || g.n, d.name, d.garanzia, d.supporto,
                   d.garanzia_date + g.n * 40, d.supporto_date + g.n * 40, d.extra
              FROM inventory_devices d, generate_series(1, :copie) AS g(n)
        """), {"copie": copie})


# ==================================================================
# 4. le divergenze fra worker e vista Scadenze, provate (§11)
# ==================================================================

def test_the_worker_still_reminds_about_decommissioned_devices(db, engine):
    """⚠ Divergenza DELIBERATA: la vista salta i dismessi, il worker no.

    `due_items` scorre `walk(doc)` e non guarda `stato`. Una macchina dismessa con la
    garanzia in scadenza ha sempre prodotto un promemoria, e la fase 2F conserva quel
    comportamento invece di «migliorarlo»: se un domani si decide che i dismessi non
    devono più produrre avvisi, è una scelta di prodotto con la sua riga nel registro,
    non l'effetto collaterale di una migrazione tecnica.

    Il test mette i due risultati uno accanto all'altro e pretende che divergano. Se
    diventassero uguali, qualcuno ha cambiato la semantica di uno dei due.
    """
    from app.inventory import queries

    bootstrap(engine, CORPORA["decommissioned"])
    dal_worker = {i.device for i in from_projection().items}
    with read_snapshot() as snap:
        vista = queries.expiries(snap, today=TODAY, warning_days=7)
    dalla_vista = {v["device"]["name"] for v in vista.items}

    assert "dismesso" in dal_worker, "il worker deve continuare a ricordarli"
    assert "dismesso" not in dalla_vista, "la vista deve continuare a saltarli"
    # Tutto il resto combacia: la divergenza è solo sui dismessi.
    assert dal_worker - dalla_vista == {"dismesso"}


def test_the_worker_ignores_expired_items_and_the_view_lists_them(db, engine):
    """⚠ Divergenza DELIBERATA: la vista elenca gli scaduti, il worker li salta.

    Un avviso per una scadenza già passata è un prodotto diverso — si ripete ogni
    giorno per sempre, o non si ripete? — e nessuna fase della 2 lo decide. Restano
    visibili nella vista Scadenze, che è dove si guardano.
    """
    from app.inventory import queries

    bootstrap(engine, CORPORA["expired"])
    dal_worker = {(i.device, i.kind) for i in from_projection().items}
    with read_snapshot() as snap:
        vista = queries.expiries(snap, today=TODAY, warning_days=7)
    scaduti_in_vista = {(v["device"]["name"], v["kind"]) for v in vista.items
                        if v["level"] == "expired"}

    assert scaduti_in_vista, "la vista deve elencare gli scaduti"
    assert not (dal_worker & scaduti_in_vista), \
        "il worker non deve mandare avvisi su scadenze già passate"
    # Il dispositivo con la garanzia scaduta e il supporto in finestra è dovuto per
    # metà: un filtro per DISPOSITIVO invece che per (dispositivo, tipo) sbaglierebbe
    # qui, e solo qui.
    assert ("misto", "supporto") in dal_worker
    assert ("misto", "garanzia") not in dal_worker


@pytest.mark.parametrize("codice", [
    "senza-zeri",    # 2027-3-15      → il frontend sì, entrambi i backend no
    "slash",         # 2027/03/15
    "lungo",         # March 15, 2027
    "iso-ora",       # 2027-03-15T10:00:00Z
    "parziale",      # 2027-03
    "anno",          # 2027
    "feb-30",        # V8 lo fa scorrere al 2 marzo; `parse_expiry` lo rifiuta
])
def test_the_forms_only_the_frontend_understands_reach_neither_backend(db, engine,
                                                                      codice):
    """Le forme che `new Date(v)` accetta e `parse_expiry` no.

    Sono le otto misurate in fase 2E (§8.46). Il punto della fase 2F è che il worker e
    l'endpoint le trattano allo STESSO modo — entrambi sulle colonne derivate, quindi
    entrambi sull'unico parser — mentre la vista del frontend le mostra. La divergenza
    è fra frontend e backend, non fra worker ed endpoint, ed è nel registro.

    Non si è aggiunto un secondo interprete di date: due idee di «data valida»
    divergono, e divergono proprio sui casi limite.
    """
    bootstrap(engine, CORPORA["broken-dates"])
    with engine.begin() as c:
        riga = c.execute(text(
            "SELECT garanzia, garanzia_date FROM inventory_devices "
            " WHERE code = :c"), {"c": codice}).mappings().one()
    assert riga["garanzia"], "il testo grezzo resta nell'inventario"
    assert riga["garanzia_date"] is None, "nessuna data derivata"
    assert codice not in {i.device for i in from_projection(windows=[3650]).items}


def test_whitespace_around_an_iso_date_is_still_a_date(db, engine):
    """`parse_expiry` fa `.strip()`, quindi «  2026-08-15  » È una data.

    Sembra un dettaglio e non lo è: è il caso che distingue «il parser è quello» da
    «il parser somiglia a quello». Un'implementazione scritta a mano avrebbe quasi
    certamente sbagliato qui.
    """
    bootstrap(engine, CORPORA["broken-dates"])
    dovuti = {i.device for i in from_projection().items}
    assert "spazi" in dovuti


def test_a_non_string_expiry_yields_no_date_from_either_side(db, engine):
    """Una `garanzia` che non è testo: colonna NULL da una parte, `None` dall'altra.

    Le due strade arrivano allo stesso posto per ragioni diverse — la mappa relazionale
    scarta il valore perché non ci sta nella colonna, `parse_expiry` lo scarta perché
    non è una stringa — e il test lo verifica invece di presumerlo. Se una delle due
    ragioni cambiasse, la parità si romperebbe qui.
    """
    bootstrap(engine, CORPORA["broken-dates"])
    with engine.begin() as c:
        for codice in ("numero", "lista"):
            riga = c.execute(text(
                "SELECT garanzia, garanzia_date, extra -> 'garanzia' AS grezzo "
                "  FROM inventory_devices WHERE code = :c"),
                {"c": codice}).mappings().one()
            assert riga["garanzia"] is None
            assert riga["garanzia_date"] is None
            assert riga["grezzo"] is not None, "il valore si conserva in extra"


# ==================================================================
# 5. la proiezione deve essere attuale: si fallisce CHIUSO (§13)
# ==================================================================

def _guasta(engine, come: str) -> None:
    """Rende la proiezione non attuale in uno dei quattro modi possibili."""
    if come == "assente":
        sql(engine, "DELETE FROM inventory_projection_state")
    elif come == "versione":
        # La versione deve ESISTERE: c'è una chiave esterna verso `inventory_versions`,
        # e un numero inventato darebbe un errore di vincolo invece della condizione
        # che si vuole provare. Quindi si sale di una versione e si riporta lo stato
        # indietro a quella prima.
        with engine.begin() as c:
            prima = c.execute(text(
                "SELECT version, canonical_sha256 FROM inventory_versions "
                " ORDER BY version LIMIT 1")).one()
        sql(engine, "UPDATE inventory_projection_state "
                    "   SET head_version = :v, head_sha256 = :s",
            v=int(prima[0]), s=prima[1])
    elif come == "digest":
        sql(engine, "UPDATE inventory_projection_state SET head_sha256 = :s",
            s="0" * 64)
    elif come == "mappa":
        sql(engine, "UPDATE inventory_projection_state SET mapper_version = 999")
    else:                                                # pragma: no cover
        raise AssertionError(come)


@pytest.mark.parametrize("come", ["assente", "versione", "digest", "mappa"])
def test_the_source_refuses_a_projection_that_is_not_current(db, engine, come):
    """Le quattro condizioni della §4, una per volta.

    Fallire chiuso e non ripiegare è la scelta: un ripiego su
    `inventory_versions.doc` funzionerebbe, nessuno aprirebbe un ticket, e il difetto
    di coerenza resterebbe lì fino al giorno in cui qualcuno lo scopre da solo.
    """
    bootstrap(engine, CORPORA["windows"])
    save(engine, CORPORA["kinds"])            # serve una seconda versione a «versione»
    _guasta(engine, come)
    with pytest.raises(ProjectionNotCurrentError):
        from_projection()


def test_an_uninitialised_inventory_is_not_a_broken_projection(db, engine):
    """Nessuna testa → `NotBootstrappedError`, non «proiezione non attuale».

    Sono due stati operativi diversi: uno si risolve col bootstrap, l'altro con un
    `--rebuild`, e il worker li registra con esiti diversi. Confonderli manderebbe
    un operatore a eseguire il comando sbagliato.
    """
    with pytest.raises(NotBootstrappedError):
        from_projection()


@pytest.mark.parametrize("come", ["assente", "versione", "digest", "mappa"])
def test_a_run_on_a_broken_projection_sends_nothing_and_stays_retryable(
        db, engine, smtp, come):
    """§13 per intero, e la riga che conta è l'ultima.

    Atteso: nessun invio, nessun promemoria, nessuna soglia superata, nessuna consegna,
    e il giro di oggi **non concluso** — perché «proiezione non attuale» non è «niente
    è dovuto». Concluderlo con un esito sarebbe stato più ordinato da leggere e avrebbe
    perso la giornata: `claim_run` avrebbe risposto «già eseguito oggi» fino a
    mezzanotte, e l'avviso sarebbe arrivato un giorno tardi (o mai, se la proiezione
    resta rotta più a lungo).
    """
    bootstrap(engine, CORPORA["windows"])
    save(engine, CORPORA["kinds"])
    _guasta(engine, come)

    risultato = wk.run_once(engine, now_utc=_at(TODAY))
    assert risultato.reason == "projection_not_current"
    assert risultato.sent == 0
    assert smtp.sent == []
    assert _reminders(engine) == []
    assert _deliveries(engine) == []
    #: Il battito NON deve avanzare: `last_run_date` è ciò che il monitoraggio guarda,
    #: e dichiarare un giro fatto senza aver guardato l'inventario è la bugia che la
    #: §13 vieta.
    assert risultato.run_date is None
    riga = _run_row(engine, TODAY)
    assert riga is not None, "la prenotazione del giro deve esserci"
    assert riga["finished_at"] is None, "il giro NON deve risultare concluso"
    assert riga["outcome"] is None


def test_the_run_recovers_by_itself_once_the_projection_is_repaired(db, engine, smtp):
    """Il giro perso si riprende, senza intervento e senza aspettare domani.

    È la conseguenza di non aver concluso la riga: al tick successivo `claim_run` la
    ritrova senza `finished_at` e riparte. La riparazione qui è un `--rebuild`, cioè
    esattamente il rimedio che il messaggio d'errore indica.
    """
    from app.inventory import projection

    bootstrap(engine, CORPORA["windows"])
    _guasta(engine, "mappa")
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "projection_not_current"
    assert smtp.sent == []

    with engine.begin() as c:
        projection.rebuild(c)

    risultato = wk.run_once(engine, now_utc=_at(TODAY, hour_utc=8))
    assert risultato.reason == "sent", risultato.reason
    assert len(smtp.sent) == 1
    assert _run_row(engine, TODAY)["outcome"] == "sent"


def test_an_unreachable_database_does_not_become_nothing_due(db, engine, smtp,
                                                             monkeypatch):
    """Database irraggiungibile → il giro solleva, e NON dichiara «niente da fare».

    Si rompe solo l'engine di LETTURA: così la prenotazione del giro riesce e il
    guasto cade esattamente dove la fase 2F ha messo la lettura nuova. Il ciclo del
    worker cattura l'eccezione e scrive `state="error"` nel battito (è già così per
    qualunque guasto del database); ciò che questo test pretende è che non venga
    inventato un esito positivo.
    """
    import app.db as dbmod
    rotto = create_engine(
        "postgresql+psycopg://tsm:testpw@127.0.0.1:1/nessuno",
        connect_args={"connect_timeout": 1})
    monkeypatch.setattr(dbmod, "_read_engine", rotto)

    bootstrap(engine, CORPORA["windows"])
    with pytest.raises(Exception):
        wk.run_once(engine, now_utc=_at(TODAY))

    assert smtp.sent == []
    assert _reminders(engine) == []
    riga = _run_row(engine, TODAY)
    assert riga is None or riga["finished_at"] is None
    rotto.dispose()


# ==================================================================
# 6. la revisione non deve muoversi sotto il calcolo (§5)
# ==================================================================

def test_the_candidates_carry_the_revision_they_came_from(db, engine):
    """Versione e digest viaggiano coi candidati, e sono quelli della testa."""
    versione = bootstrap(engine, CORPORA["windows"])
    trovati = from_projection()
    with engine.begin() as c:
        atteso = c.execute(text(
            "SELECT canonical_sha256 FROM inventory_versions WHERE version = :v"),
            {"v": versione}).scalar_one()
    assert trovati.version == versione
    assert trovati.sha256 == atteso


def test_unchanged_is_true_only_while_the_revision_holds(db, engine):
    """Il guardiano, provato in isolamento e nei due sensi."""
    bootstrap(engine, CORPORA["windows"])
    trovati = from_projection()
    with engine.begin() as c:
        assert candidates.unchanged(c, version=trovati.version,
                                    sha256=trovati.sha256)

    save(engine, CORPORA["kinds"])
    with engine.begin() as c:
        assert not candidates.unchanged(c, version=trovati.version,
                                        sha256=trovati.sha256), \
            "un salvataggio deve invalidare i candidati"

    # E cade anche se la proiezione smette di essere attuale senza che la testa si
    # muova: fra i due momenti può essere partito un `--rebuild`, e i candidati non
    # sono più fondati nemmeno allora.
    aggiornati = from_projection()
    _guasta(engine, "mappa")
    with engine.begin() as c:
        assert not candidates.unchanged(c, version=aggiornati.version,
                                        sha256=aggiornati.sha256)


def test_the_guard_compares_the_digest_and_not_only_the_version(db, engine):
    """Il confronto sul DIGEST non è ridondante, e questo test è la ragione.

    ⚠ Scoperto da una mutazione. Togliendo `found.head_sha256 == sha256` dal guardiano
    tutti i test restavano verdi, e la spiegazione comoda era «mutante equivalente»:
    `inventory_head.version` punta a una riga immutabile di `inventory_versions`, quindi
    versione uguale implica digest uguale, e il confronto sul digest non può mai
    aggiungere niente.

    Il ragionamento è giusto **finché l'immutabilità tiene**. E l'immutabilità la
    impone un privilegio (nessun ruolo di runtime ha `UPDATE` su `inventory_versions`),
    non una legge di natura: un ripristino parziale, un `UPDATE` a mano del
    proprietario, un guasto del supporto la violano. Quando è violata, il digest è
    l'unica cosa che resta a dirlo — la versione, per definizione, non si è mossa.

    Quindi si costruisce lo stato: si cambia il digest registrato della versione in
    testa **e** quello dichiarato dalla proiezione, allo stesso valore falso. La
    proiezione resta «attuale» (i due si corrispondono), la versione è la stessa, e
    l'unica differenza è col digest che i candidati portano con sé. Solo la riga
    mutata la vede.
    """
    from app.inventory import projection

    bootstrap(engine, CORPORA["windows"])
    trovati = from_projection()

    sql(engine, "UPDATE inventory_versions SET canonical_sha256 = :s", s="a" * 64)
    sql(engine, "UPDATE inventory_projection_state SET head_sha256 = :s", s="a" * 64)

    with engine.begin() as c:
        stato = projection.currency(c)
        assert stato.current, "la proiezione deve risultare ATTUALE, o il test prova altro"
        assert stato.head_version == trovati.version, "la versione NON deve essersi mossa"
        assert not candidates.unchanged(c, version=trovati.version,
                                        sha256=trovati.sha256), \
            "il guardiano guarda solo la versione: un contenuto cambiato sotto la " \
            "stessa versione passerebbe"


def test_a_put_between_the_snapshot_and_the_claim_abandons_the_run(db, engine, smtp,
                                                                  monkeypatch):
    """La corsa vera: un `PUT` committa fra lo snapshot e la transazione che prenota.

    Non si simula con un finto: si avvolge `read_snapshot` e si salva davvero una
    versione nuova nel momento in cui lo snapshot si chiude. È esattamente la finestra
    che la §5 chiede di chiudere, e senza il controllo questo test manderebbe un avviso
    calcolato su una revisione che non esiste più.

    Atteso: nessun invio, nessun promemoria, e il giro non concluso — si ricalcola al
    tick successivo, sulla revisione nuova.
    """
    from contextlib import contextmanager

    vero = wk.read_snapshot
    fatto: list = []

    @contextmanager
    def intruso():
        with vero() as conn:
            yield conn
        if not fatto:
            fatto.append(save(engine, CORPORA["kinds"]))

    bootstrap(engine, CORPORA["windows"])
    monkeypatch.setattr(wk, "read_snapshot", intruso)

    risultato = wk.run_once(engine, now_utc=_at(TODAY))
    assert fatto, "l'intruso non ha salvato: il test non prova niente"
    assert risultato.reason == "inventory_moved"
    assert smtp.sent == []
    assert _reminders(engine) == []
    assert _run_row(engine, TODAY)["finished_at"] is None

    # Al tick successivo, con l'inventario fermo, il giro va a buon fine.
    monkeypatch.setattr(wk, "read_snapshot", vero)
    assert wk.run_once(engine, now_utc=_at(TODAY, hour_utc=8)).reason == "sent"
    assert len(smtp.sent) == 1


def test_the_head_is_not_locked_while_the_digest_is_delivered(db, engine, smtp):
    """Durante l'invio, un altro utente può salvare. Non è un dettaglio.

    Un `SELECT ... FOR UPDATE` sulla riga di testa nel controllo della revisione
    avrebbe tenuto bloccata la testa per tutta la consegna SMTP — cioè per un timeout
    di rete — e fermato ogni salvataggio degli utenti nel frattempo. Il test lo prova
    salvando DA DENTRO la consegna: se la testa fosse bloccata, questo salvataggio
    resterebbe appeso e il test scadrebbe.
    """
    salvato: list = []

    def durante(msg):
        salvato.append(save(engine, CORPORA["kinds"]).version)
        smtp.sent.append(msg)

    bootstrap(engine, CORPORA["windows"])
    import app.notifications.smtp as smtp_mod
    originale = smtp_mod.deliver
    try:
        smtp_mod.deliver = lambda msg, what="": durante(msg)
        import app.notifications.worker as wkmod
        wkmod.deliver = smtp_mod.deliver
        risultato = wk.run_once(engine, now_utc=_at(TODAY))
    finally:
        smtp_mod.deliver = originale
        import app.notifications.worker as wkmod
        wkmod.deliver = originale

    assert risultato.reason == "sent"
    assert salvato, "il salvataggio concorrente non è avvenuto: la testa era bloccata?"


# ==================================================================
# 7. il ritentativo legge la proiezione, e la pretende attuale
# ==================================================================

def test_a_retry_rebuilds_its_names_from_the_projection(db, engine, smtp):
    """Il digest ricomposto per un ritentativo prende i nomi dalle TABELLE.

    Si rinomina un dispositivo direttamente nella proiezione — non nel documento — e
    si pretende che il nome nuovo compaia nel digest del ritentativo. Prima della fase
    2F i nomi venivano dal documento e questa modifica non si sarebbe vista.
    """
    bootstrap(engine, CORPORA["windows"])
    smtp.fail_with = OSError("relay giù")
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "delivery_failed"
    smtp.fail_with = None

    sql(engine, "UPDATE inventory_devices SET name = 'RINOMINATO' "
                " WHERE code = 'd7'")
    _sblocca_ritentativi(engine)
    risultato = wk.run_once(engine, now_utc=_at(TODAY, hour_utc=12))
    assert risultato.reason == "sent", risultato.reason
    corpo = smtp.sent[-1].get_content()
    assert "RINOMINATO" in corpo


def test_a_retry_is_postponed_when_the_projection_is_not_current(db, engine, smtp):
    """Un ritentativo su una proiezione rotta si RINVIA, e non consuma un tentativo.

    Le tre risposte di `_rebuild_selection` sono distinte per questo: «lista vuota»
    significa che i promemoria non hanno più un riscontro e la consegna si chiude;
    `None` significa «non lo so», e chiudere la consegna in quel caso marcherebbe come
    inviati dei promemoria che nessuno ha ricevuto.

    E il tentativo non si consuma: si torna prima di `mark_attempt_started`, perché i
    cinque tentativi esistono per un relay guasto, non per una proiezione da
    ricostruire.
    """
    bootstrap(engine, CORPORA["windows"])
    smtp.fail_with = OSError("relay giù")
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "delivery_failed"
    smtp.fail_with = None
    tentativi_prima = _deliveries(engine)[0]["attempts"]

    _guasta(engine, "mappa")
    _sblocca_ritentativi(engine)
    risultato = wk.run_once(engine, now_utc=_at(TODAY, hour_utc=12))

    assert risultato.reason == "projection_not_current"
    assert smtp.sent == []
    consegna = _deliveries(engine)[0]
    assert consegna["state"] == "pending", "la consegna non deve essere chiusa"
    assert consegna["attempts"] == tentativi_prima, \
        "un rinvio non deve costare un tentativo"


def test_a_reminder_whose_device_vanished_leaves_the_digest(db, engine, smtp):
    """Il dispositivo cancellato esce dal digest; il promemoria si chiude col resto.

    Comportamento invariato dalla 2E, ma la strada è nuova: la chiave
    `(uid, tipo, data)` adesso si cerca nella proiezione. Si cancella la riga del
    dispositivo — non il documento — perché è la proiezione a essere la sorgente.
    """
    bootstrap(engine, CORPORA["windows"])
    smtp.fail_with = OSError("relay giù")
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "delivery_failed"
    smtp.fail_with = None

    sql(engine, "DELETE FROM inventory_devices")
    _sblocca_ritentativi(engine)
    risultato = wk.run_once(engine, now_utc=_at(TODAY, hour_utc=12))
    assert risultato.reason == "retry_empty"
    assert smtp.sent == []
    assert _deliveries(engine)[0]["state"] == "sent"


def test_changing_an_expiry_date_drops_the_stale_reminder_from_a_retry(db, engine,
                                                                      smtp):
    """La data fa parte della chiave: corretta la garanzia, la voce esce dal digest.

    Non si manda un avviso su una scadenza che qualcuno ha già spostato. È la stessa
    regola di prima, verificata sulla sorgente nuova.
    """
    bootstrap(engine, CORPORA["redated-before"])
    smtp.fail_with = OSError("relay giù")
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "delivery_failed"
    smtp.fail_with = None

    save(engine, CORPORA["redated-after"])
    _sblocca_ritentativi(engine)
    risultato = wk.run_once(engine, now_utc=_at(TODAY, hour_utc=12))
    assert risultato.reason == "retry_empty"
    assert smtp.sent == []


# ==================================================================
# 8. identità: `_uid` e non l'id di business
# ==================================================================

def test_two_devices_with_the_same_business_id_stay_two_reminders(db, engine, smtp):
    """Stesso id, `_uid` diversi: due entità di promemoria (§7 della fase 2F).

    Con gli inventari importati da un foglio di calcolo gli id ripetuti sono la norma,
    e raggruppare per id manderebbe un avviso solo per due macchine — quella che non
    riceve l'avviso è quella che scade.
    """
    bootstrap(engine, CORPORA["duplicates"])
    dovuti = from_projection().items
    per_id = [i for i in dovuti if i.device in ("dup-a", "dup-b")]
    assert len({i.entity_uid for i in per_id}) == 2

    wk.run_once(engine, now_utc=_at(TODAY))
    with engine.begin() as c:
        uid_distinti = c.execute(text(
            "SELECT count(DISTINCT entity_uid) FROM reminders")).scalar_one()
    assert int(uid_distinti) == len({i.entity_uid for i in dovuti})


def test_two_identical_twins_are_still_two(db, engine):
    """Stesso id E stesso nome, stesso rack: distinguibili solo per `_uid`.

    È il caso in cui un raggruppamento per etichetta perderebbe una riga senza che
    nessuno lo noti — il digest ne mostrerebbe una e l'altra scadrebbe in silenzio.
    """
    bootstrap(engine, CORPORA["duplicates"])
    gemelli = [i for i in from_projection().items if i.device == "gemello"]
    assert len(gemelli) == 2
    assert len({i.entity_uid for i in gemelli}) == 2


def test_moving_a_device_keeps_its_reminder_identity(db, engine, smtp):
    """Spostato di rack, stessa scadenza: NON è un promemoria nuovo.

    L'identità è l'`_uid`, non la posizione. Se il promemoria si rigenerasse a ogni
    spostamento, riordinare un armadio manderebbe un secondo avviso su ogni macchina.
    """
    bootstrap(engine, CORPORA["moved-before"])
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "sent"
    prima = _reminders(engine)

    save(engine, CORPORA["moved-after"])
    risultato = wk.run_once(engine, now_utc=_at(TODAY + timedelta(days=1)))
    assert risultato.reason == "nothing_due", risultato.reason
    assert len(smtp.sent) == 1, "lo spostamento ha prodotto un secondo avviso"
    assert _reminders(engine) == prima

    # E il contesto nel digest DEVE essere cambiato: il rack è un altro.
    dopo = from_projection().items
    assert [i.rack for i in dopo] == ["R02"]


# ==================================================================
# 9. il fuso e la data di riferimento
# ==================================================================

@pytest.mark.parametrize("giorno", [
    date(2027, 3, 28),      # ora legale in avanti: le 02:30 locali non esistono
    date(2027, 10, 31),     # ora legale indietro: le 02:30 accadono due volte
    date(2026, 12, 31),     # confine d'anno
])
def test_the_reference_date_is_the_local_calendar_date(db, engine, giorno):
    """La finestra si calcola sulla DATA LOCALE, e le due sorgenti la ricevono uguale.

    Il fuso non è un rischio di divergenza — `today` arriva da fuori in entrambe le
    implementazioni, ed è ciò che rende provabile il cambio d'ora senza aspettarlo —
    ma la §9 chiede il caso, e il caso serve a dimostrare che nessuna delle due ha
    un orologio proprio nascosto dentro.
    """
    from app.notifications.expiry import local_today

    documenti = _parity.corpora(giorno)
    bootstrap(engine, documenti["windows"])
    atteso = due_items(stored_document(engine), today=giorno,
                       warning_days=WINDOWS)
    ottenuto = from_projection(today=giorno).items
    assert identity(ottenuto) == identity(atteso)

    # E la data locale è quella del fuso configurato, non quella UTC: a Roma le 00:30
    # del 1° gennaio sono ancora il 31 dicembre in UTC.
    mezzanotte_e_mezza = datetime(giorno.year, giorno.month, giorno.day, 0, 30,
                                  tzinfo=timezone.utc)
    assert local_today(mezzanotte_e_mezza, "Europe/Rome") in (
        giorno, giorno + timedelta(days=1))


def test_no_warning_windows_means_nothing_is_due_and_no_query_runs(db, engine):
    """Nessuna finestra configurata → elenco vuoto, e nessuna query.

    Stessa uscita anticipata di `due_items`, per lo stesso motivo: `max(())`
    solleverebbe. La revisione viene restituita comunque, perché la precondizione
    sulla proiezione si verifica anche quando non c'è niente da chiedere — «non ho
    guardato» e «ho guardato e non c'era niente» devono restare distinguibili.
    """
    versione = bootstrap(engine, CORPORA["windows"])
    trovati = from_projection(windows=[])
    assert trovati.items == []
    assert trovati.version == versione
    assert due_items(stored_document(engine), today=TODAY, warning_days=[]) == []


def test_a_very_wide_window_does_not_overflow_the_date(db, engine):
    """La finestra massima ammessa dalle impostazioni: 3650 giorni.

    `today + 3650` è una data valida e il confronto resta un confronto fra date. Con
    un limite molto più grande si arriverebbe all'estremo del tipo `date` di
    PostgreSQL, ed è la validazione delle impostazioni a impedirlo (§8.38) — questo
    test verifica che il valore massimo ammesso passi davvero.
    """
    from app.settings.schema import MAX_WARNING_DAY_VALUE

    bootstrap(engine, CORPORA["windows"])
    atteso = due_items(stored_document(engine), today=TODAY,
                       warning_days=[MAX_WARNING_DAY_VALUE])
    ottenuto = from_projection(windows=[MAX_WARNING_DAY_VALUE]).items
    assert identity(ottenuto) == identity(atteso)
    assert ottenuto, "con dieci anni di preavviso qualcosa deve essere dovuto"


# ==================================================================
# 10. lo snapshot è davvero uno snapshot, e di sola lettura
# ==================================================================

def test_the_snapshot_is_repeatable_read_and_read_only(db, engine):
    """Lo si chiede a PostgreSQL, non a SQLAlchemy.

    Una dichiarazione di isolamento che il driver ignorasse in silenzio darebbe test
    verdi e letture incoerenti in produzione, sotto carico, dove non si riproducono a
    mano. Quindi: si interroga il database, e si prova che una scrittura è RIFIUTATA.
    """
    bootstrap(engine, CORPORA["windows"])
    with read_snapshot() as snap:
        assert snap.execute(text("SHOW transaction_isolation")).scalar_one() \
            == "repeatable read"
        assert snap.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        with pytest.raises(Exception) as exc:
            snap.execute(text("UPDATE worker_heartbeat SET state = 'x' "
                              " WHERE id IS TRUE"))
        assert "read-only" in str(exc.value).lower() or \
               "readonly" in type(exc.value).__name__.lower()

    # La connessione torna al pool PULITA: se l'opzione restasse attaccata, la
    # prossima transazione dell'applicazione sarebbe di sola lettura per sbaglio.
    with engine.begin() as c:
        assert c.execute(text("SHOW transaction_read_only")).scalar_one() == "off"


def test_a_save_committing_mid_read_cannot_split_the_candidate_set(db, engine):
    """Un `PUT` che committa DENTRO la lettura non produce candidati di due versioni.

    È la mutazione dell'isolamento fatta col database: se `REPEATABLE READ` non
    avesse effetto, la seconda lettura dentro lo stesso snapshot vedrebbe
    l'inventario nuovo e questo test sarebbe rosso.
    """
    bootstrap(engine, CORPORA["windows"])
    with read_snapshot() as snap:
        primo = candidates.due_items_from_projection(
            snap, today=TODAY, warning_days=WINDOWS)
        save(engine, CORPORA["kinds"])          # committa DENTRO lo snapshot
        secondo = candidates.due_items_from_projection(
            snap, today=TODAY, warning_days=WINDOWS)
    assert identity(primo.items) == identity(secondo.items)
    assert primo.version == secondo.version

    # E fuori dallo snapshot si vede l'inventario nuovo: se non lo si vedesse, il test
    # sopra sarebbe verde perché il salvataggio non è avvenuto.
    assert from_projection().version == secondo.version + 1


def test_the_isolation_is_declared_in_one_place_only(db, engine):
    """`REPEATABLE READ` si dichiara in `db.read_snapshot` e in nessun altro posto.

    Dalla fase 2F i lettori della proiezione sono due processi — l'API e il worker — e
    due dichiarazioni dello stesso isolamento divergono in silenzio: una delle due
    letture continuerebbe a funzionare sotto READ COMMITTED, e il difetto comparirebbe
    solo quando qualcuno salva mentre qualcun altro legge.
    """
    radice = Path(wk.__file__).resolve().parents[1]
    dichiarazioni = []
    for percorso in sorted(radice.rglob("*.py")):
        testo = percorso.read_text(encoding="utf-8")
        for riga in testo.splitlines():
            if "isolation_level=" in riga and not riga.strip().startswith("#"):
                dichiarazioni.append(f"{percorso.name}:{riga.strip()}")
    assert len(dichiarazioni) == 1, dichiarazioni
    assert dichiarazioni[0].startswith("db.py:")


# ==================================================================
# 11. i privilegi del ruolo del worker, chiesti a PostgreSQL
# ==================================================================

WORKER_READS = ("inventory_head", "inventory_versions",
                "inventory_projection_state", "inventory_locations",
                "inventory_rooms", "inventory_racks", "inventory_devices",
                "settings")


@pytest.mark.parametrize("tabella", WORKER_READS)
def test_the_worker_role_can_read_what_the_candidate_query_needs(engine, tabella):
    """Il ruolo `tsm_worker` ha le `SELECT` che la query dei candidati richiede.

    Nessuna migrazione nuova nella fase 2F: le concessioni c'erano già dalla
    0009/0010/0011/0012. Questo test è la verifica di quel fatto, tabella per tabella
    — se una mancasse, il worker in produzione fallirebbe alle otto del mattino con
    «permission denied» e nessuno lo scoprirebbe prima.
    """
    with engine.begin() as c:
        ok = c.execute(text(
            "SELECT has_table_privilege('tsm_worker', :t, 'SELECT')"),
            {"t": tabella}).scalar_one()
    assert ok is True, f"tsm_worker non può leggere {tabella}"


@pytest.mark.parametrize("tabella", ("inventory_locations", "inventory_rooms",
                                     "inventory_racks", "inventory_devices",
                                     "inventory_projection_state",
                                     "inventory_head", "inventory_versions",
                                     "settings"))
@pytest.mark.parametrize("verbo", ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
def test_the_worker_role_cannot_write_the_inventory(engine, tabella, verbo):
    """La metà che conta: il worker LEGGE e non scrive.

    Manda avvisi. Non ha nessun motivo per poter riscrivere la proiezione che sta
    leggendo, e il giorno in cui qualcuno gli concedesse `UPDATE` «per correggere una
    data» questa matrice è la riga che il diff mostra accanto.
    """
    with engine.begin() as c:
        ok = c.execute(text(
            "SELECT has_table_privilege('tsm_worker', :t, :p)"),
            {"t": tabella, "p": verbo}).scalar_one()
    assert ok is False, f"tsm_worker può {verbo} su {tabella}"


def test_the_worker_role_really_cannot_write_a_derived_date(db, engine):
    """Non solo il catalogo: si PROVA a scrivere, come il ruolo, e si viene respinti.

    `has_table_privilege` legge il catalogo, che è la dichiarazione. Questo esegue
    l'`UPDATE` e si fa dire no da PostgreSQL, che è il fatto. Le due cose coincidono
    quasi sempre, e «quasi» è la ragione per cui ci sono entrambi i test.

    ⚠ `SET ROLE` e non una connessione nuova. Le migrazioni creano i ruoli di runtime
    con `CREATE ROLE … LOGIN NOINHERIT` e **senza password** — la password la assegna
    l'operations al deploy, da un file di secret (§8.19) — quindi in prova non esiste
    nessuna credenziale con cui collegarsi come `tsm_worker`. La prima stesura ci
    provava e finiva in uno `skip`: un test saltato somiglia troppo a un test passato,
    e questo in particolare avrebbe taciuto proprio sul privilegio che deve
    sorvegliare. `SET ROLE` fa applicare a PostgreSQL gli stessi controlli, dentro la
    stessa sessione, senza inventare credenziali.
    """
    bootstrap(engine, CORPORA["windows"])

    with engine.connect() as c:
        with c.begin():
            c.execute(text("SET LOCAL ROLE tsm_worker"))
            assert c.execute(text("SELECT current_user")).scalar_one() == "tsm_worker"
            # Le letture che gli servono funzionano davvero, non solo nel catalogo.
            n = c.execute(text("SELECT count(*) FROM inventory_devices")).scalar_one()
            assert int(n) > 0
            assert c.execute(text(
                "SELECT count(*) FROM inventory_projection_state")).scalar_one() == 1

    for istruzione in (
            "UPDATE inventory_devices SET garanzia_date = NULL",
            "DELETE FROM inventory_devices",
            "UPDATE inventory_projection_state SET mapper_version = 999",
            "INSERT INTO inventory_locations (uid, ordinal) "
            "VALUES (gen_random_uuid(), 99)",
            "UPDATE inventory_head SET version = 1 WHERE id IS TRUE"):
        with engine.connect() as c:
            with pytest.raises(Exception) as exc:
                with c.begin():
                    c.execute(text("SET LOCAL ROLE tsm_worker"))
                    c.execute(text(istruzione))
            assert "permission denied" in str(exc.value).lower(), \
                f"{istruzione!r} non è stata respinta: {exc.value}"


def test_the_relational_parents_are_not_nullable(engine):
    """Le tre chiavi esterne dell'albero sono `NOT NULL`.

    La query dei candidati usa `JOIN` interne. Se una di quelle colonne diventasse
    annullabile, una `JOIN` interna farebbe sparire dei promemoria **in silenzio** —
    il guasto peggiore possibile per un sistema di avvisi. Il test pretende la
    proprietà dallo schema invece di fidarsi di un commento.
    """
    atteso = {("inventory_rooms", "location_uid"),
              ("inventory_racks", "room_uid"),
              ("inventory_devices", "rack_uid")}
    with engine.begin() as c:
        righe = c.execute(text("""
            SELECT table_name, column_name, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND (table_name, column_name) IN (
                     ('inventory_rooms','location_uid'),
                     ('inventory_racks','room_uid'),
                     ('inventory_devices','rack_uid'))
        """)).all()
    trovate = {(r[0], r[1]) for r in righe}
    assert trovate == atteso
    for r in righe:
        assert r[2] == "NO", f"{r[0]}.{r[1]} è annullabile: le JOIN interne mentono"


# ==================================================================
# 12. il digest composto dalla sorgente nuova
# ==================================================================

def test_the_digest_body_carries_the_context_from_the_joins(db, engine, smtp):
    """Sito, sala e rack nel corpo del messaggio vengono dalle JOIN."""
    bootstrap(engine, CORPORA["tree"])
    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "sent"
    corpo = smtp.sent[0].get_content()
    assert "alfa / sala-a / rack R01" in corpo
    assert "beta / sala-a / rack R01" in corpo


def test_a_hostile_name_from_the_projection_adds_no_header(db, engine, smtp):
    """Il nome arriva INTATTO dalla proiezione, e la difesa lo sanifica dopo.

    Sanificare nella sorgente avrebbe reso non verificabile la difesa che sta in
    `digest.sanitise_field`: un test che non vede mai un `\\r\\n` non dimostra che
    quel `\\r\\n` sarebbe stato fermato.
    """
    bootstrap(engine, CORPORA["hostile"])
    ostile = [i.device for i in from_projection().items if "Bcc:" in i.device]
    assert ostile, "il nome ostile non è arrivato intatto dalla proiezione"

    assert wk.run_once(engine, now_utc=_at(TODAY)).reason == "sent"
    msg = smtp.sent[0]
    assert msg["Bcc"] is None
    assert len(msg.get_all("To") or []) == 1
    assert "\r\nBcc:" not in msg.get_content()


def test_an_empty_inventory_is_nothing_due_not_a_failure(db, engine, smtp):
    """Inventario senza siti: «niente è dovuto», con l'esito registrato.

    È lo stato di un'installazione appena inizializzata, e deve restare distinto da un
    guasto: la §13 vieta di trasformare un guasto in «niente da fare», e questo test è
    l'altra metà — «niente da fare» deve continuare a essere possibile.
    """
    bootstrap(engine, CORPORA["empty"])
    risultato = wk.run_once(engine, now_utc=_at(TODAY))
    assert risultato.reason == "nothing_due"
    assert smtp.sent == []
    assert _run_row(engine, TODAY)["outcome"] == "nothing_due"


def test_the_seed_inventory_produces_the_same_digest_from_both_sources(db, engine,
                                                                      smtp):
    """Il seed di produzione: le due sorgenti compongono lo STESSO corpo.

    È il confronto alla scala e alla forma che contano. Il corpo del digest è la somma
    di tutto — selezione, ordinamento, etichette, contesto, soglie — quindi
    confrontarlo carattere per carattere copre in un colpo ciò che i test precedenti
    coprono separatamente.
    """
    from app.notifications.digest import build_digest

    bootstrap(engine, CORPORA["seed-dated"])
    ora = _at(TODAY)
    dall_oracolo = due_items(stored_document(engine), today=TODAY,
                             warning_days=WINDOWS)
    dalla_proiezione = from_projection().items
    assert dall_oracolo, "il corpus con le date non produce scadenze: fixture rotta"

    def corpo(voci):
        finte = [{"reminder_id": n, "threshold_days": 30, "item": v}
                 for n, v in enumerate(voci)]
        return build_digest(finte, sender="ced@example.internal",
                            recipients=RECIPIENTS, message_id="<x@tsm.local>",
                            now=ora, today=TODAY).get_content()

    assert corpo(dalla_proiezione) == corpo(dall_oracolo)


# ==================================================================
# 13. le impostazioni e la readiness non sono cambiate (§14)
# ==================================================================

def test_readiness_does_not_depend_on_the_worker(db, engine):
    """`/api/ready` non guarda se il worker ha girato oggi (§14).

    La salute del worker è un problema di monitoraggio suo, e ha il suo battito e il
    suo healthcheck. Legarli farebbe cadere l'API per un guasto degli avvisi — e
    l'inverso: un'API pronta non dice niente sul fatto che gli avvisi partano.
    """
    import app.api.health as health
    sorgente = Path(health.__file__).read_text(encoding="utf-8")
    for vietato in ("scheduler_runs", "worker_heartbeat", "reminder_deliveries",
                    "reminders"):
        assert vietato not in sorgente, \
            f"la readiness guarda lo stato del worker: {vietato}"


def test_disabled_notifications_never_touch_the_projection(db, engine, smtp,
                                                           monkeypatch):
    """Notifiche spente → non si legge nemmeno la proiezione.

    L'ordine dei controlli è rimasto quello: prima le impostazioni, poi l'inventario.
    Con le notifiche spente non si apre nessuno snapshot, e una proiezione rotta non
    produce nemmeno un errore — spegnere gli avvisi deve fermare tutto il lavoro, non
    solo l'invio.
    """
    letture: list = []
    vero = wk.read_snapshot
    monkeypatch.setattr(wk, "read_snapshot",
                        lambda: (letture.append(1), vero())[1])

    bootstrap(engine, CORPORA["windows"])
    _guasta(engine, "assente")
    set_settings(engine, enabled=False)
    risultato = wk.run_once(engine, now_utc=_at(TODAY))
    assert risultato.reason == "notifications_disabled"
    assert letture == [], "la proiezione è stata letta con le notifiche spente"


# ==================================================================
# aiuti
# ==================================================================

def _executable_source(modulo) -> str:
    """Il sorgente di un modulo SENZA le stringhe di documentazione.

    ⚠ Serve, e la prima stesura non lo faceva: i controlli statici cercavano
    `inventory/expiries` nel testo del modulo e lo trovavano nel commento che spiega
    perché il worker NON lo chiama. Un controllo statico che cade sulla sua stessa
    spiegazione è un controllo che costringe a smettere di spiegare — il modo più
    sicuro di perdere la ragione della regola.

    `ast.unparse` di un albero da cui si sono tolte le docstring dà il codice che
    verrà eseguito, e niente altro. I commenti `#` l'`ast` li ha già scartati.
    """
    import ast

    albero = ast.parse(Path(modulo.__file__).read_text(encoding="utf-8"))
    for nodo in ast.walk(albero):
        if not isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        corpo = nodo.body
        if (corpo and isinstance(corpo[0], ast.Expr)
                and isinstance(corpo[0].value, ast.Constant)
                and isinstance(corpo[0].value.value, str)):
            nodo.body = corpo[1:] or [ast.Pass()]
    return ast.unparse(albero)


def _at(day: date, hour_utc: int = 7, minute: int = 0) -> datetime:
    """08:00 a Roma = 06:00 UTC in estate: alle 07:00 UTC l'ora pianificata è passata."""
    return datetime(day.year, day.month, day.day, hour_utc, minute,
                    tzinfo=timezone.utc)


def _reminders(engine) -> list[tuple]:
    with engine.begin() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT entity_uid, expiry_kind, expiry_date, threshold_days, state "
            "  FROM reminders ORDER BY id")).all()]


def _deliveries(engine) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, state, attempts, message_id FROM reminder_deliveries "
            " ORDER BY id")).mappings().all()]


def _run_row(engine, run_date: date) -> dict | None:
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT run_date, finished_at, outcome, due_count, sent_count "
            "  FROM scheduler_runs WHERE run_date = :d"),
            {"d": run_date}).mappings().first()
    return dict(row) if row else None


def _sblocca_ritentativi(engine) -> None:
    """Rende il ritentativo dovuto adesso, invece di aspettare il backoff."""
    with engine.begin() as c:
        c.execute(text("UPDATE reminder_deliveries SET next_attempt_after = NULL "
                       " WHERE state = 'pending'"))


# ==================================================================
# finto server di posta — stessa forma di test_worker_pg.py
# ==================================================================

class FakeSMTP:
    sent: list = []
    fail_with: Exception | None = None
    connections: int = 0

    def __init__(self, host, port, timeout=None, context=None):
        type(self).connections += 1
        if type(self).fail_with is not None:
            raise type(self).fail_with

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def ehlo(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        type(self).sent.append(msg)

    @classmethod
    def reset(cls):
        cls.sent = []
        cls.fail_with = None
        cls.connections = 0


@pytest.fixture
def smtp(monkeypatch):
    import app.notifications.smtp as mod
    from app.config import get_settings
    FakeSMTP.reset()
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", FakeSMTP)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_host", "relay.interno", raising=False)
    monkeypatch.setattr(cfg, "smtp_sender", "ced@example.internal", raising=False)
    monkeypatch.setattr(cfg, "smtp_username", "", raising=False)
    monkeypatch.setattr(cfg, "smtp_tls_mode", "starttls", raising=False)
    yield FakeSMTP
    FakeSMTP.reset()
