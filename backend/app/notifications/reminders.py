"""Ciclo di vita durevole dei promemoria: registrazione, precedenza, consegne.

Lo stato «già inviato» vive nel DATABASE, non nella memoria del processo. Un
worker riavviato deve sapere cosa ha già mandato, e la memoria di APScheduler (o
di qualunque scheduler) non lo sa per definizione: si azzera proprio nel momento
in cui la domanda diventa importante.

Riferimento: BACKEND-PLAN.md §8.41.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.notifications.expiry import DueItem, applicable_thresholds

#: Tentativi massimi di una consegna, e attesa fra i tentativi. Limitati di
#: proposito: un relay guasto non deve produrre un ciclo stretto di tentativi né
#: di messaggi.
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (60, 300, 900, 3600, 10800)

#: Attesa prima di ricomporre un digest per promemoria la cui consegna ha esaurito
#: i tentativi. Senza, con SMTP rotto si creerebbe una consegna nuova a ogni giro,
#: per sempre.
RETRY_COOLDOWN = timedelta(hours=6)

STATE_PENDING = "pending"
STATE_SENT = "sent"
STATE_SUPERSEDED = "superseded"

#: Stato di una CONSEGNA che ha esaurito i tentativi.
#:
#: Si chiamava `abandoned`, e il nome diceva una cosa falsa: dopo l'attesa i
#: promemoria di quella consegna tornano eleggibili e vengono ricomposti in un
#: digest nuovo. Niente è stato abbandonato — è stata esaurita una serie di
#: tentativi, e il promemoria continua a esistere. Un nome che promette la fine di
#: qualcosa che riprende sei ore dopo porta chi legge il registro (o il codice) a
#: concludere che un avviso è stato perso, quando è soltanto in attesa.
STATE_RETRY_EXHAUSTED = "retry_exhausted"


@dataclass(frozen=True)
class Delivery:
    id: int
    message_id: str
    attempts: int
    recipients_count: int | None
    reminder_count: int


def recipients_hash(recipients: Sequence[str]) -> str:
    """Impronta dell'insieme dei destinatari.

    Si registra l'impronta e non l'elenco: a chi legge il registro serve poter
    dire «la configurazione era quella» o «era cambiata», non avere una seconda
    copia di indirizzi di persone in un'altra tabella. L'ordine non conta, quindi
    si ordina prima.
    """
    joined = "\n".join(sorted(r.strip().lower() for r in recipients))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def new_message_id(domain: str = "tsm.local") -> str:
    """`Message-ID` generato dal server, stabile per tutta la vita della consegna.

    Si riusa a OGNI ritentativo. Se il relay ha accettato il messaggio e il
    processo è morto prima di registrarlo, il ritentativo manda un messaggio con
    lo stesso identificativo: il duplicato resta possibile — con SMTP non è
    evitabile — ma diventa riconoscibile da un client di posta invece di
    sembrare un secondo avviso.
    """
    return f"<{secrets.token_hex(16)}@{domain}>"


# ==================================================================
# registrazione e precedenza fra soglie
# ==================================================================

def register_and_select(conn: Connection, items: list[DueItem], *,
                        warning_days: list[int], now: datetime) -> list[dict]:
    """Registra i promemoria applicabili e restituisce quelli da mandare ADESSO.

    Per ogni gruppo `(dispositivo, tipo, data di scadenza)` si manda **la soglia
    più urgente fra quelle applicabili e non ancora inviate**, e le soglie più
    larghe si marcano `superseded`.

    È la regola che evita che un riavvio dopo un'assenza lunga produca tre email
    sullo stesso dispositivo. Con `warningDays = [90, 30, 7]` e una macchina
    spenta dal giorno 35 al giorno 5: la soglia 90 era già stata mandata, restano
    applicabili 30 e 7, si manda **7** e si segna 30 come superata. Una email,
    quella giusta — la più urgente è anche la più informativa, perché contiene il
    numero di giorni che restano davvero.
    """
    selected: list[dict] = []

    for item in items:
        thresholds = applicable_thresholds(item.days_remaining, warning_days)
        if not thresholds:
            continue

        # Le righe si creano per TUTTE le soglie applicabili: quelle non scelte
        # servono come traccia («questa soglia non è stata mandata perché
        # superata»), e la loro esistenza impedisce a un'esecuzione successiva di
        # riconsiderarle.
        for n in thresholds:
            conn.execute(text("""
                INSERT INTO reminders (entity_uid, expiry_kind, expiry_date,
                                       threshold_days, state)
                VALUES (:uid, :kind, :d, :n, 'pending')
                ON CONFLICT ON CONSTRAINT uq_reminder_identity DO NOTHING
            """), {"uid": item.entity_uid, "kind": item.kind,
                   "d": item.expiry, "n": n})

        # Stato attuale delle soglie applicabili, bloccato per evitare che due
        # worker scelgano la stessa riga. Il lock consultivo del worker rende
        # questo caso già impossibile, ma i due meccanismi coprono guasti
        # diversi: uno il worker duplicato, l'altro la riga duplicata.
        rows = conn.execute(text("""
            SELECT id, threshold_days, state, hold_until
              FROM reminders
             WHERE entity_uid = :uid AND expiry_kind = :kind AND expiry_date = :d
               AND threshold_days = ANY(:ns)
             ORDER BY threshold_days
             FOR UPDATE
        """), {"uid": item.entity_uid, "kind": item.kind, "d": item.expiry,
               "ns": thresholds}).mappings().all()

        pending = [r for r in rows
                   if r["state"] == STATE_PENDING
                   and (r["hold_until"] is None or r["hold_until"] <= now)]
        if not pending:
            continue

        chosen = pending[0]                 # la più urgente = la soglia minore
        selected.append({
            "reminder_id": chosen["id"],
            "threshold_days": chosen["threshold_days"],
            "item": item,
        })

        # Tutte le soglie più larghe ancora in attesa diventano superate: il
        # digest urgente le racconta già.
        superseded = [r["id"] for r in pending[1:]]
        if superseded:
            conn.execute(text("""
                UPDATE reminders SET state = 'superseded'
                 WHERE id = ANY(:ids)
            """), {"ids": superseded})

    return selected


# ==================================================================
# consegne
# ==================================================================

def claim_retryable_delivery(conn: Connection, now: datetime) -> Delivery | None:
    """Una consegna già esistente da ritentare, se c'è.

    Si ritenta PRIMA di comporre qualcosa di nuovo, e si riusa la stessa riga —
    quindi lo stesso `Message-ID`. Creare una consegna nuova a ogni tentativo
    significherebbe un identificativo diverso ogni volta, cioè trasformare un
    ritentativo in un secondo avviso agli occhi di chi lo riceve.

    `SKIP LOCKED` perché se un altro processo la sta già ritentando, questo deve
    passare oltre e non aspettare.
    """
    row = conn.execute(text("""
        SELECT id, message_id, attempts, recipients_count, reminder_count
          FROM reminder_deliveries
         WHERE state = 'pending'
           AND attempts > 0
           AND (next_attempt_after IS NULL OR next_attempt_after <= :now)
         ORDER BY id
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    """), {"now": now}).mappings().first()
    if row is None:
        return None
    return Delivery(id=row["id"], message_id=row["message_id"],
                    attempts=row["attempts"],
                    recipients_count=row["recipients_count"],
                    reminder_count=row["reminder_count"])


def create_delivery(conn: Connection, reminder_ids: list[int], *,
                    recipients: Sequence[str], now: datetime) -> Delivery:
    """Nuova consegna, con i promemoria agganciati."""
    row = conn.execute(text("""
        INSERT INTO reminder_deliveries (message_id, state, recipients_hash,
                                         recipients_count, reminder_count)
        VALUES (:mid, 'pending', :rh, :rc, :n)
     RETURNING id, message_id, attempts, recipients_count, reminder_count
    """), {"mid": new_message_id(), "rh": recipients_hash(recipients),
           "rc": len(recipients), "n": len(reminder_ids)}).mappings().one()

    conn.execute(text("UPDATE reminders SET delivery_id = :did WHERE id = ANY(:ids)"),
                 {"did": row["id"], "ids": reminder_ids})
    return Delivery(id=row["id"], message_id=row["message_id"],
                    attempts=row["attempts"],
                    recipients_count=row["recipients_count"],
                    reminder_count=row["reminder_count"])


def reminders_of_delivery(conn: Connection, delivery_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT id, entity_uid, expiry_kind, expiry_date, threshold_days
          FROM reminders WHERE delivery_id = :d ORDER BY id
    """), {"d": delivery_id}).mappings().all()]


