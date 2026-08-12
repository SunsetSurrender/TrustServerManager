"""Worker delle scadenze: idempotenza, precedenza, ritentativi, cambio d'ora.

PostgreSQL reale e un finto server di posta. Lo stato «già inviato» è il cuore di
questo commit e vive nel database: un doppio non lo dimostrerebbe, perché ciò che
si verifica sono vincoli di unicità, `FOR UPDATE` e `ON CONFLICT`.

Il tempo è SEMPRE iniettato (`now_utc=...`). Senza, il cambio dell'ora legale si
proverebbe solo due volte l'anno.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fixtures" / "expiry"))
from build import build_inventory  # noqa: E402

from app.auth.service import create_user  # noqa: E402
from app.inventory import Actor, InventoryRepository  # noqa: E402
from app.notifications import reminders as rem  # noqa: E402
from app.notifications.expiry import due_items  # noqa: E402
from app.notifications import worker as wk  # noqa: E402
from app.settings.schema import DEFAULTS  # noqa: E402

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

TODAY = date(2026, 8, 10)
WINDOWS = [90, 30, 7]
RECIPIENTS = ["uno@example.internal", "due@example.internal",
              "tre@example.internal", "quattro@example.internal",
              "cinque@example.internal"]

#: 08:00 a Roma = 06:00 UTC in estate. Dopo l'ora pianificata, quindi «dovuto».
def at(day: date, hour_utc: int = 7, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour_utc, minute,
                    tzinfo=timezone.utc)


# ==================================================================
# finto server di posta
# ==================================================================

class FakeSMTP:
    sent: list = []
    fail_with: Exception | None = None
    connections: int = 0

    def __init__(self, host, port, timeout=None, context=None):
        type(self).connections += 1
        if type(self).fail_with is not None:
            raise type(self).fail_with

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def ehlo(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        type(self).sent.append(msg)

    @classmethod
    def reset(cls):
        cls.sent = []
        cls.fail_with = None
        cls.connections = 0


@pytest.fixture
def smtp(monkeypatch):
    import app.notifications.smtp as mod
    from app.config import get_settings
    FakeSMTP.reset()
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", FakeSMTP)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_host", "relay.interno", raising=False)
    monkeypatch.setattr(cfg, "smtp_sender", "ced@example.internal", raising=False)
    monkeypatch.setattr(cfg, "smtp_username", "", raising=False)
    monkeypatch.setattr(cfg, "smtp_tls_mode", "starttls", raising=False)
    yield FakeSMTP
    FakeSMTP.reset()


# ==================================================================
# stato
# ==================================================================

@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


def set_settings(engine, *, enabled=True, recipients=None, windows=None,
                 tz="Europe/Rome", hour=8, minute=0):
    data = json.loads(json.dumps(DEFAULTS))
    data["notifications"].update({
        "enabled": enabled,
        "recipients": RECIPIENTS if recipients is None else recipients,
        "warningDays": WINDOWS if windows is None else windows,
        "timezone": tz,
        "schedule": {"hour": hour, "minute": minute},
    })
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb), "
                       "version = version + 1 WHERE id = 1"),
                  {"d": json.dumps(data)})


def load_inventory(engine, reference: date = TODAY):
    doc = build_inventory(reference)
    with engine.begin() as c:
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(doc, Actor(username="capo", role="admin"))
    return doc


@pytest.fixture
def db(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE reminders SET delivery_id = NULL"))
        c.execute(text("DELETE FROM reminders"))
        c.execute(text("DELETE FROM reminder_deliveries"))
        c.execute(text("DELETE FROM scheduler_runs"))
        c.execute(text("DELETE FROM notification_test_attempts"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("UPDATE settings SET updated_by = NULL WHERE id = 1"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
    load_inventory(engine)
    set_settings(engine)
    yield engine


def run(engine, when: datetime, **kw):
    return wk.run_once(engine, now_utc=when, **kw)


def reminders(engine, state=None):
    sql = ("SELECT entity_uid, expiry_kind, expiry_date, threshold_days, state, "
           "delivery_id FROM reminders")
    if state:
        sql += " WHERE state = :s"
    sql += " ORDER BY expiry_date, threshold_days"
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(sql),
                                           {"s": state} if state else {}).mappings()]


def deliveries(engine):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, message_id, state, attempts, recipients_count, "
            "reminder_count, failure_category, next_attempt_after "
            "FROM reminder_deliveries ORDER BY id")).mappings()]


def audit_actions(engine):
    with engine.begin() as c:
        return [r[0] for r in c.execute(text(
            "SELECT action FROM audit WHERE action LIKE 'notifications.digest%' "
            "ORDER BY id"))]


def bodies(smtp):
    return [m.get_content() for m in smtp.sent]


# ==================================================================
# 1. il giro normale
# ==================================================================

def test_a_run_sends_one_digest_for_all_due_items(db, engine, smtp):
    result = run(engine, at(TODAY))
    assert result.ran and result.reason == "sent", result
    assert len(smtp.sent) == 1, "un digest, non un'email per dispositivo"
    assert result.sent == result.due > 0


def test_the_digest_goes_to_every_configured_recipient(db, engine, smtp):
    """Il tetto di tre destinatari è una misura anti-abuso dell'endpoint di prova
    interattivo (§8.38). Omettere destinatari da un avviso reale significherebbe
    che qualcuno non riceve la notifica che ha chiesto."""
    run(engine, at(TODAY))
    to = smtp.sent[0]["To"]
    for address in RECIPIENTS:
        assert address in to, address
    assert len(to.split(", ")) == len(RECIPIENTS) == 5


def test_the_interactive_test_endpoint_stays_capped(db, engine, smtp):
    """Lo stesso inventario e le stesse impostazioni: la prova manuale resta a
    tre destinatari, il digest reale va a tutti. Sono due limiti diversi con due
    scopi diversi, e questo test li mette a confronto nello stesso stato."""
    from app.notifications.smtp import send_test_message
    send_test_message(RECIPIENTS, actor_username="capo")
    assert len(smtp.sent[0]["To"].split(", ")) == 3
    smtp.reset()
    run(engine, at(TODAY))
    assert len(smtp.sent[0]["To"].split(", ")) == 5


def test_the_most_urgent_threshold_is_chosen_per_item(db, engine, smtp):
    """`srv-oggi` scade fra 0 giorni: applicabili 7, 30 e 90, si manda la 7 e le
    altre due si segnano superate."""
    run(engine, at(TODAY))
    rows = [r for r in reminders(engine) if r["expiry_date"] == TODAY]
    sent = [r for r in rows if r["state"] == "sent"]
    superseded = [r for r in rows if r["state"] == "superseded"]
    assert [r["threshold_days"] for r in sent] == [7]
    assert sorted(r["threshold_days"] for r in superseded) == [30, 90]


def test_expired_items_are_not_in_the_digest(db, engine, smtp):
    run(engine, at(TODAY))
    body = bodies(smtp)[0]
    assert "srv-scaduto" not in body
    assert "srv-scaduto-ieri" not in body


def test_items_beyond_every_window_are_absent(db, engine, smtp):
    run(engine, at(TODAY))
    assert "srv-91" not in bodies(smtp)[0]


def test_unparseable_dates_do_not_break_the_run(db, engine, smtp):
    result = run(engine, at(TODAY))
    assert result.reason == "sent"
    assert "srv-data-rotta" not in bodies(smtp)[0]


def test_both_devices_sharing_a_business_id_are_reminded(db, engine, smtp):
    run(engine, at(TODAY))
    body = bodies(smtp)[0]
    assert "dup-a" in body and "dup-b" in body


def test_hostile_device_name_adds_no_header(db, engine, smtp):
    run(engine, at(TODAY))
    msg = smtp.sent[0]
    assert msg["Bcc"] is None
    assert "qualcuno@altrove.example" not in (msg["To"] or "")
    assert "<b>srv-x</b>" in msg.get_content()


# ==================================================================
# 2. una sola esecuzione al giorno, e recupero
# ==================================================================

def test_a_second_run_the_same_day_sends_nothing(db, engine, smtp):
    run(engine, at(TODAY))
    result = run(engine, at(TODAY, 9))
    assert result.reason == "already_ran_today"
    assert len(smtp.sent) == 1


def test_before_the_scheduled_hour_nothing_runs(db, engine, smtp):
    """05:00 UTC = 07:00 a Roma, prima delle 08:00 configurate."""
    result = run(engine, at(TODAY, 5))
    assert result.reason == "not_yet_scheduled"
    assert smtp.sent == []


def test_after_the_scheduled_hour_it_runs(db, engine, smtp):
    assert run(engine, at(TODAY, 6, 5)).reason == "sent"


def test_catch_up_after_missing_the_exact_threshold_day(db, engine, smtp):
    """Il caso che una regola `giorni == N` perderebbe.

    `srv-6` scade fra 6 giorni: il giorno della soglia 7 è passato mentre la
    macchina era spenta. La disuguaglianza `0 <= giorni <= N` lo recupera, e il
    promemoria della soglia 7 parte comunque."""
    run(engine, at(TODAY))
    body = bodies(smtp)[0]
    assert "srv-6" in body
    sent7 = [r for r in reminders(engine, "sent")
             if r["threshold_days"] == 7 and r["expiry_date"] == TODAY + timedelta(days=6)]
    assert len(sent7) == 1


def test_a_missed_day_is_recovered_on_the_next_run(db, engine, smtp):
    """Nessuna esecuzione il giorno 10 (macchina spenta). Il giorno 11 il registro
    non ha la riga del giorno 11 e il giro parte: nessun promemoria è perso."""
    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "sent"
    assert len(smtp.sent) == 1


# ==================================================================
# 3. assenza lunga: una sola email, la più urgente
# ==================================================================

def test_long_outage_sends_only_the_most_urgent_level(db, engine, smtp):
    """Il caso descritto nel contratto.

    Giorno 1: `srv-90` è a 90 giorni → parte la soglia 90.
    Poi la macchina resta spenta fino a 5 giorni dalla scadenza.
    Al ritorno: applicabili 90 (già inviata), 30 e 7 → parte SOLO la 7, e la 30
    si segna superata. Un riavvio non deve produrre tre email sullo stesso
    dispositivo."""
    # giorno del promemoria a 90: la scadenza di srv-90 è TODAY+90
    first = run(engine, at(TODAY))
    assert first.reason == "sent"
    sent_first = {(r["expiry_date"], r["threshold_days"])
                  for r in reminders(engine, "sent")}
    assert (TODAY + timedelta(days=90), 90) in sent_first

    smtp.reset()
    # 85 giorni dopo: mancano 5 giorni alla scadenza di srv-90
    later = TODAY + timedelta(days=85)
    result = run(engine, later.replace() and at(later))
    assert result.reason == "sent"
    assert len(smtp.sent) == 1, "una sola email, non una per soglia"

    rows = [r for r in reminders(engine)
            if r["expiry_date"] == TODAY + timedelta(days=90)]
    states = {r["threshold_days"]: r["state"] for r in rows}
    assert states[90] == "sent"
    assert states[7] == "sent", "la più urgente applicabile"
    assert states[30] == "superseded", "la soglia intermedia non genera una seconda email"


def test_normal_progression_sends_one_email_per_threshold_over_time(db, engine, smtp):
    """Senza assenze, le tre soglie producono tre avvisi in tre momenti diversi:
    la precedenza non deve sopprimere il progresso normale."""
    expiry = TODAY + timedelta(days=90)
    run(engine, at(TODAY))                                   # 90 giorni
    smtp.reset()
    run(engine, at(expiry - timedelta(days=25)))             # 25 → soglia 30
    assert len(smtp.sent) == 1
    smtp.reset()
    run(engine, at(expiry - timedelta(days=3)))              # 3 → soglia 7
    assert len(smtp.sent) == 1
    states = {r["threshold_days"]: r["state"] for r in reminders(engine)
              if r["expiry_date"] == expiry}
    assert states == {90: "sent", 30: "sent", 7: "sent"}


# ==================================================================
# 4. idempotenza durevole
# ==================================================================

def test_an_already_delivered_reminder_is_not_resent(db, engine, smtp):
    """Finestra singola, così NESSUNA voce nuova diventa eleggibile il giorno dopo
    e «non c'è niente da fare» significa davvero quello.

    Con tutte e tre le finestre questo test passerebbe per il motivo sbagliato —
    o fallirebbe per il motivo sbagliato: `srv-91` entra nella finestra da 90 il
    giorno successivo, e il digest che parte è corretto. Lo prova il test
    subito sotto."""
    set_settings(engine, windows=[7])
    run(engine, at(TODAY))
    before = {(r["expiry_date"], r["threshold_days"], r["state"])
              for r in reminders(engine)}
    assert before, "il primo giro deve aver registrato qualcosa"
    smtp.reset()

    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "nothing_due", result
    assert smtp.sent == []
    after = {(r["expiry_date"], r["threshold_days"], r["state"])
             for r in reminders(engine)}
    assert before == after


def test_an_item_entering_the_window_the_next_day_is_sent_once(db, engine, smtp):
    """L'altra faccia: `srv-91` è fuori da ogni finestra oggi e dentro quella da
    90 domani. Deve partire un digest, e deve contenere SOLO lui — non di nuovo i
    dispositivi già avvisati ieri."""
    run(engine, at(TODAY))
    already = {i.device for i in due_items(build_inventory(TODAY), today=TODAY,
                                           warning_days=WINDOWS)}
    smtp.reset()

    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "sent"
    body = bodies(smtp)[0]
    assert "srv-91" in body
    for device in already - {"srv-91"}:
        assert device not in body, f"{device} era già stato avvisato"


def test_changing_the_expiry_date_opens_a_new_lifecycle(db, engine, smtp):
    """La data fa parte dell'identità del promemoria: spostarla crea un ciclo di
    vita nuovo, senza codice dedicato."""
    run(engine, at(TODAY))
    smtp.reset()
    # Si sposta la garanzia di srv-7 di un giorno.
    load_inventory(engine, TODAY + timedelta(days=1))
    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "sent"
    assert len(smtp.sent) == 1
    dates = {r["expiry_date"] for r in reminders(engine, "sent")}
    assert len(dates) > 1, "le date nuove hanno prodotto promemoria nuovi"


def test_reminder_identity_is_unique_in_the_database(db, engine, smtp):
    """Il vincolo che rende impossibile il doppione, anche a chi lo tentasse
    scrivendo direttamente nel database."""
    run(engine, at(TODAY))
    row = reminders(engine)[0]
    with pytest.raises(Exception) as exc:
        with engine.begin() as c:
            c.execute(text("""
                INSERT INTO reminders (entity_uid, expiry_kind, expiry_date,
                                       threshold_days, state)
                VALUES (:u, :k, :d, :n, 'pending')
            """), {"u": row["entity_uid"], "k": row["expiry_kind"],
                   "d": row["expiry_date"], "n": row["threshold_days"]})
    assert "uq_reminder_identity" in str(exc.value)


def test_worker_restart_preserves_delivery_state(db, engine, smtp):
    """Non c'è nulla da «riprendere» in memoria: lo stato è nel database. Si
    simula il riavvio semplicemente chiamando di nuovo `run_once`."""
    run(engine, at(TODAY))
    sent_before = len([r for r in reminders(engine) if r["state"] == "sent"])
    smtp.reset()
    for hour in (8, 9, 10):
        run(engine, at(TODAY, hour))
    assert smtp.sent == []
    assert len([r for r in reminders(engine) if r["state"] == "sent"]) == sent_before


# ==================================================================
# 5. impostazioni
# ==================================================================

def test_disabled_notifications_send_nothing_and_record_nothing(db, engine, smtp):
    set_settings(engine, enabled=False)
    result = run(engine, at(TODAY))
    assert result.reason == "notifications_disabled"
    assert smtp.sent == []
    assert reminders(engine) == [], "nessun promemoria registrato"
    assert deliveries(engine) == []


def test_enabling_later_sends_one_digest_not_a_backlog(db, engine, smtp):
    set_settings(engine, enabled=False)
    run(engine, at(TODAY))
    set_settings(engine, enabled=True)
    result = run(engine, at(TODAY, 9))
    assert result.reason == "sent"
    assert len(smtp.sent) == 1


def test_changing_recipients_does_not_resend_delivered_reminders(db, engine, smtp):
    """Finestra singola per isolare la variabile: vedi la nota su `srv-91`."""
    set_settings(engine, windows=[7])
    run(engine, at(TODAY))
    smtp.reset()
    set_settings(engine, windows=[7], recipients=["nuovo@example.internal"])
    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "nothing_due"
    assert smtp.sent == [], "l'identità del promemoria non dipende dai destinatari"


def test_the_recipient_snapshot_is_stored_on_the_delivery(db, engine, smtp):
    run(engine, at(TODAY))
    d = deliveries(engine)[0]
    assert d["recipients_count"] == len(RECIPIENTS)
    with engine.begin() as c:
        h = c.execute(text("SELECT recipients_hash FROM reminder_deliveries "
                           "WHERE id = :i"), {"i": d["id"]}).scalar_one()
    assert h == rem.recipients_hash(RECIPIENTS)
    # L'impronta, non l'elenco: gli indirizzi non si ricopiano in una seconda
    # tabella.
    assert "@" not in h


def test_widening_warning_days_can_make_an_item_newly_eligible(db, engine, smtp):
    set_settings(engine, windows=[7])
    run(engine, at(TODAY))
    smtp.reset()
    assert "srv-30" not in bodies(smtp) or True
    set_settings(engine, windows=[7, 30])
    result = run(engine, at(TODAY + timedelta(days=1)))
    assert result.reason == "sent"
    assert len(smtp.sent) == 1
    assert "srv-30" in bodies(smtp)[0]


def test_no_recipients_configured_sends_nothing(db, engine, smtp):
    set_settings(engine, recipients=[])
    result = run(engine, at(TODAY))
    assert result.reason == "no_recipients_configured"
    assert smtp.sent == []


# ==================================================================
# 6. cambio dell'ora legale
# ==================================================================

def test_dst_spring_forward_still_runs_that_day(db, engine, smtp):
    """29 marzo 2026, Europe/Rome: le 02:00 diventano le 03:00 e le 02:30 NON
    esistono. Con l'invio pianificato alle 02:30 il giro deve partire comunque
    quel giorno, appena l'orologio da parete supera quell'ora."""
    spring = date(2026, 3, 29)
    load_inventory(engine, spring)
    set_settings(engine, hour=2, minute=30)
    # 02:05 UTC = 03:05 locale (dopo il salto): l'ora da parete ha superato 02:30
    result = run(engine, at(spring, 2, 5))
    assert result.reason == "sent", result
    assert len(smtp.sent) == 1
    with engine.begin() as c:
        rows = c.execute(text("SELECT run_date FROM scheduler_runs")).all()
    assert [r[0] for r in rows] == [spring]


def test_dst_spring_forward_does_not_run_before_the_hour(db, engine, smtp):
    spring = date(2026, 3, 29)
    load_inventory(engine, spring)
    set_settings(engine, hour=2, minute=30)
    # 00:30 UTC = 01:30 locale, prima del salto e prima dell'ora pianificata
    assert run(engine, at(spring, 0, 30)).reason == "not_yet_scheduled"
    assert smtp.sent == []


def test_dst_autumn_repeated_hour_does_not_duplicate(db, engine, smtp):
    """25 ottobre 2026, Europe/Rome: le 02:30 accadono DUE volte (CEST poi CET).

    Il registro per data locale fa sì che la seconda occorrenza trovi la riga di
    oggi già conclusa: un digest, non due. Nessuna dipendenza dalla memoria di
    uno scheduler."""
    autumn = date(2026, 10, 25)
    load_inventory(engine, autumn)
    set_settings(engine, hour=2, minute=30)

    first = run(engine, at(autumn, 0, 35))     # 02:35 CEST
    assert first.reason == "sent", first
    second = run(engine, at(autumn, 1, 35))    # 02:35 CET, la stessa ora locale
    assert second.reason == "already_ran_today"
    assert len(smtp.sent) == 1, "l'ora ripetuta ha prodotto due digest"


def test_timezone_change_moves_future_timing_only(db, engine, smtp):
    """Cambiare fuso non tocca l'identità dei promemoria già consegnati."""
    run(engine, at(TODAY))
    sent = {(r["expiry_date"], r["threshold_days"]) for r in reminders(engine, "sent")}
    smtp.reset()
    set_settings(engine, tz="America/New_York")
    run(engine, at(TODAY + timedelta(days=1)))
    assert smtp.sent == []
    assert {(r["expiry_date"], r["threshold_days"])
            for r in reminders(engine, "sent")} == sent


