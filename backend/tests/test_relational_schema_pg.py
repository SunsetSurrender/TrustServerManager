"""Le tabelle normalizzate: forma, vincoli, privilegi — e che NIENTE le scriva.

PostgreSQL reale, perché ciò che si verifica è comportamento del database:
`DEFERRABLE` è una proprietà di un vincolo, la cascata è una proprietà di una
chiave esterna, e «il ruolo dell'API non può scrivere» sono privilegi.

Due cose che questa suite deve dimostrare più delle altre:

  1. le colonne SQL e i campi delle dataclass coincidono — due elenchi in due file
     divergono, se nessuno li confronta;
  2. `GET` e `PUT` non sono cambiati: dopo un salvataggio le tabelle normalizzate
     restano VUOTE. È l'affermazione centrale, e l'unico modo di provarla è provare
     a violarla.

Qui si guardano la FORMA e i VINCOLI. Il popolamento, la rilettura e il confronto
dei digest — la fase 2B — stanno in `test_projection_pg.py`.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from app.inventory import Actor, InventoryRepository, canonical_sha256
from app.inventory import projection
from app.inventory.relational import (
    MAPPER_VERSION,
    ROW_CLASS,
    assemble,
    column_names,
    normalise,
)
from app.inventory.relational_validate import errors, validate_model

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

#: kind → tabella. La corrispondenza sta qui e in nessun altro posto.
TABLE = {"location": "inventory_locations", "room": "inventory_rooms",
         "rack": "inventory_racks", "device": "inventory_devices",
         "manual": "inventory_manual_entries"}

LOC = "aaaaaaaa-0000-4000-8000-0000000000d1"
ROOM = "bbbbbbbb-0000-4000-8000-0000000000d1"
RACK_A = "cccccccc-0000-4000-8000-0000000000da"
RACK_B = "cccccccc-0000-4000-8000-0000000000db"
DEV_A = "dddddddd-0000-4000-8000-0000000000da"
DEV_B = "dddddddd-0000-4000-8000-0000000000db"


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
    with engine.begin() as c:
        # L'ordine è imposto dalle chiavi esterne; `CASCADE` fa il resto.
        c.execute(text("TRUNCATE inventory_locations, inventory_manual_entries "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_projection_state"))
    yield engine


def document(swap: bool = False) -> dict:
    a, b = ("R01", "R02") if not swap else ("R02", "R01")

    def rack(uid, code, x):
        return {"_uid": uid, "id": code, "name": code, "u": 45,
                "x": x, "y": 0.2, "w": 0.4, "h": 0.8, "devices": []}

    doc = {
        "schemaVersion": 1,
        "locations": [{
            "_uid": LOC, "id": "sito", "nome": "Sito",
            "sale": [{
                "_uid": ROOM, "id": "sala", "nome": "Sala", "w": 10, "h": 8,
                "vani": [], "racks": [rack(RACK_A, a, 0.1), rack(RACK_B, b, 0.6)],
            }],
        }],
    }
    doc["locations"][0]["sale"][0]["racks"][0]["devices"] = [
        {"_uid": DEV_A, "id": "srv", "name": "srv-1", "u": 1},
        # STESSO identificativo nello stesso rack: ammesso, vedi il commento sui
        # vincoli nella migrazione 0010.
        {"_uid": DEV_B, "id": "srv", "name": "srv-2", "u": 2},
    ]
    return doc


# ------------------------------------------------------------------ aiuti

def insert_scenario(conn, *, swap: bool = False) -> None:
    """Popola le tabelle a mano. La fase 2B non esiste ancora: qui si costruisce
    solo quanto basta per esercitare i vincoli."""
    a, b = ("R01", "R02") if not swap else ("R02", "R01")
    conn.execute(text("INSERT INTO inventory_locations (uid, code, nome, ordinal) "
                      "VALUES (:u, 'sito', 'Sito', 0)"), {"u": LOC})
    conn.execute(text("INSERT INTO inventory_rooms (uid, location_uid, code, nome, "
                      "ordinal) VALUES (:u, :l, 'sala', 'Sala', 0)"),
                 {"u": ROOM, "l": LOC})
    for uid, code, ordinal in ((RACK_A, a, 0), (RACK_B, b, 1)):
        conn.execute(text("INSERT INTO inventory_racks (uid, room_uid, code, name, "
                          "ordinal) VALUES (:u, :r, :c, :c, :o)"),
                     {"u": uid, "r": ROOM, "c": code, "o": ordinal})


def uid_map(conn, sql: str) -> dict:
    """`{uid come STRINGA: valore}` da una query che seleziona (uid, qualcosa).

    ⚠ Una colonna `uuid` letta con `text()` torna come `uuid.UUID`, non come
    stringa. Il tipo `pg.UUID(as_uuid=False)` della migrazione vale solo quando la
    query passa dai metadati SQLAlchemy della tabella; una query testuale usa il
    comportamento predefinito di psycopg.

    Conta oltre questi test: chi leggerà queste righe nelle fasi 2B/2C/2D deve
    convertire, altrimenti `assemble` metterebbe oggetti `UUID` nel campo `_uid` —
    non serializzabili in JSON e diversi dalla stringa a cui il digest si aspetta di
    corrispondere. Il difetto si manifesterebbe come «il digest non torna», il che
    non fa pensare a un tipo.
    """
    return {str(k): v for k, v in conn.execute(text(sql)).all()}


def columns_of(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(text("""
        SELECT column_name FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = :t
    """), {"t": table}).all()}


def constraint(conn, name: str) -> dict | None:
    row = conn.execute(text("""
        SELECT conname, contype, condeferrable, condeferred,
               pg_get_constraintdef(oid) AS definition
          FROM pg_constraint WHERE conname = :n
    """), {"n": name}).mappings().first()
    return dict(row) if row else None


# ==================================================================
# 1. lo schema e le dataclass non possono divergere
# ==================================================================

@pytest.mark.parametrize("kind", sorted(TABLE))
def test_the_table_columns_match_the_dataclass_fields(engine, kind):
    """⚠ Il test che tiene insieme i due file.

    `FIELD_MAP` e le dataclass vivono in `app/inventory/relational.py`, le colonne
    nella migrazione 0010. Sono lo stesso elenco scritto due volte, e due elenchi
    che nessuno confronta divergono: una colonna aggiunta senza il campo
    corrispondente resterebbe vuota per sempre, e un campo senza colonna farebbe
    fallire l'inserimento in produzione.
    """
    with engine.begin() as c:
        sql_columns = columns_of(c, TABLE[kind])
    assert sql_columns == set(column_names(kind)), {
        "solo in SQL": sorted(sql_columns - set(column_names(kind))),
        "solo nella dataclass": sorted(set(column_names(kind)) - sql_columns),
        "dataclass": ROW_CLASS[kind].__name__,
    }


def a_version(engine) -> int:
    """Una versione vera in testa, e la tabella di stato VUOTA.

    Una riga di stato senza versione o senza digest non significherebbe niente —
    «la proiezione rispecchia... boh» — e l'assenza della RIGA è già il modo di dire
    «non rispecchia nulla». La terza via è impossibile per costruzione (0011), e
    questi test devono quindi partire da una versione che esiste davvero.

    ⚠ Dalla fase 2C il bootstrap scrive ANCHE la proiezione, riga di stato compresa.
    I test che seguono verificano i VINCOLI di quella tabella inserendo righe a
    mano, quindi hanno bisogno di trovarla vuota: senza questa ripulitura il primo
    `insert_state` fallirebbe per conflitto di chiave primaria, cioè per un vincolo
    diverso da quello in prova, e il test passerebbe (o fallirebbe) per il motivo
    sbagliato.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        version = InventoryRepository(c).bootstrap(
            document(), Actor(username="capo", role="admin")).version
    with engine.begin() as c:
        projection.clear(c)
    return version


