"""Il lato Python del contratto di dominio (fase 2G, §8.50).

Esegue `fixtures/domain/*.json` contro `app/domain.py`. Le stesse fixture le esegue
`tools/domain-contract-tests.mjs` contro `handoff/domain.js`, e
`test_domain_sql_pg.py` contro lo SQL.

⚠ Le attese NON si aggiornano perché un test è rosso. Sono decisioni di prodotto
scritte a mano in `tools/make-domain-fixtures.mjs`: un test rosso dice che
l'implementazione non le segue ancora, non che l'attesa era sbagliata. L'unica volta in
cui si toccano è quando la decisione cambia — e allora il rosso è il messaggio giusto,
in tutte e tre le suite contemporaneamente.

Non c'è nessun database qui: `app/domain.py` è puro. Il lato SQL sta nel file
`test_domain_sql_pg.py`, che ha bisogno di PostgreSQL vero.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pytest

from app import domain

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "domain"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def iso(text: str) -> date:
    """La data di una fixture. Passa dal parser del contratto di proposito: se
    `parse_expiry` si rompesse, i corpora che ne dipendono devono fallire per quello e
    non per un secondo interprete scritto qui."""
    parsed = domain.parse_expiry(text)
    assert parsed is not None, f"fixture con data non ISO: {text!r}"
    return parsed


def ids_of(cases: list, key: str = "name") -> list[str]:
    return [str(c.get(key, i)) for i, c in enumerate(cases)]


# ==================================================================
# 1. presenza fisica e stato operativo, indipendenti
# ==================================================================

PRESENCE = load("presence")["cases"]


@pytest.mark.parametrize("case", PRESENCE, ids=ids_of(PRESENCE))
def test_stato_e_presenza_sono_indipendenti(case):
    device = case["device"]
    assert domain.stato_of(device) == case["stato"]
    assert domain.presenza_of(device) == case["presenza"]
    assert domain.occupies_space(device) is case["occupies"]
    assert domain.notifies(device) is case["notifies"]


def test_il_vocabolario_della_presenza_e_quello_dichiarato():
    assert domain.DEVICE_PRESENCES == ("presente", "rimosso")
    assert domain.DEFAULT_PRESENZA == "presente"


def test_la_presenza_non_si_deduce_dallo_stato():
    """Il caso che chiude §1: nessuna combinazione di stato implica una presenza.

    Detto in modo verificabile: per ogni stato esistono entrambe le presenze, e per
    ogni presenza entrambe le risposte a «occupa». Se qualcuno reintroducesse una
    deduzione — `presenza = rimosso if stato == dismesso` — una delle due mappe
    diventerebbe costante e questo test lo direbbe.
    """
    per_stato = {}
    for stato in domain.DEVICE_STATES:
        occupa = {domain.occupies_space({"stato": stato, "presenza": p})
                  for p in domain.DEVICE_PRESENCES}
        per_stato[stato] = occupa
        assert occupa == {True, False}, (
            f"lo stato {stato!r} determina l'occupazione: la presenza non è più "
            f"indipendente")
    for presenza in domain.DEVICE_PRESENCES:
        avvisa = {domain.notifies({"stato": s, "presenza": presenza})
                  for s in domain.DEVICE_STATES}
        assert avvisa == {True, False}, (
            f"la presenza {presenza!r} determina l'idoneità agli avvisi")


# ==================================================================
# 2. capacità: slot U distinti
# ==================================================================

CAPACITY = load("capacity")["cases"]


@pytest.mark.parametrize("case", CAPACITY, ids=ids_of(CAPACITY))
def test_capacita_conta_gli_slot_distinti(case):
    cap = domain.rack_capacity(case["rackU"], case["devices"])
    assert cap.used_u == case["usedU"]
    assert cap.free_u == case["freeU"]
    assert cap.largest_free_run == case["largestFreeRun"]
    assert domain.percent(cap.used_u, cap.total_u) == case["percent"]

    if "slots" in case:
        # L'insieme è la DEFINIZIONE; `rack_capacity` è l'implementazione che non
        # enumera. Confrontarli è ciò che impedisce all'ottimizzazione di divergere
        # dalla definizione che dovrebbe realizzare.
        slots = sorted(domain.occupied_slots(case["rackU"], case["devices"]))
        assert slots == case["slots"]
        assert len(slots) == cap.used_u


SUM_H_CASES = [c for c in CAPACITY if "sumH" in c]


@pytest.mark.parametrize("case", SUM_H_CASES, ids=ids_of(SUM_H_CASES))
def test_la_vecchia_definizione_somma_h_da_un_numero_diverso(case):
    """⚠ Il test che rende rossa la reintroduzione di `SUM(h)` (§13).

    Non basta che `used_u` sia giusto: bisogna che il caso DISTINGUA le due
    definizioni. Un corpus in cui `SUM(h)` e gli slot distinti coincidono passerebbe
    anche con l'implementazione sbagliata, e un test che non può fallire non protegge
    niente. Qui si calcola la formula vecchia — `sum(d.h || 1)` su tutti i
    dispositivi, presenza compresa — e si pretende che sia diversa.
    """
    somma = sum(1 if d.get("h") in (None, 0) else d["h"] for d in case["devices"])
    assert somma == case["sumH"], (
        "la formula legacy dichiarata nella fixture non è quella che dava il "
        "prototipo: `sum(d.h || 1)` su OGNI dispositivo")
    assert somma != case["usedU"], (
        f"{case['name']}: SUM(h) e gli slot distinti coincidono, quindi questo caso "
        f"non può accorgersi del ritorno del difetto")


def test_un_rack_enorme_non_enumera_nessuno_slot():
    """Il rack da tre miliardi di unità: il conto è sugli ESTREMI.

    Non si misura il tempo (una soglia temporale in una suite è un test che diventa
    rosso quando la macchina è occupata): si constata che il risultato è corretto su
    un'altezza per cui l'enumerazione non potrebbe terminare. Se qualcuno riscrivesse
    `rack_capacity` con un `range()`, questo test non finirebbe — ed è un modo di
    fallire perfettamente chiaro.
    """
    cap = domain.rack_capacity(3_000_000_000, [{"u": 1, "h": 2}])
    assert (cap.used_u, cap.free_u) == (2, 2_999_999_998)


# ==================================================================
# 3. percentuale HALF-UP
# ==================================================================

PERCENT = load("percent")["cases"]


@pytest.mark.parametrize("case", PERCENT,
                         ids=[f"{c['used']}su{c['total']}" for c in PERCENT])
def test_la_percentuale_arrotonda_meta_verso_l_alto(case):
    assert domain.percent(case["used"], case["total"]) == case["percent"]


def test_la_percentuale_non_usa_l_arrotondamento_di_python():
    """⚠ La controprova di §3: `round()` di Python dà una risposta DIVERSA.

    Senza questa, il corpus dimostrerebbe soltanto che `percent` restituisce dei
    numeri. Con questa, dimostra che restituisce numeri che l'arrotondamento nativo
    NON avrebbe dato — cioè che il difetto esisteva e che è chiuso.
    """
    divergenti = [(u, t) for u, t in [(1, 8), (1, 200), (5, 200), (1, 40)]
                  if round(u / t * 100) != domain.percent(u, t)]
    assert divergenti, (
        "nessun caso in cui round() di Python differisce: il corpus non può "
        "accorgersi se qualcuno lo reintroduce")
    assert domain.percent(1, 8) == 13 and round(1 / 8 * 100) == 12


def test_la_percentuale_non_usa_la_virgola_mobile():
    """Nessun `.5` deve dipendere da come un float rappresenta un decimale.

    Si esercita ogni `used/total` con `total` fino a 400: sono i casi in cui
    `used*100/total` cade esattamente su una metà, e sono anche quelli in cui
    `floor(x + 0.5)` in virgola mobile può sbagliare per un epsilon.
    """
    for total in range(1, 401):
        for used in range(0, total + 1):
            atteso = (used * 200 + total) // (total * 2)
            assert domain.percent(used, total) == atteso, (used, total)


# ==================================================================
# 4. file: identità del gruppo ≠ etichetta mostrata
# ==================================================================

ROWS = load("rows")


@pytest.mark.parametrize("case", ROWS["cases"], ids=ids_of(ROWS["cases"]))
def test_il_gruppo_di_una_fila(case):
    group = domain.row_group({"row": case["row"]})
    assert group.assigned is case["assigned"]
    assert group.value == case["value"]
    assert group.label == case["label"]


def test_fila_non_impostata_e_fila_uguale_a_trattino_sono_gruppi_distinti():
    """⚠ Il difetto §4, e il test che lo rende rosso se torna.

    Il prototipo raggruppava per `rk.row || '—'`, e nel seed di produzione esiste un
    rack la cui fila È «—» (CS-Q01): quel rack finiva insieme a tutti quelli senza
    fila. Le due CHIAVI devono differire; le due ETICHETTE devono coincidere, perché
    l'aspetto dell'interfaccia non cambia.
    """
    unset = domain.row_group({"row": ROWS["distinctGroups"]["unset"]})
    dash = domain.row_group({"row": ROWS["distinctGroups"]["literalDash"]})
    assert unset.key != dash.key
    assert unset.label == dash.label == domain.ROW_UNSET_LABEL
    assert unset.assigned is False and dash.assigned is True


def test_la_chiave_del_gruppo_non_puo_collidere_con_un_valore_del_documento():
    """La chiave contiene un byte NUL, che nessun valore di documento può contenere.

    Non è un dettaglio di implementazione: è la RAGIONE per cui la collisione non può
    ripresentarsi. Con un separatore stampabile, un rack la cui fila valesse
    esattamente quel separatore ricreerebbe il difetto — e sarebbe la stessa storia,
    con un carattere diverso.
    """
    from app.inventory.json_strings import is_representable_text

    for value in (None, "", "—", "A", "\x00none", "\x00row\x00A"):
        group = domain.row_group({"row": value})
        assert "\x00" in group.key
    assert not is_representable_text("\x00")


def test_i_gruppi_si_ordinano_col_residuo_per_ultimo():
    groups, seen = [], set()
    for raw in ROWS["ordering"]["input"]:
        group = domain.row_group({"row": raw})
        if group.key not in seen:
            seen.add(group.key)
            groups.append(group)
    ordered = sorted(groups, key=domain.row_sort_key)
    assert [g.label for g in ordered] == ROWS["ordering"]["expectedLabels"]
    assert [g.assigned for g in ordered] == ROWS["ordering"]["expectedAssigned"]


# ==================================================================
# 5. scadenze: un interprete di date solo
# ==================================================================

EXPIRIES = load("expiries")


@pytest.mark.parametrize("case", EXPIRIES["parse"],
                         ids=[repr(c["raw"]) for c in EXPIRIES["parse"]])
def test_l_interprete_delle_date(case):
    got = domain.parse_expiry(case["raw"])
    assert (None if got is None else got.isoformat()) == case["date"]


@pytest.mark.parametrize("case", EXPIRIES["parseNonString"],
                         ids=[repr(c["raw"]) for c in EXPIRIES["parseNonString"]])
def test_un_valore_non_testuale_non_e_una_data(case):
    assert domain.parse_expiry(case["raw"]) is None


def test_lo_scanner_delle_scadenze_usa_QUESTO_parser():
    """⚠ Un interprete solo, e lo si dimostra sull'IDENTITÀ della funzione.

    `notifications/expiry.parse_expiry` e `relational.DERIVED` devono essere questo
    stesso oggetto, non una funzione che si comporta allo stesso modo: due funzioni
    equivalenti oggi divergono domani, e divergono sui casi limite — che sono
    precisamente quelli che un inventario compilato a mano produce.
    """
    from app.notifications import expiry as scanner

    assert scanner.parse_expiry is domain.parse_expiry


@pytest.mark.parametrize("case", EXPIRIES["days"]["cases"],
                         ids=[f"{c['today']}->{c['expiry']}"
                              for c in EXPIRIES["days"]["cases"]])
def test_i_giorni_rimanenti_sono_una_differenza_fra_date(case):
    assert (iso(case["expiry"]) - iso(case["today"])).days == case["days"]


@pytest.mark.parametrize("case", EXPIRIES["level"]["cases"],
                         ids=[f"{c['days']}gg-soglia{c['warning']}"
                              for c in EXPIRIES["level"]["cases"]])
def test_il_livello_ispettivo(case):
    assert domain.expiry_level(case["days"], case["warning"]) == case["level"]


@pytest.mark.parametrize("case", EXPIRIES["notificationDue"]["cases"],
                         ids=[f"{c['days']}gg-{c['windows']}"
                              for c in EXPIRIES["notificationDue"]["cases"]])
def test_la_regola_della_finestra_del_worker(case):
    assert domain.notification_due(case["days"], case["windows"]) is case["due"]


# ==================================================================
# 6. idoneità agli avvisi
# ==================================================================

NOTIF = load("notifications")["cases"]


@pytest.mark.parametrize("case", NOTIF, ids=[repr(c["device"]) for c in NOTIF])
def test_l_idoneita_dipende_solo_dallo_stato(case):
    assert domain.notifies(case["device"]) is case["eligible"]


def test_solo_dismesso_e_inidoneo():
    assert domain.NOTIFY_INELIGIBLE_STATES == ("dismesso",)
    idonei = [s for s in domain.DEVICE_STATES if domain.notifies({"stato": s})]
    assert idonei == ["attivo", "manutenzione", "dismissione"]


# ==================================================================
# 7. indirizzi
# ==================================================================

ADDR = load("addresses")


@pytest.mark.parametrize("case", ADDR["parse"],
                         ids=[repr(c["raw"]) for c in ADDR["parse"]])
def test_l_interprete_degli_indirizzi(case):
    got = domain.parse_address(case["raw"])
    assert (None if got is None else got.family) == case["family"]
    if case["family"] is not None:
        assert got.text == case["text"]


@pytest.mark.parametrize("case", ADDR["query"],
                         ids=[repr(c["raw"]) for c in ADDR["query"]])
def test_l_interprete_delle_query_di_indirizzo(case):
    got = domain.parse_address_query(case["raw"])
    assert (None if got is None else got.kind) == case["kind"]
    if case["kind"] is not None:
        assert got.family == case["family"]
        assert [got.lo.text, got.hi.text] == [case["lo"], case["hi"]]


MATCHES = ADDR["matches"]["cases"]


@pytest.mark.parametrize("case", MATCHES,
                         ids=[f"{c['query']}-{c['ip']}" for c in MATCHES])
def test_un_indirizzo_cade_o_no_nell_intervallo(case):
    query = domain.parse_address_query(case["query"])
    assert domain.address_matches(case["ip"], query) is case["match"]


def test_un_ip_esatto_non_trova_il_suo_prefisso():
    """⚠ Il difetto §13 più visibile: `10.0.0.1` non deve trovare `10.0.0.100`.

    Ci sono due modi di sbagliarlo, e questo test chiude entrambi: la ricerca esatta
    che degenera in sottostringa, e la sottostringa che resta l'unica modalità perché
    `10.0.0.1` non viene riconosciuto come indirizzo. Da qui le due asserzioni.
    """
    query = domain.parse_address_query("10.0.0.1")
    assert query is not None and query.kind == "exact", (
        "un IP esatto non è riconosciuto come indirizzo: finisce nella ricerca "
        "testuale, e `10.0.0.1` è una sottostringa di `10.0.0.100`")
    assert domain.address_matches("10.0.0.1", query) is True
    for vicino in ("10.0.0.10", "10.0.0.100", "110.0.0.1", "10.0.0.123"):
        assert domain.address_matches(vicino, query) is False, vicino
    # E la controprova: come TESTO quel prefisso combacia davvero.
    assert domain.contains("10.0.0.100", "10.0.0.1") is True


def test_le_famiglie_non_si_mescolano():
    v4 = domain.parse_address_query("0.0.0.0/0")
    v6 = domain.parse_address_query("::/0")
    assert domain.address_matches("2001:db8::1", v4) is False
    assert domain.address_matches("10.0.0.1", v6) is False
    # Stesso valore numerico, famiglie diverse: NON combacia.
    quattro = domain.parse_address("10.0.0.1")
    sei = domain.parse_address("::a00:1")
    assert quattro.value == sei.value
    assert domain.address_matches("::a00:1",
                                  domain.parse_address_query("10.0.*")) is False


def test_non_esistono_intervalli_ne_jolly_ipv6():
    """§5: non si inventa una grammatica che nessuno ha chiesto."""
    for q in ("2001:db8::1-2001:db8::9", "2001:db8::*", "::*", "fe80::1 - fe80::9"):
        assert domain.parse_address_query(q) is None, q


FUZZ = load("addresses-fuzz")


def test_il_corpus_differenziale_degli_indirizzi():
    """⚠ Attese CALCOLATE da `handoff/domain.js`, e qui sta la loro utilità.

    Non è un giudizio di prodotto — quelli stanno in `addresses.json`, scritti a mano
    — è la pretesa che le DUE implementazioni non divergano su nessuna delle forme
    mutate. Cinquemila forme non si benedicono una per una; si può però pretendere che
    nessuna riceva due risposte diverse, e questo è il posto dove si vede.
    """
    divergenze = []
    for raw, want in FUZZ["verdicts"].items():
        got = domain.parse_address(raw)
        mine = (None if got is None else
                {"family": got.family, "value": str(got.value), "text": got.text})
        if mine != want["address"]:
            divergenze.append((raw, mine, want["address"]))

        gotq = domain.parse_address_query(raw)
        mineq = (None if gotq is None else
                 {"family": gotq.family, "kind": gotq.kind, "lo": gotq.lo.text,
                  "hi": gotq.hi.text, "loValue": str(gotq.lo.value),
                  "hiValue": str(gotq.hi.value)})
        if mineq != want["query"]:
            divergenze.append((raw, mineq, want["query"]))

    assert not divergenze, (
        f"{len(divergenze)} divergenze fra Python e JavaScript sulle "
        f"{FUZZ['count']} forme del corpus; le prime tre: {divergenze[:3]}")


def test_il_corpus_differenziale_esercita_davvero_entrambi_i_rami():
    """Un corpus di sole forme rifiutate darebbe zero divergenze senza dimostrare
    niente. Si pretende che una parte sostanziale sia riconosciuta."""
    riconosciuti = sum(1 for v in FUZZ["verdicts"].values() if v["query"] is not None)
    assert FUZZ["count"] >= 2000, FUZZ["count"]
    assert riconosciuti >= 500, (
        f"solo {riconosciuti} forme su {FUZZ['count']} sono indirizzi: il corpus "
        f"non esercita il ramo che accetta")


# ==================================================================
# 8. ricerca testuale
# ==================================================================

SEARCH = load("search")


def test_i_campi_cercabili_sono_quelli_dichiarati():
    assert list(domain.DEVICE_SEARCH_FIELDS) == SEARCH["deviceFields"]
    assert list(domain.RACK_SEARCH_FIELDS) == SEARCH["rackFields"]
    assert "note" not in domain.DEVICE_SEARCH_FIELDS


@pytest.mark.parametrize("case", SEARCH["device"], ids=ids_of(SEARCH["device"]))
def test_la_ricerca_sui_dispositivi(case):
    assert domain.device_matches(case["device"], case["q"].lower()) is case["match"]


@pytest.mark.parametrize("case", SEARCH["rack"], ids=ids_of(SEARCH["rack"]))
def test_la_ricerca_sui_rack(case):
    assert domain.rack_matches(case["rack"], case["q"].lower()) is case["match"]


def test_percento_e_underscore_sono_caratteri_letterali():
    """⚠ Con `LIKE` una query contenente `%` troverebbe tutto. Il test lo dice
    confrontando i due esiti sullo stesso dato: se qualcuno passasse a `LIKE`, il
    secondo asserto diventerebbe rosso."""
    assert domain.device_matches({"name": "Sconto 50%"}, "%") is True
    assert domain.device_matches({"name": "Nodo Alfa"}, "%") is False
    assert domain.device_matches({"name": "nodo_alfa"}, "_") is True
    assert domain.device_matches({"name": "nodo-alfa"}, "_") is False


# ==================================================================
# 9. etichette
# ==================================================================

LABELS = load("labels")


@pytest.mark.parametrize("case", LABELS["device"],
                         ids=[repr(c["device"]) for c in LABELS["device"]])
def test_l_etichetta_di_un_dispositivo(case):
    assert domain.device_label(case["device"]) == case["label"]


@pytest.mark.parametrize("case", LABELS["rack"],
                         ids=[repr(c["rack"]) for c in LABELS["rack"]])
def test_l_etichetta_di_un_rack(case):
    assert domain.rack_label(case["rack"]) == case["label"]


@pytest.mark.parametrize("case", LABELS["room"],
                         ids=[repr(c["room"]) for c in LABELS["room"]])
def test_l_etichetta_di_una_sala(case):
    assert domain.room_label(case["room"]) == case["label"]


@pytest.mark.parametrize("case", LABELS["location"],
                         ids=[repr(c["location"]) for c in LABELS["location"]])
def test_l_etichetta_di_un_sito(case):
    assert domain.location_label(case["location"]) == case["label"]


CONTEXT = LABELS["context"]["cases"]


@pytest.mark.parametrize("case", CONTEXT, ids=[repr(c["rack"]) for c in CONTEXT])
def test_il_contesto_resta_strutturato(case):
    """⚠ Tre campi separati, e mai una stringa unica da spezzare dopo.

    Il corpus porta i valori che l'impacchettamento su `/` corrompeva: `10.0.0.0/24`
    diventava `10.0.0.0`, e un sito con uno `/` nel codice spostava di un posto sito,
    sala e rack.
    """
    assert {
        "location": domain.location_label(case["location"]),
        "room": domain.room_label(case["room"]),
        "rack": domain.rack_label(case["rack"]),
    } == case["labels"]


def test_nessuna_etichetta_e_un_valore_dell_implementazione():
    """⚠ Mai «None», e la fase 2F lo produceva di proposito (§8.48 voce 11).

    Si esercita ogni forma di assenza, non solo quelle delle fixture: è la proprietà
    che non deve avere eccezioni.
    """
    vietati = {"None", "undefined", "null", "NULL", "nan"}
    for value in (None, "", 0, False, [], {}, 0.0):
        for etichetta in (domain.device_label({"name": value, "id": value}),
                          domain.rack_label({"name": value, "id": value}),
                          domain.room_label({"nome": value, "id": value}),
                          domain.location_label({"nome": value, "id": value})):
            assert etichetta == domain.NO_NAME, (value, etichetta)
            assert etichetta not in vietati
    assert domain.label(None, None) == "(senza nome)"


def test_la_barra_in_un_codice_non_viene_troncata():
    """La divergenza voluta della 2F, ora contratto: `10.0.0.0/24` resta intero."""
    assert domain.rack_label({"id": "10.0.0.0/24"}) == "10.0.0.0/24"
    assert domain.location_label({"id": "a/b/c"}) == "a/b/c"
