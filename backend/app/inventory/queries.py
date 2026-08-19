"""Interrogazioni SQL sulla proiezione relazionale: ricerca, capacità, scadenze.

Questo modulo NON inventa semantica. Ogni funzione riproduce il comportamento che
l'applicazione ha già, e il riferimento è il frontend
(`handoff/Sala Server v2.dc.html`) per ricerca e capacità, lo scanner delle scadenze
(`app/notifications/expiry.py`) per l'interpretazione delle date.

    search(conn, …)     la barra di ricerca globale, comprese le forme IP
    capacity(conn)      la vista Capacità: unità occupate, libere, blocco contiguo
    expiries(conn, …)   la vista Scadenze: garanzia e supporto, con il livello

⚠ Perché la parità viene prima della bellezza (§8.46)
----------------------------------------------------
PostgreSQL sa fare ricerca full-text, sa fare `inet <<= cidr`, sa fare tante cose che
il JavaScript del frontend non fa. Usarle cambierebbe il RISULTATO, e il risultato è
il prodotto: un utente che cerca `srv` e riceve un insieme diverso da quello che
riceveva ieri non ha ottenuto una ricerca migliore, ha ottenuto una ricerca rotta.

Quindi:

  - la ricerca testuale è **sottostringa, senza distinzione di maiuscole** (`strpos`
    su `lower(...)`), non tokenizzata. `strpos` e non `LIKE` perché `LIKE` attribuisce
    un significato a `%` e `_`, che in una casella di ricerca sono caratteri normali:
    con `LIKE` una query contenente `%` troverebbe tutto;
  - le forme IP sono **quelle che `parseIpQuery` riconosce**, e solo quelle: esatta,
    CIDR, intervallo, jolly. Niente IPv6, perché `ipToNum` è IPv4 e un dispositivo con
    `2001:db8::1` oggi non si trova per range — trovarlo sarebbe un comportamento
    nuovo, non una correzione;
  - `used_u` **non è** `SUM(h)`. Vedi `capacity`.

⚠ Nessun `inet`, e nessuna colonna derivata nuova
------------------------------------------------
Il progetto originale prevedeva una colonna `inet` derivata dall'IP. Non c'è, per tre
ragioni che si tengono insieme:

  1. `ipToNum` è IPv4 e rifiuta tutto il resto. Una colonna `inet` accetterebbe anche
     IPv6 e le forme abbreviate, cioè aggiungerebbe semantica che il prodotto non ha;
  2. una colonna derivata NUOVA cambia la distribuzione dei dati fra colonne, quindi
     obbligherebbe ad alzare `MAPPER_VERSION` — e con essa a un `--rebuild` in
     manutenzione, per una query che a questa scala funziona con una scansione;
  3. l'aritmetica di `ipToNum` si scrive come espressione (`_IPNUM`) ed è esatta.

Se un giorno il numero di dispositivi lo giustificasse, il posto dove aggiungere un
indice per espressione è documentato in `_IPNUM`, e non richiede una colonna.

⚠ Che cosa NON si cerca: `extra`
-------------------------------
Un valore che la mappa non ha potuto mettere in una colonna tipizzata sta in `extra`
(§8.42), e `validate_model` lo segnala con `carried_verbatim`, il cui messaggio dice
esattamente: «quel campo, per questa riga, non risponde a una query». Questo modulo
rispetta quella dichiarazione invece di contraddirla. La conseguenza è una divergenza
misurata: un rack i cui `seriali` contengono un numero porta l'intero array in `extra`,
e i suoi seriali non si trovano — mentre il frontend, che fa `String(sn)`, li trova.
È registrata nelle fixture di parità come stranezza, non scoperta in produzione.

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

Riferimento: BACKEND-PLAN.md §8.46.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.inventory.errors import InventoryError
from app.inventory.projection import require_current_head

# ==================================================================
# limiti e codici
# ==================================================================

#: Pagina predefinita e massimo assoluto.
#:
#: Il frontend mostra `results.slice(0, 12)`, ma quello è un troncamento di
#: VISUALIZZAZIONE deciso dallo spazio nel menu a tendina, non un limite semantico:
#: la ricerca legacy calcola tutti i risultati e poi ne disegna dodici. Un'API che
#: restituisse dodici righe senza dirlo mentirebbe; una che le restituisce tutte
#: senza limite si fa spiegare dal primo inventario grande perché non va. Da qui un
#: default generoso, un massimo, e un cursore per il resto.
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
EXPIRY_DEFAULT_LIMIT = 200
EXPIRY_MAX_LIMIT = 1000

#: Soglia del livello «entro N giorni». 90 è la costante del frontend
#: (`giorni <= 90`), non una scelta di questo commit.
DEFAULT_WARNING_DAYS = 90
MAX_WARNING_DAYS = 3650

#: Segnaposto del raggruppamento per fila, copiato dal frontend (`rk.row || '—'`).
#: E' una SENTINELLA che collide col dato: nel seed reale un rack ha la fila «—».
#: Vedi la nota in `capacity`.
ROW_SENTINEL = "—"


class QueryRejected(InventoryError):
    """Parametro di interrogazione non accettabile. 422, non 503: è del client."""
    code = "invalid_query"


class CursorRejected(InventoryError):
    """Cursore non decodificabile, o appartenente a un'altra interrogazione."""
    code = "invalid_cursor"


