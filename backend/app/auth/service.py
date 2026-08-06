"""Autenticazione: password, sessioni, attore.

Puro rispetto a HTTP: non conosce Request, Response o cookie. Sa di utenti,
sessioni e database. L'adattatore HTTP è in `app.api.auth`.

Riferimento: BACKEND-PLAN.md §8.1, §8.6, §8.20.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth.audit import (
    LOGIN_BLOCKED,
    LOGIN_FAILURE,
    LOGIN_SUCCESS,
    LOGOUT,
    PASSWORD_CHANGED,
    record_auth_event,
)
from app.auth.ratelimit import check_rate_limit, record_attempt
from app.inventory import Actor
from app.util import safe_ip

#: Parametri di default di argon2-cffi (argon2id). Non si inventano numeri:
#: la libreria aggiorna i propri default seguendo le raccomandazioni.
_hasher = PasswordHasher()

#: Durata della sessione. Il cookie non porta scadenza propria: quella che conta
#: è nel database, così una revoca ha effetto immediato.
SESSION_TTL = timedelta(hours=12)

#: Byte di entropia del token di sessione.
TOKEN_BYTES = 32

ROLES = ("view", "edit", "admin")

def _out_of_band(fn) -> None:
    """Esegue una scrittura in una transazione PROPRIA, che sopravvive al rollback.

    Serve alla registrazione dei tentativi falliti e al loro audit. Un accesso
    fallito fa sollevare l'handler e la transazione della richiesta viene
    annullata: se quei dati vivessero in quella transazione verrebbero cancellati
    insieme all'errore, e il limitatore non conterebbe mai nulla.

    Gli errori PROPAGANO. Chi chiama decide se sono fatali (§8.32).
    """
    from app.db import get_engine
    with get_engine().connect() as own:
        with own.begin():
            fn(own)


def _out_of_band_best_effort(fn, what: str) -> None:
    """Come sopra, ma un guasto si registra nei log e non cambia la risposta.

    Usata SOLO per l'audit di un tentativo fallito: non riuscire a scrivere una
    riga di registro è un problema, ma trasformare «credenziali errate» in
    «errore del server» è peggio — nasconde al client l'informazione vera e fa
    scattare i suoi ripristini automatici.
    """
    import logging
    try:
        _out_of_band(fn)
    except Exception:                                  # pragma: no cover
        logging.getLogger(__name__).exception(
            "scrittura fuori banda fallita: %s", what)


#: Hash di confronto per le utenze inesistenti. Generato all'avvio con gli stessi
#: parametri di quelli reali, così la verifica costa lo stesso tempo: è ciò che
#: rende l'enumerazione per tempo di risposta inutile. Non è una password valida
#: perché il valore in chiaro non esiste da nessuna parte.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


class AuthError(Exception):
    """Base. `code` è stabile, il messaggio è per gli umani."""
    code = "auth_error"

    def __init__(self, message: str = "autenticazione non valida"):
        super().__init__(message)
        self.message = message


class InvalidCredentials(AuthError):
    """Credenziali errate, utenza inesistente o disabilitata.

    Un solo errore per tutti e tre i casi di proposito: distinguerli direbbe a
    chi prova quali utenze esistono.
    """
    code = "invalid_credentials"


class NotAuthenticated(AuthError):
    code = "not_authenticated"


class PasswordChangeRequired(AuthError):
    """Password provvisoria: la sessione è valida ma può fare solo tre cose (§8.26)."""
    code = "password_change_required"


class RateLimiterUnavailable(AuthError):
    """Il contatore dei tentativi non è utilizzabile: si nega l'accesso.

    Non è pignoleria. Se i tentativi non si possono contare il limitatore non
    esiste, e non esistere IN SILENZIO significa tentativi illimitati mentre le
    risposte continuano a dire «credenziali errate, riprova». Meglio un errore di
    servizio, che è visibile e conservativo (§8.32).
    """
    code = "rate_limiter_unavailable"


class TooManyAttempts(AuthError):
    """Limitatore dei tentativi di accesso (§8.28)."""
    code = "too_many_attempts"

    def __init__(self, message: str, retry_after_seconds: int = 0):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class AuthenticatedUser:
    id: Any
    username: str
    role: str
    must_change_pw: bool

    def to_actor(self, ip: str | None = None) -> Actor:
        return Actor(username=self.username, role=self.role, user_id=self.id, ip=ip)


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def _token_hash(token: str) -> str:
    """Nel database va l'hash del token, non il token.

    SHA-256 e non argon2: il token ha 256 bit di entropia da un CSPRNG, quindi
    non è attaccabile per forza bruta e un hash lento non aggiungerebbe nulla
    tranne latenza su ogni richiesta autenticata.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- utenze