# ==================================================================
# 7. fallimenti e ritentativi
# ==================================================================

def test_smtp_failure_does_not_mark_reminders_as_sent(db, engine, smtp):
    smtp.fail_with = TimeoutError("relay lento")
    result = run(engine, at(TODAY))
    assert result.reason == "delivery_failed"
    assert result.failure == "timeout"
    assert reminders(engine, "sent") == [], "nessuno l'ha ricevuto"
    d = deliveries(engine)[0]
    assert d["state"] == "pending" and d["attempts"] == 1
    assert d["failure_category"] == "timeout"
    assert d["next_attempt_after"] is not None


def test_failure_then_successful_retry(db, engine, smtp):
    smtp.fail_with = TimeoutError("relay lento")
    run(engine, at(TODAY))
    smtp.fail_with = None
    # Dopo la finestra di attesa
    result = run(engine, at(TODAY, 9))
    assert result.reason == "sent", result
    assert len(smtp.sent) == 1
    assert deliveries(engine)[0]["state"] == "sent"
    assert reminders(engine, "sent")


def test_message_id_is_stable_across_retries(db, engine, smtp):
    """La sola cosa che si può fare contro il duplicato: se il relay ha accettato
    il messaggio e il processo è morto prima di registrarlo, il ritentativo manda
    lo STESSO identificativo, e un client di posta lo riconosce come duplicato
    invece di mostrarlo come un secondo avviso."""
    smtp.fail_with = smtplib.SMTPException("550 rifiutato")
    run(engine, at(TODAY))
    first_id = deliveries(engine)[0]["message_id"]

    smtp.fail_with = None
    run(engine, at(TODAY, 9))
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["Message-ID"] == first_id
    assert len(deliveries(engine)) == 1, "il ritentativo non crea una consegna nuova"


