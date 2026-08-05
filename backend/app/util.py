"""Utilità minime condivise."""
from __future__ import annotations

import ipaddress


def safe_ip(value: str | None) -> str | None:
    """Indirizzo IP valido, oppure None.

    Le colonne `ip` sono di tipo `inet`: un valore non valido fa fallire l'INSERT,
    e un'operazione che riesce non deve poter essere annullata da un dettaglio
    diagnostico. Casi reali in cui `request.client.host` non è un IP: il
    TestClient di Starlette usa la stringa "testclient", dietro un socket unix il
    peer non ha indirizzo, e un `X-Forwarded-For` malformato può contenere
    qualsiasi cosa.

    Perdere l'IP è accettabile; perdere la scrittura no.
    """
    if not value:
        return None
    candidate = value.strip()
    # Un X-Forwarded-For può essere una lista: conta il primo, che è il client.
    if "," in candidate:
        candidate = candidate.split(",", 1)[0].strip()
    # Forma [::1]:1234 o 1.2.3.4:1234
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
