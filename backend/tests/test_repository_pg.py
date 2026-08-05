"""Repository dell'inventario — test di integrazione su PostgreSQL REALE.

Non ci sono doppi: il comportamento che conta qui (`SELECT ... FOR UPDATE`,
identity bigint, atomicità del rollback) è comportamento del database, e un
finto non lo dimostrerebbe.

Si saltano se `TSM_DB_URL` non è impostata, così la suite pura resta eseguibile
da sola. Vedi backend/README.md per il comando.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.authz import UNSUPPORTED_DOMAIN_EVENT
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import (
    Actor,
    AlreadyBootstrappedError,
    DocumentRejectedError,
    IdentityRejectedError,
    InventoryRepository,
    MAX_CLIENT_HINT_CHARS,
    NotAuthorizedError,
    NotBootstrappedError,
    VersionConflictError,
    canonical_sha256,
)

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata: test PG saltati")

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RACK_A = "cccccccc-0000-4000-8000-00000000000a"
RACK_B = "cccccccc-0000-4000-8000-00000000000b"
DEV_A = "dddddddd-0000-4000-8000-00000000000a"
DEV_B = "dddddddd-0000-4000-8000-00000000000b"

ADMIN = Actor(username="admin", role="admin", user_id=None, ip="10.0.0.1")
EDITOR = Actor(username="operatore", role="edit")
VIEWER = Actor(username="lettore", role="view")


def base_doc() -> dict:
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
            {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [], "racks": [
                {"_uid": RACK_A, "id": "R01", "name": "R01", "u": 45,
                 "x": 0.5, "y": 0.5, "w": 0.6, "h": 0.8, "devices": [
                     {"_uid": DEV_A, "id": "srv-01", "name": "srv-01", "u": 10},
                     {"_uid": DEV_B, "id": "srv-02", "name": "srv-02", "u": 20}]},
                {"_uid": RACK_B, "id": "R02", "name": "R02", "u": 45,
                 "x": 1.5, "y": 0.5, "w": 0.6, "h": 0.8, "devices": []}]}]}],
    }


def clone(d: dict) -> dict:
    return json.loads(json.dumps(d))


# ------------------------------------------------------------------- fixture

@pytest.fixture(scope="session")
def engine():
    eng = create_engine(DSN, future=True)
    # Lo schema lo crea Alembic: così il test dimostra anche che la migrazione
    # funziona, invece di verificare tabelle create a mano che potrebbero
    # divergere da quelle reali.
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    """Connessione con transazione annullata alla fine: ogni test parte pulito."""
    with engine.connect() as c:
        trans = c.begin()
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        yield c
        trans.rollback()


@pytest.fixture
def repo(conn):
    return InventoryRepository(conn)


@pytest.fixture
def bootstrapped(repo):
    result = repo.bootstrap(base_doc(), ADMIN)
    return result.version


# --------------------------------------------------------------- bootstrap

def test_bootstrap_creates_version_and_head(repo, conn):
    result = repo.bootstrap(base_doc(), ADMIN)
    assert result.created
    assert result.version > 0
    assert repo.head_version() == result.version
    assert repo.get_current().version == result.version

    row = conn.execute(text(
        "SELECT actor_username, actor_role, canonical_sha256 FROM inventory_versions"
    )).one()
    assert row[0] == "admin" and row[1] == "admin"
    assert len(row[2]) == 64


def test_bootstrap_is_one_time_only(repo, bootstrapped):
    with pytest.raises(AlreadyBootstrappedError):
        repo.bootstrap(base_doc(), ADMIN)


def test_bootstrap_writes_exactly_one_head_row(repo, conn, bootstrapped):
    assert conn.execute(text("SELECT count(*) FROM inventory_head")).scalar() == 1


def test_head_singleton_cannot_be_duplicated(repo, conn, bootstrapped):
    """Il vincolo è nel database, non solo nel codice."""
    with pytest.raises(Exception):
        conn.execute(text("INSERT INTO inventory_head (id, version) VALUES (TRUE, :v)"),
                     {"v": bootstrapped})


def test_reads_fail_before_bootstrap(repo):
    assert repo.head_version() is None
    with pytest.raises(NotBootstrappedError):
        repo.get_current()


def test_save_before_bootstrap_fails(repo):
    with pytest.raises(NotBootstrappedError):
        repo.save(1, base_doc(), ADMIN)


def test_bootstrap_from_legacy_strips_extracted_roots(repo, conn):
    legacy = base_doc()
    legacy.update({"utenti": [{"email": "admin", "password": "admin"}],
                   "registro": [], "smtp": {"password": "p"}, "versione": 3})
    result = repo.bootstrap(legacy, ADMIN, from_legacy=True)
    stored = repo.get_current().doc
    for k in ("utenti", "registro", "smtp", "versione"):
        assert k not in stored
    assert "password" not in json.dumps(stored)
    assert result.created


def test_bootstrap_rejects_legacy_without_the_flag(repo):
    legacy = base_doc()
    legacy["utenti"] = []
    with pytest.raises(DocumentRejectedError):
        repo.bootstrap(legacy, ADMIN)


# ------------------------------------------------------------------ salvataggi

def test_save_creates_new_version_and_moves_head(repo, bootstrapped):
    nxt = clone(base_doc())
    nxt["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "Dell R760"
    result = repo.save(bootstrapped, nxt, EDITOR)
    assert result.created
    assert result.version > bootstrapped
    assert repo.head_version() == result.version
    assert [e["event"] for e in result.events] == ["update"]
    assert result.scopes == ("devices",)


def test_versions_are_append_only(repo, conn, bootstrapped):
    nxt = clone(base_doc())
    nxt["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M2"
    v2 = repo.save(bootstrapped, nxt, EDITOR).version
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 2
    # la versione precedente è ancora leggibile e intatta
    assert repo.get_version(bootstrapped).doc["locations"][0]["sale"][0]["racks"][0][
        "devices"][0].get("model", "") == ""
    assert repo.get_version(v2).doc["locations"][0]["sale"][0]["racks"][0][
        "devices"][0]["model"] == "M2"


def test_versions_are_database_generated_and_increasing(repo, bootstrapped):
    seen = [bootstrapped]
    doc = clone(base_doc())
    for i in range(3):
        doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = f"M{i}"
        seen.append(repo.save(seen[-1], clone(doc), EDITOR).version)
    assert seen == sorted(set(seen))


def test_stale_base_version_conflicts(repo, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M1"
    v2 = repo.save(bootstrapped, doc, EDITOR).version

    doc2 = clone(base_doc())
    doc2["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M2"
    with pytest.raises(VersionConflictError) as exc:
        repo.save(bootstrapped, doc2, EDITOR)      # baseVersion superata
    assert exc.value.current_version == v2
    assert repo.head_version() == v2


# ---------------------------------------------------------------- no-op

def test_canonical_noop_returns_current_version_without_history(repo, conn, bootstrapped):
    result = repo.save(bootstrapped, base_doc(), EDITOR)
    assert result.created is False
    assert result.version == bootstrapped
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1
    assert conn.execute(text("SELECT count(*) FROM audit")).scalar() == 1   # solo il bootstrap
    assert repo.head_version() == bootstrapped


def test_noop_when_only_defaults_are_materialised(repo, conn, bootstrapped):
    """Un client che scrive esplicitamente i default non sta cambiando nulla
    (§8.14): non deve creare una versione."""
    explicit = clone(base_doc())
    for d in explicit["locations"][0]["sale"][0]["racks"][0]["devices"]:
        d.update({"stato": "attivo", "h": 1, "type": "altro", "model": "", "ip": "",
                  "serial": "", "owner": "", "garanzia": "", "supporto": "", "note": ""})
    result = repo.save(bootstrapped, explicit, EDITOR)
    assert result.created is False
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1


def test_noop_allowed_even_for_view_role(repo, bootstrapped):
    """Nessun evento, quindi niente da autorizzare (§8.15)."""
    result = repo.save(bootstrapped, base_doc(), VIEWER)
    assert result.created is False


# -------------------------------------------------------- autorizzazione

def test_view_cannot_write(repo, conn, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    with pytest.raises(NotAuthorizedError) as exc:
        repo.save(bootstrapped, doc, VIEWER)
    assert exc.value.details[0]["requiredRole"] == "edit"
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1
    assert repo.head_version() == bootstrapped


def test_edit_cannot_touch_structure(repo, conn, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["id"] = "R01-bis"
    with pytest.raises(NotAuthorizedError):
        repo.save(bootstrapped, doc, EDITOR)
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1


def test_edit_cascade_delete_is_rejected_whole(repo, conn, bootstrapped):
    """Eliminare un rack cancella anche i suoi dispositivi: i delete di
    dispositivo sarebbero concessi, quello del rack no, e la modifica intera
    viene respinta."""
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"] = [
        r for r in doc["locations"][0]["sale"][0]["racks"] if r["_uid"] != RACK_A]
    with pytest.raises(NotAuthorizedError) as exc:
        repo.save(bootstrapped, doc, EDITOR)
    assert any(v["entity"] == "rack" for v in exc.value.details)
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1


def test_admin_can_do_the_same_cascade(repo, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"] = [
        r for r in doc["locations"][0]["sale"][0]["racks"] if r["_uid"] != RACK_A]
    result = repo.save(bootstrapped, doc, ADMIN)
    assert result.created
    assert {e["entity"] for e in result.events} == {"rack", "device"}


def test_edit_can_move_device_between_racks(repo, bootstrapped):
    doc = clone(base_doc())
    racks = doc["locations"][0]["sale"][0]["racks"]
    dev = [d for d in racks[0]["devices"] if d["_uid"] == DEV_A][0]
    racks[0]["devices"] = [d for d in racks[0]["devices"] if d["_uid"] != DEV_A]
    racks[1]["devices"].append(dev)
    result = repo.save(bootstrapped, doc, EDITOR)
    assert [e["event"] for e in result.events] == ["move"]


# --------------------------------------------------------------- identità

def test_identity_replacement_rejected(repo, conn, bootstrapped):
    doc = clone(base_doc())
    racks = doc["locations"][0]["sale"][0]["racks"]
    racks[0]["devices"] = [d for d in racks[0]["devices"] if d["_uid"] != DEV_A]
    racks[0]["devices"].append({"_uid": str(uuid.uuid4()), "id": "srv-01",
                                "name": "srv-01", "u": 10})
    with pytest.raises(IdentityRejectedError) as exc:
        repo.save(bootstrapped, doc, ADMIN)
    assert exc.value.details[0]["code"] == "identity_replacement"
    assert conn.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1


def test_missing_uid_rejected_as_document_error(repo, bootstrapped):
    doc = clone(base_doc())
    del doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["_uid"]
    with pytest.raises(DocumentRejectedError):
        repo.save(bootstrapped, doc, ADMIN)


def test_forbidden_roots_rejected_on_save(repo, bootstrapped):
    doc = clone(base_doc())
    doc["utenti"] = [{"email": "x"}]
    with pytest.raises(DocumentRejectedError) as exc:
        repo.save(bootstrapped, doc, ADMIN)
    assert exc.value.details[0]["code"] == "forbidden_root_key"


def test_client_cannot_change_schema_version_on_save(repo, bootstrapped):
    doc = clone(base_doc())
    doc["schemaVersion"] = CURRENT_SCHEMA_VERSION + 1
    with pytest.raises(DocumentRejectedError):
        repo.save(bootstrapped, doc, ADMIN)


# ------------------------------------------------------------------- audit

def test_audit_written_in_same_transaction_with_server_events(repo, conn, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    version = repo.save(bootstrapped, doc, EDITOR,
                        client_hint="Modificato srv-01").version

    row = conn.execute(text("""
        SELECT actor_username, actor_role, inventory_version, action, scopes,
               events, client_hint
          FROM audit WHERE inventory_version = :v
    """), {"v": version}).one()
    assert row[0] == "operatore" and row[1] == "edit"
    assert row[2] == version
    assert row[3] == "inventory.save"
    assert row[4] == ["devices"]
    events = row[5]
    assert [e["event"] for e in events] == ["update"]      # calcolati dal server
    assert row[6] == "Modificato srv-01"                    # solo testo di comodo


def test_client_hint_is_length_limited(repo, conn, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    version = repo.save(bootstrapped, doc, EDITOR, client_hint="x" * 10_000).version
    stored = conn.execute(text("SELECT client_hint FROM audit WHERE inventory_version = :v"),
                          {"v": version}).scalar()
    assert len(stored) <= MAX_CLIENT_HINT_CHARS


def test_actor_snapshot_is_stored_not_referenced(repo, conn, bootstrapped):
    """username e ruolo sono istantanee: l'audit deve dire chi era quella persona
    allora, anche dopo una disattivazione o un cambio di ruolo (§8.6)."""
    uid = uuid.uuid4()
    actor = Actor(username="mario.rossi", role="admin", user_id=uid)
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["id"] = "R01-bis"
    version = repo.save(bootstrapped, doc, actor).version
    row = conn.execute(text("""
        SELECT actor_username, actor_role, actor_user_id
          FROM inventory_versions WHERE version = :v"""), {"v": version}).one()
    assert row[0] == "mario.rossi" and row[1] == "admin" and row[2] == uid


def test_actor_user_id_is_optional(repo, conn, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    version = repo.save(bootstrapped, doc, EDITOR).version
    assert conn.execute(text(
        "SELECT actor_user_id FROM inventory_versions WHERE version = :v"),
        {"v": version}).scalar() is None


# ------------------------------------------------------- guasti iniettati

def test_failure_at_audit_insert_leaves_nothing_behind(engine, monkeypatch):
    """Se l'audit fallisce non deve sopravvivere la versione: una modifica non
    tracciata è esattamente il buco che spostare l'audit sul server ha chiuso."""
    with engine.connect() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        c.commit()

    with engine.begin() as c:
        repo = InventoryRepository(c)
        v1 = repo.bootstrap(base_doc(), ADMIN).version

    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"

    with pytest.raises(RuntimeError, match="guasto audit"):
        with engine.begin() as c:
            repo = InventoryRepository(c)
            monkeypatch.setattr(repo, "_insert_audit",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("guasto audit")))
            repo.save(v1, doc, EDITOR)

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM audit")).scalar() == 1
        assert c.execute(text("SELECT version FROM inventory_head")).scalar() == v1


