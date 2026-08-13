"""La proiezione relazionale: costruirla, rileggerla, e non fidarsi.

Questo è il modulo che tocca SQL. La mappa (`relational.py`) resta pura; qui c'è
tutto ciò che sa di colonne, di legature di parametri e di transazioni.

    status(conn)    che versione la proiezione dichiara di rispecchiare
    verify(conn)    riassembla da SQL e confronta i digest: SOLA LETTURA
    rebuild(conn)   ricostruisce tutto, e ABORTA se il giro non torna

⚠ Nessuno consuma la proiezione. Non `GET`, non `PUT`, non la readiness, non lo
scheduler, non il frontend (§8.42). La fase 2B costruisce una rappresentazione in
ombra e la confronta; il passaggio è la 2D, e avviene solo dopo che il confronto è
stato verde ripetutamente su dati veri. Un `GET` che assembla male restituisce un
documento plausibile, il client lo rimanda con un `PUT`, e la differenza diventa una
versione nuova con un contenuto che nessuno ha scritto.

L'ordine della ricostruzione, che è la sostanza
----------------------------------------------
  1. LOCK della riga di testa (`FOR UPDATE`), come fa un salvataggio (§8.11)
  2. lettura del documento e del digest REGISTRATO di quella versione
  3. il digest registrato deve combaciare con quello ricalcolato
  4. `normalise` + `validate_model`: nessun errore, o si aborta prima di scrivere
  5. si svuota la proiezione e la si riscrive per intero, riga di stato compresa
  6. si rilegge **da SQL** e si riassembla
  7. il modello riletto deve essere uguale a quello scritto **e** il digest del
     documento riassemblato deve combaciare con quello registrato

Il passo 1 è ciò che rende «atomica sotto la testa bloccata» una frase con un
significato: un `PUT` concorrente aspetta lì, quindi la proiezione non può
rispecchiare una testa che è cambiata sotto di lei. Il passo 7 è la ragione di
tutto il resto: un popolamento «che sembra andato bene» non vale niente.

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
from app.inventory.errors import InventoryError, NotBootstrappedError
from app.inventory.relational import (
    DERIVED,
    FIELD_MAP,
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
from app.inventory.repository import canonical_sha256

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
class ProjectionStatus:
    """Che cosa la proiezione dichiara, e che cos'è vero adesso."""

    head_version: int | None = None
    head_sha256: str | None = None
    projected_version: int | None = None
    projected_sha256: str | None = None
    projected_at: datetime | None = None
    counts: dict = field(default_factory=dict)

    @property
    def present(self) -> bool:
        """L'assenza della riga è un dato: «non rispecchia nessuna versione»."""
        return self.projected_version is not None

    @property
    def fresh(self) -> bool:
        return (self.present
                and self.projected_version == self.head_version
                and self.projected_sha256 == self.head_sha256)

    @property
    def behind(self) -> int | None:
        if self.head_version is None or self.projected_version is None:
            return None
        return self.head_version - self.projected_version

    def describe(self) -> str:
        """Una riga in italiano, perché la legge una persona.

        Una proiezione non aggiornata **è prevista** in questa fase: la
        sincronizzazione a ogni salvataggio è la 2C. Il messaggio lo dice, così chi
        lo legge non va a cercare un guasto che non c'è.
        """
        if self.head_version is None:
            return "nessuna versione in testa: non c'è niente da rispecchiare"
        if not self.present:
            return ("la proiezione non rispecchia nessuna versione "
                    "(mai costruita, oppure svuotata)")
        if self.fresh:
            return f"aggiornata alla versione {self.projected_version}"
        if self.projected_version != self.head_version:
            return (f"NON aggiornata: rispecchia la {self.projected_version}, "
                    f"la testa è la {self.head_version} "
                    f"({self.behind} version{'e' if self.behind == 1 else 'i'} "
                    "di scarto). È previsto: la sincronizzazione a ogni "
                    "salvataggio è la fase 2C")
        return ("NON aggiornata: stessa versione ma digest diverso. La versione "
                f"{self.projected_version} è stata verificata con "
                f"{(self.projected_sha256 or '')[:12]}… e adesso in testa risulta "
                f"{(self.head_sha256 or '')[:12]}… — un'istantanea immutabile non "
                "cambia, quindi qualcosa l'ha cambiata fuori dall'API")


