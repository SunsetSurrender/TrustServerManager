"""Il lato SQL del contratto di dominio (fase 2G, §8.50). PostgreSQL vero.

Terza implementazione, stesse fixture. `test_domain_contract.py` le esegue contro
`app/domain.py`, `tools/domain-contract-tests.mjs` contro `handoff/domain.js`, e questo
file contro le interrogazioni su PostgreSQL.

⚠ Perché serve un file a parte, invece di fidarsi di `test_queries_pg.py`.

Quella suite misura il **delta** rispetto al comportamento della 2E: dice che lo SQL è
cambiato esattamente quanto dichiarato. Non dice che lo SQL sia **conforme al
contratto** — potrebbe essere cambiato nel modo dichiarato e restare comunque diverso da
ciò che il frontend calcola. Sono due domande, e la seconda è quella che rende vera la
frase «il risultato locale del frontend coincide con quello dell'endpoint».

⚠ Le esclusioni sono DICHIARATE, non silenziose
----------------------------------------------
Alcuni casi delle fixture non sono esprimibili in un documento canonico, e vanno esclusi
da questo confronto **con la ragione scritta**:

  - `rackU: null` — la canonicalizzazione riempie `u` col default 45 (§8.14), quindi un
    documento con `u` nullo non esiste. Il caso resta valido per il modello puro, che
    riceve valori da chiamanti diversi;
  - `rackU: 3000000000` — supera `int32`, quindi la mappa lo porta in `extra` e la
    colonna resta NULL (§8.42). È un LIMITE della proiezione, e ha un test proprio che
    lo fissa invece di far sparire il caso.

Un'esclusione senza motivo è il modo in cui un test smette di provare qualcosa senza che
nessuno se ne accorga; qui sono due, entrambe con il loro test.

Riferimento: BACKEND-PLAN.md §8.50.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text

from app import domain
from app.db import read_snapshot
from app.inventory import Actor, InventoryRepository
from app.inventory import queries as q

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "domain"
ADMIN = Actor(username="capo", role="admin")
TODAY = date(2026, 8, 20)


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def uid(prefix: str, n: int) -> str:
    return f"{(prefix * 8)[:8]}-0000-4000-8000-{n:012d}"


@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


def _wipe(engine) -> None:
    """Database vuoto, con lo stesso metodo di `test_queries_pg.py`.

    ⚠ `TRUNCATE ... CASCADE` su testa e versioni, e non una sequenza di `DELETE`.
    Due ragioni, entrambe imparate qui:

      - `inventory_head.version` punta a `inventory_versions`, quindi cancellare le
        versioni prima della testa viola la chiave esterna. Un ordine «dai figli ai
        genitori» è quello giusto per la gerarchia dell'inventario e quello sbagliato
        per quella coppia;
      - una serie di `DELETE` in una transazione, ripetuta a ogni fixture di modulo,
        prende i lock in un ordine che con `read_snapshot` ancora aperto produce
        DEADLOCK. `TRUNCATE CASCADE` prende un lock esclusivo e finisce.

    I `vani` non hanno una tabella propria: sono value object della sala (§8.4).
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM inventory_projection_state"))


def bootstrap(engine, doc: dict) -> None:
    _wipe(engine)
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(doc, ADMIN)


# ==================================================================
# 1. capacità
# ==================================================================
#
# ⚠ Un rack per caso, in una sala sola: un solo `bootstrap` per tutta la famiglia
# invece di uno per caso. I rack non interagiscono fra loro — la capacità è per rack —
# quindi metterli insieme non cambia nessuna risposta e rende il test eseguibile in un
# tempo che qualcuno accetterà di aspettare.

CAPACITY = load("capacity")["cases"]

#: I casi che un documento canonico non può contenere. Vedi il docstring del modulo.
CAP_ESCLUSI = {
    "rack di altezza null": "la canonicalizzazione riempie `u` col default 45",
    "RACK ENORME: nessuna enumerazione degli slot, il conto e sugli estremi":
        "3 000 000 000 supera int32: la mappa lo porta in `extra` (test proprio)",
}

