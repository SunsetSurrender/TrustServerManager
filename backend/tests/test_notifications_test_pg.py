"""Invio di prova: non è un relay, è limitato, e non mente sull'esito.

PostgreSQL reale per il limitatore (che è durevole e concorrente) e un finto per
SMTP — qui il doppio è quello giusto: un server di posta vero renderebbe il test
dipendente dalla rete, e ciò che si vuole verificare è cosa fa il NOSTRO codice
quando quello di posta risponde in un certo modo.
"""
from __future__ import annotations

import json
import os
import smtplib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.main import app
from app.settings.schema import DEFAULTS

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

#: Client HTTPS e `Origin` corrispondente: vedi il commento in conftest.py.
from conftest import ORIGIN, api_client  # noqa: E402

RECIPIENTS = ["uno@example.internal", "due@example.internal",
              "tre@example.internal", "quattro@example.internal"]


# ==================================================================
# doppio del server di posta
# ==================================================================

class FakeSMTP:
    """Registra ciò che è stato inviato. Un'istanza per connessione."""

    inviati: list = []
    esplodi_con: Exception | None = None
    connessioni: int = 0

    def __init__(self, host, port, timeout=None, context=None):
        type(self).connessioni += 1
        self.host, self.port, self.timeout = host, port, timeout
        self.autenticato = None
        if type(self).esplodi_con is not None:
            raise type(self).esplodi_con

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def ehlo(self):
        pass

    def login(self, user, password):
        self.autenticato = (user, password)

    def send_message(self, msg):
        type(self).inviati.append(msg)

    @classmethod
    def azzera(cls):
        cls.inviati = []
        cls.esplodi_con = None
        cls.connessioni = 0


@pytest.fixture
def smtp(monkeypatch):
    """SMTP configurato e funzionante, salvo diversa indicazione del test."""
    import app.notifications.smtp as mod
    from app.config import get_settings

    FakeSMTP.azzera()
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", FakeSMTP)

    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_host", "relay.interno", raising=False)
    monkeypatch.setattr(cfg, "smtp_sender", "ced@example.internal", raising=False)
    monkeypatch.setattr(cfg, "smtp_username", "", raising=False)
    monkeypatch.setattr(cfg, "smtp_tls_mode", "starttls", raising=False)
    yield FakeSMTP
    FakeSMTP.azzera()


