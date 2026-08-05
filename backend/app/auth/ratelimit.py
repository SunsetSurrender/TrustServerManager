"""Limitazione dei tentativi di accesso.

Durevole (tabella `login_attempts`) e non in memoria: un contatore in memoria si
azzera a ogni riavvio, che è precisamente il momento in cui chi insiste ne
approfitta, e non sopravvive a più repliche.

Due finestre, perché rispondono a due attacchi diversi:

  per utenza  → qualcuno prova molte password su UNA persona
  per IP      → qualcuno prova UNA password su molte utenze (password spraying),
                che il contatore per utenza non vedrebbe mai

Riferimento: BACKEND-PLAN.md §8.28.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import get_settings


@dataclass(frozen=True)
class RateLimitStatus:
    blocked: bool
    reason: str = ""
    retry_after_seconds: int = 0


def record_attempt(conn: Connection, username: str, ip: str | None,
                   success: bool) -> None:
    conn.execute(text("""
        INSERT INTO login_attempts (username, ip, success)
        VALUES (:u, :ip, :ok)
    """), {"u": (username or "")[:200], "ip": ip, "ok": success})


def check_rate_limit(conn: Connection, username: str,
                     ip: str | None) -> RateLimitStatus:
    """Stato del limitatore per questa coppia (utenza, IP).

    Si contano i soli tentativi FALLITI: un accesso riuscito non deve avvicinare
    l'utente al blocco, altrimenti chi lavora normalmente verrebbe punito.
    """
    s = get_settings()
    window = f"{s.login_failure_window_seconds} seconds"

    by_username = conn.execute(text(f"""
        SELECT count(*) FROM login_attempts
         WHERE username = :u AND success = FALSE
           AND ts > now() - interval '{window}'
    """), {"u": (username or "")[:200]}).scalar_one()

    if by_username >= s.login_max_failures_per_username:
        return RateLimitStatus(
            True, "troppi tentativi per questa utenza",
            s.login_failure_window_seconds)

    if ip:
        by_ip = conn.execute(text(f"""
            SELECT count(*) FROM login_attempts
             WHERE ip = :ip AND success = FALSE
               AND ts > now() - interval '{window}'
        """), {"ip": ip}).scalar_one()
        if by_ip >= s.login_max_failures_per_ip:
            return RateLimitStatus(
                True, "troppi tentativi da questo indirizzo",
                s.login_failure_window_seconds)

    return RateLimitStatus(False)