#: ⚠ NUOVO nella 2H. I casi che il CANCELLO del documento ora rifiuta, e che quindi
#: possono esistere in una sola forma: righe scritte quando il cancello non c'era.
#:
#: La 2H applica il limite della voce 16 del registro — `rack.u` in `1..2^31-1` — e
#: `validate_normal_document` respinge un'altezza `0` o negativa col codice
#: `rack_u_out_of_range`. Questi due casi non passano più da `bootstrap`.
#:
#: Escluderli sarebbe stato più semplice e sbagliato. Un'altezza `<= 0` **esiste** nei
#: dati vecchi: il form del prototipo la stringeva a `1..60`, ma un ripristino da JSON
#: non stringeva niente, e la vista Capacità aveva un difetto proprio su `rk.u = 0`
#: (percentuale `NaN%`). Lo SQL deve continuare a calcolarla come il modello puro, e un
#: caso escluso è un caso che nessuno guarda più.
#:
#: Si scrivono quindi come esistono davvero: `bootstrap` con un'altezza lecita, poi un
#: UPDATE sulla colonna. È l'unico posto della suite dove si scrive nella proiezione
#: aggirando il documento, ed è deliberato: sta simulando un dato storico, non
#: fabbricando una comodità. La proiezione resta «attuale» perché versione e digest
#: registrati non cambiano — la cecità alle colonne derivate della voce 13, usata qui
#: di proposito e con la sua nota.
CAP_LEGACY = {
    "rack di altezza 0: non ha unita, non e occupato al 100%":
        "il cancello della 2H rifiuta u=0: riga possibile solo come dato storico",
    "rack di altezza negativa": "il cancello della 2H rifiuta u<0: riga possibile solo come dato storico",
}

#: Altezza lecita con cui i casi legacy entrano, prima di essere riportati al loro
#: valore vero. Un valore qualunque dentro l'intervallo: la capacità viene ricalcolata
#: dalla colonna dopo l'UPDATE, quindi questo numero non compare in nessuna attesa.
CAP_LEGACY_PLACEHOLDER = 45

CAP_INCLUSI = [c for c in CAPACITY if c["name"] not in CAP_ESCLUSI]


def _doc_capacita() -> tuple[dict, dict]:
    """Documento con un rack per caso, e la mappa nome-del-caso → uid del rack."""
    racks, per_caso = [], {}
    for i, case in enumerate(CAP_INCLUSI, start=1):
        rack_uid = uid("c", i)
        per_caso[case["name"]] = rack_uid
        altezza = (CAP_LEGACY_PLACEHOLDER if case["name"] in CAP_LEGACY
                   else case["rackU"])
        racks.append({
            "_uid": rack_uid, "id": f"R{i:03d}", "u": altezza,
            "devices": [{"_uid": uid("d", i * 100 + j), "id": f"d{i}-{j}", **dev}
                        for j, dev in enumerate(case["devices"], start=1)],
        })
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 1), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 1), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    return doc, per_caso


@pytest.fixture(scope="module")
def capacita(engine):
    doc, per_caso = _doc_capacita()
    bootstrap(engine, doc)

    # I casi legacy tornano alla loro altezza vera direttamente in colonna: il
    # documento non potrebbe più portarla. Vedi CAP_LEGACY.
    if CAP_LEGACY:
        with engine.begin() as c:
            for nome in CAP_LEGACY:
                altezza = next(x["rackU"] for x in CAPACITY if x["name"] == nome)
                c.execute(text("UPDATE inventory_racks SET u = :u WHERE uid = :uid"),
                          {"u": altezza, "uid": per_caso[nome]})

    with read_snapshot() as snap:
        report = q.capacity(snap)
    per_uid = {r["uid"]: r for L in report.locations for room in L["rooms"]
               for r in room["racks"]}
    return {nome: per_uid[u] for nome, u in per_caso.items()}


@pytest.mark.parametrize("case", CAP_INCLUSI, ids=[c["name"] for c in CAP_INCLUSI])
def test_lo_sql_conta_gli_slot_distinti(case, capacita):
    rack = capacita[case["name"]]
    assert rack["usedU"] == case["usedU"], f"U occupate: {case['name']}"
    assert rack["freeU"] == case["freeU"], f"U libere: {case['name']}"
    assert rack["largestFreeRun"] == case["largestFreeRun"], \
        f"blocco contiguo: {case['name']}"