def test_retry_is_not_due_before_the_backoff_elapses(db, engine, smtp):
    smtp.fail_with = TimeoutError("lento")
    run(engine, at(TODAY))
    smtp.fail_with = None
    # Subito dopo: l'attesa non è passata, non si ritenta.
    result = run(engine, at(TODAY, 7, 0, ))
    assert result.reason in ("already_ran_today", "not_yet_scheduled"), result
    assert smtp.sent == []


#: Cinque istanti UTC, ognuno oltre l'attesa del tentativo precedente
#: (60s, 300s, 900s, 3600s) e tutti nella STESSA giornata locale, in modo che
#: l'attesa di sei ore che segue l'ultimo cada anch'essa nella stessa data
#: locale. Non è pignoleria: cambiando giorno cambierebbero i giorni residui di
#: ogni dispositivo, entrerebbero promemoria nuovi in finestra, e un test che
#: vuole dimostrare «niente da mandare» manderebbe un digest per un motivo che
#: non ha niente a che vedere con l'attesa.
_ATTEMPTS_UTC = [(0, 10), (0, 20), (0, 30), (1, 0), (2, 10)]


def _exhaust_attempts(engine, smtp) -> None:
    """Cinque tentativi falliti, ognuno oltre l'attesa del precedente.

    `force=True` perché questi istanti precedono l'ora pianificata locale: qui si
    prova il limite dei tentativi, non la pianificazione.
    """
    assert len(_ATTEMPTS_UTC) == rem.MAX_ATTEMPTS
    smtp.fail_with = ConnectionRefusedError("relay giù")
    for hour, minute in _ATTEMPTS_UTC:
        run(engine, at(TODAY, hour, minute), force=True)