def insert_state(conn, version: int, *, id_value: str = "TRUE") -> None:
    conn.execute(text(f"""
        INSERT INTO inventory_projection_state (id, head_version, head_sha256)
        VALUES ({id_value}, :v, repeat('f', 64))
    """), {"v": version})


def test_the_state_table_holds_at_most_one_row(db, engine):
    """Due stati di radice sarebbero due risposte alla domanda «quale versione
    rispecchiano le tabelle»."""
    version = a_version(engine)
    with engine.begin() as c:
        insert_state(c, version)
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            insert_state(c, version, id_value="FALSE")
        c.rollback()
    assert "ck_projection_state_singleton" in str(err.value)
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            insert_state(c, version)
        c.rollback()
    assert "inventory_projection_state_pkey" in str(err.value)


@pytest.mark.parametrize("column", ["head_version", "head_sha256"])
def test_a_half_written_state_row_is_impossible(db, engine, column):
    """Una riga che dichiara di rispecchiare una versione senza dire quale, oppure
    senza dire che cosa si è verificato, sarebbe uno stato che nessuno saprebbe
    interpretare."""
    version = a_version(engine)
    values = {"head_version": version, "head_sha256": "f" * 64}
    values[column] = None
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text("INSERT INTO inventory_projection_state "
                           "(id, head_version, head_sha256) "
                           "VALUES (TRUE, :head_version, :head_sha256)"), values)
        c.rollback()
    assert column in str(err.value) and "null" in str(err.value).lower()