@pytest.mark.parametrize("case", CAP_INCLUSI, ids=[c["name"] for c in CAP_INCLUSI])
def test_le_tre_implementazioni_danno_lo_stesso_numero(case, capacita):
    """⚠ Il confronto che dà senso a §12 del requisito.

    Lo SQL è già stato confrontato con l'attesa scritta a mano; qui si confronta con
    ciò che il MODELLO PURO calcola sullo stesso ingresso. Le due asserzioni sembrano
    la stessa cosa e non lo sono: la prima dice «lo SQL è conforme al contratto», la
    seconda «lo SQL e il codice che il frontend chiama danno lo stesso numero». Se la
    seconda cadesse mentre la prima regge, avrei un contratto soddisfatto da due
    implementazioni che restituiscono valori diversi — che è esattamente il difetto che
    questa fase esiste per rendere impossibile.
    """
    puro = domain.rack_capacity(case["rackU"], case["devices"])
    rack = capacita[case["name"]]
    assert (rack["usedU"], rack["freeU"], rack["largestFreeRun"]) == \
           (puro.used_u, puro.free_u, puro.largest_free_run)


def test_le_esclusioni_sono_due_e_hanno_una_ragione():
    """Un'esclusione senza motivo è un test che smette di provare qualcosa."""
    assert len(CAP_ESCLUSI) == 2
    nomi = {c["name"] for c in CAPACITY}
    assert set(CAP_ESCLUSI) <= nomi, (
        f"un'esclusione non corrisponde a nessun caso: {set(CAP_ESCLUSI) - nomi}")
    assert all(len(r) > 20 for r in CAP_ESCLUSI.values())


def test_i_casi_legacy_sono_davvero_rifiutati_dal_cancello():
    """⚠ La coerenza fra il pretesto e il fatto.

    `CAP_LEGACY` dice «questi il cancello li rifiuta», e su quella frase poggia il
    permesso di scriverli con un UPDATE. Se il cancello smettesse di rifiutarli — o se
    qualcuno mettesse in `CAP_LEGACY` un caso che passa benissimo dal documento — la
    scorciatoia resterebbe in piedi senza la sua ragione. Qui si verifica la frase:
    ogni caso legacy deve essere DAVVERO respinto, e ogni caso incluso deve essere
    DAVVERO accettato.
    """
    from app.inventory.document import RACK_U_OUT_OF_RANGE

    nomi = {c["name"] for c in CAPACITY}
    assert set(CAP_LEGACY) <= nomi
    assert not (set(CAP_LEGACY) & set(CAP_ESCLUSI)), (
        "un caso non può essere insieme escluso e legacy: sono due destini diversi")

    for nome in CAP_LEGACY:
        altezza = next(c["rackU"] for c in CAPACITY if c["name"] == nome)
        assert not domain.rack_height_supported(altezza), (
            f"«{nome}» ha altezza {altezza!r}, che il cancello ACCETTA: allora deve "
            f"entrare dal documento come tutti gli altri, non da un UPDATE")

    for case in CAP_INCLUSI:
        if case["name"] in CAP_LEGACY:
            continue
        assert domain.rack_height_supported(case["rackU"]), (
            f"«{case['name']}» ha un'altezza che il cancello rifiuta: va dichiarata "
            f"in CAP_LEGACY, altrimenti il bootstrap del modulo fallisce per intero")

    assert RACK_U_OUT_OF_RANGE == "rack_u_out_of_range"