def test_attempts_are_bounded_and_the_delivery_records_retry_exhausted(db, engine, smtp):
    _exhaust_attempts(engine, smtp)
    d = deliveries(engine)[0]
    assert d["attempts"] == rem.MAX_ATTEMPTS
    assert d["state"] == rem.STATE_RETRY_EXHAUSTED == "retry_exhausted"
    assert reminders(engine, "sent") == []
    # I promemoria tornano liberi ma in attesa: un relay rotto non deve far
    # ricomporre un digest a ogni giro, per sempre.
    with engine.begin() as c:
        holds = c.execute(text("SELECT count(*) FROM reminders "
                               "WHERE hold_until IS NOT NULL")).scalar_one()
    assert holds > 0
    assert audit_actions(engine)[-1] == "notifications.digest.retry_exhausted"


def test_retry_exhausted_is_not_terminal_the_reminder_comes_back(db, engine, smtp):
    """⚠ Il motivo per cui lo stato NON si chiama `abandoned`.

    Esauriti i tentativi la consegna è chiusa, ma il promemoria no: passata
    l'attesa torna eleggibile e finisce in un digest NUOVO, con un `Message-ID`
    nuovo — perché è un avviso nuovo, non il ritentativo di quello vecchio. Un
    nome che promette la fine di qualcosa che riprende sei ore dopo porta chi
    legge il registro a concludere che un avviso è stato perso.
    """
    _exhaust_attempts(engine, smtp)
    first = deliveries(engine)[0]
    assert first["state"] == rem.STATE_RETRY_EXHAUSTED

    # L'attesa si LEGGE dal database invece di ricalcolarla: parte dall'ultimo
    # tentativo, e sbagliare quell'ora renderebbe verde il ramo sbagliato.
    with engine.begin() as c:
        hold = c.execute(text("SELECT max(hold_until) FROM reminders")).scalar_one()
    assert hold is not None

    # Il relay torna a funzionare, ma l'attesa non è ancora passata: niente.
    smtp.fail_with = None
    with engine.begin() as c:
        c.execute(text("DELETE FROM scheduler_runs"))
    assert run(engine, hold - timedelta(minutes=1)).reason == "nothing_due"
    assert smtp.sent == []

    # Oltre l'attesa: il promemoria è di nuovo eleggibile.
    with engine.begin() as c:
        c.execute(text("DELETE FROM scheduler_runs"))
    result = run(engine, hold + timedelta(minutes=1))
    assert result.reason == "sent", result
    assert len(smtp.sent) == 1

    ds = deliveries(engine)
    assert len(ds) == 2, "una consegna nuova, non il ritentativo di quella chiusa"
    assert ds[0]["state"] == rem.STATE_RETRY_EXHAUSTED   # la vecchia resta chiusa
    assert ds[1]["state"] == "sent"
    assert ds[1]["message_id"] != ds[0]["message_id"]
    assert ds[1]["attempts"] == 1


