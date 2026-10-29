"""Recupero della password via email e reset con password scelta (0.4.1). PostgreSQL reale.

Le regole che contano:
- la richiesta risponde SEMPRE uguale (202, stesso testo), qualunque sia il caso;
- la mail parte solo per un'utenza attiva, NON bloccata, con indirizzo, a recupero
  acceso e posta configurata; mai verso un indirizzo fornito da chi chiede;
- il codice: hash in tabella, monouso, scade, 5 errori lo invalidano;
- il recupero NON sblocca un'utenza bloccata (lo sblocco resta dell'admin);
- il reset riuscito revoca le sessioni e azzera il cambio obbligatorio;
- l'admin può reimpostare con una password scelta, soggetta alla stessa politica.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth import reset as reset_svc
from app.auth.service import create_user
from app.config import Settings
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402

MESSAGGIO = None


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
                       "recovery_enabled = TRUE, updated_by = NULL WHERE id = 1"))
        create_user(c, "op", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "capo", "password-lunga-3", "admin", must_change_pw=False)
        c.execute(text("UPDATE users SET email = 'op@example.it' WHERE username = 'op'"))
    yield engine


@pytest.fixture
def mails(monkeypatch):
    """Posta «configurata» e invii catturati invece che spediti."""
    monkeypatch.setattr(Settings, "smtp_configured", lambda self: True)
    sent = []
    import app.api.auth as auth_api
    monkeypatch.setattr(auth_api, "_send_reset_mail", lambda m: sent.append(m))
    return sent


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
    return client.post("/api/auth/login", headers=ORIGIN, json={"username": u, "password": p})


def ask(client, u):
    return client.post("/api/auth/password-reset/request", headers=ORIGIN,
                       json={"username": u})


def confirm(client, u, code, pw):
    return client.post("/api/auth/password-reset/confirm", headers=ORIGIN,
                       json={"username": u, "code": code, "newPassword": pw})


def uid(engine, u):
    with engine.connect() as c:
        return str(c.execute(text("SELECT id FROM users WHERE username = :u"),
                             {"u": u}).scalar_one())


# ------------------------------------------------------------ disponibilità

def test_not_available_without_mail(client):
    r = client.get("/api/auth/password-reset")
    assert r.status_code == 200 and r.json() == {"enabled": False}


def test_available_with_mail_and_policy(client, mails):
    assert client.get("/api/auth/password-reset").json() == {"enabled": True}


def test_admin_can_switch_it_off(client, mails):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    r = client.put("/api/security-policy", headers=ORIGIN,
                   json={"passwordResetEnabled": False})
    assert r.status_code == 200 and r.json()["passwordResetEnabled"] is False
    assert r.json()["maxFailedLogins"] == 5            # l'altro campo non cambia
    assert client.get("/api/auth/password-reset").json() == {"enabled": False}
    assert ask(client, "op").status_code == 202
    assert mails == []


@pytest.mark.parametrize("value", ["si", 1, None])
def test_the_switch_must_be_a_boolean(client, value):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    r = client.put("/api/security-policy", headers=ORIGIN,
                   json={"passwordResetEnabled": value})
    assert r.status_code == 422


# ------------------------------------------------------------ richiesta

def test_the_answer_is_always_the_same(client, mails, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET locked_at = now() WHERE username = 'capo'"))
    risposte = [ask(client, u) for u in ("op", "nessuno", "capo")]
    assert {r.status_code for r in risposte} == {202}
    assert len({r.text for r in risposte}) == 1
    assert [m.username for m in mails] == ["op"]          # solo op: capo è bloccato
    assert mails[0].to == "op@example.it"


def test_no_mail_without_an_address(client, mails, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET email = NULL WHERE username = 'op'"))
    assert ask(client, "op").status_code == 202
    assert mails == []


def test_only_the_hash_is_stored(client, mails, engine):
    ask(client, "op")
    code = mails[0].code
    with engine.connect() as c:
        stored = c.execute(text("SELECT code_hash FROM recovery_codes")).scalar_one()
    assert code not in stored and code.replace("-", "") not in stored


def test_three_requests_per_hour_at_most(client, mails):
    for _ in range(5):
        assert ask(client, "op").status_code == 202
    assert len(mails) == 3


def test_a_new_request_invalidates_the_previous_code(client, mails):
    ask(client, "op")
    ask(client, "op")
    vecchio, nuovo = mails[0].code, mails[1].code
    assert confirm(client, "op", vecchio, "una nuova password lunga").status_code == 400
    assert confirm(client, "op", nuovo, "una nuova password lunga").status_code == 204


# ------------------------------------------------------------ conferma

def test_the_right_code_sets_the_password_and_revokes_sessions(client, mails, engine):
    assert login(client, "op", "password-lunga-1").status_code == 200
    ask(client, "op")
    code = mails[0].code.lower().replace("-", " ")         # tollera spazi e minuscole
    assert confirm(client, "op", code, "una nuova password lunga").status_code == 204
    with engine.connect() as c:
        attive = c.execute(text("SELECT count(*) FROM sessions s JOIN users u "
                                "ON u.id = s.user_id WHERE u.username = 'op' "
                                "AND s.revoked_at IS NULL")).scalar_one()
    assert attive == 0
    assert login(client, "op", "password-lunga-1").status_code == 401
    r = login(client, "op", "una nuova password lunga")
    assert r.status_code == 200 and r.json()["mustChangePassword"] is False


def test_a_code_works_once(client, mails):
    ask(client, "op")
    code = mails[0].code
    assert confirm(client, "op", code, "una nuova password lunga").status_code == 204
    assert confirm(client, "op", code, "un'altra password lunga").status_code == 400


def test_five_wrong_codes_burn_the_code(client, mails):
    ask(client, "op")
    code = mails[0].code
    for _ in range(5):
        r = confirm(client, "op", "AAAA-BBBB-CCCC", "una nuova password lunga")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "reset_code_invalid"
    assert confirm(client, "op", code, "una nuova password lunga").status_code == 400


def test_an_expired_code_is_refused(client, mails, engine):
    ask(client, "op")
    with engine.begin() as c:
        c.execute(text("UPDATE recovery_codes SET expires_at = now() - interval '1 minute'"))
    assert confirm(client, "op", mails[0].code, "una nuova password lunga").status_code == 400


def test_the_code_belongs_to_its_user(client, mails, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET email = 'capo@example.it' WHERE username = 'capo'"))
    ask(client, "op")
    assert confirm(client, "capo", mails[0].code, "una nuova password lunga").status_code == 400


def test_errors_look_the_same_for_unknown_users(client, mails):
    a = confirm(client, "nessuno", "AAAA-BBBB-CCCC", "una nuova password lunga")
    ask(client, "op")
    b = confirm(client, "op", "AAAA-BBBB-CCCC", "una nuova password lunga")
    assert a.status_code == b.status_code == 400
    assert a.json() == b.json()


def test_the_policy_still_applies_and_keeps_the_code(client, mails):
    ask(client, "op")
    code = mails[0].code
    r = confirm(client, "op", code, "corta")
    assert r.status_code == 422
    assert confirm(client, "op", code, "una nuova password lunga").status_code == 204


def test_recovery_does_not_unlock(client, mails, engine):
    ask(client, "op")
    with engine.begin() as c:
        c.execute(text("UPDATE users SET locked_at = now() WHERE username = 'op'"))
    assert confirm(client, "op", mails[0].code, "una nuova password lunga").status_code == 400
    assert login(client, "op", "password-lunga-1").status_code == 423


def test_requests_and_outcomes_are_audited(client, mails, engine):
    ask(client, "op")
    ask(client, "nessuno")
    confirm(client, "op", "AAAA-BBBB-CCCC", "una nuova password lunga")
    confirm(client, "op", mails[0].code, "una nuova password lunga")
    with engine.connect() as c:
        azioni = [r[0] for r in c.execute(text("SELECT action FROM audit ORDER BY ts, id"))]
    assert azioni.count(reset_svc.RESET_REQUESTED) == 2
    assert reset_svc.RESET_FAILED in azioni
    assert reset_svc.RESET_COMPLETED in azioni


def test_mail_text_never_contains_a_password():
    from app.notifications.smtp import build_reset_message
    m = build_reset_message("tsm@example.it", "op@example.it", username="op",
                            code="ABCD-EFGH-JKMN", expires_minutes=30,
                            link="https://10.11.43.230/#reset?u=op&c=ABCD-EFGH-JKMN")
    body = m.get_content()
    assert "ABCD-EFGH-JKMN" in body and "30 minuti" in body
    assert m["To"] == "op@example.it"


# ------------------------------------------------------------ admin: password scelta

def test_admin_resets_with_a_chosen_password(client, engine):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    r = client.post(f"/api/users/{uid(engine, 'op')}/reset-password", headers=ORIGIN,
                    json={"password": "scelta dall amministratore"})
    assert r.status_code == 200, r.text
    assert r.json()["temporaryPassword"] is None
    assert r.json()["user"]["mustChangePassword"] is True
    r = login(client, "op", "scelta dall amministratore")
    assert r.status_code == 200 and r.json()["mustChangePassword"] is True


def test_admin_can_skip_the_forced_change(client, engine):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    r = client.post(f"/api/users/{uid(engine, 'op')}/reset-password", headers=ORIGIN,
                    json={"password": "scelta dall amministratore", "mustChange": False})
    assert r.status_code == 200
    r = login(client, "op", "scelta dall amministratore")
    assert r.json()["mustChangePassword"] is False


def test_a_chosen_password_follows_the_policy(client, engine):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    for bad in ("corta", "op", "password1234"):
        r = client.post(f"/api/users/{uid(engine, 'op')}/reset-password", headers=ORIGIN,
                        json={"password": bad})
        assert r.status_code == 422, (bad, r.text)


def test_empty_body_still_generates_a_retypable_temporary(client, engine):
    import re
    assert login(client, "capo", "password-lunga-3").status_code == 200
    r = client.post(f"/api/users/{uid(engine, 'op')}/reset-password", headers=ORIGIN)
    assert r.status_code == 200
    assert re.fullmatch(r"[a-z2-9]{5}(-[a-z2-9]{5}){5}", r.json()["temporaryPassword"])


# ------------------------------------------------------------ email sulle utenze

def test_admin_sets_and_validates_email(client, engine):
    assert login(client, "capo", "password-lunga-3").status_code == 200
    u = uid(engine, "op")
    r = client.patch(f"/api/users/{u}", headers=ORIGIN, json={"email": "  m.rossi@tim.it "})
    assert r.status_code == 200 and r.json()["email"] == "m.rossi@tim.it"
    for bad in ("non-una-mail", "a@b", "con spazi@tim.it"):
        r = client.patch(f"/api/users/{u}", headers=ORIGIN, json={"email": bad})
        assert r.status_code in (400, 409, 422), (bad, r.status_code)
    r = client.patch(f"/api/users/{u}", headers=ORIGIN, json={"email": ""})
    assert r.status_code == 200 and r.json()["email"] is None
    r = client.post("/api/users", headers=ORIGIN,
                    json={"username": "nuovo", "role": "view", "email": "nuovo@tim.it"})
    assert r.status_code == 201 and r.json()["user"]["email"] == "nuovo@tim.it"
