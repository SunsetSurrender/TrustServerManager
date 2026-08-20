"""La proiezione relazionale: costruirla, rileggerla, e non fidarsi.

Questo è il modulo che tocca SQL. La mappa (`relational.py`) resta pura; qui c'è
tutto ciò che sa di colonne, di legature di parametri e di transazioni.

    currency(conn)        la proiezione rispecchia la testa? Strutturale, 3 query
    require_current(...)  come sopra, ma SOLLEVA. La usa il salvataggio
    status(conn)          rapporto completo per una persona, conteggi compresi
    verify(conn)          riassembla da SQL e confronta: SOLA LETTURA
    current_document(c)   il documento CORRENTE da SQL, con la prova. La usa il GET
    synchronise(conn, …)  porta la proiezione a un modello, e lo DIMOSTRA
    rebuild(conn)         ricostruisce dalla testa, e ABORTA se il giro non torna

Chi la scrive (fase 2C, §8.44) e chi la LEGGE (fase 2D, §8.45)
-------------------------------------------------------------
Dalla fase 2C la proiezione si mantiene a ogni salvataggio: `repository.save` chiama
`synchronise` DENTRO la transazione della richiesta, e `repository.bootstrap` fa lo
stesso per la versione 1. Sono gli unici due scrittori del percorso applicativo;
`scripts/project.py --rebuild` resta lo scrittore esplicito del proprietario.

Dalla fase 2D la proiezione è anche ciò che si LEGGE: `GET /api/inventory` restituisce
`current_document`, cioè il documento riassemblato dalle tabelle, non
`inventory_versions.doc`. Le tabelle normalizzate sono lo stato operativo CORRENTE;
l'istantanea JSONB immutabile resta la storia e il giudice della coerenza.

Chi NON la legge, e resta fuori da questa fase:

  - lo scheduler delle notifiche continua a leggere il documento, non le colonne
    data derivate: sarebbe una decisione da prendere di proposito, con i suoi test;
  - non esiste nessun endpoint di ricerca o di capacità che interroghi le tabelle;
  - il frontend non sa che la proiezione esista, e non deve saperlo: il contratto è
    il documento (§8.22), e la 2D non lo cambia di una virgola.

Tre domande diverse, tre costi diversi, e la separazione è VOLUTA (§8.45):

  - la READINESS legge solo lo STATO — versione, digest, versione della mappa: tre
    confronti fra valori già registrati. È una sonda che gira ogni pochi secondi per
    sempre, e riassemblare l'inventario lì trasformerebbe il controllo in carico;
  - il `GET` riassembla e verifica il giro completo, perché sta per SERVIRE quel
    documento a un utente. Lo paga una volta per richiesta, non una volta al secondo;
  - `project.py --verify` fa la verifica operativa completa, e resta indipendente:
    non è «chiama il GET», perché deve funzionare anche quando il GET non funziona.

L'ordine, che è la sostanza
--------------------------
Un salvataggio (l'ordine completo sta in `repository.save`):

  … lock della testa → la proiezione DEVE già rispecchiarla → no-op / conflitto /
  autorizzazione → si inserisce l'istantanea nuova → `synchronise` sul candidato →
  rilettura e quattro controlli → riferimenti alle foto → audit → testa.

Una ricostruzione:

  1. LOCK della riga di testa (`FOR UPDATE`), come fa un salvataggio (§8.11)
  2. lettura del documento e del digest REGISTRATO di quella versione
  3. il digest registrato deve combaciare con quello ricalcolato
  4. `normalise` + `validate_model`: nessun errore, o si aborta prima di scrivere
  5. `synchronise`: svuota, riscrive, rilegge da SQL, e pretende i quattro controlli

Il passo 1 è ciò che rende «atomica sotto la testa bloccata» una frase con un
significato: un `PUT` concorrente aspetta lì, quindi la proiezione non può
rispecchiare una testa che è cambiata sotto di lei. Il passo 5 è la ragione di tutto
il resto: un popolamento «che sembra andato bene» non vale niente.

Una lettura (`current_document`, fase 2D):

  1. testa: numero di versione e digest REGISTRATO — non il documento
  2. la proiezione deve dichiarare esattamente quella coppia, con la mappa corrente
  3. `read_model`: le cinque tabelle più la riga di stato
  4. `validate_model`: nessun errore, comprese le colonne DERIVATE
  5. `assemble` + digest, che deve combaciare con la testa E con la dichiarazione

Qui il lock NON c'è, e l'assenza è deliberata: una lettura non deve bloccare una
scrittura. Il posto del lock lo prende lo SNAPSHOT — la transazione del chiamante è
`REPEATABLE READ, READ ONLY`, quindi i passi 1-4 vedono lo stesso istante del
database. Senza, un `PUT` che commettesse fra il passo 1 e il passo 3 farebbe
confrontare la testa vecchia con le righe nuove: i digest non tornerebbero e il
`GET` risponderebbe «proiezione incoerente» a fronte di attività perfettamente
normale. Il modo di garantire lo snapshot NON è in questo modulo — è in
`app/api/deps.py`, che apre la connessione — e c'è un test che interroga il database
per sapere in che isolamento sta girando davvero.

Perché la verifica è la STESSA per i due scrittori
-------------------------------------------------
`synchronise` è un corpo solo. Se il salvataggio avesse una verifica propria, copiata
da `rebuild`, il giorno in cui una delle due si irrobustisce l'altra resta indietro —
e sarebbe quella sul percorso delle richieste, cioè quella che protegge i dati degli
utenti invece di un comando che un sistemista lancia a mano.

La transazione è del CHIAMANTE
------------------------------
Come per `InventoryRepository`: qui non si fa `commit`. Un fallimento SOLLEVA, e
chi possiede la transazione la manda in rollback — così non sopravvive una
proiezione a metà, che è precisamente lo stato che nessuno saprebbe interpretare.

Riferimento: BACKEND-PLAN.md §8.42.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.identity import canonicalise, diff_as_dicts
from app.inventory.digest import canonical_sha256
from app.inventory.errors import (
    InventoryError,
    NotBootstrappedError,
    ProjectionInconsistentError,
    ProjectionNotCurrentError,
)
from app.inventory.relational import (
    DERIVED,
    FIELD_MAP,
    MAPPER_VERSION,
    DeviceRow,
    LocationRow,
    ManualRow,
    RackRow,
    RelationalModel,
    RoomRow,
    assemble,
    from_column_number,
    normalise,
    to_column_number,
)
from app.inventory.relational_validate import errors, validate_model, warnings

STATE_TABLE = "inventory_projection_state"

#: (tipo, tabella, colonna del genitore). L'ordine è quello di inserimento: un
#: figlio non può precedere il genitore.
LEVELS = (
    ("location", "inventory_locations", None),
    ("room", "inventory_rooms", "location_uid"),
    ("rack", "inventory_racks", "room_uid"),
    ("device", "inventory_devices", "rack_uid"),
    ("manual", "inventory_manual_entries", None),
)

#: Colonne che vanno legate come JSONB, cioè serializzate prima.
_JSONB = {"extra", "vani", "blocchi"}
#: Colonne `numeric`: vedi il contratto di legatura in `relational.py`.
_NUMERIC = {("room", "w"), ("room", "h"), ("rack", "x"), ("rack", "y"),
            ("rack", "w"), ("rack", "h")}
#: Colonne `text[]`.
_ARRAY = {("rack", "seriali")}


# ==================================================================
# esiti
# ==================================================================

class ProjectionAborted(InventoryError):
    """La ricostruzione si è fermata. La transazione del chiamante va in rollback.

    Porta con sé il motivo E i dettagli: un abort che dice solo «i digest
    differiscono» costringe chi lo legge a rifare a mano il confronto che il codice
    aveva già fatto.
    """

    def __init__(self, reason: str, message: str, details: list | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or []


@dataclass(frozen=True)
class Currency:
    """Attualità: la proiezione rispecchia la testa di ADESSO, e con quale mappa.

    Solo confronti STRUTTURALI — numeri e stringhe già registrati — quindi tre query
    e nessun riassemblaggio. È la distinzione che permette alla readiness di fare
    questa domanda a ogni sonda (§8.44): la fedeltà, che costa un giro completo, la
    dimostrano la verifica transazionale dopo ogni scrittura e `project.py --verify`.
    """

    head_version: int | None = None
    head_sha256: str | None = None
    projected_version: int | None = None
    projected_sha256: str | None = None
    mapper_version: int | None = None

    @property
    def present(self) -> bool:
        """L'assenza della riga è un dato: «non rispecchia nessuna versione»."""
        return self.projected_version is not None

    @property
    def mapper_supported(self) -> bool:
        """La mappa che ha scritto la proiezione è quella che gira adesso.

        `None` (proiezione della fase 2B, che non lo registrava) NON è supportata: la
        distribuzione dei dati fra colonne ed `extra` non è verificabile a posteriori,
        e presumerla è il modo di scoprirla sbagliata quando qualcuno interrogherà.
        """
        return self.mapper_version == MAPPER_VERSION

    @property
    def current(self) -> bool:
        return (self.present
                and self.projected_version == self.head_version
                and self.projected_sha256 == self.head_sha256
                and self.mapper_supported)

    def problem(self) -> str | None:
        """Il motivo tecnico, o None. Serve alla diagnosi, non al contratto: sul filo
        il codice è uno solo, `projection_not_current`."""
        if self.head_version is None:
            return "inventario_non_inizializzato"
        if not self.present:
            return "proiezione_assente"
        if self.projected_version != self.head_version:
            return "proiezione_vecchia_di_versione"
        if self.projected_sha256 != self.head_sha256:
            return "proiezione_vecchia_di_digest"
        if not self.mapper_supported:
            return "versione_della_mappa_non_supportata"
        return None

    def as_dict(self) -> dict:
        return {"testa_versione": self.head_version,
                "testa_digest": self.head_sha256,
                "proiezione_versione": self.projected_version,
                "proiezione_digest": self.projected_sha256,
                "versione_mappa": self.mapper_version,
                "versione_mappa_attesa": MAPPER_VERSION}


