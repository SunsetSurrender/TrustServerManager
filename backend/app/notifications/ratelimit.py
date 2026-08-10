"""Limitazione degli invii di prova. Separata da quella degli accessi.

Sono due limitatori perché proteggono da due danni diversi. Quello degli accessi
(§8.28) ferma chi prova password; questo ferma la generazione di posta. Un invio
di prova è autenticato, amministrativo e legittimo: il problema non è chi non
dovrebbe essere lì, ma una sessione di amministratore compromessa — o un
pulsante premuto in un ciclo — che trasforma il servizio in un generatore di
messaggi verso indirizzi reali.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import get_settings
from app.util import safe_ip

#: Chiave del lock consultivo. Serve a serializzare prenotazione e conteggio:
#: sotto READ COMMITTED due richieste simultanee conterebbero entrambe «due
#: invii finora», passerebbero entrambe e ne partirebbero tre. Il lock è di
#: transazione, quindi si rilascia da solo al commit o al rollback.
_LOCK_KEY = 0x7473_6D6E_74      # "tsmnt"


class NotificationTestRateLimited(Exception):
    """Troppi invii di prova nella finestra."""

    code = "notification_test_rate_limited"

    def __init__(self, message: str, retry_after_seconds: int = 0,
                 scope: str = ""):
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        self.scope = scope


class NotificationLimiterUnavailable(Exception):
    """Il contatore non è utilizzabile: si NEGA l'invio.

    Stessa logica del limitatore degli accessi (§8.32): un limite che non si può
    contare non esiste, e non esistere in silenzio significa nessun limite
    mentre le risposte continuano a sembrare normali.
    """

    code = "notification_test_unavailable"

    def __init__(self, message: str = "limitatore degli invii non utilizzabile"):
        super().__init__(message)
        self.message = message


def reserve_slot(conn: Connection, *, actor_user_id, ip: str | None) -> None:
    """Conta e prenota, nella STESSA transazione. Solleva se il limite è pieno.

    La riga si scrive PRIMA dell'invio, non dopo. Se si registrasse alla fine,
    dieci richieste concorrenti troverebbero tutte il contatore a zero e
    partirebbero tutte; e un invio che va in timeout dopo dieci secondi non
    verrebbe contato affatto, che è precisamente il caso in cui qualcuno
    riprova.

    Il chiamante deve usare una transazione PROPRIA (fuori banda): la
    prenotazione deve sopravvivere anche se la richiesta poi fallisce.
    """
    s = get_settings()
    window = f"{s.notification_test_window_seconds} seconds"

    try:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})

        total = conn.execute(text(f"""
            SELECT count(*) FROM notification_test_attempts
             WHERE ts > now() - interval '{window}'
        """)).scalar_one()

        mine = conn.execute(text(f"""
            SELECT count(*) FROM notification_test_attempts
             WHERE actor_user_id = :a AND ts > now() - interval '{window}'
        """), {"a": actor_user_id}).scalar_one()
    except Exception as exc:                                # pragma: no cover
        raise NotificationLimiterUnavailable() from exc

    if mine >= s.notification_test_max_per_actor:
        raise NotificationTestRateLimited(
            "troppi invii di prova da questa utenza",
            s.notification_test_window_seconds, "actor")
    # Il limite complessivo non è ridondante: senza, N amministratori
    # (o N sessioni della stessa persona su utenze diverse) moltiplicherebbero
    # il tetto, e il relay vedrebbe comunque un volume che non ci si aspetta.
    if total >= s.notification_test_max_global:
        raise NotificationTestRateLimited(
            "troppi invii di prova sul servizio",
            s.notification_test_window_seconds, "global")

    try:
        conn.execute(text("""
            INSERT INTO notification_test_attempts (actor_user_id, ip)
            VALUES (:a, :ip)
        """), {"a": actor_user_id, "ip": safe_ip(ip)})
    except Exception as exc:                                # pragma: no cover
        raise NotificationLimiterUnavailable() from exc
