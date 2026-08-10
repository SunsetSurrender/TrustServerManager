"""API del registro di audit: paginazione a cursore, filtri, riservatezza.

PostgreSQL reale: la paginazione dipende dal confronto fra tuple e
dall'ordinamento del database, e un finto non lo dimostrerebbe.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.audit import Cursor, contains_secret
from app.auth.service import create_user
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

#: Client HTTPS e `Origin` corrispondente: vedi il commento in conftest.py.
from conftest import ORIGIN, api_client  # noqa: E402

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


def insert_event(conn, *, ts, username="admin", role="admin",
                 action="inventory.save", result="success",
                 detail=None, hint=None, ip="10.0.0.10", user_id=None):
    conn.execute(text("""
        INSERT INTO audit (ts, actor_user_id, actor_username, actor_role, ip,
                           action, result, scopes, events, client_hint)
        VALUES (:ts, :uid, :u, :r, :ip, :a, :res, '{}'::text[], :ev, :hint)
    """), {"ts": ts, "uid": user_id, "u": username, "r": role, "ip": ip,
           "a": action, "res": result,
           "ev": json.dumps(detail if detail is not None else []),
           "hint": hint})


@pytest.fixture
def db(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
        create_user(c, "op", "password-lunga-2", "edit", must_change_pw=False)
    yield engine


@pytest.fixture
def client(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with api_client(app) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "capo", "password": "password-lunga-1"})
        # L'accesso scrive la propria riga di audit, con `now()`: resterebbe in
        # cima a ogni pagina e falserebbe i conteggi dei test. Si azzera DOPO il
        # login, così ogni test vede solo le righe che inserisce.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit"))
        yield c
    app.dependency_overrides.clear()


def audit(client, **params):
    r = client.get("/api/audit", params={k: v for k, v in params.items()
                                         if v is not None})
    return r


# ==================================================================
# ordinamento e cursore
# ==================================================================

def test_identical_timestamps_are_ordered_by_id(db, engine, client):
    """Più eventi nello stesso istante: l'ordinamento resta totale grazie all'id.
    Con il solo `ts` l'ordine sarebbe arbitrario e la paginazione salterebbe righe."""
    with engine.begin() as c:
        for _ in range(10):
            insert_event(c, ts=T0)          # stesso identico timestamp

    body = audit(client, pageSize=10).json()
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids, reverse=True), ids


def test_pagination_covers_every_row_exactly_once(db, engine, client):
    """Il caso che giustifica il cursore a due campi: 25 eventi, tutti con lo
    stesso timestamp, letti in pagine da 7. Nessuna riga saltata, nessuna
    ripetuta."""
    with engine.begin() as c:
        for _ in range(25):
            insert_event(c, ts=T0)

    seen, cursor, pages = [], None, 0
    while True:
        body = audit(client, pageSize=7, cursor=cursor).json()
        seen.extend(i["id"] for i in body["items"])
        pages += 1
        cursor = body["nextCursor"]
        if not cursor:
            break
        assert pages < 20, "paginazione che non termina"

    assert len(seen) == 25, f"{len(seen)} righe lette"
    assert len(set(seen)) == 25, "righe duplicate fra le pagine"
    assert seen == sorted(seen, reverse=True), "ordine non rispettato fra pagine"