@dataclass(frozen=True)
class ProjectionStatus:
    """Che cosa la proiezione dichiara, e che cos'è vero adesso."""

    head_version: int | None = None
    head_sha256: str | None = None
    projected_version: int | None = None
    projected_sha256: str | None = None
    projected_at: datetime | None = None
    mapper_version: int | None = None
    counts: dict = field(default_factory=dict)

    @property
    def currency(self) -> Currency:
        return Currency(head_version=self.head_version,
                        head_sha256=self.head_sha256,
                        projected_version=self.projected_version,
                        projected_sha256=self.projected_sha256,
                        mapper_version=self.mapper_version)

    @property
    def present(self) -> bool:
        """L'assenza della riga è un dato: «non rispecchia nessuna versione»."""
        return self.projected_version is not None

    @property
    def fresh(self) -> bool:
        return self.currency.current

    @property
    def behind(self) -> int | None:
        if self.head_version is None or self.projected_version is None:
            return None
        return self.head_version - self.projected_version

    def describe(self) -> str:
        """Una riga in italiano, perché la legge una persona.

        ⚠ Il significato di «non aggiornata» è cambiato con la fase 2C, e il
        messaggio con esso. In 2B era NORMALE — nessuno sincronizzava — e dirlo
        serviva a non far cercare un guasto che non c'era. Adesso ogni salvataggio
        mantiene le due rappresentazioni, quindi una proiezione vecchia significa una
        cosa sola: **l'API sta rifiutando le scritture**, e finché resta così le
        rifiuterà. Il rimedio va detto, perché chi legge questa riga è la persona che
        deve applicarlo.
        """
        if self.head_version is None:
            return "nessuna versione in testa: non c'è niente da rispecchiare"
        if not self.present:
            return ("la proiezione non rispecchia nessuna versione (mai costruita, "
                    "oppure svuotata): l'API RIFIUTA i salvataggi finché non si "
                    "esegue `project.py --rebuild`")
        if self.fresh:
            return f"aggiornata alla versione {self.projected_version}"
        if self.projected_version != self.head_version:
            return (f"NON aggiornata: rispecchia la {self.projected_version}, "
                    f"la testa è la {self.head_version} "
                    f"({self.behind} version{'e' if self.behind == 1 else 'i'} "
                    "di scarto). Dalla fase 2C ogni salvataggio la mantiene, quindi "
                    "questo scarto NON è previsto: l'API rifiuta i salvataggi "
                    "finché non si esegue `project.py --rebuild`")
        if self.projected_sha256 != self.head_sha256:
            return ("NON aggiornata: stessa versione ma digest diverso. La versione "
                    f"{self.projected_version} è stata verificata con "
                    f"{(self.projected_sha256 or '')[:12]}… e adesso in testa risulta "
                    f"{(self.head_sha256 or '')[:12]}… — un'istantanea immutabile non "
                    "cambia, quindi qualcosa l'ha cambiata fuori dall'API")
        return ("NON utilizzabile: la proiezione è stata scritta da una versione "
                f"della mappa diversa da questa ({self.mapper_version} invece di "
                f"{MAPPER_VERSION}). Le righe riassemblerebbero lo stesso documento "
                "e starebbero nelle colonne sbagliate, cosa che il digest non vede: "
                "serve `project.py --rebuild`")