def test_the_projection_starts_out_mirroring_nothing(db, engine):
    """Nessuna riga di stato significa «la proiezione non rispecchia nessuna
    versione»: è lo stato in cui la migrazione lascia le tabelle, e la fase 2B è ciò
    che lo cambierà."""
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_projection_state")).scalar_one() == 0
        for table in TABLE.values():
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_truncating_the_versions_also_clears_the_projection_state(db, engine):
    """⚠ Conseguenza voluta della chiave esterna, trovata da un test.

    `inventory_projection_state.head_version` referenzia `inventory_versions`, quindi un
    `TRUNCATE inventory_versions CASCADE` porta via anche lo stato. È coerente con il
    significato — senza versioni non c'è niente da rispecchiare — ed è la ragione per
    cui la migrazione NON semina una riga: la si ritroverebbe misteriosamente
    sparita dopo qualunque ripulitura.
    """
    version = a_version(engine)
    with engine.begin() as c:
        insert_state(c, version)
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_projection_state")).scalar_one() == 0


# ==================================================================
# 2. l'identità è l'_uid, il codice è un attributo
# ==================================================================

def test_the_primary_key_is_the_uid(engine):
    with engine.begin() as c:
        for kind, table in TABLE.items():
            # `CAST(:t AS regclass)` e non `:t::regclass`: `text()` di SQLAlchemy
            # interpreta `:` come inizio di un parametro, e `::` lo manda in errore
            # di sintassi.
            pk = c.execute(text("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                 WHERE contype = 'p' AND conrelid = CAST(:t AS regclass)
            """), {"t": table}).scalar_one()
            assert pk == "PRIMARY KEY (uid)", (kind, pk)


def test_renaming_a_rack_keeps_its_identity(db, engine):
    """Una rinomina cambia il codice e conserva la chiave: è ciò che una chiave
    primaria sul codice renderebbe impossibile, spezzando la storia (§8.4)."""
    with engine.begin() as c:
        insert_scenario(c)
        c.execute(text("UPDATE inventory_racks SET code = 'R01-NUOVO', "
                       "name = 'Rinominato' WHERE uid = :u"), {"u": RACK_A})
        row = c.execute(text("SELECT uid, code FROM inventory_racks WHERE uid = :u"),
                        {"u": RACK_A}).one()
    assert str(row[0]) == RACK_A and row[1] == "R01-NUOVO"


# ==================================================================
# 3. vincoli con ambito, e differibili
# ==================================================================

@pytest.mark.parametrize("name,expected", [
    ("uq_location_code", "UNIQUE (code)"),
    ("uq_room_code", "UNIQUE (location_uid, code)"),
    ("uq_rack_code", "UNIQUE (room_uid, code)"),
    ("uq_manual_code", "UNIQUE (code)"),
    ("uq_location_ordinal", "UNIQUE (ordinal)"),
    ("uq_room_ordinal", "UNIQUE (location_uid, ordinal)"),
    ("uq_rack_ordinal", "UNIQUE (room_uid, ordinal)"),
    ("uq_device_ordinal", "UNIQUE (rack_uid, ordinal)"),
    ("uq_manual_ordinal", "UNIQUE (ordinal)"),
])
def test_the_scoped_constraints_exist_and_are_deferrable(engine, name, expected):
    with engine.begin() as c:
        found = constraint(c, name)
    assert found is not None, f"vincolo {name} assente"
    assert found["definition"].startswith(expected), found["definition"]
    assert found["condeferrable"] is True, "un rinominio valido può collidere a metà"
    # `INITIALLY IMMEDIATE`: il default resta stretto, e un errore che compare
    # sullo statement colpevole è molto più facile da diagnosticare di uno che
    # compare al commit. Chi ha bisogno di differire lo dichiara.
    assert found["condeferred"] is False


def test_a_duplicate_rack_code_in_the_same_room_is_refused(db, engine):
    with engine.connect() as c:
        insert_scenario(c)
        with pytest.raises(Exception) as err:
            c.execute(text("UPDATE inventory_racks SET code = 'R01' WHERE uid = :u"),
                      {"u": RACK_B})
        c.rollback()
    assert "uq_rack_code" in str(err.value)


def test_the_same_rack_code_in_two_different_rooms_is_allowed(db, engine):
    """L'unicità ha un AMBITO: il seed di produzione è pieno di sale che hanno
    entrambe un rack «R01»."""
    other_room = "bbbbbbbb-0000-4000-8000-0000000000d2"
    other_rack = "cccccccc-0000-4000-8000-0000000000dc"
    with engine.begin() as c:
        insert_scenario(c)
        c.execute(text("INSERT INTO inventory_rooms (uid, location_uid, code, nome, "
                       "ordinal) VALUES (:u, :l, 'sala-2', 'Sala 2', 1)"),
                  {"u": other_room, "l": LOC})
        c.execute(text("INSERT INTO inventory_racks (uid, room_uid, code, name, "
                       "ordinal) VALUES (:u, :r, 'R01', 'R01', 0)"),
                  {"u": other_rack, "r": other_room})
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_racks "
                              "WHERE code = 'R01'")).scalar_one() == 2


def test_two_racks_can_swap_their_codes_in_one_transaction(db, engine):
    """⚠ Il caso per cui i vincoli devono essere differibili.

    Scambiare due codici è un'operazione legittima, e a metà transazione i due
    valori collidono. Senza `DEFERRABLE` l'unica via sarebbe un valore di comodo
    intermedio — cioè uno stato che non è mai stato vero, scritto nel database per
    aggirare un vincolo.
    """
    with engine.begin() as c:
        insert_scenario(c)
    with engine.begin() as c:
        c.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        c.execute(text("UPDATE inventory_racks SET code = 'R02' WHERE uid = :u"),
                  {"u": RACK_A})
        c.execute(text("UPDATE inventory_racks SET code = 'R01' WHERE uid = :u"),
                  {"u": RACK_B})
    with engine.begin() as c:
        codes = uid_map(c, "SELECT uid, code FROM inventory_racks")
    assert codes[RACK_A] == "R02" and codes[RACK_B] == "R01"


def test_a_swap_without_deferring_fails_at_the_first_statement(db, engine):
    """La controprova: se il vincolo non fosse `IMMEDIATE` per default, il test
    precedente passerebbe anche senza `SET CONSTRAINTS`, e non dimostrerebbe
    niente."""
    with engine.begin() as c:
        insert_scenario(c)
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text("UPDATE inventory_racks SET code = 'R02' WHERE uid = :u"),
                      {"u": RACK_A})
        c.rollback()
    assert "uq_rack_code" in str(err.value)


def test_two_racks_can_swap_their_ordinals(db, engine):
    """Un riordino è un evento di dominio (§8.10) e passa da qui: gli ordinali si
    scambiano, e a metà transazione due rack condividono una posizione."""
    with engine.begin() as c:
        insert_scenario(c)
    with engine.begin() as c:
        c.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        c.execute(text("UPDATE inventory_racks SET ordinal = 1 WHERE uid = :u"),
                  {"u": RACK_A})
        c.execute(text("UPDATE inventory_racks SET ordinal = 0 WHERE uid = :u"),
                  {"u": RACK_B})
    with engine.begin() as c:
        order = uid_map(c, "SELECT uid, ordinal FROM inventory_racks")
    assert order[RACK_A] == 1 and order[RACK_B] == 0


def test_two_devices_with_the_same_code_in_one_rack_are_accepted(db, engine):
    """NESSUN vincolo su (rack_uid, code), e non è una dimenticanza: l'import
    tabellare produce identificativi ripetuti, il validatore di identità li tollera
    da sempre, e vincolarli farebbe rifiutare alla fase 2C documenti che la fase 1
    accetta."""
    with engine.begin() as c:
        insert_scenario(c)
        for uid, ordinal in ((DEV_A, 0), (DEV_B, 1)):
            c.execute(text("INSERT INTO inventory_devices (uid, rack_uid, code, "
                           "name, ordinal) VALUES (:u, :r, 'srv', 'srv', :o)"),
                      {"u": uid, "r": RACK_A, "o": ordinal})
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_devices "
                              "WHERE code = 'srv'")).scalar_one() == 2
        assert constraint(c, "uq_device_code") is None


def test_a_negative_ordinal_is_refused(db, engine):
    with engine.connect() as c:
        insert_scenario(c)
        with pytest.raises(Exception) as err:
            c.execute(text("UPDATE inventory_racks SET ordinal = -1 WHERE uid = :u"),
                      {"u": RACK_A})
        c.rollback()
    assert "ck_rack_ordinal" in str(err.value)


# ==================================================================
# 3-bis. le colonne data DERIVATE (0011)
# ==================================================================

@pytest.mark.parametrize("column,source", [("garanzia_date", "garanzia"),
                                           ("supporto_date", "supporto")])
def test_the_derived_date_columns_are_dates_and_the_text_stays_text(engine,
                                                                   column, source):
    """Il testo resta testo, e la data interpretata gli sta accanto.

    Cambiare il tipo di `garanzia` avrebbe costretto a scartare «in attesa» e le
    date malformate, cioè a perdere il dato per farlo entrare in un tipo (§8.42).
    """
    with engine.begin() as c:
        types = {r[0]: r[1] for r in c.execute(text("""
            SELECT column_name, data_type FROM information_schema.columns
             WHERE table_name = 'inventory_devices'
        """)).all()}
    assert types[column] == "date"
    assert types[source] == "text"


@pytest.mark.parametrize("column", ["garanzia_date", "supporto_date"])
def test_a_derived_date_cannot_survive_its_text(db, engine, column):
    """Il solo `CHECK` che si può scrivere senza reimplementare il parser in SQL.

    Non dice QUALE data debba essere — quello lo dice il parser dello scanner
    (§8.41), e un'espressione SQL equivalente sarebbe una seconda idea di «data
    valida» destinata a divergere. Esclude però la deriva più grossa: la colonna
    derivata che sopravvive alla cancellazione dell'originale.
    """
    source = column.removesuffix("_date")
    with engine.begin() as c:
        insert_scenario(c)
        c.execute(text(f"INSERT INTO inventory_devices (uid, rack_uid, code, name, "
                       f"ordinal, {source}, {column}) "
                       f"VALUES (:u, :r, 'srv', 'srv', 0, '2027-03-14', "
                       f"'2027-03-14')"), {"u": DEV_A, "r": RACK_A})
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text(f"UPDATE inventory_devices SET {source} = NULL "
                           f"WHERE uid = :u"), {"u": DEV_A})
        c.rollback()
    assert f"ck_device_{column}_needs_text" in str(err.value)


@pytest.mark.parametrize("column", ["garanzia_date", "supporto_date"])
def test_the_expiry_query_has_a_partial_index(engine, column):
    """La domanda è sempre «quali scadenze cadono fra due date», che implica
    `IS NOT NULL`: nel seed reale la maggior parte dei dispositivi non ha date, e
    indicizzarne i NULL sarebbe indice sprecato."""
    with engine.begin() as c:
        definition = c.execute(text("""
            SELECT indexdef FROM pg_indexes
             WHERE tablename = 'inventory_devices' AND indexname = :n
        """), {"n": f"ix_device_{column}"}).scalar_one()
    assert column in definition
    assert "WHERE" in definition and "NOT NULL" in definition


# ==================================================================
# 4. gerarchia e foto
# ==================================================================

def test_deleting_a_location_cascades_to_its_descendants(db, engine):
    """La proiezione è STATO CORRENTE, non storia: cancellare un sito porta via le
    sue sale e i suoi rack. La storia resta in `inventory_versions`, che è
    append-only (§8.19) e non viene toccata."""
    with engine.begin() as c:
        insert_scenario(c)
        c.execute(text("INSERT INTO inventory_devices (uid, rack_uid, code, name, "
                       "ordinal) VALUES (:u, :r, 'srv', 'srv', 0)"),
                  {"u": DEV_A, "r": RACK_A})
    with engine.begin() as c:
        c.execute(text("DELETE FROM inventory_locations WHERE uid = :u"), {"u": LOC})
    with engine.begin() as c:
        for table in ("inventory_rooms", "inventory_racks", "inventory_devices"):
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_a_rack_cannot_point_at_a_nonexistent_photo(db, engine):
    with engine.connect() as c:
        insert_scenario(c)
        with pytest.raises(Exception) as err:
            c.execute(text("UPDATE inventory_racks SET photo_id = "
                           "'6ba7b810-9dad-41d1-80b4-00c04fd430c8' WHERE uid = :u"),
                      {"u": RACK_A})
        c.rollback()
    assert "photos" in str(err.value)


def test_the_current_photo_also_protects_the_bytes(db, engine):
    """La colonna non ha `ON DELETE`, come `inventory_photo_refs` (§8.5): il
    database rifiuta di cancellare una foto ancora riferita. Se la proiezione e i
    riferimenti storici divergessero, la GC risulterebbe bloccata su una foto
    orfana — un guasto nella direzione sicura."""
    import hashlib
    payload = b"\x89PNG\r\n\x1a\n" + b"finti byte"
    sha = hashlib.sha256(payload).hexdigest()
    with engine.begin() as c:
        photo_id = c.execute(text("""
            INSERT INTO photos (mime, bytes, sha256, size_bytes)
            VALUES ('image/png', :b, :s, :n) RETURNING id
        """), {"b": payload, "s": sha, "n": len(payload)}).scalar_one()
        insert_scenario(c)
        c.execute(text("UPDATE inventory_racks SET photo_id = :p WHERE uid = :u"),
                  {"p": photo_id, "u": RACK_A})
    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text("DELETE FROM photos WHERE id = :p"), {"p": photo_id})
        c.rollback()
    assert "inventory_racks" in str(err.value)
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_racks SET photo_id = NULL"))
        c.execute(text("DELETE FROM photos WHERE id = :p"), {"p": photo_id})


# ==================================================================
# 5. privilegi: dalla fase 2C li ha l'API, e solo l'API
# ==================================================================

PROJECTION_TABLES = sorted(set(TABLE.values()) | {"inventory_projection_state"})


@pytest.mark.parametrize("table", PROJECTION_TABLES)
def test_the_api_role_can_maintain_the_projection(engine, table):
    """⚠ L'aspettativa OPPOSTA a quella della fase 2B, per la stessa tabella.

    La 0010 negava la scrittura a entrambi i ruoli di runtime e scriveva perché: «i
    privilegi di scrittura li concede la fase 2C, con il codice che li usa». Adesso
    quel codice esiste, e questo test è il rovescio di quello che c'era.

    `TRUNCATE` resta però negato, e non è un dettaglio: la sincronizzazione usa
    `DELETE` di proposito, per non prendere un lock ACCESS EXCLUSIVE che bloccherebbe
    anche i lettori della fase 2D. Un privilegio che non serve è un privilegio che
    può essere sfruttato (§8.19).
    """
    with engine.begin() as c:
        for privilege, expected in (("SELECT", True), ("INSERT", True),
                                    ("UPDATE", True), ("DELETE", True),
                                    ("TRUNCATE", False)):
            got = c.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": "tsm_api", "t": table, "p": privilege}).scalar_one()
            assert got is expected, f"tsm_api {privilege} {table} = {got}"


@pytest.mark.parametrize("table", PROJECTION_TABLES)
def test_the_worker_role_still_cannot_write_the_projection(engine, table):
    """Il worker legge e non scrive: le colonne data derivate esistono per le query,
    e il passaggio dello scanner è una decisione successiva (§8.44). Concedergli la
    scrittura adesso sarebbe un privilegio senza codice che lo usa."""
    with engine.begin() as c:
        for privilege, expected in (("SELECT", True), ("INSERT", False),
                                    ("UPDATE", False), ("DELETE", False),
                                    ("TRUNCATE", False)):
            got = c.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                            {"r": "tsm_worker", "t": table,
                             "p": privilege}).scalar_one()
            assert got is expected, f"tsm_worker {privilege} {table} = {got}"


# ==================================================================
# 6. GET e PUT non sono cambiati
# ==================================================================

def test_a_real_save_now_maintains_the_projection(db, engine):
    """⚠ L'affermazione centrale della fase 2C, ed è l'OPPOSTO di quella della 2B.

    Il test che stava qui si chiamava `..._does_not_touch_the_projection` e
    pretendeva tabelle vuote dopo un salvataggio, perché in 2A/2B un `PUT` che le
    avesse popolate avrebbe voluto dire che il comportamento era cambiato senza la
    transazione unica, senza i riferimenti alle foto e senza il confronto dei digest.

    Adesso quelle tre cose ci sono, quindi l'aspettativa si capovolge: la storia si
    scrive E la proiezione la segue, nella stessa transazione. Si è conservato lo
    scenario — un salvataggio con `swap=True`, cioè uno scambio di codici ambito, che
    è il caso in cui una sincronizzazione incrementale si romperebbe sull'unicità.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(document(),
                                         Actor(username="capo", role="admin"))
    with engine.begin() as c:
        repo = InventoryRepository(c)
        head = repo.get_current()
        repo.save(head.version, document(swap=True),
                  Actor(username="capo", role="admin"))

    with engine.begin() as c:
        # La storia è stata scritta...
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one() == 2
        # ...e la proiezione NON è vuota.
        for table in TABLE.values():
            n = c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            if table != "inventory_manual_entries":       # il documento non ne ha
                assert n > 0, table
        # E lo stato dichiara la versione 2, non la 1.
        state = c.execute(text("SELECT head_version, mapper_version "
                               "  FROM inventory_projection_state")).one()
        assert state[0] == 2
        assert state[1] == MAPPER_VERSION

    # E il giro torna: le tabelle riassemblano esattamente la testa.
    with engine.begin() as c:
        assert projection.verify(c).ok


def test_the_mapper_round_trips_the_document_stored_in_the_database(db, engine):
    """Il giro completo sul documento VERO, letto dal database.

    Sono i passi 1, 3, 4 e 5 della fase 2B — meno il popolamento, che questo commit
    non fa. Serve a sapere ADESSO se la mappa regge sul documento che c'è davvero,
    invece di scoprirlo durante una migrazione.
    """
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(document(),
                                         Actor(username="capo", role="admin"))
    with engine.begin() as c:
        snapshot = InventoryRepository(c).get_current()

    model = normalise(snapshot.doc)
    assert errors(validate_model(model)) == []
    assert canonical_sha256(assemble(model)) == canonical_sha256(snapshot.doc)


def test_the_mapper_round_trips_the_production_seed_from_the_database(db, engine):
    """Lo stesso, sul seed di PRODUZIONE: 3 siti, 6 sale, 102 rack, 86 dispositivi.

    Il documento passa dal database — quindi attraverso la serializzazione JSONB di
    PostgreSQL, che è dove un numero può cambiare forma. Provarlo solo in memoria
    lascerebbe fuori proprio il passaggio che la fase 2B compie.
    """
    from pathlib import Path
    from app.inventory.document import strip_legacy_fields
    seed_path = Path(__file__).resolve().parents[2] / "fixtures" / "seed.json"
    with seed_path.open(encoding="utf-8") as fh:
        seed = strip_legacy_fields(json.load(fh))[0]

    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(seed, Actor(username="capo", role="admin"))
    with engine.begin() as c:
        snapshot = InventoryRepository(c).get_current()

    model = normalise(snapshot.doc)
    found = validate_model(model)
    assert errors(found) == [], [f.as_dict() for f in errors(found)]
    assert model.counts() == {"locations": 3, "rooms": 6, "racks": 102,
                              "devices": 86, "manual": 0}
    assert canonical_sha256(assemble(model)) == canonical_sha256(snapshot.doc)


def test_the_expiry_fixture_round_trips_through_the_database(db, engine):
    """L'inventario delle scadenze contiene nomi ostili con `\\r\\n`, date rotte e
    campi assenti: tutto ciò che un documento reale ha e una fixture pulita no."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "fixtures" / "expiry" / "build.py"
    spec = importlib.util.spec_from_file_location("tsm_fixture_expiry_pg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = module.build_inventory(date(2026, 8, 10))

    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(doc, Actor(username="capo", role="admin"))
    with engine.begin() as c:
        snapshot = InventoryRepository(c).get_current()

    model = normalise(snapshot.doc)
    assert errors(validate_model(model)) == []
    assert canonical_sha256(assemble(model)) == canonical_sha256(snapshot.doc)
