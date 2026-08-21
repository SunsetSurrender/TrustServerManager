"""Il DELTA fra la semantica misurata (2E) e quella decisa (2G). PostgreSQL vero.

⚠ Questo file era la suite di PARITÀ della fase 2E: confrontava lo SQL con le attese
calcolate facendo girare il JavaScript del prototipo, copiato alla lettera in
`tools/make-query-fixtures.mjs`. La fase 2G ha CAMBIATO quella semantica di proposito
(§8.48, §8.50), quindi una parte di quei confronti deve fallire — e cancellarli sarebbe
il modo peggiore di prenderne atto.

Da qui la riscrittura: le fixture della 2E restano, e diventano il **termine di
paragone storico**. Ogni confronto è ora di una delle due specie:

  - **invariato**: la 2G non ha toccato quel comportamento, e la parità deve reggere
    ancora. Sono la maggior parte, e sono ciò che dimostra che la 2G ha cambiato SOLO
    quello che dichiara;
  - **delta dichiarato**: la 2G ha deciso diversamente, e il test pretende che la
    differenza sia ESATTAMENTE quella dichiarata — non «diversa», ma diversa in quel
    modo e in nessun altro.

La seconda specie è la ragione per cui questo file vale più di prima. Un test
cancellato dice «qui non guardiamo più»; un test che fissa il delta dice «qui è
cambiato questo, e nient'altro» — ed è l'unica forma che si accorge se domani cambiasse
anche qualcos'altro.

Le fixture del CONTRATTO della 2G stanno altrove (`fixtures/domain/`, eseguite da
`test_domain_contract.py` e `test_domain_sql_pg.py`): là si verifica la conformità a
ciò che si è deciso, qui la distanza da ciò che c'era.

    fixtures/query/*.json    documento + attese del legacy, per 29 corpora
    tools/make-query-fixtures.mjs   il generatore, con gli algoritmi verbatim

L'obiettivo è una migrazione di COMPORTAMENTO. Una query SQL più elegante che
restituisce un insieme diverso non è un miglioramento: è un prodotto diverso, e chi la
usa non se ne accorge finché non cerca qualcosa che prima trovava.

Tre famiglie
------------
  ricerca    la barra globale: sottostringa senza distinzione di maiuscole su cinque
             campi del dispositivo e tre del rack, più le quattro forme IP
  capacità   la vista Capacità: slot DISTINTI occupati, non `SUM(h)`
  scadenze   la vista Scadenze: garanzia e supporto, livelli, ordine per data

Dove la parità NON è possibile, e perché è scritto invece che nascosto
--------------------------------------------------------------------
Tre corpora portano `quirks`: documenti che il backend accetta e che il frontend non
sa calcolare — `rack.u` assente (RangeError), `rack.u` da tre miliardi (memoria
esaurita), `rack.u` stringa (coercizione silenziosa). Là non si pretende parità: si
verifica che lo SQL risponda con un numero sensato e si REGISTRA la divergenza.

E un corpus, `expiry-parsing`, è una divergenza voluta: `new Date(v)` del frontend
accetta `2027/03/15` e `March 15, 2027`, che `parse_expiry` del backend rifiuta. Le
colonne derivate sono la sorgente interrogabile (§10 del requisito), quindi lo SQL
segue loro. Il test elenca esattamente quali forme divergono.

Riferimento: BACKEND-PLAN.md §8.46.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import Actor, InventoryRepository, DocumentRejectedError
from app.inventory import queries as q
from app.inventory.document import strip_legacy_fields

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "query"
ADMIN = Actor(username="capo", role="admin")


def _load_corpora() -> dict[str, dict]:
    if not FIXTURES.is_dir():
        raise RuntimeError(
            f"fixture non trovate in {FIXTURES}. Generarle con "
            "`node tools/make-query-fixtures.mjs` (vedi l'intestazione del generatore)")
    out = {}
    for path in sorted(FIXTURES.glob("*.json")):
        if path.name.startswith("_"):      # ingressi del generatore, non corpora
            continue
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    if not out:
        raise RuntimeError(f"nessun corpus in {FIXTURES}")
    return out


CORPORA = _load_corpora()

#: Documenti che il MAGAZZINO delle istantanee rifiuta (§8.16), quindi non possono
#: essere una versione da interrogare.
#:
#: ⚠ VUOTO, e la ragione è interessante: `jsonb-hostile-numbers` sarebbe stato l'unico,
#: e non è fra i corpora perché `-0.0` e `1e+20` non sopravvivono a un giro attraverso
#: JavaScript (`JSON.stringify(-0)` è `"0"`). Il generatore lo esclude di proposito
#: invece di produrre una variante normalizzata col nome di quella ostile. Il tuple
#: resta perché la condizione può ripresentarsi, e `test_the_documents_that_cannot_be
#: _stored_are_exactly_this_list` pretende che sia vero: se un documento cominciasse a
#: essere rifiutato, quel test lo direbbe.
NOT_STORABLE: tuple[str, ...] = ()

#: Corpus di DIVERGENZA voluta sull'interpretazione delle date: ha un test proprio che
#: elenca le forme, e resta fuori dal ciclo di parità delle scadenze.
PARSING_DIVERGENCE = "expiry-parsing"

STRICT = [n for n, c in CORPORA.items()
          if not c.get("quirks") and n not in NOT_STORABLE]
QUIRKY = [n for n, c in CORPORA.items() if c.get("quirks")]
SEARCHABLE = [n for n in STRICT if CORPORA[n].get("search")]
EXPIRABLE = [n for n in STRICT if n != PARSING_DIVERGENCE]


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


def _photo_ids(doc: dict) -> set[str]:
    """Gli UUID delle foto che il documento referenzia.

    Si ricavano dal documento invece di essere scritti a mano: alcuni corpora vengono
    dai generatori Python e portano le loro foto, e una costante copiata qui
    smetterebbe di essere vera al primo cambio di fixture — con un errore
    (`PhotoNotFound`) che non dice «la costante è vecchia».
    """
    out = set()
    for L in doc.get("locations", []) or []:
        for R in L.get("sale", []) or []:
            for k in R.get("racks", []) or []:
                foto = k.get("foto")
                if isinstance(foto, str) and foto:
                    out.add(foto)
    return out


def _clean(engine, doc: dict | None = None) -> None:
    """Database pulito, con le foto che il documento pretende.

    ⚠ Le foto devono ESISTERE prima del bootstrap: un documento che referenzia una
    foto assente non è un documento a cui manca un pezzo, è un documento non valido
    (§8.5), e il repository lo rifiuta. Le si cancellano DOPO i rack, perché
    `inventory_racks.photo_id` le protegge — la stessa chiave esterna che impedisce
    alla GC di portare via la foto dello stato corrente.
    """
    import hashlib

    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM inventory_projection_state"))
        c.execute(text("DELETE FROM photos"))
        for n, photo_id in enumerate(sorted(_photo_ids(doc or {}))):
            payload = b"\x89PNG\r\n\x1a\n" + bytes([n % 256])
            c.execute(text("""
                INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
                VALUES (:i, 'image/png', :b, :s, :n)
            """), {"i": photo_id, "b": payload, "n": len(payload),
                   "s": hashlib.sha256(payload).hexdigest()})


def load(engine, name: str) -> dict:
    """Bootstrappa il documento del corpus e restituisce il corpus.

    Il bootstrap è la strada più corta per avere una testa E una proiezione coerenti
    (fase 2C): mantiene entrambe nella stessa transazione, quindi dopo di lui le query
    hanno una proiezione attuale senza bisogno di nessun `--rebuild`.
    """
    corpus = CORPORA[name]
    doc = corpus["doc"]
    _clean(engine, doc)
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(doc, ADMIN, from_legacy=True)
    return corpus


def snapshot(engine):
    """Una connessione di SOLA LETTURA in REPEATABLE READ, come quella delle rotte.

    Non è cosmetica: le query pretendono uno snapshot stabile, e provarle su una
    connessione ordinaria vorrebbe dire non provare la condizione in cui girano.
    """
    return engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True)


# ==================================================================
# 1. parità della RICERCA
# ==================================================================

def _identita(results: list) -> list:
    """L'insieme dei risultati per IDENTITÀ, in ordine.

    Si confrontano gli `uid` e non i dizionari interi: la 2G ha aggiunto campi alle
    voci (`label`, `presenza`) e un confronto strutturale fallirebbe per l'aggiunta di
    un campo invece che per un risultato diverso. Ciò che va confrontato con le
    fixture della 2E è QUALI entità la ricerca trova, e in quale ordine.
    """
    return [(r["kind"], (r["device"]["uid"] if r["kind"] == "device"
                         else r["rack"]["uid"])) for r in results]


def _identita_legacy(results: list) -> list:
    return [(r["kind"], (r["device"]["uid"] if r["kind"] == "device"
                         else r["rack"]["uid"])) for r in results]


@pytest.mark.parametrize("name", SEARCHABLE, ids=SEARCHABLE)
def test_search_is_a_superset_of_the_frontend_and_only_by_the_new_fields(engine, name):
    """⚠ DELTA DICHIARATO: la ricerca trova di più, e solo per i motivi dichiarati.

    La 2G ha aggiunto quattro campi (`id`, `tipo`, `stato`, `presenza`) e la
    partecipazione di `extra` (§8.50.6). Quindi l'insieme dei risultati testuali è un
    **sovrainsieme** di quello della 2E, e ogni voce in più deve essere spiegata da uno
    di quei motivi.

    Il test non si accontenta di «è cambiato»: per ogni risultato nuovo verifica che
    combaci su un campo che PRIMA non si cercava. Se comparisse per un motivo che non è
    nell'elenco — un `LIKE` scappato, una tokenizzazione, un `OR` sbagliato — il test
    lo dice invece di accettarlo.

    ⚠ Le query di INDIRIZZO sono escluse da questo confronto e hanno il loro test: là
    la 2G non aggiunge, **cambia** — `10.0.0.1` passa dalla modalità testo alla
    modalità indirizzo, quindi l'insieme può rimpicciolire.
    """
    from app import domain

    corpus = load(engine, name)
    nuovi_campi = ("id", "tipo", "stato", "presenza")
    with snapshot(engine) as conn:
        with conn.begin():
            for case in corpus["search"]:
                if case.get("legacyThrows"):
                    continue
                # Le forme che la 2G riconosce come indirizzo cambiano modalità.
                if domain.parse_address_query(case["q"].strip()) is not None:
                    continue
                page = q.search(conn, q=case["q"], limit=q.SEARCH_MAX_LIMIT)
                mio = _identita(page.results)
                legacy = _identita_legacy(case["results"])

                assert set(legacy) <= set(mio), (
                    f"{name} q={case['q']!r}: la 2G ha PERSO risultati, e non doveva "
                    f"perderne nessuno sui campi testuali\n"
                    f"  mancanti {sorted(set(legacy) - set(mio))}")
                # L'ordine dei risultati comuni non cambia: è l'ordine di documento.
                assert [x for x in mio if x in set(legacy)] == legacy, (
                    f"{name} q={case['q']!r}: l'ordine dei risultati comuni è cambiato")

                needle = case["q"].strip().lower()
                for kind, uid in set(mio) - set(legacy):
                    obj = _entita(corpus["doc"], kind, uid)
                    assert obj is not None, (kind, uid)
                    if kind == "rack":
                        # I rack hanno gli stessi tre campi di prima: l'unica novità è
                        # `extra`, cioè i `seriali` non rappresentabili in colonna.
                        assert domain.rack_matches(obj, needle), (
                            f"{name} q={case['q']!r}: rack {uid} in più senza un "
                            f"campo che combaci")
                        continue
                    motivi = [f for f in nuovi_campi
                              if domain.contains(domain.device_search_value(obj, f),
                                                 needle)]
                    assert motivi, (
                        f"{name} q={case['q']!r}: dispositivo {uid} in più, e nessuno "
                        f"dei campi NUOVI combacia: non è il delta dichiarato")


def _entita(doc: dict, kind: str, uid: str):
    for L in doc.get("locations") or []:
        for R in L.get("sale") or []:
            for K in R.get("racks") or []:
                if kind == "rack" and K.get("_uid") == uid:
                    return K
                for V in K.get("devices") or []:
                    if kind == "device" and V.get("_uid") == uid:
                        return V
    return None


def _brief(results: list) -> list:
    """Etichette leggibili: un diff fra due liste di dizionari annidati è illeggibile."""
    out = []
    for r in results:
        if r["kind"] == "device":
            out.append(f"dev:{r['device']['name']}@{r['rack']['code']}")
        else:
            out.append(f"rack:{r['rack']['code']}")
    return out


def test_search_is_case_insensitive_substring_and_not_tokenised(engine):
    """La forma del confronto, isolata: sottostringa, non parola.

    Il rischio concreto della fase 2E era sostituire `includes` con la ricerca
    full-text di PostgreSQL, che tokenizza: `to_tsquery('web')` NON trova
    `SRV-Web-01`, perché il token è `srv-web-01`. Sarebbe una ricerca «migliore» che
    perde risultati, e l'utente non ha modo di capire perché.
    """
    load(engine, "search-text")
    with snapshot(engine) as conn:
        with conn.begin():
            def nomi(query):
                return sorted(r["device"]["name"] for r in
                              q.search(conn, q=query, limit=200).results
                              if r["kind"] == "device")

            # sottostringa in mezzo a una parola, che un tokenizzatore perderebbe
            assert nomi("eb-0") == ["SRV-Web-01", "srv-web-02"]

            # ⚠ La query arriva già in minuscolo (`search` fa `.strip().lower()`),
            # quindi l'insensibilità alle maiuscole dipende SOLO da `lower(colonna)`. Va
            # provata su valori che HANNO maiuscole, e la prima stesura di questo test
            # non lo faceva: confrontava `nomi("EB-0") == nomi("eb-0")`, che sono la
            # stessa chiamata dopo la normalizzazione — un'asserzione che non poteva
            # fallire. L'ha trovata una mutazione che togliva `lower()` dalla colonna:
            # la parità è diventata rossa, questo test è rimasto verde.
            assert nomi("srv-web-01") == ["SRV-Web-01"], \
                "manca `lower()` sulla colonna: un valore con maiuscole non si trova"
            assert nomi("poweredge r750") == ["SRV-Web-01"]
            assert nomi("rossi") == ["SRV-Web-01", "srv-web-02"], \
                "`owner` è 'Rossi' e 'ROSSI': entrambi devono combaciare"
            assert nomi("abc123") == ["SRV-Web-01"], "`serial` è 'ABC123'"
            # E la query, dal suo lato, viene normalizzata: due grafie danno lo stesso
            # insieme. Vale la pena affermarlo, ma NON è la stessa proprietà di sopra.
            assert nomi("SRV-WEB-01") == nomi("srv-web-01")
            # sottostringa a cavallo di uno spazio: combacia con `PowerEdge R750` E
            # con `poweredge r640`, perché la sottostringa non conosce le parole. Era
            # la mia attesa a essere sbagliata, non il codice.
            assert nomi("edge r") == ["SRV-Web-01", "srv-web-02"]
            assert nomi("edge r7") == ["SRV-Web-01"]
            # una sola lettera è una query legittima
            assert len(nomi("a")) >= 1


def test_search_treats_percent_and_underscore_as_plain_characters(engine):
    """`%` e `_` sono caratteri, non jolly.

    È il difetto che `LIKE` introdurrebbe senza farsi notare: con
    `LIKE '%' || :q || '%'` una query contenente `%` combacia con tutto, e una
    contenente `_` con qualunque carattere. `strpos` non ha questo problema, e questo
    test è ciò che impedisce a un futuro «ottimizziamo con LIKE» di passare inosservato.
    """
    load(engine, "search-text")
    with snapshot(engine) as conn:
        with conn.begin():
            assert q.search(conn, q="%", limit=200).results == []
            assert q.search(conn, q="_", limit=200).results == []
            assert q.search(conn, q="srv%", limit=200).results == []
            assert q.search(conn, q="s_v-web", limit=200).results == []
            # e il controllo positivo, altrimenti il test passerebbe anche con una
            # ricerca che non trova mai niente
            assert q.search(conn, q="srv", limit=200).results != []


def test_an_address_query_matches_no_racks_even_when_a_rack_is_named_like_an_ip(engine):
    """In modalità INDIRIZZO i rack non partecipano — invariato, e confermato (§8.48 v.3).

    Il corpus ha un rack che si chiama `10.0.0.1` proprio per rendere il caso
    osservabile. Un rack con quel nome non è una macchina con quell'indirizzo, e
    restituirlo a chi cerca un host è un falso positivo che *sembra* una risposta.
    """
    load(engine, "search-ip")
    with snapshot(engine) as conn:
        with conn.begin():
            for query in ("10.0.0.0/24", "10.0.0.1", "10.0.0.1-10.0.0.9", "10.0.*"):
                page = q.search(conn, q=query, limit=200)
                assert page.address is not None, f"{query} non riconosciuto"
                assert all(r["kind"] == "device" for r in page.results), (
                    f"{query}: un rack è entrato in modalità indirizzo")


def test_an_exact_ip_no_longer_matches_its_own_prefix(engine):
    """⚠ DELTA DICHIARATO, e il più visibile di tutta la fase (§8.48 voce 1).

    Prima: `10.0.0.1` non era una forma riconosciuta, quindi finiva nella ricerca
    TESTUALE — e come sottostringa combaciava con `10.0.0.100` e col rack che si
    chiama «10.0.0.1». Chi cercava una macchina precisa riceveva la sua vicina di
    sottorete.

    Adesso: `10.0.0.1` significa quell'indirizzo.

    ⚠ Il test fissa ENTRAMBI i comportamenti, il vecchio e il nuovo. Il vecchio non è
    più raggiungibile dall'endpoint, quindi lo si riproduce con la sottostringa nuda —
    che è ciò che il prototipo faceva — e si constata che DAREBBE una risposta diversa.
    Senza quella metà, il test direbbe soltanto «trova una cosa», non «ha smesso di
    trovarne tre».
    """
    from app import domain

    load(engine, "search-ip")
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.search(conn, q="10.0.0.1", limit=200)
            assert page.address is not None
            assert page.address.kind == "exact"
            assert (page.address.lo, page.address.hi) == ("10.0.0.1", "10.0.0.1")
            trovati = {r["device"]["code"] for r in page.results}
            assert trovati == {"d-10-0-0-1", "d-doppio"}, trovati
            assert "d-10-0-0-100" not in trovati, (
                "il falso positivo da sottostringa è tornato")
            assert not any(r["kind"] == "rack" for r in page.results)

    # La controprova: come TESTO quella query combaciava davvero con tre dispositivi.
    # Se questa metà smettesse di reggere, il corpus non conterrebbe più il caso e il
    # test sopra passerebbe senza dimostrare niente.
    assert domain.contains("10.0.0.100", "10.0.0.1")
    assert domain.contains("10.0.0.1", "10.0.0.1")


def test_ipv6_is_now_searchable_by_address_and_by_network(engine):
    """⚠ DELTA DICHIARATO (§8.48 voce 2): l'IPv6 si trova per indirizzo e per CIDR.

    Prima `ipToNum` era IPv4 e un dispositivo IPv6 era raggiungibile solo per testo.
    Adesso per indirizzo esatto, per CIDR, e ancora per testo.

    ⚠ E le FAMIGLIE non si mescolano: un CIDR IPv6 che copre tutto (`::/0`) non trova
    nessun IPv4, e viceversa. È una proprietà del tipo `inet` — che ordina per famiglia
    e poi per indirizzo — e il test la pretende DAL DATABASE invece di fidarsi della
    documentazione.
    """
    load(engine, "search-ip")
    with snapshot(engine) as conn:
        with conn.begin():
            esatto = q.search(conn, q="2001:db8::1", limit=200)
            assert esatto.address is not None and esatto.address.family == 6
            assert {r["device"]["code"] for r in esatto.results} == {"d-ipv6"}

            rete = q.search(conn, q="2001:db8::/32", limit=200)
            assert {r["device"]["code"] for r in rete.results} == {"d-ipv6"}

            # Tutto IPv6 non contiene nessun IPv4...
            tutto_v6 = q.search(conn, q="::/0", limit=200)
            assert {r["device"]["code"] for r in tutto_v6.results} == {"d-ipv6"}
            # ...e tutto IPv4 non contiene nessun IPv6.
            tutto_v4 = q.search(conn, q="0.0.0.0/0", limit=200)
            assert "d-ipv6" not in {r["device"]["code"] for r in tutto_v4.results}
            assert tutto_v4.results, "il corpus non ha nessun IPv4: test vacuo"

            # E resta cercabile come testo, che è la strada che aveva prima.
            testo = q.search(conn, q="2001:db8", limit=200)
            assert "d-ipv6" in {r["device"]["code"] for r in testo.results
                                if r["kind"] == "device"}


def test_half_an_address_is_still_a_text_search(engine):
    """`10.0.0` non è un indirizzo, ed è giusto che sia testo — invariato.

    Non è un'incoerenza con il test sopra: mezzo indirizzo è un prefisso, e chi lo
    scrive sta cercando una sottorete a mano. La differenza fra le due modalità è che
    la prima esiste solo per le forme che hanno un significato di rete preciso.
    """
    load(engine, "search-ip")
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.search(conn, q="10.0.0", limit=200)
            assert page.address is None
            trovati = {r["device"]["code"] for r in page.results
                       if r["kind"] == "device"}
            assert {"d-10-0-0-1", "d-10-0-0-100"} <= trovati


def test_an_empty_query_returns_nothing(engine):
    """Casella vuota: il frontend non cerca (`if (q)`), e nemmeno noi.

    Restituire l'inventario intero sarebbe la risposta comoda: un client che monta la
    pagina con la casella vuota si porterebbe a casa tutto senza chiederlo.
    """
    load(engine, "search-text")
    with snapshot(engine) as conn:
        with conn.begin():
            for vuota in ("", "   ", "\t"):
                page = q.search(conn, q=vuota, limit=200)
                assert page.results == []
                assert page.address is None
                # ma la revisione c'è: la risposta descrive comunque una revisione
                assert page.revision.version >= 1


# ==================================================================
# 2. parità della CAPACITÀ
# ==================================================================

@pytest.mark.parametrize("name", STRICT, ids=STRICT)
def test_capacity_matches_the_frontend(engine, name):
    """Per ogni sala: totale, occupate, percentuale, conteggio rack, file, e per ogni
    rack: occupate e blocco contiguo libero più grande."""
    corpus = load(engine, name)
    atteso_per_sala = {r["roomUid"]: r
                       for L in corpus["capacity"] for r in L["rooms"]}

    with snapshot(engine) as conn:
        with conn.begin():
            report = q.capacity(conn)

    viste = {r["uid"]: r for L in report.locations for r in L["rooms"]}
    assert set(viste) == set(atteso_per_sala), \
        f"{name}: insieme di sale diverso"

    for uid, atteso in atteso_per_sala.items():
        vista = viste[uid]
        assert vista["totalU"] == atteso["totalU"], f"{name}/{uid}: U totali"
        assert vista["usedU"] == atteso["usedU"], f"{name}/{uid}: U occupate"
        assert vista["occupancyPercent"] == atteso["occupancyPercent"], \
            f"{name}/{uid}: percentuale"
        assert vista["rackCount"] == atteso["rackCount"], f"{name}/{uid}: n. rack"

        # miglior slot: il rack col blocco libero più lungo
        if atteso["bestFreeRun"] == 0:
            assert vista["bestRack"] is None, f"{name}/{uid}: nessun blocco libero"
        else:
            assert vista["bestRack"] is not None, f"{name}/{uid}: manca il miglior slot"
            assert vista["bestRack"]["freeRun"] == atteso["bestFreeRun"], \
                f"{name}/{uid}: lunghezza del blocco libero"
            assert vista["bestRack"]["code"] == atteso["bestRackCode"], \
                f"{name}/{uid}: rack del miglior slot"

        # ⚠ DELTA DICHIARATO sulle FILE (§8.48 voce 7). Il prototipo raggruppava per
        # `rk.row || '—'`, quindi «fila non impostata» e «fila il cui valore è —»
        # finivano nello stesso gruppo. La 2G li separa.
        #
        # Il confronto è quindi sulle ETICHETTE aggregate, non sui gruppi: la somma di
        # `totalU` per etichetta deve reggere ancora — nessuna unità è comparsa o
        # sparita — mentre il NUMERO di gruppi può essere maggiore. È la forma più
        # stretta che si può pretendere senza rifiutare il cambiamento.
        def _per_etichetta(righe, chiave="rowLabel"):
            out = {}
            for b in righe:
                e = b.get(chiave, b.get("row"))
                tot, usa = out.get(e, (0, 0))
                out[e] = (tot + b["totalU"], usa + b["usedU"])
            return out

        assert _per_etichetta(vista["rows"]) == _per_etichetta(atteso["rows"], "row"), \
            f"{name}/{uid}: le unità per ETICHETTA di fila non tornano"
        assert len(vista["rows"]) >= len(atteso["rows"]), \
            f"{name}/{uid}: la 2G separa i gruppi, non li fonde"

        per_rack = {r["uid"]: r for r in vista["racks"]}
        for rack_atteso in atteso["racks"]:
            rack = per_rack[rack_atteso["uid"]]
            assert rack["usedU"] == rack_atteso["usedU"], \
                f"{name}/{uid}/{rack_atteso['code']}: U occupate"
            assert rack["largestFreeRun"] == rack_atteso["largestFreeRun"], \
                f"{name}/{uid}/{rack_atteso['code']}: blocco contiguo"
            assert rack["u"] == rack_atteso["u"], \
                f"{name}/{uid}/{rack_atteso['code']}: altezza"
            assert rack["deviceCount"] == rack_atteso["deviceCount"]


def test_used_u_is_not_the_sum_of_heights(engine):
    """⚠ La scoperta che regge tutta la famiglia capacità, isolata in un test.

    Se `used_u` fosse `SUM(h)` — la traduzione che verrebbe naturale scrivere — questo
    test sarebbe rosso in tre modi diversi, e il corpus li contiene tutti e tre di
    proposito:

      sovrapposti   due dispositivi da 4 U che si accavallano su 3 slot occupano 5
                    slot, non 8
      oltre l'altezza  un dispositivo da 10 U che parte allo slot 4 di un rack da 5
                    occupa 2 slot, non 10
      pieno         un rack da 4 U con due dispositivi da 2 U è pieno: 4, e non «4 per
                    caso perché la somma torna»

    ⚠ Sui DISMESSI la 2G ha cambiato la DOMANDA, non la risposta (§8.48 voce 6). Il
    prototipo aveva un ramo vuoto — `if ((d.stato || 'attivo') === 'dismesso') {}` —
    che *non* li escludeva, quindi occupavano; ma lo stato operativo non è ciò che
    decide se un apparato occupa uno slot. Adesso decide la PRESENZA FISICA, e i
    corpora della 2E non hanno `presenza`, quindi canonicalizzano a `presente` e
    occupano ancora: i numeri qui sotto sono gli stessi, per una ragione diversa e
    dichiarata. Il caso `rimosso` è coperto dalle fixture del contratto e da
    `test_domain_sql_pg.py`.
    """
    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            report = q.capacity(conn)
    per_codice = {r["code"]: r for L in report.locations for room in L["rooms"]
                  for r in room["racks"]}

    # due da 4 U che si sovrappongono su [3,5]: slot 2..6 = 5, non 8
    assert per_codice["R-overlap"]["usedU"] == 5
    # da 10 U allo slot 4 in un rack da 5: slot 4,5 = 2, non 10
    assert per_codice["R-oltre"]["usedU"] == 2
    # rack da 4 U pieno
    assert per_codice["R-pieno"]["usedU"] == 4
    assert per_codice["R-pieno"]["largestFreeRun"] == 0
    # rack vuoto: tutto libero, e il blocco contiguo è il rack intero
    assert per_codice["R-vuoto"]["usedU"] == 0
    assert per_codice["R-vuoto"]["largestFreeRun"] == 10
    # dismessi: OCCUPANO (2 U) più il dispositivo attivo (1 U)
    assert per_codice["R-dismessi"]["usedU"] == 3, \
        "il frontend NON esclude i dismessi dalla capacità: il ramo è un blocco vuoto"


def test_capacity_edge_positions_follow_the_frontend(engine):
    """Slot iniziale ≤ 0, altezza 0 e altezza negativa.

    Sono i casi in cui un ciclo `for` di JavaScript si comporta in un modo che nessuno
    ha progettato: `u = 0` occupa lo slot 0 (che non si conta) e l'1; `u` negativo non
    tocca niente di contato; `h = 0` vale 1 per via di `d.h || 1`; `h` negativo non
    entra nemmeno nel ciclo. L'unione di intervalli riproduce tutti e quattro.
    """
    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            report = q.capacity(conn)
    limiti = next(r for L in report.locations for room in L["rooms"]
                  for r in room["racks"] if r["code"] == "R-limiti")
    # z0 (u=0,h=2) → slot 1 · zneg (u=-3,h=2) → niente · h0 (u=4,h=0) → slot 4
    # hneg (u=5,h=-2) → niente.  Totale: 2 slot.
    assert limiti["usedU"] == 2


def test_capacity_percentage_rounds_like_javascript(engine):
    """`Math.round` di JavaScript, non `round()` di Python.

    Differiscono esattamente su .5: JavaScript arrotonda verso l'alto,
    Python arrotonda al pari (`round(0.5) == 0`). Con un rack da 8 U e 4 occupate la
    percentuale è 50 e nessuno se ne accorge; il caso che divide è una sala in cui il
    rapporto cade su un mezzo per cento, e allora un client che confronta il numero del
    server con quello del browser vede due valori diversi per la stessa sala.
    """
    load(engine, "capacity")
    corpus = CORPORA["capacity"]
    atteso = {r["roomUid"]: r["occupancyPercent"]
              for L in corpus["capacity"] for r in L["rooms"]}
    with snapshot(engine) as conn:
        with conn.begin():
            report = q.capacity(conn)
    for L in report.locations:
        for room in L["rooms"]:
            assert room["occupancyPercent"] == atteso[room["uid"]]

    # E la regola in astratto, dove si vede la differenza fra i due arrotondamenti.
    assert int((1 / 8) * 100 + 0.5) == 13        # 12.5 → 13 come JavaScript
    assert round((1 / 8) * 100) == 12           # round() di Python darebbe 12


# ==================================================================
# 3. parità delle SCADENZE
# ==================================================================

@pytest.mark.parametrize("name", EXPIRABLE, ids=EXPIRABLE)
def test_expiries_match_the_frontend(engine, name):
    """Stesse righe, stesso ordine, stessi giorni, stessi livelli.

    `today` è la data di riferimento della fixture, e i giorni del frontend sono stati
    calcolati alla mezzanotte locale di quel giorno: è la condizione sotto la quale
    `Math.round((dt - now)/86400000)` e `(scadenza - oggi).days` coincidono
    esattamente. Vedi la nota nel generatore.
    """
    corpus = load(engine, name)
    today = date.fromisoformat(corpus["refDate"])

    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today,
                              warning_days=corpus["warningDays"],
                              limit=q.EXPIRY_MAX_LIMIT)

    # ⚠ Si confronta con le righe che il BACKEND è in grado di interpretare.
    #
    # `new Date(v)` del frontend accetta `2026-2-3` e il 29 febbraio di un anno non
    # bisestile; `parse_expiry` no. Le colonne derivate le ha scritte `parse_expiry`,
    # quindi quelle righe non esistono in SQL — e non è un difetto dello SQL, è la
    # divergenza fra due interpretatori di date, elencata da
    # `test_the_date_parsing_divergence_is_exactly_this_set`.
    #
    # La marcatura `isoStrict` viene dal generatore, e un test separato dimostra che
    # concorda con `parse_expiry` vero su ogni valore di ogni corpus: senza quella
    # prova, filtrare qui vorrebbe dire confrontare lo SQL con sé stesso.
    # ⚠ DELTA DICHIARATO: le fixture della 2E riproducevano il `continue` del
    # prototipo, che SALTAVA i dispositivi dismessi. La 2G li include, perché la vista
    # è ispettiva (§8.48 voce 8). Quindi il confronto si fa a dismessi esclusi — quello
    # è il sottoinsieme in cui la parità deve reggere — e le voci dei dismessi si
    # verificano a parte, qui sotto.
    dismessi = {i["device"]["uid"] for i in page.items
                if i["device"]["stato"] == "dismesso"}
    voci = [i for i in page.items if i["device"]["uid"] not in dismessi]

    atteso = [i for i in corpus["expiries"] if i["isoStrict"]]
    scartate = [i["raw"] for i in corpus["expiries"] if not i["isoStrict"]]
    assert len(voci) == len(atteso), (
        f"{name}: {len(voci)} scadenze SQL (dismessi esclusi) contro "
        f"{len(atteso)} del frontend\n"
        f"  SQL     {[(i['kind'], i['raw']) for i in voci]}\n"
        f"  legacy  {[(i['kind'], i['raw']) for i in atteso]}\n"
        f"  scartate perché il backend non le interpreta: {scartate}")

    # E le voci dei dismessi: compaiono, e portano `notifiable: false`. Se
    # comparissero con `notifiable: true` il worker e la vista sarebbero di nuovo in
    # disaccordo, questa volta al contrario.
    for voce in page.items:
        if voce["device"]["uid"] in dismessi:
            assert voce["notifiable"] is False, (
                f"{name}: un dismesso dichiarato azionabile")

    for sql_item, js_item in zip(voci, atteso):
        assert sql_item["device"]["uid"] == js_item["device"]["uid"], \
            f"{name}: ordine diverso"
        assert sql_item["kind"] == js_item["kind"]
        assert sql_item["raw"] == js_item["raw"]
        assert sql_item["daysRemaining"] == js_item["daysRemaining"], \
            f"{name}: giorni per {js_item['raw']}"
        assert sql_item["level"] == js_item["level"], \
            f"{name}: livello per {js_item['raw']}"
        assert sql_item["rack"]["uid"] == js_item["rack"]["uid"]
        assert sql_item["room"]["uid"] == js_item["room"]["uid"]
        assert sql_item["location"]["uid"] == js_item["location"]["uid"]


def test_expiries_now_include_decommissioned_devices_and_say_so(engine):
    """⚠ ROVESCIATO dalla 2G (§8.48 voce 8, §7 del requisito).

    Prima: la vista SALTAVA i dismessi e la capacità li includeva — la stessa
    applicazione, due significati per lo stesso stato, e nessuno l'aveva deciso.

    Adesso: la vista è ISPETTIVA e li mostra, perché un apparato dismesso ha un
    contratto che scade e chi fa l'inventario dei contratti deve poterlo vedere. Il
    worker non gli manda avvisi, e la risposta lo DICE con `notifiable`.

    ⚠ Il test pretende tutte e tre le cose: che compaiano, che siano marcati non
    azionabili, e che il filtro sappia isolarli. Solo la prima sarebbe un test che
    passa anche se `notifiable` fosse costante.
    """
    corpus = load(engine, "expiries")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, limit=500)
            solo = q.expiries(conn, today=today, limit=500, stato="dismesso")

    nomi = {i["device"]["name"] for i in page.items}
    assert "dismesso" in nomi, "un dispositivo dismesso deve essere ispezionabile"
    assert "in dismissione" in nomi

    per_nome = {i["device"]["name"]: i for i in page.items}
    assert per_nome["dismesso"]["notifiable"] is False
    # `in dismissione` non è un valore del vocabolario (`dismissione` lo è): resta
    # idoneo, perché uno stato fuori elenco non si esclude a naso.
    assert per_nome["in dismissione"]["notifiable"] is True

    assert {i["device"]["name"] for i in solo.items} == {"dismesso"}
    # ⚠ E i totali distinguono «in finestra» da «azionabile»: se `notifiable`
    # coincidesse sempre con `warning`, il campo non direbbe niente.
    assert page.totals["notifiable"] < page.totals["warning"] + page.totals["expired"]


def test_expiries_ignore_unparseable_and_empty_values(engine):
    """«in attesa», stringa vuota, campo assente: non compaiono e non sono un guasto.

    È la semantica stabilita in §8.41 e §8.42: un campo scritto a mano è un dato, non
    un errore. Il posto dove si nota è la vista Scadenze, che lo ignora nei calcoli, e
    l'inventario, che lo conserva — e infatti `GET /api/inventory` lo restituisce
    ancora.
    """
    corpus = load(engine, "expiries")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, limit=500)
            # il testo è ancora nella colonna, e la data derivata è NULL
            row = conn.execute(text(
                "SELECT supporto, supporto_date FROM inventory_devices "
                " WHERE supporto = 'in attesa'")).one()
    assert row[0] == "in attesa" and row[1] is None
    assert all(i["raw"] != "in attesa" for i in page.items)
    assert all(i["raw"] not in ("", None) for i in page.items)


def test_expiry_levels_and_thresholds(engine):
    """I tre livelli e la soglia, sul giorno esatto in cui cambiano.

    `giorni < 0` scaduto, `giorni <= 90` entro la soglia, oltre futuro. Il corpus ha
    di proposito il 90 e il 91: una soglia sbagliata di uno è il difetto che nessuno
    nota fino al giorno in cui un avviso non arriva.
    """
    corpus = load(engine, "expiries")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, warning_days=90, limit=500)
    per_nome = {}
    for i in page.items:
        per_nome.setdefault(i["device"]["name"], []).append(i)

    assert per_nome["oggi"][0]["daysRemaining"] == 0
    assert per_nome["oggi"][0]["level"] == "warning"
    assert per_nome["ieri"][0]["level"] == "expired"
    assert per_nome["ieri"][0]["daysRemaining"] == -1
    assert per_nome["novanta"][0]["level"] == "warning"
    assert per_nome["novantuno"][0]["level"] == "future"
    assert per_nome["futura"][0]["level"] == "future"
    # totali coerenti con le righe
    assert page.totals["expired"] == sum(1 for i in page.items
                                         if i["level"] == "expired")
    assert page.totals["warning"] == sum(1 for i in page.items
                                         if i["level"] == "warning")
    assert page.totals["future"] == sum(1 for i in page.items
                                        if i["level"] == "future")


def test_a_device_with_both_dates_yields_two_rows(engine):
    """Garanzia e supporto sono due scadenze, non una con due date."""
    corpus = load(engine, "expiries")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, limit=500)
    entrambe = [i for i in page.items if i["device"]["name"] == "entrambe"]
    assert sorted(i["kind"] for i in entrambe) == ["garanzia", "supporto"]
    assert len({i["device"]["uid"] for i in entrambe}) == 1


def test_devices_sharing_a_business_id_are_distinct_rows(engine):
    """Due dispositivi con lo stesso `id`: due righe, identità diverse.

    L'`id` di business arriva dall'import tabellare e può ripetersi (§8.42). Se le
    query lo usassero per identificare, una delle due righe scomparirebbe — e
    scomparirebbe silenziosamente, che è il modo peggiore.
    """
    corpus = load(engine, "expiries")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, limit=500)
            ricerca = q.search(conn, q="doppio", limit=200)
    doppi = [i for i in page.items if i["device"]["code"] == "e-doppio"]
    assert len(doppi) == 2
    assert len({i["device"]["uid"] for i in doppi}) == 2
    trovati = [r for r in ricerca.results if r["kind"] == "device"]
    assert len(trovati) == 2
    assert len({r["device"]["uid"] for r in trovati}) == 2


def test_the_iso_strict_marking_agrees_with_parse_expiry():
    """⚠ La contromisura alla riscrittura, e senza di essa la parità sarebbe circolare.

    Il generatore delle fixture marca ogni data con `isoStrict`, calcolata da una
    RISCRITTURA in JavaScript di `parse_expiry`. La parità delle scadenze usa quella
    marcatura per filtrare, quindi se la riscrittura fosse sbagliata il filtro
    nasconderebbe proprio le righe su cui lo SQL sbaglia.

    Qui si chiude il cerchio: per OGNI valore di OGNI corpus, la marcatura deve
    concordare con `parse_expiry` vero. Non serve un database — è un confronto fra due
    funzioni pure — quindi il test gira anche senza PostgreSQL.
    """
    from app.notifications.expiry import parse_expiry

    controllati = 0
    for name, corpus in CORPORA.items():
        for item in corpus.get("expiries") or []:
            atteso = parse_expiry(item["raw"]) is not None
            assert item["isoStrict"] == atteso, (
                f"{name}: la marcatura di {item['raw']!r} dice "
                f"{item['isoStrict']} e `parse_expiry` dice {atteso}. La riscrittura "
                "in JavaScript di `parse_expiry` è divergente: correggere "
                "`isoStrict` in tools/make-query-fixtures.mjs")
            controllati += 1
    assert controllati > 30, \
        f"solo {controllati} date controllate: il corpus non copre abbastanza"


def test_the_date_parsing_divergence_is_exactly_this_set(engine):
    """⚠ Divergenza VOLUTA, misurata ed elencata (§11 del requisito).

    `new Date(v)` del frontend accetta molto più di `parse_expiry`, che pretende
    `YYYY-MM-DD` esatto. Le colonne derivate sono la sorgente interrogabile, quindi lo
    SQL segue loro: certe forme che la vista Scadenze mostra non compaiono in questa
    query.

    Non si è aggiunto un secondo interprete di date, e la ragione sta già scritta in
    `relational._parse_expiry`: due idee di «data valida» in due moduli divergono, e
    divergono sui casi limite. Meglio una divergenza NOTA fra due strati che due
    parser che si credono d'accordo.

    Questo test è l'elenco. Se domani `parse_expiry` diventasse più permissivo, o il
    frontend più severo, diventerebbe rosso e la differenza andrebbe ridiscussa invece
    di scoperta.
    """
    corpus = load(engine, "expiry-parsing")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            page = q.expiries(conn, today=today, limit=500)

    dal_frontend = {i["raw"] for i in corpus["expiries"]}
    dallo_sql = {i["raw"] for i in page.items}

    solo_frontend = dal_frontend - dallo_sql
    solo_sql = dallo_sql - dal_frontend

    assert solo_sql == set(), \
        f"lo SQL interpreta date che il frontend non interpreta: {solo_sql}"

    # La marcatura del generatore e la realtà devono concordare: se divergessero,
    # sarebbe la marcatura a essere sbagliata, e con essa il filtro della parità.
    atteso_divergente = {i["raw"] for i in corpus["expiries"] if not i["isoStrict"]}
    assert solo_frontend == atteso_divergente, (
        "l'insieme divergente non è quello che la marcatura dichiara.\n"
        f"  solo frontend: {sorted(solo_frontend)}\n"
        f"  marcate non-ISO: {sorted(atteso_divergente)}")

    # E l'elenco per esteso: è LA documentazione della divergenza. Se cambia, va
    # ridiscussa di proposito invece di essere scoperta da un utente.
    assert solo_frontend == {
        "2027-3-15",              # mese e giorno a una cifra
        "2027/03/15",             # separatore diverso
        "March 15, 2027",         # forma testuale inglese
        "2027-03-15T10:00:00Z",   # ISO con ora
        "2027-03",                # anno-mese
        "2027",                   # solo anno
        "2027-02-30",             # 30 febbraio: V8 lo ROTOLA al 2 marzo
    }, ("l'insieme delle forme divergenti è cambiato. Non è necessariamente un "
        f"difetto, ma va deciso di proposito.\n  solo frontend: {sorted(solo_frontend)}")

    # Le forme che entrambi rifiutano restano nell'inventario e in nessuna scadenza.
    # `15/03/2027` è fra queste: V8 la legge come mese 15 e la scarta, quindi non è una
    # divergenza — l'avevo messa nell'elenco per analogia con `2027/03/15`, e la misura
    # mi ha corretto.
    for entrambi_rifiutano in ("in attesa", "da definire", "domani", "15/03/2027",
                               "2027-13-01", "0000-00-00"):
        assert entrambi_rifiutano not in dal_frontend, \
            f"{entrambi_rifiutano!r}: ora il frontend la interpreta"
        assert entrambi_rifiutano not in dallo_sql
    # ⚠ Il 30 febbraio è nell'elenco dei divergenti, non fra i rifiutati da entrambi:
    # `new Date('2027-02-30')` in V8 non è invalida, ROTOLA al 2 marzo. Quindi la vista
    # Scadenze mostra una scadenza al 2 marzo per un testo che dice 30 febbraio, e lo
    # SQL non la mostra affatto. Fra le due, quella che non inventa una data è la nostra.
    assert "2027-02-30" not in dallo_sql, "30 febbraio non è una data per il backend"
    assert "2027-02-30" in solo_frontend,         "V8 non rotola più il 30 febbraio: la divergenza è cambiata"
    # `  2027-03-15  ` con spazi: `parse_expiry` fa `.strip()`, quindi la accetta.
    assert "  2027-03-15  " in dallo_sql


# ==================================================================
# 4. dove il legacy NON è calcolabile
# ==================================================================

@pytest.mark.parametrize("name", QUIRKY, ids=QUIRKY)
def test_quirky_documents_do_not_break_the_queries(engine, name):
    """Documenti che il frontend non sa calcolare: lo SQL deve comunque rispondere.

    Tre casi reali, tutti trovati facendo girare il generatore delle fixture e non
    leggendo il codice:

      `rack.u` assente        `new Array(NaN)` → RangeError
      `rack.u` = 3 000 000 000  alloca tre miliardi di elementi → memoria esaurita
      `rack.u` = "45"         non solleva: coerce, e i totali della sala diventano
                              concatenazioni di stringhe ('04545')

    Non c'è parità da misurare, ma c'è un requisito: lo SQL non deve né sollevare né
    impiegare un tempo proporzionale all'altezza dichiarata del rack. È il motivo per
    cui la capacità è un'unione di intervalli e non un `generate_series`: su un rack da
    tre miliardi di U quella traduzione produrrebbe tre miliardi di righe dentro una
    richiesta HTTP.
    """
    corpus = load(engine, name)
    assert corpus["quirks"], "corpus senza stranezze nel ciclo delle stranezze"

    with snapshot(engine) as conn:
        with conn.begin():
            # non solleva, e la struttura è quella attesa
            report = q.capacity(conn)
            assert report.locations
            for L in report.locations:
                for room in L["rooms"]:
                    assert room["usedU"] >= 0
                    assert room["totalU"] >= 0
                    assert 0 <= room["occupancyPercent"] <= 100
                    for rack in room["racks"]:
                        assert rack["usedU"] >= 0
                        assert rack["largestFreeRun"] >= 0
            # e le altre due famiglie rispondono
            q.search(conn, q="a", limit=50)
            q.expiries(conn, today=date.fromisoformat(corpus["refDate"]), limit=50)


def test_a_rack_with_no_height_in_the_column_has_no_capacity(engine):
    """⚠ RISCRITTO nella 2H. Il soggetto è cambiato, e il fatto vecchio è altrove.

    Prima: si leggeva `rel-oversized-integers`, che portava `u = 3 000 000 000`, e si
    pretendeva di trovarlo in `extra` con la colonna NULL. Dalla 2H quel documento non
    entra più — `rack.u` fuori da `1..2^31-1` è rifiutato con `rack_u_out_of_range`
    (voce 16 del registro) — quindi il corpus non contiene più quel rack, e il test
    diventava rosso per una ragione giusta.

    Ciò che QUESTO modulo deve continuare a provare non è la mappa (è il soggetto di
    `test_domain_sql_pg.test_un_rack_piu_alto_di_int32_e_RIFIUTATO`), ma
    l'INTERROGAZIONE: un rack la cui colonna `u` è NULL non ha altezza e non ha slot, e
    la vista Capacità deve dirlo senza inciampare.

    Quel caso resta raggiungibile — un dato storico entrato quando il cancello non
    c'era — e si scrive come tale, con la stessa tecnica del test che segue: la colonna
    a mano. Passare dal documento sarebbe impossibile, che è il punto.
    """
    load(engine, "capacity")
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_racks SET u = NULL "
                       " WHERE code = 'R-parziale'"))

    with snapshot(engine) as conn:
        with conn.begin():
            report = q.capacity(conn)
    racks = [r for L in report.locations for room in L["rooms"] for r in room["racks"]]
    senza = [r for r in racks if r["code"] == "R-parziale"]
    assert senza, "il rack senza altezza non compare nel resoconto: sparirebbe da una vista"
    r = senza[0]
    assert r["u"] is None
    assert r["usedU"] == 0, "senza altezza non ci sono slot da occupare"
    assert r["largestFreeRun"] == 0
    # ⚠ `freeU` è `None`, non `0`, e la differenza è quella fra «nessuna unità libera»
    # e «non si sa quante ne siano libere». Dire zero sarebbe una risposta precisa a
    # una domanda senza risposta.
    assert r["freeU"] is None
    # I dispositivi restano CONTATI: il rack non ha altezza, ma le sue macchine
    # esistono. Perderle qui vorrebbe dire che un rack malformato nasconde il proprio
    # contenuto.
    assert r["deviceCount"] > 0

    stanza = [room for L in report.locations for room in L["rooms"]
              if any(x["code"] == "R-parziale" for x in room["racks"])][0]
    assert stanza["totalU"] == sum((x["u"] or 0) for x in stanza["racks"]), (
        "il totale della sala deve ignorare l'altezza sconosciuta, non propagarla")
    assert stanza["occupancyPercent"] >= 0


def test_a_huge_but_storable_rack_height_does_not_enumerate_slots(engine):
    """La proprietà vera: il costo NON dipende dall'altezza dichiarata del rack.

    Si porta un rack a due miliardi di U — sotto il limite di `int4`, quindi
    memorizzabile — scrivendo direttamente nella colonna come farebbe un DBA, e si
    misura. Con `generate_series(1, 2000000000)` questa chiamata non tornerebbe:
    l'unione di intervalli costa quanto i DISPOSITIVI, e i dispositivi sono tre.

    ⚠ Si scrive la colonna a mano di proposito: passando dal documento il valore
    finirebbe in `extra` e non ci sarebbe niente da misurare — che è esattamente
    l'errore in cui è caduta la prima stesura di questo test.
    """
    import time

    load(engine, "capacity")
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_racks SET u = 2000000000 "
                       " WHERE code = 'R-parziale'"))

    with snapshot(engine) as conn:
        with conn.begin():
            t0 = time.perf_counter()
            report = q.capacity(conn)
            trascorso = time.perf_counter() - t0

    alto = [r for L in report.locations for room in L["rooms"]
            for r in room["racks"] if (r["u"] or 0) == 2_000_000_000]
    assert alto, "il rack alto non è nella risposta"
    assert trascorso < 5.0, f"la capacità ha impiegato {trascorso:.1f}s: enumera?"
    # I dispositivi sono gli stessi di prima: 1 U allo slot 1 e 2 U allo slot 5.
    assert alto[0]["usedU"] == 3
    # E il blocco contiguo libero è quasi tutto il rack: dallo slot 7 alla fine.
    assert alto[0]["largestFreeRun"] == 2_000_000_000 - 6


# ==================================================================
# 5. la precondizione e la revisione
# ==================================================================

def test_every_response_carries_the_revision_it_describes(engine):
    """`version` e `sha256` di ogni risposta sono quelli della testa (§4)."""
    load(engine, "capacity")
    with engine.begin() as c:
        version = c.execute(text("SELECT version FROM inventory_head "
                                 " WHERE id IS TRUE")).scalar_one()
        sha = c.execute(text("SELECT canonical_sha256 FROM inventory_versions "
                             " WHERE version = :v"), {"v": version}).scalar_one()

    with snapshot(engine) as conn:
        with conn.begin():
            for revisione in (q.search(conn, q="a").revision,
                              q.capacity(conn).revision,
                              q.expiries(conn, today=date(2026, 8, 10)).revision):
                assert revisione.version == version
                assert revisione.sha256 == sha


@pytest.mark.parametrize("guasto", [
    "DELETE FROM inventory_projection_state",
    # ⚠ La versione VECCHIA e non una inventata: `head_version` ha una chiave esterna
    # verso `inventory_versions`, quindi `= 999` fallisce con una violazione di vincolo
    # e non prova niente sulla precondizione. Serve una versione che esista: da qui il
    # secondo salvataggio più sotto.
    "UPDATE inventory_projection_state SET head_version = 1",
    "UPDATE inventory_projection_state SET head_sha256 = repeat('0', 64)",
    "UPDATE inventory_projection_state SET mapper_version = 99",
    "UPDATE inventory_projection_state SET mapper_version = NULL",
], ids=["assente", "versione", "digest", "mappa", "mappa-nulla"])
def test_queries_fail_closed_on_a_stale_projection(engine, guasto):
    """Tutte e tre le famiglie rifiutano, e nessuna ripiega sul JSON (§3).

    Il ripiego sarebbe possibile — il documento è a due tabelle di distanza — e
    sarebbe la scelta sbagliata per la stessa ragione della fase 2D: filtrare il JSON
    darebbe la risposta giusta e nasconderebbe il difetto.
    """
    from app.inventory import ProjectionNotCurrentError

    corpus = load(engine, "capacity")
    # Un secondo salvataggio, così esiste una versione 1 diversa dalla testa (2) e
    # `head_version = 1` è una versione VECCHIA e non inesistente.
    import copy
    modificato = copy.deepcopy(corpus["doc"])
    modificato["locations"][0]["nome"] = "rinominato per avere due versioni"
    with engine.begin() as c:
        repo = InventoryRepository(c)
        repo.save(repo.head_version(), modificato, ADMIN)
    with engine.begin() as c:
        c.execute(text(guasto))

    with snapshot(engine) as conn:
        with conn.begin():
            for chiamata in (lambda: q.search(conn, q="a"),
                             lambda: q.capacity(conn),
                             lambda: q.expiries(conn, today=date(2026, 8, 10))):
                with pytest.raises(ProjectionNotCurrentError):
                    chiamata()


def test_queries_do_not_reassemble_the_document(engine):
    """⚠ Le query NON pagano il costo di fedeltà della fase 2D (§12).

    La prova è per comportamento: si corrompe una colonna in modo che il documento
    riassemblato cambierebbe. `GET /api/inventory` diventa 503 `projection_inconsistent`
    perché riassembla e ricalcola il digest; le query invece RISPONDONO, perché la
    proiezione è attuale e loro non riassemblano niente.

    Non è una lacuna, è la divisione del lavoro decisa in §8.45 e confermata in §12 del
    requisito: riassemblare a ogni ricerca costerebbe il 70% Python misurato in
    §8.45.1. Se un giorno si volesse la verifica anche qui, va misurata prima.
    """
    from app.inventory import ProjectionInconsistentError, projection

    load(engine, "capacity")
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_racks SET name = 'CORROTTO' WHERE uid = "
                       "(SELECT uid FROM inventory_racks ORDER BY uid LIMIT 1)"))

    with snapshot(engine) as conn:
        with conn.begin():
            # il percorso di fedeltà se ne accorge...
            with pytest.raises(ProjectionInconsistentError):
                projection.current_document(conn)
            # ...e le query rispondono, perché la proiezione è ATTUALE
            assert q.capacity(conn).locations
            assert q.search(conn, q="corrotto").results != [], \
                "la colonna corrotta è cercabile: le query leggono le TABELLE"


# ==================================================================
# 6. paginazione
# ==================================================================

def test_search_pagination_walks_every_result_exactly_once(engine):
    """Cursore a chiave: sfogliando si ottiene l'insieme completo, senza ripetizioni.

    Con `OFFSET` questo test passerebbe su un inventario fermo e fallirebbe a
    intermittenza su uno vivo. Con una chiave deterministica non c'è intermittenza da
    cercare.
    """
    load(engine, "seed")
    with snapshot(engine) as conn:
        with conn.begin():
            tutto = q.search(conn, q="r0", limit=q.SEARCH_MAX_LIMIT)
            assert len(tutto.results) > 5, "serve una query con abbastanza risultati"
            assert tutto.next_cursor is None

            visti, cursore, giri = [], None, 0
            while True:
                pagina = q.search(conn, q="r0", limit=2, cursor=cursore)
                visti.extend(pagina.results)
                giri += 1
                assert giri < 200, "il cursore non avanza"
                if pagina.next_cursor is None:
                    break
                cursore = pagina.next_cursor

    assert visti == tutto.results, \
        "sfogliare a due per pagina non ha dato lo stesso elenco"
    chiavi = [(r["kind"], r.get("device", {}).get("uid") or r["rack"]["uid"])
              for r in visti]
    assert len(chiavi) == len(set(chiavi)), "un risultato è comparso due volte"


def test_expiry_pagination_walks_every_row_exactly_once(engine):
    corpus = load(engine, "expiry-fixture")
    today = date.fromisoformat(corpus["refDate"])
    with snapshot(engine) as conn:
        with conn.begin():
            tutto = q.expiries(conn, today=today, limit=q.EXPIRY_MAX_LIMIT)
            assert len(tutto.items) > 3

            visti, cursore, giri = [], None, 0
            while True:
                pagina = q.expiries(conn, today=today, limit=2, cursor=cursore)
                visti.extend(pagina.items)
                giri += 1
                assert giri < 200
                if pagina.next_cursor is None:
                    break
                cursore = pagina.next_cursor
    assert visti == tutto.items


def test_ordering_is_deterministic_when_display_fields_collide(engine):
    """Due risultati indistinguibili per nome devono comunque avere un ordine.

    Il corpus `search-text` ha due dispositivi con lo stesso `id` di business e nomi
    che differiscono di una cifra; `same-code-same-rack` ne ha due con lo stesso `id`
    nello stesso rack. L'ultimo spareggio è l'`uid`, che è unico per costruzione,
    quindi l'ordine è totale — e ripetendo la query non cambia.
    """
    load(engine, "rel-same-code-same-rack")
    with snapshot(engine) as conn:
        with conn.begin():
            giri = [[(r["kind"], r.get("device", {}).get("uid") or r["rack"]["uid"])
                     for r in q.search(conn, q="a", limit=200).results]
                    for _ in range(5)]
    assert all(g == giri[0] for g in giri), "l'ordine cambia fra due esecuzioni"


@pytest.mark.parametrize("cursore", [
    "non-base64!!", "e30", "eyJ2Ijo5OTk5fQ",
    "eyJ2IjoxLCJxIjoiYWx0cm8iLCJrIjpbXX0",
], ids=["non-base64", "senza-chiave", "versione-sbagliata", "altra-query"])
def test_a_malformed_cursor_is_rejected_with_a_stable_code(engine, cursore):
    """422 e un codice stabile, senza spiegare quale byte è sbagliato (§15).

    Un cursore è un valore opaco che abbiamo emesso noi: se torna rotto, il client ha
    un difetto, e descrivergli la struttura interna non lo aiuta a ripararlo.
    """
    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            with pytest.raises(q.CursorRejected) as caught:
                q.search(conn, q="a", cursor=cursore)
    assert caught.value.code == "invalid_cursor"
    # il messaggio non riporta il cursore né la sua struttura
    assert cursore not in caught.value.message
    assert "base64" not in caught.value.message.lower() or cursore == "non-base64!!"


def test_a_cursor_from_another_query_is_rejected(engine):
    """⚠ Un cursore VALIDO ma di un'altra ricerca: 422, non righe arbitrarie.

    Il cursore porta la query di provenienza proprio per questo. Senza il controllo, un
    client che riusa per sbaglio il cursore di «r0» cercando «srv» riceverebbe la
    seconda pagina di un insieme che non ha mai chiesto — righe corrette per una
    domanda diversa, che è il modo peggiore di sbagliare perché sembra funzionare.

    ⚠ Il cursore si OTTIENE da una ricerca vera, non si costruisce a mano: un cursore
    fabbricato con una chiave vuota veniva rifiutato dal controllo sull'ARITÀ, quindi il
    test passava senza esercitare il controllo sulla query. L'ha trovata una mutazione
    che toglieva quel controllo e non faceva diventare rosso niente.
    """
    load(engine, "seed")
    with snapshot(engine) as conn:
        with conn.begin():
            prima = q.search(conn, q="r0", limit=2)
            assert prima.next_cursor, "serve una ricerca con più di una pagina"

            # Lo stesso cursore sulla stessa query: funziona.
            seconda = q.search(conn, q="r0", limit=2, cursor=prima.next_cursor)
            assert seconda.results and seconda.results != prima.results

            # Su un'altra query: rifiutato.
            with pytest.raises(q.CursorRejected) as caught:
                q.search(conn, q="srv", limit=2, cursor=prima.next_cursor)
    assert caught.value.code == "invalid_cursor"


@pytest.mark.parametrize("limite", [0, -1, 10_000])
def test_an_out_of_range_limit_is_rejected(engine, limite):
    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            with pytest.raises(q.QueryRejected) as caught:
                q.search(conn, q="a", limit=limite)
    assert caught.value.code == "invalid_query"


def test_an_out_of_range_warning_days_is_rejected(engine):
    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            with pytest.raises(q.QueryRejected):
                q.expiries(conn, today=date(2026, 8, 10), warning_days=-1)
            with pytest.raises(q.QueryRejected):
                q.expiries(conn, today=date(2026, 8, 10),
                           warning_days=q.MAX_WARNING_DAYS + 1)


# ==================================================================
# 7. il documento non si tocca
# ==================================================================

def test_the_document_is_unchanged_by_any_query(engine):
    """Le query sono letture: né versioni, né audit, né righe di proiezione toccate.

    `READ ONLY` lo impone già a livello di database, ma la connessione dei test è la
    stessa che usano le rotte solo se questo test lo verifica: qui si guarda lo stato
    prima e dopo.
    """
    load(engine, "expiries")
    with engine.begin() as c:
        prima = (
            c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one(),
            c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            c.execute(text("SELECT synchronised_at FROM inventory_projection_state")
                      ).scalar_one(),
        )
    with snapshot(engine) as conn:
        with conn.begin():
            q.search(conn, q="doppio", limit=200)
            q.capacity(conn)
            q.expiries(conn, today=date(2026, 8, 10), limit=200)
    with engine.begin() as c:
        dopo = (
            c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one(),
            c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            c.execute(text("SELECT synchronised_at FROM inventory_projection_state")
                      ).scalar_one(),
        )
    assert prima == dopo


def test_a_query_cannot_write_even_if_it_tried(engine):
    """La transazione è `READ ONLY`: è PostgreSQL a rifiutare, non le buone intenzioni."""
    import psycopg

    load(engine, "capacity")
    with snapshot(engine) as conn:
        with conn.begin():
            with pytest.raises(Exception) as caught:
                conn.execute(text("UPDATE inventory_racks SET name = name"))
    assert isinstance(caught.value.orig, psycopg.errors.ReadOnlySqlTransaction)


def test_the_documents_that_cannot_be_stored_are_exactly_this_list(engine):
    """L'elenco `NOT_STORABLE` è vero, e non una scusa per saltare un test.

    Se un documento cominciasse a essere accettato, o un altro venisse rifiutato,
    questo test diventerebbe rosso — che è l'unico modo di non lasciare uno `skip`
    permanente in una suite.
    """
    for name in NOT_STORABLE:
        _clean(engine, CORPORA[name]["doc"])
        with pytest.raises(DocumentRejectedError):
            with engine.begin() as c:
                InventoryRepository(c).bootstrap(CORPORA[name]["doc"], ADMIN,
                                                 from_legacy=True)
    for name in STRICT:
        _clean(engine, CORPORA[name]["doc"])
        with engine.begin() as c:      # non solleva
            InventoryRepository(c).bootstrap(CORPORA[name]["doc"], ADMIN,
                                             from_legacy=True)


# ==================================================================
# 8. il contratto HTTP delle tre rotte
# ==================================================================
#
# Le sezioni precedenti provano la SEMANTICA chiamando le funzioni. Qui si prova che le
# rotte esistono, che sono letture autenticate, che i codici d'errore arrivano al
# client e che nessuna espone SQL o nomi di tabella (§2, §15 del requisito).

from app.api.deps import get_connection, require_actor          # noqa: E402
from app.main import app                                        # noqa: E402

from conftest import ORIGIN, api_client                         # noqa: E402

ROUTES = ("/api/inventory/search?q=srv", "/api/inventory/capacity",
          "/api/inventory/expiries")


@pytest.fixture
def as_role(engine):
    """Client HTTPS con un ruolo scelto dal test.

    `get_snapshot_reader` NON è sostituito: le rotte aprono lo snapshot vero, quindi i
    test HTTP esercitano la stessa transazione `REPEATABLE READ, READ ONLY` che gira in
    produzione.
    """
    def _make(role: str):
        def _dep():
            with engine.connect() as conn:
                with conn.begin():
                    yield conn
        app.dependency_overrides[get_connection] = _dep
        app.dependency_overrides[require_actor] = \
            lambda: Actor(username=f"tale-{role}", role=role)
        return api_client(app)
    yield _make
    app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["view", "edit", "admin"])
@pytest.mark.parametrize("route", ROUTES)
def test_every_authenticated_role_may_read(engine, as_role, role, route):
    """`view`, `edit` e `admin` allo stesso modo (§2).

    Sono letture, e nel frontend la barra di ricerca, la vista Capacità e la vista
    Scadenze le vede chiunque abbia una sessione. Renderle amministrative qui
    restringerebbe una funzione esistente — che è un cambio di prodotto travestito da
    prudenza.
    """
    load(engine, "expiry-fixture")
    with as_role(role) as c:
        r = c.get(route)
    assert r.status_code == 200, f"{route} come {role}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body["version"], int)
    assert isinstance(body["sha256"], str) and len(body["sha256"]) == 64
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ROUTES)
def test_the_query_routes_refuse_anonymous_requests(engine, route):
    """401, e con l'inventario NON inizializzato: un ordine invertito rivelerebbe lo
    stato del servizio a chi non è autenticato."""
    def _dep():
        from app.db import get_engine
        with get_engine().connect() as conn:
            with conn.begin():
                yield conn

    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as c:
            r = c.get(route)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "not_authenticated"


@pytest.mark.parametrize("route", ROUTES)
def test_a_temporary_password_session_cannot_read(engine, route):
    """403 `password_change_required`: la restrizione è strutturale (§8.26).

    Passa da `require_actor`, quindi una rotta nuova è ristretta per costruzione. Si
    esercita la dipendenza VERA e non un doppio: un `require_actor` sostituito
    proverebbe soltanto che il sostituto funziona.
    """
    from app.auth.service import create_user

    provvisoria = "collaudo delle interrogazioni sql"
    load(engine, "expiry-fixture")

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
                conn.execute(text("DELETE FROM users WHERE username = 'query'"))
                create_user(conn, username="query", password=provvisoria, role="view")
            acceso = c.post("/api/auth/login", headers=ORIGIN,
                            json={"username": "query", "password": provvisoria})
            assert acceso.status_code == 200, acceso.text
            assert acceso.json()["mustChangePassword"] is True
            r = c.get(route)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


@pytest.mark.parametrize("route", ROUTES)
def test_a_stale_projection_becomes_503_on_every_route(engine, as_role, route):
    """503 `projection_not_current`, e nessun dettaglio interno nella risposta (§15)."""
    load(engine, "expiry-fixture")
    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_projection_state"))
    with as_role("view") as c:
        r = c.get(route)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "projection_not_current"
    testo = json.dumps(r.json(), ensure_ascii=False)
    for vietato in ("inventory_", "SELECT", "psycopg", "Traceback", "rebuild",
                    "sha256", "strpos", "generate_series"):
        assert vietato not in testo, f"la risposta espone «{vietato}»: {testo}"


def test_a_missing_query_parameter_is_a_client_error(engine, as_role):
    """`q` obbligatorio: 422 dalla validazione di FastAPI, non 500."""
    load(engine, "search-text")
    with as_role("view") as c:
        r = c.get("/api/inventory/search")
    assert r.status_code == 422


@pytest.mark.parametrize("brutto,codice", [
    ("/api/inventory/search?q=a&cursor=rotto!!", "invalid_cursor"),
    ("/api/inventory/expiries?cursor=rotto!!", "invalid_cursor"),
])
def test_a_malformed_cursor_is_422_over_http(engine, as_role, brutto, codice):
    """Il codice stabile arriva al client, e non diventa un 503 generico.

    È la ragione per cui `QueryRejected`/`CursorRejected` stanno PRIMA del ramo
    generico di `InventoryError` nella mappa degli errori: quello risponderebbe
    `unavailable`, cioè «riprova più tardi» per un difetto che riprovando si ripete
    identico.
    """
    load(engine, "search-text")
    with as_role("view") as c:
        r = c.get(brutto)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == codice


def test_out_of_range_parameters_are_refused_by_the_contract(engine, as_role):
    """`limit` e `warningDays` hanno limiti dichiarati nella firma: 422 di FastAPI."""
    load(engine, "search-text")
    with as_role("view") as c:
        assert c.get("/api/inventory/search?q=a&limit=0").status_code == 422
        assert c.get("/api/inventory/search?q=a&limit=99999").status_code == 422
        assert c.get("/api/inventory/expiries?warningDays=-1").status_code == 422
        assert c.get("/api/inventory/expiries?warningDays=99999").status_code == 422


def test_the_search_route_says_which_address_form_it_recognised(engine, as_role):
    """`address` nella risposta: forma, famiglia ed estremi.

    ⚠ Sostituisce `ipRange`, che era una coppia di interi a 32 bit — una forma che
    l'IPv6 non ci sta dentro, e che non diceva QUALE forma era stata riconosciuta. Il
    client deve poter dire all'utente che ha cercato una rete e non un testo, e con
    l'indirizzo esatto (§8.50.6) la distinzione conta di più: `10.0.0.1` adesso ha un
    significato preciso, e chi lo scrive deve poter vedere quale.
    """
    load(engine, "search-ip")
    with as_role("view") as c:
        rete = c.get("/api/inventory/search?q=10.0.0.0/24").json()
        esatto = c.get("/api/inventory/search?q=10.0.0.1").json()
        sei = c.get("/api/inventory/search?q=2001:db8::/32").json()
        testo = c.get("/api/inventory/search?q=DHCP").json()

    assert rete["address"] == {"family": 4, "kind": "cidr",
                              "lo": "10.0.0.0", "hi": "10.0.0.255"}
    assert esatto["address"] == {"family": 4, "kind": "exact",
                                 "lo": "10.0.0.1", "hi": "10.0.0.1"}
    assert sei["address"]["family"] == 6 and sei["address"]["kind"] == "cidr"
    assert testo["address"] is None


def test_the_expiries_route_reports_the_date_it_used(engine, as_role):
    """`today` nella risposta: un conteggio di giorni senza la data da cui è calcolato
    non è verificabile da chi lo legge."""
    load(engine, "expiry-fixture")
    with as_role("view") as c:
        body = c.get("/api/inventory/expiries").json()
    assert date.fromisoformat(body["today"])
    assert body["warningDays"] == q.DEFAULT_WARNING_DAYS
    # `notifiable` dalla 2G: quante delle voci in finestra genererebbero davvero
    # un'email. È il numero che spiega la differenza fra questa vista e il worker.
    assert set(body["totals"]) == {"expired", "warning", "future", "notifiable"}
    assert body["filters"] == {"stato": None, "presenza": None}


def test_there_is_no_endpoint_that_executes_an_arbitrary_query(engine):
    """Nessuna rotta accetta SQL, un ordinamento arbitrario o un nome di colonna.

    ⚠ Si guarda lo schema OpenAPI, non `app.routes`. Due ragioni: in FastAPI 0.141
    `app.routes` contiene involucri `_IncludedRouter` che non hanno un `path` — la prima
    stesura di questo test cercava `path` e trovava un insieme vuoto, quindi passava
    guardando niente — e soprattutto lo schema È il contratto pubblicato. Un endpoint
    aggiunto domani in un altro modulo compare qui perché compare nel contratto.
    """
    schema = app.openapi()
    query_routes = {p for p in schema["paths"] if p.startswith("/api/inventory/")}
    assert query_routes == {"/api/inventory/search", "/api/inventory/capacity",
                            "/api/inventory/expiries"}, \
        f"rotte di interrogazione inattese: {sorted(query_routes)}"

    # Nessuna delle tre accetta un parametro che nomini SQL, colonne o ordinamenti: la
    # differenza fra tre domande con un significato e una domanda arbitraria.
    for percorso in sorted(query_routes):
        operazioni = schema["paths"][percorso]
        assert set(operazioni) == {"get"}, \
            f"{percorso} espone metodi oltre a GET: {sorted(operazioni)}"
        nomi = {p["name"] for p in operazioni["get"].get("parameters", [])}
        for vietato in ("sql", "where", "order", "orderBy", "sort", "column",
                        "columns", "filter", "raw", "expr"):
            assert vietato not in nomi, \
                f"{percorso} accetta il parametro «{vietato}»"
        assert "requestBody" not in operazioni["get"], \
            f"{percorso} accetta un corpo: è una lettura"

    # E i parametri che accetta sono esattamente quelli previsti, così un parametro
    # aggiunto senza pensarci si fa notare.
    attesi = {
        # `stato` e `presenza` anche qui dalla 2H, per la vista Dismessi: STESSO
        # vocabolario e stessa validazione delle scadenze. È un'estensione di lettura
        # ristretta, non una domanda arbitraria — e la differenza è che il server
        # conosce in anticipo l'elenco dei valori ammessi.
        "/api/inventory/search": {"q", "stato", "presenza", "limit", "cursor"},
        "/api/inventory/capacity": set(),
        # `stato` e `presenza` dalla 2G: filtri a VOCABOLARIO, non espressioni.
        # Sono la differenza fra «restringi la domanda» e «esegui questa query», ed è
        # la ragione per cui il controllo sui nomi vietati qui sopra li lascia passare
        # e non li confonde con `filter`.
        "/api/inventory/expiries": {"warningDays", "stato", "presenza",
                                    "limit", "cursor"},
    }
    for percorso, nomi_attesi in attesi.items():
        nomi = {p["name"]
                for p in schema["paths"][percorso]["get"].get("parameters", [])}
        assert nomi == nomi_attesi, f"{percorso}: parametri {sorted(nomi)}"


@pytest.mark.parametrize("route", ROUTES)
def test_the_query_routes_do_not_accept_a_body_or_mutate(engine, as_role, route):
    """Sono `GET`: `POST`, `PUT` e `DELETE` non esistono su questi percorsi."""
    load(engine, "expiry-fixture")
    base = route.split("?")[0]
    with as_role("admin") as c:
        for metodo in ("post", "put", "delete", "patch"):
            r = getattr(c, metodo)(base, headers=ORIGIN)
            assert r.status_code == 405, f"{metodo.upper()} {base}: {r.status_code}"


# ==================================================================
# 9. indici: quelli che ci sono, e quelli che NON si sono aggiunti
# ==================================================================
#
# ⚠ La fase 2E aggiunge ZERO indici, e non per pigrizia: perché le misure non ne
# giustificano nessuno (§13 del requisito, §8.46.1 del piano). Questi test pinnano il
# perché, così la decisione non va rifatta a memoria fra sei mesi.

def indici(engine, tabella: str) -> dict[str, str]:
    with engine.begin() as c:
        return {r[0]: r[1] for r in c.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
            {"t": tabella}).all()}


def test_the_expiry_query_already_has_its_indexes(engine):
    """Le date derivate sono già indicizzate, dalla fase 2B.

    §13 elenca «parsed expiry dates» fra i candidati, e ci sono già:
    `ix_device_garanzia_date` e `ix_device_supporto_date`, PARZIALI su
    `IS NOT NULL`. Aggiungerne altri sarebbe duplicare, e un indice duplicato costa
    scritture senza rendere letture.

    Il test verifica anche che siano parziali: nel seed reale la maggioranza dei
    dispositivi non ha date, e indicizzare gli 86 NULL per trovarne dodici sarebbe
    indice sprecato — che è la ragione con cui la 0011 li ha creati così.
    """
    trovati = indici(engine, "inventory_devices")
    for nome, colonna in (("ix_device_garanzia_date", "garanzia_date"),
                          ("ix_device_supporto_date", "supporto_date")):
        assert nome in trovati, f"{nome} non esiste più: la 0011 è stata modificata?"
        definizione = trovati[nome]
        assert colonna in definizione
        assert "WHERE" in definizione.upper(), \
            f"{nome} non è più parziale: {definizione}"


def test_the_parent_lookups_are_already_indexed(engine):
    """⚠ Le chiavi esterne sono già indicizzate, dalla 0010.

    §13 elenca «parent foreign keys» fra i candidati. Esistono già:
    `ix_device_rack`, `ix_rack_room`, `ix_room_location`. Aggiungerne altri sarebbe
    duplicare, e un indice duplicato costa una scrittura per ogni riga inserita — che
    con la fase 2C significa a ogni salvataggio, perché la sincronizzazione reinserisce
    tutte le righe.

    ⚠ Nota di processo, perché mi ha fatto sbagliare una conclusione. La prima versione
    di questo test diceva «non serve, i vincoli di unicità lo coprono», e l'avevo
    dedotto da un EXPLAIN che mostrava `uq_device_ordinal` al posto di `ix_device_rack`.
    Era vero, ma solo perché la mia stessa sonda delle prestazioni aveva fatto
    `DROP INDEX ix_device_rack` sul database di prova poco prima. Una sonda che modifica
    lo schema che sta misurando produce numeri veri su un sistema che non esiste.

    Il test guarda la colonna GUIDA e non il nome: qualunque indice che cominci con la
    colonna di risalita va bene, e se un giorno ne restasse solo uno composto la
    copertura resterebbe reale.
    """
    def guida(engine, tabella, colonna):
        """Esiste un indice la cui PRIMA colonna è questa?"""
        for definizione in indici(engine, tabella).values():
            fra_parentesi = definizione[definizione.rfind("(") + 1:
                                        definizione.rfind(")")]
            prima = fra_parentesi.split(",")[0].strip()
            if prima == colonna:
                return definizione
        return None

    for tabella, colonna in (("inventory_devices", "rack_uid"),
                             ("inventory_racks", "room_uid"),
                             ("inventory_rooms", "location_uid")):
        assert guida(engine, tabella, colonna), (
            f"nessun indice guida su {tabella}({colonna}): le risalite "
            "genitore-figlio delle query diventerebbero scansioni")


def test_no_query_index_was_added_by_this_phase(engine):
    """L'insieme degli indici è quello delle fasi precedenti.

    Un indice aggiunto «per sicurezza» è un costo di scrittura permanente pagato per un
    beneficio che nessuno ha misurato. Le misure di §8.46.1 dicono:

      - `lower(name)` NON serve: la ricerca è una SOTTOSTRINGA (`strpos`), e nessun
        btree la può servire. Con l'indice presente il piano resta `Seq Scan`;
      - `(espressione IP)` porta la ricerca per rete da 14,7 a 8,4 ms a venti volte la
        scala di produzione. Sei millisecondi su una query che ne impiega quindici non
        sono un problema da risolvere, e §13 lo dice esplicitamente: «a sequential scan
        that completes in milliseconds is acceptable»;
      - le chiavi esterne e le date sono già coperte (i due test qui sopra).

    ⚠ E una scoperta sui TRE indici che la 0010 aveva creato «per le interrogazioni per
    cui la normalizzazione esiste»: `ix_device_code`, `ix_device_ip`, `ix_device_serial`
    sono btree sulla colonna grezza, e la ricerca vera è una SOTTOSTRINGA. Un btree non
    serve una sottostringa, quindi per l'endpoint di ricerca quei tre indici sono
    INERTI: il piano resta `Seq Scan`. Non si rimuovono — è fuori dallo scopo di questo
    commit, e servirebbero a un confronto per uguaglianza che un giorno potrebbe
    esistere — ma va scritto che non stanno sostenendo la ricerca che c'è.

    ⚠ La 2G ne aggiunge UNO, e la ragione è cambiata insieme all'implementazione:
    l'espressione IP non esiste più — c'è una COLONNA `ip_addr inet` scritta dal parser
    del dominio (§8.50.10) — e una colonna si indicizza. L'indice è PARZIALE
    (`WHERE ip_addr IS NOT NULL`) perché una parte dei dispositivi non ha un indirizzo
    interpretabile, e indicizzarne i NULL sarebbe indice sprecato.
    `presenza` NON è indicizzata: la domanda è «quali NON sono rimossi», vera per la
    quasi totalità delle righe.
    """
    attesi_devices = {
        "inventory_devices_pkey", "uq_device_ordinal",
        # dalla 0010: risalita al rack, e tre indici per la ricerca
        "ix_device_rack", "ix_device_code", "ix_device_ip", "ix_device_serial",
        # dalla 0011: le date derivate, parziali
        "ix_device_garanzia_date", "ix_device_supporto_date",
        # dalla 0013: l'indirizzo interpretato, parziale
        "ix_device_ip_addr",
    }
    trovati = set(indici(engine, "inventory_devices"))
    assert trovati == attesi_devices, (
        "l'insieme degli indici su inventory_devices è cambiato.\n"
        f"  in più:  {sorted(trovati - attesi_devices)}\n"
        f"  in meno: {sorted(attesi_devices - trovati)}\n"
        "Se è un indice nuovo: va misurato e documentato in §8.46.1, non aggiunto "
        "perché sembrava utile.")


def test_the_address_column_replaces_the_expression_and_is_ordered_by_family(engine):
    """⚠ ROVESCIATO dalla 2G: non «indicizzabile se un giorno servisse», ma indicizzato.

    Il test vecchio creava e distruggeva un indice sull'espressione `ipnum_sql` per
    dimostrare che si POTEVA. L'espressione non esiste più: c'è una colonna `inet`
    scritta dal parser del dominio, e l'indice è nella migrazione 0013.

    ⚠ Quello che questo test verifica adesso è la proprietà su cui poggia la
    separazione delle famiglie: `inet` ordina prima per FAMIGLIA e poi per indirizzo.
    È la ragione per cui un intervallo IPv4 non può contenere un IPv6, e la si pretende
    DAL DATABASE — la documentazione di PostgreSQL lo dice, ma una proprietà su cui si
    appoggia una regola di prodotto va misurata, non citata.
    """
    load(engine, "search-ip")
    with engine.begin() as c:
        ordinati = [r[0] for r in c.execute(text(
            "SELECT ip_addr::text FROM inventory_devices "
            " WHERE ip_addr IS NOT NULL ORDER BY ip_addr")).all()]
        # Ogni IPv4 precede ogni IPv6, qualunque sia il valore numerico.
        famiglie = [r[0] for r in c.execute(text(
            "SELECT family(ip_addr) FROM inventory_devices "
            " WHERE ip_addr IS NOT NULL ORDER BY ip_addr")).all()]
        # E il confronto fra famiglie diverse è deciso dalla famiglia, non dai bit.
        misto = c.execute(text(
            "SELECT '255.255.255.255'::inet < '::'::inet")).scalar_one()

    assert ordinati, "il corpus non ha indirizzi interpretabili: test vacuo"
    assert famiglie == sorted(famiglie), (
        f"`inet` non ordina per famiglia: {list(zip(famiglie, ordinati))}")
    assert set(famiglie) == {4, 6}, (
        f"il corpus non contiene entrambe le famiglie: {set(famiglie)}")
    assert misto is True, (
        "l'IPv4 più alto possibile deve precedere l'IPv6 più basso: è la proprietà su "
        "cui poggia la separazione delle famiglie nella ricerca")
