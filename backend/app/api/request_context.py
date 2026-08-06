"""Che cosa si può credere di una richiesta.

Due domande, entrambe con la stessa forma: quale parte di ciò che arriva è
affermata da noi e quale dal client?

  - l'IP del client   → dagli header solo se il peer è il nostro proxy (§8.28)
  - l'origine         → deve combaciare, sulle richieste che modificano (§8.27)
"""
from __future__ import annotations

import ipaddress
import logging

from fastapi import Request

from app.config import get_settings
from app.util import safe_ip

log = logging.getLogger(__name__)

#: Metodi che modificano stato. Solo per questi serve la validazione di origine:
#: una GET non cambia niente, e pretendere `Origin` su di essa romperebbe la
#: navigazione normale senza guadagnare nulla.
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _peer_is_trusted(peer: str | None) -> bool:
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for entry in get_settings().trusted_proxy_list():
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            log.warning("voce non valida in TSM_TRUSTED_PROXIES: %r", entry)
    return False


def client_ip(request: Request) -> str | None:
    """IP del client, credendo a `X-Forwarded-For` SOLO se il peer è il proxy.

    Fidarsi dell'header da chiunque significa lasciare che il client dichiari il
    proprio indirizzo — e con esso aggiri la limitazione dei tentativi di accesso
    semplicemente cambiando una stringa a ogni richiesta.

    Si prende l'ULTIMA voce della catena, non la prima. Il nostro nginx
    sovrascrive l'header con il solo `$remote_addr` (§8.34), quindi la voce è una
    sola e le due scelte coincidono — ma se un giorno un proxy davanti accodasse
    invece di sovrascrivere, l'ultima voce resterebbe quella scritta da un
    componente fidato, mentre la prima sarebbe quella scelta dal chiamante.
    Fra due letture equivalenti oggi si sceglie quella che regge domani.
    """
    peer = request.client.host if request.client else None
    if _peer_is_trusted(peer):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            for candidate in reversed(parts):
                ip = safe_ip(candidate)
                if ip:
                    return ip
    return safe_ip(peer)


def _self_origin(request: Request) -> str:
    """L'origine con cui il CLIENT ci ha raggiunti, non quella dell'ultimo salto.

    Dietro un proxy che termina TLS, `request.url.scheme` è `http`: è lo schema
    della connessione fra nginx e l'API, non quello del browser. Confrontare
    `Origin: https://host` con quello produce un rifiuto su ogni richiesta che
    modifica stato — bug reale, trovato dal test end-to-end attraverso nginx e
    invisibile chiamando l'API direttamente.

    Lo schema vero lo dichiara `X-Forwarded-Proto`, ma **solo se il peer è il
    nostro proxy**: altrimenti sarebbe il client a decidere da quale origine
    sembra arrivare, e il controllo non varrebbe niente.
    """
    scheme = request.url.scheme
    host = request.headers.get("host", "")
    peer = request.client.host if request.client else None
    if _peer_is_trusted(peer):
        forwarded_proto = request.headers.get("x-forwarded-proto")
        if forwarded_proto:
            scheme = forwarded_proto.split(",")[0].strip()
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_host:
            host = forwarded_host.split(",")[0].strip()
    return f"{scheme}://{host}".rstrip("/")


def origin_is_acceptable(request: Request) -> tuple[bool, str]:
    """L'origine della richiesta è la nostra? Restituisce (ok, motivo).

    Si applica solo alle richieste che modificano stato **e portano il cookie di
    sessione**: senza cookie non c'è autorità da abusare, e pretendere `Origin`
    romperebbe i client non-browser senza proteggere nulla.

    Il cookie è già `SameSite=strict`, quindi un browser non lo invia da un altro
    sito. Questo controllo è il secondo livello: copre il caso stesso-sito ma
    origine diversa, e un eventuale difetto nella gestione di SameSite.

    NON si abilita CORS con credenziali: non esiste un caso d'uso in cui un altro
    sito debba poter chiamare questa API con il cookie dell'utente, e abilitarlo
    smonterebbe da solo tutto il resto.
    """
    from app.api.deps import SESSION_COOKIE

    if request.method not in STATE_CHANGING:
        return True, ""
    if SESSION_COOKIE not in request.cookies:
        return True, ""

    allowed = get_settings().allowed_origins()
    origin = request.headers.get("origin")

    if origin:
        if not allowed:
            # Nessuna origine configurata: si accetta solo l'origine con cui il
            # client ci ha raggiunti, invece di accettare qualsiasi cosa.
            expected = _self_origin(request)
            return (origin.rstrip("/") == expected,
                    f"Origin {origin!r} non combacia con {expected!r}")
        return (origin.rstrip("/") in allowed,
                f"Origin {origin!r} non è fra le origini consentite")

    # Nessun `Origin`: alcuni browser lo omettono su richieste same-origin
    # non-CORS. Si ricade su `Referer`, che in quel caso c'è.
    referer = request.headers.get("referer")
    if referer:
        for candidate in (allowed or ()):
            if referer.startswith(candidate + "/") or referer.rstrip("/") == candidate:
                return True, ""
        if not allowed:
            expected = _self_origin(request)
            return (referer.startswith(expected),
                    f"Referer {referer!r} non combacia con {expected!r}")
        return False, f"Referer {referer!r} non è fra le origini consentite"

    return False, ("richiesta con cookie di sessione e senza Origin né Referer: "
                   "rifiutata")