@dataclass(frozen=True)
class VerifyResult:
    """L'esito di un confronto a sola lettura.

    Due domande distinte, e restano distinte perché hanno cause diverse:

      - `faithful`: le tabelle riassemblano ESATTAMENTE la versione che dichiarano
        di rispecchiare. Un guasto qui è un difetto del codice o una scrittura fuori
        dall'API;
      - `current`: quella versione è la testa di adesso, con una mappa supportata.
        Un guasto qui è operativo — un `--rebuild` mai eseguito dopo un
        aggiornamento — e dalla fase 2C significa che l'API rifiuta le scritture.

    In fase 2B `current` non era un requisito e `ok` era solo `faithful`. Adesso
    servono entrambe: una proiezione fedele a una versione vecchia è esattamente lo
    stato che la 2C non ammette.
    """

    status: ProjectionStatus
    faithful: bool
    reason: str = ""
    details: list = field(default_factory=list)

    @property
    def current(self) -> bool:
        return self.status.currency.current

    @property
    def ok(self) -> bool:
        return self.faithful and self.current


@dataclass(frozen=True)
class RebuildReport:
    version: int
    sha256: str
    counts: dict
    rows_written: int
    warnings: list = field(default_factory=list)


@dataclass(frozen=True)
class SyncReport:
    """Esito di una sincronizzazione già VERIFICATA. Se esiste, il giro è tornato."""

    version: int
    sha256: str
    rows_written: int
    warnings: list = field(default_factory=list)


@dataclass(frozen=True)
class CurrentDocument:
    """Il documento corrente RIASSEMBLATO da SQL, con la prova che è quello giusto.

    Se questo oggetto esiste, allora — dentro un solo istante del database:

        digest(doc) == proiezione.head_sha256 == testa.canonical_sha256
        proiezione.head_version == testa.version
        il modello riletto non ha errori, colonne DERIVATE comprese

    `warnings` esce perché la stessa domanda ha due gravità (§8.42): una data non
    interpretabile o un enum fuori vocabolario sono avvisi, non guasti, e non devono
    rendere il `GET` indisponibile. Un inventario reale ne contiene sempre qualcuno.
    Non finiscono nella risposta HTTP — il contratto del frontend è il documento —
    ma finiscono nei log, dove sono l'unico modo di sapere che quel campo, per quella
    riga, non risponderà a una query.
    """

    version: int
    sha256: str
    doc: dict
    warnings: list = field(default_factory=list)


# ==================================================================
# lettura dello stato
# ==================================================================

def _head(conn: Connection, *, lock: bool = False) -> tuple[int, dict, str] | None:
    """(versione, documento, digest registrato) della testa. `None` se non c'è.

    ⚠ DUE query, non una con JOIN, per la stessa ragione documentata in
    `repository.save` (§8.11): sotto READ COMMITTED, chi aspetta un `FOR UPDATE`
    rivaluta al risveglio solo la riga bloccata, mentre le altre tabelle del join
    restano lette con lo snapshot vecchio. Con un JOIN, chi arriva secondo non
    vedrebbe la versione appena inserita e otterrebbe «non inizializzato» al posto
    di un documento.
    """
    sql = "SELECT version FROM inventory_head WHERE id IS TRUE"
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql)).first()
    if row is None:
        return None
    version = int(row[0])

    snapshot = conn.execute(
        text("SELECT doc, canonical_sha256 FROM inventory_versions "
             "WHERE version = :v"), {"v": version}).first()
    if snapshot is None:                # impossibile: c'è una FK dalla testa
        raise NotBootstrappedError(
            f"la testa punta alla versione {version}, che non esiste")
    return version, snapshot[0], snapshot[1]


#: kind → chiave usata da `RelationalModel.counts()`. Le due nomenclature esistevano
#: entrambe («rack» e «racks») e un test le ha messe a confronto: due vocabolari per
#: la stessa cosa si confrontano male, e chi legge il rapporto non sa quale sta
#: guardando.
COUNT_KEY = {"location": "locations", "room": "rooms", "rack": "racks",
             "device": "devices", "manual": "manual"}


def counts(conn: Connection) -> dict[str, int]:
    """Righe per collezione, con le stesse chiavi di `RelationalModel.counts()`."""
    return {COUNT_KEY[kind]: int(conn.execute(text(f"SELECT count(*) FROM {table}"))
                                 .scalar_one())
            for kind, table, _parent in LEVELS}


def currency(conn: Connection) -> Currency:
    """Attualità, con tre query e NESSUN riassemblaggio.

    Volutamente separata da `status`: quella conta anche le righe (cinque
    `count(*)`) e serve a una persona che legge un rapporto. Questa risponde alla
    sola domanda strutturale, ed è quella che stanno sul percorso di una richiesta —
    la readiness a ogni sonda, il salvataggio prima di scrivere.

    Non legge il documento e non tocca `inventory_versions.doc`: confronta il numero
    di versione e il digest REGISTRATO, che sono già entrambi materializzati. Un
    controllo di attualità che riassemblasse l'inventario intero costerebbe, a ogni
    sonda della readiness, quanto un `--verify` (§8.44).
    """
    head_row = conn.execute(text(
        "SELECT version FROM inventory_head WHERE id IS TRUE")).first()
    if head_row is None:
        head_version, head_sha = None, None
    else:
        head_version = int(head_row[0])
        # Il digest REGISTRATO nella versione, non ricalcolato: è quello che lo
        # stato della proiezione dichiara di aver verificato, quindi è quello con cui
        # va confrontato. Ricalcolarlo qui confronterebbe due cose diverse.
        sha_row = conn.execute(text(
            "SELECT canonical_sha256 FROM inventory_versions WHERE version = :v"),
            {"v": head_version}).first()
        head_sha = None if sha_row is None else sha_row[0]

    state = conn.execute(text(
        f"SELECT head_version, head_sha256, mapper_version FROM {STATE_TABLE}"
    )).first()
    return Currency(
        head_version=head_version,
        head_sha256=head_sha,
        projected_version=None if state is None else int(state[0]),
        projected_sha256=None if state is None else state[1],
        mapper_version=None if state is None or state[2] is None else int(state[2]),
    )


def require_current(conn: Connection, *, version: int, sha256: str) -> Currency:
    """Pretende che la proiezione rispecchi ESATTAMENTE (version, sha256). O solleva.

    `version` e `sha256` li passa chi ha già la testa in mano e BLOCCATA — il
    salvataggio — invece di rileggerla: rileggerla qui significherebbe fare la
    domanda su una testa che, senza il lock, potrebbe non essere la stessa su cui si
    sta per scrivere.

    Solleva `ProjectionNotCurrentError`, che è un 503: non è colpa del client, ed è
    un rifiuto di operare, non un errore del documento.
    """
    state = conn.execute(text(
        f"SELECT head_version, head_sha256, mapper_version FROM {STATE_TABLE}"
    )).first()
    found = Currency(
        head_version=version, head_sha256=sha256,
        projected_version=None if state is None else int(state[0]),
        projected_sha256=None if state is None else state[1],
        mapper_version=None if state is None or state[2] is None else int(state[2]),
    )
    if not found.current:
        raise ProjectionNotCurrentError(
            "la proiezione relazionale non rispecchia la testa "
            f"({found.problem()}): eseguire `project.py --rebuild` come "
            "proprietario dello schema",
            [found.as_dict()])
    return found


