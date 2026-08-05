"""Gestione delle utenze da parte degli amministratori. Logica pura.

Regole che non sono negoziabili e che vivono qui, non nelle rotte:

  - **Nessuna cancellazione fisica.** `audit.actor_user_id` punta a `users`, e
    cancellare un utente romperebbe la tracciabilità che è il motivo per cui
    l'audit è stato spostato sul server (§8.6). Il ruolo di runtime non ha
    nemmeno il privilegio `DELETE` (§8.19).

  - **L'ultimo amministratore attivo è protetto**, sia dalla disattivazione sia
    dalla retrocessione di ruolo. Sono due modi di ottenere lo stesso danno — un
    sistema senza nessuno che possa amministrarlo — e vanno bloccati entrambi.

  - **Ogni controllo di unicità o di conteggio sta DENTRO la transazione** e usa
    `FOR UPDATE`. Verificare prima aprirebbe una finestra in cui due richieste
    concorrenti passano entrambe: due retrocessioni simultanee dell'ultimo e del
    penultimo admin lascerebbero zero amministratori.

Riferimento: BACKEND-PLAN.md §8.30.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth.audit import record_auth_event
from app.auth.service import (
    ROLES,
    AuthError,
    count_active_admins,
    hash_password,
    revoke_all_sessions,
)

USER_CREATED = "users.created"
USER_UPDATED = "users.updated"
USER_DISABLED = "users.disabled"
USER_ENABLED = "users.enabled"
USER_PASSWORD_RESET = "users.password_reset"

#: Lunghezza della password provvisoria generata. Non viene mai registrata: torna
#: una volta sola nella risposta all'amministratore che l'ha chiesta.
TEMP_PASSWORD_BYTES = 12

PROFILE_FIELDS = ("nome", "cognome", "telefono", "team")


class UserError(AuthError):
    code = "user_error"


class UsernameTaken(UserError):
    code = "username_taken"


class UserNotFound(UserError):
    code = "user_not_found"


class LastAdminProtected(UserError):
    """Non si può togliere l'ultimo amministratore attivo, in nessun modo."""
    code = "last_admin_protected"


@dataclass(frozen=True)
class UserRow:
    id: Any
    username: str
    role: str
    must_change_pw: bool
    disabled: bool
    nome: str | None
    cognome: str | None
    telefono: str | None
    team: str | None
    last_login_at: Any
    created_at: Any

    def as_dict(self) -> dict:
        return {
            "id": str(self.id), "username": self.username, "role": self.role,
            "mustChangePassword": self.must_change_pw, "disabled": self.disabled,
            "nome": self.nome, "cognome": self.cognome,
            "telefono": self.telefono, "team": self.team,
            "lastLoginAt": self.last_login_at.isoformat() if self.last_login_at else None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


_SELECT = """
    SELECT id, username, role, must_change_pw, (disabled_at IS NOT NULL) AS disabled,
           nome, cognome, telefono, team, last_login_at, created_at
      FROM users
"""


def _row(r) -> UserRow:
    return UserRow(id=r[0], username=str(r[1]), role=r[2], must_change_pw=bool(r[3]),
                   disabled=bool(r[4]), nome=r[5], cognome=r[6], telefono=r[7],
                   team=r[8], last_login_at=r[9], created_at=r[10])


def list_users(conn: Connection, *, include_disabled: bool = False) -> list[UserRow]:
    sql = _SELECT + ("" if include_disabled else " WHERE disabled_at IS NULL")
    sql += " ORDER BY username"
    return [_row(r) for r in conn.execute(text(sql))]


def get_user(conn: Connection, user_id: Any) -> UserRow:
    r = conn.execute(text(_SELECT + " WHERE id = :id"), {"id": user_id}).first()
    if r is None:
        raise UserNotFound(f"utenza inesistente: {user_id}")
    return _row(r)


def _generate_temp_password() -> str:
    return secrets.token_urlsafe(TEMP_PASSWORD_BYTES)


def create_user(conn: Connection, *, username: str, role: str, actor,
                password: str | None = None, **profile) -> tuple[UserRow, str]:
    """Crea un'utenza con password **provvisoria**. Restituisce (utenza, password).

    La password torna al chiamante una volta sola e non viene registrata da
    nessuna parte (§8.25): l'amministratore la comunica alla persona, che al primo
    accesso è obbligata a cambiarla.
    """
    if role not in ROLES:
        raise UserError(f"ruolo non valido: {role!r}")
    username = (username or "").strip()
    if not username:
        raise UserError("username obbligatorio")

    # Unicità dentro la transazione: `username` è unico anche fra i disabilitati,
    # quindi riusare il nome di un utente disattivato è una RIATTIVAZIONE
    # esplicita, non un inserimento — che darebbe un errore di vincolo
    # incomprensibile all'amministratore (§8.6).
    existing = conn.execute(text(
        "SELECT id, disabled_at FROM users WHERE username = :u FOR UPDATE"),
        {"u": username}).first()
    if existing is not None:
        raise UsernameTaken(
            f"l'utenza {username!r} esiste già"
            + (" ed è disattivata: riattivarla invece di ricrearla"
               if existing[1] is not None else ""))

    temp = password or _generate_temp_password()
    row = conn.execute(text("""
        INSERT INTO users (username, role, password_hash, must_change_pw,
                           nome, cognome, telefono, team)
        VALUES (:u, :r, :pw, TRUE, :nome, :cognome, :telefono, :team)
     RETURNING id
    """), {"u": username, "r": role, "pw": hash_password(temp),
           **{f: profile.get(f) for f in PROFILE_FIELDS}}).first()

    record_auth_event(conn, USER_CREATED, username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"targetUsername": username, "targetRole": role})
    return get_user(conn, row[0]), temp


