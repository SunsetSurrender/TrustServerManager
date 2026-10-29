"""Pulizia giornaliera dei dati di accesso (migrazione 0016). PostgreSQL reale."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app.auth.service import create_user
from app.maintenance import purge

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


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
        for t in ("recovery_codes", "login_attempts", "sessions", "audit"):
            c.execute(text(f"DELETE FROM {t}"))
        c.execute(text("DELETE FROM maintenance_runs WHERE job = 'auth_purge'"))
        c.execute(text("DELETE FROM users"))
        uid = create_user(c, "op", "password-lunga-1", "edit", must_change_pw=False)
        old, new = NOW - timedelta(days=120), NOW - timedelta(days=10)
        for ts in (old, old, new):
            c.execute(text("INSERT INTO login_attempts (ts, username, success) "
                           "VALUES (:t, 'op', false)"), {"t": ts})
        rows = [  # (creata, scade, revocata)  → attesa
            (old, old + timedelta(hours=12), None),            # scaduta da tanto: via
            (old, NOW + timedelta(days=1), old),               # revocata da tanto: via
            (new, new + timedelta(hours=12), None),            # scaduta da poco: resta
            (old, NOW + timedelta(days=1), None),              # ancora valida: resta SEMPRE
        ]
        for i, (cr, ex, rv) in enumerate(rows):
            c.execute(text("""INSERT INTO sessions (user_id, token_hash, created_at,
                              expires_at, revoked_at) VALUES (:u, :h, :c, :e, :r)"""),
                      {"u": uid, "h": f"h{i}", "c": cr, "e": ex, "r": rv})
        for cr in (old, new):
            c.execute(text("""INSERT INTO recovery_codes (user_id, code_hash, created_at,
                              expires_at) VALUES (:u, 'x', :c, :c)"""), {"u": uid, "c": cr})
    yield engine


def counts(engine):
    with engine.connect() as c:
        return tuple(c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                     for t in ("login_attempts", "sessions", "recovery_codes"))


def test_purges_only_what_is_older_than_retention(db):
    r = purge.run_once(db, now_utc=NOW, force=True)
    assert r.ran and (r.login_attempts, r.sessions, r.recovery_codes) == (2, 2, 1)
    assert counts(db) == (1, 2, 1)


def test_a_live_session_is_never_purged(db):
    purge.run_once(db, now_utc=NOW, force=True, days=30)
    with db.connect() as c:
        live = c.execute(text("SELECT count(*) FROM sessions WHERE revoked_at IS NULL "
                              "AND expires_at > :n"), {"n": NOW}).scalar_one()
    assert live == 1


def test_runs_once_per_day_and_waits_for_its_hour(db):
    early = NOW.replace(hour=1)
    assert purge.run_once(db, now_utc=early).reason == "not_yet_scheduled"
    assert purge.run_once(db, now_utc=NOW, force=True).ran
    assert purge.run_once(db, now_utc=NOW, force=True).reason == "already_ran_today"


def test_one_audit_row_with_counts(db):
    purge.run_once(db, now_utc=NOW, force=True)
    with db.connect() as c:
        rows = c.execute(text("SELECT events FROM audit WHERE action = :a"),
                         {"a": purge.AUDIT_ACTION}).all()
    assert len(rows) == 1 and rows[0][0][0]["loginAttempts"] == 2


def test_retention_has_a_floor(monkeypatch):
    monkeypatch.setenv("TSM_AUTH_RETENTION_DAYS", "1")
    assert purge.retention_days() == purge.MIN_RETENTION_DAYS
    monkeypatch.setenv("TSM_AUTH_RETENTION_DAYS", "boh")
    assert purge.retention_days() == purge.DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("TSM_AUTH_RETENTION_DAYS", "180")
    assert purge.retention_days() == 180
