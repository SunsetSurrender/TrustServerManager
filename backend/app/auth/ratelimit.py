"""Limitazione dei tentativi di accesso.

Durevole (tabella `login_attempts`) e non in memoria: un contatore in memoria si
azzera a ogni riavvio, che è precisamente il momento in cui chi insiste ne
approfitta, e non sopravvive a più repliche.

Due difese, perché rispondono a due attacchi diversi:

  per utenza  → qualcuno prova molte password su UNA persona: blocco
                dell'utenza, in `app.auth.lockout`
  per IP      → qualcuno prova UNA password su molte utenze (password spraying),
                che il contatore per utenza non vedrebbe mai: finestra qui

⚠ Su `tsm-prd` tutti arrivano dal web gateway, quindi l'IP è condiviso: il limite
per IP è alto (`TSM_LOGIN_MAX_FAILURES_PER_IP`, predefinito 100) e serve solo
contro un tentativo in massa.

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

    # Il contatore PER UTENZA non sta più qui: dalla migrazione 0014 è il blocco
    # dell'utenza (`app.auth.lockout`), permanente fino allo sblocco di un
    # amministratore. Una finestra temporale sulla stessa utenza sarebbe un
    # secondo limite, più basso o più alto della soglia scelta nell'interfaccia,
    # che decide al posto suo.
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
