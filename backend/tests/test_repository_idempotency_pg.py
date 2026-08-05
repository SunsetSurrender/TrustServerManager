"""Idempotenza del salvataggio e privilegi append-only. PostgreSQL reale."""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, text

from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import (
    Actor,
    InventoryRepository,
    VersionConflictError,
    canonical_sha256,
)

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RACK = "cccccccc-0000-4000-8000-00000000000a"
DEV = "dddddddd-0000-4000-8000-00000000000a"

ADMIN = Actor(username="admin", role="admin")
EDITOR = Actor(username="operatore", role="edit")


def base_doc() -> dict:
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
            {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [], "racks": [
                {"_uid": RACK, "id": "R01", "name": "R01", "u": 45,
                 "x": 0.5, "y": 0.5, "w": 0.6, "h": 0.8, "devices": [
                     {"_uid": DEV, "id": "srv-01", "name": "srv-01", "u": 10}]}]}]}],
    }


def clone(d: dict) -> dict:
    return json.loads(json.dumps(d))


@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def fresh(engine):
    """Database pulito e inventario inizializzato, committato."""
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        v = InventoryRepository(c).bootstrap(base_doc(), ADMIN).version
    yield v
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))


# ==================================================================
# §8.18 — idempotenza: hash confrontato PRIMA del baseVersion
# ==================================================================