def test_un_rack_piu_alto_di_int32_e_RIFIUTATO(engine):
    """⚠ RISCRITTO nella 2H: era «divergenza dichiarata», ora è «limite applicato».

    Il fatto tecnico non è cambiato e resta scritto qui, perché è la RAGIONE del
    limite: `rack.u` nel documento è un intero JSON senza massimo, la colonna è
    `integer`, quindi la mappa porta tre miliardi in `extra` e lascia la colonna NULL
    (§8.42). Da lì nascevano due risposte — la vista Capacità dallo SQL vedeva un rack
    senza altezza, il modello puro calcolava su tre miliardi — e la voce 16 del
    registro le dichiarava.

    Dichiararle non bastava. La 2H applica il limite: `validate_normal_document`
    rifiuta il documento con `rack_u_out_of_range`, quindi la divergenza non ha più un
    ingresso. Non si passa a `bigint` — sarebbe cambiare il tipo di una colonna, e
    quindi la versione della mappa e una ricostruzione, per un dato che l'interfaccia
    non produce e che nel browser esaurisce la memoria della scheda.

    Il test prova tre cose, in quest'ordine, e l'ordine è il ragionamento:
      1. la MAPPA continua a comportarsi come prima (il valore in `extra`, la colonna
         NULL) — perché un dato storico può esistere in quella forma;
      2. da quella forma nascerebbero DAVVERO due numeri diversi — altrimenti il
         limite starebbe difendendo da niente;
      3. il documento che la produrrebbe viene RIFIUTATO, e per il motivo giusto.
    """
    from app.inventory.document import RACK_U_OUT_OF_RANGE, validate_normal_document
    from app.inventory.errors import DocumentRejectedError
    from app.inventory.relational import normalise
    from app.inventory.relational_validate import codes, validate_model

    enorme = 3_000_000_000
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 9), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 9), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [],
                  "racks": [{"_uid": uid("c", 9), "id": "R-enorme", "u": enorme,
                             "devices": [{"_uid": uid("d", 9), "id": "d1",
                                          "u": 1, "h": 2}]}]}]}]}

    # --- 1. la mappa: il valore in `extra`, la colonna vuota ---
    modello = normalise(doc)
    rack = modello.racks[0]
    assert rack.u is None, "la colonna int32 non può contenere tre miliardi"
    assert rack.extra["u"] == enorme, "il valore deve sopravvivere in `extra`"
    # E `validate_model` lo SEGNALA senza chiamarlo errore: una riga così è fedele al
    # documento, quindi una proiezione che la contiene non è rotta. È la distinzione su
    # cui poggia la scelta di mettere il divieto nel cancello del documento e non qui.
    trovati = codes(validate_model(modello))
    assert "carried_verbatim" in trovati
    assert RACK_U_OUT_OF_RANGE not in trovati, (
        "il codice del cancello non deve comparire fra i risultati di `validate_model`: "
        "renderebbe «incoerente» una proiezione sana, e le letture risponderebbero 503 "
        "per un dato storico")

    # --- 2. le due letture divergono davvero ---
    dal_documento = domain.rack_capacity(enorme, [{"u": 1, "h": 2}])
    dalla_colonna = domain.rack_capacity(rack.u, [{"u": 1, "h": 2}])
    assert dal_documento.used_u == 2 and dal_documento.total_u == enorme
    assert dalla_colonna.used_u == 0 and dalla_colonna.total_u == 0

    # --- 3. e per questo il documento non entra ---
    problemi = validate_normal_document(doc)
    assert [e.code for e in problemi] == [RACK_U_OUT_OF_RANGE]
    with pytest.raises(DocumentRejectedError):
        bootstrap(engine, doc)
    # Nessuna riga scritta: il rifiuto è PRIMA di persistere.
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_racks")).scalar() == 0


# ==================================================================
# 2. percentuale e file
# ==================================================================

def test_la_percentuale_dello_sql_e_quella_del_contratto(engine):
    """Le percentuali le calcola Python sui totali che lo SQL ha aggregato.

    ⚠ Il test non è vacuo per questo: verifica che l'endpoint non abbia una SECONDA
    formula — un `round()` in SQL sarebbe la scorciatoia ovvia — e che i totali su cui
    la applica siano quelli giusti. I casi scelti sono metà esatte, dove le tre
    implementazioni native darebbero risposte diverse.
    """
    # Un rack da 8 U con 1 occupata: 12,5% → 13 (HALF-UP), non 12 (al pari).
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 2), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 2), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [],
                  "racks": [{"_uid": uid("c", 2), "id": "R8", "u": 8,
                             "devices": [{"_uid": uid("d", 2), "id": "d", "u": 1,
                                          "h": 1}]}]}]}]}
    bootstrap(engine, doc)
    with read_snapshot() as snap:
        report = q.capacity(snap)
    sala = report.locations[0]["rooms"][0]
    assert sala["usedU"] == 1 and sala["totalU"] == 8
    assert sala["occupancyPercent"] == 13, "HALF-UP: 12,5% è 13"
    assert round(1 / 8 * 100) == 12, (
        "se `round()` di Python dicesse 13 il caso non distinguerebbe le due regole")
    assert sala["rows"][0]["occupancyPercent"] == 13


