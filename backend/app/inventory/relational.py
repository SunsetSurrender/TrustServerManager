"""Mappa pura fra il documento canonico e la rappresentazione relazionale.

Nessun database, nessun SQLAlchemy: righe come dataclass immutabili. È la stessa
scelta fatta per il motore di diff (§8.10) e per lo stesso motivo — la parte che
deve essere *provata* è la mappa, e provarla contro un database significherebbe
provarla insieme a un database.

    normalise(doc)   documento canonico → modello relazionale
    assemble(model)  modello relazionale → documento canonico

L'invariante che definisce questo modulo, e da cui discende tutto il resto:

    canonicalise(assemble(normalise(doc))) == canonicalise(doc)

e quindi anche `canonical_sha256` uguale e `diff_documents(...) == []`.

Il documento è APERTO, e questo decide il progetto
--------------------------------------------------
Lo schema congelato (§8.16) vincola le chiavi di RADICE, non i campi delle
entità: `validate_document` pretende un `_uid` valido e univoco e non dice nulla
sulle altre chiavi. Il frontend, di suo, deriva ogni entità dall'esistente
proprio perché «i campi sconosciuti e i metadati futuri sopravvivono» (§8.4).

Quindi una mappa che elencasse le colonne e buttasse il resto sarebbe **lossy per
costruzione**: basterebbe un campo nuovo aggiunto dall'interfaccia — o uno
vecchio che nessuno ricorda — perché l'invariante cada, e cadrebbe in produzione,
sul documento di un cliente. Ogni entità ha perciò una colonna `extra` che porta
ciò che le colonne non rappresentano.

`extra` non è una scorciatoia: è lo stesso principio con cui i `vani` restano
JSONB (§8.12). Si normalizza ciò che serve interrogare o vincolare; il resto si
conserva senza pretendere di capirlo.

NULL significa «non rappresentato qui»
--------------------------------------
Un documento aperto può contenere `u: "45"` o `seriali: [1, 2]`: valori che una
colonna tipizzata non può contenere senza mentire. La regola è una sola:

    la colonna vale NULL  ⇔  la chiave è in `extra`

Mai entrambe le cose, mai nessuna delle due. L'alternativa — colonna NOT NULL con
un valore di comodo più la copia in `extra` — darebbe una tabella che si può
interrogare e che risponde il falso, che è peggio di una tabella che dichiara di
non sapere.

Le uniche colonne SEMPRE valorizzate sono quelle che generiamo noi: `uid`, il
riferimento al genitore e `ordinal`.

L'ordine è un dato, non un accidente
------------------------------------
Ogni collezione ha una colonna `ordinal` esplicita, e `assemble` ordina per
quella. L'ordine delle righe che PostgreSQL restituisce senza `ORDER BY` non è
definito, e un `reorder` è un evento di dominio (§8.10): affidarsi all'ordine
fisico significherebbe generare eventi di riordino fantasma al primo `VACUUM`.

Colonne DERIVATE: fuori dal giro completo, di proposito
-------------------------------------------------------
`garanzia_date` e `supporto_date` sono l'interpretazione delle due caselle di
testo (§8.42). Non compaiono in `FIELD_MAP`, quindi `assemble` non le rimette nel
documento e non partecipano all'invariante — e non devono: sarebbero un campo che
l'utente non ha mai scritto.

Ne segue una cosa da tenere presente: **l'invariante del giro completo non può
accorgersi di una data derivata sbagliata.** Una colonna derivata rotta lascia il
documento identico e il digest uguale. È `validate_model` a confrontarla con il
parser, e l'unico posto dove quella differenza si vede.

Riferimento: BACKEND-PLAN.md §8.42.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal
from typing import Any

from app.identity import canonicalise
from app.identity.model import UUID_RE
from app.inventory.json_numbers import is_number, is_representable
from app.inventory.json_strings import is_representable_text

# ==================================================================
# vocabolari
# ==================================================================

#: Chiavi di radice del documento che il modello rappresenta con qualcosa di
#: proprio. Tutto il resto finisce in `root_extra` — oggi mai, perché lo schema
#: congelato (§8.16) ammette solo queste tre, e il carrello esiste per non
#: dipendere da quel fatto.
ROOT_KEYS = ("schemaVersion", "locations", "manuale")

#: Le collezioni di figli: appartengono alla gerarchia, non agli attributi del
#: genitore, e non devono finire in `extra`.
CHILD_KEY = {"location": "sale", "room": "racks", "rack": "devices"}

#: Vocabolario dei tipi di dispositivo e degli stati, come nell'interfaccia
#: (`Component.types()` / `Component.stati()`). Serve alla VALIDAZIONE, non alla
#: mappa: un valore fuori elenco si conserva com'è e si segnala, perché un
#: inventario importato da un foglio di calcolo ne contiene sempre qualcuno e
#: rifiutarlo perderebbe il dato invece di correggerlo.
DEVICE_TYPES = ("server", "rete", "storage", "firewall", "alimentazione", "altro")
DEVICE_STATES = ("attivo", "manutenzione", "dismissione", "dismesso")


# ==================================================================
# righe
# ==================================================================
#
# Il nome del campo Python è quello della colonna SQL. La corrispondenza con la
# chiave del documento è dichiarata in `FIELD_MAP` più sotto, e un test su
# PostgreSQL confronta i campi di queste dataclass con `information_schema`: due
# elenchi che devono coincidere e che vivono in due file divergono, se nessuno li
# confronta.


@dataclass(frozen=True)
class LocationRow:
    uid: str
    ordinal: int
    code: Any = None
    nome: Any = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RoomRow:
    uid: str
    location_uid: Any
    ordinal: int
    code: Any = None
    nome: Any = None
    w: Any = None
    h: Any = None
    area: Any = None
    dim: Any = None
    segnaposto: Any = None
    #: Value object posseduto dalla sala (§8.12): nessuna identità visibile,
    #: nessun CRUD indipendente, nessuno spostamento, nessuna interrogazione
    #: globale. La geometria delle porte è annidata dentro i vani e segue la stessa
    #: regola. Una tabella `vani` più una tabella `porte` darebbero due join per
    #: disegnare una pianta e zero garanzie in più.
    vani: Any = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RackRow:
    uid: str
    room_uid: Any
    ordinal: int
    code: Any = None
    name: Any = None
    #: `row` nel documento. Rinominata perché `row` è una parola chiave SQL e una
    #: colonna che va sempre citata è una colonna che prima o poi qualcuno cita male.
    row_label: Any = None
    u: Any = None
    x: Any = None
    y: Any = None
    w: Any = None
    h: Any = None
    seriali: Any = None
    #: Foto CORRENTE del rack. Le foto che servono alle versioni STORICHE le
    #: protegge `inventory_photo_refs` (§8.5): questa colonna dice qual è la foto
    #: adesso, non quali byte si possono cancellare.
    photo_id: Any = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceRow:
    uid: str
    rack_uid: Any
    ordinal: int
    code: Any = None
    name: Any = None
    type: Any = None
    stato: Any = None
    model: Any = None
    ip: Any = None
    serial: Any = None
    owner: Any = None
    #: TESTO, non `date`. L'inventario reale contiene «in attesa», date malformate
    #: e caselle vuote: una colonna `date` costringerebbe a scartare o a
    #: reinterpretare quei valori, e il posto dove si decide che una data non è
    #: leggibile è già lo scanner delle scadenze (§8.41), che le ignora in silenzio
    #: e le mostra nella vista Scadenze.
    garanzia: Any = None
    supporto: Any = None
    note: Any = None
    u: Any = None
    h: Any = None
    #: DERIVATE dalle due precedenti col parser dello scanner delle scadenze
    #: (§8.41). Non tornano nel documento: sono la forma interrogabile di un
    #: valore che resta autorevole nella sua colonna di testo. `None` significa
    #: «quel testo non è una data», ed è il caso normale.
    garanzia_date: Any = None
    supporto_date: Any = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ManualRow:
    uid: str
    ordinal: int
    code: Any = None
    titolo: Any = None
    #: Value object della voce, come i vani per la sala: paragrafi senza identità
    #: propria, che si modificano solo insieme alla voce che li contiene.
    blocchi: Any = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RelationalModel:
    """Lo stato del documento in forma relazionale. Immutabile e ordinato."""

    schema_version: Any = None
    #: `manuale` ASSENTE e `manuale: []` sono due documenti diversi, e
    #: `canonicalise` conserva la differenza: materializza la radice solo se
    #: c'era. Senza questo booleano il primo salvataggio dopo la migrazione
    #: aggiungerebbe una radice che l'utente non ha mai creato, e comparirebbe
    #: nell'audit come una modifica che nessuno ha fatto.
    has_manual: bool = False
    locations: tuple[LocationRow, ...] = ()
    rooms: tuple[RoomRow, ...] = ()
    racks: tuple[RackRow, ...] = ()
    devices: tuple[DeviceRow, ...] = ()
    manual: tuple[ManualRow, ...] = ()
    #: Chiavi di radice non rappresentate. Con lo schema congelato è sempre vuoto;
    #: esiste perché l'invariante non deve dipendere da quel fatto.
    root_extra: dict = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {"locations": len(self.locations), "rooms": len(self.rooms),
                "racks": len(self.racks), "devices": len(self.devices),
                "manual": len(self.manual)}


# ------------------------------------------------------------------
# «rappresentabile»: una domanda sul TIPO DELLA COLONNA, non sul tipo Python
# ------------------------------------------------------------------
#
# Sono predicati e non elenchi di tipi, perché quattro casi non si esprimono con
# `isinstance`, e tre dei quattro li ha trovati una sonda contro PostgreSQL vero
# invece del ragionamento:
#
#  - `bool` è un `int` in Python (`isinstance(True, int)` è vero). Senza cura,
#    `u: True` finirebbe in una colonna intera come 1 e tornerebbe indietro come
#    `1`: una differenza che il diff riporterebbe come una modifica dell'utente.
#
#  - ⚠ `seriali` è un `text[]`, non un JSONB. Una lista Python qualsiasi *sembra*
#    rappresentabile, ma `["ok", 12345]` in un `text[]` diventa
#    `{"ok","12345"}` — il numero torna indietro come stringa e l'invariante cade
#    in silenzio. Trovato da un test che asserviva la cosa sbagliata: la mappa
#    accettava la lista, e la colonna non l'avrebbe potuta contenere. Serve
#    «lista di sole stringhe», che `isinstance(v, list)` non sa dire.
#
#  - ⚠ `u` e `h` sono `integer`, cioè int32. `u: 99999999999` non è un valore
#    assurdo da difendersi in teoria: è un `INSERT` che fallisce con «integer out
#    of range» a metà del popolamento. Sta in `extra`, dove non ha limiti.
#
#  - ⚠ le colonne geometriche sono `numeric`, e il giro attraverso `numeric` NON
#    è fedele per tutti i float. Vedi la nota sul contratto di legatura qui sotto.
#
# I `vani` e i `blocchi` invece SONO JSONB, e reggono qualunque struttura.

#: Estremi di una colonna `integer`.
INT32_MIN, INT32_MAX = -2147483648, 2147483647


def _is_str(v: Any) -> bool:
    """⚠ Una colonna `text` non può contenere ogni stringa, e la regola è UNA SOLA.

    «PostgreSQL conserva questa stringa?» è la stessa domanda per una colonna `text`,
    per un JSONB e per l'istantanea, e la risposta sta in `json_strings.py`
    (misurata). Riscriverla qui darebbe due idee di stringa rappresentabile che
    divergono sui casi limite, cioè proprio dove serve.

    Ciò che cambia è la CONSEGUENZA, e per il testo è più stretta che per i numeri:
    un numero non rappresentabile trova posto in `extra`, che è JSONB e lossless; una
    stringa che PostgreSQL rifiuta **non entra nemmeno in `extra`**. Perciò la colonna
    dice di non poterla contenere (e il valore va in `extra`), e `validate_model`
    aggiunge un ERRORE: la proiezione di quel documento non si può scrivere affatto.
    Chiamarlo `carried_verbatim` — «integro, ma non interrogabile» — sarebbe falso.

    Non è raggiungibile dalla testa: una stringa così non è mai potuta diventare una
    versione, perché l'`INSERT` in JSONB l'avrebbe rifiutata. La mappa resta comunque
    totale, perché è chiamata anche su documenti che non vengono dal database.
    """
    return isinstance(v, str) and is_representable_text(v)


def _is_int(v: Any) -> bool:
    return (isinstance(v, int) and not isinstance(v, bool)
            and INT32_MIN <= v <= INT32_MAX)


def _is_num(v: Any) -> bool:
    """⚠ Il predicato che dipende da un CONTRATTO DI LEGATURA, e va letto.

    La colonna è `numeric`. Chi la scrive **deve legare `Decimal(repr(v))`, non il
    float**: passando il float, psycopg lo manda come `float8` e la conversione a
    `numeric` di PostgreSQL è lossy. Misurato, non supposto:

        10.0                  →  numeric 10                  → torna 10 (int!)
        0.30000000000000004   →  numeric 0.3                 → torna 0.3

    Legando `Decimal(repr(v))` PostgreSQL conserva le cifre e la SCALA, e il giro
    torna fedele. Restano i valori che `numeric` non può restituire come sono
    arrivati, e li tiene fuori `json_numbers` — finiscono in `extra`, che è lossless.

    ⚠ **Una regola sola, non due.** La domanda «questo numero sopravvive a un giro
    attraverso `numeric`?» è la stessa per una colonna `numeric` e per JSONB, perché
    JSONB i numeri li tiene in `numeric`. Scrivere qui una seconda versione della
    regola vorrebbe dire due idee di «numero rappresentabile» che divergono, e
    divergerebbero sui casi limite: esattamente i valori per cui la regola esiste.
    La differenza fra i due usi non è la regola, è la CONSEGUENZA — qui il valore va
    in `extra`, nell'istantanea il documento si rifiuta (§8.16).

    La mappa resta TOTALE anche per i documenti salvati PRIMA di quel rifiuto: le
    versioni storiche sono immutabili e possono contenere valori che oggi non
    passerebbero più.
    """
    return is_number(v) and is_representable(v)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_json_list(v: Any) -> bool:
    """Qualunque lista: la colonna è JSONB."""
    return isinstance(v, list)


def _is_str_list(v: Any) -> bool:
    """Lista di sole stringhe: la colonna è `text[]`."""
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_uuid(v: Any) -> bool:
    """La colonna è `uuid`: una stringa qualsiasi non ci sta.

    Il documento validato garantisce già che `foto` sia un UUID o assente (§8.5),
    ma la mappa deve essere TOTALE: un valore fuori forma si conserva in `extra`
    invece di far fallire un inserimento a valle.
    """
    return isinstance(v, str) and bool(UUID_RE.match(v))


#: (nome del campo/colonna, chiave nel documento, «ci sta nella colonna?»).
#:
#: È l'unica dichiarazione della corrispondenza: `normalise` e `assemble` la
#: leggono entrambe, quindi non possono divergere. Aggiungere una colonna significa
#: aggiungere una riga qui, un campo nella dataclass e una colonna nella
#: migrazione — e il test di coerenza su PostgreSQL fallisce se se ne dimentica una.
FIELD_MAP: dict[str, tuple[tuple[str, str, Any], ...]] = {
    "location": (
        ("code", "id", _is_str),
        ("nome", "nome", _is_str),
    ),
    "room": (
        ("code", "id", _is_str),
        ("nome", "nome", _is_str),
        ("w", "w", _is_num),
        ("h", "h", _is_num),
        ("area", "area", _is_str),
        ("dim", "dim", _is_str),
        ("segnaposto", "segnaposto", _is_bool),
        ("vani", "vani", _is_json_list),
    ),
    "rack": (
        ("code", "id", _is_str),
        ("name", "name", _is_str),
        ("row_label", "row", _is_str),
        ("u", "u", _is_int),
        ("x", "x", _is_num),
        ("y", "y", _is_num),
        ("w", "w", _is_num),
        ("h", "h", _is_num),
        ("seriali", "seriali", _is_str_list),
        ("photo_id", "foto", _is_uuid),
    ),
    "device": (
        ("code", "id", _is_str),
        ("name", "name", _is_str),
        ("type", "type", _is_str),
        ("stato", "stato", _is_str),
        ("model", "model", _is_str),
        ("ip", "ip", _is_str),
        ("serial", "serial", _is_str),
        ("owner", "owner", _is_str),
        ("garanzia", "garanzia", _is_str),
        ("supporto", "supporto", _is_str),
        ("note", "note", _is_str),
        ("u", "u", _is_int),
        ("h", "h", _is_int),
    ),
    "manual": (
        ("code", "id", _is_str),
        ("titolo", "titolo", _is_str),
        ("blocchi", "blocchi", _is_json_list),
    ),
}

#: Colonne DERIVATE: (nome della colonna, colonna di origine, come si deriva).
#:
#: Non stanno in `FIELD_MAP` di proposito — non hanno una chiave del documento e
#: non tornano indietro. Il valore si calcola da un'ALTRA COLONNA e non dal
#: documento grezzo, così una `garanzia` non rappresentabile (finita in `extra`)
#: non produce una data derivata: colonna NULL, data NULL, e il `CHECK`
#: `ck_device_garanzia_date_needs_text` resta soddisfatto.
DERIVED: dict[str, tuple[tuple[str, str, Any], ...]] = {}


def _parse_expiry(value: Any) -> date | None:
    """⚠ Import LOCALE, e non è pigrizia.

    Si usa il parser dello scanner delle scadenze (§8.41), non un secondo parser
    scritto qui: così `garanzia_date` significa esattamente «la data che il worker
    userà», e non «la data secondo un'altra idea di data valida». Due idee di data
    valida in due moduli divergono, e divergono sui casi limite.

    L'import è dentro la funzione perché `from app.notifications.expiry import ...`
    esegue prima `app/notifications/__init__.py`, che importa il limitatore e quindi
    **SQLAlchemy**. Al livello del modulo la mappa pura si porterebbe dietro il
    database per una funzione di dieci righe che non ne ha bisogno.
    """
    from app.notifications.expiry import parse_expiry
    return parse_expiry(value)


DERIVED["device"] = (("garanzia_date", "garanzia", _parse_expiry),
                     ("supporto_date", "supporto", _parse_expiry))

#: Campi che generiamo noi e che non vengono dal documento: non finiscono mai in
#: `extra` e non compaiono nel documento riassemblato. Un test pretende che ogni
#: campo di ogni dataclass stia o qui, o fra i derivati, o in `FIELD_MAP` — una
#: colonna che non sta in nessuno dei tre non verrebbe mai scritta, e resterebbe
#: vuota per sempre senza che niente lo segnali.
GENERATED = ("uid", "ordinal", "extra", "location_uid", "room_uid", "rack_uid")

ROW_CLASS = {"location": LocationRow, "room": RoomRow, "rack": RackRow,
             "device": DeviceRow, "manual": ManualRow}


def document_key(kind: str, column: str) -> str | None:
    """Chiave del documento per una colonna, se ne ha una."""
    for name, key, _fits in FIELD_MAP[kind]:
        if name == column:
            return key
    return None


def derived_names(kind: str) -> tuple[str, ...]:
    return tuple(name for name, _source, _fn in DERIVED.get(kind, ()))


# ------------------------------------------------------------------
# il contratto di legatura delle colonne `numeric`
# ------------------------------------------------------------------
#
# Le due metà stanno qui, accanto al predicato che le giustifica, e non in chi
# scrive il database: separarle vorrebbe dire che `_is_num` promette una fedeltà
# che dipende da codice scritto altrove. Sono pure, quindi si provano senza
# database — ed è la prova che serve, perché il difetto che coprono si manifesta
# come «il digest non torna».


def to_column_number(value: Any) -> Any:
    """Valore del documento → parametro per una colonna `numeric`.

    `Decimal` SEMPRE, anche per gli interi. Non è pedanteria: la scrittura è un
    `executemany`, e legare a volte un `int` e a volte un `Decimal` per la stessa
    colonna farebbe variare i tipi dei parametri fra le righe dello stesso
    statement. Un tipo unico per colonna è una cosa in meno che può dipendere
    dall'ordine delle righe.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, int):
        return Decimal(value)
    return value


