"""0.4.3: email in HTML con logo e failover fra relay. Senza database."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as N

import pytest

from app.notifications import smtp as smtp_mod
from app.notifications.digest import build_digest
from app.notifications.smtp import (SmtpSendFailed, build_reset_message,
                                    build_test_message, deliver, relay_hosts)

NOW = dt.datetime(2026, 10, 29, 13, 0, tzinfo=dt.timezone.utc)


def _parts(msg):
    return [p.get_content_type() for p in msg.walk()]


def _html(msg):
    return msg.get_body(("html",)).get_content()


def _item(dev, days, kind="garanzia"):
    return {"item": N(kind=kind, expiry=NOW.date() + dt.timedelta(days=days), days_remaining=days,
                      device=dev, rack="R01", room="Backend", location="Pomezia", entity_uid="u"),
            "threshold_days": 30}


@pytest.mark.parametrize("msg", [
    build_test_message("tsm@x.it", ["a@x.it"], actor_username="admin", now=NOW),
    build_reset_message("tsm@x.it", "a@x.it", username="op", code="ABCD-EFGH-JKMN",
                        expires_minutes=30, link="https://10.11.43.230/#reset?u=op&c=ABCD-EFGH-JKMN"),
    build_digest([_item("srv-01", 3)], sender="tsm@x.it", recipients=["a@x.it"],
                 message_id="<m@x>", now=NOW, today=NOW.date()),
])
def test_every_mail_has_text_html_and_inline_logo(msg):
    assert _parts(msg) == ["multipart/alternative", "text/plain", "multipart/related",
                           "text/html", "image/png"]
    logo = [p for p in msg.walk() if p.get_content_type() == "image/png"][0]
    cid = logo["Content-ID"].strip("<>")
    assert f"cid:{cid}" in _html(msg)
    assert "http://" not in _html(msg).replace("https://10.11.43.230", "")   # niente immagini remote
    assert msg.get_body(("plain",)).get_content().strip()                      # il testo resta


def test_reset_mail_shows_code_and_link():
    m = build_reset_message("tsm@x.it", "a@x.it", username="op", code="ABCD-EFGH-JKMN",
                            expires_minutes=30, link="https://h/#reset?u=op&c=ABCD-EFGH-JKMN")
    h = _html(m)
    assert "ABCD-EFGH-JKMN" in h and "30 minuti" in h and "https://h/#reset" in h
    assert "ABCD-EFGH-JKMN" in m.get_body(("plain",)).get_content()


def test_inventory_text_is_escaped_in_html():
    m = build_digest([_item('srv-<script>alert(1)</script>', 3)], sender="tsm@x.it",
                     recipients=["a@x.it"], message_id="<m@x>", now=NOW, today=NOW.date())
    h = _html(m)
    assert "<script>" not in h and "&lt;script&gt;" in h


def test_digest_marks_urgent_items():
    m = build_digest([_item("srv-a", 2), _item("srv-b", 60, "supporto")], sender="tsm@x.it",
                     recipients=["a@x.it"], message_id="<m@x>", now=NOW, today=NOW.date())
    h = _html(m)
    assert "entro 7 giorni" in h and "fra 2 giorni" in h and "fra 60 giorni" in h


# ------------------------------------------------------------ failover fra relay

class _Fake:
    sent: list = []
    down: set = set()

    def __init__(self, host, port, timeout=None, **kw):
        if host in _Fake.down:
            raise ConnectionRefusedError(f"{host} giù")
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        pass

    def ehlo(self, *a):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        if self.host == "rifiuta":
            import smtplib
            raise smtplib.SMTPRecipientsRefused({"a@x.it": (550, b"no")})
        _Fake.sent.append(self.host)


@pytest.fixture
def relay(monkeypatch):
    def settings(host):
        s = N(smtp_host=host, smtp_port=25, smtp_tls_mode="none", smtp_tls_verify=True,
              smtp_timeout_seconds=5, smtp_username="", smtp_sender="tsm@x.it",
              smtp_configured=lambda: True, smtp_password=lambda: "")
        monkeypatch.setattr(smtp_mod, "get_settings", lambda: s)
    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", _Fake)
    _Fake.sent, _Fake.down = [], set()
    return settings


def _msg():
    return build_test_message("tsm@x.it", ["a@x.it"], actor_username="admin", now=NOW)


def test_hosts_are_a_comma_list(relay):
    relay(" legolas1 , legolas2 ,")
    assert relay_hosts() == ["legolas1", "legolas2"]


def test_the_first_relay_is_used_when_up(relay):
    relay("legolas1,legolas2")
    deliver(_msg())
    assert _Fake.sent == ["legolas1"]


def test_falls_back_to_the_second_relay(relay):
    relay("legolas1,legolas2")
    _Fake.down = {"legolas1"}
    deliver(_msg())
    assert _Fake.sent == ["legolas2"]


def test_all_down_reports_connection_failed(relay):
    relay("legolas1,legolas2")
    _Fake.down = {"legolas1", "legolas2"}
    with pytest.raises(SmtpSendFailed) as e:
        deliver(_msg())
    assert e.value.reason == "connection_failed"


def test_a_refusal_does_not_fail_over(relay):
    relay("rifiuta,legolas2")
    with pytest.raises(SmtpSendFailed) as e:
        deliver(_msg())
    assert e.value.reason == "recipients_refused" and _Fake.sent == []
