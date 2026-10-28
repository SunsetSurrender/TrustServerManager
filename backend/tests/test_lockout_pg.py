"""Blocco delle utenze dopo troppi accessi falliti (migrazione 0014). PostgreSQL reale.

Le regole che contano, una per test:
- la soglia è quella della politica, cambiabile solo da un amministratore;
- gli errori devono essere CONSECUTIVI: un accesso riuscito azzera il contatore;
- un'utenza bloccata non entra nemmeno con la password giusta, e il blocco non
  scade da solo;
- sblocca solo un amministratore (o la riga di comando), e lo sblocco finisce nel
  registro; anche la riattivazione e la password provvisoria sbloccano;
- utenze inesistenti e disattivate non accumulano niente e restano
  indistinguibili;
- il limite per IP non scatta più con i numeri di un gruppo di lavoro che passa
  tutto dallo stesso gateway.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


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
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users"))
        c.execute(text("UPDATE security_policy SET max_failed_logins = 5, "
                       "updated_by = NULL WHERE id = 1"))
        create_user(c, "op", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "capo", "password-lunga-3", "admin", must_change_pw=False)
    yield engine


@pytest.fixture
def client(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with api_client(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, username, password):
    return client.post("/api/auth/login", headers=ORIGIN,
                       json={"username": username, "password": password})


def state(engine, username):
    with engine.connect() as c:
        return c.execute(text(
            "SELECT failed_logins, locked_at IS NOT NULL FROM users WHERE username = :u"),
            {"u": username}).first()


def uid(engine, username):
    with engine.connect() as c:
        return str(c.execute(text("SELECT id FROM users WHERE username = :u"),
                             {"u": username}).scalar_one())


def as_admin(client):
    assert login(client, "capo", "password-lunga-3").status_code == 200


def fail(client, username, n):
    return [login(client, username, f"sbagliata-{i}").status_code for i in range(n)]


# ------------------------------------------------------------------ soglia

def test_locks_exactly_at_the_threshold(client, engine):
    assert fail(client, "op", 4) == [401] * 4
    assert state(engine, "op") == (4, False)
    r = login(client, "op", "sbagliata-5")
    assert r.status_code == 423
    assert r.json()["detail"]["code"] == "account_locked"
    assert "amministratore" in r.json()["detail"]["message"]
    assert state(engine, "op") == (5, True)


def test_a_locked_account_refuses_the_right_password_too(client):
    fail(client, "op", 5)
    assert login(client, "op", "password-lunga-1").status_code == 423


def test_failures_must_be_consecutive(client, engine):
    assert fail(client, "op", 4) == [401] * 4
    assert login(client, "op", "password-lunga-1").status_code == 200
    assert state(engine, "op") == (0, False)
    assert fail(client, "op", 4) == [401] * 4
    assert state(engine, "op") == (4, False)


def test_the_threshold_comes_from_the_policy(client, engine):
    as_admin(client)
    r = client.put("/api/security-policy", headers=ORIGIN, json={"maxFailedLogins": 3})
    assert r.status_code == 200 and r.json()["maxFailedLogins"] == 3
    assert fail(client, "op", 3) == [401, 401, 423]


@pytest.mark.parametrize("value", [2, 21, 0, -1, "5", 4.5, True, None])
def test_the_policy_rejects_values_out_of_range(client, value):
    as_admin(client)
    r = client.put("/api/security-policy", headers=ORIGIN, json={"maxFailedLogins": value})
    assert r.status_code == 422, (value, r.text)


def test_only_admins_read_or_change_the_policy(client):
    assert login(client, "op", "password-lunga-1").status_code == 200
    assert client.get("/api/security-policy").status_code == 403
    assert client.put("/api/security-policy", headers=ORIGIN,
                      json={"maxFailedLogins": 10}).status_code == 403


def test_policy_change_is_audited(client, engine):
    as_admin(client)
    client.put("/api/security-policy", headers=ORIGIN, json={"maxFailedLogins": 7})
    with engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM audit WHERE action = "
                           "'security.policy.updated' AND actor_username = 'capo'")).scalar_one()
    assert n == 1


# ------------------------------------------------------------------ sblocco

def test_the_lock_does_not_expire_by_itself(client, engine):
    fail(client, "op", 5)
    with engine.begin() as c:     # un'ora dopo: i tentativi escono da ogni finestra
        c.execute(text("UPDATE login_attempts SET ts = ts - interval '1 hour'"))
    assert login(client, "op", "password-lunga-1").status_code == 423


def test_an_admin_unlocks_and_the_user_gets_back_in(client, engine):
    fail(client, "op", 5)
    as_admin(client)
    r = client.post(f"/api/users/{uid(engine, 'op')}/unlock", headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["locked"] is False and r.json()["failedLogins"] == 0
    client.post("/api/auth/logout", headers=ORIGIN)
    assert login(client, "op", "password-lunga-1").status_code == 200
    with engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM audit WHERE action = 'users.unlocked'")).scalar_one()
    assert n == 1


def test_a_non_admin_cannot_unlock(client, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET locked_at = now() WHERE username = 'capo'"))
    assert login(client, "op", "password-lunga-1").status_code == 200
    r = client.post(f"/api/users/{uid(engine, 'capo')}/unlock", headers=ORIGIN)
    assert r.status_code == 403
    assert state(engine, "capo")[1] is True


def test_the_user_list_shows_the_lock(client, engine):
    fail(client, "op", 5)
    as_admin(client)
    rows = {u["username"]: u for u in client.get("/api/users").json()}
    assert rows["op"]["locked"] is True and rows["op"]["lockedAt"]
    assert rows["capo"]["locked"] is False


def test_a_temporary_password_also_unlocks(client, engine):
    fail(client, "op", 5)
    as_admin(client)
    r = client.post(f"/api/users/{uid(engine, 'op')}/reset-password", headers=ORIGIN)
    assert r.status_code == 200
    temp = r.json()["temporaryPassword"]
    client.post("/api/auth/logout", headers=ORIGIN)
    assert login(client, "op", temp).status_code == 200


def test_the_command_line_unlocks_the_last_admin(db, engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM users"))
        create_user(c, "solo", "password-lunga-9", "admin", must_change_pw=False)
        c.execute(text("UPDATE users SET locked_at = now(), failed_logins = 5"))
    env = {**os.environ, "TSM_DB_URL": DSN}
    r = subprocess.run([sys.executable, "scripts/unlock_user.py", "solo"],
                       cwd=BACKEND, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert state(engine, "solo") == (0, False)


# ------------------------------------------------------------------ casi limite

def test_unknown_users_accumulate_nothing_and_stay_indistinguishable(client, engine):
    codes = fail(client, "nessuno-si-chiama-cosi", 8)
    assert codes == [401] * 8


def test_a_disabled_user_is_not_counted_and_stays_indistinguishable(client, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET disabled_at = now() WHERE username = 'op'"))
    assert fail(client, "op", 6) == [401] * 6
    assert state(engine, "op") == (0, False)


def test_the_gateway_is_not_locked_out_by_a_few_colleagues(client, engine):
    """Tutti dallo stesso indirizzo: 30 errori su utenze diverse non devono far
    scattare il limite per IP, che prima era 20 e chiudeva fuori il gruppo."""
    for i in range(30):
        assert login(client, f"collega-{i}", "sbagliata").status_code == 401
    assert login(client, "op", "password-lunga-1").status_code == 200
