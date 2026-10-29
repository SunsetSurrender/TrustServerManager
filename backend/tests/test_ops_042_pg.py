"""0.4.2: uscita per inattività, dati personali dal Profilo, stato del sistema.
PostgreSQL reale."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402


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
        c.execute(text("DELETE FROM recovery_codes"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users"))
        c.execute(text("UPDATE security_policy SET max_failed_logins = 5, "
                       "idle_timeout_minutes = 60, updated_by = NULL WHERE id = 1"))
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


def login(client, u, p):
    r = client.post("/api/auth/login", headers=ORIGIN, json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r


# ------------------------------------------------------------ inattività

def test_me_reports_the_idle_timeout(client):
    r = login(client, "op", "password-lunga-1")
    assert r.json()["idleTimeoutMinutes"] == 60
    assert client.get("/api/auth/me").json()["idleTimeoutMinutes"] == 60


def test_an_idle_session_is_closed_and_revoked(client, engine):
    login(client, "op", "password-lunga-1")
    with engine.begin() as c:
        c.execute(text("UPDATE sessions SET last_seen_at = now() - interval '61 minutes'"))
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "session_idle"
    with engine.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM sessions WHERE revoked_at IS NULL")).scalar_one() == 0
    # Alzare la soglia dopo non la resuscita.
    with engine.begin() as c:
        c.execute(text("UPDATE security_policy SET idle_timeout_minutes = 720"))
    assert client.get("/api/auth/me").status_code == 401


def test_activity_keeps_the_session_alive(client, engine):
    login(client, "op", "password-lunga-1")
    with engine.begin() as c:
        c.execute(text("UPDATE sessions SET last_seen_at = now() - interval '59 minutes'"))
    assert client.get("/api/auth/me").status_code == 200
    with engine.connect() as c:
        recente = c.execute(text(
            "SELECT last_seen_at > now() - interval '1 minute' FROM sessions")).scalar_one()
    assert recente


def test_zero_disables_the_idle_timeout(client, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE security_policy SET idle_timeout_minutes = 0"))
    login(client, "op", "password-lunga-1")
    with engine.begin() as c:
        c.execute(text("UPDATE sessions SET last_seen_at = now() - interval '11 hours'"))
    assert client.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("value,ok", [(0, True), (15, True), (720, True), (14, False),
                                      (721, False), (-1, False), ("60", False), (True, False)])
def test_idle_policy_validation(client, value, ok):
    login(client, "capo", "password-lunga-3")
    r = client.put("/api/security-policy", headers=ORIGIN, json={"idleTimeoutMinutes": value})
    assert (r.status_code == 200) == ok, (value, r.text)
    if ok:
        assert r.json()["idleTimeoutMinutes"] == value
        assert r.json()["maxFailedLogins"] == 5


# ------------------------------------------------------------ dati personali

def test_users_edit_their_own_profile(client, engine):
    login(client, "op", "password-lunga-1")
    r = client.patch("/api/auth/me", headers=ORIGIN,
                     json={"nome": "Mario", "cognome": "Rossi", "telefono": "06 123",
                           "email": " m.rossi@tim.it "})
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["nome"], b["cognome"], b["telefono"], b["email"]) == \
        ("Mario", "Rossi", "06 123", "m.rossi@tim.it")
    assert client.get("/api/auth/me").json()["email"] == "m.rossi@tim.it"


def test_team_and_role_are_not_self_service(client, engine):
    login(client, "op", "password-lunga-1")
    r = client.patch("/api/auth/me", headers=ORIGIN,
                     json={"team": "admin", "role": "admin", "nome": "Mario"})
    assert r.status_code == 200
    with engine.connect() as c:
        row = c.execute(text("SELECT role, team, nome FROM users WHERE username = 'op'")).first()
    assert row == ("edit", None, "Mario")


def test_profile_email_is_validated(client):
    login(client, "op", "password-lunga-1")
    r = client.patch("/api/auth/me", headers=ORIGIN, json={"email": "non-una-mail"})
    assert 400 <= r.status_code < 500


def test_profile_needs_a_session(client):
    assert client.patch("/api/auth/me", headers=ORIGIN, json={"nome": "x"}).status_code == 401


# ------------------------------------------------------------ stato del sistema

def test_status_is_admin_only(client):
    login(client, "op", "password-lunga-1")
    assert client.get("/api/system/status").status_code == 403


def test_status_reports_the_essentials(client, engine):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO db_dumps (finished_at, ok, file, bytes, disk_total_bytes,
                                                disk_free_bytes, kept)
                          VALUES (now(), true, 'tsm-x.dump', 2048, 1000, 200, 3)"""))
    login(client, "capo", "password-lunga-3")
    r = client.get("/api/system/status")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["overall"] in ("ok", "warn", "crit")
    nomi = {(i["gruppo"], i["nome"]): i for i in b["items"]}
    assert ("Database", "Migrazione") in nomi and nomi[("Database", "Migrazione")]["stato"] == "ok"
    assert nomi[("Backup", "Ultimo dump")]["stato"] == "ok"
    assert nomi[("Backup", "Disco dati (/srv/tsm-data)")]["stato"] == "warn"   # 80%: fra 75 e 90
    with engine.begin() as c:
        c.execute(text("DELETE FROM db_dumps"))


def test_status_never_leaks_secrets(client):
    login(client, "capo", "password-lunga-3")
    corpo = client.get("/api/system/status").text.lower()
    for parola in ("password_hash", "argon2", "secret", "smtp_host"):
        assert parola not in corpo