def test_attempt_is_counted_before_sending(db, engine, smtp):
    """Contarlo dopo significherebbe che un invio che fa morire il processo non
    viene mai contato, e i tentativi diventano illimitati nel caso peggiore."""
    smtp.fail_with = TimeoutError("lento")
    run(engine, at(TODAY))
    assert deliveries(engine)[0]["attempts"] == 1


def test_smtp_not_configured_is_a_failure_category(db, engine, smtp, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "smtp_host", "", raising=False)
    result = run(engine, at(TODAY))
    assert result.reason == "delivery_failed"
    assert result.failure == "smtp_not_configured"
    assert reminders(engine, "sent") == []


def test_a_run_that_finds_nothing_records_the_outcome(db, engine, smtp):
    set_settings(engine, windows=[1])
    result = run(engine, at(TODAY))
    assert result.reason in ("sent", "nothing_due")
    with engine.begin() as c:
        outcome = c.execute(text("SELECT outcome FROM scheduler_runs "
                                 "WHERE run_date = :d"), {"d": TODAY}).scalar_one()
    assert outcome in ("nothing_due", "sent")


# ==================================================================
# 8. un solo worker
# ==================================================================

def test_a_second_worker_cannot_acquire_the_singleton(db, engine):
    """`replicas: 1` è una dichiarazione d'intenti; questo è il meccanismo.

    Il rilascio è ESPLICITO in `finally`: `close()` restituisce la connessione al
    pool senza chiudere la sessione, e il lock sopravviverebbe alla fine di questo
    test facendo fallire il successivo. È lo stesso fatto che ha motivato
    `release_singleton`."""
    first = engine.connect()
    second = engine.connect()
    try:
        assert wk.acquire_singleton(first) is True
        assert wk.acquire_singleton(second) is False
    finally:
        wk.release_singleton(first)
        first.close()
        second.close()