def test_failure_at_head_update_leaves_nothing_behind(engine, monkeypatch):
    """Se l'aggiornamento della testa fallisce non devono sopravvivere né la
    versione né l'audit: sarebbero un registro che racconta una modifica mai
    avvenuta."""
    with engine.connect() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        c.commit()

    with engine.begin() as c:
        v1 = InventoryRepository(c).bootstrap(base_doc(), ADMIN).version

    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"

    with pytest.raises(RuntimeError, match="guasto head"):
        with engine.begin() as c:
            repo = InventoryRepository(c)
            monkeypatch.setattr(repo, "_update_head",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("guasto head")))
            repo.save(v1, doc, EDITOR)

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM audit")).scalar() == 1
        assert c.execute(text("SELECT version FROM inventory_head")).scalar() == v1


# -------------------------------------------------------------- concorrenza

def test_concurrent_writers_same_base_version(engine):
    """Due scritture con lo stesso baseVersion: una vince, l'altra ottiene un
    conflitto pulito. Il `SELECT ... FOR UPDATE` sulla riga di testa fa aspettare
    il secondo, che poi rilegge la testa aggiornata — invece di scoprire il
    problema come violazione di chiave primaria, cioè un 500 travestito."""
    with engine.connect() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        c.commit()
    with engine.begin() as c:
        v1 = InventoryRepository(c).bootstrap(base_doc(), ADMIN).version

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    lock = threading.Lock()

    def writer(model: str):
        doc = clone(base_doc())
        doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = model
        try:
            with engine.begin() as c:
                repo = InventoryRepository(c)
                barrier.wait(timeout=10)          # entrambi partono insieme
                result = repo.save(v1, doc, EDITOR)
            with lock:
                outcomes.append(("ok", result.version))
        except VersionConflictError as exc:
            with lock:
                outcomes.append(("conflict", exc.current_version))
        except Exception as exc:                   # pragma: no cover
            with lock:
                outcomes.append(("error", repr(exc)))

    threads = [threading.Thread(target=writer, args=(m,)) for m in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["conflict", "ok"], outcomes

    with engine.connect() as c:
        # esattamente una versione nuova, e la testa la punta
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 2
        head = c.execute(text("SELECT version FROM inventory_head")).scalar()
        winner = [v for k, v in outcomes if k == "ok"][0]
        assert head == winner
        assert c.execute(text(
            "SELECT count(*) FROM audit WHERE inventory_version = :v"),
            {"v": winner}).scalar() == 1


def test_sequential_writers_both_succeed_with_fresh_base(engine):
    with engine.connect() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        c.commit()
    with engine.begin() as c:
        v = InventoryRepository(c).bootstrap(base_doc(), ADMIN).version

    for model in ("A", "B", "C"):
        doc = clone(base_doc())
        doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = model
        with engine.begin() as c:
            v = InventoryRepository(c).save(v, doc, EDITOR).version

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 4
        assert c.execute(text("SELECT version FROM inventory_head")).scalar() == v


# --------------------------------------------------- letture e hash canonico

def test_current_read_uses_head_not_max_version(engine):
    """Inserire a mano una versione più alta senza spostare la testa: la lettura
    corrente deve continuare a restituire quella in testa."""
    with engine.connect() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY"))
        c.commit()
    with engine.begin() as c:
        repo = InventoryRepository(c)
        v1 = repo.bootstrap(base_doc(), ADMIN).version

    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO inventory_versions (doc, canonical_sha256, actor_username, actor_role)
            VALUES (:doc, :sha, 'fuori-banda', 'admin')
        """), {"doc": json.dumps(base_doc()), "sha": "0" * 64})

    with engine.connect() as c:
        repo = InventoryRepository(c)
        assert repo.head_version() == v1
        assert repo.get_current().version == v1
        assert c.execute(text("SELECT max(version) FROM inventory_versions")).scalar() > v1


def test_canonical_sha_is_stable_and_ignores_uids():
    a = base_doc()
    b = clone(a)
    b["locations"][0]["sale"][0]["racks"][0]["devices"][0]["_uid"] = str(uuid.uuid4())
    assert canonical_sha256(a) == canonical_sha256(b)

    c = clone(a)
    c["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    assert canonical_sha256(a) != canonical_sha256(c)


def test_canonical_sha_recorded_matches_recomputed(repo, conn, bootstrapped):
    stored = conn.execute(text(
        "SELECT doc, canonical_sha256 FROM inventory_versions WHERE version = :v"),
        {"v": bootstrapped}).one()
    assert canonical_sha256(stored[0]) == stored[1]


def test_list_versions_joins_audit(repo, bootstrapped):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    repo.save(bootstrapped, doc, EDITOR, client_hint="ciao")
    rows = repo.list_versions()
    assert len(rows) == 2
    assert rows[0]["client_hint"] == "ciao"
    assert rows[0]["scopes"] == ["devices"]