def status(conn: Connection) -> ProjectionStatus:
    """Confronto fra ciò che la proiezione dichiara e la testa vera. Sola lettura.

    È il modo previsto di vedere che la proiezione è vecchia: una domanda a cui si
    può rispondere in qualunque momento invece di fidarsi di un'esecuzione andata
    bene mesi prima.
    """
    head = _head(conn)
    state = conn.execute(text(
        f"SELECT head_version, head_sha256, synchronised_at, mapper_version "
        f"  FROM {STATE_TABLE}"
    )).first()
    return ProjectionStatus(
        head_version=None if head is None else head[0],
        head_sha256=None if head is None else head[2],
        projected_version=None if state is None else int(state[0]),
        projected_sha256=None if state is None else state[1],
        projected_at=None if state is None else state[2],
        mapper_version=None if state is None or state[3] is None else int(state[3]),
        counts=counts(conn),
    )


# ==================================================================
# scrittura
# ==================================================================

def _params(kind: str, row: Any, parent_column: str | None) -> dict:
    """Parametri per l'`INSERT` di una riga.

    Ogni colonna passa dalla legatura che il suo tipo SQL pretende. Le colonne
    `numeric` in particolare: vedi `to_column_number` e la nota che spiega perché
    passare un float lì è lossy.
    """
    out: dict[str, Any] = {"uid": row.uid, "ordinal": row.ordinal,
                           "extra": json.dumps(row.extra, ensure_ascii=False)}
    if parent_column:
        out[parent_column] = getattr(row, parent_column)

    for column, _key, _fits in FIELD_MAP[kind]:
        value = getattr(row, column)
        if column in _JSONB:
            value = None if value is None else json.dumps(value, ensure_ascii=False)
        elif (kind, column) in _NUMERIC:
            value = to_column_number(value)
        out[column] = value

    for name, _source, _fn in DERIVED.get(kind, ()):
        out[name] = getattr(row, name)
    return out


def _insert_sql(kind: str, table: str, parent_column: str | None) -> str:
    columns = ["uid", "ordinal", "extra"]
    if parent_column:
        columns.append(parent_column)
    columns += [c for c, _k, _f in FIELD_MAP[kind]]
    columns += [n for n, _s, _f in DERIVED.get(kind, ())]

    def placeholder(column: str) -> str:
        # Cast espliciti dove psycopg non può indovinare il tipo: una lista vuota
        # non dice di che cosa è la lista, e una stringa JSON non dice di essere
        # JSON. Meglio dichiararlo che scoprirlo con un errore di tipo a metà del
        # popolamento.
        if column in _JSONB:
            return f"CAST(:{column} AS jsonb)"
        if (kind, column) in _ARRAY:
            return f"CAST(:{column} AS text[])"
        return f":{column}"

    return (f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholder(c) for c in columns)})")


def _rows_of(model: RelationalModel, kind: str) -> tuple:
    return {"location": model.locations, "room": model.rooms, "rack": model.racks,
            "device": model.devices, "manual": model.manual}[kind]


def clear(conn: Connection) -> None:
    """Svuota la proiezione.

    `DELETE` e non `TRUNCATE`, di proposito. `TRUNCATE` prende un lock
    ACCESS EXCLUSIVE che blocca anche i lettori: oggi nessuno legge, ma la fase 2D
    leggerà, e una ricostruzione che congela le letture sarebbe una scelta fatta
    per abitudine. Il `DELETE` sui siti porta via sale, rack e dispositivi con la
    cascata; le voci di manuale non hanno genitore.
    """
    conn.execute(text("DELETE FROM inventory_locations"))
    conn.execute(text("DELETE FROM inventory_manual_entries"))
    conn.execute(text(f"DELETE FROM {STATE_TABLE}"))


def write_model(conn: Connection, model: RelationalModel) -> int:
    """Scrive il modello nelle tabelle. Restituisce il numero di righe scritte.

    Non svuota niente: chiamare `clear` prima è responsabilità del chiamante, così
    la sequenza «svuota, scrivi, rileggi, confronta» resta visibile in un posto solo
    (`rebuild`) invece di essere nascosta in una funzione che fa due cose.
    """
    written = 0
    for kind, table, parent in LEVELS:
        rows = _rows_of(model, kind)
        if not rows:
            continue
        conn.execute(text(_insert_sql(kind, table, parent)),
                     [_params(kind, r, parent) for r in rows])
        written += len(rows)
    return written


def _write_state(conn: Connection, model: RelationalModel, *, version: int,
                 sha256: str) -> None:
    """La ricevuta: che versione la proiezione rispecchia, e con quale mappa.

    `mapper_version` si scrive da `MAPPER_VERSION`, cioè dal codice che sta scrivendo
    in questo istante. È l'unico valore che può essere vero: dedurlo dalle righe o
    lasciarlo al chiamante lo renderebbe una dichiarazione senza garanzia.
    """
    conn.execute(text(f"""
        INSERT INTO {STATE_TABLE}
               (id, head_version, head_sha256, mapper_version, schema_version,
                has_manual, root_extra, synchronised_at)
        VALUES (TRUE, :version, :sha, :mapper, :schema_version, :has_manual,
                CAST(:root_extra AS jsonb), now())
    """), {
        "version": version,
        "sha": sha256,
        "mapper": MAPPER_VERSION,
        "schema_version": model.schema_version
        if isinstance(model.schema_version, int) else None,
        "has_manual": model.has_manual,
        "root_extra": json.dumps(model.root_extra, ensure_ascii=False),
    })


# ==================================================================
# rilettura
# ==================================================================