def mark_attempt_started(conn: Connection, delivery_id: int, now: datetime) -> int:
    """Incrementa il contatore PRIMA di provare a spedire.

    L'ordine è deliberato: se il processo muore durante l'invio, al riavvio il
    tentativo risulta comunque consumato. Contarlo dopo significherebbe che un
    invio che fa morire il processo non viene mai contato, e i tentativi
    diventano illimitati proprio nel caso peggiore.
    """
    return conn.execute(text("""
        UPDATE reminder_deliveries
           SET attempts = attempts + 1, last_attempt_at = :now
         WHERE id = :id
     RETURNING attempts
    """), {"id": delivery_id, "now": now}).scalar_one()


def mark_sent(conn: Connection, delivery_id: int, now: datetime) -> None:
    conn.execute(text("""
        UPDATE reminder_deliveries
           SET state = 'sent', sent_at = :now, failure_category = NULL,
               next_attempt_after = NULL
         WHERE id = :id
    """), {"id": delivery_id, "now": now})
    conn.execute(text("""
        UPDATE reminders SET state = 'sent', sent_at = :now
         WHERE delivery_id = :id AND state = 'pending'
    """), {"id": delivery_id, "now": now})


def mark_failed(conn: Connection, delivery_id: int, *, category: str,
                attempts: int, now: datetime) -> bool:
    """Registra il fallimento. True se la consegna ha ESAURITO i tentativi.

    Un fallimento di posta NON marca i promemoria come inviati: restano
    `pending`, che è l'unica risposta onesta — nessuno li ha ricevuti.
    """
    if attempts >= MAX_ATTEMPTS:
        conn.execute(text("""
            UPDATE reminder_deliveries
               SET state = 'retry_exhausted', failure_category = :cat,
                   next_attempt_after = NULL
             WHERE id = :id
        """), {"id": delivery_id, "cat": category})
        # I promemoria tornano liberi, ma con un'attesa: la prossima esecuzione
        # non deve ricomporre subito lo stesso digest verso un relay che è
        # ancora rotto.
        #
        # È questa riga a rendere `retry_exhausted` NON terminale: la consegna
        # finisce qui, il promemoria no. Passata l'attesa, `register_and_select`
        # lo ritrova `pending` e lo mette in un digest nuovo, con un `Message-ID`
        # nuovo — perché è un avviso nuovo, non il ritentativo di quello vecchio.
        conn.execute(text("""
            UPDATE reminders
               SET delivery_id = NULL, hold_until = :hold
             WHERE delivery_id = :id AND state = 'pending'
        """), {"id": delivery_id, "hold": now + RETRY_COOLDOWN})
        return True

    delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1]
    conn.execute(text("""
        UPDATE reminder_deliveries
           SET failure_category = :cat, next_attempt_after = :nxt
         WHERE id = :id
    """), {"id": delivery_id, "cat": category,
           "nxt": now + timedelta(seconds=delay)})
    return False


