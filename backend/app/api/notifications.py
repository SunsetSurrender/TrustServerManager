"""Invio di prova delle notifiche. Solo amministratori.

    POST /api/notifications/test

Il corpo deve essere vuoto o `{}`. Non esiste `to`, non esiste `subject`, non
esiste un corpo del messaggio: destinatari e testo vengono dalle impostazioni
salvate e da questo codice. Un endpoint che accettasse quei campi sarebbe un
relay di posta autenticato, e la differenza fra «verifica la configurazione» e
«manda quello che vuoi a chi vuoi» sta tutta in ciò che non si accetta.

Ordine delle operazioni, e perché è quello
------------------------------------------
    1. controlli che non generano posta   (configurazione, destinatari)
    2. prenotazione del limite            (transazione propria, committata)
    3. invio                              (rete, con timeout)
    4. audit dell'esito                   (transazione propria, best-effort)

La prenotazione sta PRIMA dell'invio: registrandola dopo, dieci richieste
simultanee troverebbero tutte il contatore a zero, e un invio che va in timeout
non verrebbe contato affatto — che è proprio il caso in cui qualcuno riprova.
Sta invece DOPO i controlli che non mandano niente, perché consumare un
tentativo per dire «SMTP non configurato» punirebbe l'amministratore per un
problema che non è suo.

L'asimmetria del punto 4 è deliberata: vedi sotto.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from sqlalchemy.engine import Connection

from app.api.deps import NO_STORE, get_connection, require_admin
from app.auth.audit import (
    RESULT_DENIED,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    record_auth_event,
)
from app.db import get_engine
from app.inventory import Actor
from app.notifications import (
    NoRecipients,
    NotificationLimiterUnavailable,
    NotificationTestRateLimited,
    SmtpNotConfigured,
    SmtpSendFailed,
    reserve_slot,
    send_test_message,
)
from app.settings import SettingsCorrupted, SettingsMissing, copy_notifications
from app.settings import repository as repo

router = APIRouter()
log = logging.getLogger(__name__)

NOTIFICATION_TEST = "notifications.test"


def _own_transaction(fn):
    """Una transazione propria, che non dipende da quella della richiesta.

    La prenotazione del limite deve sopravvivere a un fallimento successivo, e
    l'audit dell'esito avviene DOPO l'invio — cioè dopo aver fatto qualcosa che
    il database non può annullare.
    """
    with get_engine().connect() as own:
        with own.begin():
            return fn(own)


def _audit(actor: Actor, *, result: str, detail: dict) -> bool:
    """Registra l'esito. Restituisce se ci è riuscito, senza mai sollevare."""
    try:
        _own_transaction(lambda c: record_auth_event(
            c, NOTIFICATION_TEST, username=actor.username,
            user_id=actor.user_id, role=actor.role, ip=actor.ip,
            result=result, detail=detail))
        return True
    except Exception:                                       # pragma: no cover
        log.exception("audit dell'invio di prova non riuscito (esito: %s)", result)
        return False


@router.post("/notifications/test",
             summary="Invia un messaggio di prova ai destinatari configurati")
def test_notification(response: Response,
                      payload: Any = Body(default=None),
                      conn: Connection = Depends(get_connection),
                      actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)

    # Il corpo può essere assente, vuoto o `{}`. Qualunque chiave viene
    # rifiutata: è il controllo che impedisce all'endpoint di crescere in un
    # relay, e va fatto qui e non «più avanti», perché più avanti non c'è.
    if payload not in (None, "", {}):
        raise HTTPException(
            422,
            detail={"code": "unexpected_fields",
                    "message": "questo endpoint non accetta parametri: "
                               "destinatari e testo vengono dalle impostazioni "
                               "salvate"},
            headers=NO_STORE)

    # --- 1. controlli che non generano posta ---
    try:
        row = repo.load(conn)
    except (SettingsMissing, SettingsCorrupted) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None

    # I destinatari sono quelli COMMITTATI. Non quelli che il client ha in
    # pagina: provare una configurazione non salvata direbbe che funziona
    # qualcosa che non è quello che poi verrà usato.
    recipients = copy_notifications(row.data)["recipients"]

    # --- 2. prenotazione del limite, in una transazione propria ---
    try:
        _own_transaction(lambda c: reserve_slot(
            c, actor_user_id=actor.user_id, ip=actor.ip))
    except NotificationTestRateLimited as exc:
        _audit(actor, result=RESULT_DENIED,
               detail={"reason": "rate_limited", "scope": exc.scope})
        headers = dict(NO_STORE)
        if exc.retry_after_seconds:
            headers["Retry-After"] = str(exc.retry_after_seconds)
        raise HTTPException(429, detail={"code": exc.code, "message": exc.message},
                            headers=headers) from None
    except NotificationLimiterUnavailable as exc:
        # Si fallisce CHIUSO (§8.32): un limite che non si può contare non
        # esiste, e per un endpoint che genera posta è la peggiore delle
        # inesistenze.
        log.error("limitatore degli invii di prova non utilizzabile")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None

    # --- 3. invio ---
    try:
        outcome = send_test_message(recipients, actor_username=actor.username)
    except (SmtpNotConfigured, NoRecipients) as exc:
        _audit(actor, result=RESULT_FAILURE, detail={"reason": exc.code})
        # 503 e non 422: non è il client ad aver sbagliato la richiesta, è il
        # servizio a non essere in condizione di eseguirla.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None
    except SmtpSendFailed as exc:
        _audit(actor, result=RESULT_FAILURE, detail={"reason": exc.reason})
        # `reason` è una CATEGORIA da un elenco chiuso. Il testo dell'eccezione,
        # che contiene host e risposta del relay, resta nei log.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": exc.code, "message": exc.message,
                                    "reason": exc.reason},
                            headers=NO_STORE) from None

    # --- 4. audit dell'esito, con l'asimmetria ---
    #
    # Il messaggio È PARTITO. Se ora la scrittura dell'audit fallisce non si può
    # rispondere «non riuscito»: il client lo leggerebbe come «riprova», e ogni
    # tentativo manderebbe un altro messaggio vero a persone vere. Si risponde
    # quindi successo, si dice che la traccia manca (`auditRecorded: false`), e
    # il guasto va nei log a livello di errore.
    #
    # Nell'altro verso l'asimmetria non c'è: se l'invio fallisce, un audit non
    # riuscito non cambia la risposta — non c'è niente di irreversibile da
    # proteggere.
    recorded = _audit(actor, result=RESULT_SUCCESS,
                      detail={"recipients": outcome.recipients,
                              "configuredRecipients": len(recipients)})
    if not recorded:
        log.error("invio di prova RIUSCITO ma non registrato nell'audit: "
                  "attore %s, %d destinatari", actor.username, outcome.recipients)

    return {
        "sent": True,
        # Quanti ne hanno ricevuto e quanti ne sono configurati: la prova ne usa
        # al massimo tre (§8.38), e senza i due numeri sembrerebbe che gli altri
        # destinatari non siano configurati.
        "recipients": outcome.recipients,
        "configuredRecipients": len(recipients),
        "auditRecorded": recorded,
    }