def update_user(conn: Connection, user_id: Any, *, actor,
                role: str | None = None, **profile) -> UserRow:
    """Cambia ruolo e/o profilo.

    Retrocedere l'ultimo amministratore attivo è vietato: è il secondo modo di
    ottenere un sistema senza amministratori, e va bloccato come la
    disattivazione.
    """
    current = conn.execute(text(
        "SELECT role, disabled_at FROM users WHERE id = :id FOR UPDATE"),
        {"id": user_id}).first()
    if current is None:
        raise UserNotFound(f"utenza inesistente: {user_id}")

    changes: dict[str, Any] = {}
    if role is not None and role != current[0]:
        if role not in ROLES:
            raise UserError(f"ruolo non valido: {role!r}")
        if (current[0] == "admin" and current[1] is None
                and count_active_admins(conn) <= 1):
            raise LastAdminProtected(
                "non si può retrocedere l'ultimo amministratore attivo")
        changes["role"] = role

    for f in PROFILE_FIELDS:
        if f in profile:
            changes[f] = profile[f]

    if not changes:
        return get_user(conn, user_id)

    assignments = ", ".join(f"{k} = :{k}" for k in changes)
    conn.execute(text(f"UPDATE users SET {assignments}, updated_at = now() "
                      f"WHERE id = :id"), {**changes, "id": user_id})

    record_auth_event(conn, USER_UPDATED, username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"targetUserId": str(user_id),
                              "changedFields": sorted(changes)})
    return get_user(conn, user_id)


def set_disabled(conn: Connection, user_id: Any, disabled: bool, *, actor) -> UserRow:
    """Disattivazione logica o riattivazione. Mai `DELETE` (§8.6)."""
    current = conn.execute(text(
        "SELECT role, disabled_at, username FROM users WHERE id = :id FOR UPDATE"),
        {"id": user_id}).first()
    if current is None:
        raise UserNotFound(f"utenza inesistente: {user_id}")

    already = current[1] is not None
    if already == disabled:
        return get_user(conn, user_id)

    if disabled:
        if str(user_id) == str(actor.user_id):
            raise UserError("non si può disattivare la propria utenza")
        if current[0] == "admin" and count_active_admins(conn) <= 1:
            raise LastAdminProtected(
                "non si può disattivare l'ultimo amministratore attivo")
        conn.execute(text("UPDATE users SET disabled_at = now(), updated_at = now() "
                          "WHERE id = :id"), {"id": user_id})
        # Le sessioni cadono subito: senza, l'utente resterebbe operativo fino
        # alla scadenza del cookie, cioè per ore dopo la disattivazione.
        revoked = revoke_all_sessions(conn, user_id)
        record_auth_event(conn, USER_DISABLED, username=actor.username,
                          user_id=actor.user_id, role=actor.role, ip=actor.ip,
                          detail={"targetUsername": str(current[2]),
                                  "revokedSessions": revoked})
    else:
        conn.execute(text("UPDATE users SET disabled_at = NULL, updated_at = now() "
                          "WHERE id = :id"), {"id": user_id})
        record_auth_event(conn, USER_ENABLED, username=actor.username,
                          user_id=actor.user_id, role=actor.role, ip=actor.ip,
                          detail={"targetUsername": str(current[2])})
    return get_user(conn, user_id)


def reset_password(conn: Connection, user_id: Any, *, actor) -> tuple[UserRow, str]:
    """Reimposta a una password provvisoria e revoca le sessioni.

    Restituisce la password una volta sola. Revocare è necessario: senza, chi
    aveva la sessione aperta continuerebbe a operare come se niente fosse, e un
    reset chiesto perché la password è compromessa non servirebbe a nulla.
    """
    current = conn.execute(text(
        "SELECT username FROM users WHERE id = :id FOR UPDATE"),
        {"id": user_id}).first()
    if current is None:
        raise UserNotFound(f"utenza inesistente: {user_id}")

    temp = _generate_temp_password()
    conn.execute(text("""
        UPDATE users SET password_hash = :pw, must_change_pw = TRUE, updated_at = now()
         WHERE id = :id
    """), {"pw": hash_password(temp), "id": user_id})
    revoked = revoke_all_sessions(conn, user_id)

    record_auth_event(conn, USER_PASSWORD_RESET, username=actor.username,
                      user_id=actor.user_id, role=actor.role, ip=actor.ip,
                      detail={"targetUsername": str(current[0]),
                              "revokedSessions": revoked})
    return get_user(conn, user_id), temp
