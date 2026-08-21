"""Interrogazioni SQL sulla proiezione relazionale: ricerca, capacità, scadenze.

Dalla fase 2G questo modulo implementa il **contratto di dominio** (`app/domain.py`,
§8.50), non più il comportamento misurato del prototipo.

    search(conn, …)     la barra di ricerca globale, testo e indirizzi
    capacity(conn)      capacità: unità occupate, libere, blocco contiguo
    expiries(conn, …)   scadenze ispezionabili: garanzia e supporto, con il livello

⚠ Che cosa è cambiato rispetto alla 2E, e perché
-----------------------------------------------
La 2E riproduceva fedelmente il frontend, di proposito: cambiare la semantica durante
una migrazione tecnica avrebbe reso impossibile dire quale delle due cose aveva
cambiato un comportamento. Quella scelta è scaduta con la 2G, che è dove le
incoerenze misurate diventano decisioni. Le cinque che toccano questo modulo:

  1. **indirizzo ESATTO** (§8.48 voce 1). `10.0.0.1` non era una forma riconosciuta,
     quindi finiva nella ricerca testuale — e `10.0.0.1` è una sottostringa di
     `10.0.0.100`. Chi cercava una macchina precisa riceveva la sua vicina;
  2. **IPv6** (voce 2). `ipToNum` era IPv4, quindi un dispositivo IPv6 non si trovava
     per rete. Adesso si trova, e con il CIDR;
  3. **nove campi cercabili** (voce 4), con `id`, `tipo`, `stato` e `presenza`. Le
     `note` restano fuori per decisione;
  4. **la capacità guarda la PRESENZA** (voci 5 e 6), non lo stato operativo, e conta
     gli slot distinti. Il blocco vuoto `if (… === 'dismesso') {}` del prototipo
     lasciava occupare i dismessi per caso; ora occupa ciò che è fisicamente là;
  5. **le file sono gruppi strutturali** (voce 7), non la sentinella `row || '—'` che
     collideva col rack CS-Q01 del seed.

E una che NON è cambiata, perché non era un difetto: in modalità indirizzo i **rack non
partecipano** (voce 3). Un rack che si chiama «10.0.0.1» non è una macchina con quel
indirizzo, e restituirlo a chi cerca un host sarebbe un falso positivo che sembra una
risposta. §5 del requisito lo conferma.

⚠ Nessuna semantica scritta qui
------------------------------
Ogni decisione — quali campi, quale arrotondamento, cosa occupa, cosa è una data —
viene da `app/domain.py`, e questo modulo la traduce in SQL. Non è pedanteria
architetturale: è il modo in cui la risposta dell'endpoint e quella che il frontend
calcola in locale restano la stessa. Se un giorno divergessero, il posto dove
guarderebbe chi indaga sarebbe questo file, e ciò che troverebbe è che qui non c'è
niente da decidere.

Le due colonne DERIVATE che rendono possibile tutto questo — `garanzia_date` /
`supporto_date` e `ip_addr` — le scrive la mappa con le funzioni del dominio (§8.44,
`relational.DERIVED`). PostgreSQL non interpreta mai il testo dell'utente: riceve
valori già normalizzati e li confronta.

⚠ `extra` PARTECIPA alla ricerca, dalla 2G
-----------------------------------------
Un valore che la mappa non ha potuto mettere in una colonna tipizzata sta in `extra`
(§8.42), e nella 2E questo modulo non lo cercava: un rack i cui `seriali` contengono un
numero portava l'intero array in `extra`, e i suoi seriali non si trovavano — mentre il
frontend, che fa `String(sn)`, li trovava. Era una divergenza registrata come stranezza
e resta una risposta sbagliata: l'utente vede il seriale sullo schermo e la ricerca dice
che non esiste. Adesso ogni campo cercabile si guarda nella colonna **oppure** in
`extra`, che è la stessa regola che `candidates.py` applica alle etichette.

La transazione è del CHIAMANTE, e deve essere lo SNAPSHOT
--------------------------------------------------------
Come per `current_document` (§8.45): nessun `commit`, nessun lock, e tutte le letture
di una risposta stanno in una transazione `REPEATABLE READ, READ ONLY` aperta da
`app/api/deps.py`. Una risposta descrive un solo istante del database, testa e digest
compresi.

⚠ Nessuna verifica di fedeltà per richiesta (§12). Le query pretendono che la
proiezione sia ATTUALE — tre confronti fra valori registrati — e non riassemblano il
documento per ricalcolarne il digest: quello costa il 70% Python misurato in §8.45.1, e
pagarlo su ogni ricerca sarebbe carico senza una ragione. Il percorso di fedeltà
completa resta `GET /api/inventory`; quello operativo `project.py --verify`.

⚠ Ne segue un limite, che va detto: una colonna DERIVATA corrotta a mano darebbe
risposte sbagliate a queste tre interrogazioni senza che niente lo dica, perché il
digest è cieco alle derivate per costruzione (§8.47.4). È lo stesso punto cieco che il
worker chiude eseguendo `validate_model` una volta al giorno; qui non si può pagare
quel costo a ogni ricerca, e la rete di sicurezza è `project.py --verify`.

Riferimento: BACKEND-PLAN.md §8.46 (la 2E), §8.50 (il contratto).
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app import domain
from app.inventory.errors import InventoryError
from app.inventory.projection import require_current_head

# ==================================================================
# limiti e codici
# ==================================================================

#: Pagina predefinita e massimo assoluto.
#:
#: Il frontend mostra `results.slice(0, 12)`, ma quello è un troncamento di
#: VISUALIZZAZIONE deciso dallo spazio nel menu a tendina, non un limite semantico:
#: la ricerca calcola tutti i risultati e poi ne disegna dodici. Un'API che
#: restituisse dodici righe senza dirlo mentirebbe; una che le restituisce tutte
#: senza limite si fa spiegare dal primo inventario grande perché non va. Da qui un
#: default generoso, un massimo, e un cursore per il resto.
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
EXPIRY_DEFAULT_LIMIT = 200
EXPIRY_MAX_LIMIT = 1000

#: Soglia del livello «entro N giorni». 90 è la costante della vista Scadenze, e resta
#: una costante di VISUALIZZAZIONE: non ha niente a che vedere con le finestre di
#: preavviso del worker, che stanno nelle impostazioni e sono un elenco.
DEFAULT_WARNING_DAYS = 90
MAX_WARNING_DAYS = 3650


class QueryRejected(InventoryError):
    """Parametro di interrogazione non accettabile. 422, non 503: è del client."""
    code = "invalid_query"


class CursorRejected(InventoryError):
    """Cursore non decodificabile, o appartenente a un'altra interrogazione."""
    code = "invalid_cursor"


