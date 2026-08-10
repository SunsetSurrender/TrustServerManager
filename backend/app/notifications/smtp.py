"""Invio del messaggio di prova. Nessun parametro dal chiamante.

Questo modulo NON è un relay. Non accetta destinatario, oggetto o corpo: prende
i destinatari dalle impostazioni salvate e compone un messaggio scritto qui. È
la differenza fra «verifica che la posta funzioni» e «manda una mail a chi
vuoi», e la prima si ottiene solo togliendo alla seconda ogni appiglio — una
sessione di amministratore compromessa non deve poter diventare un mittente
anonimo verso indirizzi arbitrari.

Riservatezza degli errori
-------------------------
Un'eccezione di `smtplib` contiene l'host del relay, a volte l'utenza, e la
risposta completa del server. Nella risposta HTTP va solo una CATEGORIA scelta
da un elenco chiuso; il testo integrale finisce nei log, dove serve a chi opera
e non a chi sonda.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger(__name__)

#: Quanti destinatari riceve la PROVA. Le impostazioni ne ammettono venti, ma un
#: invio di prova non deve poter diventare un invio di massa: se il relay
#: funziona per tre indirizzi funziona per tutti, e la differenza fra tre e venti
#: è solo quanta posta si genera premendo un pulsante.
MAX_TEST_RECIPIENTS = 3

SUBJECT = "Trust Server Manager — messaggio di prova"


class SmtpNotConfigured(Exception):
    """Il trasporto non è configurato: non c'è niente da tentare."""

    code = "smtp_not_configured"

    def __init__(self, message: str = "invio email non configurato"):
        super().__init__(message)
        self.message = message


class NoRecipients(Exception):
    """Nessun destinatario nelle impostazioni salvate.

    Non è un errore del trasporto ed è l'amministratore a poterlo risolvere,
    quindi merita un codice suo invece di finire fra i guasti SMTP.
    """

    code = "no_recipients_configured"

    def __init__(self, message: str = "nessun destinatario configurato"):
        super().__init__(message)
        self.message = message


class SmtpSendFailed(Exception):
    """Invio non riuscito. `reason` è una categoria, MAI il testo dell'errore."""

    code = "smtp_send_failed"

    #: Elenco chiuso. Se un giorno servisse una categoria nuova la si aggiunge
    #: qui consapevolmente; nessun percorso può farne comparire una che contenga
    #: testo proveniente dal server di posta.
    REASONS = ("connection_failed", "timeout", "auth_failed",
               "recipients_refused", "sender_refused", "tls_failed",
               "protocol_error")

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason if reason in self.REASONS else "protocol_error"
        self.message = message


@dataclass(frozen=True)
class SendOutcome:
    recipients: int


def choose_test_recipients(configured: list[str]) -> list[str]:
    """I primi `MAX_TEST_RECIPIENTS` fra quelli SALVATI. Nient'altro è ammesso."""
    return [r for r in configured if r][:MAX_TEST_RECIPIENTS]


