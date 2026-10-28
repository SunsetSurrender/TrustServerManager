"""Recupero della password via email (migrazione 0015).

Flusso:

1. dalla schermata di accesso si chiede il recupero indicando l'UTENZA;
2. se l'utenza esiste, è attiva, NON è bloccata e ha un indirizzo email, si
   genera un codice monouso e lo si manda per posta;
3. con utenza + codice si imposta una password nuova (stessa politica di sempre).

Scelte, e perché:

- **Risposta sempre identica.** La richiesta risponde 202 con lo stesso testo in
  ogni caso: utenza inesistente, senza email, bloccata, recupero spento, SMTP
  non configurato. Altrimenti il modulo diventerebbe un modo per scoprire quali
  utenze esistono. Il motivo vero finisce nel registro di audit, che vedono solo
  gli amministratori.

- **Codice corto e ridigitabile**, non solo un link: su tsm-prd si arriva dal web
  gateway, e il link della mail spesso non si apre dove la mail si legge. Il
  codice (`ABCD-EFGH-JKMN`, 12 simboli da 31, circa 59 bit) basta perché:
  scade in 30 minuti, si usa una volta, e dopo 5 tentativi sbagliati si
  invalida. La mail contiene comunque anche il link, per chi può aprirlo.

- **Si conserva solo l'hash** del codice (SHA-256: il codice è casuale, non una
  password scelta da una persona, quindi non serve un hash lento).

- **Un'utenza bloccata NON si sblocca da sola.** Il blocco dopo troppi accessi
  falliti lo toglie solo un amministratore: se il recupero via email lo
  scavalcasse, chi prova a indovinare una password avrebbe un secondo ingresso.
  Per un'utenza bloccata la mail non parte; e se il blocco arriva DOPO la
  richiesta, il codice viene rifiutato.

- **Al massimo 3 richieste all'ora per utenza**, e una richiesta nuova invalida
  i codici precedenti: in giro c'è sempre un codice solo.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth import lockout
from app.auth.audit import record_auth_event
from app.auth.passwords import PasswordRejected, check_policy, hash_password, verify_password
from app.config import get_settings

RESET_REQUESTED = "auth.password_reset.requested"
RESET_COMPLETED = "auth.password_reset.completed"
RESET_FAILED = "auth.password_reset.failed"

CODE_TTL = timedelta(minutes=30)
MAX_ATTEMPTS_PER_CODE = 5
MAX_REQUESTS_PER_HOUR = 3

#: Stesso alfabeto delle provvisorie, in MAIUSCOLO: niente 0/O, 1/I/L.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS = 3
CODE_GROUP_LEN = 4


class ResetCodeInvalid(Exception):
    """Codice errato, scaduto, già usato o utenza non utilizzabile. Un solo errore."""
    code = "reset_code_invalid"

    def __init__(self, message: str = "codice non valido o scaduto: chiedine uno nuovo"):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ResetMail:
    """Ciò che serve per spedire. Esiste solo se la mail deve davvero partire."""
    to: str
    username: str
    code: str
    expires_minutes: int


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def normalise_code(raw: str) -> str:
    """`abcd efgh-jkmn` → `ABCD-EFGH-JKMN`. Tollera spazi, trattini e minuscole."""
    s = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(s) != CODE_GROUPS * CODE_GROUP_LEN:
        return ""
    return "-".join(s[i:i + CODE_GROUP_LEN]
                    for i in range(0, len(s), CODE_GROUP_LEN))


def generate_code() -> str:
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUP_LEN))
                    for _ in range(CODE_GROUPS))


def is_available(conn: Connection) -> bool:
    """Il recupero è utilizzabile: acceso nella politica E posta configurata."""
    return (lockout.get_policy(conn).password_reset_enabled
            and get_settings().smtp_configured())


def request_reset(conn: Connection, username: str, *, ip: str | None) -> ResetMail | None:
    """Registra la richiesta. Restituisce la mail da spedire, oppure None.

    Il chiamante risponde allo stesso modo in entrambi i casi.
    """
    username = (username or "").strip()[:200]

    def esito(motivo: str, user_id: Any = None) -> None:
        record_auth_event(conn, RESET_REQUESTED, username=username or None,
                          user_id=user_id, ip=ip,
                          result="success" if motivo == "inviato" else "denied",
                          detail={"esito": motivo})

    if not username:
        return None
    if not lockout.get_policy(conn).password_reset_enabled:
        esito("recupero disattivato")
        return None
    if not get_settings().smtp_configured():
        esito("posta non configurata")
        return None

    row = conn.execute(text("""
        SELECT id, email, disabled_at, locked_at FROM users
         WHERE username = :u FOR UPDATE
    """), {"u": username}).first()
    if row is None:
        esito("utenza inesistente")
        return None
    uid, email, disabled_at, locked_at = row
    if disabled_at is not None:
        esito("utenza disattivata", uid)
        return None
    if locked_at is not None:
        esito("utenza bloccata: serve lo sblocco di un amministratore", uid)
        return None
    if not email:
        esito("nessun indirizzo email", uid)
        return None

    recenti = conn.execute(text("""
        SELECT count(*) FROM recovery_codes
         WHERE user_id = :uid AND created_at > now() - interval '1 hour'
    """), {"uid": uid}).scalar_one()
    if recenti >= MAX_REQUESTS_PER_HOUR:
        esito("troppe richieste nell'ultima ora", uid)
        return None

    # Un codice solo alla volta: i precedenti ancora validi decadono.
    conn.execute(text("""
        UPDATE recovery_codes SET invalidated_at = now()
         WHERE user_id = :uid AND used_at IS NULL AND invalidated_at IS NULL
    """), {"uid": uid})
    code = generate_code()
    conn.execute(text("""
        INSERT INTO recovery_codes (user_id, code_hash, expires_at, requested_ip)
        VALUES (:uid, :h, now() + :ttl, :ip)
    """), {"uid": uid, "h": _hash(code), "ttl": CODE_TTL, "ip": ip})
    esito("inviato", uid)
    return ResetMail(to=str(email), username=username, code=code,
                     expires_minutes=int(CODE_TTL.total_seconds() // 60))


def confirm_reset(conn: Connection, username: str, code: str, new_password: str,
                  *, ip: str | None, out_of_band) -> None:
    """Imposta la nuova password se utenza e codice corrispondono.

    `out_of_band(fn)` esegue `fn(conn)` in una transazione propria: il conteggio
    dei tentativi sbagliati deve sopravvivere al rollback della richiesta, che
    fallisce proprio perché il codice è sbagliato.
    """
    username = (username or "").strip()[:200]
    norm = normalise_code(code)

    row = conn.execute(text("""
        SELECT u.id, u.disabled_at, u.locked_at, u.password_hash,
               t.id, t.code_hash, t.expires_at > now(), t.failed_attempts
          FROM users u
          LEFT JOIN LATERAL (
                SELECT id, code_hash, expires_at, failed_attempts
                  FROM recovery_codes
                 WHERE user_id = u.id AND used_at IS NULL AND invalidated_at IS NULL
                 ORDER BY created_at DESC LIMIT 1) t ON TRUE
         WHERE u.username = :u
    """), {"u": username}).first()

    def fallito(motivo: str, user_id: Any = None, token_id: Any = None,
                conta: bool = False) -> ResetCodeInvalid:
        def scrivi(c):
            if conta and token_id is not None:
                c.execute(text("""
                    UPDATE recovery_codes
                       SET failed_attempts = failed_attempts + 1,
                           invalidated_at = CASE WHEN failed_attempts + 1 >= :max
                                                 THEN now() ELSE invalidated_at END
                     WHERE id = :tid
                """), {"tid": token_id, "max": MAX_ATTEMPTS_PER_CODE})
            record_auth_event(c, RESET_FAILED, username=username or None,
                              user_id=user_id, ip=ip, result="denied",
                              detail={"esito": motivo})
        out_of_band(scrivi)
        return ResetCodeInvalid()

    if row is None:
        raise fallito("utenza inesistente")
    uid, disabled_at, locked_at, pw_hash, tid, code_hash, valid, _fails = row
    if disabled_at is not None:
        raise fallito("utenza disattivata", uid)
    if tid is None:
        raise fallito("nessun codice attivo", uid)
    if not valid:
        raise fallito("codice scaduto", uid, tid)
    if not norm or not secrets.compare_digest(_hash(norm), code_hash):
        raise fallito("codice errato", uid, tid, conta=True)
    if locked_at is not None:
        raise fallito("utenza bloccata dopo la richiesta", uid, tid)

    # Il codice è giusto: da qui gli errori riguardano la PASSWORD, e il codice
    # resta valido per riprovare con una password migliore.
    new_password = check_policy(new_password, username=username)
    if verify_password(pw_hash, new_password):
        raise PasswordRejected(
            "password_unchanged",
            "la nuova password deve essere diversa da quella attuale")

    conn.execute(text("""
        UPDATE users SET password_hash = :pw, must_change_pw = FALSE,
                         failed_logins = 0, updated_at = now()
         WHERE id = :uid
    """), {"pw": hash_password(new_password), "uid": uid})
    conn.execute(text(
        "UPDATE recovery_codes SET used_at = now() WHERE id = :tid"),
        {"tid": tid})
    revoked = conn.execute(text("""
        UPDATE sessions SET revoked_at = now()
         WHERE user_id = :uid AND revoked_at IS NULL
    """), {"uid": uid}).rowcount
    record_auth_event(conn, RESET_COMPLETED, username=username, user_id=uid, ip=ip,
                      detail={"revokedSessions": revoked})
