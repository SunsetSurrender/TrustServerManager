"""Rotte HTTP: contratto congelato, mappa degli errori, nessun accesso anonimo.

PostgreSQL reale, perché le rotte esistono per parlare col repository.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection, require_actor
from app.auth.service import create_user
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import Actor, InventoryRepository, canonical_sha256
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RACK = "cccccccc-0000-4000-8000-00000000000a"
DEV = "dddddddd-0000-4000-8000-00000000000a"

ADMIN = Actor(username="admin", role="admin")

#: Client HTTPS e `Origin` corrispondente: il cookie di sessione è `Secure` e su
#: `http://` non verrebbe inviato affatto. Vedi il commento in conftest.py.
from conftest import ORIGIN, api_client  # noqa: E402


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
def head_version(engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        v = InventoryRepository(c).bootstrap(base_doc(), ADMIN).version
    yield v


@pytest.fixture
def conn_override(engine):
    """Sostituisce get_connection: una transazione per richiesta, committata."""
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(conn_override):
    """Client SENZA attore: serve a dimostrare che le rotte sono inaccessibili."""
    with api_client(app) as c:
        yield c


@pytest.fixture
def as_editor(conn_override):
    app.dependency_overrides[require_actor] = \
        lambda: Actor(username="operatore", role="edit")
    with api_client(app) as c:
        yield c
    app.dependency_overrides.pop(require_actor, None)


@pytest.fixture
def as_viewer(conn_override):
    app.dependency_overrides[require_actor] = \
        lambda: Actor(username="lettore", role="view")
    with api_client(app) as c:
        yield c
    app.dependency_overrides.pop(require_actor, None)


# ==================================================================
# §8.20 — nessun accesso anonimo, nessun ripiego di sviluppo
# ==================================================================

def test_get_inventory_requires_authentication(client, head_version):
    r = client.get("/api/inventory")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "not_authenticated"


def test_put_inventory_requires_authentication(client, head_version):
    r = client.put("/api/inventory", headers=ORIGIN,
                   json={"baseVersion": head_version, "doc": base_doc()})
    assert r.status_code == 401


def test_no_dev_admin_fallback_exists(client, head_version):
    """Nessuna variabile d'ambiente, header o parametro concede l'accesso.

    Il ripiego di sviluppo è pericoloso proprio perché funziona: sopravvive ai
    refactoring, non fa fallire nessun test, e un giorno diventa un accesso
    amministrativo anonimo.
    """
    for headers in ({"X-Debug-Role": "admin"}, {"Authorization": "Bearer admin"},
                    {"X-Actor": "admin"}, {"X-Forwarded-User": "admin"}):
        r = client.get("/api/inventory", headers=headers)
        assert r.status_code == 401, headers
    r = client.get("/api/inventory?role=admin&dev=1")
    assert r.status_code == 401


def test_unauthenticated_responses_are_not_cached(client, head_version):
    r = client.get("/api/inventory")
    assert r.headers.get("cache-control") == "no-store"


# ==================================================================
# §8.22 — contratto congelato
# ==================================================================

def test_get_returns_the_frozen_shape(as_editor, head_version):
    r = as_editor.get("/api/inventory")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"version", "schemaVersion", "sha256", "doc"}
    assert body["version"] == head_version
    assert body["schemaVersion"] == CURRENT_SCHEMA_VERSION
    assert body["sha256"] == canonical_sha256(body["doc"])
    assert len(body["sha256"]) == 64
    assert r.headers.get("cache-control") == "no-store"


def test_put_change_returns_changed_true(as_editor, head_version):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "Dell R760"
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc,
                            "action": "Modificato srv-01"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"version", "schemaVersion", "sha256", "changed"}
    assert body["changed"] is True
    assert body["version"] > head_version
    assert r.headers.get("cache-control") == "no-store"


def test_put_noop_returns_changed_false(as_editor, head_version):
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": base_doc()})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == head_version


def test_put_idempotent_replay_returns_changed_false(as_editor, head_version):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    first = as_editor.put("/api/inventory", headers=ORIGIN,
                          json={"baseVersion": head_version, "doc": clone(doc)}).json()
    # stesso baseVersion (ormai superato) e stesso documento
    again = as_editor.put("/api/inventory", headers=ORIGIN,
                          json={"baseVersion": head_version, "doc": clone(doc)})
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert again.json()["version"] == first["version"]


def test_put_conflict_shape(as_editor, head_version):
    a = clone(base_doc())
    a["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "A"
    first = as_editor.put("/api/inventory", headers=ORIGIN,
                          json={"baseVersion": head_version, "doc": a}).json()

    b = clone(base_doc())
    b["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "B"
    r = as_editor.put("/api/inventory", headers=ORIGIN, json={"baseVersion": head_version, "doc": b})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "version_conflict"
    assert detail["currentVersion"] == first["version"]
    assert detail["currentSha256"] == first["sha256"]
    assert r.headers.get("cache-control") == "no-store"


def test_action_label_is_length_limited_by_the_contract(as_editor, head_version):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc,
                            "action": "x" * 5000})
    assert r.status_code == 422        # rifiutato dal contratto, non troncato in silenzio


# ==================================================================
# §8.21 — mappa degli errori, senza fughe di dettaglio
# ==================================================================

def test_forbidden_for_role_maps_to_403(as_viewer, head_version):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["model"] = "M"
    r = as_viewer.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["code"] == "not_authorized"
    assert detail["violations"][0]["requiredRole"] == "edit"


def test_forbidden_root_key_maps_to_422(as_editor, head_version):
    doc = clone(base_doc())
    doc["utenti"] = [{"email": "x", "password": "y"}]
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc})
    assert r.status_code == 422
    problems = r.json()["detail"]["problems"]
    codes = {p["code"] for p in problems}
    assert "forbidden_root_key" in codes
    # `path` nomina la chiave incriminata — è strutturale e serve al client per
    # correggersi. Il CONTENUTO rifiutato invece non deve tornare: né il valore
    # della password, né i messaggi che citano valori del documento.
    blob = json.dumps(problems)
    assert '"path": "utenti"' in blob or "utenti" in blob
    assert "message" not in blob
    for leaked in ("x@", "\"y\"", "email"):
        assert leaked not in blob, leaked


def test_identity_rejection_maps_to_422(as_editor, head_version):
    import uuid
    doc = clone(base_doc())
    rack = doc["locations"][0]["sale"][0]["racks"][0]
    rack["devices"] = [{"_uid": str(uuid.uuid4()), "id": "srv-01",
                        "name": "srv-01", "u": 10}]
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "identity_rejected"


def test_oversized_document_maps_to_413(as_editor, head_version):
    doc = clone(base_doc())
    doc["locations"][0]["sale"][0]["racks"][0]["devices"][0]["note"] = "x" * (4 * 1024 * 1024 + 10)
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc})
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "document_too_large"


def test_request_size_limit_at_application_level(as_editor):
    """Secondo livello: nginx ha il suo, ma questo vale anche per chi arriva
    direttamente all'API (§8.24)."""
    huge = "x" * (6 * 1024 * 1024)
    r = as_editor.put("/api/inventory", content=huge,
                      headers={**ORIGIN, "content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["code"] == "request_too_large"


def test_not_bootstrapped_maps_to_503(as_editor, engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    r = as_editor.get("/api/inventory")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "not_bootstrapped"


def test_errors_never_leak_sql_or_tracebacks(as_editor, head_version):
    doc = clone(base_doc())
    doc["schemaVersion"] = 99
    r = as_editor.put("/api/inventory", headers=ORIGIN,
                      json={"baseVersion": head_version, "doc": doc})
    body = r.text.lower()
    for leak in ("traceback", "psycopg", "select ", "insert into", "sqlalchemy",
                 "/app/", "line "):
        assert leak not in body, leak


# ==================================================================
# bootstrap fuori da HTTP (§8.17)
# ==================================================================

# `app.routes` in questa versione di FastAPI contiene wrapper dei router inclusi
# e non espone `path`: la superficie stabile su cui asserire è lo schema OpenAPI,
# che è anche quella che descrive il contratto verso il client.

def test_no_bootstrap_route_exists():
    """Il bootstrap non passa da HTTP (§8.17), e non è solo una convenzione: il
    ruolo di runtime non ha nemmeno il privilegio di inserire la riga di testa."""
    paths = set(app.openapi()["paths"])
    assert not any("bootstrap" in p for p in paths), sorted(paths)


def test_inventory_routes_are_only_get_and_put():
    ops = app.openapi()["paths"]["/api/inventory"]
    assert set(ops) == {"get", "put"}, sorted(ops)


def test_api_surface_is_the_expected_set():
    """Nessuna rotta comparsa per distrazione: la superficie è quella dichiarata."""
    assert set(app.openapi()["paths"]) == {
        "/api/health", "/api/ready",
        "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/auth/password",
        "/api/inventory",
        # Le tre interrogazioni della fase 2E (§8.46). Sono TRE, nominate, di sola
        # lettura: non esiste un endpoint che esegua una query fornita dal client, e
        # `test_queries_pg.py` lo verifica anche sui PARAMETRI — nessuno di loro accetta
        # `sql`, `where`, `orderBy` o un nome di colonna.
        "/api/inventory/search", "/api/inventory/capacity", "/api/inventory/expiries",
        "/api/users", "/api/users/{user_id}",
        "/api/users/{user_id}/disable", "/api/users/{user_id}/enable",
        "/api/users/{user_id}/reset-password",
        "/api/audit",
        "/api/settings",
        # Un solo endpoint di notifica, e manda un messaggio FISSO ai
        # destinatari salvati: non accetta destinatario, oggetto né corpo
        # (§8.38). Se qui comparisse una rotta di invio con parametri, sarebbe
        # un relay di posta autenticato.
        "/api/notifications/test",
        # Due rotte per le foto, e NESSUN `DELETE` (§8.5): le versioni storiche
        # dell'inventario referenziano le foto, quindi cancellarne i byte
        # trasformerebbe un rollback in un riquadro rotto. I byte li libera la
        # garbage collection nel worker, con un ruolo di database che l'API non ha.
        "/api/photos", "/api/photos/{photo_id}",
    }


def test_no_photo_delete_route_exists():
    """La verifica esplicita, perché è una rotta che qualcuno aggiungerà per
    comodità: «l'admin vuole poter rimuovere una foto». Rimuoverla dal rack è un
    salvataggio dell'inventario; cancellarne i byte romperebbe la storia di
    qualcun altro (§8.5)."""
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/api/photos"):
            assert "delete" not in ops, f"{path}: {sorted(ops)}"
    assert set(app.openapi()["paths"]["/api/photos"]) == {"post"}
    assert set(app.openapi()["paths"]["/api/photos/{photo_id}"]) == {"get"}


# ==================================================================
# §8.23 — readiness
# ==================================================================

def test_ready_requires_inventory_head(client, engine, head_version):
    r = client.get("/api/ready")
    assert r.status_code == 200
    assert r.json()["inventory"] == "ok"

    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    r = client.get("/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["database"] == "ok"
    assert body["migrations"] == "ok"
    assert body["inventory"] == "not-ready"


def test_health_stays_ok_without_inventory(client, engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ==================================================================
# autenticazione end-to-end
# ==================================================================

@pytest.fixture
def real_auth_client(conn_override, engine):
    """Client senza override dell'attore: l'autenticazione è quella vera."""
    # Ordine obbligato: `audit.actor_user_id` ha una FK verso `users` (0004),
    # quindi le utenze non si possono togliere prima delle righe che le citano.
    def _reset(c):
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users WHERE username LIKE 'test-%'"))

    with engine.begin() as c:
        _reset(c)
        create_user(c, "test-editor", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "test-temp", "password-lunga-2", "admin", must_change_pw=True)
    with api_client(app) as c:
        yield c
    with engine.begin() as c:
        _reset(c)


def test_login_then_read_inventory(real_auth_client, head_version):
    r = real_auth_client.post("/api/auth/login",
                              json={"username": "test-editor",
                                    "password": "password-lunga-1"})
    assert r.status_code == 200
    assert r.json() == {"authenticated": True, "username": "test-editor",
                        "role": "edit", "mustChangePassword": False}

    r = real_auth_client.get("/api/inventory")
    assert r.status_code == 200
    assert r.json()["version"] == head_version


def test_wrong_password_is_401_and_indistinguishable(real_auth_client):
    a = real_auth_client.post("/api/auth/login",
                              json={"username": "test-editor", "password": "sbagliata"})
    b = real_auth_client.post("/api/auth/login",
                              json={"username": "non-esiste", "password": "sbagliata"})
    assert a.status_code == b.status_code == 401
    # Lo stesso codice per utenza inesistente e password errata: distinguerli
    # direbbe a chi prova quali utenze esistono.
    assert a.json()["detail"]["code"] == b.json()["detail"]["code"] == "invalid_credentials"


def test_me_requires_session(real_auth_client):
    assert real_auth_client.get("/api/auth/me").status_code == 401


def test_temporary_password_blocks_writes_but_allows_me(real_auth_client, head_version):
    # forma dettagliata in tests/test_hardening_pg.py (§8.26)
    r = real_auth_client.post("/api/auth/login",
                              json={"username": "test-temp", "password": "password-lunga-2"})
    assert r.status_code == 200
    assert r.json()["mustChangePassword"] is True

    # /auth/me deve funzionare: il client deve poter sapere che serve il cambio
    assert real_auth_client.get("/api/auth/me").json()["mustChangePassword"] is True
    # l'inventario no
    r = real_auth_client.get("/api/inventory")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


def test_logout_revokes_the_session(real_auth_client, head_version):
    real_auth_client.post("/api/auth/login",
                          json={"username": "test-editor", "password": "password-lunga-1"})
    assert real_auth_client.get("/api/inventory").status_code == 200
    assert real_auth_client.post(
        "/api/auth/logout", headers=ORIGIN).status_code == 204
    assert real_auth_client.get("/api/inventory").status_code == 401


def test_password_change_revokes_sessions(real_auth_client):
    real_auth_client.post("/api/auth/login",
                          json={"username": "test-editor", "password": "password-lunga-1"})
    r = real_auth_client.post("/api/auth/password",
                              headers=ORIGIN,
                              json={"currentPassword": "password-lunga-1",
                                    "newPassword": "password-nuova-lunga"})
    assert r.status_code == 204
    # la sessione è stata revocata dal cambio password
    assert real_auth_client.get("/api/auth/me").status_code == 401


def test_session_token_is_not_stored_in_clear(real_auth_client, engine):
    r = real_auth_client.post("/api/auth/login",
                              json={"username": "test-editor",
                                    "password": "password-lunga-1"})
    token = real_auth_client.cookies.get("tsm_session")
    assert token
    with engine.connect() as c:
        rows = [row[0] for row in c.execute(text("SELECT token_hash FROM sessions"))]
    assert token not in rows
    assert all(len(h) == 64 for h in rows)


def test_session_cookie_flags(real_auth_client):
    r = real_auth_client.post("/api/auth/login",
                              json={"username": "test-editor",
                                    "password": "password-lunga-1"})
    cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("samesite", "SameSite")