def read_model(conn: Connection) -> RelationalModel:
    """Ricostruisce il modello **da SQL**. È il passo che rende il confronto una prova.

    ⚠ Due conversioni obbligatorie, e nessuna delle due è cosmetica:

      - `uuid` → stringa. Una colonna `uuid` letta con `text()` torna come oggetto
        `uuid.UUID`: `assemble` lo metterebbe nel campo `_uid`, dove non è
        serializzabile in JSON e non è uguale alla stringa a cui il digest deve
        corrispondere. Il sintomo sarebbe «il digest non torna», che non fa pensare
        a un tipo.
      - `numeric` → int o float, secondo la scala (`from_column_number`).
    """
    state = conn.execute(text(
        f"SELECT schema_version, has_manual, root_extra FROM {STATE_TABLE}"
    )).first()

    def rows(kind: str, table: str, parent: str | None, cls):
        columns = ["uid", "ordinal"] + ([parent] if parent else []) \
            + [c for c, _k, _f in FIELD_MAP[kind]] \
            + [n for n, _s, _f in DERIVED.get(kind, ())] + ["extra"]
        # ⚠ `ORDER BY` esplicito: l'ordine fisico di PostgreSQL non è definito, e
        # l'ordine delle righe non è ciò che porta l'informazione — la porta
        # `ordinal` — ma un ordine stabile rende confrontabili due dump fatti a
        # mano, e non costa niente.
        sql = (f"SELECT {', '.join(columns)} FROM {table} "
               f"ORDER BY ordinal, uid")
        out = []
        for row in conn.execute(text(sql)).mappings().all():
            values = {}
            for column in columns:
                value = row[column]
                if column in ("uid", parent) or (kind, column) == ("rack", "photo_id"):
                    value = None if value is None else str(value)
                elif (kind, column) in _NUMERIC:
                    value = from_column_number(value)
                values[column] = value
            out.append(cls(**values))
        return tuple(out)

    return RelationalModel(
        schema_version=None if state is None else state[0],
        has_manual=False if state is None else bool(state[1]),
        root_extra={} if state is None else (state[2] or {}),
        locations=rows("location", "inventory_locations", None, LocationRow),
        rooms=rows("room", "inventory_rooms", "location_uid", RoomRow),
        racks=rows("rack", "inventory_racks", "room_uid", RackRow),
        devices=rows("device", "inventory_devices", "rack_uid", DeviceRow),
        manual=rows("manual", "inventory_manual_entries", None, ManualRow),
    )


def model_differences(written: RelationalModel,
                      read_back: RelationalModel) -> list[dict]:
    """Differenze fra il modello scritto e quello riletto, riga per riga e campo
    per campo.

    ⚠ Perché non basta confrontare i documenti.

    Se un valore uscisse da una colonna e rientrasse in `extra`, i due DOCUMENTI
    resterebbero identici — `assemble` emette le chiavi di `extra` insieme alle
    altre — e il digest combacerebbe. Le tabelle però sarebbero diverse, e la
    proiezione non risponderebbe più alle query per cui esiste. È il difetto
    invisibile esattamente dove conta, e questo confronto è l'unico che lo vede.

    Il confronto è per `uid`, non per posizione: l'ordine delle tuple non è un
    dato, `ordinal` sì — e viene confrontato come tutti gli altri campi.
    """
    out: list[dict] = []
    for key in ("schema_version", "has_manual", "root_extra"):
        a, b = getattr(written, key), getattr(read_back, key)
        if a != b:
            out.append({"livello": "document", "campo": key,
                        "scritto": repr(a), "riletto": repr(b)})

    for kind, _table, _parent in LEVELS:
        before = {r.uid: r for r in _rows_of(written, kind)}
        after = {r.uid: r for r in _rows_of(read_back, kind)}
        for uid in sorted(set(before) - set(after), key=str):
            out.append({"livello": kind, "uid": str(uid), "campo": "(riga)",
                        "scritto": "presente", "riletto": "assente"})
        for uid in sorted(set(after) - set(before), key=str):
            out.append({"livello": kind, "uid": str(uid), "campo": "(riga)",
                        "scritto": "assente", "riletto": "presente"})
        for uid in sorted(set(before) & set(after), key=str):
            a, b = before[uid], after[uid]
            for name in a.__dataclass_fields__:
                va, vb = getattr(a, name), getattr(b, name)
                # `type` compreso: `10` e `10.0` sono uguali per `==` e diversi
                # per `json.dumps`, cioè diversi per il digest.
                if va != vb or type(va) is not type(vb):
                    out.append({"livello": kind, "uid": str(uid), "campo": name,
                                "scritto": f"{va!r} ({type(va).__name__})",
                                "riletto": f"{vb!r} ({type(vb).__name__})"})
    return out


def verify(conn: Connection) -> VerifyResult:
    """Riassembla da SQL e confronta col digest della versione RISPECCHIATA.

    Sola lettura, nessun lock: si può eseguire quando si vuole, anche su una
    proiezione vecchia.

    ⚠ Fedeltà e attualità restano due domande diverse, e questa funzione MISURA la
    prima. «Le tabelle riassemblano esattamente la versione che dichiarano di
    rispecchiare» è la fedeltà; «rispecchiano l'ultima versione» è l'attualità, che
    si legge da `status`/`currency` e che il risultato riporta a parte
    (`VerifyResult.current`).

    Sono separate perché hanno cause diverse — un difetto del codice contro un
    comando mai eseguito — e perché la fedeltà si può ancora misurare su una
    proiezione vecchia, il che è precisamente ciò che serve a chi sta diagnosticando.
    Dalla fase 2C però servono entrambe: una proiezione fedele a una versione vecchia
    è lo stato in cui l'API rifiuta le scritture, quindi `ok` le pretende tutte e
    due, e lo strumento esce con 1.
    """
    state = status(conn)
    if not state.present:
        return VerifyResult(status=state, faithful=False,
                            reason="nessuno_stato",
                            details=[{"nota": "la proiezione non dichiara nessuna "
                                              "versione: non c'è niente da verificare"}])

    snapshot = conn.execute(text(
        "SELECT doc, canonical_sha256 FROM inventory_versions WHERE version = :v"
    ), {"v": state.projected_version}).first()
    if snapshot is None:
        return VerifyResult(status=state, faithful=False,
                            reason="versione_rispecchiata_inesistente",
                            details=[{"versione": state.projected_version}])

    # ⚠ Prima si verifica il GIUDICE, poi l'imputato.
    #
    # Tutto il resto di questa funzione confronta la proiezione col
    # `canonical_sha256` REGISTRATO nella versione. Se quel digest non corrisponde più
    # al documento che gli sta accanto, il confronto non ha un riferimento di cui
    # fidarsi: la proiezione potrebbe risultare «fedele» a un digest che non descrive
    # più niente. `rebuild` questo controllo lo faceva da sempre (§8.42) e si rifiuta
    # di costruire; `verify` non lo faceva, ed era un buco preciso — un `UPDATE` a
    # mano su `inventory_versions.doc` che lasciasse intatto il digest passava la
    # verifica. Dalla fase 2D conta di più: l'istantanea immutabile non è solo storia,
    # è l'oracolo contro cui ogni `GET` si misura (§8.45).
    recomputed = canonical_sha256(snapshot[0])
    if recomputed != snapshot[1]:
        return VerifyResult(
            status=state, faithful=False,
            reason="digest_della_versione_incoerente",
            details=[{"versione": state.projected_version,
                      "registrato": snapshot[1], "ricalcolato": recomputed,
                      "nota": "l'istantanea immutabile e il suo digest non "
                              "corrispondono più: il riferimento della verifica non "
                              "è attendibile, e la proiezione non è l'imputato"}])

    model = read_model(conn)

    # ⚠ La coerenza del modello, non solo il digest.
    #
    # Il digest è cieco alle colonne DERIVATE: `garanzia_date` non torna nel
    # documento, quindi una data interpretata male lascia il digest identico. Se
    # questa verifica guardasse solo il digest, l'unico difetto che l'invariante non
    # può vedere sarebbe anche l'unico che lo strumento fatto per vederlo non
    # guarda.
    known = {str(r[0]) for r in conn.execute(text("SELECT id FROM photos")).all()}
    found = errors(validate_model(model, known_photo_ids=known))
    if found:
        return VerifyResult(status=state, faithful=False,
                            reason="modello_incoerente",
                            details=[f.as_dict() for f in found])

    rebuilt = assemble(model)
    digest = canonical_sha256(rebuilt)
    if digest == snapshot[1] == state.projected_sha256:
        return VerifyResult(status=state, faithful=True)

    return VerifyResult(
        status=state, faithful=False, reason="digest_diverso",
        details=[{
            "riassemblato_da_sql": digest,
            "registrato_nella_versione": snapshot[1],
            "verificato_alla_costruzione": state.projected_sha256,
        }] + diff_as_dicts(canonicalise(snapshot[0]), rebuilt)[:20])


