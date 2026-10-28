"""Blocco delle utenze dopo troppi accessi falliti consecutivi.

Regole (migrazione 0014):

- ogni accesso fallito su un'utenza ESISTENTE e attiva incrementa
  `users.failed_logins`; un accesso riuscito lo azzera;
- raggiunta la soglia di `security_policy.max_failed_logins` l'utenza si blocca
  (`users.locked_at`) e resta bloccata finché un amministratore non la sblocca:
  il blocco non scade da solo;
- un'utenza bloccata non entra nemmeno con la password giusta.

Perché un blocco permanente e non una finestra temporale: tutti gli operatori di
`tsm-prd` arrivano dal web gateway con lo stesso indirizzo, quindi l'unico
contatore che distingue le persone è quello sull'utenza. E un blocco che si
scioglie da solo dopo un quarto d'ora non dice a nessuno che qualcuno ha
provato a indovinare una password; uno sblocco esplicito sì, e lascia una riga
nel registro.

L'ultimo amministratore bloccato non resta chiuso fuori per sempre:
`scripts/unlock_user.py` sblocca dalla riga di comando del server.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth.audit import record_auth_event

USER_LOCKED = "users.locked"
USER_UNLOCKED = "users.unlocked"
SECURITY_POLICY_UPDATED = "security.policy.updated"

MIN_MAX_FAILED_LOGINS = 3
MAX_MAX_FAILED_LOGINS = 20
DEFAULT_MAX_FAILED_LOGINS = 5


class PolicyRejected(Exception):
    code = "security_policy_invalid"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SecurityPolicy:
    max_failed_logins: int
    updated_at: Any
    updated_by: str | None
    #: Recupero della password via email (migrazione 0015).
    password_reset_enabled: bool = True

    def as_dict(self) -> dict:
        return {
            "maxFailedLogins": self.max_failed_logins,
            "minMaxFailedLogins": MIN_MAX_FAILED_LOGINS,
            "maxMaxFailedLogins": MAX_MAX_FAILED_LOGINS,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "updatedBy": self.updated_by,
            "passwordResetEnabled": self.password_reset_enabled,
        }


def get_policy(conn: Connection) -> SecurityPolicy:
    row = conn.execute(text(
        "SELECT max_failed_logins, updated_at, updated_by, password_reset_enabled "
        "  FROM security_policy WHERE id = 1")).first()
    if row is None:
        # Senza riga si applica il predefinito, non «nessun limite»: un controllo
        # di sicurezza che si spegne quando manca un dato è un controllo spento.
        # Il recupero via email invece si spegne: è una porta in più, non un freno.
        return SecurityPolicy(DEFAULT_MAX_FAILED_LOGINS, None, None, False)
    return SecurityPolicy(int(row[0]), row[1], row[2], bool(row[3]))


_UNSET = object()


def set_policy(conn: Connection, max_failed_logins: Any = _UNSET, *, actor,
               password_reset_enabled: Any = _UNSET) -> SecurityPolicy:
    """Cambia uno o entrambi i campi. Un campo non passato resta com'è."""
    if max_failed_logins is _UNSET and password_reset_enabled is _UNSET:
        raise PolicyRejected("nessun campo da modificare")
    before = get_policy(conn)
    n = before.max_failed_logins
    enabled = before.password_reset_enabled
    if max_failed_logins is not _UNSET:
        if isinstance(max_failed_logins, bool) or not isinstance(max_failed_logins, int):
            raise PolicyRejected("la soglia deve essere un numero intero")
        if not MIN_MAX_FAILED_LOGINS <= max_failed_logins <= MAX_MAX_FAILED_LOGINS:
            raise PolicyRejected(
                f"la soglia deve essere compresa fra {MIN_MAX_FAILED_LOGINS} "
                f"e {MAX_MAX_FAILED_LOGINS}")
        n = max_failed_logins
    if password_reset_enabled is not _UNSET:
        if not isinstance(password_reset_enabled, bool):
            raise PolicyRejected("passwordResetEnabled deve essere true o false")
        enabled = password_reset_enabled
    conn.execute(text("""
        UPDATE security_policy
           SET max_failed_logins = :n, password_reset_enabled = :e,
               updated_at = now(), updated_by = :who
         WHERE id = 1
    """), {"n": n, "e": enabled, "who": actor.username})
    record_auth_event(conn, SECURITY_POLICY_UPDATED, username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"maxFailedLogins": n,
                              "previous": before.max_failed_logins,
                              "passwordResetEnabled": enabled,
                              "previousPasswordResetEnabled":
                                  before.password_reset_enabled})
    return get_policy(conn)


def register_failure(conn: Connection, user_id: Any, username: str,
                     ip: str | None) -> bool:
    """Conta un accesso fallito. Restituisce True se l'utenza è (ora) bloccata.

    Un solo UPDATE, con il confronto con la soglia dentro: due tentativi
    concorrenti non possono leggere lo stesso contatore e superare entrambi la
    soglia senza che nessuno dei due blocchi.
    """
    limit = get_policy(conn).max_failed_logins
    row = conn.execute(text("""
        UPDATE users
           SET failed_logins = failed_logins + 1,
               locked_at = CASE
                   WHEN locked_at IS NULL AND failed_logins + 1 >= :lim THEN now()
                   ELSE locked_at END
         WHERE id = :uid AND disabled_at IS NULL
     RETURNING failed_logins, locked_at,
               (locked_at IS NOT NULL AND failed_logins = :lim) AS just_locked
    """), {"uid": user_id, "lim": limit}).first()
    if row is None:
        return False
    if row[2]:
        record_auth_event(conn, USER_LOCKED, username=username, user_id=user_id,
                          ip=ip, result="denied", detail={"failedLogins": int(row[0]),
                                         "threshold": limit})
    return row[1] is not None


def reset_failures(conn: Connection, user_id: Any) -> None:
    conn.execute(text(
        "UPDATE users SET failed_logins = 0 WHERE id = :uid AND failed_logins <> 0"),
        {"uid": user_id})


def unlock(conn: Connection, user_id: Any, *, actor_username: str,
           actor_user_id: Any = None, actor_role: str | None = None,
           ip: str | None = None) -> bool:
    """Sblocca e azzera il contatore. Restituisce True se era bloccata."""
    row = conn.execute(text("""
        UPDATE users u SET locked_at = NULL, failed_logins = 0, updated_at = now()
          FROM (SELECT id, username, locked_at FROM users WHERE id = :uid FOR UPDATE) old
         WHERE u.id = old.id
     RETURNING old.username, old.locked_at
    """), {"uid": user_id}).first()
    if row is None or row[1] is None:
        return False
    record_auth_event(conn, USER_UNLOCKED, username=actor_username,
                      user_id=actor_user_id, role=actor_role, ip=ip,
                      detail={"targetUsername": str(row[0])})
    return True
