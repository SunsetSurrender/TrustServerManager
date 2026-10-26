"""Controllo dell'origine sulle richieste che modificano stato (§8.27).

Senza `Origin`, il controllo ricade su `Referer`. Il confronto era un
`startswith` sull'origine nuda: `https://tsm.example` accettava anche
`https://tsm.example.evil.test/…` e `https://tsm.example:8443/…`, cioè un
altro host o un'altra porta. Qui si fissa che conta l'origine ESATTA.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.api import request_context as rc
from app.api.deps import SESSION_COOKIE


class _Settings:
    def __init__(self, origins: tuple[str, ...]):
        self._origins = origins

    def allowed_origins(self) -> tuple[str, ...]:
        return self._origins

    def trusted_proxy_list(self) -> tuple[str, ...]:
        return ("172.16.0.0/12",)


def _request(*, referer: str | None = None, origin: str | None = None,
             method: str = "POST", cookie: bool = True) -> Request:
    headers = [(b"host", b"tsm.example")]
    if cookie:
        headers.append((b"cookie", f"{SESSION_COOKIE}=x".encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    # Il peer è il nostro nginx: X-Forwarded-Proto è creduto.
    headers.append((b"x-forwarded-proto", b"https"))
    scope = {"type": "http", "method": method, "path": "/api/x", "headers": headers,
             "scheme": "http", "server": ("api", 8000), "client": ("172.18.0.5", 40000),
             "query_string": b""}
    return Request(scope)


@pytest.fixture(params=[(), ("https://tsm.example",)], ids=["senza-origine-configurata", "con-origine-configurata"])
def settings(request, monkeypatch):
    monkeypatch.setattr(rc, "get_settings", lambda: _Settings(request.param))
    return request.param


@pytest.mark.parametrize("referer", [
    "https://tsm.example",
    "https://tsm.example/",
    "https://tsm.example/sala?x=1",
])
def test_referer_della_stessa_origine_accettato(settings, referer):
    assert rc.origin_is_acceptable(_request(referer=referer))[0]


@pytest.mark.parametrize("referer", [
    "https://tsm.example.evil.test/",
    "https://tsm.example.evil.test",
    "https://tsm.example:8443/",
    "https://tsm.exampleX/",
    "http://tsm.example/",
])
def test_referer_di_un_altra_origine_rifiutato(settings, referer):
    assert not rc.origin_is_acceptable(_request(referer=referer))[0]


def test_origin_esatta_ancora_richiesta(settings):
    assert rc.origin_is_acceptable(_request(origin="https://tsm.example"))[0]
    assert not rc.origin_is_acceptable(_request(origin="https://tsm.example.evil.test"))[0]


def test_senza_cookie_o_in_lettura_non_si_controlla(settings):
    assert rc.origin_is_acceptable(_request(referer="https://evil.test/", cookie=False))[0]
    assert rc.origin_is_acceptable(_request(referer="https://evil.test/", method="GET"))[0]


def test_senza_origin_ne_referer_rifiutato(settings):
    assert not rc.origin_is_acceptable(_request())[0]