def test_lo_sql_separa_la_fila_non_impostata_da_quella_che_vale_trattino(engine):
    """⚠ Il difetto §8.48 voce 7, sul lato SQL.

    Tre rack: uno senza fila, uno la cui fila è letteralmente «—», uno con «A». Devono
    essere TRE gruppi. Con la sentinella del prototipo i primi due si fondevano, e il
    totale di unità libere di quella fila era la somma di due cose diverse.
    """
    racks = []
    for i, (codice, fila) in enumerate(
            [("R-vuota", ""), ("R-trattino", "—"), ("R-a", "A")], start=1):
        racks.append({"_uid": uid("c", 20 + i), "id": codice, "u": 10, "row": fila,
                      "devices": []})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 3), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 3), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    bootstrap(engine, doc)
    with read_snapshot() as snap:
        report = q.capacity(snap)
    sala = report.locations[0]["rooms"][0]

    assert len(sala["rows"]) == 3, (
        f"i gruppi si sono fusi: {[(b['row'], b['rowLabel']) for b in sala['rows']]}")
    per_etichetta = {}
    for b in sala["rows"]:
        per_etichetta.setdefault(b["rowLabel"], []).append(b)
    # Due gruppi mostrano la stessa etichetta «—» e restano due.
    assert len(per_etichetta["—"]) == 2
    assert {b["rowAssigned"] for b in per_etichetta["—"]} == {True, False}
    # E il valore grezzo distingue: chi vuole saperlo lo può ancora sapere.
    assert {b["row"] for b in per_etichetta["—"]} == {"—", None} or \
           {b["row"] for b in per_etichetta["—"]} == {"—", ""}
    # Ordine: le file dichiarate prima, il residuo per ultimo.
    assert sala["rows"][-1]["rowAssigned"] is False


# ==================================================================
# 3. ricerca
# ==================================================================

SEARCH = load("search")

#: Un dispositivo per caso, ognuno nel suo rack: i rack non si cercano per i campi dei
#: dispositivi, quindi non c'è interferenza. Le query si applicano a TUTTO
#: l'inventario, e per ogni caso si guarda soltanto se il SUO dispositivo compare —
#: che è la domanda della fixture.


@pytest.fixture(scope="module")
def ricerca(engine):
    """Un dispositivo per caso, e una SALA per ogni caso di rack.

    ⚠ I codici dei rack sono unici nell'ambito della loro SALA (`UNIQUE_CODE_KINDS`), e
    più di un caso della fixture usa `id: "K1"` — perché il codice è ciò che si cerca.
    Metterli nella stessa sala produceva quattro `duplicate_scoped_code`, cioè un
    documento che il bootstrap RIFIUTA: non un difetto del contratto, un difetto
    dell'impianto del test. Una sala per caso li rende legittimi senza toccare i dati.
    """
    dev_uid, rack_uid = {}, {}
    racks_dispositivi = []
    for i, case in enumerate(SEARCH["device"], start=1):
        du, ru = uid("d", 300 + i), uid("c", 300 + i)
        dev_uid[case["name"]] = du
        racks_dispositivi.append({"_uid": ru, "id": f"K{i:03d}", "u": 10,
                                  "devices": [{"_uid": du, **case["device"]}]})
    sale = [{"_uid": uid("b", 4), "id": "sala-dispositivi", "nome": "Dispositivi",
             "w": 10, "h": 8, "vani": [], "racks": racks_dispositivi}]
    for i, case in enumerate(SEARCH["rack"], start=1):
        ru = uid("c", 400 + i)
        rack_uid[case["name"]] = ru
        sale.append({"_uid": uid("b", 40 + i), "id": f"sala-rack-{i}",
                     "nome": f"Rack {i}", "w": 10, "h": 8, "vani": [],
                     "racks": [{"_uid": ru, "u": 10, "devices": [], **case["rack"]}]})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 4), "id": "sito", "nome": "Sito", "sale": sale}]}
    bootstrap(engine, doc)
    return dev_uid, rack_uid


@pytest.mark.parametrize("case", SEARCH["device"],
                         ids=[c["name"] for c in SEARCH["device"]])
def test_la_ricerca_sql_sui_dispositivi(case, ricerca):
    dev_uid, _ = ricerca
    atteso = dev_uid[case["name"]]
    with read_snapshot() as snap:
        page = q.search(snap, q=case["q"], limit=q.SEARCH_MAX_LIMIT)
    trovati = {r["device"]["uid"] for r in page.results if r["kind"] == "device"}
    assert (atteso in trovati) is case["match"], (
        f"{case['name']}: q={case['q']!r}")


@pytest.mark.parametrize("case", SEARCH["rack"],
                         ids=[c["name"] for c in SEARCH["rack"]])