def from_column_number(value: Any) -> Any:
    """Valore letto da una colonna `numeric` → valore del documento.

    `Decimal` con scala 0 era un intero, con scala > 0 era un float. È
    l'informazione che `numeric` conserva e che `float8` avrebbe perso.
    """
    if isinstance(value, Decimal):
        return int(value) if value.as_tuple().exponent >= 0 else float(value)
    return value


# ==================================================================
# documento → modello
# ==================================================================

def _split(kind: str, obj: dict) -> tuple[dict, dict]:
    """(valori delle colonne, `extra`) per una entità.

    Ogni chiave del documento finisce in ESATTAMENTE uno dei due. La chiave dei
    figli (`sale`, `racks`, `devices`) non finisce in nessuno dei due: è la
    gerarchia, e la porta il modello.
    """
    columns: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    mapped_keys = {key: (name, fits) for name, key, fits in FIELD_MAP[kind]}
    child = CHILD_KEY.get(kind)

    for key, value in obj.items():
        if key == "_uid" or key == child:
            continue
        mapping = mapped_keys.get(key)
        if mapping is not None:
            name, fits = mapping
            if fits(value):
                columns[name] = value
                continue
        # Non mappata, oppure mappata ma non rappresentabile dalla colonna: si
        # conserva così com'è.
        extra[key] = value

    return columns, extra