def build_test_message(sender: str, recipients: list[str],
                       *, actor_username: str, now: datetime | None = None) -> EmailMessage:
    """Il messaggio, composto interamente qui.

    Oggetto e corpo sono costanti più due dati del server (istante e utenza che
    ha chiesto la prova). L'utenza c'è perché chi riceve il messaggio deve poter
    capire da chi arriva senza chiedere in giro; è un dato del server, non un
    testo del client.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(
        "Questo è un messaggio di prova di Trust Server Manager.\n\n"
        f"Richiesto da: {actor_username}\n"
        f"Istante: {stamp}\n\n"
        "Se lo stai leggendo, la configurazione di invio funziona: gli avvisi "
        "di scadenza raggiungeranno questo indirizzo.\n\n"
        "Nessuna azione richiesta.\n"
    )
    return msg


def _connect(s, context: ssl.SSLContext | None):
    if s.smtp_tls_mode == "tls":
        return smtplib.SMTP_SSL(s.smtp_host, s.smtp_port,
                                timeout=s.smtp_timeout_seconds, context=context)
    return smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)


def send_test_message(recipients: list[str], *, actor_username: str) -> SendOutcome:
    """Invia UN messaggio di prova. Solleva con una categoria, mai con un testo."""
    s = get_settings()
    if not s.smtp_configured():
        raise SmtpNotConfigured()
    targets = choose_test_recipients(recipients)
    if not targets:
        raise NoRecipients()

    context: ssl.SSLContext | None = None
    if s.smtp_tls_mode in ("tls", "starttls"):
        context = ssl.create_default_context()
        if not s.smtp_tls_verify:
            # Deroga esplicita dell'operations, per un relay interno con
            # certificato non riconosciuto. Non è un ripiego automatico: senza
            # la variabile impostata, un certificato non valido fa fallire
            # l'invio invece di farlo passare in silenzio.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

    msg = build_test_message(s.smtp_sender, targets, actor_username=actor_username)

    # ⚠ L'ORDINE DELLE CLAUSOLE È SOSTANZIALE, non stilistico.
    #
    # `smtplib.SMTPException` DERIVA DA `OSError`, e così `ssl.SSLError` e
    # `TimeoutError`. Una clausola `except OSError` messa troppo in alto
    # intercetta quindi anche gli errori di protocollo e li etichetta come
    # «connessione non riuscita»: la risposta manda chi legge a controllare la
    # rete mentre il relay ha risposto benissimo, dicendo no. Il caso è stato
    # trovato da un test, non a ragionamento — dal più specifico al più generico,
    # con `OSError` per ultimo.
    try:
        with _connect(s, context) as server:
            if s.smtp_tls_mode == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if s.smtp_username.strip():
                server.login(s.smtp_username, s.smtp_password())
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        log.exception("invio di prova: autenticazione SMTP rifiutata")
        raise SmtpSendFailed("auth_failed",
                             "il server di posta ha rifiutato le credenziali") from None
    except smtplib.SMTPRecipientsRefused:
        log.exception("invio di prova: destinatari rifiutati")
        raise SmtpSendFailed("recipients_refused",
                             "il server di posta ha rifiutato i destinatari") from None
    except smtplib.SMTPSenderRefused:
        log.exception("invio di prova: mittente rifiutato")
        raise SmtpSendFailed("sender_refused",
                             "il server di posta ha rifiutato il mittente") from None
    except (ssl.SSLError, smtplib.SMTPNotSupportedError):
        log.exception("invio di prova: negoziazione TLS non riuscita")
        raise SmtpSendFailed("tls_failed",
                             "negoziazione TLS con il server di posta non riuscita") from None
    except TimeoutError:
        # `socket.timeout` È `TimeoutError` da Python 3.10.
        log.exception("invio di prova: timeout verso il server di posta")
        raise SmtpSendFailed("timeout",
                             "il server di posta non ha risposto entro il tempo massimo") from None
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
        log.exception("invio di prova: connessione al server di posta non riuscita")
        raise SmtpSendFailed("connection_failed",
                             "server di posta non raggiungibile") from None
    except smtplib.SMTPException:
        # Il relay ha risposto, e ha risposto no. Va distinto dal non averlo
        # raggiunto: sono due indagini diverse per chi deve rimediare.
        log.exception("invio di prova: errore di protocollo SMTP")
        raise SmtpSendFailed("protocol_error",
                             "il server di posta ha risposto con un errore") from None
    except OSError:
        # Livello socket: connessione rifiutata, DNS, rete irraggiungibile.
        # ULTIMA clausola, perché è la più generica delle tre famiglie.
        log.exception("invio di prova: connessione al server di posta non riuscita")
        raise SmtpSendFailed("connection_failed",
                             "server di posta non raggiungibile") from None

    log.info("invio di prova riuscito verso %d destinatari", len(targets))
    return SendOutcome(recipients=len(targets))