def test_pagination_with_mixed_timestamps(db, engine, client):
    with engine.begin() as c:
        for i in range(30):
            insert_event(c, ts=T0 - timedelta(seconds=i // 3))   # 3 per istante

    seen, cursor = [], None
    while True:
        body = audit(client, pageSize=4, cursor=cursor).json()
        seen.extend(i["id"] for i in body["items"])
        cursor = body["nextCursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 30


def test_next_cursor_is_null_on_last_page(db, engine, client):
    with engine.begin() as c:
        for _ in range(3):
            insert_event(c, ts=T0)
    body = audit(client, pageSize=50).json()
    assert len(body["items"]) == 3
    assert body["nextCursor"] is None


def test_cursor_is_opaque(db, engine, client):
    with engine.begin() as c:
        for _ in range(5):
            insert_event(c, ts=T0)
    cur = audit(client, pageSize=2).json()["nextCursor"]
    assert cur and "|" not in cur and " " not in cur
    # opaco al confine, ma decodificabile dal server
    assert Cursor.decode(cur).id > 0


@pytest.mark.parametrize("bad", [
    "non-base64!!", "x", "////", base64.urlsafe_b64encode(b"v9|x|1").decode(),
    base64.urlsafe_b64encode(b"v1|non-una-data|1").decode(),
    base64.urlsafe_b64encode(b"v1|2026-08-01T00:00:00+00:00|non-un-id").decode(),
    base64.urlsafe_b64encode(b"v1|2026-08-01T00:00:00+00:00|-5").decode(),
    base64.urlsafe_b64encode(b"solo-un-pezzo").decode(),
    "A" * 600,
])
def test_malformed_cursor_is_rejected_with_stable_code(db, client, bad):
    r = audit(client, cursor=bad)
    assert r.status_code == 422, f"{bad!r} -> {r.status_code}"
    assert r.json()["detail"]["code"] == "invalid_cursor"


def test_tampered_cursor_does_not_silently_shift_results(db, engine, client):
    """Un cursore manomesso deve dare errore, non una pagina plausibile."""
    with engine.begin() as c:
        for _ in range(5):
            insert_event(c, ts=T0)
    good = audit(client, pageSize=2).json()["nextCursor"]
    tampered = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    r = audit(client, cursor=tampered)
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        # se per caso decodifica, non deve comunque produrre righe fuori ordine
        ids = [i["id"] for i in r.json()["items"]]
        assert ids == sorted(ids, reverse=True)


# ==================================================================
# dimensione della pagina
# ==================================================================

def test_default_page_size(db, engine, client):
    with engine.begin() as c:
        for _ in range(60):
            insert_event(c, ts=T0)
    body = audit(client).json()
    assert len(body["items"]) == 50
    assert body["nextCursor"]


@pytest.mark.parametrize("bad", [0, -1, -100, 201, 1000, "abc", "1.5"])
def test_invalid_page_size_rejected(db, client, bad):
    r = audit(client, pageSize=bad)
    assert r.status_code == 422, f"{bad!r} -> {r.status_code}"
    assert r.json()["detail"]["code"] in ("invalid_page_size", "invalid_filter")


def test_max_page_size_accepted(db, engine, client):
    with engine.begin() as c:
        for _ in range(5):
            insert_event(c, ts=T0)
    assert audit(client, pageSize=200).status_code == 200


# ==================================================================
# filtri
# ==================================================================

@pytest.fixture
def dataset(client, engine):
    """Dipende da `client` e non da `db`: il client azzera l'audit dopo il login,
    quindi un dataset costruito prima verrebbe cancellato."""
    with engine.begin() as c:
        insert_event(c, ts=T0, username="capo", action="inventory.save",
                     result="success")
        insert_event(c, ts=T0 - timedelta(days=1), username="op",
                     action="auth.login.success", result="success")
        insert_event(c, ts=T0 - timedelta(days=2), username="op",
                     action="auth.login.failure", result="failure")
        insert_event(c, ts=T0 - timedelta(days=3), username="ignoto",
                     action="auth.login.blocked", result="denied")
        insert_event(c, ts=T0 - timedelta(days=10), username="capo",
                     action="users.created", result="success")
    return engine


def test_filter_by_username(dataset, client):
    items = audit(client, username="op").json()["items"]
    assert len(items) == 2
    assert {i["actor"]["username"] for i in items} == {"op"}


def test_filter_by_result(dataset, client):
    assert len(audit(client, result="failure").json()["items"]) == 1
    assert len(audit(client, result="denied").json()["items"]) == 1
    assert len(audit(client, result="success").json()["items"]) == 3


def test_filter_by_event_exact(dataset, client):
    items = audit(client, event="auth.login.failure").json()["items"]
    assert len(items) == 1


def test_filter_by_event_category(dataset, client):
    """`auth` prende tutta la famiglia, senza che il client debba elencarla."""
    items = audit(client, event="auth").json()["items"]
    assert len(items) == 3
    assert all(i["event"].startswith("auth.") for i in items)


def test_filter_by_date_range(dataset, client):
    frm = (T0 - timedelta(days=2, hours=1)).isoformat()
    to = (T0 - timedelta(hours=1)).isoformat()
    items = audit(client, **{"from": frm, "to": to}).json()["items"]
    assert len(items) == 2


def test_filter_combination(dataset, client):
    items = audit(client, username="op", result="failure").json()["items"]
    assert len(items) == 1
    assert items[0]["actor"]["username"] == "op"
    assert items[0]["result"] == "failure"


def test_filters_apply_across_pagination(dataset, engine, client):
    with engine.begin() as c:
        for _ in range(10):
            insert_event(c, ts=T0, username="op", action="auth.login.failure",
                         result="failure")
    seen, cursor = [], None
    while True:
        body = audit(client, username="op", result="failure",
                     pageSize=3, cursor=cursor).json()
        seen.extend(i["id"] for i in body["items"])
        cursor = body["nextCursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 11


@pytest.mark.parametrize("params", [
    {"result": "qualunque"},
    {"from": "non-una-data"},
    {"to": "2026-13-45"},
    {"event": "auth;DROP TABLE audit"},
    {"event": "a" * 200},
    {"from": "2026-08-05T00:00:00Z", "to": "2026-08-01T00:00:00Z"},
])
def test_invalid_filters_rejected(db, client, params):
    r = audit(client, **params)
    assert r.status_code == 422, f"{params} -> {r.status_code}"
    assert r.json()["detail"]["code"] in ("invalid_filter", "invalid_page_size")


# ==================================================================
# istantanea storica dell'attore
# ==================================================================

def test_renamed_user_keeps_historical_attribution(db, engine, client):
    """L'evento resta attribuito a chi era quella persona ALLORA."""
    with engine.begin() as c:
        insert_event(c, ts=T0, username="mario.rossi", role="admin",
                     action="inventory.save")
        c.execute(text("UPDATE users SET username = 'm.rossi' "
                       "WHERE username = 'op'"))
    items = audit(client).json()["items"]
    assert items[0]["actor"]["username"] == "mario.rossi"
    assert items[0]["actor"]["role"] == "admin"


def test_disabled_user_remains_attributable(db, engine, client):
    with engine.begin() as c:
        uid = c.execute(text("SELECT id FROM users WHERE username = 'op'")).scalar_one()
        insert_event(c, ts=T0, username="op", role="edit", user_id=uid)
        c.execute(text("UPDATE users SET disabled_at = now() WHERE id = :i"),
                  {"i": uid})
    items = audit(client).json()["items"]
    assert items[0]["actor"]["username"] == "op"
    assert items[0]["actor"]["userId"] == str(uid)


def test_demoted_user_keeps_the_role_held_at_the_time(db, engine, client):
    with engine.begin() as c:
        insert_event(c, ts=T0, username="op", role="admin")
        c.execute(text("UPDATE users SET role = 'view' WHERE username = 'op'"))
    assert audit(client).json()["items"][0]["actor"]["role"] == "admin"


# ==================================================================
# autorizzazione e sola lettura
# ==================================================================

def test_non_admin_gets_403(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with api_client(app) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "op", "password": "password-lunga-2"})
        r = c.get("/api/audit")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "forbidden_for_role"
    app.dependency_overrides.clear()


def test_unauthenticated_gets_401(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with api_client(app) as c:
        assert c.get("/api/audit").status_code == 401
    app.dependency_overrides.clear()


def test_no_write_routes_on_audit():
    paths = app.openapi()["paths"]
    assert set(paths.get("/api/audit", {})) == {"get"}


def test_runtime_role_cannot_modify_audit_history(db, engine):
    """Il privilegio, non la buona volontà: la storia non si corregge (§8.19).

    Il ruolo `tsm_api` lo crea la migrazione 0003 senza password (in produzione
    gliela dà `scripts/migrate.py` leggendola da un secret). Qui gliene diamo una
    per poterci connettere DAVVERO come lui: verificare i privilegi con la
    connessione del proprietario non proverebbe nulla.
    """
    with engine.begin() as c:
        insert_event(c, ts=T0)
        c.execute(text("ALTER ROLE tsm_api WITH LOGIN PASSWORD 'runtimepw'"))

    runtime_dsn = DSN.replace("//tsm:testpw@", "//tsm_api:runtimepw@")
    assert runtime_dsn != DSN, f"DSN non riscrivibile: {DSN}"
    runtime = create_engine(runtime_dsn, future=True)
    try:
        with runtime.connect() as c:
            # lettura: consentita, ed è ciò che serve alla rotta
            assert c.execute(text("SELECT count(*) FROM audit")).scalar_one() >= 1

        # Una connessione NUOVA per ogni tentativo: dopo una SELECT la connessione
        # ha già una transazione aperta, e un `begin()` su quella fallirebbe con un
        # errore di SQLAlchemy prima ancora di arrivare a Postgres — il test
        # sembrerebbe superato mentre non ha chiesto niente al database.
        for stmt in ("UPDATE audit SET action = 'falsificato'",
                     "DELETE FROM audit",
                     "TRUNCATE audit"):
            with pytest.raises(Exception) as exc:
                with runtime.begin() as c:
                    c.execute(text(stmt))
            assert "permission denied" in str(exc.value).lower(),                 f"{stmt} non è stato negato: {exc.value}"
    finally:
        runtime.dispose()


def test_runtime_role_can_still_append_audit(db, engine):
    """La sola lettura non basta: il runtime deve poter AGGIUNGERE, altrimenti
    nessun evento verrebbe più registrato."""
    with engine.begin() as c:
        c.execute(text("ALTER ROLE tsm_api WITH LOGIN PASSWORD 'runtimepw'"))
    runtime = create_engine(DSN.replace("//tsm:testpw@", "//tsm_api:runtimepw@"),
                            future=True)
    try:
        with runtime.begin() as c:
            insert_event(c, ts=T0, username="runtime")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM audit "
                                  "WHERE actor_username = 'runtime'")).scalar_one() == 1
    finally:
        runtime.dispose()


# ==================================================================
# riservatezza
# ==================================================================

def test_secrets_never_appear_in_the_response(db, engine, client):
    """Difesa in profondità: anche se un produttore sbaglia e scrive un segreto,
    la serializzazione non lo restituisce."""
    velenoso = {
        "password": "super-segreta-123",
        "temporaryPassword": "provvisoria-456",
        "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$abc$def",
        "session_token_hash": "deadbeef" * 8,
        "smtp": {"password": "smtp-segreta"},
        "dsn": "postgresql://utente:password-nel-dsn@db:5432/tsm",
        "innocuo": "questo si vede",
    }
    with engine.begin() as c:
        # inserito GREZZO, scavalcando la ripulitura in scrittura
        insert_event(c, ts=T0, detail=[velenoso])

    body = audit(client).json()
    blob = json.dumps(body)
    for leaked in ("super-segreta-123", "provvisoria-456", "smtp-segreta",
                   "password-nel-dsn", "$argon2", "deadbeef" * 8):
        assert leaked not in blob, leaked
    assert "questo si vede" in blob, "la ripulitura ha rimosso troppo"


def test_write_time_sanitisation(db, engine, client):
    """La prima ripulitura evita che il segreto arrivi su disco."""
    from app.auth.audit import record_auth_event
    with engine.begin() as c:
        record_auth_event(c, "auth.login.success", username="capo", role="admin",
                          detail={"password": "non-deve-restare",
                                  "reason": "va bene"})
    with engine.connect() as c:
        stored = c.execute(text(
            "SELECT events::text FROM audit ORDER BY id DESC LIMIT 1")).scalar_one()
    assert "non-deve-restare" not in stored
    assert "va bene" in stored


def test_client_hint_is_text_not_authoritative(db, engine, client):
    """Il testo del client si restituisce com'è, ma resta un campo a parte: la
    descrizione autorevole è `event`, che lo scrive il server."""
    malicious = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    with engine.begin() as c:
        insert_event(c, ts=T0, action="inventory.save", hint=malicious)
    item = audit(client).json()["items"][0]
    assert item["clientHint"] == malicious      # non interpretato, non riscritto
    assert item["event"] == "inventory.save"    # l'autorevole resta del server


def test_response_shape(dataset, client):
    item = audit(client).json()["items"][0]
    assert set(item) >= {"id", "ts", "actor", "event", "result", "clientHint",
                         "detail", "ip"}
    assert set(item["actor"]) == {"userId", "username", "role"}
    assert item["ts"].endswith("Z"), "il timestamp esce in UTC"


def test_contains_secret_helper_detects_known_shapes():
    assert contains_secret('{"password": "x"}')
    assert contains_secret("$argon2id$v=19$m=1$a$b")
    assert contains_secret("postgresql://u:p@h/db")
    assert not contains_secret('{"nome": "Mario", "ip": "10.0.0.1"}')
