"""Semantica transazionale dell'autenticazione e della gestione utenze.

Cinque proprietà, ognuna verificata iniettando un guasto nel punto in cui
conterebbe:

  1. sessione + audit del login riuscito  → ATOMICI
  2. password + revoca + audit            → ATOMICI
  3. mutazione utenza + revoca + audit    → ATOMICI
  4. audit del login FALLITO              → indipendente, e non trasforma un 401 in 500
  5. persistenza del limitatore           → se non scrive, il login FALLISCE CHIUSO

Le prime tre stanno nella transazione della richiesta e devono cadere insieme. La
quarta deve sopravvivere al rollback (§8.25) ma non poter peggiorare la risposta.
La quinta è l'unica dove un guasto deve NEGARE l'accesso: se i tentativi non si
possono contare, il limitatore non esiste, e non esistere in silenzio significa
tentativi illimitati.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.auth import service as auth_service
from app.auth import users as users_svc
from app.auth.service import (
    InvalidCredentials,
    change_own_password,
    create_user,
    login,
)
from app.inventory import Actor

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

BOOM = RuntimeError("guasto iniettato")


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
        create_user(c, "op", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "capo", "password-lunga-2", "admin", must_change_pw=False)
        create_user(c, "vice", "password-lunga-3", "admin", must_change_pw=False)
    yield engine


def counts(engine) -> dict:
    with engine.connect() as c:
        return {
            "sessions": c.execute(text("SELECT count(*) FROM sessions")).scalar_one(),
            "live": c.execute(text("SELECT count(*) FROM sessions "
                                   "WHERE revoked_at IS NULL")).scalar_one(),
            "audit": c.execute(text("SELECT count(*) FROM audit")).scalar_one(),
            "attempts": c.execute(text("SELECT count(*) FROM login_attempts")).scalar_one(),
        }


def uid_of(engine, username: str):
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM users WHERE username = :u"),
                         {"u": username}).scalar_one()


def pw_hash(engine, username: str) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT password_hash FROM users WHERE username = :u"),
                         {"u": username}).scalar_one()


# ==================================================================
# 1. sessione + audit del login riuscito: atomici
# ==================================================================

def test_login_success_session_and_audit_are_atomic(db, engine, monkeypatch):
    before = counts(engine)

    # L'audit del successo è l'ultima scrittura: se fallisce, non deve restare la
    # sessione. Una sessione senza la sua riga di audit è un accesso non tracciato.
    monkeypatch.setattr(auth_service, "record_auth_event",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))

    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            login(c, "op", "password-lunga-1", ip="10.0.0.1")

    after = counts(engine)
    assert after["sessions"] == before["sessions"], "sessione sopravvissuta senza audit"
    assert after["audit"] == before["audit"]


def test_login_success_writes_session_and_audit_together(db, engine):
    with engine.begin() as c:
        token, user = login(c, "op", "password-lunga-1", ip="10.0.0.1")
    assert token
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sessions "
                              "WHERE revoked_at IS NULL")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM audit "
                              "WHERE action = 'auth.login.success'")).scalar_one() == 1
        # e il tentativo riuscito è registrato nella stessa transazione
        assert c.execute(text("SELECT count(*) FROM login_attempts "
                              "WHERE success = TRUE")).scalar_one() == 1


# ==================================================================
# 2. password + revoca + audit: atomici
# ==================================================================

def test_password_change_is_atomic_with_revocation_and_audit(db, engine, monkeypatch):
    with engine.begin() as c:
        login(c, "op", "password-lunga-1")
    original = pw_hash(engine, "op")
    before = counts(engine)
    uid = uid_of(engine, "op")

    monkeypatch.setattr(auth_service, "record_auth_event",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))

    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            change_own_password(c, uid, "password-lunga-1", "password-nuova-lunga")

    # Niente a metà: password invariata, sessione ancora viva, audit invariato.
    assert pw_hash(engine, "op") == original
    after = counts(engine)
    assert after["live"] == before["live"], "sessione revocata senza cambio password"
    assert after["audit"] == before["audit"]

    # La password vecchia funziona ancora: il cambio non è avvenuto a metà.
    # `undo()` prima, altrimenti è questo login a sollevare il guasto iniettato.
    monkeypatch.undo()
    with engine.begin() as c:
        login(c, "op", "password-lunga-1")


def test_password_change_rollback_leaves_sessions_usable(db, engine, monkeypatch):
    with engine.begin() as c:
        token, _ = login(c, "op", "password-lunga-1")
    uid = uid_of(engine, "op")

    monkeypatch.setattr(auth_service, "revoke_all_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            change_own_password(c, uid, "password-lunga-1", "password-nuova-lunga")

    monkeypatch.undo()
    # la sessione è ancora risolvibile: la revoca non è avvenuta a metà
    with engine.begin() as c:
        assert auth_service.resolve_session(c, token).username == "op"


# ==================================================================
# 3. mutazione utenza + revoca + audit: atomici
# ==================================================================

def test_disable_is_atomic_with_revocation_and_audit(db, engine, monkeypatch):
    with engine.begin() as c:
        login(c, "op", "password-lunga-1")
    before = counts(engine)
    uid = uid_of(engine, "op")
    admin = Actor(username="capo", role="admin", user_id=uid_of(engine, "capo"))

    monkeypatch.setattr(users_svc, "record_auth_event",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            users_svc.set_disabled(c, uid, True, actor=admin)

    with engine.connect() as c:
        disabled = c.execute(text("SELECT disabled_at FROM users WHERE id = :i"),
                             {"i": uid}).scalar_one()
    assert disabled is None, "utenza disattivata senza audit"
    assert counts(engine)["live"] == before["live"], "sessioni revocate senza disable"


def test_reset_password_is_atomic(db, engine, monkeypatch):
    with engine.begin() as c:
        login(c, "op", "password-lunga-1")
    original = pw_hash(engine, "op")
    before = counts(engine)
    uid = uid_of(engine, "op")
    admin = Actor(username="capo", role="admin", user_id=uid_of(engine, "capo"))

    monkeypatch.setattr(users_svc, "record_auth_event",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            users_svc.reset_password(c, uid, actor=admin)

    assert pw_hash(engine, "op") == original
    assert counts(engine)["live"] == before["live"]


def test_role_change_is_atomic(db, engine, monkeypatch):
    uid = uid_of(engine, "vice")
    admin = Actor(username="capo", role="admin", user_id=uid_of(engine, "capo"))
    monkeypatch.setattr(users_svc, "record_auth_event",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            users_svc.update_user(c, uid, actor=admin, role="view")
    with engine.connect() as c:
        assert c.execute(text("SELECT role FROM users WHERE id = :i"),
                         {"i": uid}).scalar_one() == "admin"


# ==================================================================
# 4. audit del login fallito: indipendente, e non peggiora la risposta
# ==================================================================

def test_failed_login_audit_survives_the_rollback(db, engine):
    """La transazione della richiesta viene annullata dal 401: il tentativo e la
    riga di audit devono restare, altrimenti il limitatore non conterebbe mai."""
    with pytest.raises(InvalidCredentials):
        with engine.begin() as c:
            login(c, "op", "sbagliata", ip="10.0.0.9")
            raise AssertionError("login non ha sollevato")   # pragma: no cover

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM login_attempts "
                              "WHERE success = FALSE")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM audit "
                              "WHERE action = 'auth.login.failure'")).scalar_one() == 1


def test_failed_login_audit_failure_does_not_become_a_500(db, engine, monkeypatch):
    """Se l'AUDIT del fallimento non si scrive, la risposta resta 401.

    Non riuscire a scrivere una riga di registro è un problema; trasformare
    «credenziali errate» in «errore del server» è peggio, perché nasconde al
    client l'informazione vera e fa scattare i suoi ripristini automatici.
    """
    real = auth_service.record_auth_event

    def only_failure_breaks(conn, action, **kw):
        if action == "auth.login.failure":
            raise BOOM
        return real(conn, action, **kw)

    monkeypatch.setattr(auth_service, "record_auth_event", only_failure_breaks)

    with pytest.raises(InvalidCredentials):        # NON RuntimeError
        with engine.begin() as c:
            login(c, "op", "sbagliata", ip="10.0.0.9")

    # il tentativo è comunque stato contato: il limitatore continua a funzionare
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM login_attempts "
                              "WHERE success = FALSE")).scalar_one() == 1


# ==================================================================
# 5. limitatore: se non persiste, il login FALLISCE CHIUSO
# ==================================================================

def test_rate_limiter_write_failure_fails_login_closed(db, engine, monkeypatch):
    """Se il contatore dei tentativi non si scrive, non si risponde 401.

    Un 401 direbbe al chiamante «credenziali errate, riprova» mentre il
    limitatore è di fatto disattivato: tentativi illimitati senza che nessuno se
    ne accorga. Si nega l'accesso con un errore di servizio, che è la scelta
    conservativa e visibile.
    """
    monkeypatch.setattr(auth_service, "record_attempt",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))

    with pytest.raises(auth_service.RateLimiterUnavailable):
        with engine.begin() as c:
            login(c, "op", "sbagliata", ip="10.0.0.9")


def test_rate_limiter_read_failure_fails_login_closed(db, engine, monkeypatch):
    """Anche non poter LEGGERE il contatore è un limitatore assente."""
    monkeypatch.setattr(auth_service, "check_rate_limit",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))
    with pytest.raises(Exception) as exc:
        with engine.begin() as c:
            login(c, "op", "password-lunga-1")
    assert not isinstance(exc.value, InvalidCredentials)


def test_rate_limiter_failure_does_not_leak_whether_credentials_were_right(
        db, engine, monkeypatch):
    """Con il limitatore guasto la risposta è la stessa per credenziali giuste e
    sbagliate: un errore di servizio non deve diventare un oracolo."""
    monkeypatch.setattr(auth_service, "record_attempt",
                        lambda *a, **k: (_ for _ in ()).throw(BOOM))

    errors = []
    for password in ("password-lunga-1", "del-tutto-sbagliata"):
        try:
            with engine.begin() as c:
                login(c, "op", password)
        except Exception as e:
            errors.append(type(e).__name__)
    # la password giusta non arriva nemmeno a creare la sessione
    assert errors == ["RateLimiterUnavailable", "RateLimiterUnavailable"], errors
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 0