def create_user(conn: Connection, username: str, password: str, role: str, *,
                must_change_pw: bool = True, **profile) -> Any:
    """Crea un'utenza. Usata dalla CLI di bootstrap e (in seguito) dagli admin."""
    if role not in ROLES:
        raise ValueError(f"ruolo non valido: {role!r}")
    row = conn.execute(text("""
        INSERT INTO users (username, role, password_hash, must_change_pw,
                           nome, cognome, telefono, team)
        VALUES (:username, :role, :pw, :must_change,
                :nome, :cognome, :telefono, :team)
     RETURNING id
    """), {
        "username": username, "role": role, "pw": hash_password(password),
        "must_change": must_change_pw,
        "nome": profile.get("nome"), "cognome": profile.get("cognome"),
        "telefono": profile.get("telefono"), "team": profile.get("team"),
    }).first()
    return row[0]


def count_active_admins(conn: Connection) -> int:
    return conn.execute(text(
        "SELECT count(*) FROM users WHERE role = 'admin' AND disabled_at IS NULL"
    )).scalar_one()


# ------------------------------------------------------------------ sessioni

def login(conn: Connection, username: str, password: str, *,
          ip: str | None = None, user_agent: str | None = None) -> tuple[str, AuthenticatedUser]:
    """Verifica le credenziali e apre una sessione. Restituisce (token, utente).

    Il token è l'unico momento in cui esiste in chiaro: va nel cookie e non
    viene scritto da nessuna parte.
    """
    ip = safe_ip(ip)

    # --- limitazione, prima di toccare la password ---
    status = check_rate_limit(conn, username, ip)
    if status.blocked:
        _out_of_band_best_effort(
            lambda c: record_auth_event(c, LOGIN_BLOCKED, username=username, ip=ip,
                                       detail={"reason": status.reason}),
            "audit del blocco")
        raise TooManyAttempts(status.reason, status.retry_after_seconds)

    row = conn.execute(text("""
        SELECT id, username, role, password_hash, must_change_pw, disabled_at
          FROM users WHERE username = :u
    """), {"u": username}).first()

    # Verifica Argon2 anche quando l'utenza NON esiste, contro l'enumerazione:
    # senza, un'utenza inesistente risponderebbe in microsecondi e una esistente
    # in decine di millisecondi, e la differenza è misurabile da remoto.
    stored = row[3] if row else _DUMMY_HASH
    ok = verify_password(stored, password)

    if row is None or not ok or row[5] is not None:
        uid = row[0] if row else None
        urole = row[2] if row else None

        # 1. Il contatore DEVE persistere: senza, il limitatore è disattivato e
        #    nessuno se ne accorge. Se non si scrive, si nega l'accesso.
        try:
            _out_of_band(lambda c: record_attempt(c, username, ip, success=False))
        except Exception as exc:
            raise RateLimiterUnavailable(
                "impossibile registrare il tentativo di accesso") from exc

        # 2. L'audit è importante ma non deve poter peggiorare la risposta.
        _out_of_band_best_effort(
            lambda c: record_auth_event(c, LOGIN_FAILURE, username=username, ip=ip,
                                        user_id=uid, role=urole),
            "audit del tentativo fallito")
        # Un solo errore per utenza inesistente, password errata e utenza
        # disabilitata: distinguerli direbbe a chi prova quali utenze esistono.
        raise InvalidCredentials()

    token = secrets.token_urlsafe(TOKEN_BYTES)
    conn.execute(text("""
        INSERT INTO sessions (user_id, token_hash, expires_at, ip, user_agent, last_seen_at)
        VALUES (:uid, :th, :exp, :ip, :ua, now())
    """), {"uid": row[0], "th": _token_hash(token),
           "exp": _now() + SESSION_TTL, "ip": safe_ip(ip), "ua": user_agent})
    conn.execute(text("UPDATE users SET last_login_at = now() WHERE id = :uid"),
                 {"uid": row[0]})
    try:
        record_attempt(conn, username, ip, success=True)
    except Exception as exc:
        # Anche qui si fallisce chiuso: un limitatore che non registra i successi
        # non è affidabile, e non si distingue dall'esterno da uno guasto sui
        # fallimenti. La transazione porta via anche la sessione appena creata.
        raise RateLimiterUnavailable(
            "impossibile registrare il tentativo di accesso") from exc
    record_auth_event(conn, LOGIN_SUCCESS, username=str(row[1]), user_id=row[0],
                      role=row[2], ip=ip,
                      detail={"mustChangePassword": bool(row[4])})

    return token, AuthenticatedUser(id=row[0], username=str(row[1]), role=row[2],
                                    must_change_pw=bool(row[4]))