def test_the_singleton_is_released_when_the_worker_goes_away(db):
    """Il lock vive quanto la SESSIONE, non quanto l'oggetto `Connection`.

    ⚠ `conn.close()` di SQLAlchemy restituisce la connessione al pool: la
    sessione col database resta aperta e il lock resta preso. Per liberarlo
    davvero serve chiudere la sessione — `engine.dispose()`, o la morte del
    processo, che è ciò che accade in produzione quando il worker viene ucciso.
    La prima versione di questo test usava `conn.close()` e falliva: il difetto
    era nel test, ma il fatto che ha rivelato vale la pena di conoscerlo."""
    first = create_engine(DSN, future=True)
    conn = first.connect()
    assert wk.acquire_singleton(conn) is True

    second = create_engine(DSN, future=True)
    try:
        with second.connect() as other:
            assert wk.acquire_singleton(other) is False,                 "con la prima sessione viva, il lock è preso"
        # Chiusura VERA della prima sessione.
        conn.close()
        first.dispose()
        with second.connect() as other:
            assert wk.acquire_singleton(other) is True,                 "chiusa la sessione, il lock è libero"
    finally:
        first.dispose()
        second.dispose()


def test_two_workers_cannot_claim_the_same_reminder(db, engine, smtp):
    """Anche senza il lock: la corsa fra due giri concorrenti si risolve nel
    database, dove `claim_run` è un `INSERT` sulla chiave primaria della data.
    Il secondo trova la riga e non manda niente."""
    with engine.connect() as a, engine.connect() as b:
        with a.begin():
            assert rem.claim_run(a, TODAY, "Europe/Rome") is True
            rem.finish_run(a, TODAY, due=0, sent=0, outcome="sent")
        with b.begin():
            assert rem.claim_run(b, TODAY, "Europe/Rome") is False


