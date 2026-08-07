"""Ripulitura dei dettagli di audit.

Si applica DUE VOLTE: quando l'evento si scrive e di nuovo quando si serializza
la risposta. Non è ridondanza inutile — è l'unica difesa che regge nel tempo.

Il registro è alimentato da produttori diversi, e ne arriveranno altri. Prima o
poi qualcuno metterà in `detail` un oggetto che contiene una chiave di troppo:
la ripulitura in scrittura evita che finisca su disco, quella in lettura evita
che esca comunque se è già finita su disco o se il produttore ha aggirato la
prima. Una sola delle due lascia scoperto metà del problema.

Riferimento: BACKEND-PLAN.md §8.36.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[rimosso]"

#: Frammenti che, comparendo nel NOME di una chiave, ne fanno rimuovere il valore.
#: Si guarda il nome e non il contenuto: indovinare cosa «sembra» un segreto
#: produce falsi negativi sui segreti nuovi e falsi positivi su tutto il resto.
SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "hash",
                       "credential", "apikey", "api_key", "authorization",
                       "cookie", "session")

#: Valori che sono riconoscibilmente un segreto anche se la chiave non lo dice.
#: Solo forme inequivocabili: un hash Argon2/bcrypt e una stringa di connessione
#: con credenziali. Niente euristiche generiche.
VALUE_PATTERNS = (
    re.compile(r"\$argon2[a-z]*\$"),
    re.compile(r"\$2[aby]\$\d{2}\$"),                       # bcrypt
    re.compile(r"\b\w+://[^\s:/@]+:[^\s:/@]+@"),            # dsn con password
)

#: Profondità e dimensione massime: un `detail` non è un posto dove riversare un
#: documento. Il troncamento è visibile, non silenzioso.
MAX_DEPTH = 6
MAX_ITEMS = 200
MAX_STRING = 2000


def _is_sensitive_key(key: str) -> bool:
    k = str(key).lower()
    return any(part in k for part in SENSITIVE_KEY_PARTS)


def _clean_string(value: str) -> str:
    for pattern in VALUE_PATTERNS:
        if pattern.search(value):
            return REDACTED
    if len(value) > MAX_STRING:
        return value[:MAX_STRING] + f"… [troncato, {len(value)} caratteri]"
    return value


def sanitize(value: Any, _depth: int = 0) -> Any:
    """Copia ripulita di `value`. Non modifica l'originale."""
    if _depth > MAX_DEPTH:
        return "[troppo annidato]"

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["…"] = f"[troncato, {len(value)} chiavi]"
                break
            out[str(k)] = REDACTED if _is_sensitive_key(k) else sanitize(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        items = list(value)[:MAX_ITEMS]
        cleaned = [sanitize(v, _depth + 1) for v in items]
        if len(value) > MAX_ITEMS:
            cleaned.append(f"[troncato, {len(value)} elementi]")
        return cleaned

    if isinstance(value, str):
        return _clean_string(value)

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return _clean_string(str(value))


def contains_secret(blob: str) -> bool:
    """Vero se una stringa serializzata contiene qualcosa di riconoscibilmente
    segreto. Usata dai test come rete finale sulla risposta completa."""
    lowered = blob.lower()
    if any(f'"{p}"' in lowered for p in SENSITIVE_KEY_PARTS):
        return True
    return any(p.search(blob) for p in VALUE_PATTERNS)