# ==================================================================
# esiti
# ==================================================================

@dataclass(frozen=True)
class Revision:
    """La revisione che una risposta descrive. Letta nello STESSO snapshot (§4)."""
    version: int
    sha256: str


@dataclass(frozen=True)
class AddressRange:
    """L'intervallo riconosciuto in una query di indirizzo, per la risposta."""
    family: int
    kind: str
    lo: str
    hi: str

    def as_dict(self) -> dict:
        return {"family": self.family, "kind": self.kind, "lo": self.lo,
                "hi": self.hi}


@dataclass(frozen=True)
class SearchPage:
    revision: Revision
    query: str
    address: AddressRange | None
    #: I filtri applicati, come li ha visti il server. Escono nella risposta per la
    #: stessa ragione di `ExpiryPage.filters`: un elenco filtrato che non dice di
    #: essere filtrato si legge come completo.
    filters: dict
    results: list[dict]
    next_cursor: str | None


@dataclass(frozen=True)
class CapacityReport:
    revision: Revision
    locations: list[dict]


@dataclass(frozen=True)
class ExpiryPage:
    revision: Revision
    today: date
    warning_days: int
    filters: dict
    totals: dict
    items: list[dict]
    next_cursor: str | None


# ==================================================================
# cursore
# ==================================================================
#
# Opaco, e con dentro la CHIAVE di ordinamento — non un offset (§8). Un `OFFSET` su un
# insieme che cambia salta o ripete righe, e lo fa proprio quando qualcuno sta
# salvando: cioè nel caso in cui uno sfoglia i risultati perché ce ne sono molti.
#
# Porta anche la query: un cursore ottenuto cercando «srv» e riusato cercando «nas»
# non è una pagina successiva, è un errore del client, e va detto (422) invece di
# restituire righe arbitrarie.
#
# NON porta la versione dell'inventario, e la scelta è deliberata: rifiutare il
# cursore a ogni salvataggio renderebbe impossibile sfogliare un inventario vivo. La
# risposta porta `version`, che è come il client si accorge del cambiamento (§4).

_CURSOR_VERSION = 1


def encode_cursor(query: str, key: list) -> str:
    payload = json.dumps({"v": _CURSOR_VERSION, "q": query, "k": key},
                         ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, query: str, arity: int) -> list:
    """Chiave di ripartenza, o `CursorRejected`. Non solleva niente d'altro.

    Ogni forma di guasto — base64 rotto, JSON rotto, versione sconosciuta, query
    diversa, chiave della lunghezza sbagliata — dà lo stesso codice stabile. Il
    dettaglio NON esce: un cursore è un valore che il client ci ha rimandato, e
    spiegargli quale byte è sbagliato non gli serve a niente.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError):
        raise CursorRejected("cursore non decodificabile") from None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise CursorRejected("cursore di una versione non riconosciuta")
    if payload.get("q") != query:
        raise CursorRejected("cursore appartenente a un'altra interrogazione")
    key = payload.get("k")
    if not isinstance(key, list) or len(key) != arity:
        raise CursorRejected("cursore con una chiave non utilizzabile")
    return key


# ==================================================================
# contesto restituito (§7)
# ==================================================================
#
# Identità immutabile più il minimo per localizzare il risultato: sito, sala, rack.
# Mai il documento intero, e mai il solo `id` di business — che è rinominabile e
# duplicabile, quindi non identifica niente (§8.4).
#
# ⚠ Tre campi STRUTTURATI, e l'etichetta accanto al codice. Non una stringa
# «sito / sala / rack» da spezzare dopo: era il difetto del percorso impacchettato
# (§8.48 voci 11 e 12), che troncava `10.0.0.0/24` a `10.0.0.0`.

_CONTEXT_COLUMNS = """
       k.uid  AS rack_uid,  k.code  AS rack_code,  k.name AS rack_name,
       k.extra -> 'id' AS rack_code_extra, k.extra -> 'name' AS rack_name_extra,
       r.uid  AS room_uid,  r.code  AS room_code,  r.nome AS room_nome,
       r.extra -> 'id' AS room_code_extra, r.extra -> 'nome' AS room_nome_extra,
       l.uid  AS loc_uid,   l.code  AS loc_code,   l.nome AS loc_nome,
       l.extra -> 'id' AS loc_code_extra, l.extra -> 'nome' AS loc_nome_extra,
       l.ordinal AS l_ord, r.ordinal AS r_ord, k.ordinal AS k_ord
"""

_CONTEXT_JOINS = """
  JOIN inventory_racks     k ON k.uid = d.rack_uid
  JOIN inventory_rooms     r ON r.uid = k.room_uid
  JOIN inventory_locations l ON l.uid = r.location_uid
"""


def _value(column: Any, extra: Any) -> Any:
    """Il valore di un campo, da colonna **oppure** da `extra`.

    La mappa mette ogni chiave in ESATTAMENTE uno dei due (§8.42): nella colonna se il
    tipo ci sta, in `extra` altrimenti. Guardare solo la colonna farebbe sparire un
    `name: 42` — e per l'utente quel dispositivo si chiama «42», perché è così che
    l'interfaccia lo mostra.

    ⚠ `extra -> 'chiave'` e non `->>`: `->>` darebbe il TESTO JSON, quindi `"42"` con
    le virgolette per una stringa. L'etichetta è la forma testuale del VALORE.
    """
    return column if column is not None else extra


def _label(*pairs) -> str:
    """`domain.label` sulle coppie (colonna, extra). Mai «None» (§9)."""
    return domain.label(*(_value(col, extra) for col, extra in pairs))


def _context(row: Any) -> dict:
    return {
        "rack": {"uid": str(row.rack_uid), "code": row.rack_code,
                 "name": row.rack_name,
                 "label": _label((row.rack_name, row.rack_name_extra),
                                 (row.rack_code, row.rack_code_extra))},
        "room": {"uid": str(row.room_uid), "code": row.room_code,
                 "nome": row.room_nome,
                 "label": _label((row.room_nome, row.room_nome_extra),
                                 (row.room_code, row.room_code_extra))},
        "location": {"uid": str(row.loc_uid), "code": row.loc_code,
                     "nome": row.loc_nome,
                     "label": _label((row.loc_nome, row.loc_nome_extra),
                                     (row.loc_code, row.loc_code_extra))},
    }


#: Le colonne del dispositivo che compaiono in una risposta, `extra` compreso.
_DEVICE_COLUMNS = """
       d.uid AS dev_uid, d.code AS dev_code, d.name AS dev_name,
       d.extra -> 'id' AS dev_code_extra, d.extra -> 'name' AS dev_name_extra,
       d.type AS dev_type, d.stato AS dev_stato, d.presenza AS dev_presenza,
       d.u AS dev_u, d.h AS dev_h