# ==================================================================
# lettura del documento corrente (fase 2D, §8.45)
# ==================================================================

def _referenced_photo_ids(conn: Connection) -> set[str]:
    """Le foto che i rack referenziano ADESSO, e che esistono davvero.

    Non `SELECT id FROM photos`, che è quello che fanno `verify` e `rebuild`: quelli
    sono strumenti del proprietario e possono permettersi di leggere tutto. Questa
    query sta sul percorso di una richiesta, e il suo costo deve crescere col numero
    di RACK, non col numero di foto — l'inventario è la cosa che si sta servendo, la
    tabella delle immagini è un magazzino che cresce per conto suo.

    Non tocca `photos.bytes`: la lettura dell'inventario ha bisogno dell'IDENTITÀ
    delle foto, non del loro contenuto. I byte restano una richiesta a parte
    (`GET /api/photos/{uuid}`), e devono restarlo — inserirli qui vorrebbe dire
    trasferire decine di megabyte per disegnare una pianta (§8.5).

    ⚠ Si potrebbe obiettare che il controllo è ridondante: `inventory_racks.photo_id`
    ha una chiave esterna verso `photos.id`, quindi una foto referenziata e assente è
    uno stato che PostgreSQL non ammette. È vero. Si fa comunque, perché passare
    `known_photo_ids=None` significa «non controllare», e `validate_model` avverte
    esplicitamente che un controllo saltato somiglia molto a un controllo passato. Il
    costo è una scansione di solo indice su una chiave primaria.
    """
    rows = conn.execute(text("""
        SELECT id FROM photos
         WHERE id IN (SELECT photo_id FROM inventory_racks
                       WHERE photo_id IS NOT NULL)
    """)).all()
    return {str(r[0]) for r in rows}


def require_current_head(conn: Connection) -> tuple[int, str, Currency]:
    """(versione, digest registrato, dichiarazione) della testa, con la proiezione
    che la rispecchia.

    I due passi che ogni lettura della proiezione deve fare prima di guardare una sola
    riga di entità: leggere la testa, e pretendere che lo stato la dichiari. Solleva
    `NotBootstrappedError` o `ProjectionNotCurrentError`.

    Estratta perché dalla fase 2E ce ne sono QUATTRO di chiamanti —
    `current_document` più le tre interrogazioni (§8.46) — e una precondizione copiata
    quattro volte è una precondizione che prima o poi differisce in uno dei quattro
    posti. Che poi è il posto dove nessuno guarderà.

    ⚠ Legge il DIGEST della versione, non il documento. È la differenza fra un
    metadato e un contenuto: chi ha in mano il digest può verificare, chi ha in mano il
    documento può restituirlo — e allora prima o poi lo restituirà (§8.45).
    """
    head_row = conn.execute(text(
        "SELECT version FROM inventory_head WHERE id IS TRUE")).first()
    if head_row is None:
        raise NotBootstrappedError(
            "nessuna versione in testa: eseguire prima il bootstrap")
    version = int(head_row[0])

    sha_row = conn.execute(text(
        "SELECT canonical_sha256 FROM inventory_versions WHERE version = :v"
    ), {"v": version}).first()
    if sha_row is None:                 # impossibile: c'è una FK dalla testa
        raise NotBootstrappedError(
            f"la testa punta alla versione {version}, che non esiste")

    # Restituisce anche la `Currency` VERIFICATA, così chi ha bisogno del digest che
    # la proiezione dichiara non deve rileggere lo stato: sarebbe una query in più e —
    # peggio — una seconda lettura, che in teoria potrebbe rispondere diversamente se
    # qualcuno la facesse fuori dallo snapshot.
    declared = require_current(conn, version=version, sha256=sha_row[0])
    return version, sha_row[0], declared


@dataclass(frozen=True)
class ValidatedProjection:
    """Una proiezione ATTUALE e COERENTE, con il modello già in mano.

    Esiste perché dalla fase 2F i chiamanti sono due e fanno cose diverse con lo stesso
    risultato: il `GET` continua fino al documento, il worker si fermata qui e passa a
    interrogare le date. Restituire il modello insieme alla dichiarazione evita al
    secondo di rileggerlo — e, più importante, evita che la precondizione esista in due
    copie che un giorno differiranno in una delle due.
    """

    version: int
    #: Il digest REGISTRATO nella versione in testa. Metadato, non contenuto.
    recorded: str
    declared: Currency
    model: RelationalModel
    #: TUTTI i riscontri, errori esclusi (quelli hanno già sollevato). Chi vuole gli
    #: avvisi li filtra con `warnings()`: sono non bloccanti per progetto — un campo
    #: `garanzia` scritto a mano non deve fermare né una lettura né un avviso.
    findings: list