def test_la_ricerca_sql_sui_rack(case, ricerca):
    _, rack_uid = ricerca
    atteso = rack_uid[case["name"]]
    with read_snapshot() as snap:
        page = q.search(snap, q=case["q"], limit=q.SEARCH_MAX_LIMIT)
    trovati = {r["rack"]["uid"] for r in page.results if r["kind"] == "rack"}
    assert (atteso in trovati) is case["match"], (
        f"{case['name']}: q={case['q']!r}")


ADDR_MATCHES = load("addresses")["matches"]["cases"]


@pytest.fixture(scope="module")
def indirizzi(engine):
    """Un dispositivo per ogni IP distinto del corpus, più uno senza IP."""
    ips = []
    for case in ADDR_MATCHES:
        if case["ip"] not in ips:
            ips.append(case["ip"])
    racks, per_ip = [], {}
    for i, ip in enumerate(ips, start=1):
        du = uid("d", 500 + i)
        per_ip[repr(ip)] = du
        dev = {"_uid": du, "id": f"a{i}", "u": 1}
        if ip is not None:
            dev["ip"] = ip
        racks.append({"_uid": uid("c", 500 + i), "id": f"A{i:03d}", "u": 10,
                      "devices": [dev]})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 5), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 5), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    bootstrap(engine, doc)
    return per_ip


@pytest.mark.parametrize("case", ADDR_MATCHES,
                         ids=[f"{c['query']}-{c['ip']}" for c in ADDR_MATCHES])
def test_la_ricerca_sql_per_indirizzo(case, indirizzi):
    """⚠ Il contratto degli indirizzi, verificato su PostgreSQL.

    Comprende i due casi che valgono più di tutti: `10.0.0.1` non trova `10.0.0.100`,
    e le famiglie non si mescolano — quest'ultima è una proprietà del tipo `inet`, e
    verificarla qui è verificarla dove viene usata.
    """
    atteso_uid = indirizzi[repr(case["ip"])]
    with read_snapshot() as snap:
        page = q.search(snap, q=case["query"], limit=q.SEARCH_MAX_LIMIT)
    trovati = {r["device"]["uid"] for r in page.results if r["kind"] == "device"}
    assert (atteso_uid in trovati) is case["match"], (
        f"query={case['query']!r} ip={case['ip']!r}")


def test_la_colonna_dell_indirizzo_e_lo_stesso_indirizzo_del_parser(engine, indirizzi):
    """⚠ PostgreSQL e il parser concordano sul VALORE, non sulla scrittura.

    È la premessa su cui poggia la modalità indirizzo: il confronto avviene fra valori
    normalizzati, e se le due normalizzazioni divergessero un indirizzo non troverebbe
    se stesso.

    ⚠ Il test è nato più stretto — confrontava il TESTO di `host(ip_addr)` con
    `parse_address(...).text` — ed è diventato rosso su un caso vero: per
    `::a00:1` (IPv4-*compatible*, senza `ffff`) PostgreSQL scrive `::10.0.0.1` col
    quartetto puntato, `ipaddress` di Python scrive `::a00:1`. Sono lo STESSO
    indirizzo, scritto in due forme che RFC 5952 lascia entrambe leggibili.

    La differenza è innocua e vale la pena spiegare perché, invece di attenuare il test
    e passare avanti:

      - il CONFRONTO avviene fra valori `inet`, non fra testi: `queries.py` passa gli
        estremi come `CAST(:lo AS inet)`, e PostgreSQL li interpreta;
      - la RILETTURA passa da `psycopg`, che restituisce un `ipaddress.IPv6Address` —
        quindi il testo che `read_model` confronta con la mappa è quello di Python da
        entrambe le parti, e l'invariante del giro completo non lo vede mai.

    Quindi si asserisce l'uguaglianza dove il prodotto la usa — il valore — e si FISSA
    la differenza di scrittura, così se un giorno qualcuno mettesse `host(ip_addr)` in
    una risposta scoprirebbe da qui che non è la forma canonica di Python.
    """
    with engine.begin() as c:
        righe = c.execute(text(
            "SELECT ip, host(ip_addr), ip_addr IS NULL FROM inventory_devices "
            " WHERE ip IS NOT NULL")).all()
    assert righe, "nessun indirizzo nel corpus: test vacuo"

    scritture_diverse = []
    for grezzo, scritto, nullo in righe:
        atteso = domain.parse_address(grezzo)
        if atteso is None:
            assert nullo, f"{grezzo!r}: la colonna doveva restare NULL"
            continue
        assert not nullo, f"{grezzo!r}: la colonna doveva contenere un indirizzo"
        # L'uguaglianza che conta: lo stesso VALORE, deciso da PostgreSQL.
        with engine.begin() as c:
            uguale = c.execute(text(
                "SELECT ip_addr = CAST(:canonico AS inet) FROM inventory_devices "
                " WHERE ip = :grezzo"),
                {"canonico": atteso.text, "grezzo": grezzo}).scalar_one()
        assert uguale is True, (
            f"{grezzo!r}: PostgreSQL e il parser non concordano sul valore")
        if scritto != atteso.text:
            scritture_diverse.append((grezzo, scritto, atteso.text))

    # E la differenza di SCRITTURA, fissata: riguarda gli IPv4-compatible, e solo loro.
    assert scritture_diverse == [("::a00:1", "::10.0.0.1", "::a00:1")], (
        f"l'insieme delle differenze di scrittura è cambiato: {scritture_diverse}")


