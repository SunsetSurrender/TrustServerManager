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

#: Esiti possibili di un evento (colonna `result`, migrazione 0006).
RESULT_SUCCESS = "success"
RESULT_FAILURE = "failure"
RESULT_DENIED = "denied"

LOGIN_SUCCESS = "auth.login.success"
LOGIN_FAILURE = "auth.login.failure"
LOGIN_BLOCKED = "auth.login.blocked"
LOGOUT = "auth.logout"
PASSWORD_CHANGED = "auth.password.changed"

#: L'hash è stato ricalcolato con i parametri Argon2 correnti dopo un accesso
#: riuscito (§8.43). Si registra perché è l'unico modo che ha chi opera di sapere
#: che la migrazione degli hash sta procedendo, e quando è finita: non ci sono
#: query che possano dirlo senza leggere i parametri dentro gli hash, cosa che
#: nessuna rotta fa e non deve fare. Il dettaglio è VUOTO di proposito — né hash,
#: né parametri di partenza, che direbbero quanto era debole quell'utenza.
PASSWORD_REHASHED = "auth.password.rehashed"

#: L'utenza tentata arriva dalla richiesta: va limitata come qualsiasi input non
#: attendibile prima di finire in una colonna.
MAX_USERNAME_AUDIT_CHARS = 200


#: Azioni il cui esito non è "success". Sta qui, accanto alla scrittura, invece
#: che nella lettura: dedurlo dal nome dell'azione al momento della query sarebbe
#: la stessa regola in due punti, e i due punti prima o poi divergono.
_RESULT_BY_ACTION = {
    LOGIN_FAILURE: RESULT_FAILURE,
    LOGIN_BLOCKED: RESULT_DENIED,
}


def record_auth_event(conn: Connection, action: str, *,
                      username: str | None = None,
                      user_id: Any = None,
                      role: str | None = None,
                      ip: str | None = None,
                      result: str | None = None,
                      detail: dict | None = None) -> None:
    """Una riga di audit per un evento di autenticazione.

    `inventory_version` resta NULL: questi eventi non toccano l'inventario.
    `actor_role` può essere NULL (migrazione 0005): un accesso fallito non ha un
    ruolo, e inventarne uno in un registro di audit è peggio di lasciarlo vuoto.
    """
    import json

    from app.audit.sanitize import sanitize

    safe_username = (username or "")[:MAX_USERNAME_AUDIT_CHARS] or "(sconosciuto)"
    # Prima ripulitura, in scrittura: un dettaglio con una chiave di troppo non
    # deve nemmeno arrivare su disco (§8.36). La seconda avviene in lettura.
    safe_detail = sanitize(detail) if detail else None

    conn.execute(text("""
        INSERT INTO audit (actor_user_id, actor_username, actor_role, ip,
                           inventory_version, action, result, scopes, events,
                           client_hint)
        VALUES (:user_id, :username, :role, :ip,
                NULL, :action, :result, '{}'::text[], :events, NULL)
    """), {
        "user_id": user_id,
        "username": safe_username,
        "role": role,
        "ip": ip,
        "action": action,
        "result": result or _RESULT_BY_ACTION.get(action, RESULT_SUCCESS),
        "events": json.dumps([safe_detail] if safe_detail else []),
    })