# ==================================================================
# le forme IP del frontend, tradotte una volta sola
# ==================================================================

_IPV4 = r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
_RE_CIDR = re.compile(rf"^({_IPV4})/(\d{{1,2}})$")
_RE_RANGE = re.compile(rf"^({_IPV4})\s*-\s*({_IPV4})$")
_RE_WILDCARD = re.compile(r"^((?:\d{1,3}\.){1,3})\*$")
_RE_EXACT = re.compile(rf"^{_IPV4}$")


def ip_to_num(value: Any) -> int | None:
    """`ipToNum` del frontend, alla lettera. IPv4 puntato, ottetti ≤ 255, o `None`.

    Serve al generatore delle fixture? No: quello copia il JavaScript. Serve QUI,
    perché `parse_ip_query` deve interpretare la query con le stesse regole con cui
    l'espressione SQL interpreta le colonne — e due interpretazioni diverse dei
    limiti (`999.0.0.1`) darebbero un intervallo che nessun dispositivo può centrare.
    """
    if value is None:
        return None
    text_value = str(value).strip()
    if not _RE_EXACT.match(text_value):
        return None
    parts = [int(p) for p in text_value.split(".")]
    if any(p > 255 for p in parts):
        return None
    return ((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3]


def parse_ip_query(raw: str) -> tuple[int, int] | None:
    """`parseIpQuery` del frontend, alla lettera: CIDR, intervallo, jolly, o `None`.

    ⚠ L'ORDINE dei tentativi è quello del frontend e non è indifferente. E il
    fallimento restituisce `None`, che significa «non è una query IP, cercala come
    testo» — non «nessun risultato». `10.0.0.0/33` è testo, e come testo non trova
    niente; ma la differenza si vede in un caso reale: `10.0.0` non è una forma IP e
    come testo trova `10.0.0.1`, `10.0.0.2`… che è precisamente ciò che l'utente si
    aspetta scrivendo mezzo indirizzo.
    """
    q = (raw or "").strip()

    m = _RE_CIDR.match(q)
    if m:
        base, bits = ip_to_num(m.group(1)), int(m.group(2))
        if base is None or bits > 32:
            return None
        size = 2 ** (32 - bits)
        start = (base // size) * size
        return start, start + size - 1

    m = _RE_RANGE.match(q)
    if m:
        a, b = ip_to_num(m.group(1)), ip_to_num(m.group(2))
        if a is None or b is None:
            return None
        return min(a, b), max(a, b)

    m = _RE_WILDCARD.match(q)
    if m:
        parts = [int(p) for p in m.group(1).split(".") if p]
        if any(p > 255 for p in parts):
            return None
        lo = parts + [0] * (4 - len(parts))
        hi = parts + [255] * (4 - len(parts))
        return (ip_to_num(".".join(str(p) for p in lo)),
                ip_to_num(".".join(str(p) for p in hi)))

    return None


#: `ipToNum(d.ip)` come ESPRESSIONE SQL. Restituisce NULL dove il frontend
#: restituisce `null`, cioè per tutto ciò che non è un IPv4 puntato con ottetti ≤ 255.
#:
#: ⚠ `btrim` con l'insieme esplicito: il `.trim()` di JavaScript togli anche
#: tabulazioni e ritorni a capo, che `btrim(s)` senza argomenti non tocca. Resta fuori
#: lo spazio insecabile (U+00A0), che JavaScript considera spazio e PostgreSQL no: una
#: divergenza teorica, registrata qui e non nascosta.
#:
#: La regex garantisce da 1 a 3 cifre per ottetto, quindi `::int` non può eccedere: il
#: controllo `<= 255` che segue è quello del frontend, non una difesa dal cast.
#:
#: Se un giorno servisse un indice, è questa l'espressione da indicizzare
#: (`CREATE INDEX … ON inventory_devices ((espressione))`), senza aggiungere colonne.
_IPNUM = r"""
CASE WHEN btrim({col}, E' \t\n\r\f\v')
          ~ '^[0-9]{{1,3}}\.[0-9]{{1,3}}\.[0-9]{{1,3}}\.[0-9]{{1,3}}$'
      AND split_part(btrim({col}, E' \t\n\r\f\v'), '.', 1)::int <= 255
      AND split_part(btrim({col}, E' \t\n\r\f\v'), '.', 2)::int <= 255
      AND split_part(btrim({col}, E' \t\n\r\f\v'), '.', 3)::int <= 255
      AND split_part(btrim({col}, E' \t\n\r\f\v'), '.', 4)::int <= 255
     THEN ((split_part(btrim({col}, E' \t\n\r\f\v'), '.', 1)::bigint * 256
            + split_part(btrim({col}, E' \t\n\r\f\v'), '.', 2)::bigint) * 256
            + split_part(btrim({col}, E' \t\n\r\f\v'), '.', 3)::bigint) * 256
            + split_part(btrim({col}, E' \t\n\r\f\v'), '.', 4)::bigint
END
"""


def ipnum_sql(column: str) -> str:
    return _IPNUM.format(col=column)


# ==================================================================
# esiti
# ==================================================================

@dataclass(frozen=True)
class Revision:
    """La revisione che una risposta descrive. Letta nello STESSO snapshot (§4)."""
    version: int
    sha256: str


@dataclass(frozen=True)
class SearchPage:
    revision: Revision
    query: str
    ip_range: tuple[int, int] | None
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

_CONTEXT_COLUMNS = """
       k.uid  AS rack_uid,  k.code  AS rack_code,  k.name AS rack_name,
       r.uid  AS room_uid,  r.code  AS room_code,  r.nome AS room_nome,
       l.uid  AS loc_uid,   l.code  AS loc_code,   l.nome AS loc_nome,
       l.ordinal AS l_ord, r.ordinal AS r_ord, k.ordinal AS k_ord
"""

_CONTEXT_JOINS = """
  JOIN inventory_racks     k ON k.uid = d.rack_uid
  JOIN inventory_rooms     r ON r.uid = k.room_uid
  JOIN inventory_locations l ON l.uid = r.location_uid
"""


def _context(row: Any) -> dict:
    return {
        "rack": {"uid": str(row.rack_uid), "code": row.rack_code,
                 "name": row.rack_name},
        "room": {"uid": str(row.room_uid), "code": row.room_code,
                 "nome": row.room_nome},
        "location": {"uid": str(row.loc_uid), "code": row.loc_code,
                     "nome": row.loc_nome},
    }


# ==================================================================
# A. ricerca
# ==================================================================

def search(conn: Connection, *, q: str, limit: int | None = None,
           cursor: str | None = None) -> SearchPage:
    """La barra di ricerca globale del frontend, in SQL.

    Due modalità, e sono ESCLUSIVE — è così nel frontend e non è un dettaglio:

      - se la query è una forma IP riconosciuta (`parse_ip_query`), si cercano SOLO i
        dispositivi per intervallo di indirizzo. I rack non combaciano affatto, nemmeno
        quello che si chiama «10.0.0.1»: `if (!ipRange && (rk.id...))`;
      - altrimenti si cerca la sottostringa, senza distinzione di maiuscole, nei
        dispositivi su `name, model, ip, serial, owner` e nei rack su
        `id, name, seriali[]`.

    ⚠ I campi dei DISPOSITIVI sono cinque, e `id`, `type`, `stato`, `note` NON ci
    sono. Sembra una dimenticanza del frontend e forse lo è, ma è il comportamento
    attuale: aggiungerli qui vorrebbe dire che la stessa query restituisce più
    risultati sul server che nel browser, cioè due prodotti diversi. La vista
    Inventario ha i suoi filtri per colonna, che sono un'altra cosa (§8.46).

    L'ORDINE è quello del documento — sito, sala, rack, e per ogni rack prima il rack
    e poi i suoi dispositivi — con l'`uid` come ultimo spareggio, così è totale anche
    quando ordinali e nomi collidono.
    """
    limit = _clamp_limit(limit, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT)
    revision = _revision(conn)

    needle = (q or "").strip().lower()
    if not needle:
        # Il frontend con la casella vuota non cerca: `if (q) { … }`. Restituire
        # l'inventario intero sarebbe la risposta comoda e sbagliata.
        return SearchPage(revision=revision, query=q or "", ip_range=None,
                          results=[], next_cursor=None)

    ip_range = parse_ip_query(needle)
    after = decode_cursor(cursor, q or "", 6) if cursor else None

    rows = _search_rows(conn, needle=needle, ip_range=ip_range, after=after,
                        limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]

    results = []
    for row in rows:
        item = {"kind": row.kind, **_context(row)}
        if row.kind == "device":
            item["device"] = {"uid": str(row.dev_uid), "code": row.dev_code,
                              "name": row.dev_name, "type": row.dev_type,
                              "stato": row.dev_stato, "u": row.dev_u, "h": row.dev_h}
        else:
            item["deviceCount"] = row.device_count
        results.append(item)

    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(q or "", [last.l_ord, last.r_ord, last.k_ord,
                                              last.kind_rank, last.d_ord,
                                              str(last.sort_uid)])
    return SearchPage(revision=revision, query=q or "", ip_range=ip_range,
                      results=results, next_cursor=next_cursor)


def _search_rows(conn: Connection, *, needle: str, ip_range: tuple[int, int] | None,
                 after: list | None, limit: int) -> list:
    """Le righe della ricerca, già ordinate. Una query sola, con `UNION ALL`.

    ⚠ Il confronto per riga (`(a,b,c,…) > (…)`) è ciò che rende il cursore una
    chiave e non un offset: PostgreSQL lo risolve con l'ordine lessicografico della
    tupla, che è esattamente l'ordine dell'`ORDER BY`.
    """
    params: dict[str, Any] = {"q": needle, "limit": limit}

    if ip_range is not None:
        params["lo"], params["hi"] = ip_range
        # ⚠ UNA sola valutazione dell'espressione, e nessun `IS NOT NULL`.
        #
        # `NULL BETWEEN a AND b` è NULL, che in una `WHERE` non è vero: il controllo di
        # non-nullità è già implicito, e scriverlo raddoppiava la valutazione di
        # un'espressione con nove `btrim` e otto `split_part`. Misurato a 1720
        # dispositivi: 9,3 ms con due valutazioni, 6,9 ms con una.
        device_where = f"({ipnum_sql('d.ip')}) BETWEEN :lo AND :hi"
        # In modalità IP i rack non partecipano: si tiene comunque il ramo, con una
        # condizione falsa, così la forma della query — e quindi l'ordinamento e il
        # cursore — è una sola. Un `UNION ALL` con un ramo vuoto costa un nulla e
        # risparmia una seconda versione di questo SQL da tenere allineata.
        rack_where = "FALSE"
    else:
        device_where = """(
               strpos(lower(coalesce(d.name,   '')), :q) > 0
            OR strpos(lower(coalesce(d.model,  '')), :q) > 0
            OR strpos(lower(coalesce(d.ip,     '')), :q) > 0
            OR strpos(lower(coalesce(d.serial, '')), :q) > 0
            OR strpos(lower(coalesce(d.owner,  '')), :q) > 0
        )"""
        # ⚠ `k.code` SENZA coalesce, di proposito: il frontend scrive
        # `rk.id.toLowerCase()` senza difese, quindi un rack senza `id` lo fa
        # sollevare. Qui `lower(NULL)` è NULL e la riga non combacia: non si finge
        # che il legacy avrebbe risposto «no», si constata che non avrebbe risposto.
        rack_where = """(
               strpos(lower(k.code), :q) > 0
            OR strpos(lower(coalesce(k.name, '')), :q) > 0
            OR EXISTS (SELECT 1 FROM unnest(coalesce(k.seriali, '{}'::text[])) AS s
                        WHERE strpos(lower(s), :q) > 0)
        )"""

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
                   NULL::text AS dev_name, NULL::text AS dev_type,
                   NULL::text AS dev_stato, NULL::int AS dev_u, NULL::int AS dev_h,
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
                   d.uid AS dev_uid, d.code AS dev_code,
                   d.name AS dev_name, d.type AS dev_type,
                   d.stato AS dev_stato, d.u AS dev_u, d.h AS dev_h,
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
    """La vista Capacità del frontend, in SQL.

    ⚠ `used_u` NON è `SUM(device.h)`, e questa è la scoperta che vale più di tutte in
    questa fase. Il frontend costruisce un vettore di occupazione per rack e poi CONTA
    GLI SLOT DISTINTI occupati:

        const occ = new Array(rk.u + 1).fill(false);
        for (const d of rk.devices) { … for (let k = d.u; k < d.u + (d.h || 1); k++)
                                          if (k <= rk.u) occ[k] = true; }
        for (let k = 1; k <= rk.u; k++) { if (occ[k]) { rkUsed++; … } }

    Da cui tre conseguenze che `SUM(h)` sbaglierebbe:

      - due dispositivi **sovrapposti** occupano gli slot in comune una volta sola;
      - un dispositivo che **sporge** oltre l'altezza del rack viene tagliato;
      - `h` nullo o zero vale 1 (`d.h || 1`), `h` negativo non occupa niente, e uno
        slot di partenza ≤ 0 finisce fuori dal conteggio, che va da 1 a `rk.u`.

    ⚠ E i dispositivi DISMESSI occupano. Nel frontend c'è
    `if ((d.stato || 'attivo') === 'dismesso') {}` — un blocco **vuoto**. Con ogni
    probabilità l'intenzione era escluderli; il fatto è che non li esclude. La vista
    Scadenze invece li salta per davvero (`continue`), quindi la stessa applicazione
    tratta `dismesso` in due modi. Qui si riproduce il comportamento, e l'ambiguità è
    documentata (§8.46) invece di essere risolta di nascosto da questo commit.

    ⚠ Perché unione di INTERVALLI e non `generate_series`
    ----------------------------------------------------
    Enumerare gli slot sarebbe la traduzione ovvia e sarebbe un difetto: `rack.u` è un
    `integer` senza massimo, e il documento `oversized-integers` ne contiene uno da
    3 000 000 000. Un `generate_series` su quel rack produrrebbe tre miliardi di righe
    dentro una richiesta HTTP. Il frontend, sullo stesso documento, esaurisce la
    memoria del browser — l'ho scoperto facendo morire il generatore delle fixture.
    L'unione di intervalli costa quanto i DISPOSITIVI, non quanto l'altezza del rack,
    e sull'altezza non fa nessuna ipotesi.
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
                "locationUid": str(row["loc_uid"]), "locationCode": row["loc_code"],
                "locationNome": row["loc_nome"],
                "l_ord": row["l_ord"], "r_ord": row["r_ord"],
                "racks": [], "rows": {},
            }
        room = per_room[key]
        if row["rack_uid"] is None:      # sala senza rack: LEFT JOIN
            continue
        used = int(row["used_u"] or 0)
        height = row["height"]
        rack = {
            "uid": str(row["rack_uid"]), "code": row["rack_code"],
            "name": row["rack_name"], "row": row["row_label"],
            "u": height, "usedU": used,
            "freeU": (None if height is None else max(0, height - used)),
            "largestFreeRun": int(row["largest_free_run"] or 0),
            "deviceCount": int(row["device_count"] or 0),
        }
        room["racks"].append(rack)
        # ⚠ La chiave del gruppo è `rk.row || '—'` del frontend, sentinella COMPRESA.
        #
        # Il frontend usa la stringa «—» per dire «nessuna fila», e nel seed di
        # produzione esiste un rack la cui fila È «—» (CS-Q01): la sentinella collide
        # col dato, e i due rack finiscono nello stesso gruppo. Rimappare la sentinella
        # a `null` darebbe due gruppi dove il frontend ne mostra uno, cioè una vista
        # Capacità che non corrisponde a quella che gli utenti conoscono.
        #
        # Il valore GREZZO resta nel campo `row` del singolo rack: chi vuole sapere se
        # la fila è davvero «—» o assente lo può ancora distinguere.
        label = row["row_label"] or ROW_SENTINEL
        bucket = room["rows"].setdefault(label, {"row": label, "totalU": 0,
                                                 "usedU": 0})
        bucket["totalU"] += height or 0
        bucket["usedU"] += used

    locations: dict[str, dict] = {}
    loc_order: list[str] = []
    for key in order:
        room = per_room[key]
        total = sum((r["u"] or 0) for r in room["racks"])
        used = sum(r["usedU"] for r in room["racks"])
        # `Math.round(pct * 100)` del frontend. La metà esatta va verso l'alto in
        # JavaScript, e `round()` di Python va al pari: `round(0.5)` è 0. Da qui
        # `floor(x + 0.5)`, che è quello che fa JavaScript.
        percent = 0 if not total else int((used / total) * 100 + 0.5)
        best = None
        for rack in room["racks"]:
            if best is None or rack["largestFreeRun"] > best["largestFreeRun"]:
                best = rack
        rows = sorted(room["rows"].values(), key=lambda b: b["row"])
        for bucket in rows:
            bucket["freeU"] = max(0, bucket["totalU"] - bucket["usedU"])

        entry = {
            "uid": room["roomUid"], "code": room["roomCode"],
            "nome": room["roomNome"],
            "totalU": total, "usedU": used, "freeU": max(0, total - used),
            "occupancyPercent": percent, "rackCount": len(room["racks"]),
            "bestRack": (None if best is None or best["largestFreeRun"] == 0 else
                         {"uid": best["uid"], "code": best["code"],
                          "freeRun": best["largestFreeRun"]}),
            "rows": rows, "racks": room["racks"],
        }
        loc_key = room["locationUid"]
        if loc_key not in locations:
            loc_order.append(loc_key)
            locations[loc_key] = {"uid": loc_key, "code": room["locationCode"],
                                  "nome": room["locationNome"], "rooms": []}
        locations[loc_key]["rooms"].append(entry)

    return CapacityReport(revision=revision,
                          locations=[locations[k] for k in loc_order])


#: Occupazione per rack come UNIONE DI INTERVALLI (gaps and islands).
#:
#: 1. `span`   ogni dispositivo dà l'intervallo di slot che occupa, già ritagliato a
#:             `[1, rack.u]`. `h` nullo o zero vale 1; un intervallo vuoto (h negativo,
#:             slot iniziale oltre l'altezza) sparisce col `lo <= hi`;
#: 2. `island` si fondono gli intervalli che si toccano o si sovrappongono: una nuova
#:             isola comincia dove `lo` supera di più di uno il massimo `hi` visto
#:             prima. È qui che le sovrapposizioni smettono di contare due volte;
#: 3. `merged` un intervallo per isola;
#: 4. `gap`    i buchi: prima della prima isola, fra due isole, dopo l'ultima. Il
#:             blocco contiguo libero più grande è il buco più lungo — e per un rack
#:             senza dispositivi è il rack intero.
_CAPACITY_SQL = """
WITH span AS (
    SELECT k.uid AS rack_uid, k.u AS height,
           GREATEST(d.u, 1) AS lo,
           LEAST(d.u + (CASE WHEN d.h IS NULL OR d.h = 0 THEN 1 ELSE d.h END) - 1,
                 k.u) AS hi
      FROM inventory_racks k
      JOIN inventory_devices d ON d.rack_uid = k.uid
     WHERE d.u IS NOT NULL AND k.u IS NOT NULL AND k.u >= 1
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
       r.ordinal AS r_ord,
       l.uid AS loc_uid, l.code AS loc_code, l.nome AS loc_nome,
       l.ordinal AS l_ord,
       k.uid AS rack_uid, k.code AS rack_code, k.name AS rack_name,
       k.row_label, k.u AS height,
       COALESCE(g.used_u, 0) AS used_u,
       -- Un rack senza nessun intervallo occupato è libero per intero.
       COALESCE(g.largest_free_run, GREATEST(k.u, 0)) AS largest_free_run,
       (SELECT count(*) FROM inventory_devices dd WHERE dd.rack_uid = k.uid)
           AS device_count
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
             limit: int | None = None,
             cursor: str | None = None) -> ExpiryPage:
    """La vista Scadenze del frontend, in SQL, sulle colonne DERIVATE.

    Semantica del frontend, riprodotta:

      - i dispositivi **dismessi** si saltano (`continue`, e qui è un `continue` vero,
        non il blocco vuoto della capacità);
      - garanzia e supporto sono due righe distinte per lo stesso dispositivo;
      - un valore vuoto o non interpretabile **non compare** e non è un guasto:
        `supporto = "in attesa"` resta nell'inventario e non produce una scadenza;
      - si ordina per DATA crescente, e tutti i livelli compaiono — scaduto,
        entro N giorni, futuro. Non è una query «cosa scade presto», è l'elenco.

    ⚠ Le date le interpreta `parse_expiry` (`YYYY-MM-DD` esatto), perché sono le
    colonne `garanzia_date`/`supporto_date` a essere interrogabili e quelle colonne le
    ha scritte lui. Il frontend usa `new Date(v)`, che accetta molto di più
    (`2027/03/15`, `March 15, 2027`, `2027-3-15`). Le forme che il frontend interpreta
    e il backend no **non compaiono** in questa risposta pur essendo visibili nella
    vista Scadenze: è una divergenza reale, misurata dal corpus `expiry-parsing`, e
    documentata in §8.46. Non si è aggiunto un secondo interprete di date: due idee di
    «data valida» divergono, e divergerebbero proprio sui casi limite.

    ⚠ `today` è una DATA DI CALENDARIO nel fuso configurato, non un istante. Il
    frontend fa `Math.round((dt - Date.now()) / 86400000)`, quindi il suo conteggio
    dipende dall'ora del giorno e può differire di uno da questo. I due coincidono
    esattamente a mezzanotte locale — vedi la nota nel generatore delle fixture.
    """
    if not isinstance(warning_days, int) or isinstance(warning_days, bool) \
            or warning_days < 0 or warning_days > MAX_WARNING_DAYS:
        raise QueryRejected(
            f"warningDays deve essere un intero fra 0 e {MAX_WARNING_DAYS}")

    limit = _clamp_limit(limit, EXPIRY_DEFAULT_LIMIT, EXPIRY_MAX_LIMIT)
    revision = _revision(conn)
    after = decode_cursor(cursor, str(warning_days), 6) if cursor else None

    params: dict[str, Any] = {"today": today, "limit": limit + 1,
                             "warning": warning_days}
    keyset = ""
    if after is not None:
        params.update({f"a{i}": v for i, v in enumerate(after)})
        keyset = ("WHERE (expiry, l_ord, r_ord, k_ord, d_ord, kind_rank) "
                  "    > (CAST(:a0 AS date), :a1, :a2, :a3, :a4, :a5)")

    rows = conn.execute(text(_EXPIRY_SQL.format(keyset=keyset)), params).all()
    more = len(rows) > limit
    rows = rows[:limit]

    items = []
    for row in rows:
        items.append({
            "kind": row.kind,
            "raw": row.raw,
            "expiry": row.expiry.isoformat(),
            "daysRemaining": int(row.days),
            "level": row.level,
            "device": {"uid": str(row.dev_uid), "code": row.dev_code,
                       "name": row.dev_name, "type": row.dev_type,
                       "stato": row.dev_stato, "u": row.dev_u, "h": row.dev_h},
            **_context(row),
        })

    totals = conn.execute(text(_EXPIRY_TOTALS_SQL),
                          {"today": today, "warning": warning_days}).mappings().one()

    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(str(warning_days),
                                    [last.expiry.isoformat(), last.l_ord, last.r_ord,
                                     last.k_ord, last.d_ord, last.kind_rank])
    return ExpiryPage(revision=revision, today=today, warning_days=warning_days,
                      totals=dict(totals), items=items, next_cursor=next_cursor)


#: ⚠ `coalesce(nullif(d.stato, ''), 'attivo')` e non `coalesce(d.stato, 'attivo')`.
#: Il frontend scrive `(d.stato || 'attivo')`, e in JavaScript la stringa vuota è
#: falsa: `stato: ""` significa «attivo». Con il solo `coalesce` una stringa vuota
#: resterebbe vuota, diversa da `'dismesso'`, e per caso darebbe la stessa risposta —
#: ma per il motivo sbagliato, e smetterebbe di darla il giorno in cui qualcuno
#: aggiunge un altro stato da escludere.
_EXPIRY_SELECT = """
    SELECT '{kind}'::text AS kind, {rank} AS kind_rank,
           d.{kind} AS raw, d.{kind}_date AS expiry,
           (d.{kind}_date - CAST(:today AS date)) AS days,
           CASE WHEN (d.{kind}_date - CAST(:today AS date)) < 0 THEN 'expired'
                WHEN (d.{kind}_date - CAST(:today AS date)) <= :warning THEN 'warning'
                ELSE 'future' END AS level,
           d.uid AS dev_uid, d.code AS dev_code, d.name AS dev_name,
           d.type AS dev_type, d.stato AS dev_stato, d.u AS dev_u, d.h AS dev_h,
           d.ordinal AS d_ord,
           {context}
      FROM inventory_devices d
      {joins}
     WHERE d.{kind}_date IS NOT NULL
       AND coalesce(nullif(d.stato, ''), 'attivo') <> 'dismesso'
"""

_EXPIRY_SQL = f"""
WITH scad AS (
    {_EXPIRY_SELECT.format(kind='garanzia', rank=0, context=_CONTEXT_COLUMNS,
                           joins=_CONTEXT_JOINS)}
    UNION ALL
    {_EXPIRY_SELECT.format(kind='supporto', rank=1, context=_CONTEXT_COLUMNS,
                           joins=_CONTEXT_JOINS)}
)
SELECT * FROM scad
{{keyset}}
 ORDER BY expiry, l_ord, r_ord, k_ord, d_ord, kind_rank
 LIMIT :limit
"""

_EXPIRY_TOTALS_SQL = """
WITH scad AS (
    SELECT (d.garanzia_date - CAST(:today AS date)) AS days
      FROM inventory_devices d
     WHERE d.garanzia_date IS NOT NULL
       AND coalesce(nullif(d.stato, ''), 'attivo') <> 'dismesso'
    UNION ALL
    SELECT (d.supporto_date - CAST(:today AS date)) AS days
      FROM inventory_devices d
     WHERE d.supporto_date IS NOT NULL
       AND coalesce(nullif(d.stato, ''), 'attivo') <> 'dismesso'
)
SELECT count(*) FILTER (WHERE days < 0)                        AS expired,
       count(*) FILTER (WHERE days >= 0 AND days <= :warning)  AS warning,
       count(*) FILTER (WHERE days > :warning)                 AS future
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