def resolve_session(conn: Connection, token: str | None) -> AuthenticatedUser:
    """Sessione → utente. Solleva `NotAuthenticated` se non è valida.

    Ricontrolla `disabled_at` a ogni richiesta e non si fida della sessione:
    disattivare un'utenza deve avere effetto subito, non alla scadenza del
    cookie (§8.6).
    """
    if not token:
        raise NotAuthenticated("nessun cookie di sessione")

    row = conn.execute(text("""
        SELECT s.id, u.id, u.username, u.role, u.must_change_pw
          FROM sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = :th
           AND s.revoked_at IS NULL
           AND s.expires_at > now()
           AND u.disabled_at IS NULL
    """), {"th": _token_hash(token)}).first()
    if row is None:
        raise NotAuthenticated("sessione inesistente, scaduta o revocata")

    conn.execute(text("UPDATE sessions SET last_seen_at = now() WHERE id = :sid"),
                 {"sid": row[0]})
    return AuthenticatedUser(id=row[1], username=str(row[2]), role=row[3],
                             must_change_pw=bool(row[4]))


def logout(conn: Connection, token: str | None, *, ip: str | None = None) -> None:
    if not token:
        return
    row = conn.execute(text("""
        SELECT u.id, u.username, u.role FROM sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = :th AND s.revoked_at IS NULL
    """), {"th": _token_hash(token)}).first()
    conn.execute(text("""
        UPDATE sessions SET revoked_at = now()
         WHERE token_hash = :th AND revoked_at IS NULL
    """), {"th": _token_hash(token)})
    if row is not None:
        record_auth_event(conn, LOGOUT, username=str(row[1]), user_id=row[0],
                          role=row[2], ip=safe_ip(ip))


def revoke_all_sessions(conn: Connection, user_id: Any) -> int:
    """Revoca tutte le sessioni di un utente. Da chiamare alla disattivazione e
    al cambio password: una password cambiata non deve lasciare in giro sessioni
    aperte con quella vecchia."""
    result = conn.execute(text("""
        UPDATE sessions SET revoked_at = now()
         WHERE user_id = :uid AND revoked_at IS NULL
    """), {"uid": user_id})
    return result.rowcount or 0


def change_own_password(conn: Connection, user_id: Any,
                        current_password: str, new_password: str) -> None:
    """Cambio password proprio. Azzera `must_change_pw` e revoca le altre
    sessioni."""
    row = conn.execute(text(
        "SELECT password_hash FROM users WHERE id = :uid AND disabled_at IS NULL"),
        {"uid": user_id}).first()
    if row is None or not verify_password(row[0], current_password):
        raise InvalidCredentials("password attuale errata")
    if not new_password or len(new_password) < 10:
        raise AuthError("la nuova password deve avere almeno 10 caratteri")

    conn.execute(text("""
        UPDATE users SET password_hash = :pw, must_change_pw = FALSE, updated_at = now()
         WHERE id = :uid
    """), {"pw": hash_password(new_password), "uid": user_id})
    # TUTTE le sessioni, compresa quella che sta facendo il cambio: chi cambia
    # password si aspetta che le altre sessioni cadano, e la propria ripartirà da
    # un accesso nuovo. Non si registra nulla della password (§8.25).
    revoked = revoke_all_sessions(conn, user_id)
    who = conn.execute(text("SELECT username, role FROM users WHERE id = :uid"),
                       {"uid": user_id}).first()
    record_auth_event(conn, PASSWORD_CHANGED,
                      username=str(who[0]) if who else None, user_id=user_id,
                      role=who[1] if who else None,
                      detail={"revokedSessions": revoked})


def disable_user(conn: Connection, user_id: Any) -> None:
    """Disattivazione logica: mai DELETE (§8.6).

    Non si può disattivare l'ultimo admin attivo, e il controllo sta DENTRO la
    transazione del chiamante: farlo prima aprirebbe una finestra in cui due
    disattivazioni concorrenti lasciano il sistema senza amministratori.
    """
    row = conn.execute(text(
        "SELECT role, disabled_at FROM users WHERE id = :uid FOR UPDATE"),
        {"uid": user_id}).first()
    if row is None or row[1] is not None:
        return
    if row[0] == "admin" and count_active_admins(conn) <= 1:
        raise AuthError("non si può disattivare l'ultimo amministratore attivo")
    conn.execute(text(
        "UPDATE users SET disabled_at = now(), updated_at = now() WHERE id = :uid"),
        {"uid": user_id})
    revoke_all_sessions(conn, user_id)