# ==================================================================
# 4. scadenze e idoneità
# ==================================================================

NOTIF = load("notifications")["cases"]


def test_lo_sql_e_il_worker_sono_d_accordo_sull_idoneita(engine):
    """⚠ Il contratto dell'idoneità, verificato sui DUE percorsi SQL.

    La vista Scadenze deve mostrare tutti; il worker deve escludere i dismessi. Il
    corpus è lo stesso, e si esercita una volta contro `queries.expiries` e una contro
    `candidates.due_items_from_projection`: se una delle due prendesse la regola
    dell'altra, il test lo dice.
    """
    from app.notifications import candidates

    racks = []
    per_caso = {}
    fra_una_settimana = (TODAY + timedelta(days=7)).isoformat()
    for i, case in enumerate(NOTIF, start=1):
        du = uid("d", 700 + i)
        per_caso[i] = (du, case)
        racks.append({"_uid": uid("c", 700 + i), "id": f"E{i:03d}", "u": 10,
                      "devices": [{"_uid": du, "id": f"e{i}", "u": 1,
                                   "garanzia": fra_una_settimana,
                                   **case["device"]}]})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 7), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 7), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    bootstrap(engine, doc)

    with read_snapshot() as snap:
        vista = q.expiries(snap, today=TODAY, warning_days=90, limit=500)
        dal_worker = candidates.due_items_from_projection(
            snap, today=TODAY, warning_days=[90, 30, 7])

    nella_vista = {i["device"]["uid"] for i in vista.items}
    azionabili = {i["device"]["uid"] for i in vista.items if i["notifiable"]}
    dal_worker_uid = {i.entity_uid for i in dal_worker.items}

    for _, (du, case) in per_caso.items():
        assert du in nella_vista, (
            f"{case['device']}: la vista è ispettiva, deve mostrarlo")
        assert (du in dal_worker_uid) is case["eligible"], (
            f"{case['device']}: idoneità del worker")
        assert (du in azionabili) is case["eligible"], (
            f"{case['device']}: `notifiable` della vista non concorda col worker")

    # E la vista è un SOVRAINSIEME stretto: se coincidessero, una delle due avrebbe
    # preso la regola dell'altra.
    assert dal_worker_uid < nella_vista


EXPIRY_LEVELS = load("expiries")["level"]["cases"]


def test_i_livelli_dello_sql_sono_quelli_del_contratto(engine):
    """`expired` / `warning` / `future`, con la soglia della vista."""
    racks = []
    per_caso = {}
    for i, case in enumerate(EXPIRY_LEVELS, start=1):
        du = uid("d", 800 + i)
        scadenza = (TODAY + timedelta(days=case["days"])).isoformat()
        per_caso[du] = case
        racks.append({"_uid": uid("c", 800 + i), "id": f"L{i:03d}", "u": 10,
                      "devices": [{"_uid": du, "id": f"l{i}", "u": 1,
                                   "garanzia": scadenza}]})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 8), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 8), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    bootstrap(engine, doc)

    for soglia in sorted({c["warning"] for c in EXPIRY_LEVELS}):
        with read_snapshot() as snap:
            page = q.expiries(snap, today=TODAY, warning_days=soglia, limit=500)
        trovati = {i["device"]["uid"]: i for i in page.items}
        for du, case in per_caso.items():
            if case["warning"] != soglia:
                continue
            voce = trovati[du]
            assert voce["daysRemaining"] == case["days"]
            assert voce["level"] == case["level"], (
                f"{case['days']}gg soglia {soglia}: {voce['level']}")