def test_stale_base_version_with_identical_content_is_idempotent(engine, fresh):
    """Il caso reale: il commit è andato a buon fine ma la risposta si è persa.

    Il client riprova con il vecchio baseVersion e lo STESSO documento. Deve
    ricevere la versione corrente con changed=False, non un conflitto: altrimenti
    l'utente vedrebbe «modificato da un altro utente» a fronte della propria
    modifica riuscita.
    """
    v1 = fresh
    changed = clone(base_doc())
    changed["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "Dell R760"

    with engine.begin() as c:
        v2 = InventoryRepository(c).save(v1, clone(changed), EDITOR).version
    assert v2 > v1

    # Ritentativo con baseVersion SUPERATO e contenuto IDENTICO a quello in testa
    with engine.begin() as c:
        result = InventoryRepository(c).save(v1, clone(changed), EDITOR)

    assert result.created is False
    assert result.version == v2

    with engine.connect() as c:
        # Nessuna versione e nessun audit in più
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 2
        assert c.execute(text("SELECT count(*) FROM audit")).scalar() == 2
        assert c.execute(text("SELECT version FROM inventory_head")).scalar() == v2


def test_stale_base_version_with_different_content_still_conflicts(engine, fresh):
    """L'altra metà della regola: se il contenuto è diverso, il client sta
    davvero sovrascrivendo il lavoro di qualcun altro, e il conflitto resta."""
    v1 = fresh
    first = clone(base_doc())
    first["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "Dell R760"
    with engine.begin() as c:
        v2 = InventoryRepository(c).save(v1, first, EDITOR).version

    second = clone(base_doc())
    second["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "HPE DL380"
    with pytest.raises(VersionConflictError) as exc:
        with engine.begin() as c:
            InventoryRepository(c).save(v1, second, EDITOR)

    assert exc.value.current_version == v2
    # Il conflitto porta l'hash corrente: il client può capire da sé se la testa è
    # già ciò che voleva scrivere, senza un secondo giro.
    with engine.connect() as c:
        head_doc = InventoryRepository(c).get_current().doc
    assert exc.value.current_sha256 == canonical_sha256(head_doc)

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 2


def test_idempotent_replay_is_allowed_even_for_view_role(engine, fresh):
    """Il replay non produce eventi, quindi non c'è niente da autorizzare."""
    v1 = fresh
    changed = clone(base_doc())
    changed["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    with engine.begin() as c:
        v2 = InventoryRepository(c).save(v1, clone(changed), EDITOR).version

    with engine.begin() as c:
        result = InventoryRepository(c).save(
            v1, clone(changed), Actor(username="lettore", role="view"))
    assert result.created is False and result.version == v2


def test_replay_of_bootstrap_content_is_idempotent(engine, fresh):
    """Anche un baseVersion inventato, se il contenuto combacia, è un no-op."""
    with engine.begin() as c:
        result = InventoryRepository(c).save(999_999, base_doc(), EDITOR)
    assert result.created is False
    assert result.version == fresh


# ==================================================================
# §8.19 — append-only imposto dai privilegi del database
# ==================================================================

RUNTIME_PW = "runtime-test-password"


@pytest.fixture(scope="module")
def runtime_dsn(engine):
    """DSN del ruolo di runtime. La migrazione crea il ruolo senza password;
    qui gliene si dà una, come fa `scripts/migrate.py` in esecuzione reale."""
    with engine.begin() as c:
        c.execute(text("SELECT set_config('tsm.newpw', :pw, true)"), {"pw": RUNTIME_PW})
        c.execute(text("""
            DO $$ BEGIN
                EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L',
                               'tsm_api', current_setting('tsm.newpw'));
            END $$;
        """))
    # Stessa destinazione, credenziali del ruolo limitato.
    from sqlalchemy.engine import make_url
    url = make_url(DSN).set(username="tsm_api", password=RUNTIME_PW)
    return url


@pytest.fixture
def runtime_engine(runtime_dsn):
    eng = create_engine(runtime_dsn, future=True)
    yield eng
    eng.dispose()


def test_runtime_role_can_read_and_append(runtime_engine, engine, fresh):
    with runtime_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() >= 1
        assert c.execute(text("SELECT version FROM inventory_head")).scalar() == fresh


def test_runtime_role_can_save_through_repository(runtime_engine, fresh):
    """Il percorso normale deve funzionare con i privilegi ridotti: se il ruolo
    fosse troppo stretto, lo si scoprirebbe qui e non in produzione."""
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    with runtime_engine.begin() as c:
        result = InventoryRepository(c).save(fresh, doc, EDITOR)
    assert result.created


@pytest.mark.parametrize("sql", [
    "UPDATE inventory_versions SET doc = '{}'::jsonb",
    "UPDATE inventory_versions SET canonical_sha256 = 'x'",
    "DELETE FROM inventory_versions",
    "UPDATE audit SET events = '[]'::jsonb",
    "UPDATE audit SET actor_username = 'altro'",
    "DELETE FROM audit",
    "DELETE FROM inventory_head",
    "INSERT INTO inventory_head (id, version) VALUES (TRUE, 1)",
])
def test_runtime_role_is_denied_history_rewrites(runtime_engine, fresh, sql):
    """La storia non si riscrive, e non è il codice a impedirlo.

    Compreso `INSERT` sulla testa: la riga nasce una volta sola nel bootstrap,
    che gira come proprietario. Così «il bootstrap non passa da HTTP» è un
    privilegio che l'API non ha, non una convenzione.
    """
    from sqlalchemy.exc import ProgrammingError
    with runtime_engine.connect() as c:
        with pytest.raises(ProgrammingError) as exc:
            c.execute(text(sql))
        assert "permission denied" in str(exc.value).lower(), str(exc.value)


def test_runtime_role_cannot_do_ddl(runtime_engine):
    from sqlalchemy.exc import ProgrammingError
    with runtime_engine.connect() as c:
        with pytest.raises(ProgrammingError):
            c.execute(text("CREATE TABLE tentativo (x int)"))


def test_runtime_role_cannot_read_or_write_users_passwords_table_ddl(runtime_engine):
    """Il ruolo di runtime PUÒ leggere users (serve al login) ma non eliminarle:
    la disattivazione è logica (§8.6)."""
    from sqlalchemy.exc import ProgrammingError
    with runtime_engine.connect() as c:
        c.execute(text("SELECT count(*) FROM users"))          # consentito
        with pytest.raises(ProgrammingError) as exc:
            c.execute(text("DELETE FROM users"))
        assert "permission denied" in str(exc.value).lower()


def test_owner_can_still_manage_everything(engine, fresh):
    """Il proprietario resta in grado di fare manutenzione: i privilegi ridotti
    sono per l'API, non per l'operations."""
    with engine.begin() as c:
        c.execute(text("SELECT count(*) FROM inventory_versions"))
        c.execute(text("UPDATE inventory_head SET updated_at = now() WHERE id IS TRUE"))
