"""0.5.0: unione dei salvataggi, blocchi per rack, presenza, avvisi di sistema,
diagnostica. PostgreSQL reale, sessioni vere."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import Actor, InventoryRepository
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402

LOC = "aaaaaaaa-0000-4000-8000-000000000001"
ROOM = "bbbbbbbb-0000-4000-8000-000000000001"
RA = "cccccccc-0000-4000-8000-00000000000a"
RB = "cccccccc-0000-4000-8000-00000000000b"
DA = "dddddddd-0000-4000-8000-00000000000a"
DN = "dddddddd-0000-4000-8000-00000000000e"


def base_doc() -> dict:
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
            {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [], "racks": [
                {"_uid": RA, "id": "R01", "name": "R01", "u": 45, "x": 0.5, "y": 0.5,
                 "w": 0.6, "h": 0.8, "devices": [
                     {"_uid": DA, "id": "srv-01", "name": "srv-01", "u": 10}]},
                {"_uid": RB, "id": "R02", "name": "R02", "u": 45, "x": 1.5, "y": 0.5,
                 "w": 0.6, "h": 0.8, "devices": []}]}]}],
    }


def rack(doc, uid):
    return next(k for k in doc["locations"][0]["sale"][0]["racks"] if k["_uid"] == uid)


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
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM edit_locks"))
        c.execute(text("DELETE FROM recovery_codes"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM users"))
        c.execute(text("DELETE FROM system_alerts"))
        c.execute(text("DELETE FROM status_snapshots"))
        c.execute(text("UPDATE system_alert_config SET enabled = true, include_admins = true, "
                       "extra_recipients = '{}', min_level = 'crit', repeat_hours = 24, "
                       "last_send_attempt_at = NULL, last_send_ok_at = NULL, "
                       "last_send_error = NULL, last_test_at = NULL"))
        create_user(c, "anna", "password-lunga-1", "edit", must_change_pw=False)
        create_user(c, "bruno", "password-lunga-2", "edit", must_change_pw=False)
        create_user(c, "lettore", "password-lunga-4", "view", must_change_pw=False)
        create_user(c, "capo", "password-lunga-3", "admin", must_change_pw=False)
        c.execute(text("UPDATE users SET nome = 'Anna', cognome = 'Bianchi' WHERE username = 'anna'"))
        c.execute(text("UPDATE users SET email = 'capo@example.it' WHERE username = 'capo'"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(base_doc(), Actor(username="admin", role="admin"))
    yield engine


@pytest.fixture
def override(engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    yield
    app.dependency_overrides.clear()


def _client(u, p):
    c = api_client(app)
    c.__enter__()
    r = c.post("/api/auth/login", headers=ORIGIN, json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def anna(db, override):
    c = _client("anna", "password-lunga-1")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def bruno(db, override):
    c = _client("bruno", "password-lunga-2")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def capo(db, override):
    c = _client("capo", "password-lunga-3")
    yield c
    c.__exit__(None, None, None)


def _get(c):
    r = c.get("/api/inventory")
    assert r.status_code == 200
    return r.json()["version"], r.json()["doc"]


def _put(c, base, doc):
    return c.put("/api/inventory", headers=ORIGIN, json={"baseVersion": base, "doc": doc})


# ============================================================ unione

def test_saves_on_different_racks_are_merged(anna, bruno):
    v, doc_a = _get(anna)
    _, doc_b = _get(bruno)
    rack(doc_a, RA)["devices"][0]["model"] = "DL380"
    assert _put(anna, v, doc_a).status_code == 200
    rack(doc_b, RB)["devices"].append({"_uid": DN, "id": "fw-01", "name": "fw-01", "u": 40})
    r = _put(bruno, v, doc_b)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["merged"] is True and b["changed"] is True
    assert rack(b["doc"], RA)["devices"][0]["model"] == "DL380"
    assert rack(b["doc"], RB)["devices"][0]["id"] == "fw-01"
    _, now = _get(anna)
    assert rack(now, RA)["devices"][0]["model"] == "DL380"
    assert rack(now, RB)["devices"][0]["id"] == "fw-01"


def test_the_merged_save_is_annotated_in_the_audit(anna, bruno, engine):
    v, doc_a = _get(anna)
    _, doc_b = _get(bruno)
    rack(doc_a, RA)["devices"][0]["model"] = "A"
    assert _put(anna, v, doc_a).status_code == 200
    rack(doc_b, RB)["devices"].append({"_uid": DN, "id": "fw-01", "name": "fw-01", "u": 40})
    assert _put(bruno, v, doc_b).status_code == 200
    with engine.connect() as c:
        hint = c.execute(text("SELECT client_hint FROM audit WHERE actor_username = 'bruno' "
                              " AND action = 'inventory.save'")).scalar()
    assert "unito alla versione" in hint


def test_same_device_still_conflicts(anna, bruno):
    v, doc_a = _get(anna)
    _, doc_b = _get(bruno)
    rack(doc_a, RA)["devices"][0]["model"] = "A"
    rack(doc_b, RA)["devices"][0]["model"] = "B"
    assert _put(anna, v, doc_a).status_code == 200
    r = _put(bruno, v, doc_b)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "version_conflict"


def test_a_view_user_cannot_sneak_changes_through_a_merge(db, override, anna):
    lett = _client("lettore", "password-lunga-4")
    v, doc_a = _get(anna)
    _, doc_l = _get(lett)
    rack(doc_a, RA)["devices"][0]["model"] = "A"
    assert _put(anna, v, doc_a).status_code == 200
    rack(doc_l, RB)["devices"].append({"_uid": DN, "id": "fw-01", "name": "fw-01", "u": 40})
    r = _put(lett, v, doc_l)
    assert r.status_code == 403
    lett.__exit__(None, None, None)


# ============================================================ presenza e blocchi

def _ping(c, rack_uid=None):
    r = c.post("/api/presence", headers=ORIGIN, json={"rackUid": rack_uid})
    assert r.status_code == 200, r.text
    return r.json()


def test_presence_lists_who_is_online(anna, bruno):
    _ping(anna)
    out = _ping(bruno, RB)
    nomi = {o["username"]: o for o in out["online"]}
    assert set(nomi) == {"anna", "bruno"}
    assert nomi["anna"]["display"] == "Anna Bianchi"
    assert nomi["bruno"]["you"] and nomi["bruno"]["rackUid"] == RB


def test_presence_does_not_keep_an_idle_session_alive(anna, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE sessions SET last_seen_at = now() - interval '30 minutes'"))
    _ping(anna)
    with engine.connect() as c:
        recente = c.execute(text(
            "SELECT last_seen_at > now() - interval '1 minute' FROM sessions")).scalar()
    assert not recente


def test_a_rack_is_locked_by_the_first_editor(anna, bruno):
    a = _ping(anna, RA)
    assert a["lock"] == {"rackUid": RA, "granted": True, "heldBy": None}
    b = _ping(bruno, RA)
    assert b["lock"]["granted"] is False and b["lock"]["heldBy"] == "Anna Bianchi"
    assert any(l["rackUid"] == RA and l["display"] == "Anna Bianchi" for l in b["locks"])


def test_saving_a_rack_locked_by_someone_else_is_refused(anna, bruno):
    _ping(anna, RA)
    v, doc = _get(bruno)
    rack(doc, RA)["devices"][0]["model"] = "X"
    r = _put(bruno, v, doc)
    assert r.status_code == 423
    d = r.json()["detail"]
    assert d["code"] == "rack_locked" and "Anna Bianchi" in d["message"] and "R01" in d["message"]
    # Un altro rack si salva.
    rack(doc, RA)["devices"][0].pop("model")
    rack(doc, RB)["devices"].append({"_uid": DN, "id": "fw-01", "name": "fw-01", "u": 40})
    assert _put(bruno, v, doc).status_code == 200
    # Chi tiene il blocco salva.
    v, doc = _get(anna)
    rack(doc, RA)["devices"][0]["model"] = "Y"
    assert _put(anna, v, doc).status_code == 200


def test_moving_to_another_rack_releases_the_previous_lock(anna, bruno):
    _ping(anna, RA)
    _ping(anna, RB)
    assert _ping(bruno, RA)["lock"]["granted"] is True


def test_an_expired_lock_is_free(anna, bruno, engine):
    _ping(anna, RA)
    with engine.begin() as c:
        c.execute(text("UPDATE edit_locks SET expires_at = now() - interval '1 second'"))
    assert _ping(bruno, RA)["lock"]["granted"] is True


def test_logout_releases_the_lock(anna, bruno):
    _ping(anna, RA)
    anna.post("/api/auth/logout", headers=ORIGIN)
    assert _ping(bruno, RA)["lock"]["granted"] is True


def test_view_users_never_lock(db, override, bruno):
    lett = _client("lettore", "password-lunga-4")
    assert _ping(lett, RA)["lock"] is None
    assert _ping(bruno, RA)["lock"]["granted"] is True
    lett.__exit__(None, None, None)


def test_admin_can_force_unlock(anna, bruno, capo, engine):
    _ping(anna, RA)
    assert bruno.post(f"/api/locks/{RA}/release", headers=ORIGIN).status_code == 403
    r = capo.post(f"/api/locks/{RA}/release", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["heldBy"] == "anna"
    assert _ping(bruno, RA)["lock"]["granted"] is True
    assert capo.post(f"/api/locks/{RA}/release", headers=ORIGIN).status_code == 200  # di bruno
    assert capo.post(f"/api/locks/{RA}/release", headers=ORIGIN).status_code == 404
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit WHERE action = "
                              "'inventory.lock.released_by_admin'")).scalar() == 2


def test_presence_needs_a_session(db, override):
    with api_client(app) as c:
        assert c.post("/api/presence", headers=ORIGIN, json={}).status_code == 401


def test_garbage_rack_uid_is_ignored(anna):
    assert _ping(anna, "'; DROP TABLE users; --")["lock"] is None


# ============================================================ avvisi di sistema

class _Posta:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, msg):
        if self.fail:
            from app.notifications.smtp import SmtpSendFailed
            raise SmtpSendFailed("connection_failed", "relay giù")
        self.sent.append(msg)


@pytest.fixture
def smtp_on(monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(type(s), "smtp_configured", lambda self: True)
    monkeypatch.setattr(s, "smtp_sender", "tsm@example.it", raising=False)
    yield s


def _items(stato="crit"):
    return [{"gruppo": "Worker", "nome": "Battito", "valore": "mai ricevuto", "stato": stato}]


def _run(engine, monkeypatch, items, now, posta):
    from app.api import system
    from app.ops import sentinel
    monkeypatch.setattr(system, "collect", lambda conn, now=None: items)
    return sentinel.run_once(engine, now=now, deliver=posta)


def test_a_critical_problem_mails_the_admins_once(db, smtp_on, monkeypatch, engine):
    posta = _Posta()
    t0 = datetime.now(timezone.utc)
    r = _run(engine, monkeypatch, _items(), t0, posta)
    assert r["sent"] and r["new"] == 1
    assert len(posta.sent) == 1
    m = posta.sent[0]
    assert m["To"] == "capo@example.it" and "Worker · Battito" in m["Subject"]
    assert "mai ricevuto" in m.get_body(("html",)).get_content()
    # Il giro dopo non ripete.
    r = _run(engine, monkeypatch, _items(), t0 + timedelta(minutes=5), posta)
    assert not r["sent"] and len(posta.sent) == 1
    # Dopo `repeat_hours` sì, come promemoria.
    r = _run(engine, monkeypatch, _items(), t0 + timedelta(hours=25), posta)
    assert r["sent"] and r["repeat"] == 1
    # Torna a posto: «risolto».
    r = _run(engine, monkeypatch, [], t0 + timedelta(hours=26), posta)
    assert r["sent"] and r["resolved"] == 1
    assert "Risolto" in posta.sent[-1]["Subject"]
    r = _run(engine, monkeypatch, [], t0 + timedelta(hours=27), posta)
    assert not r["sent"]


def test_warnings_only_when_asked(db, smtp_on, monkeypatch, engine):
    posta = _Posta()
    t0 = datetime.now(timezone.utc)
    assert not _run(engine, monkeypatch, _items("warn"), t0, posta)["sent"]
    with engine.begin() as c:
        c.execute(text("UPDATE system_alert_config SET min_level = 'warn'"))
    assert _run(engine, monkeypatch, _items("warn"), t0 + timedelta(minutes=5), posta)["sent"]


def test_extra_recipients_and_no_admins(db, smtp_on, monkeypatch, engine):
    posta = _Posta()
    with engine.begin() as c:
        c.execute(text("UPDATE system_alert_config SET include_admins = false, "
                       "extra_recipients = '{noc@example.it}'"))
    _run(engine, monkeypatch, _items(), datetime.now(timezone.utc), posta)
    assert posta.sent[0]["To"] == "noc@example.it"


def test_disabled_or_without_recipients_nothing_is_sent_but_it_is_recorded(
        db, smtp_on, monkeypatch, engine):
    posta = _Posta()
    with engine.begin() as c:
        c.execute(text("UPDATE system_alert_config SET enabled = false"))
    r = _run(engine, monkeypatch, _items(), datetime.now(timezone.utc), posta)
    assert not posta.sent and r["reason"] == "avvisi disattivati"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM system_alerts WHERE resolved_at IS NULL")
                         ).scalar() == 1
    # Riattivati: il problema ancora aperto parte al giro dopo.
    with engine.begin() as c:
        c.execute(text("UPDATE system_alert_config SET enabled = true"))
    r = _run(engine, monkeypatch, _items(), datetime.now(timezone.utc), posta)
    assert r["sent"] and len(posta.sent) == 1


def test_a_failed_send_is_retried_later_not_every_tick(db, smtp_on, monkeypatch, engine):
    t0 = datetime.now(timezone.utc)
    r = _run(engine, monkeypatch, _items(), t0, _Posta(fail=True))
    assert not r["sent"] and "non riuscito" in r["reason"]
    posta = _Posta()
    r = _run(engine, monkeypatch, _items(), t0 + timedelta(minutes=5), posta)
    assert not posta.sent and "più tardi" in r["reason"]
    r = _run(engine, monkeypatch, _items(), t0 + timedelta(minutes=35), posta)
    assert r["sent"] and len(posta.sent) == 1


def test_cert_reminder_is_sent_once_per_threshold(db, smtp_on, monkeypatch, engine):
    from app.api import system
    posta = _Posta()
    t0 = datetime.now(timezone.utc)
    monkeypatch.setattr(system, "_cert_expiry", lambda: t0 + timedelta(days=25))
    r = _run(engine, monkeypatch, [], t0, posta)
    assert r["sent"] and r["reminders"] == 1
    assert "certificato" in posta.sent[0]["Subject"].lower()
    assert not _run(engine, monkeypatch, [], t0 + timedelta(hours=1), posta)["sent"]
    monkeypatch.setattr(system, "_cert_expiry", lambda: t0 + timedelta(days=5))
    assert _run(engine, monkeypatch, [], t0 + timedelta(days=20), posta)["sent"]
    assert len(posta.sent) == 2


def test_the_daily_snapshot_is_written(db, smtp_on, monkeypatch, engine):
    _run(engine, monkeypatch, [], datetime.now(timezone.utc), _Posta())
    with engine.connect() as c:
        row = c.execute(text("SELECT racks, devices FROM status_snapshots")).first()
    assert row == (2, 1)


def test_the_real_collect_runs_inside_the_sentinel(db, smtp_on, engine):
    from app.ops import sentinel
    r = sentinel.run_once(engine, deliver=_Posta())
    assert r["ran"]


# ============================================================ API amministrative

def test_alerts_config_roundtrip_and_validation(capo, anna):
    assert anna.get("/api/system/alerts").status_code == 403
    r = capo.get("/api/system/alerts")
    assert r.status_code == 200 and r.json()["recipients"] == ["capo@example.it"]
    r = capo.put("/api/system/alerts", headers=ORIGIN, json={
        "enabled": True, "includeAdmins": True, "extraRecipients": "noc@example.it, x@y.it",
        "minLevel": "warn", "repeatHours": 12})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["extraRecipients"] == ["noc@example.it", "x@y.it"] and b["minLevel"] == "warn"
    assert b["recipients"] == ["capo@example.it", "noc@example.it", "x@y.it"]
    for bad in ({"extraRecipients": ["non-una-mail"]}, {"minLevel": "tutto"},
                {"repeatHours": 0}, {"repeatHours": 500}):
        r = capo.put("/api/system/alerts", headers=ORIGIN, json=bad)
        assert r.status_code == 422, bad


def test_alerts_test_mail(capo, smtp_on, monkeypatch):
    import app.notifications.smtp as smtp
    posta = _Posta()
    monkeypatch.setattr(smtp, "deliver", posta)
    r = capo.post("/api/system/alerts/test", headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert posta.sent and "Prova" in posta.sent[0]["Subject"]
    r = capo.post("/api/system/alerts/test", headers=ORIGIN)
    assert r.status_code == 429


def test_diagnostics_shape(capo, anna):
    assert anna.get("/api/system/diagnostics").status_code == 403
    r = capo.get("/api/system/diagnostics")
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b) >= {"history", "dumps", "alerts", "access", "expiryRuns", "online", "locks"}
    assert b["access"]["ok24h"] >= 2


def test_status_has_the_new_items(capo):
    nomi = {(i["gruppo"], i["nome"]) for i in capo.get("/api/system/status").json()["items"]}
    for voce in (("Backup", "Prova di ripristino"), ("Avvisi", "Avvisi di sistema"),
                 ("Accessi", "Accessi falliti (24 h)"), ("Utenze", "Collegati adesso"),
                 ("Utenze", "Rack in modifica")):
        assert voce in nomi, voce


# ============================================================ privilegi reali
#
# I test qui sopra girano come proprietario dello schema, che può tutto. In
# produzione l'API è `tsm_api`: qui si eseguono le STESSE query con quel ruolo,
# perché un GRANT mancante si scopre solo così (è successo: scheduler_runs).

def test_everything_the_api_reads_and_writes_works_as_tsm_api(db, engine, monkeypatch):
    from fastapi import Response

    from app.api import system
    from app.api.presence import state
    from app.ops import alerts as A
    from app.ops.sentinel import snapshot
    with engine.connect() as c:
        with c.begin():
            uid = c.execute(text("SELECT id FROM users WHERE username = 'capo'")).scalar()
            c.execute(text("SET LOCAL ROLE tsm_api"))
            now = datetime.now(timezone.utc)
            items = system.collect(c, now)
            snapshot(c, items, now)
            cfg = A.load_config(c)
            A.evaluate(c, items + _items(), now=now, cfg=cfg,
                       cert_expiry=now + timedelta(days=5))
            A.recipients(c, cfg)
            A.save_config(c, {"extraRecipients": ["a@b.it"]}, by="capo")
            state(c, user_id=uid)
            out = system.diagnostics(Response(), c, Actor(username="capo", role="admin",
                                                          user_id=uid))
            assert "history" in out
            c.execute(text("""
                INSERT INTO edit_locks (rack_uid, user_id, expires_at)
                VALUES (CAST(:r AS uuid), :u, now() + interval '1 minute')
                ON CONFLICT (rack_uid) DO UPDATE SET expires_at = now()"""),
                {"r": RA, "u": uid})
            c.execute(text("UPDATE sessions SET presence_at = now() WHERE false"))
            # il controllo dei blocchi nel salvataggio
            from app.identity.canonical import canonicalise
            b = canonicalise(base_doc())
            n = json.loads(json.dumps(b))
            rack(n, RA)["devices"][0]["model"] = "X"
            altro = Actor(username="anna", role="edit", user_id=c.execute(text(
                "SELECT id FROM users WHERE username = 'anna'")).scalar())
            from app.inventory import RackLockedError
            with pytest.raises(RackLockedError):
                InventoryRepository(c)._require_unlocked(b, n, altro)


def test_autoheal_interventions_are_mailed(db, smtp_on, monkeypatch, engine):
    """Il timer sulla VM annota i suoi interventi; la sentinella li manda."""
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO system_alerts (key, kind, level, title, detail, resolved_at)
            VALUES ('autoheal:1793350000:1', 'reminder', 'warn', 'Ripristino automatico',
                    'il container api non rispondeva (unhealthy): riavviato', now())"""))
    posta = _Posta()
    t0 = datetime.now(timezone.utc)
    r = _run(engine, monkeypatch, [], t0, posta)
    assert r["sent"] and r["reminders"] == 1
    assert posta.sent[0]["Subject"] == "[TSM] Ripristino automatico"
    assert "unhealthy" in posta.sent[0].get_body(("html",)).get_content()
    assert not _run(engine, monkeypatch, [], t0 + timedelta(minutes=5), posta)["sent"]