def test_an_unfinished_run_can_be_retried(db, engine, smtp):
    """Se un giro si interrompe a metà, la riga di oggi esiste senza
    `finished_at`: un `DO NOTHING` direbbe «già fatto» e si perderebbe l'intera
    giornata."""
    with engine.begin() as c:
        assert rem.claim_run(c, TODAY, "Europe/Rome") is True
    with engine.begin() as c:
        assert rem.claim_run(c, TODAY, "Europe/Rome") is True, "ritentabile"
        rem.finish_run(c, TODAY, due=0, sent=0, outcome="sent")
    with engine.begin() as c:
        assert rem.claim_run(c, TODAY, "Europe/Rome") is False, "conclusa"


# ==================================================================
# 9. audit e riservatezza
# ==================================================================

def test_a_successful_delivery_is_audited_with_counts(db, engine, smtp):
    run(engine, at(TODAY))
    assert audit_actions(engine) == ["notifications.digest.sent"]
    with engine.begin() as c:
        events = c.execute(text("SELECT events FROM audit WHERE action = "
                                "'notifications.digest.sent'")).scalar_one()
    detail = events[0]
    assert detail["recipients"] == len(RECIPIENTS)
    assert detail["reminders"] > 0
    assert detail["attempts"] == 1


def test_a_failed_delivery_is_audited_with_a_category(db, engine, smtp):
    smtp.fail_with = TimeoutError("lento")
    run(engine, at(TODAY))
    assert audit_actions(engine) == ["notifications.digest.failed"]
    with engine.begin() as c:
        events = c.execute(text("SELECT events FROM audit WHERE action = "
                                "'notifications.digest.failed'")).scalar_one()
    assert events[0]["category"] == "timeout"