def _derived(kind: str, columns: dict) -> dict:
    """Colonne derivate da altre colonne. Vedi `DERIVED`.

    Si legge da `columns` e non dal documento: una `garanzia` che non è una
    stringa è finita in `extra`, la colonna di testo è NULL, e la data derivata
    deve essere NULL insieme a lei — altrimenti il database avrebbe una data
    interpretata senza il testo da cui è stata interpretata.
    """
    out = {}
    for name, source, derive in DERIVED.get(kind, ()):
        out[name] = derive(columns.get(source))
    return out


def normalise(doc: Any) -> RelationalModel:
    """Documento → modello relazionale. Pura, totale, deterministica.

    **Canonicalizza in ingresso.** Non è una comodità: se la mappa partisse dal
    documento grezzo, i default (§8.14) non sarebbero materializzati e le colonne
    resterebbero vuote per campi che l'applicazione considera già valorizzati.
    Canonicalizzare qui rende `normalise` idempotente rispetto alla forma e fa sì
    che l'invariante valga anche per un documento non canonico in ingresso.

    NON solleva mai: un documento incoerente produce un modello incoerente, che
    `validate_model` sa descrivere. Sollevare qui trasformerebbe ogni controllo
    in «la prima cosa che è andata storta», e la migrazione (fase 2B) ha bisogno
    dell'elenco completo.
    """
    canonical = canonicalise(doc) if isinstance(doc, dict) else {}
    if not isinstance(canonical, dict):
        canonical = {}

    root_extra = {k: v for k, v in canonical.items() if k not in ROOT_KEYS}

    # `schemaVersion` segue la stessa regola di tutte le altre colonne: la colonna
    # è un `integer`, quindi un valore che non ci sta viaggia in `root_extra`. Con
    # lo schema congelato (§8.13) non ci sono documenti così — ed è esattamente il
    # genere di fatto su cui l'invariante non deve poggiare. `validate_model`
    # continua a chiamarlo `missing_schema_version`, che è la cosa giusta da dire:
    # un documento senza versione di schema va rifiutato, non normalizzato.
    raw_schema = canonical.get("schemaVersion")
    schema_version = raw_schema if _is_int(raw_schema) else None
    if raw_schema is not None and schema_version is None:
        root_extra["schemaVersion"] = raw_schema

    locations: list[LocationRow] = []
    rooms: list[RoomRow] = []
    racks: list[RackRow] = []
    devices: list[DeviceRow] = []

    for li, L in enumerate(canonical.get("locations") or []):
        L = L if isinstance(L, dict) else {}
        cols, extra = _split("location", L)
        locations.append(LocationRow(uid=L.get("_uid"), ordinal=li,
                                     extra=extra, **cols))
        for ri, R in enumerate(L.get("sale") or []):
            R = R if isinstance(R, dict) else {}
            cols, extra = _split("room", R)
            rooms.append(RoomRow(uid=R.get("_uid"), location_uid=L.get("_uid"),
                                 ordinal=ri, extra=extra, **cols))
            for ki, K in enumerate(R.get("racks") or []):
                K = K if isinstance(K, dict) else {}
                cols, extra = _split("rack", K)
                racks.append(RackRow(uid=K.get("_uid"), room_uid=R.get("_uid"),
                                     ordinal=ki, extra=extra, **cols))
                for di, V in enumerate(K.get("devices") or []):
                    V = V if isinstance(V, dict) else {}
                    cols, extra = _split("device", V)
                    devices.append(DeviceRow(uid=V.get("_uid"),
                                             rack_uid=K.get("_uid"),
                                             ordinal=di, extra=extra,
                                             **_derived("device", cols), **cols))

    manual: list[ManualRow] = []
    has_manual = canonical.get("manuale") is not None
    if has_manual:
        for mi, M in enumerate(canonical["manuale"] or []):
            M = M if isinstance(M, dict) else {}
            cols, extra = _split("manual", M)
            manual.append(ManualRow(uid=M.get("_uid"), ordinal=mi,
                                    extra=extra, **cols))

    return RelationalModel(
        schema_version=schema_version,
        has_manual=has_manual,
        locations=tuple(locations),
        rooms=tuple(rooms),
        racks=tuple(racks),
        devices=tuple(devices),
        manual=tuple(manual),
        root_extra=root_extra,
    )


