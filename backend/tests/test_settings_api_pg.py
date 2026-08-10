"""API delle impostazioni: concorrenza ottimistica, validazione, riservatezza.

PostgreSQL reale. La concorrenza è il punto di questa suite e non si dimostra
con un finto: il conflitto nasce da un `UPDATE` che incrementa una colonna sotto
un `FOR UPDATE`, cioè da comportamento del database.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.audit.sanitize import contains_secret
from app.auth.service import create_user
from app.main import app
from app.settings.schema import DEFAULTS, default_document

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402  (client HTTPS: vedi conftest)


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
        # Ordine obbligato: `settings.updated_by` e
        # `notification_test_attempts.actor_user_id` puntano a `users`, quindi si
        # sganciano PRIMA di cancellare le utenze — altrimenti è la chiave
        # esterna a far fallire il fixture, e il test sembra rotto per un motivo
        # che non c'entra.
        c.execute(text("UPDATE settings SET data = :d, version = 1, "
                       "updated_by = NULL, updated_at = now() WHERE id = 1"),
                  {"d": json.dumps(DEFAULTS)})
        c.execute(text("DELETE FROM notification_test_attempts"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
        create_user(c, "vice", "password-lunga-3", "admin", must_change_pw=False)
        create_user(c, "op", "password-lunga-2", "edit", must_change_pw=False)
        create_user(c, "nuovo", "password-lunga-4", "admin", must_change_pw=True)
    yield engine


def _client(engine, **kw):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    return api_client(app, **kw)


def login(c: TestClient, username: str, password: str) -> None:
    r = c.post("/api/auth/login", headers=ORIGIN,
               json={"username": username, "password": password})
    assert r.status_code == 200, r.text


@pytest.fixture
def client(db, engine):
    with _client(engine) as c:
        login(c, "capo", "password-lunga-1")
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def other_admin(db, engine):
    """Una SECONDA sessione, con il proprio cookie. Il conflitto fra due
    amministratori non si simula riusando lo stesso client."""
    with _client(engine) as c:
        login(c, "vice", "password-lunga-3")
        yield c
    app.dependency_overrides.clear()


def notif(**over) -> dict:
    n = json.loads(json.dumps(DEFAULTS["notifications"]))
    n.update(over)
    return {"notifications": n}


def put(c: TestClient, payload, etag, **kw):
    headers = dict(ORIGIN)
    if etag is not None:
        headers["If-Match"] = etag
    headers.update(kw.pop("headers", {}))
    return c.put("/api/settings", json=payload, headers=headers, **kw)


def current(c: TestClient):
    r = c.get("/api/settings")
    assert r.status_code == 200, r.text
    return r.json(), r.headers["ETag"]


# ==================================================================
# lettura
# ==================================================================

def test_get_returns_typed_document_and_etag(client):
    body, etag = current(client)
    assert body["version"] == 1
    assert etag == '"1"'
    assert body["notifications"] == default_document()["notifications"]
    assert set(body) == {"version", "notifications", "smtp", "updatedAt"}


def test_get_exposes_only_smtp_configured_flag(client):
    """`smtp` contiene un booleano e nient'altro: non l'host, non l'utenza, non
    il percorso del secret. Un oggetto con dei parametri dentro è l'oggetto in
    cui un giorno qualcuno aggiunge `password`."""
    body, _ = current(client)
    assert set(body["smtp"]) == {"configured"}
    assert isinstance(body["smtp"]["configured"], bool)


def test_get_is_not_cacheable(client):
    r = client.get("/api/settings")
    assert r.headers["Cache-Control"] == "no-store"


def test_get_requires_authentication(db, engine):
    with _client(engine) as c:
        assert c.get("/api/settings").status_code == 401
    app.dependency_overrides.clear()


def test_get_requires_admin(db, engine):
    with _client(engine) as c:
        login(c, "op", "password-lunga-2")
        r = c.get("/api/settings")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "forbidden_for_role"
    app.dependency_overrides.clear()


def test_temporary_password_blocks_settings(db, engine):
    """Sessione con password provvisoria: valida ma ristretta (§8.26)."""
    with _client(engine) as c:
        c.post("/api/auth/login", headers=ORIGIN,
               json={"username": "nuovo", "password": "password-lunga-4"})
        r = c.get("/api/settings")
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "password_change_required"
    app.dependency_overrides.clear()


# ==================================================================
# concorrenza ottimistica
# ==================================================================

def test_save_increments_revision_and_returns_new_etag(client):
    _, etag = current(client)
    r = put(client, notif(recipients=["team@example.internal"]), etag)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["changed"] is True
    assert r.headers["ETag"] == '"2"'


def test_second_admin_with_a_stale_revision_gets_409(client, other_admin):
    """Il caso che giustifica tutto il meccanismo: due amministratori leggono la
    stessa revisione, il primo salva, il secondo NON deve sovrascriverlo."""
    _, etag_a = current(client)
    _, etag_b = current(other_admin)
    assert etag_a == etag_b

    assert put(client, notif(recipients=["primo@example.it"]), etag_a).status_code == 200

    r = put(other_admin, notif(recipients=["secondo@example.it"]), etag_b)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "settings_version_conflict"
    assert detail["currentVersion"] == 2
    # L'ETag corrente viaggia anche nel conflitto: il client può ricaricare
    # senza una seconda GET.
    assert r.headers["ETag"] == '"2"'


def test_the_conflicting_save_changed_nothing(client, other_admin):
    """Un 409 non deve lasciare tracce: se il secondo salvataggio avesse scritto
    e poi segnalato il conflitto, il danno sarebbe già fatto."""
    _, etag = current(client)
    put(client, notif(recipients=["primo@example.it"]), etag)
    put(other_admin, notif(recipients=["secondo@example.it"]), etag)

    body, _ = current(client)
    assert body["notifications"]["recipients"] == ["primo@example.it"]
    assert body["version"] == 2


def test_second_admin_succeeds_after_reloading(client, other_admin):
    _, etag = current(client)
    put(client, notif(recipients=["primo@example.it"]), etag)
    _, fresh = current(other_admin)
    r = put(other_admin, notif(recipients=["primo@example.it",
                                           "secondo@example.it"]), fresh)
    assert r.status_code == 200
    assert r.json()["version"] == 3


def test_canonical_noop_does_not_increment_the_revision(client):
    """Salvare senza cambiare niente NON fa salire la revisione.

    Se salisse, aprire la schermata e premere Salva farebbe fallire il
    salvataggio di un collega che aveva la pagina aperta: un conflitto
    inventato, che insegna a ignorare quelli veri."""
    body, etag = current(client)
    r = put(client, {"notifications": body["notifications"]}, etag)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    assert r.json()["changed"] is False
    assert r.headers["ETag"] == '"1"'


def test_reordered_but_equivalent_payload_is_a_noop(client):
    """`[90, 7, 30]` e `[7, 30, 90]` sono la stessa configurazione: la
    canonicalizzazione lo riconosce e la revisione resta ferma."""
    body, etag = current(client)
    payload = {"notifications": dict(body["notifications"])}
    payload["notifications"]["warningDays"] = list(
        reversed(body["notifications"]["warningDays"])) or [30]
    r = put(client, payload, etag)
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["version"] == 1


def test_noop_writes_no_audit_row(client, engine):
    body, etag = current(client)
    put(client, {"notifications": body["notifications"]}, etag)
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM audit "
                           "WHERE action = 'settings.updated'")).scalar_one()
    assert n == 0, "un salvataggio che non cambia niente non è un evento"


def test_missing_if_match_is_refused(client):
    r = put(client, notif(), None)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "if_match_required"


def test_weak_etag_is_accepted_because_gzip_produces_one(client):
    """`W/"1"` deve funzionare, e il motivo è concreto.

    Il modulo gzip di nginx indebolisce l'ETag quando comprime: il server manda
    `"1"`, il browser riceve `W/"1"` e può solo rimandare quello. Con il solo
    confronto forte ogni salvataggio dall'interfaccia riceveva 422, mentre le
    stesse chiamate da uno script — che non chiede la compressione — passavano.
    L'ha trovato il test nel browser, non questo file: qui non c'è nginx.

    È anche corretto nel merito: la distinzione forte/debole riguarda l'identità
    byte per byte della rappresentazione, e il nostro validatore è un numero di
    revisione, che la compressione non cambia."""
    r = put(client, notif(recipients=["a@b.it"]), 'W/"1"')
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2


def test_weak_etag_with_a_stale_revision_still_conflicts(client):
    """Accettare la forma debole non allenta il controllo: conta la revisione."""
    _, etag = current(client)
    put(client, notif(recipients=["primo@example.it"]), etag)
    r = put(client, notif(recipients=["secondo@example.it"]), 'W/"1"')
    assert r.status_code == 409


@pytest.mark.parametrize("etag", [
    "1",              # senza virgolette
    "*",              # «qualunque versione»: è l'ultimo-che-scrive-vince
    'W/1',            # debole ma senza virgolette
    'w/"1"',          # il prefisso è maiuscolo per la RFC
    '"abc"',
    '""',
    '"1',
    '"-1"',
    '"1", "2"',
    '"' + "9" * 40 + '"',
    "   ",
])
def test_malformed_if_match_is_refused(client, etag):
    r = put(client, notif(), etag)
    assert r.status_code == 422, etag
    assert r.json()["detail"]["code"] in ("if_match_malformed", "if_match_required")


def test_wrong_but_wellformed_if_match_is_a_conflict_not_a_validation_error(client):
    """Un ETag valido ma vecchio è un CONFLITTO, non un errore di sintassi: sono
    due problemi diversi e il client reagisce in modo diverso."""
    r = put(client, notif(recipients=["a@b.it"]), '"999"')
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "settings_version_conflict"


def test_if_match_is_checked_against_the_locked_row(client, engine):
    """La revisione si confronta DOPO aver preso il blocco: verificarla prima
    lascerebbe una finestra in cui due richieste passano entrambe."""
    _, etag = current(client)
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET version = version + 1 WHERE id = 1"))
    r = put(client, notif(recipients=["a@b.it"]), etag)
    assert r.status_code == 409


# ==================================================================
# validazione tipizzata
# ==================================================================

@pytest.mark.parametrize("payload,code", [
    (notif(recipients=["non-un-indirizzo"]), "invalid_recipient"),
    (notif(recipients=["a@b.it", "A@B.IT"]), "duplicate_recipient"),
    (notif(recipients=[f"u{i}@e.it" for i in range(30)]), "too_many_recipients"),
    (notif(timezone="Europa/Roma"), "invalid_timezone"),
    (notif(warningDays=[0]), "invalid_warning_day"),
    (notif(warningDays=[-5]), "invalid_warning_day"),
    (notif(warningDays=list(range(1, 30))), "too_many_warning_days"),
    (notif(schedule={"hour": 99, "minute": 0}), "invalid_schedule"),
    (notif(enabled="si"), "invalid_type"),
    ({"notifications": {}}, "missing_field"),
    ({}, "missing_field"),
])
def test_invalid_settings_are_refused_with_a_stable_code(client, payload, code):
    _, etag = current(client)
    r = put(client, payload, etag)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == code


def test_unknown_field_is_refused(client):
    _, etag = current(client)
    payload = notif()
    payload["notifications"]["colore"] = "rosso"
    r = put(client, payload, etag)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_field"
    assert r.json()["detail"]["field"] == "notifications.colore"


@pytest.mark.parametrize("payload", [
    {"notifications": {}, "smtpPassword": "hunter2"},
    {"notifications": {"password": "hunter2"}},
    {"notifications": {"schedule": {"secret": "hunter2"}}},
    {"notifications": {"deep": {"nested": {"apiKey": "hunter2"}}}},
])
def test_secret_like_fields_are_refused_recursively(client, payload):
    _, etag = current(client)
    r = put(client, payload, etag)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "secret_field_rejected"
    # E il valore rifiutato non torna indietro nel messaggio.
    assert "hunter2" not in r.text


def test_read_only_fields_are_refused(client):
    """Rimandare il documento della GET così com'è non funziona, e il codice lo
    dice: la concorrenza si gestisce con If-Match, non con un campo nel corpo."""
    body, etag = current(client)
    r = put(client, body, etag)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "read_only_field"


def test_invalid_settings_change_nothing(client):
    _, etag = current(client)
    put(client, notif(recipients=["rotto"]), etag)
    body, _ = current(client)
    assert body["version"] == 1
    assert body["notifications"] == default_document()["notifications"]


def test_malformed_json_gets_our_error_shape(client):
    """Un solo formato di errore su tutta l'API: il client non deve saper
    leggere anche quello di FastAPI."""
    r = client.put("/api/settings", content="{non json",
                   headers={**ORIGIN, "If-Match": '"1"',
                            "Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_body"


def test_oversized_body_is_refused(client):
    _, etag = current(client)
    payload = notif(recipients=["a@b.it"])
    payload["notifications"]["timezone"] = "Europe/Rome"
    huge = {"notifications": payload["notifications"], "riempitivo": "x" * 40000}
    r = put(client, huge, etag)
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "settings_too_large"


def test_non_admin_cannot_save(db, engine):
    with _client(engine) as c:
        login(c, "op", "password-lunga-2")
        r = put(c, notif(recipients=["a@b.it"]), '"1"')
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "forbidden_for_role"
    app.dependency_overrides.clear()


def test_unauthenticated_cannot_save(db, engine):
    with _client(engine) as c:
        assert put(c, notif(), '"1"').status_code == 401
    app.dependency_overrides.clear()


def test_enabled_false_round_trips(client):
    """Il valore falso esplicito sopravvive al salvataggio e alla rilettura."""
    _, etag = current(client)
    put(client, notif(enabled=True, recipients=["a@b.it"]), etag)
    _, etag = current(client)
    r = put(client, notif(enabled=False, recipients=["a@b.it"]), etag)
    assert r.status_code == 200
    body, _ = current(client)
    assert body["notifications"]["enabled"] is False


# ==================================================================
# audit
# ==================================================================

def test_save_writes_an_audit_row_with_the_changed_field_names(client, engine):
    _, etag = current(client)
    put(client, notif(recipients=["team@example.internal"], enabled=True), etag)

    with engine.begin() as c:
        row = c.execute(text("""
            SELECT actor_username, actor_role, result, events
              FROM audit WHERE action = 'settings.updated'
        """)).first()
    assert row is not None, "un cambio di configurazione senza traccia"
    assert row[0] == "capo" and row[1] == "admin" and row[2] == "success"
    detail = row[3][0]
    assert detail["fromVersion"] == 1 and detail["toVersion"] == 2
    assert set(detail["changedFields"]) == {"notifications.enabled",
                                            "notifications.recipients"}


def test_audit_records_field_names_not_values(client, engine):
    """Gli indirizzi dei destinatari sono dati di persone: nel registro finiscono
    i nomi dei campi cambiati, non il loro contenuto."""
    _, etag = current(client)
    put(client, notif(recipients=["mario.rossi@example.internal"]), etag)
    with engine.begin() as c:
        events = c.execute(text("SELECT events FROM audit "
                                "WHERE action = 'settings.updated'")).scalar_one()
    assert "mario.rossi" not in json.dumps(events)


def test_audit_failure_rolls_back_the_settings_change(db, engine, monkeypatch):
    """La modifica e la sua traccia stanno o cadono insieme.

    Una configurazione cambiata senza sapere da chi è precisamente ciò che
    l'audit esiste per impedire: se la riga non si scrive, la modifica non
    resta.

    `raise_server_exceptions=False` serve al TestClient, non all'applicazione:
    per default ripropaga l'eccezione al chiamante invece di restituire la
    risposta del gestore, e qui interessa proprio vedere la risposta — cioè cosa
    riceverebbe un browser."""
    import app.settings.repository as repository

    def esplodi(*a, **kw):
        raise RuntimeError("audit non disponibile")

    with _client(engine, raise_server_exceptions=False) as c:
        login(c, "capo", "password-lunga-1")
        _, etag = current(c)

        monkeypatch.setattr(repository, "record_auth_event", esplodi)
        r = put(c, notif(recipients=["a@b.it"]), etag)
        assert r.status_code == 503
        monkeypatch.undo()

        body, _ = current(c)
        assert body["version"] == 1, "la modifica è sopravvissuta all'audit fallito"
        assert body["notifications"]["recipients"] == []
    app.dependency_overrides.clear()


# ==================================================================
# riservatezza
# ==================================================================

def test_no_secret_reaches_any_settings_response(client):
    body, etag = current(client)
    put(client, notif(recipients=["a@b.it"]), etag)
    for r in (client.get("/api/settings"),
              client.get("/api/audit")):
        assert not contains_secret(r.text), r.text[:400]


def test_smtp_password_is_never_in_the_response(client, monkeypatch):
    """Anche con un secret SMTP presente e leggibile, il documento non lo cita."""
    from app.config import get_settings as cfg
    monkeypatch.setattr(type(cfg()), "smtp_password",
                        lambda self: "password-smtp-segretissima")
    r = client.get("/api/settings")
    assert "password-smtp-segretissima" not in r.text
    assert "smtp_password" not in r.text.lower()


def test_settings_response_does_not_leak_infrastructure(client):
    """Niente stringa di connessione, niente percorsi di secret, niente host del
    relay: la risposta parla di configurazione applicativa e basta."""
    r = client.get("/api/settings")
    lowered = r.text.lower()
    for frammento in ("postgresql", "://", "/run/secrets", "tsm_api", "smtp_host"):
        assert frammento not in lowered, frammento


# ==================================================================
# privilegi del ruolo di runtime
# ==================================================================

def test_runtime_role_cannot_insert_or_delete_settings(db, engine):
    """La riga unica nasce nella migrazione. Il ruolo di runtime la legge e la
    aggiorna; non può crearne una seconda né cancellarla — come per
    `inventory_head` (§8.19), la garanzia sta nei privilegi e non nel codice."""
    with engine.begin() as c:
        c.execute(text("ALTER ROLE tsm_api WITH PASSWORD 'provaprova'"))

    url = DSN.split("://", 1)[1]
    hostpart = url.split("@", 1)[1]
    runtime_dsn = f"postgresql+psycopg://tsm_api:provaprova@{hostpart}"
    runtime = create_engine(runtime_dsn, future=True)
    try:
        # Legge e aggiorna: deve poterlo fare, altrimenti l'API non funziona.
        with runtime.connect() as c:
            assert c.execute(text("SELECT version FROM settings")).scalar_one() >= 1
        with runtime.connect() as c:
            with c.begin():
                c.execute(text("UPDATE settings SET updated_at = now() WHERE id = 1"))

        for statement in ("INSERT INTO settings (id, data) VALUES (2, '{}'::jsonb)",
                          "DELETE FROM settings WHERE id = 1",
                          "TRUNCATE settings"):
            # Una connessione nuova per ogni istruzione: dopo un errore la
            # transazione è avvelenata e l'istruzione successiva fallirebbe per
            # quello invece che per i privilegi.
            with runtime.connect() as c:
                with pytest.raises(Exception) as exc:
                    with c.begin():
                        c.execute(text(statement))
                assert "permission denied" in str(exc.value).lower(), statement
    finally:
        runtime.dispose()


def test_runtime_role_cannot_rewrite_the_test_attempt_counter(db, engine):
    """Il contatore degli invii di prova si accoda e si conta, non si corregge:
    con `DELETE` un difetto potrebbe azzerare il limite."""
    with engine.begin() as c:
        c.execute(text("ALTER ROLE tsm_api WITH PASSWORD 'provaprova'"))

    hostpart = DSN.split("://", 1)[1].split("@", 1)[1]
    runtime = create_engine(f"postgresql+psycopg://tsm_api:provaprova@{hostpart}",
                            future=True)
    try:
        with runtime.connect() as c:
            with c.begin():
                c.execute(text("INSERT INTO notification_test_attempts (ip) "
                               "VALUES ('10.0.0.1')"))
        for statement in ("DELETE FROM notification_test_attempts",
                          "UPDATE notification_test_attempts SET ip = NULL",
                          "TRUNCATE notification_test_attempts"):
            with runtime.connect() as c:
                with pytest.raises(Exception) as exc:
                    with c.begin():
                        c.execute(text(statement))
                assert "permission denied" in str(exc.value).lower(), statement
    finally:
        runtime.dispose()


# ==================================================================
# coerenza fra migrazione e schema
# ==================================================================

@pytest.fixture
def pristine_migration(engine):
    """La riga delle impostazioni **come l'ha scritta la migrazione**.

    Serve un downgrade e un upgrade veri: qualunque fixture che scriva la riga
    prima del controllo renderebbe il test una verifica di sé stesso. È
    esattamente l'errore che una prima versione di questi test conteneva — la
    fixture `db` sovrascriveva `settings.data`, e l'asserzione «la migrazione
    inserisce il documento giusto» passava senza aver mai guardato ciò che la
    migrazione inserisce.

    Come effetto collaterale, è anche l'unica cosa che prova che `downgrade()`
    funziona.
    """
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0006_audit_read")
    command.upgrade(cfg, "head")
    yield engine


def test_migration_default_matches_the_canonical_default(pristine_migration, engine):
    """Il documento inserito dalla migrazione 0007 DEVE essere la forma canonica.

    Se divergessero, la prima GET restituirebbe un documento che la PUT
    successiva considera modificato: la revisione salirebbe da sola e il primo
    salvataggio di un secondo amministratore fallirebbe senza motivo."""
    with engine.begin() as c:
        stored = c.execute(text("SELECT data FROM settings WHERE id = 1")).scalar_one()
    assert stored == default_document()


def test_migration_stores_a_json_object_not_a_json_string(pristine_migration, engine):
    """Il difetto che questo test è nato per prendere.

    Passando una stringa già serializzata a un parametro tipizzato JSONB, il
    driver la serializza una seconda volta e nella colonna finisce una *stringa*
    JSON. Da Python non si nota — `json.loads` la apre — ma ogni operatore jsonb
    smette di funzionare: `data -> 'notifications'` su una stringa restituisce
    NULL, e lo scoprirebbe lo scheduler mesi dopo, come «non trova destinatari».
    """
    with engine.begin() as c:
        kind = c.execute(text(
            "SELECT jsonb_typeof(data) FROM settings WHERE id = 1")).scalar_one()
        # La prova che conta: l'operatore jsonb deve restituire qualcosa.
        reachable = c.execute(text(
            "SELECT data -> 'notifications' -> 'schedule' ->> 'hour' "
            "FROM settings WHERE id = 1")).scalar_one()
    assert kind == "object", f"jsonb_typeof = {kind}"
    assert reachable == "8"


def test_a_saved_document_is_also_a_json_object(client, engine):
    """Lo stesso controllo dopo una PUT: il cast esplicito vale anche in scrittura."""
    _, etag = current(client)
    assert put(client, notif(recipients=["a@b.it"]), etag).status_code == 200
    with engine.begin() as c:
        kind = c.execute(text(
            "SELECT jsonb_typeof(data) FROM settings WHERE id = 1")).scalar_one()
        first = c.execute(text(
            "SELECT data -> 'notifications' -> 'recipients' ->> 0 "
            "FROM settings WHERE id = 1")).scalar_one()
    assert kind == "object"
    assert first == "a@b.it"


def test_load_refuses_a_json_string_instead_of_accommodating_it(db, engine):
    """Se la colonna contiene una stringa JSON, l'API deve FALLIRE, non arrangiarsi.

    Un `json.loads` di comodo farebbe funzionare le rotte e lascerebbe rotti gli
    operatori jsonb: il guasto si manifesterebbe altrove e molto più tardi.
    """
    from app.settings.repository import SettingsCorrupted, load
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = to_jsonb('{\"a\": 1}'::text) "
                       "WHERE id = 1"))
    try:
        with engine.connect() as c:
            with pytest.raises(SettingsCorrupted):
                load(c)
    finally:
        # Si ripristina subito: la riga è unica e condivisa, e lasciarla rotta
        # farebbe fallire i test successivi per un motivo che non è il loro.
        with engine.begin() as c:
            c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb) WHERE id = 1"),
                      {"d": json.dumps(DEFAULTS)})


def test_settings_table_holds_exactly_one_row(db, engine):
    """La singolarità è un vincolo del database, non una convenzione."""
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM settings")).scalar_one() == 1
    with pytest.raises(Exception) as exc:
        with engine.begin() as c:
            c.execute(text("INSERT INTO settings (id, data) VALUES (2, '{}'::jsonb)"))
    assert "ck_settings_singleton" in str(exc.value) or "duplicate" in str(exc.value).lower()