# ==================================================================
# esecuzioni pianificate, per data locale
# ==================================================================

def claim_run(conn: Connection, run_date, tz_name: str) -> bool:
    """Prenota l'esecuzione di questa data locale. False se già fatta.

    La chiave primaria su `run_date` fa tutto il lavoro:

    - macchina spenta all'ora prevista → alla riaccensione la riga non c'è e
      l'esecuzione parte (recupero);
    - ora ripetuta del cambio ora d'autunno → alle 02:30 che accadono due volte,
      la seconda trova la riga e non manda niente.

    Nessuna delle due dipende dalla memoria di un processo, ed è per questo che
    il recupero non è affidato al comportamento «misfire» di uno scheduler.

    Il conflitto NON è un semplice `DO NOTHING`: si riprende una riga rimasta
    **non conclusa**. Se il giro si interrompe a metà — un errore transitorio del
    database, il processo ucciso — la riga di oggi esisterebbe senza
    `finished_at`, e un `DO NOTHING` direbbe «oggi è già stato fatto» quando non
    è stato mandato niente: si perderebbe l'intera giornata. Ritentare è sicuro
    perché l'identità del promemoria è nel database: ciò che era già stato
    inviato resta inviato.
    """
    inserted = conn.execute(text("""
        INSERT INTO scheduler_runs (run_date, timezone)
        VALUES (:d, :tz)
        ON CONFLICT (run_date) DO UPDATE
           SET started_at = now(), timezone = :tz
         WHERE scheduler_runs.finished_at IS NULL
     RETURNING run_date
    """), {"d": run_date, "tz": tz_name}).first()
    return inserted is not None


def finish_run(conn: Connection, run_date, *, due: int, sent: int,
               outcome: str) -> None:
    conn.execute(text("""
        UPDATE scheduler_runs
           SET finished_at = now(), due_count = :due, sent_count = :sent,
               outcome = :o
         WHERE run_date = :d
    """), {"d": run_date, "due": due, "sent": sent, "o": outcome[:200]})