# ==================================================================
# modello → documento
# ==================================================================

def _entity(kind: str, row: Any, child_key: str | None = None,
            children: list[dict] | None = None) -> dict:
    """Una entità del documento a partire da una riga.

    L'ordine delle chiavi è DETERMINISTICO: `_uid`, poi le colonne nell'ordine in
    cui `FIELD_MAP` le dichiara, poi le chiavi di `extra` ordinate, poi i figli.
    L'uguaglianza fra dizionari non dipende dall'ordine, ma la serializzazione sì:
    un documento che si riassembla sempre identico byte per byte rende
    confrontabili anche i digest intermedi, e un ordine casuale renderebbe
    illeggibile qualunque `diff` fatto a mano su due dump.
    """
    out: dict[str, Any] = {"_uid": row.uid}
    for name, key, _fits in FIELD_MAP[kind]:
        value = getattr(row, name)
        # NULL significa «non rappresentato qui»: la chiave, se c'era, sta in
        # `extra`. Emetterla comunque inventerebbe un campo che il documento
        # originale non aveva.
        if value is not None:
            out[key] = value
    for key in sorted(row.extra):
        out[key] = row.extra[key]
    if child_key is not None:
        out[child_key] = children or []
    return out


def assemble(model: RelationalModel) -> dict:
    """Modello relazionale → documento canonico.

    Ordina per `ordinal`, mai per l'ordine di arrivo delle righe: l'ordine fisico
    di PostgreSQL non è definito e un riordino fantasma sarebbe un evento di
    dominio (§8.10) che nessuno ha causato.

    Le righe orfane — una sala il cui `location_uid` non esiste — vengono
    **omesse**, non attaccate altrove. Il posto dove un genitore mancante si
    racconta è `validate_model`, che lo chiama `unknown_parent`; inventare un
    genitore qui produrrebbe un documento plausibile e falso.
    """
    devices_by_rack: dict[Any, list[DeviceRow]] = {}
    for d in model.devices:
        devices_by_rack.setdefault(d.rack_uid, []).append(d)
    racks_by_room: dict[Any, list[RackRow]] = {}
    for r in model.racks:
        racks_by_room.setdefault(r.room_uid, []).append(r)
    rooms_by_location: dict[Any, list[RoomRow]] = {}
    for r in model.rooms:
        rooms_by_location.setdefault(r.location_uid, []).append(r)

    def ordered(rows: list) -> list:
        # `ordinal` primo, `uid` come spareggio: due righe con lo stesso ordinale
        # sono un difetto (lo segnala `validate_model`), ma il riassemblaggio deve
        # restare deterministico anche allora, altrimenti lo stesso modello darebbe
        # due documenti diversi e il confronto dei digest della fase 2B
        # fallirebbe a intermittenza.
        return sorted(rows, key=lambda r: (r.ordinal, str(r.uid)))

    locations = []
    for L in ordered(list(model.locations)):
        rooms = []
        for R in ordered(rooms_by_location.get(L.uid, [])):
            racks = []
            for K in ordered(racks_by_room.get(R.uid, [])):
                devices = [_entity("device", V)
                           for V in ordered(devices_by_rack.get(K.uid, []))]
                racks.append(_entity("rack", K, "devices", devices))
            rooms.append(_entity("room", R, "racks", racks))
        locations.append(_entity("location", L, "sale", rooms))

    out: dict[str, Any] = {}
    if model.schema_version is not None:
        out["schemaVersion"] = model.schema_version
    for key in sorted(model.root_extra):
        out[key] = model.root_extra[key]
    out["locations"] = locations
    if model.has_manual:
        out["manuale"] = [_entity("manual", M) for M in ordered(list(model.manual))]
    return out


def round_trip(doc: Any) -> dict:
    """Comodità per i test e per la fase 2B: `assemble(normalise(doc))`."""
    return assemble(normalise(doc))


def column_names(kind: str) -> tuple[str, ...]:
    """Nomi delle colonne di una entità, compresi quelli che generiamo noi."""
    return tuple(f.name for f in fields(ROW_CLASS[kind]))
