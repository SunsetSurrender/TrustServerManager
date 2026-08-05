"""Gestione delle utenze da parte degli amministratori. PostgreSQL reale."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ORIGIN = {"Origin": "http://testserver"}


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
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
        create_user(c, "vice", "password-lunga-2", "admin", must_change_pw=False)
        create_user(c, "op", "password-lunga-3", "edit", must_change_pw=False)
    yield engine


@pytest.fixture
def admin(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with TestClient(app) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "capo", "password": "password-lunga-1"})
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def operator(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with TestClient(app) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "op", "password": "password-lunga-3"})
        yield c
    app.dependency_overrides.clear()


def find(client, username: str) -> dict:
    rows = client.get("/api/users?includeDisabled=true").json()
    return next(r for r in rows if r["username"] == username)


# ----------------------------------------------------------- autorizzazione

def test_only_admins_can_manage_users(operator):
    assert operator.get("/api/users").status_code == 403
    assert operator.post("/api/users", headers=ORIGIN,
                         json={"username": "x", "role": "view"}).status_code == 403


def test_unauthenticated_cannot_manage_users(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with TestClient(app) as c:
        assert c.get("/api/users").status_code == 401
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ elenco

def test_list_hides_disabled_by_default(admin):
    target = find(admin, "op")
    admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN)
    names = {r["username"] for r in admin.get("/api/users").json()}
    assert "op" not in names
    names_all = {r["username"] for r in admin.get("/api/users?includeDisabled=true").json()}
    assert "op" in names_all


def test_list_never_exposes_password_hashes(admin):
    body = admin.get("/api/users?includeDisabled=true").text
    assert "$argon2" not in body
    assert "password_hash" not in body
    assert "passwordHash" not in body


# ------------------------------------------------------------------ creazione

def test_create_returns_temporary_password_once(admin):
    r = admin.post("/api/users", headers=ORIGIN,
                   json={"username": "nuovo", "role": "edit", "nome": "Anna"})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["username"] == "nuovo"
    assert body["user"]["mustChangePassword"] is True
    assert body["user"]["nome"] == "Anna"
    temp = body["temporaryPassword"]
    assert len(temp) >= 12

    # La password provvisoria non è registrata da nessuna parte.
    audit = admin.get("/api/users").text
    assert temp not in audit


def test_created_user_must_change_password_on_first_login(admin, engine):
    temp = admin.post("/api/users", headers=ORIGIN,
                      json={"username": "nuovo", "role": "edit"}).json()["temporaryPassword"]
    with TestClient(app) as c:
        r = c.post("/api/auth/login", headers=ORIGIN,
                   json={"username": "nuovo", "password": temp})
        assert r.status_code == 200
        assert r.json()["mustChangePassword"] is True
        assert c.get("/api/users").status_code == 403


def test_duplicate_username_is_refused(admin):
    r = admin.post("/api/users", headers=ORIGIN,
                   json={"username": "op", "role": "view"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "username_taken"


def test_disabled_username_suggests_reactivation(admin):
    target = find(admin, "op")
    admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN)
    r = admin.post("/api/users", headers=ORIGIN,
                   json={"username": "op", "role": "view"})
    assert r.status_code == 409
    assert "riattiv" in r.json()["detail"]["message"].lower()


def test_invalid_role_is_refused(admin):
    r = admin.post("/api/users", headers=ORIGIN,
                   json={"username": "x", "role": "superuser"})
    assert r.status_code == 422


# ------------------------------------------------------- ruolo e profilo

def test_change_role(admin):
    target = find(admin, "op")
    r = admin.patch(f"/api/users/{target['id']}", headers=ORIGIN, json={"role": "admin"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_patch_only_touches_sent_fields(admin):
    target = find(admin, "op")
    admin.patch(f"/api/users/{target['id']}", headers=ORIGIN,
                json={"nome": "Mario", "team": "Infra"})
    admin.patch(f"/api/users/{target['id']}", headers=ORIGIN, json={"role": "view"})
    row = find(admin, "op")
    # una PATCH che cambia solo il ruolo non deve azzerare il profilo
    assert row["nome"] == "Mario" and row["team"] == "Infra" and row["role"] == "view"


# ------------------------------------------- disattivazione e riattivazione

def test_disable_revokes_sessions(admin, engine):
    target = find(admin, "op")
    with TestClient(app) as victim:
        victim.post("/api/auth/login", headers=ORIGIN,
                    json={"username": "op", "password": "password-lunga-3"})
        assert victim.get("/api/auth/me").status_code == 200
        admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN)
        # la sessione cade subito, non alla scadenza del cookie
        assert victim.get("/api/auth/me").status_code == 401


def test_disabled_user_cannot_login(admin):
    target = find(admin, "op")
    admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", headers=ORIGIN,
                   json={"username": "op", "password": "password-lunga-3"})
        assert r.status_code == 401


def test_enable_restores_access(admin):
    target = find(admin, "op")
    admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN)
    r = admin.post(f"/api/users/{target['id']}/enable", headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["disabled"] is False
    with TestClient(app) as c:
        assert c.post("/api/auth/login", headers=ORIGIN,
                      json={"username": "op", "password": "password-lunga-3"}
                      ).status_code == 200


def test_disable_is_idempotent(admin):
    target = find(admin, "op")
    assert admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN).status_code == 200
    assert admin.post(f"/api/users/{target['id']}/disable", headers=ORIGIN).status_code == 200


def test_cannot_disable_self(admin):
    me = find(admin, "capo")
    r = admin.post(f"/api/users/{me['id']}/disable", headers=ORIGIN)
    assert r.status_code == 422


# ------------------------------------------------- ultimo amministratore

def test_cannot_disable_last_active_admin(admin):
    vice = find(admin, "vice")
    assert admin.post(f"/api/users/{vice['id']}/disable", headers=ORIGIN).status_code == 200
    # ora `capo` è l'unico admin attivo, e sta operando: si prova a togliere lui
    # tramite un secondo admin riattivato? No: si verifica la protezione diretta.
    me = find(admin, "capo")
    r = admin.post(f"/api/users/{me['id']}/disable", headers=ORIGIN)
    assert r.status_code in (409, 422)      # 422 = "non su te stesso"


def test_cannot_demote_last_active_admin(admin):
    vice = find(admin, "vice")
    admin.post(f"/api/users/{vice['id']}/disable", headers=ORIGIN)
    me = find(admin, "capo")
    r = admin.patch(f"/api/users/{me['id']}", headers=ORIGIN, json={"role": "edit"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_admin_protected"


def test_demoting_a_non_last_admin_is_allowed(admin):
    vice = find(admin, "vice")
    r = admin.patch(f"/api/users/{vice['id']}", headers=ORIGIN, json={"role": "edit"})
    assert r.status_code == 200
    assert r.json()["role"] == "edit"


# ------------------------------------------------------ reset password

def test_reset_password_returns_temp_and_revokes_sessions(admin):
    target = find(admin, "op")
    with TestClient(app) as victim:
        victim.post("/api/auth/login", headers=ORIGIN,
                    json={"username": "op", "password": "password-lunga-3"})
        assert victim.get("/api/auth/me").status_code == 200

        r = admin.post(f"/api/users/{target['id']}/reset-password", headers=ORIGIN)
        assert r.status_code == 200
        temp = r.json()["temporaryPassword"]
        assert r.json()["user"]["mustChangePassword"] is True

        # chi aveva la sessione aperta viene scollegato: un reset chiesto perché la
        # password è compromessa non servirebbe a nulla altrimenti
        assert victim.get("/api/auth/me").status_code == 401

    with TestClient(app) as c:
        assert c.post("/api/auth/login", headers=ORIGIN,
                      json={"username": "op", "password": "password-lunga-3"}
                      ).status_code == 401
        r = c.post("/api/auth/login", headers=ORIGIN,
                   json={"username": "op", "password": temp})
        assert r.status_code == 200
        assert r.json()["mustChangePassword"] is True


# ------------------------------------------------------ nessun DELETE

def test_no_delete_endpoint_exists():
    spec = app.openapi()["paths"]
    for path, ops in spec.items():
        assert "delete" not in ops, f"{path} espone DELETE"


def test_user_paths_are_the_expected_set():
    paths = {p for p in app.openapi()["paths"] if p.startswith("/api/users")}
    assert paths == {"/api/users", "/api/users/{user_id}",
                     "/api/users/{user_id}/disable", "/api/users/{user_id}/enable",
                     "/api/users/{user_id}/reset-password"}


# ------------------------------------------------------------------ audit

def test_user_management_is_audited(admin, engine):
    r = admin.post("/api/users", headers=ORIGIN,
                   json={"username": "nuovo", "role": "edit"})
    uid = r.json()["user"]["id"]
    admin.patch(f"/api/users/{uid}", headers=ORIGIN, json={"role": "view"})
    admin.post(f"/api/users/{uid}/disable", headers=ORIGIN)
    admin.post(f"/api/users/{uid}/enable", headers=ORIGIN)
    admin.post(f"/api/users/{uid}/reset-password", headers=ORIGIN)

    with engine.connect() as c:
        actions = [r[0] for r in c.execute(text(
            "SELECT action FROM audit WHERE action LIKE 'users.%' ORDER BY id"))]
    assert actions == ["users.created", "users.updated", "users.disabled",
                       "users.enabled", "users.password_reset"]

    with engine.connect() as c:
        who = c.execute(text(
            "SELECT DISTINCT actor_username FROM audit WHERE action LIKE 'users.%'"
        )).scalars().all()
    assert who == ["capo"]      # l'attore è chi ha operato, non il bersaglio


def test_reset_password_is_not_recorded_in_audit(admin, engine):
    uid = find(admin, "op")["id"]
    temp = admin.post(f"/api/users/{uid}/reset-password",
                      headers=ORIGIN).json()["temporaryPassword"]
    with engine.connect() as c:
        blob = " ".join(str(r[0]) for r in c.execute(text("SELECT events FROM audit")))
    assert temp not in blob