def require_valid_model(conn: Connection) -> ValidatedProjection:
    """Attualità **e** coerenza del modello. La precondizione completa, in un posto.

    I quattro passi che ogni lettura della proiezione deve fare prima di fidarsi di una
    riga, nell'ordine in cui costano meno:

    1. **la testa esiste** e `inventory_versions` ha la sua riga
       (`NotBootstrappedError`);
    2. **la proiezione la dichiara**: versione, digest e versione della mappa
       (`ProjectionNotCurrentError`, tre query e nessun riassemblaggio). Prima di
       leggere una sola riga di entità, così il caso «proiezione non mantenuta» costa
       tre query invece di una lettura completa;
    3. **le righe**, con `read_model`;
    4. **la coerenza del modello**, con `validate_model`. È l'unico controllo che vede
       le colonne **DERIVATE**.

    ⚠ Il passo 4 è quello che non si può sostituire con nulla di più economico, ed è la
    ragione per cui questa funzione esiste. `garanzia_date` **non torna nel documento**:
    una data derivata sbagliata — o azzerata a mano — lascia il documento riassemblato
    identico byte per byte e il digest uguale. Nessun confronto di digest la vede, per
    costruzione. È il punto cieco trovato nella fase 2B, e `validate_model` è l'unica
    cosa che lo chiude.

    Dalla fase 2F lo pretende anche il **worker** (§8.47.4): senza, una `garanzia_date`
    azzerata faceva smettere gli avvisi in silenzio, e il worker non aveva modo di
    accorgersene — la sua guardia confrontava numeri e digest, che quella corruzione
    non muove. Costa una lettura completa della proiezione una volta al giorno, ed è il
    prezzo dichiarato per non tacere su una scadenza.

    Gli **avvisi non bloccano**: `garanzia = "in attesa"` è un valore che una persona
    scrive, non un guasto dello schema. Bloccano solo gli ERRORI.

    Nessun lock: una lettura non deve fermare una scrittura. La coerenza fra i quattro
    passi la dà la TRANSAZIONE del chiamante, che deve essere
    `REPEATABLE READ, READ ONLY` (`db.read_snapshot`). Sotto READ COMMITTED sarebbe
    corretta quasi sempre e produrrebbe un `projection_inconsistent` spurio ogni volta
    che un `PUT` committa nel mezzo — cioè accuserebbe la proiezione di essere corrotta
    proprio mentre funziona.
    """
    version, recorded, declared = require_current_head(conn)
    model = read_model(conn)
    found = validate_model(model, known_photo_ids=_referenced_photo_ids(conn))
    broken = errors(found)
    if broken:
        raise ProjectionInconsistentError(
            f"la proiezione della versione {version} non è coerente "
            f"({len(broken)} errori)",
            [f.as_dict() for f in broken])
    return ValidatedProjection(version=version, recorded=recorded,
                               declared=declared, model=model, findings=found)


def current_document(conn: Connection) -> CurrentDocument:
    """Il documento corrente, riassemblato dalle TABELLE. È ciò che `GET` restituisce.

    Non legge `inventory_versions.doc`. Legge di quella tabella solo il
    `canonical_sha256` della versione in testa, che è metadato: serve come GIUDICE.
    La differenza è tutta la fase 2D — se questa funzione leggesse il documento
    immutabile e lo confrontasse col riassemblato, potrebbe restituire quello in caso
    di dubbio, e il ripiego cancellerebbe esattamente il difetto che la fase 2 esiste
    per scoprire (§8.45).

    ── Che cosa pretende, e perché in quest'ordine ──────────────────────────────

    1. **attualità** (`require_current`), prima di leggere una sola riga di entità.
       È il confronto che costa tre query e che nega la risposta se la proiezione
       dichiara una versione vecchia, nessuna versione, o una mappa che non gira più.
       Farlo prima significa che il caso «la proiezione non è mantenuta» costa tre
       query invece di un riassemblaggio completo, e — cosa più importante —
       distingue *non attuale* (condizione dichiarata, rimedio `--rebuild`) da
       *incoerente* (dichiarazione falsa, causa esterna). Due codici diversi perché
       chi legge un 503 deve sapere quale dei due mondi sta guardando;

    2. **coerenza del modello** (`validate_model`), sul modello RILETTO. È l'unico
       controllo che vede le colonne DERIVATE. `garanzia_date` non torna nel
       documento: una data interpretata male lascia il documento identico e il digest
       uguale, quindi il punto 3 non la vedrebbe mai. È il punto cieco trovato nella
       fase 2B, e questo è il posto in cui si chiude anche in lettura;

    3. **il giro completo** (`assemble` + digest). Il documento che si sta per servire
       deve avere il digest della versione in testa, verificato contro DUE riferimenti
       indipendenti: quello registrato nell'istantanea e quello che la proiezione
       dichiara di aver verificato quando è stata scritta. `require_current` ha già
       provato che i due combaciano, quindi il confronto è formalmente ridondante —
       si scrive esplicito lo stesso, perché se un giorno l'attualità si allentasse,
       questo controllo continuerebbe a reggere da solo.

    Un fallimento del punto 2 o del punto 3 NON è un caso da correggere in silenzio:
    solleva `ProjectionInconsistentError`, il `GET` diventa 503, e il documento non
    esce. Vedi quella classe per il perché non si ripiega sul JSON.

    ── Lo snapshot, che questa funzione non crea ────────────────────────────────

    Nessun lock: una lettura non deve fermare una scrittura. La coerenza fra il passo
    1 e il passo 3 la dà la TRANSAZIONE del chiamante, che deve essere
    `REPEATABLE READ, READ ONLY` (`app/api/deps.py`). Sotto READ COMMITTED questa
    funzione sarebbe corretta il 99,9% delle volte e produrrebbe un 503 spurio ogni
    volta che un `PUT` committa nel mezzo — cioè il modo peggiore di sbagliare: un
    guasto raro, non riproducibile, e con un messaggio che accusa la proiezione di
    essere corrotta quando invece funzionava.
    """
    # --- 1..4: attualità, righe, coerenza del modello ---
    valid = require_valid_model(conn)
    version, recorded, declared = valid.version, valid.recorded, valid.declared
    model, found = valid.model, valid.findings

    # --- 5. il giro completo, contro due riferimenti indipendenti ---
    doc = assemble(model)
    digest = canonical_sha256(doc)
    if digest != recorded or digest != declared.projected_sha256:
        raise ProjectionInconsistentError(
            f"il documento riassemblato da SQL non è la versione {version} in testa",
            [{"riassemblato_da_sql": digest,
              "registrato_nella_versione": recorded,
              "verificato_alla_costruzione": declared.projected_sha256,
              "versione": version}])

    return CurrentDocument(version=version, sha256=digest, doc=doc,
                           warnings=[f.as_dict() for f in warnings(found)])


# ==================================================================
# sincronizzazione: scrivere, rileggere, e pretendere la prova
# ==================================================================