def test_le_date_non_interpretabili_non_producono_scadenze(engine):
    """⚠ Il corpus delle date, sul lato SQL: le sette forme di `new Date` non esistono.

    La colonna derivata l'ha scritta `domain.parse_expiry`, quindi le forme che il
    contratto rifiuta non sono interrogabili — e il valore GREZZO resta comunque
    nell'inventario, che è la seconda metà della decisione (§8.50.7).
    """
    casi = [c for c in load("expiries")["parse"] if isinstance(c["raw"], str)]
    racks = []
    per_caso = {}
    for i, case in enumerate(casi, start=1):
        du = uid("d", 900 + i)
        per_caso[du] = case
        racks.append({"_uid": uid("c", 900 + i), "id": f"D{i:03d}", "u": 10,
                      "devices": [{"_uid": du, "id": f"g{i}", "u": 1,
                                   "garanzia": case["raw"]}]})
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 6), "id": "sito", "nome": "Sito",
        "sale": [{"_uid": uid("b", 6), "id": "sala", "nome": "Sala",
                  "w": 10, "h": 8, "vani": [], "racks": racks}]}]}
    bootstrap(engine, doc)

    with read_snapshot() as snap:
        page = q.expiries(snap, today=TODAY, warning_days=3650, limit=1000)
    per_uid = {i["device"]["uid"]: i for i in page.items}

    for du, case in per_caso.items():
        if case["date"] is None:
            assert du not in per_uid, (
                f"{case['raw']!r} non è una data del contratto e non deve comparire")
        else:
            assert du in per_uid, f"{case['raw']!r} doveva comparire"
            assert per_uid[du]["expiry"] == case["date"]
            # ⚠ E il valore grezzo esce INTATTO: `  2027-03-15  ` con gli spazi resta
            # com'è. Interpretarlo non significa riscriverlo.
            assert per_uid[du]["raw"] == case["raw"]

    # Il grezzo di TUTTI, anche dei rifiutati, è ancora nell'inventario.
    with engine.begin() as c:
        conservati = {r[0] for r in c.execute(text(
            "SELECT garanzia FROM inventory_devices "
            " WHERE garanzia IS NOT NULL")).all()}
    attesi = {case["raw"] for case in per_caso.values() if case["raw"] != ""}
    assert attesi <= conservati, sorted(attesi - conservati)


# ==================================================================
# 5. etichette
# ==================================================================

def test_le_etichette_dello_sql_sono_quelle_del_contratto(engine):
    """Nome → codice → «(senza nome)», sul contesto restituito dalle interrogazioni."""
    casi = load("labels")["context"]["cases"]
    racks = []
    per_caso = {}
    for i, case in enumerate(casi, start=1):
        du = uid("d", 1000 + i)
        per_caso[du] = case
        rack = {"_uid": uid("c", 1000 + i), "u": 10,
                "devices": [{"_uid": du, "id": f"z{i}", "u": 1,
                             "garanzia": (TODAY + timedelta(days=5)).isoformat()}],
                **case["rack"]}
        racks.append(rack)
    # Sito e sala sono per DOCUMENTO, non per rack: si prende il primo caso per loro e
    # gli altri si verificano solo sul rack. Un sito per caso vorrebbe dire un
    # documento per caso, cioè un bootstrap per caso.
    doc = {"schemaVersion": 1, "locations": [{
        "_uid": uid("a", 10), "sale": [{
            "_uid": uid("b", 10), "w": 10, "h": 8, "vani": [], "racks": racks,
            **casi[0]["room"]}],
        **casi[0]["location"]}]}
    bootstrap(engine, doc)

    with read_snapshot() as snap:
        page = q.expiries(snap, today=TODAY, warning_days=90, limit=500)
    per_uid = {i["device"]["uid"]: i for i in page.items}

    for du, case in per_caso.items():
        voce = per_uid[du]
        assert voce["rack"]["label"] == case["labels"]["rack"], case["rack"]
        assert voce["location"]["label"] == casi[0]["labels"]["location"]
        assert voce["room"]["label"] == casi[0]["labels"]["room"]

    # E nessuna etichetta è un valore dell'implementazione.
    for voce in page.items:
        for parte in ("rack", "room", "location"):
            assert voce[parte]["label"] not in ("None", "null", "undefined")
        assert voce["device"]["label"] not in ("None", "null", "undefined")