"""


def _device(row: Any) -> dict:
    """Il dispositivo in una risposta.

    `stato` e `presenza` escono col DEFAULT applicato: un dispositivo senza `presenza`
    è `presente`, e restituire `null` costringerebbe ogni client a riapplicare la
    regola — cioè a possedere una copia della semantica, che è ciò che questa fase
    elimina. Il valore grezzo resta nel documento, che è il posto dove si guarda cosa
    l'utente ha scritto.
    """
    return {
        "uid": str(row.dev_uid), "code": row.dev_code, "name": row.dev_name,
        "label": _label((row.dev_name, row.dev_name_extra),
                        (row.dev_code, row.dev_code_extra)),
        "type": domain.tipo_of({"type": row.dev_type}),
        "stato": domain.stato_of({"stato": row.dev_stato}),
        "presenza": domain.presenza_of({"presenza": row.dev_presenza}),
        "u": row.dev_u, "h": row.dev_h,
    }


# ==================================================================
# frammenti SQL condivisi
# ==================================================================

def _falsy_sql(column: str, default: str) -> str:
    """`(col || default)` in SQL, con la falsità di JavaScript.

    `nullif(col, '')` e non solo `coalesce`: `stato: ""` significa «attivo», perché la
    stringa vuota è falsa in JavaScript e la canonicalizzazione materializza il
    default. Col solo `coalesce` una stringa vuota resterebbe vuota, e per caso darebbe
    la stessa risposta su `<> 'dismesso'` — ma per il motivo sbagliato, e smetterebbe
    di darla il giorno in cui si confronta con `=`.
    """
    return f"coalesce(nullif({column}, ''), '{default}')"


#: Un dispositivo occupa spazio se la sua presenza non è `rimosso` (§2).
_OCCUPIES = (f"{_falsy_sql('d.presenza', domain.DEFAULT_PRESENZA)} "
             f"<> '{domain.PRESENZA_ABSENT}'")

#: Un dispositivo genera nuovi avvisi se il suo stato non è `dismesso` (§7).
_NOTIFIES = (f"{_falsy_sql('d.stato', domain.DEFAULT_STATO)} "
             f"NOT IN ('{domain.NOTIFY_INELIGIBLE_STATES[0]}')")


def _text_match(column: str, extra_key: str | None = None,
                default: str | None = None) -> str:
    """`domain.contains` su un campo: colonna, **oppure** `extra`, poi il default.

    `strpos` su `lower(...)` e non `LIKE`: `LIKE` attribuisce un significato a `%` e
    `_`, che in una casella di ricerca sono caratteri normali — con `LIKE` una query
    contenente `%` troverebbe tutto (§5).

    `extra ->> 'chiave'` qui, e non `->`: serve la forma TESTUALE del valore, che per
    un numero è `42` e per una stringa è il contenuto senza virgolette — cioè
    esattamente `str(value)` in Python.

    ⚠ L'ORDINE dei tre termini non è indifferente, e la prima stesura lo aveva
    sbagliato: applicava il default PRIMA di guardare `extra`, quindi `type: 42` — che
    non è una stringa, quindi finisce in `extra` e lascia la colonna NULL — si cercava
    come «altro» invece che come «42». `domain.tipo_of` dà «42», e la divergenza non
    l'ha trovata nessuna fixture: l'ho vista rileggendo il codice, e ho aggiunto il
    caso al corpus perché la prossima volta la trovi un test.

    L'ordine giusto è quello del contratto: **colonna, poi `extra`, poi il default**.
    """
    termini = [column]
    if extra_key is not None:
        # ⚠ Solo stringhe e numeri. `domain.contains` restituisce False per i booleani
        # e per tutto il resto, e `e::text` di un `true` JSON darebbe «true», cioè un
        # risultato che Python non produce.
        alias = column.split(".")[0]
        termini.append(
            f"CASE WHEN jsonb_typeof({alias}.extra -> '{extra_key}') "
            f"          IN ('string', 'number') "
            f"     THEN {alias}.extra ->> '{extra_key}' END")
    if default is not None:
        # `nullif(colonna, '')` sul PRIMO termine: la stringa vuota è falsa in
        # JavaScript, quindi `stato: ""` significa «attivo» e deve passare al default
        # invece di fermarsi sul vuoto.
        termini[0] = f"nullif({column}, '')"
        termini.append(f"'{default}'")
    return f"strpos(lower(coalesce({', '.join(termini)}, '')), :q) > 0"


# ==================================================================
# A. ricerca
# ==================================================================

def search(conn: Connection, *, q: str, stato: str | None = None,
           presenza: str | None = None, limit: int | None = None,
           cursor: str | None = None) -> SearchPage:
    """La barra di ricerca globale, con la semantica finale (§5).

    Due modalità, ed è la query a scegliere:

      - **indirizzo**, se `domain.parse_address_query` riconosce la forma: esatto IPv4
        o IPv6, CIDR IPv4 o IPv6, intervallo IPv4, jolly IPv4. Si cercano SOLO i
        dispositivi, per intervallo di indirizzo. I rack non partecipano;
      - **testo** altrimenti: sottostringa letterale senza distinzione di maiuscole,
        sui nove campi del dispositivo e sui tre del rack.

    ⚠ Il caso che vale più di tutti: `10.0.0.1` adesso significa *quell'*indirizzo, e
    non trova `10.0.0.100`. Prima era una sottostringa, e chi cercava una macchina
    precisa riceveva la sua vicina di sottorete (§8.48 voce 1).

    ⚠ `10.0.0` continua a essere TESTO, e a trovare `10.0.0.1`, `10.0.0.100`… Non è
    un'incoerenza: mezzo indirizzo non è un indirizzo, e chi lo scrive sta cercando un
    prefisso. La differenza fra le due modalità è che la prima esiste solo per le forme
    che hanno un significato di rete preciso.

    L'ORDINE è quello del documento — sito, sala, rack, e per ogni rack prima il rack
    e poi i suoi dispositivi — con l'`uid` come ultimo spareggio, così è totale anche
    quando ordinali e nomi collidono.

    Filtri (⚠ estensione della fase 2H, §9 del requisito)
    ----------------------------------------------------
    `stato` e `presenza` restringono ai dispositivi che li soddisfano. Stesso
    vocabolario di `domain`, stessi default della falsità di JavaScript, stesso
    `_reject_unknown` delle scadenze: nessuna interpretazione nuova. Servono alla vista
    Dismessi, che è un elenco filtrato di dispositivi ritenuti, non un archivio a parte.

    Due conseguenze, decise e non incidentali:

      - con un filtro attivo i RACK non partecipano. Un rack non ha uno stato operativo
        né una presenza fisica: restituirlo in un elenco filtrato per «dismesso»
        significherebbe mostrare una riga che il filtro non ha nemmeno guardato. È lo
        stesso ragionamento per cui i rack non partecipano alla modalità indirizzo;
      - con un filtro attivo una `q` VUOTA è legittima e restituisce tutto ciò che il
        filtro seleziona. Senza filtri resta zero risultati, che è lo stato della
        casella vuota nel frontend. La differenza: «non hai chiesto niente» contro «hai
        chiesto i dismessi e non hai aggiunto un testo».
    """
    _reject_unknown("stato", stato, domain.DEVICE_STATES)
    _reject_unknown("presenza", presenza, domain.DEVICE_PRESENCES)
    filtrata = stato is not None or presenza is not None

    limit = _clamp_limit(limit, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT)
    revision = _revision(conn)
    filters = {"stato": stato, "presenza": presenza}

    needle = (q or "").strip().lower()
    if not needle and not filtrata:
        # Il frontend con la casella vuota non cerca: `if (q) { … }`. Restituire
        # l'inventario intero sarebbe la risposta comoda e sbagliata.
        #
        # ⚠ `and not filtrata`: con un filtro la domanda è stata posta, e la risposta
        # è l'elenco che il filtro seleziona. Senza questa aggiunta la vista Dismessi
        # avrebbe dovuto inventarsi un testo da cercare per ottenere un elenco.
        return SearchPage(revision=revision, query=q or "", address=None,
                          filters=filters, results=[], next_cursor=None)

    # ⚠ La query di indirizzo si interpreta sul testo GREZZO ripulito, non su
    # `needle`: `lower()` non cambia un IPv4 ma cambia un IPv6 in maiuscolo, e la
    # forma canonica la produce comunque il dominio. Passare il minuscolo funzionerebbe
    # e sarebbe una coincidenza.
    found = domain.parse_address_query((q or "").strip()) if needle else None
    address = (None if found is None else
               AddressRange(family=found.family, kind=found.kind,
                            lo=found.lo.text, hi=found.hi.text))
    # La chiave del cursore porta i filtri, come nelle scadenze: una pagina successiva
    # chiesta con filtri diversi non è una pagina successiva.
    scope = f"{q or ''}|{stato or ''}|{presenza or ''}"
    after = decode_cursor(cursor, scope, 6) if cursor else None

    rows = _search_rows(conn, needle=needle, address=found, after=after,
                        stato=stato, presenza=presenza, limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]

    results = []
    for row in rows:
        item = {"kind": row.kind, **_context(row)}
        if row.kind == "device":
            item["device"] = _device(row)
        else:
            item["deviceCount"] = row.device_count
        results.append(item)

    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(scope, [last.l_ord, last.r_ord, last.k_ord,
                                            last.kind_rank, last.d_ord,
                                            str(last.sort_uid)])
    return SearchPage(revision=revision, query=q or "", address=address,
                      filters=filters, results=results, next_cursor=next_cursor)


#: I nove campi del dispositivo, tradotti. La chiave è il nome del contratto
#: (`domain.DEVICE_SEARCH_FIELDS`); un test pretende che le due liste combacino, così
#: aggiungere un campo al contratto e dimenticarlo qui diventa rosso.
_DEVICE_SEARCH_SQL = {
    "id":       lambda: _text_match("d.code", "id"),
    "name":     lambda: _text_match("d.name", "name"),
    "model":    lambda: _text_match("d.model", "model"),
    "ip":       lambda: _text_match("d.ip", "ip"),
    "serial":   lambda: _text_match("d.serial", "serial"),
    "owner":    lambda: _text_match("d.owner", "owner"),
    "tipo":     lambda: _text_match("d.type", "type", domain.DEFAULT_TYPE),
    "stato":    lambda: _text_match("d.stato", "stato", domain.DEFAULT_STATO),
    "presenza": lambda: _text_match("d.presenza", "presenza",
                                    domain.DEFAULT_PRESENZA),
}

#: I `seriali` del rack: array di testo **oppure** array JSON in `extra`.
#:
#: ⚠ Il secondo ramo è la 2G che chiude una risposta sbagliata: un rack i cui
#: `seriali` contengono un numero porta l'intero array in `extra`, e nella 2E quei
#: seriali non si trovavano — mentre l'utente li vedeva sullo schermo.
_RACK_SERIALI_SQL = """(
       EXISTS (SELECT 1 FROM unnest(coalesce(k.seriali, '{}'::text[])) AS s
                WHERE strpos(lower(s), :q) > 0)
    OR EXISTS (SELECT 1
                 FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof(k.extra -> 'seriali') = 'array'
                             THEN k.extra -> 'seriali' ELSE '[]'::jsonb END) AS e
                WHERE jsonb_typeof(e) IN ('string', 'number')
                  AND strpos(lower(
                        CASE WHEN jsonb_typeof(e) = 'string' THEN e #>> '{}'
                             ELSE e::text END), :q) > 0)
)"""


def _search_rows(conn: Connection, *, needle: str, address, after: list | None,
                 stato: str | None = None, presenza: str | None = None,
                 limit: int) -> list:
    """Le righe della ricerca, già ordinate. Una query sola, con `UNION ALL`.

    ⚠ Il confronto per riga (`(a,b,c,…) > (…)`) è ciò che rende il cursore una
    chiave e non un offset: PostgreSQL lo risolve con l'ordine lessicografico della
    tupla, che è esattamente l'ordine dell'`ORDER BY`.
    """
    params: dict[str, Any] = {"q": needle, "limit": limit}

    if address is not None:
        params["lo"], params["hi"] = address.lo.text, address.hi.text
        # ⚠ Un confronto fra `inet`, e nient'altro.
        #
        # `inet` ordina prima per FAMIGLIA e poi per indirizzo, quindi un intervallo
        # IPv4 non può contenere un IPv6 e viceversa: la separazione delle famiglie
        # che il dominio impone in Python qui è una proprietà del tipo. Un test la
        # pretende dal database invece di fidarsi di questa frase.
        #
        # Sostituisce l'espressione della 2E — nove `btrim` e otto `split_part` per
        # riga, valutata a ogni ricerca — con due confronti su una colonna indicizzata.
        device_where = ("d.ip_addr >= CAST(:lo AS inet) "
                        "AND d.ip_addr <= CAST(:hi AS inet)")
        # In modalità indirizzo i rack non partecipano (§5): si tiene comunque il ramo
        # con una condizione falsa, così la forma della query — e quindi l'ordinamento
        # e il cursore — è una sola. Un `UNION ALL` con un ramo vuoto costa un nulla e
        # risparmia una seconda versione di questo SQL da tenere allineata.
        rack_where = "FALSE"
    else:
        device_where = "(\n    " + "\n OR ".join(
            _DEVICE_SEARCH_SQL[f]() for f in domain.DEVICE_SEARCH_FIELDS) + "\n)"
        rack_where = "(\n    " + "\n OR ".join([
            _text_match("k.code", "id"),
            _text_match("k.name", "name"),
            _RACK_SERIALI_SQL,
        ]) + "\n)"

    # ⚠ Estensione 2H. I filtri si AGGIUNGONO alla condizione del testo o
    # dell'indirizzo, non la sostituiscono: un dismesso che non corrisponde al testo
    # non è un risultato. Con `needle` vuota la condizione del testo diventa `TRUE` e
    # resta il solo filtro — è il caso della vista Dismessi senza ricerca.
    if not needle:
        device_where = "TRUE"
        rack_where = "FALSE"
    if stato is not None or presenza is not None:
        # Un rack non ha stato né presenza: in un elenco filtrato per un attributo del
        # dispositivo non ha una riga da mostrare. Stessa scelta della modalità
        # indirizzo, e per la stessa ragione.
        rack_where = "FALSE"
        if stato is not None:
            params["stato"] = stato
            device_where += (f"\n   AND {_falsy_sql('d.stato', domain.DEFAULT_STATO)}"
                             f" = :stato")
        if presenza is not None:
            params["presenza"] = presenza
            device_where += (
                f"\n   AND {_falsy_sql('d.presenza', domain.DEFAULT_PRESENZA)}"
                f" = :presenza")

    keyset = ""
    if after is not None:
        params.update({f"a{i}": v for i, v in enumerate(after)})
        keyset = ("WHERE (l_ord, r_ord, k_ord, kind_rank, d_ord, sort_uid) "
                  "    > (:a0, :a1, :a2, :a3, :a4, CAST(:a5 AS uuid))")

    sql = f"""
        WITH hits AS (
            -- ramo RACK: `kind_rank` 0, `d_ord` -1, così un rack precede sempre i
            -- propri dispositivi, che partono da 0.
            SELECT 'rack'::text AS kind, 0 AS kind_rank, -1 AS d_ord,
                   k.uid AS sort_uid,
                   NULL::uuid AS dev_uid, NULL::text AS dev_code,
                   NULL::text AS dev_name,
                   NULL::jsonb AS dev_code_extra, NULL::jsonb AS dev_name_extra,
                   NULL::text AS dev_type, NULL::text AS dev_stato,
                   NULL::text AS dev_presenza, NULL::int AS dev_u, NULL::int AS dev_h,
                   (SELECT count(*) FROM inventory_devices dd
                     WHERE dd.rack_uid = k.uid) AS device_count,
                   {_CONTEXT_COLUMNS}
              FROM inventory_racks     k
              JOIN inventory_rooms     r ON r.uid = k.room_uid
              JOIN inventory_locations l ON l.uid = r.location_uid
             WHERE {rack_where}
            UNION ALL
            SELECT 'device'::text AS kind, 1 AS kind_rank, d.ordinal AS d_ord,
                   d.uid AS sort_uid,
                   {_DEVICE_COLUMNS},
                   NULL::bigint AS device_count,
                   {_CONTEXT_COLUMNS}
              FROM inventory_devices d
              {_CONTEXT_JOINS}
             WHERE {device_where}
        )
        SELECT * FROM hits
        {keyset}
         ORDER BY l_ord, r_ord, k_ord, kind_rank, d_ord, sort_uid
         LIMIT :limit
    """
    return conn.execute(text(sql), params).all()


# ==================================================================
# B. capacità
# ==================================================================

def capacity(conn: Connection) -> CapacityReport:
    """Capacità con la definizione FINALE (§2).

    `used_u` = numero di slot U fisici DISTINTI occupati da dispositivi la cui
    `presenza` non è `rimosso`.

    ⚠ Due cambiamenti rispetto alla 2E, e sono decisioni di prodotto:

      1. **la presenza decide, non lo stato.** Il prototipo aveva
         `if ((d.stato || 'attivo') === 'dismesso') {}` — un blocco VUOTO — quindi i
         dismessi occupavano per caso. Adesso occupa ciò che è fisicamente nel rack:
         `dismesso + presente` occupa, `dismesso + rimosso` no, `attivo + rimosso`
         no. È la separazione di §1, e qui è dove serve;
      2. **le file sono gruppi strutturali.** La sentinella `rk.row || '—'` collideva
         col rack CS-Q01 del seed, la cui fila È «—»: i due finivano nello stesso
         gruppo e il totale della fila «—» era la somma di due cose diverse. Adesso il
         gruppo è `domain.row_group`, con la chiave separata dall'etichetta.

    Le proprietà fisiche restano quelle: sovrapposizioni contano una volta, le
    sporgenze si tagliano, `h` nullo o zero vale 1, `h` negativo non occupa, uno slot
    iniziale `<= 0` sta fuori.

    ⚠ Perché unione di INTERVALLI e non `generate_series`
    ----------------------------------------------------
    Enumerare gli slot sarebbe la traduzione ovvia e sarebbe un difetto: `rack.u` è un
    `integer` senza massimo, e il documento `oversized-integers` ne contiene uno da
    3 000 000 000. Un `generate_series` su quel rack produrrebbe tre miliardi di righe
    dentro una richiesta HTTP. Il frontend, sullo stesso documento, esaurisce la
    memoria del browser — l'ho scoperto facendo morire il generatore delle fixture.
    L'unione di intervalli costa quanto i DISPOSITIVI, non quanto l'altezza del rack.
    """
    revision = _revision(conn)
    racks = conn.execute(text(_CAPACITY_SQL)).mappings().all()

    per_room: dict[str, dict] = {}
    order: list[str] = []
    for row in racks:
        key = str(row["room_uid"])
        if key not in per_room:
            order.append(key)
            per_room[key] = {
                "roomUid": key, "roomCode": row["room_code"],
                "roomNome": row["room_nome"],
                "roomLabel": _label((row["room_nome"], row["room_nome_extra"]),
                                    (row["room_code"], row["room_code_extra"])),
                "locationUid": str(row["loc_uid"]), "locationCode": row["loc_code"],
                "locationNome": row["loc_nome"],
                "locationLabel": _label((row["loc_nome"], row["loc_nome_extra"]),
                                        (row["loc_code"], row["loc_code_extra"])),
                "racks": [], "rows": {},
            }
        room = per_room[key]
        if row["rack_uid"] is None:      # sala senza rack: LEFT JOIN
            continue
        used = int(row["used_u"] or 0)
        height = row["height"]
        group = domain.row_group(row["row_label"])
        rack = {
            "uid": str(row["rack_uid"]), "code": row["rack_code"],
            "name": row["rack_name"],
            "label": _label((row["rack_name"], row["rack_name_extra"]),
                            (row["rack_code"], row["rack_code_extra"])),
            # ⚠ `row` è il valore GREZZO e `rowLabel` è ciò che si mostra: chi deve
            # sapere se la fila è davvero «—» o assente lo può ancora distinguere, e
            # chi disegna non deve riapplicare la regola.
            "row": row["row_label"], "rowAssigned": group.assigned,
            "rowLabel": group.label,
            "u": height, "usedU": used,
            "freeU": (None if height is None else max(0, height - used)),
            "largestFreeRun": int(row["largest_free_run"] or 0),
            "deviceCount": int(row["device_count"] or 0),
            "removedCount": int(row["removed_count"] or 0),
        }
        room["racks"].append(rack)
        bucket = room["rows"].setdefault(
            group.key, {"row": group.value, "rowAssigned": group.assigned,
                        "rowLabel": group.label, "totalU": 0, "usedU": 0,
                        "_group": group})
        bucket["totalU"] += height or 0
        bucket["usedU"] += used

    locations: dict[str, dict] = {}
    loc_order: list[str] = []
    for key in order:
        room = per_room[key]
        total = sum((r["u"] or 0) for r in room["racks"])
        used = sum(r["usedU"] for r in room["racks"])
        best = None
        for rack in room["racks"]:
            if best is None or rack["largestFreeRun"] > best["largestFreeRun"]:
                best = rack
        rows = sorted(room["rows"].values(),
                      key=lambda b: domain.row_sort_key(b["_group"]))
        for bucket in rows:
            bucket["freeU"] = max(0, bucket["totalU"] - bucket["usedU"])
            bucket["occupancyPercent"] = domain.percent(bucket["usedU"],
                                                        bucket["totalU"])
            del bucket["_group"]

        entry = {
            "uid": room["roomUid"], "code": room["roomCode"],
            "nome": room["roomNome"], "label": room["roomLabel"],
            "totalU": total, "usedU": used, "freeU": max(0, total - used),
            # ⚠ `domain.percent`, non `round()`: aritmetica intera HALF-UP, la stessa
            # in Python, JavaScript e SQL. `round()` di Python arrotonda al pari e su
            # un rack da 8 U con 1 occupata darebbe 12 dove il frontend mostra 13 (§3).
            "occupancyPercent": domain.percent(used, total),
            "rackCount": len(room["racks"]),
            "bestRack": (None if best is None or best["largestFreeRun"] == 0 else
                         {"uid": best["uid"], "code": best["code"],
                          "label": best["label"],
                          "freeRun": best["largestFreeRun"]}),
            "rows": rows, "racks": room["racks"],
        }
        loc_key = room["locationUid"]
        if loc_key not in locations:
            loc_order.append(loc_key)
            locations[loc_key] = {"uid": loc_key, "code": room["locationCode"],
                                  "nome": room["locationNome"],
                                  "label": room["locationLabel"], "rooms": []}
        locations[loc_key]["rooms"].append(entry)

    return CapacityReport(revision=revision,
                          locations=[locations[k] for k in loc_order])


#: Occupazione per rack come UNIONE DI INTERVALLI (gaps and islands).
#:
#: 1. `span`   ogni dispositivo PRESENTE dà l'intervallo di slot che occupa, già
#:             ritagliato a `[1, rack.u]`. `h` nullo o zero vale 1; un intervallo vuoto
#:             (h negativo, slot iniziale oltre l'altezza) sparisce col `lo <= hi`;
#: 2. `island` si fondono gli intervalli che si toccano o si sovrappongono: una nuova
#:             isola comincia dove `lo` supera di più di uno il massimo `hi` visto
#:             prima. È qui che le sovrapposizioni smettono di contare due volte;
#: 3. `merged` un intervallo per isola;
#: 4. `gap`    i buchi: prima della prima isola, fra due isole, dopo l'ultima. Il
#:             blocco contiguo libero più grande è il buco più lungo — e per un rack
#:             senza dispositivi è il rack intero.
#:
#: `removed_count` esce accanto a `device_count` di proposito: un rack con dodici
#: dispositivi di cui cinque rimossi ha sette apparati in sala, e chi guarda la
#: capacità deve poterlo leggere senza chiedere l'inventario intero.
_CAPACITY_SQL = f"""
WITH span AS (
    SELECT k.uid AS rack_uid, k.u AS height,
           GREATEST(d.u, 1) AS lo,
           LEAST(d.u + (CASE WHEN d.h IS NULL OR d.h = 0 THEN 1 ELSE d.h END) - 1,
                 k.u) AS hi
      FROM inventory_racks k
      JOIN inventory_devices d ON d.rack_uid = k.uid
     WHERE d.u IS NOT NULL AND k.u IS NOT NULL AND k.u >= 1
       AND {_OCCUPIES}
),
clipped AS (
    SELECT * FROM span WHERE lo <= hi
),
marked AS (
    SELECT rack_uid, height, lo, hi,
           CASE WHEN lo <= COALESCE(
                    max(hi) OVER (PARTITION BY rack_uid ORDER BY lo, hi
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
                    lo - 2) + 1
                THEN 0 ELSE 1 END AS fresh
      FROM clipped
),
islands AS (
    SELECT rack_uid, height, lo, hi,
           sum(fresh) OVER (PARTITION BY rack_uid ORDER BY lo, hi
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS island
      FROM marked
),
merged AS (
    SELECT rack_uid, height, island, min(lo) AS lo, max(hi) AS hi
      FROM islands GROUP BY rack_uid, height, island
),
gaps AS (
    SELECT rack_uid,
           GREATEST(
               max(lo - 1 - COALESCE(prev_hi, 0)),
               max(CASE WHEN next_lo IS NULL THEN height - hi ELSE 0 END)
           ) AS largest_free_run,
           sum(hi - lo + 1) AS used_u
      FROM (
        SELECT rack_uid, height, lo, hi,
               lag(hi)  OVER (PARTITION BY rack_uid ORDER BY lo) AS prev_hi,
               lead(lo) OVER (PARTITION BY rack_uid ORDER BY lo) AS next_lo
          FROM merged
      ) w
     GROUP BY rack_uid
)
SELECT r.uid AS room_uid, r.code AS room_code, r.nome AS room_nome,
       r.extra -> 'id' AS room_code_extra, r.extra -> 'nome' AS room_nome_extra,
       r.ordinal AS r_ord,
       l.uid AS loc_uid, l.code AS loc_code, l.nome AS loc_nome,
       l.extra -> 'id' AS loc_code_extra, l.extra -> 'nome' AS loc_nome_extra,
       l.ordinal AS l_ord,
       k.uid AS rack_uid, k.code AS rack_code, k.name AS rack_name,
       k.extra -> 'id' AS rack_code_extra, k.extra -> 'name' AS rack_name_extra,
       k.row_label, k.u AS height,
       COALESCE(g.used_u, 0) AS used_u,
       -- Un rack senza nessun intervallo occupato è libero per intero.
       COALESCE(g.largest_free_run, GREATEST(k.u, 0)) AS largest_free_run,
       (SELECT count(*) FROM inventory_devices dd WHERE dd.rack_uid = k.uid)
           AS device_count,
       (SELECT count(*) FROM inventory_devices d
         WHERE d.rack_uid = k.uid AND NOT ({_OCCUPIES})) AS removed_count
  FROM inventory_rooms r
  JOIN inventory_locations l ON l.uid = r.location_uid
  LEFT JOIN inventory_racks k ON k.room_uid = r.uid
  LEFT JOIN gaps g ON g.rack_uid = k.uid
 ORDER BY l.ordinal, r.ordinal, k.ordinal NULLS FIRST, k.uid
"""


# ==================================================================
# C. scadenze
# ==================================================================

def expiries(conn: Connection, *, today: date,
             warning_days: int = DEFAULT_WARNING_DAYS,
             stato: str | None = None, presenza: str | None = None,
             limit: int | None = None,
             cursor: str | None = None) -> ExpiryPage:
    """Le scadenze ISPEZIONABILI (§7).

    ⚠ Questa non è la domanda del worker, ed è la distinzione che la fase 2G rende
    esplicita invece di lasciarla a due implementazioni che non si parlano:

        vista Scadenze   «quali informazioni di scadenza può ispezionare un
                          operatore?» → tutte quelle valide: scadute, di oggi, future
        worker           «quale scadenza ATTUALMENTE AZIONABILE richiede un'email?»
                          → `0 <= giorni <= finestra`, e non i dismessi

    Da qui il cambiamento rispetto alla 2E: i dispositivi **dismessi COMPAIONO**. Il
    prototipo li saltava (`continue`), e saltarli era sbagliato per la domanda che
    questa vista pone: un apparato dismesso ha un contratto che scade, e chi fa
    l'inventario dei contratti deve poterlo vedere. Non riceve più promemoria via
    posta — quello è §7 — ma resta ispezionabile, cercabile e nello storico (§8).

    I filtri `stato` e `presenza` sono il modo di fare la domanda ristretta:
    `?stato=dismesso&presenza=rimosso` è l'elenco dei contratti di ciò che è stato
    portato via, che è precisamente il riscontro incrociato per cui i dismessi si
    conservano.

    Altro che resta:

      - garanzia e supporto sono due righe distinte per lo stesso dispositivo;
      - un valore vuoto o non interpretabile **non compare** e non è un guasto:
        `supporto = "in attesa"` resta nell'inventario e non produce una scadenza;
      - si ordina per DATA crescente, e tutti i livelli compaiono.

    ⚠ `today` è una DATA DI CALENDARIO nel fuso configurato, non un istante, e
    `daysRemaining` è la differenza fra due date di calendario. Dalla 2G lo è anche nel
    frontend: `Math.round((dt - Date.now()) / 86400000)` dipendeva dall'ora del giorno
    e nella notte del cambio dell'ora dava un risultato giusto per caso (§8.48 voce 9).
    """
    if not isinstance(warning_days, int) or isinstance(warning_days, bool) \
            or warning_days < 0 or warning_days > MAX_WARNING_DAYS:
        raise QueryRejected(
            f"warningDays deve essere un intero fra 0 e {MAX_WARNING_DAYS}")
    _reject_unknown("stato", stato, domain.DEVICE_STATES)
    _reject_unknown("presenza", presenza, domain.DEVICE_PRESENCES)

    limit = _clamp_limit(limit, EXPIRY_DEFAULT_LIMIT, EXPIRY_MAX_LIMIT)
    revision = _revision(conn)
    # La chiave del cursore porta i filtri: una pagina successiva chiesta con filtri
    # diversi non è una pagina successiva.
    scope = f"{warning_days}|{stato or ''}|{presenza or ''}"
    after = decode_cursor(cursor, scope, 6) if cursor else None

    where = _expiry_filters(stato, presenza)
    params: dict[str, Any] = {"today": today, "limit": limit + 1,
                              "warning": warning_days}
    if stato is not None:
        params["stato"] = stato
    if presenza is not None:
        params["presenza"] = presenza

    keyset = ""
    if after is not None:
        params.update({f"a{i}": v for i, v in enumerate(after)})
        keyset = ("WHERE (expiry, l_ord, r_ord, k_ord, d_ord, kind_rank) "
                  "    > (CAST(:a0 AS date), :a1, :a2, :a3, :a4, :a5)")

    sql = _expiry_sql(where)
    rows = conn.execute(text(sql.format(keyset=keyset)), params).all()
    more = len(rows) > limit
    rows = rows[:limit]

    items = []
    for row in rows:
        items.append({
            "kind": row.kind,
            "kindLabel": domain.EXPIRY_LABELS[row.kind],
            "raw": row.raw,
            "expiry": row.expiry.isoformat(),
            "daysRemaining": int(row.days),
            # ⚠ `domain.expiry_level`, non un `CASE` in SQL: la soglia è una decisione
            # e il posto dove sta scritta è uno.
            "level": domain.expiry_level(int(row.days), warning_days),
            # Se questa scadenza produrrebbe un avviso via posta. È l'informazione che
            # spiega la differenza fra le due viste senza costringere chi legge a
            # conoscerla: un dismesso compare con `notifiable: false`.
            "notifiable": (domain.notification_due(int(row.days), [warning_days])
                           and domain.notifies({"stato": row.dev_stato})),
            "device": _device(row),
            **_context(row),
        })

    totals = conn.execute(text(_expiry_totals_sql(where)),
                          {k: v for k, v in params.items()
                           if k in ("today", "warning", "stato", "presenza")}
                          ).mappings().one()

    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(scope,
                                    [last.expiry.isoformat(), last.l_ord, last.r_ord,
                                     last.k_ord, last.d_ord, last.kind_rank])
    return ExpiryPage(revision=revision, today=today, warning_days=warning_days,
                      filters={"stato": stato, "presenza": presenza},
                      totals=dict(totals), items=items, next_cursor=next_cursor)


def _reject_unknown(name: str, value: str | None, vocabulary: tuple) -> None:
    """Un filtro fuori vocabolario è 422, non zero risultati.

    ⚠ La differenza conta: `?stato=dismessi` (plurale) restituirebbe un elenco vuoto,
    e chi lo legge concluderebbe che non ci sono apparati dismessi. Un errore dice
    invece che la domanda era scritta male, ed è l'unica delle due risposte che porta
    a correggerla.
    """
    if value is not None and value not in vocabulary:
        raise QueryRejected(
            f"{name}={value!r} non è un valore noto ({', '.join(vocabulary)})")


def _expiry_filters(stato: str | None, presenza: str | None) -> str:
    """Le condizioni dei filtri, col DEFAULT applicato.

    `coalesce(nullif(...))` e non un confronto secco: un dispositivo senza `stato` è
    `attivo`, quindi `?stato=attivo` deve trovarlo. Senza il default lo troverebbe
    solo chi ha scritto esplicitamente «attivo» nel documento — cioè una parte
    arbitraria dell'inventario.
    """
    clauses = []
    if stato is not None:
        clauses.append(f"{_falsy_sql('d.stato', domain.DEFAULT_STATO)} = :stato")
    if presenza is not None:
        clauses.append(
            f"{_falsy_sql('d.presenza', domain.DEFAULT_PRESENZA)} = :presenza")
    return "".join(f"\n       AND {c}" for c in clauses)


#: ⚠ NESSUN filtro su `stato` qui dentro, e nella 2E c'era.
#:
#: `coalesce(nullif(d.stato, ''), 'attivo') <> 'dismesso'` riproduceva il `continue`
#: del prototipo. La 2G lo toglie: la vista Scadenze è ISPETTIVA e i dismessi restano
#: ispezionabili (§7, §8). Chi vuole la domanda ristretta la fa col filtro, che è
#: esplicito e compare nella risposta.
_EXPIRY_SELECT = """
    SELECT '{kind}'::text AS kind, {rank} AS kind_rank,
           d.{kind} AS raw, d.{kind}_date AS expiry,
           (d.{kind}_date - CAST(:today AS date)) AS days,
           {device},
           d.ordinal AS d_ord,
           {context}
      FROM inventory_devices d
      {joins}
     WHERE d.{kind}_date IS NOT NULL{filters}
"""


def _expiry_sql(filters: str) -> str:
    branches = " UNION ALL ".join(
        _EXPIRY_SELECT.format(kind=kind, rank=rank, device=_DEVICE_COLUMNS,
                              context=_CONTEXT_COLUMNS, joins=_CONTEXT_JOINS,
                              filters=filters)
        for rank, kind in enumerate(domain.EXPIRY_KINDS))
    return f"""
WITH scad AS (
    {branches}
)
SELECT * FROM scad
{{keyset}}
 ORDER BY expiry, l_ord, r_ord, k_ord, d_ord, kind_rank
 LIMIT :limit
"""


def _expiry_totals_sql(filters: str) -> str:
    branches = " UNION ALL ".join(f"""
    SELECT (d.{kind}_date - CAST(:today AS date)) AS days,
           {_falsy_sql('d.stato', domain.DEFAULT_STATO)} AS stato
      FROM inventory_devices d
     WHERE d.{kind}_date IS NOT NULL{filters}
""" for kind in domain.EXPIRY_KINDS)
    return f"""
WITH scad AS ({branches})
SELECT count(*) FILTER (WHERE days < 0)                        AS expired,
       count(*) FILTER (WHERE days >= 0 AND days <= :warning)  AS warning,
       count(*) FILTER (WHERE days > :warning)                 AS future,
       -- Quante di quelle in finestra genererebbero davvero un'email. È il numero
       -- che spiega la differenza fra questa vista e il worker senza obbligare chi
       -- legge a conoscerla.
       count(*) FILTER (WHERE days >= 0 AND days <= :warning
                          AND stato NOT IN ('{domain.NOTIFY_INELIGIBLE_STATES[0]}'))
           AS notifiable
  FROM scad
"""


# ==================================================================
# comune
# ==================================================================

def _revision(conn: Connection) -> Revision:
    """La testa, con la proiezione che DEVE rispecchiarla (§3).

    Solleva `ProjectionNotCurrentError` — 503 — altrimenti. Nessun ripiego sul
    filtraggio del JSON: sarebbe la risposta comoda, e nasconderebbe il difetto che la
    fase 2 esiste per scoprire (§8.45).

    ⚠ Il controllo comprende la versione della MAPPA, e dalla 2G quella è 2: dopo la
    migrazione 0013 la proiezione si dichiara non attuale finché `project.py --rebuild`
    non ha riscritto le righe con `presenza` e `ip_addr`. Un 503 con un rimedio è la
    risposta giusta; servire righe in cui la presenza non esiste sarebbe la sbagliata.
    """
    version, sha256, _declared = require_current_head(conn)
    return Revision(version=version, sha256=sha256)


def _clamp_limit(value: int | None, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise QueryRejected("limit deve essere un intero")
    if value < 1 or value > maximum:
        raise QueryRejected(f"limit deve essere fra 1 e {maximum}")
    return value