def synchronise(conn: Connection, model: RelationalModel, *, version: int,
                sha256: str, known_photo_ids: set[str] | None = None) -> SyncReport:
    """Porta la proiezione a `model`, e non torna se non l'ha DIMOSTRATO.

    È il cuore della fase 2C, e lo usano entrambi gli scrittori: il salvataggio
    (dentro la transazione della richiesta) e `rebuild` (dentro quella del comando
    del proprietario). Un solo corpo di proprietà, non due: se la verifica dopo un
    salvataggio fosse una copia di quella della ricostruzione, il giorno in cui una
    delle due si irrobustisce l'altra resta indietro — e sarebbe quella sul percorso
    delle richieste, cioè quella che conta.

    ── Perché SOSTITUZIONE INTEGRALE e non differenza incrementale ──────────────

    Si svuota e si riscrive. Non è pigrizia, è la scelta più difficile da sbagliare,
    e a questa scala non costa niente di misurabile (il seed reale: 197 righe).

      - **produce per costruzione lo stato del candidato.** Una differenza
        incrementale produce «lo stato precedente più le modifiche che ho saputo
        calcolare», che è la stessa cosa solo se il calcolo è completo. Aggiunte,
        rimozioni, aggiornamenti, ridenominazioni, spostamenti fra genitori,
        riordini, e ridenominazione-più-spostamento nello stesso PUT sono sei
        occasioni di sbagliare che qui semplicemente non esistono;

      - **rende innocui gli scambi di chiavi ambito.** Due rack che si scambiano il
        `code` nella stessa sala violerebbero l'unicità a metà di un `UPDATE`
        incrementale; il vincolo è `DEFERRABLE INITIALLY IMMEDIATE` proprio per
        questo. Cancellando prima e inserendo dopo, il conflitto non nasce: non
        serve appoggiarsi al rinvio, e non serve ricordarsi che serve;

      - **niente `TRUNCATE`.** `DELETE`, che è un privilegio ordinario e non prende
        un lock ACCESS EXCLUSIVE — quello bloccherebbe anche i lettori della fase
        2D. La cascata dai siti porta via sale, rack e dispositivi; le voci di
        manuale non hanno genitore e si cancellano a parte.

    Il costo è righe morte a ogni salvataggio, che a duecento righe è lavoro di
    autovacuum invisibile. Se un giorno le righe fossero centomila, questa funzione è
    il posto dove cambiare strategia — e i test la interrogano dal comportamento, non
    dall'implementazione, quindi resterebbero validi.

    ── I quattro controlli, che sono il motivo di tutto il resto ────────────────

    Non fa `commit` e non intercetta niente: SOLLEVA `ProjectionAborted` e la
    transazione del chiamante si annulla per intero. È il rollback a garantire che
    non sopravviva una proiezione a metà — non l'ordine degli statement.
    """
    # --- si svuota e si riscrive, riga di stato COMPRESA ---
    #
    # ⚠ La riga di stato si scrive QUI, non dopo la rilettura. Porta anche
    # `schemaVersion`, `has_manual` e `root_extra`, cioè il livello di RADICE del
    # documento: scritta dopo, la rilettura vedrebbe una radice vuota e il confronto
    # fallirebbe su una differenza che la scrittura non ha commesso. È un errore già
    # commesso una volta, in `rebuild`.
    clear(conn)
    written = write_model(conn, model)
    _write_state(conn, model, version=version, sha256=sha256)

    # --- rilettura DA SQL ---
    read_back = read_model(conn)

    # --- 1. il modello riletto è quello scritto ---
    #
    # Non basta confrontare i documenti: un valore che passasse da una colonna a
    # `extra` lascerebbe il documento identico e il digest uguale. Vedi
    # `model_differences`.
    differences = model_differences(model, read_back)
    if differences:
        raise ProjectionAborted(
            "modello_riletto_diverso",
            f"la proiezione della versione {version} non rilegge come è stata "
            f"scritta: {len(differences)} differenze",
            differences[:40])

    # --- 2. il modello riletto è coerente ---
    #
    # Sul modello RILETTO, non su quello scritto: qui si guarda ciò che il database
    # contiene adesso. È anche l'unico controllo che vede le colonne DERIVATE, a cui
    # il digest è cieco — `garanzia_date` non torna nel documento, quindi una data
    # interpretata male lo lascerebbe identico.
    found = validate_model(read_back, known_photo_ids=known_photo_ids)
    if errors(found):
        raise ProjectionAborted(
            "modello_riletto_incoerente",
            f"la proiezione riletta della versione {version} non è coerente: "
            f"{len(errors(found))} errori",
            [f.as_dict() for f in errors(found)])

    # --- 3/4. il documento riassemblato è quello dell'istantanea ---
    rebuilt = assemble(read_back)
    digest = canonical_sha256(rebuilt)
    if digest != sha256:
        raise ProjectionAborted(
            "digest_diverso",
            f"il documento riassemblato da SQL non è la versione {version}",
            [{"riassemblato_da_sql": digest, "atteso": sha256}])

    return SyncReport(version=version, sha256=digest, rows_written=written,
                      warnings=[f.as_dict() for f in warnings(found)])


# ==================================================================
# ricostruzione
# ==================================================================

def rebuild(conn: Connection) -> RebuildReport:
    """Ricostruisce la proiezione dalla testa. Vedi l'ordine in testa al modulo.

    SOLLEVA `ProjectionAborted` a ogni passo che non torna, e non fa `commit`: la
    transazione è del chiamante, che la manda in rollback. Non esiste un esito
    «proiezione a metà».
    """
    # --- 1. lock della testa ---
    head = _head(conn, lock=True)
    if head is None:
        raise NotBootstrappedError(
            "nessuna versione in testa: eseguire prima il bootstrap")
    version, doc, recorded = head

    # --- 2/3. il digest registrato deve valere ---
    #
    # Si confronta il registrato col ricalcolato PRIMA di usarlo come riferimento.
    # Se differiscono, il difetto non è nella proiezione ed è più grave: significa
    # che un'istantanea immutabile e il suo digest non si corrispondono più. Usare
    # il ricalcolato in silenzio sarebbe coprire proprio quel caso.
    recomputed = canonical_sha256(doc)
    if recorded != recomputed:
        raise ProjectionAborted(
            "digest_della_versione_incoerente",
            f"la versione {version} porta un digest registrato che non "
            "corrisponde al suo contenuto: la proiezione non ha un riferimento "
            "di cui fidarsi",
            [{"registrato": recorded, "ricalcolato": recomputed}])

    # --- 4. modello e coerenza, prima di scrivere ---
    model = normalise(doc)
    known = {str(r[0]) for r in conn.execute(text("SELECT id FROM photos")).all()}
    found = validate_model(model, known_photo_ids=known)
    if errors(found):
        raise ProjectionAborted(
            "modello_incoerente",
            f"il documento in testa (versione {version}) non produce un modello "
            f"coerente: {len(errors(found))} errori",
            [f.as_dict() for f in errors(found)])

    # --- 5/7. scrittura e prova, con lo STESSO corpo che usa il salvataggio ---
    #
    # `synchronise` svuota, riscrive, rilegge da SQL e pretende i quattro controlli.
    # Non è una comodità: se la ricostruzione e il salvataggio avessero due verifiche
    # separate, il giorno in cui una si irrobustisce l'altra resta indietro — e
    # scoprirlo richiederebbe di confrontare a mano due funzioni lunghe.
    report = synchronise(conn, model, version=version, sha256=recorded,
                         known_photo_ids=known)

    return RebuildReport(version=version, sha256=report.sha256,
                         counts=model.counts(),
                         rows_written=report.rows_written,
                         warnings=report.warnings)