# ==================================================================
# infrastruttura
# ==================================================================

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
    data = json.loads(json.dumps(DEFAULTS))
    data["notifications"]["recipients"] = RECIPIENTS
    data["notifications"]["enabled"] = True
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = :d, version = 1, "
                       "updated_by = NULL WHERE id = 1"), {"d": json.dumps(data)})
        c.execute(text("DELETE FROM notification_test_attempts"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
        create_user(c, "vice", "password-lunga-3", "admin", must_change_pw=False)
        create_user(c, "op", "password-lunga-2", "edit", must_change_pw=False)
    yield engine


def _client(engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    return api_client(app)


@pytest.fixture
def client(db, engine):
    with _client(engine) as c:
        r = c.post("/api/auth/login", headers=ORIGIN,
                   json={"username": "capo", "password": "password-lunga-1"})
        assert r.status_code == 200
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit"))
        yield c
    app.dependency_overrides.clear()


def prova(c: TestClient, body=None):
    return c.post("/api/notifications/test",
                  json={} if body is None else body, headers=ORIGIN)


def audit_rows(engine, action="notifications.test"):
    with engine.begin() as c:
        return c.execute(text("""
            SELECT actor_username, result, events FROM audit
             WHERE action = :a ORDER BY id
        """), {"a": action}).all()


# ==================================================================
# non è un relay
# ==================================================================

@pytest.mark.parametrize("body", [
    {"to": "estraneo@altrove.com"},
    {"recipients": ["estraneo@altrove.com"]},
    {"subject": "compra ora"},
    {"body": "testo arbitrario"},
    {"smtp": {"host": "relay.attaccante"}},
    {"qualsiasi": 1},
])
def test_the_endpoint_accepts_no_parameters(client, smtp, body):
    """Il controllo che impedisce all'endpoint di diventare un relay autenticato.

    Non basta «ignorare» i campi in più: rifiutarli è ciò che rende impossibile
    che un giorno uno di essi venga letto per sbaglio."""
    r = prova(client, body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "unexpected_fields"
    assert smtp.inviati == [], "non deve partire niente"


def test_recipients_come_from_saved_settings_only(client, smtp):
    prova(client)
    assert len(smtp.inviati) == 1
    destinatari = smtp.inviati[0]["To"]
    for address in destinatari.split(", "):
        assert address in RECIPIENTS


def test_test_send_is_limited_to_a_subset_of_recipients(client, smtp):
    """Quattro destinatari configurati, tre nel messaggio di prova: se il relay
    funziona per tre funziona per tutti, e la differenza è solo quanta posta si
    genera premendo un pulsante."""
    r = prova(client)
    assert r.status_code == 200
    assert r.json()["recipients"] == 3
    assert r.json()["configuredRecipients"] == 4
    assert len(smtp.inviati[0]["To"].split(", ")) == 3


def test_subject_and_body_are_server_defined(client, smtp):
    prova(client)
    msg = smtp.inviati[0]
    assert msg["Subject"] == "Trust Server Manager — messaggio di prova"
    assert msg["From"] == "ced@example.internal"
    testo = msg.get_content()
    assert "capo" in testo, "chi riceve deve capire da chi arriva"
    assert "prova" in testo.lower()


def test_a_successful_test_sends_exactly_once(client, smtp):
    r = prova(client)
    assert r.status_code == 200
    assert r.json()["sent"] is True
    assert len(smtp.inviati) == 1
    assert smtp.connessioni == 1


# ==================================================================
# autorizzazione
# ==================================================================

def test_non_admin_is_denied(db, engine, smtp):
    with _client(engine) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "op", "password": "password-lunga-2"})
        r = prova(c)
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "forbidden_for_role"
        assert smtp.inviati == []
    app.dependency_overrides.clear()


def test_unauthenticated_is_denied(db, engine, smtp):
    with _client(engine) as c:
        assert prova(c).status_code == 401
        assert smtp.inviati == []
    app.dependency_overrides.clear()


def test_foreign_origin_is_refused(client, smtp):
    """Stessa protezione delle altre mutazioni (§8.27): un endpoint che manda
    posta è esattamente ciò che un sito terzo vorrebbe far scattare."""
    r = client.post("/api/notifications/test", json={},
                    headers={"Origin": "https://attaccante.example"})
    assert r.status_code == 403
    assert r.json()["code"] == "origin_not_allowed"
    assert smtp.inviati == []


# ==================================================================
# limitazione
# ==================================================================

def test_rate_limit_stops_the_fourth_attempt(client, smtp):
    for i in range(3):
        assert prova(client).status_code == 200, f"tentativo {i}"
    r = prova(client)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "notification_test_rate_limited"
    assert r.headers.get("Retry-After")
    assert len(smtp.inviati) == 3, "il quarto non deve partire"


def test_rate_limit_is_recorded_before_sending(client, smtp, engine):
    """La riga si scrive PRIMA dell'invio: registrandola dopo, dieci richieste
    simultanee troverebbero tutte il contatore a zero."""
    prova(client)
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM notification_test_attempts")).scalar_one()
    assert n == 1


def test_a_failed_send_still_consumes_a_slot(client, smtp):
    """Altrimenti un relay lento diventerebbe un modo per non essere contati, ed
    è proprio il caso in cui qualcuno riprova in continuazione."""
    smtp.esplodi_con = TimeoutError("troppo lento")
    for _ in range(3):
        assert prova(client).status_code == 503
    smtp.esplodi_con = None
    assert prova(client).status_code == 429


def test_the_blocked_attempt_is_audited_as_denied(client, smtp, engine):
    for _ in range(3):
        prova(client)
    prova(client)
    rows = audit_rows(engine)
    assert rows[-1][1] == "denied"
    assert rows[-1][2][0]["reason"] == "rate_limited"


def test_global_limit_covers_more_than_one_admin(db, engine, smtp, monkeypatch):
    """Il limite per attore da solo si moltiplica per il numero di
    amministratori: il relay vedrebbe comunque un volume inatteso."""
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "notification_test_max_per_actor", 5, raising=False)
    monkeypatch.setattr(cfg, "notification_test_max_global", 3, raising=False)

    with _client(engine) as a, _client(engine) as b:
        a.post("/api/auth/login", headers=ORIGIN,
               json={"username": "capo", "password": "password-lunga-1"})
        b.post("/api/auth/login", headers=ORIGIN,
               json={"username": "vice", "password": "password-lunga-3"})
        assert prova(a).status_code == 200
        assert prova(a).status_code == 200
        assert prova(b).status_code == 200
        r = prova(b)
        assert r.status_code == 429
    app.dependency_overrides.clear()


def test_limiter_failure_denies_the_send(client, smtp, monkeypatch):
    """Si fallisce chiuso: un limite che non si può contare non esiste, e per un
    endpoint che genera posta è la peggiore delle inesistenze."""
    import app.api.notifications as route

    def esplodi(*a, **kw):
        raise route.NotificationLimiterUnavailable()

    monkeypatch.setattr(route, "reserve_slot", esplodi)
    r = prova(client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "notification_test_unavailable"
    assert smtp.inviati == []


# ==================================================================
# esiti SMTP, ripuliti
# ==================================================================

def test_smtp_exception_is_an_oserror_subclass():
    """Il fatto da cui dipende l'ordine delle clausole `except` in smtp.py.

    `smtplib.SMTPException` deriva da `OSError`: un `except OSError` messo prima
    intercetta anche gli errori di protocollo e li chiama «connessione non
    riuscita», mandando chi legge a controllare la rete mentre il relay ha
    risposto no. Se un giorno la libreria cambiasse gerarchia, questo test lo
    dice prima che lo dica una diagnosi sbagliata in produzione."""
    assert issubclass(smtplib.SMTPException, OSError)
    assert issubclass(TimeoutError, OSError)


@pytest.mark.parametrize("errore,reason", [
    (TimeoutError("timeout verso relay.interno"), "timeout"),
    (ConnectionRefusedError("relay.interno:587 rifiutata"), "connection_failed"),
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 utente=ced password errata"),
     "auth_failed"),
    (smtplib.SMTPServerDisconnected("relay.interno ha chiuso"), "connection_failed"),
    (smtplib.SMTPException("550 relay.interno rifiuta"), "protocol_error"),
])
def test_smtp_failures_are_sanitised(client, smtp, errore, reason):
    """Nella risposta va una CATEGORIA da un elenco chiuso. Il testo
    dell'eccezione — che contiene host, utenza e risposta del relay — resta nei
    log, dove serve a chi opera e non a chi sonda."""
    smtp.esplodi_con = errore
    r = prova(client)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["code"] == "smtp_send_failed"
    assert detail["reason"] == reason
    for frammento in ("relay.interno", "587", "password", "5.7.8", "Traceback"):
        assert frammento not in r.text, frammento


def test_smtp_not_configured_is_its_own_code(client, smtp, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "smtp_host", "", raising=False)
    r = prova(client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "smtp_not_configured"
    assert smtp.inviati == []


def test_no_recipients_is_its_own_code(client, smtp, engine):
    """Non è un guasto del trasporto ed è l'amministratore a poterlo risolvere:
    un codice suo evita di mandarlo a cercare un problema di rete."""
    data = json.loads(json.dumps(DEFAULTS))
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = :d WHERE id = 1"),
                  {"d": json.dumps(data)})
    r = prova(client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "no_recipients_configured"
    assert smtp.inviati == []


def test_response_never_contains_smtp_credentials(client, smtp, monkeypatch):
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_username", "ced-relay", raising=False)
    monkeypatch.setattr(type(cfg), "smtp_password",
                        lambda self: "password-smtp-segretissima")
    r = prova(client)
    assert r.status_code == 200
    assert "password-smtp-segretissima" not in r.text
    assert "ced-relay" not in r.text


def test_credentials_are_used_but_never_audited(client, smtp, engine, monkeypatch):
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_username", "ced-relay", raising=False)
    monkeypatch.setattr(type(cfg), "smtp_password",
                        lambda self: "password-smtp-segretissima")
    prova(client)
    with engine.begin() as c:
        blob = json.dumps(c.execute(text("SELECT events FROM audit")).all(),
                          default=str)
    assert "password-smtp-segretissima" not in blob
    assert "ced-relay" not in blob


# ==================================================================
# audit e asimmetria
# ==================================================================

def test_a_successful_send_is_audited(client, smtp, engine):
    r = prova(client)
    assert r.json()["auditRecorded"] is True
    rows = audit_rows(engine)
    assert len(rows) == 1
    assert rows[0][0] == "capo" and rows[0][1] == "success"
    assert rows[0][2][0]["recipients"] == 3


def test_a_failed_send_is_audited_as_failure(client, smtp, engine):
    smtp.esplodi_con = TimeoutError("lento")
    prova(client)
    rows = audit_rows(engine)
    assert rows[0][1] == "failure"
    assert rows[0][2][0]["reason"] == "timeout"


def test_audit_failure_after_a_successful_send_does_not_report_failure(
        client, smtp, monkeypatch):
    """L'asimmetria, ed è il test che la fissa.

    Il messaggio è PARTITO. Rispondere «non riuscito» perché non si è potuta
    scrivere la riga di registro farebbe riprovare il client, e ogni tentativo
    manderebbe un altro messaggio vero a persone vere. Si risponde successo, si
    dichiara che la traccia manca, e il guasto va nei log."""
    import app.api.notifications as route

    def esplodi(*a, **kw):
        raise RuntimeError("audit non disponibile")

    monkeypatch.setattr(route, "record_auth_event", esplodi)
    r = prova(client)

    assert r.status_code == 200, "un invio riuscito non diventa un errore"
    assert r.json()["sent"] is True
    assert r.json()["auditRecorded"] is False
    assert len(smtp.inviati) == 1, "e soprattutto: nessun secondo invio"


def test_audit_failure_on_a_failed_send_keeps_the_failure(client, smtp, monkeypatch):
    """Nell'altro verso l'asimmetria non c'è: senza niente di irreversibile da
    proteggere, un audit non riuscito non cambia la risposta."""
    import app.api.notifications as route

    smtp.esplodi_con = TimeoutError("lento")
    monkeypatch.setattr(route, "record_auth_event",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no")))
    r = prova(client)
    assert r.status_code == 503
    assert r.json()["detail"]["reason"] == "timeout"


# ==================================================================
# indipendenza dalle impostazioni
# ==================================================================

def test_a_test_send_never_changes_the_settings(client, smtp):
    before = client.get("/api/settings").json()
    prova(client)
    after = client.get("/api/settings").json()
    assert before["version"] == after["version"]
    assert before["notifications"] == after["notifications"]


def test_a_failed_test_send_never_changes_the_settings(client, smtp):
    before = client.get("/api/settings").json()
    smtp.esplodi_con = TimeoutError("lento")
    prova(client)
    after = client.get("/api/settings").json()
    assert before == after


def test_test_send_uses_committed_settings_not_the_pending_ones(client, smtp):
    """I destinatari sono quelli SALVATI. Provare una configurazione non ancora
    committata direbbe che funziona qualcosa che non è quello che verrà usato."""
    r = client.put("/api/settings",
                   json={"notifications": {**DEFAULTS["notifications"],
                                           "recipients": ["nuovo@example.internal"]}},
                   headers={**ORIGIN, "If-Match": '"1"'})
    assert r.status_code == 200
    prova(client)
    assert smtp.inviati[0]["To"] == "nuovo@example.internal"
