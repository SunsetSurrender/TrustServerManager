"""Hardening dell'autenticazione: sessione ristretta, stato riletto, origine,
limitazione dei tentativi, audit. PostgreSQL reale.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import Actor, InventoryRepository
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RACK = "cccccccc-0000-4000-8000-00000000000a"
DEV = "dddddddd-0000-4000-8000-00000000000a"

#: Client HTTPS e `Origin` corrispondente: vedi il commento in conftest.py.
from conftest import ORIGIN, api_client  # noqa: E402


def base_doc() -> dict:
    return {"schemaVersion": CURRENT_SCHEMA_VERSION,
            "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
                {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [],
                 "racks": [{"_uid": RACK, "id": "R01", "name": "R01", "u": 45,
                            "x": 0.5, "y": 0.5, "w": 0.6, "h": 0.8, "devices": [
                                {"_uid": DEV, "id": "srv-01", "name": "srv-01",
                                 "u": 10}]}]}]}]}


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
    """Stato pulito: utenze di prova, inventario inizializzato, contatori a zero."""
    with engine.begin() as c:
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "op", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "temp", "password-lunga-2", "edit", must_change_pw=True)
        create_user(c, "capo", "password-lunga-3", "admin", must_change_pw=False)
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(base_doc(), Actor(username="capo", role="admin"))
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


def login(client, username: str, password: str):
    return client.post("/api/auth/login", headers=ORIGIN,
                       json={"username": username, "password": password})


def uid_of(engine, username: str):
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM users WHERE username = :u"),
                         {"u": username}).scalar_one()


# ==================================================================
# §8.26 — password provvisoria: sessione valida ma ristretta
# ==================================================================

def test_temporary_password_login_succeeds_with_flags(client):
    r = login(client, "temp", "password-lunga-2")
    assert r.status_code == 200
    body = r.json()
    assert body == {"authenticated": True, "username": "temp", "role": "edit",
                    "mustChangePassword": True}
    assert "tsm_session" in r.cookies or r.headers.get("set-cookie")


def test_restricted_session_can_reach_exactly_three_endpoints(client):
    login(client, "temp", "password-lunga-2")

    # consentiti
    assert client.get("/api/auth/me").status_code == 200

    # tutto il resto: 403 PASSWORD_CHANGE_REQUIRED
    blocked = [
        ("get", "/api/inventory", None),
        ("put", "/api/inventory", {"baseVersion": 1, "doc": base_doc()}),
        ("get", "/api/users", None),
        ("post", "/api/users", {"username": "x", "role": "view"}),
    ]
    for method, path, payload in blocked:
        fn = getattr(client, method)
        r = fn(path, headers=ORIGIN, **({"json": payload} if payload else {}))
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["detail"]["code"] == "password_change_required", path


def test_restricted_session_can_change_password_and_logout(client):
    login(client, "temp", "password-lunga-2")
    r = client.post("/api/auth/password", headers=ORIGIN,
                    json={"currentPassword": "password-lunga-2",
                          "newPassword": "password-nuova-lunga"})
    assert r.status_code == 204


def test_password_change_revokes_all_sessions_and_needs_fresh_login(client, engine):
    login(client, "temp", "password-lunga-2")
    uid = uid_of(engine, "temp")

    # una seconda sessione dello stesso utente, che deve cadere anch'essa
    with api_client(app) as other:
        login(other, "temp", "password-lunga-2")
        assert other.get("/api/auth/me").status_code == 200

        client.post("/api/auth/password", headers=ORIGIN,
                    json={"currentPassword": "password-lunga-2",
                          "newPassword": "password-nuova-lunga"})

        assert client.get("/api/auth/me").status_code == 401
        assert other.get("/api/auth/me").status_code == 401

    with engine.connect() as c:
        live = c.execute(text("SELECT count(*) FROM sessions "
                              "WHERE user_id = :u AND revoked_at IS NULL"),
                         {"u": uid}).scalar_one()
    assert live == 0

    # accesso nuovo con la password nuova: ora non è più ristretto
    r = login(client, "temp", "password-nuova-lunga")
    assert r.status_code == 200
    assert r.json()["mustChangePassword"] is False
    assert client.get("/api/inventory").status_code == 200


def test_password_change_clears_the_cookie(client):
    login(client, "temp", "password-lunga-2")
    r = client.post("/api/auth/password", headers=ORIGIN,
                    json={"currentPassword": "password-lunga-2",
                          "newPassword": "password-nuova-lunga"})
    cookie = r.headers.get("set-cookie", "")
    assert "tsm_session=" in cookie
    assert ("Max-Age=0" in cookie or "max-age=0" in cookie
            or "expires=Thu, 01 Jan 1970" in cookie.lower())


# ==================================================================
# §8.26 — stato mutabile riletto a ogni richiesta
# ==================================================================

def test_role_change_takes_effect_on_next_request(client, engine):
    login(client, "op", "password-lunga-1")
    assert client.get("/api/users").status_code == 403     # edit non è admin

    with engine.begin() as c:
        c.execute(text("UPDATE users SET role = 'admin' WHERE username = 'op'"))

    # Nessun nuovo accesso: la sessione è la stessa, l'autorità è riletta.
    assert client.get("/api/users").status_code == 200
    assert client.get("/api/auth/me").json()["role"] == "admin"


def test_role_downgrade_takes_effect_immediately(client, engine):
    login(client, "capo", "password-lunga-3")
    assert client.get("/api/users").status_code == 200

    with engine.begin() as c:
        c.execute(text("UPDATE users SET role = 'view' WHERE username = 'capo'"))
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/auth/me").json()["role"] == "view"


def test_disabling_a_user_kills_the_session_immediately(client, engine):
    login(client, "op", "password-lunga-1")
    assert client.get("/api/inventory").status_code == 200

    with engine.begin() as c:
        c.execute(text("UPDATE users SET disabled_at = now() WHERE username = 'op'"))
    assert client.get("/api/inventory").status_code == 401


def test_setting_must_change_pw_restricts_an_open_session(client, engine):
    login(client, "op", "password-lunga-1")
    assert client.get("/api/inventory").status_code == 200

    with engine.begin() as c:
        c.execute(text("UPDATE users SET must_change_pw = TRUE WHERE username = 'op'"))
    r = client.get("/api/inventory")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"
    assert client.get("/api/auth/me").json()["mustChangePassword"] is True


# ==================================================================
# §8.27 — validazione dell'origine
# ==================================================================

def test_state_changing_request_without_origin_is_refused(client):
    login(client, "op", "password-lunga-1")
    r = client.put("/api/inventory", json={"baseVersion": 1, "doc": base_doc()})
    assert r.status_code == 403
    assert r.json()["code"] == "origin_not_allowed"


def test_state_changing_request_with_foreign_origin_is_refused(client):
    login(client, "op", "password-lunga-1")
    r = client.put("/api/inventory", headers={"Origin": "https://malintenzionato.example"},
                   json={"baseVersion": 1, "doc": base_doc()})
    assert r.status_code == 403
    assert r.json()["code"] == "origin_not_allowed"


def test_state_changing_request_with_matching_origin_is_allowed(client):
    login(client, "op", "password-lunga-1")
    r = client.put("/api/inventory", headers=ORIGIN,
                   json={"baseVersion": 1, "doc": base_doc()})
    assert r.status_code == 200


def test_reads_do_not_require_origin(client):
    login(client, "op", "password-lunga-1")
    assert client.get("/api/inventory").status_code == 200


def test_requests_without_session_cookie_do_not_require_origin(client):
    """Senza cookie non c'è autorità da abusare: pretendere `Origin` romperebbe i
    client non-browser senza proteggere nulla."""
    r = client.post("/api/auth/login",
                    json={"username": "op", "password": "password-lunga-1"})
    assert r.status_code == 200


def test_no_permissive_cors_headers(client):
    login(client, "op", "password-lunga-1")
    r = client.get("/api/inventory")
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}


# ==================================================================
# §8.28 — limitazione e resistenza all'enumerazione
# ==================================================================

def test_login_is_rate_limited_per_username(client, engine):
    for i in range(5):
        assert login(client, "op", f"sbagliata-{i}").status_code == 401
    r = login(client, "op", "sbagliata-x")
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "too_many_attempts"
    assert r.headers.get("retry-after")
    # anche la password GIUSTA viene bloccata: il limitatore protegge l'utenza
    assert login(client, "op", "password-lunga-1").status_code == 429


def test_successful_logins_do_not_count_towards_the_limit(client):
    for _ in range(8):
        assert login(client, "op", "password-lunga-1").status_code == 200


def test_unknown_and_wrong_password_are_indistinguishable(client):
    a = login(client, "op", "sbagliata")
    b = login(client, "non-esiste-affatto", "sbagliata")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_disabled_user_login_is_indistinguishable(client, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET disabled_at = now() WHERE username = 'op'"))
    a = login(client, "op", "password-lunga-1")
    b = login(client, "non-esiste-affatto", "password-lunga-1")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_input_sizes_are_bounded(client):
    r = client.post("/api/auth/login", headers=ORIGIN,
                    json={"username": "x" * 5000, "password": "y" * 5000})
    assert r.status_code == 422       # rifiutato dal contratto, non elaborato


def test_forwarded_ip_is_ignored_from_untrusted_peer(client, engine):
    """Il TestClient non è un proxy fidato: `X-Forwarded-For` non deve essere
    creduto, altrimenti basterebbe cambiare una stringa a ogni tentativo per
    aggirare il limitatore per IP."""
    for i in range(3):
        login(client, "op", f"sbagliata-{i}")
    with engine.connect() as c:
        ips = [r[0] for r in c.execute(text(
            "SELECT DISTINCT ip FROM login_attempts WHERE success = FALSE"))]
    # nessun tentativo registrato con l'IP inventato dal client
    for i in range(3):
        login(client, "op", f"altro-{i}")
    with engine.connect() as c:
        forged = c.execute(text(
            "SELECT count(*) FROM login_attempts WHERE ip = '9.9.9.9'")).scalar_one()
    assert forged == 0, f"IP falsificato accettato; ip visti: {ips}"


def test_forged_forwarded_header_does_not_change_recorded_ip(client, engine):
    login(client, "op", "sbagliata", )
    r = client.post("/api/auth/login", headers={**ORIGIN, "X-Forwarded-For": "9.9.9.9"},
                    json={"username": "op", "password": "sbagliata"})
    assert r.status_code == 401
    with engine.connect() as c:
        forged = c.execute(text(
            "SELECT count(*) FROM login_attempts WHERE ip = '9.9.9.9'")).scalar_one()
    assert forged == 0


# ==================================================================
# §8.25 — audit degli eventi di autenticazione, senza credenziali
# ==================================================================

def _audit(engine, action: str) -> list[dict]:
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT actor_username, actor_role, action, events, ip
              FROM audit WHERE action = :a ORDER BY id
        """), {"a": action}).mappings().all()
    return [dict(r) for r in rows]