@dataclass(frozen=True)
class VerifyResult:
    """L'esito di un confronto a sola lettura."""

    status: ProjectionStatus
    faithful: bool
    reason: str = ""
    details: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.faithful


@dataclass(frozen=True)
class RebuildReport:
    version: int
    sha256: str
    counts: dict
    rows_written: int
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


def status(conn: Connection) -> ProjectionStatus:
    """Confronto fra ciò che la proiezione dichiara e la testa vera. Sola lettura.

    È il modo previsto di vedere che la proiezione è vecchia: non un guasto da
    nascondere, ma una domanda a cui si può rispondere in qualunque momento invece
    di fidarsi di un'esecuzione andata bene mesi prima.
    """
    head = _head(conn)
    state = conn.execute(text(
        f"SELECT head_version, head_sha256, synchronised_at FROM {STATE_TABLE}"
    )).first()
    return ProjectionStatus(
        head_version=None if head is None else head[0],
        head_sha256=None if head is None else head[2],
        projected_version=None if state is None else int(state[0]),
        projected_sha256=None if state is None else state[1],
        projected_at=None if state is None else state[2],
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
    conn.execute(text(f"""
        INSERT INTO {STATE_TABLE}
               (id, head_version, head_sha256, schema_version, has_manual,
                root_extra, synchronised_at)
        VALUES (TRUE, :version, :sha, :schema_version, :has_manual,
                CAST(:root_extra AS jsonb), now())
    """), {
        "version": version,
        "sha": sha256,
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

    ⚠ Fedeltà e attualità sono due domande diverse, e questa funzione risponde
    alla prima. «Le tabelle riassemblano esattamente la versione che dichiarano di
    rispecchiare» è la fedeltà, ed è l'unica cosa che può essere un guasto adesso.
    «Rispecchiano l'ultima versione» è l'attualità, e in fase 2B **non esserlo è
    normale**: la sincronizzazione a ogni salvataggio è la 2C. Confonderle
    significherebbe che ogni salvataggio fa suonare un allarme.
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

    # --- 5. si svuota e si riscrive, riga di stato COMPRESA ---
    #
    # ⚠ La riga di stato si scrive QUI, non alla fine, e il primo tentativo la
    # scriveva alla fine. Sembrava più prudente — «nessuna riga dichiara una
    # proiezione fedele finché non lo è» — ed era sbagliato: quella riga porta anche
    # `schemaVersion`, `has_manual` e `root_extra`, cioè il livello di RADICE del
    # documento. Scritta dopo la rilettura, il passo 6 rileggeva una radice vuota e
    # il confronto fallisce su una differenza che il popolamento non ha commesso.
    #
    # La prudenza non serviva: `rebuild` solleva e la transazione del chiamante va
    # in rollback, quindi una riga che esiste è una riga la cui verifica è passata.
    # È il rollback a garantirlo, non l'ordine degli statement.
    clear(conn)
    written = write_model(conn, model)
    _write_state(conn, model, version=version, sha256=recorded)

    # --- 6. rilettura DA SQL ---
    read_back = read_model(conn)

    # --- 7. i due confronti ---
    #
    # Il modello e il documento. Non sono la stessa domanda: un valore che passasse
    # da una colonna a `extra` lascerebbe il documento identico. Vedi
    # `model_differences`.
    differences = model_differences(model, read_back)
    if differences:
        raise ProjectionAborted(
            "modello_riletto_diverso",
            f"la proiezione della versione {version} non rilegge come è stata "
            f"scritta: {len(differences)} differenze",
            differences[:40])

    rebuilt = assemble(read_back)
    digest = canonical_sha256(rebuilt)
    if digest != recorded:
        raise ProjectionAborted(
            "digest_diverso",
            f"il documento riassemblato da SQL non è la versione {version}",
            [{"riassemblato_da_sql": digest, "registrato": recorded}]
            + diff_as_dicts(canonicalise(doc), rebuilt)[:20])

    return RebuildReport(version=version, sha256=digest, counts=model.counts(),
                         rows_written=written,
                         warnings=[f.as_dict() for f in warnings(found)])
