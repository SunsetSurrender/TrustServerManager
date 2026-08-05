"""Audit degli eventi di autenticazione.

Si registra CHE COSA è accaduto e a chi, mai le credenziali. In particolare non
si registra la password, né la sua lunghezza, né un suo hash: un hash in un
registro consultabile è attaccabile offline, e la lunghezza è comunque
informazione che restringe il campo.

Per un tentativo fallito si registra l'utenza **tentata**: non è una credenziale,
ed è l'informazione che serve a chi legge il registro per capire se qualcuno sta
provando nomi a caso o insiste su una persona precisa.

Riferimento: BACKEND-PLAN.md §8.25.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

LOGIN_SUCCESS = "auth.login.success"
LOGIN_FAILURE = "auth.login.failure"
LOGIN_BLOCKED = "auth.login.blocked"
LOGOUT = "auth.logout"
PASSWORD_CHANGED = "auth.password.changed"

#: L'utenza tentata arriva dalla richiesta: va limitata come qualsiasi input non
#: attendibile prima di finire in una colonna.
MAX_USERNAME_AUDIT_CHARS = 200


def record_auth_event(conn: Connection, action: str, *,
                      username: str | None = None,
                      user_id: Any = None,
                      role: str | None = None,
                      ip: str | None = None,
                      detail: dict | None = None) -> None:
    """Una riga di audit per un evento di autenticazione.

    `inventory_version` resta NULL: questi eventi non toccano l'inventario.
    `actor_role` può essere NULL (migrazione 0005): un accesso fallito non ha un
    ruolo, e inventarne uno in un registro di audit è peggio di lasciarlo vuoto.
    """
    import json

    safe_username = (username or "")[:MAX_USERNAME_AUDIT_CHARS] or "(sconosciuto)"
    conn.execute(text("""
        INSERT INTO audit (actor_user_id, actor_username, actor_role, ip,
                           inventory_version, action, scopes, events, client_hint)
        VALUES (:user_id, :username, :role, :ip,
                NULL, :action, '{}'::text[], :events, NULL)
    """), {
        "user_id": user_id,
        "username": safe_username,
        "role": role,
        "ip": ip,
        "action": action,
        "events": json.dumps([detail] if detail else []),
    })