def test_login_success_is_audited(client, engine):
    login(client, "op", "password-lunga-1")
    rows = _audit(engine, "auth.login.success")
    assert len(rows) == 1
    assert rows[0]["actor_username"] == "op"
    assert rows[0]["actor_role"] == "edit"


def test_login_failure_is_audited_with_null_role(client, engine):
    login(client, "non-esiste-affatto", "sbagliata")
    rows = _audit(engine, "auth.login.failure")
    assert len(rows) == 1
    # un accesso fallito non ha un ruolo: la colonna è nullable proprio per non
    # dover inventare un valore in un registro di audit
    assert rows[0]["actor_role"] is None
    assert rows[0]["actor_username"] == "non-esiste-affatto"


def test_logout_is_audited(client, engine):
    login(client, "op", "password-lunga-1")
    client.post("/api/auth/logout", headers=ORIGIN)
    assert len(_audit(engine, "auth.logout")) == 1


def test_password_change_is_audited(client, engine):
    login(client, "temp", "password-lunga-2")
    client.post("/api/auth/password", headers=ORIGIN,
                json={"currentPassword": "password-lunga-2",
                      "newPassword": "password-nuova-lunga"})
    rows = _audit(engine, "auth.password.changed")
    assert len(rows) == 1
    assert rows[0]["actor_username"] == "temp"


def test_rate_limit_block_is_audited(client, engine):
    for i in range(6):
        login(client, "op", f"sbagliata-{i}")
    assert len(_audit(engine, "auth.login.blocked")) >= 1


def test_audit_never_contains_submitted_credentials(client, engine):
    secret = "password-lunga-1"
    login(client, "op", secret)
    login(client, "op", "una-password-sbagliatissima")
    login(client, "temp", "password-lunga-2")
    client.post("/api/auth/password", headers=ORIGIN,
                json={"currentPassword": "password-lunga-2",
                      "newPassword": "password-nuova-lunga"})

    with engine.connect() as c:
        blob = json.dumps([dict(r) for r in c.execute(text(
            "SELECT actor_username, action, events, client_hint FROM audit"
        )).mappings().all()], default=str)

    for leaked in (secret, "una-password-sbagliatissima", "password-lunga-2",
                   "password-nuova-lunga"):
        assert leaked not in blob, leaked
    # e nemmeno gli hash: un hash in un registro consultabile è attaccabile offline
    assert "$argon2" not in blob