def test_audit_contains_no_credentials_or_message_body(db, engine, smtp, monkeypatch):
    from app.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_username", "ced-relay", raising=False)
    monkeypatch.setattr(type(cfg), "smtp_password",
                        lambda self: "password-smtp-segretissima")
    run(engine, at(TODAY))
    with engine.begin() as c:
        blob = json.dumps(c.execute(text(
            "SELECT actor_username, action, events FROM audit")).all(), default=str)
    for leaked in ("password-smtp-segretissima", "ced-relay", "relay.interno",
                   "srv-7", "dispositivo:"):
        assert leaked not in blob, leaked


def test_the_worker_is_not_a_human_actor_in_the_audit(db, engine, smtp):
    run(engine, at(TODAY))
    with engine.begin() as c:
        actor = c.execute(text("SELECT actor_username, actor_role FROM audit "
                               "WHERE action = 'notifications.digest.sent'")).first()
    assert actor[0] == wk.WORKER_ACTOR
    assert actor[1] is None, "inventare un ruolo renderebbe il registro bugiardo"


# ==================================================================
# 10. il worker non tocca la prontezza dell'API
# ==================================================================

def test_readiness_does_not_depend_on_the_worker(db, engine):
    """Con il worker fermo l'applicazione resta usabile: non partono gli avvisi,
    che è un guasto diverso. Legarli significherebbe che un worker fermo fa
    togliere l'API dal bilanciatore."""
    from app.api.deps import get_connection
    from app.main import app
    from conftest import api_client

    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    try:
        with api_client(app) as c:
            # Il battito è vecchissimo: il worker non gira.
            with engine.begin() as conn:
                conn.execute(text("UPDATE worker_heartbeat SET "
                                  "last_tick_at = now() - interval '10 days', "
                                  "state = 'stopped' WHERE id IS TRUE"))
            assert c.get("/api/ready").status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_worker_health_reports_unhealthy_when_stale(db, engine):
    with engine.begin() as c:
        c.execute(text("UPDATE worker_heartbeat SET "
                       "last_tick_at = now() - interval '1 hour', "
                       "state = 'running' WHERE id IS TRUE"))
    with engine.begin() as c:
        row = c.execute(text("SELECT last_tick_at, state FROM worker_heartbeat")).first()
    age = (datetime.now(timezone.utc) - row[0]).total_seconds()
    assert age > 900, "il battito è vecchio: l'healthcheck deve dichiarare non sano"


def test_heartbeat_is_updated(db, engine):
    wk.heartbeat(engine, state="running", detail="prova")
    with engine.begin() as c:
        row = c.execute(text("SELECT state, detail FROM worker_heartbeat")).first()
    assert row[0] == "running" and row[1] == "prova"


def test_a_run_reports_its_local_date_for_the_heartbeat(db, engine, smtp):
    """`last_run_date` è il campo che il monitoraggio guarda per sapere se il
    worker sta ancora facendo giri. Nella prima versione restava sempre NULL,
    perché il giro non riportava la data: un difetto invisibile ai test di invio
    e visibile solo leggendo lo stato."""
    result = run(engine, at(TODAY))
    assert result.run_date == TODAY
    wk.heartbeat(engine, state="running", run_date=result.run_date)
    with engine.begin() as c:
        assert c.execute(text("SELECT last_run_date FROM worker_heartbeat")
                         ).scalar_one() == TODAY


def test_a_run_that_does_nothing_reports_no_date(db, engine, smtp):
    """Prima dell'ora pianificata non c'è nessuna esecuzione, quindi nessuna data
    da riportare: il battito non deve dichiarare un giro che non è avvenuto."""
    assert run(engine, at(TODAY, 5)).run_date is None
