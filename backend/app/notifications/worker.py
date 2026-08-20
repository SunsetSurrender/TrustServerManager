"""Worker delle notifiche di scadenza: un giro, e il ciclo che lo ripete.

Processo SEPARATO dall'API (§8.41). Non perché sia più elegante, ma perché
altrimenti il numero di scheduler dipenderebbe dal numero di worker di Uvicorn:
`--workers 4` produrrebbe quattro scheduler che mandano quattro copie di ogni
avviso, e il difetto comparirebbe il giorno in cui qualcuno scala l'API per un
motivo che con le notifiche non ha niente a che vedere.

Perché NON si usa APScheduler
-----------------------------
Il committente lo suggeriva, e la sostanza della richiesta — un processo a parte,
fusi orari con `zoneinfo`, recupero delle esecuzioni perdute, nessuna dipendenza
dal comportamento «misfire» in memoria — è rispettata. Aggiungendo APScheduler,
però, la pianificazione vivrebbe in DUE posti: la sua idea in memoria di «prossima
esecuzione» e il registro durevole `scheduler_runs`, che è quello che decide
davvero. Due fonti di verità sullo stesso fatto divergono, e quella che si
consulta leggendo il codice non è quella che comanda.

Qui il ciclo è un `sleep`, e la domanda «tocca eseguire?» ha una sola risposta,
nel database:

    data locale di oggi nel fuso configurato
      + ora e minuto configurati già passati
      + nessuna riga conclusa in `scheduler_runs` per quella data
    → si esegue

Da questa forma seguono, senza codice dedicato:

  - **recupero**: macchina spenta all'ora prevista → alla riaccensione la riga di
    oggi non c'è e il giro parte;
  - **ora legale in primavera**: se le 02:30 locali non esistono, alle 03:05 il
    confronto sull'orologio da parete è comunque soddisfatto e il giro parte
    quel giorno;
  - **ora legale in autunno**: le 02:30 accadono due volte, ma la seconda trova
    la riga di oggi già conclusa e non manda niente.

Nessuna delle tre dipende dalla memoria del processo, che è precisamente ciò che
si azzera nel momento in cui la domanda diventa importante.

Un secondo lavoro nello stesso processo
---------------------------------------
Il ciclo esegue anche la garbage collection delle foto orfane (§8.5). Stesso
processo — il lock del worker garantisce già che ce ne sia uno solo, e un secondo
container servirebbe solo a moltiplicare i modi di sbagliare — ma lavoro
LOGICAMENTE INDIPENDENTE: tabella di esecuzioni propria (`maintenance_runs`),
orario proprio, e nessuna dipendenza da `notifications.enabled`. Spegnere gli
avvisi non deve fermare la liberazione dello spazio, e un errore della GC non deve
impedire un avviso di scadenza.

Da dove arrivano le scadenze (fase 2F)
--------------------------------------
La SORGENTE dei candidati è la **proiezione relazionale**, interrogata sulle colonne
data derivate; prima era il documento canonico letto e scorso in Python (§8.47).

È cambiata **solo** quella. Soglia più urgente, soglie superate, identità del
promemoria, idempotenza, ritentativi, `Message-ID`, cooldown, destinatari,
`scheduler_runs`, audit: tutto dov'era. La fase 2F esiste per poter dire questa frase
e provarla, ed è per questo che i test di consegna non sono stati riscritti — se il
sistema di consegna fosse cambiato, sarebbero rossi.

Due condizioni nuove, entrambe che fanno FALLIRE CHIUSO:

  - la proiezione deve rispecchiare la testa. Se non lo fa, non parte nessun avviso e
    il giro di oggi resta da riprendere. Nessun ripiego su `inventory_versions.doc`:
    il ripiego funzionerebbe e coprirebbe il difetto di coerenza che la fase 2 esiste
    per scoprire (§8.45);
  - l'inventario deve essere ancora quello da cui vengono i candidati. Si legge in uno
    snapshot, si ricontrolla nella transazione che scrive, e se è cambiato si rinvia.

⚠ Il worker NON chiama `GET /api/inventory/expiries`. Quell'endpoint riproduce la
**vista Scadenze**, che sui dismessi e sugli scaduti non è d'accordo con lo scanner
(§8.48); e comunque un processo del backend che parla con sé stesso via HTTP si
porterebbe dietro autenticazione, rete e stati di errore per leggere dal database su
cui è già collegato.

Riferimento: BACKEND-PLAN.md §8.41, §8.47, §8.5.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.audit.sanitize import sanitize
from app.auth.audit import RESULT_FAILURE, RESULT_SUCCESS, record_auth_event
from app.config import get_settings as get_config
from app.db import read_snapshot
from app.inventory.errors import NotBootstrappedError, ProjectionNotCurrentError
from app.notifications import candidates
from app.notifications import reminders as rem
from app.notifications.digest import build_digest
from app.notifications.smtp import SmtpNotConfigured, SmtpSendFailed, deliver
from app.photos import gc as photo_gc
from app.settings import copy_notifications
from app.settings import repository as settings_repo

log = logging.getLogger(__name__)

#: Chiave del lock consultivo che garantisce UN SOLO worker. Diversa da quella
#: del limitatore degli invii di prova (§8.38): sono due mutue esclusioni
#: distinte e condividerle le farebbe interferire.
WORKER_LOCK_KEY = 0x7473_6D77_6B72        # "tsmwkr"

#: Ogni quanto il worker si chiede se tocca eseguire. Non è la frequenza degli
#: avvii: quella la decide `scheduler_runs`. Un intervallo corto rende il
#: recupero rapido dopo un'assenza e costa una query.
TICK_SECONDS = 300

#: Azioni di audit del worker.
DIGEST_SENT = "notifications.digest.sent"
DIGEST_FAILED = "notifications.digest.failed"
#: Tentativi esauriti. NON «avviso perso»: i promemoria tornano eleggibili dopo
#: `RETRY_COOLDOWN`, e chi legge il registro deve poterlo capire dal nome
#: dell'evento invece di dedurlo dal codice.
DIGEST_RETRY_EXHAUSTED = "notifications.digest.retry_exhausted"

#: Attore delle righe di audit. Il worker non è una persona, e inventare
#: un'utenza umana per i suoi eventi renderebbe il registro bugiardo.
WORKER_ACTOR = "(worker notifiche)"


@dataclass
class TickResult:
    ran: bool = False
    reason: str = ""
    due: int = 0
    sent: int = 0
    failure: str = ""
    #: Data locale dell'esecuzione, quando il giro ha davvero valutato qualcosa.
    #: Serve al battito: senza, il campo `last_run_date` che il monitoraggio
    #: guarda resterebbe sempre NULL — la prima versione lo faceva, e il difetto
    #: si vede solo leggendo lo stato, non i test.
    run_date: date | None = None


# ==================================================================
# esclusività: un solo worker, imposta dal database
# ==================================================================

def acquire_singleton(conn: Connection) -> bool:
    """Lock consultivo di SESSIONE, tenuto per tutta la vita del worker.

    `replicas: 1` in Compose è una dichiarazione d'intenti: non impedisce a
    nessuno di lanciare `docker compose run` a mano, né a due host di puntare
    allo stesso database durante una migrazione. Il lock lo impedisce nel posto
    in cui i due worker si incontrano davvero, cioè il database.

    È `pg_try_advisory_lock` e non `pg_advisory_lock`: aspettare
    indefinitamente lascerebbe un secondo processo vivo e silenzioso, che sembra
    funzionare e non fa niente. Meglio uscire dicendo perché.
    """
    return bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                             {"k": WORKER_LOCK_KEY}).scalar_one())


def release_singleton(conn: Connection) -> None:
    """Rilascia il lock ESPLICITAMENTE.

    ⚠ Non basta chiudere la `Connection` di SQLAlchemy: `close()` la restituisce
    al pool, la sessione col database resta aperta e il lock resta preso. In un
    processo che esce non fa differenza — la sessione muore col processo — ma in
    qualunque altro contesto (test, un futuro comando che faccia un giro solo)
    il lock rimarrebbe appeso a una connessione inattiva del pool, e il worker
    successivo si rifiuterebbe di partire dicendo che ce n'è già uno attivo.
    Trovato da un test, dove il lock è sopravvissuto alla fine del test che
    l'aveva preso.
    """
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                     {"k": WORKER_LOCK_KEY})
    except Exception:                                       # pragma: no cover
        log.exception("rilascio del lock del worker non riuscito")


# ==================================================================
# battito, per il monitoraggio e per l'healthcheck del container
# ==================================================================

def heartbeat(engine: Engine, *, state: str, detail: str = "",
              run_date: date | None = None) -> None:
    """Aggiorna la riga unica del battito. Non solleva mai.

    Un guasto nello scrivere il battito non deve fermare il worker: il battito
    serve a chi guarda da fuori, e trasformarlo in una condizione di
    funzionamento significherebbe fermare le notifiche per un problema di
    monitoraggio.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE worker_heartbeat
                   SET last_tick_at = now(), state = :s, detail = :d,
                       last_run_date = COALESCE(:rd, last_run_date)
                 WHERE id IS TRUE
            """), {"s": state[:40], "d": detail[:400] or None, "rd": run_date})
    except Exception:                                       # pragma: no cover
        log.exception("battito non scritto")


# ==================================================================
# un giro
# ==================================================================

def due_now(now_utc: datetime, *, tz_name: str, hour: int,
            minute: int) -> tuple[date, bool]:
    """(data locale, l'ora pianificata di oggi è passata?).

    Il confronto è sull'OROLOGIO DA PARETE locale, non su un istante UTC. È ciò
    che rende il comportamento corretto nei due cambi d'ora senza casi
    particolari: in primavera un'ora locale che non esiste risulta comunque
    «passata» appena l'orologio la supera; in autunno l'ora che si ripete non
    conta, perché a decidere se eseguire è il registro per data locale.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(ZoneInfo(tz_name))
    scheduled_minutes = hour * 60 + minute
    now_minutes = local.hour * 60 + local.minute
    return local.date(), now_minutes >= scheduled_minutes


def run_once(engine: Engine, *, now_utc: datetime,
             force: bool = False) -> TickResult:
    """Un giro completo: ritentativi, poi valutazione, poi invio.

    L'ordine conta. Si ritenta PRIMA di comporre qualcosa di nuovo, così un
    digest che non è ancora partito mantiene il suo `Message-ID` invece di
    diventare un secondo avviso.
    """
    cfg = get_config()

    # --- impostazioni correnti, a ogni giro ---
    # Si rileggono ogni volta: cambiare i destinatari o spegnere le notifiche
    # deve avere effetto al giro successivo, non al riavvio del worker.
    with engine.connect() as conn:
        with conn.begin():
            try:
                row = settings_repo.load(conn)
            except Exception as exc:
                log.error("impostazioni non leggibili: %s", type(exc).__name__)
                return TickResult(reason="settings_unavailable")
            notif = copy_notifications(row.data)

    if not notif["enabled"]:
        # Niente inviato, e nessun promemoria registrato come consegnato. Non si
        # creano nemmeno righe «in attesa»: alla riaccensione delle notifiche si
        # rivaluta da zero e si manda una sola volta la soglia più urgente, che è
        # il comportamento voluto invece di uno scarico di arretrati.
        return TickResult(reason="notifications_disabled")

    tz_name = notif["timezone"]
    today, passed = due_now(now_utc, tz_name=tz_name,
                            hour=notif["schedule"]["hour"],
                            minute=notif["schedule"]["minute"])

    # --- 1. c'è una consegna da ritentare? ---
    with engine.connect() as conn:
        with conn.begin():
            pending = rem.claim_retryable_delivery(conn, now_utc)
            if pending is not None:
                return _attempt_delivery(
                    conn, engine, pending, notif=notif, cfg=cfg,
                    now_utc=now_utc, today=today, run_date=None)

    if not (force or passed):
        return TickResult(reason="not_yet_scheduled")

    # --- 2. prenotazione dell'esecuzione di oggi ---
    with engine.connect() as conn:
        with conn.begin():
            if not rem.claim_run(conn, today, tz_name):
                return TickResult(reason="already_ran_today")

    # --- 3a. i candidati, da uno SNAPSHOT della proiezione ---
    #
    # Fase 2F (§8.47). La sorgente è la PROIEZIONE relazionale, non più
    # `inventory_versions.doc`: si interroga la finestra utile sulle colonne data
    # derivate invece di leggere il documento intero e scartare in Python. Ciò che
    # viene dopo — precedenza fra soglie, identità, idempotenza, consegna — non è
    # cambiato di una riga: è il punto dell'esercizio.
    #
    # Transazione SEPARATA da quella che scrive, e di sola lettura. Serve uno snapshot
    # stabile perché la lettura è multipla (testa, stato, quattro tabelle) e un `PUT`
    # che committa nel mezzo darebbe candidati di due versioni diverse.
    try:
        with read_snapshot() as snap:
            found = candidates.due_items_from_projection(
                snap, today=today, warning_days=notif["warningDays"])
    except NotBootstrappedError:
        with engine.begin() as conn:
            rem.finish_run(conn, today, due=0, sent=0,
                           outcome="inventory_not_bootstrapped")
        return TickResult(ran=True, reason="inventory_not_bootstrapped",
                          run_date=today)
    except ProjectionNotCurrentError as exc:
        # ⚠ «Proiezione non attuale» NON è «niente è dovuto», e la differenza è tutta
        # qui: sono due stati operativi diversi e confonderli significherebbe
        # dichiarare un giro riuscito senza aver guardato l'inventario. Quindi:
        # nessun invio, nessun promemoria creato, nessuna soglia superata, e — questa
        # è la riga che conta — **il giro NON si conclude**. La riga di
        # `scheduler_runs` di oggi resta senza `finished_at`, che è esattamente lo
        # stato che `claim_run` sa riprendere: appena la proiezione è riparata
        # (`project.py --rebuild`), il tick successivo rifà il giro di oggi.
        #
        # Concluderlo con un esito «non attuale» sarebbe stato più ordinato da
        # leggere e avrebbe perso la giornata: `claim_run` avrebbe risposto
        # «already_ran_today» fino a mezzanotte.
        log.error("proiezione non attuale (%s): nessun avviso inviato, "
                  "il giro di %s verrà ripreso al prossimo tick",
                  getattr(exc, "details", None) or exc.code, today)
        return TickResult(reason="projection_not_current")

    # --- 3b. prenotazione dei promemoria e invio ---
    with engine.connect() as conn:
        with conn.begin():
            # ⚠ L'inventario è ancora quello da cui vengono i candidati? Fra lo
            # snapshot e questa transazione c'è una finestra in cui un `PUT` può aver
            # cambiato tutto, e un avviso calcolato su una revisione che non esiste
            # più annuncia una scadenza che qualcuno ha appena corretto. Si abbandona
            # senza mandare e senza concludere il giro: il tick successivo ricalcola.
            if not candidates.unchanged(conn, version=found.version,
                                        sha256=found.sha256):
                log.info("inventario cambiato durante il calcolo dei candidati "
                         "(revisione %s): giro di %s rinviato al prossimo tick",
                         found.version, today)
                return TickResult(reason="inventory_moved")

            selected = rem.register_and_select(
                conn, found.items, warning_days=notif["warningDays"], now=now_utc)

            if not selected:
                rem.finish_run(conn, today, due=0, sent=0, outcome="nothing_due")
                return TickResult(ran=True, reason="nothing_due", run_date=today)

            # I destinatari sono TUTTI quelli configurati: il tetto di tre è una
            # misura anti-abuso dell'endpoint di prova interattivo (§8.38), non
            # una regola di prodotto. Omettere destinatari da un avviso reale
            # significherebbe che qualcuno non riceve la notifica che ha chiesto.
            recipients = list(notif["recipients"])
            if not recipients:
                rem.finish_run(conn, today, due=len(selected), sent=0,
                               outcome="no_recipients_configured")
                return TickResult(ran=True, due=len(selected),
                                  reason="no_recipients_configured",
                                  run_date=today)

            delivery = rem.create_delivery(
                conn, [s["reminder_id"] for s in selected],
                recipients=recipients, now=now_utc)
            result = _attempt_delivery(
                conn, engine, delivery, notif=notif, cfg=cfg, now_utc=now_utc,
                today=today, run_date=today, selected=selected)
            result.due = len(selected)
            result.run_date = today
            return result


def _attempt_delivery(conn: Connection, engine: Engine, delivery, *, notif: dict,
                      cfg, now_utc: datetime, today: date,
                      run_date: date | None,
                      selected: list[dict] | None = None) -> TickResult:
    """Un tentativo di consegna, con il conteggio registrato PRIMA dell'invio."""
    if selected is None:
        # Ritentativo: le voci si ricompongono dai promemoria agganciati alla
        # consegna, non si rivaluta l'inventario. Rivalutarlo produrrebbe un
        # digest diverso sotto lo stesso `Message-ID`.
        rebuilt = _rebuild_selection(conn, delivery.id, notif, today)
        if rebuilt is None:
            # Proiezione non attuale: il ritentativo si RINVIA. Si torna prima di
            # `mark_attempt_started`, quindi il tentativo non è consumato e il
            # `Message-ID` resta quello di sempre — un ritentativo rinviato non deve
            # costare uno dei cinque tentativi per un guasto che non è del relay.
            log.error("proiezione non attuale: ritentativo della consegna %d "
                      "rinviato", delivery.id)
            return TickResult(reason="projection_not_current")
        selected = rebuilt
        if not selected:
            rem.mark_sent(conn, delivery.id, now_utc)
            return TickResult(ran=True, reason="retry_empty")

    attempts = rem.mark_attempt_started(conn, delivery.id, now_utc)
    recipients = list(notif["recipients"])
    msg = build_digest(selected, sender=cfg.smtp_sender, recipients=recipients,
                       message_id=delivery.message_id, now=now_utc, today=today)

    try:
        deliver(msg, what="digest delle scadenze")
    except (SmtpSendFailed, SmtpNotConfigured) as exc:
        category = getattr(exc, "reason", None) or exc.code
        exhausted = rem.mark_failed(conn, delivery.id, category=category,
                                    attempts=attempts, now=now_utc)
        _audit(conn, DIGEST_RETRY_EXHAUSTED if exhausted else DIGEST_FAILED,
               result=RESULT_FAILURE,
               detail={"reminders": len(selected), "attempts": attempts,
                       "category": category, "messageId": delivery.message_id})
        if run_date is not None:
            rem.finish_run(conn, run_date, due=len(selected), sent=0,
                           outcome=f"failed:{category}")
        log.error("digest non consegnato (%s, tentativo %d)", category, attempts)
        return TickResult(ran=True, sent=0, failure=category,
                          reason="delivery_failed")

    # ⚠ La finestra che non si può chiudere: il relay ha accettato il messaggio e
    # questa riga non è ancora stata scritta. Se il processo muore qui, al
    # riavvio il promemoria risulta non inviato e il digest verrà rimandato — con
    # lo stesso `Message-ID`, che è tutto ciò che si può fare (§8.41). Si preferisce
    # un duplicato riconoscibile a una scadenza mai comunicata.
    rem.mark_sent(conn, delivery.id, now_utc)
    _audit(conn, DIGEST_SENT, result=RESULT_SUCCESS,
           detail={"reminders": len(selected), "recipients": len(recipients),
                   "attempts": attempts, "messageId": delivery.message_id})
    if run_date is not None:
        rem.finish_run(conn, run_date, due=len(selected), sent=len(selected),
                       outcome="sent")
    log.info("digest consegnato: %d promemoria a %d destinatari",
             len(selected), len(recipients))
    return TickResult(ran=True, sent=len(selected), reason="sent")


def _rebuild_selection(conn: Connection, delivery_id: int, notif: dict,
                       today: date) -> list[dict] | None:
    """Ricostruisce le voci di un digest da ritentare dai promemoria agganciati.

    L'inventario si rilegge solo per recuperare i nomi: identità, tipo, data e
    soglia vengono dai promemoria, che sono la fonte autorevole. Se un
    dispositivo è stato cancellato dall'inventario nel frattempo, la sua voce
    esce dal digest ma il promemoria resta agganciato e viene chiuso col resto:
    non si manda un avviso su qualcosa che non esiste più.

    Dalla fase 2F i nomi vengono dalla PROIEZIONE (§8.47), non dal documento, e la
    chiave del confronto è rimasta la stessa tripla `(uid, tipo, data)`: un
    promemoria si ricompone solo se quel dispositivo ha ANCORA quella data per quel
    tipo. Se qualcuno ha corretto la garanzia, la voce esce — come prima.

    Tre risultati distinti, e la distinzione è necessaria:

      - una lista PIENA: si può comporre il digest;
      - una lista VUOTA: i promemoria non hanno più un riscontro nell'inventario, la
        consegna si chiude;
      - `None`: non si sa, perché la proiezione non rispecchia la testa. Non è «vuoto»
        — chiudere la consegna qui vorrebbe dire marcare come inviati promemoria che
        nessuno ha ricevuto (§13 della fase 2F).
    """
    from app.notifications.expiry import DueItem

    rows = rem.reminders_of_delivery(conn, delivery_id)
    if not rows:
        return []

    # Snapshot proprio, preso mentre la transazione di scrittura è aperta: sono due
    # connessioni insieme, e nel worker non c'è il rischio di stallo che nell'API ha
    # imposto due pool (un processo, un giro alla volta). Il ragionamento è in testa a
    # `app/db.py`.
    try:
        with read_snapshot() as snap:
            context = candidates.context_by_key(
                snap, [str(r["entity_uid"]) for r in rows])
    except (NotBootstrappedError, ProjectionNotCurrentError):
        return None

    out: list[dict] = []
    for r in rows:
        key = (str(r["entity_uid"]), r["expiry_kind"], r["expiry_date"])
        found = context.get(key)
        if found is None:
            continue
        name, rack, room, loc = found
        out.append({
            "reminder_id": r["id"],
            "threshold_days": r["threshold_days"],
            "item": DueItem(entity_uid=key[0], kind=key[1], expiry=key[2],
                            days_remaining=(key[2] - today).days,
                            device=name, rack=rack, room=room, location=loc),
        })
    return out


def _audit(conn: Connection, action: str, *, result: str, detail: dict) -> None:
    """Evento di audit derivato dal server: conteggi e categorie, mai il corpo
    del messaggio né credenziali (§8.41)."""
    try:
        record_auth_event(conn, action, username=WORKER_ACTOR, role=None,
                          ip=None, result=result, detail=sanitize(detail))
    except Exception:                                       # pragma: no cover
        log.exception("audit del worker non scritto")


# ==================================================================
# il ciclo
# ==================================================================

_stop = False


def _handle_signal(signum, _frame) -> None:      # pragma: no cover
    global _stop
    _stop = True
    log.info("segnale %s: uscita alla fine del giro", signum)


def main() -> int:                                # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    from app.db import get_engine
    engine = get_engine()

    # La connessione del lock resta aperta per tutta la vita del processo: un
    # lock consultivo di sessione vive quanto la sessione, e chiuderla lo
    # rilascerebbe.
    lock_conn = engine.connect()
    if not acquire_singleton(lock_conn):
        log.error("un altro worker delle notifiche è già attivo su questo "
                  "database: esco. Un solo worker deve esistere (§8.41).")
        heartbeat(engine, state="refused",
                  detail="lock del worker già tenuto da un altro processo")
        lock_conn.close()
        return 3

    log.info("worker delle notifiche avviato (pid %d, giro ogni %ds)",
             os.getpid(), TICK_SECONDS)
    heartbeat(engine, state="running", detail=f"pid {os.getpid()}")

    while not _stop:
        try:
            result = run_once(engine, now_utc=datetime.now(timezone.utc))
            heartbeat(engine, state="running", run_date=result.run_date,
                      detail=f"{result.reason} (due={result.due}, sent={result.sent})")
        except Exception as exc:
            # Un giro che solleva non deve fermare il worker: la causa più
            # probabile è transitoria (database irraggiungibile), e uscire
            # significherebbe non mandare più niente fino a un intervento.
            log.exception("giro del worker fallito")
            heartbeat(engine, state="error", detail=type(exc).__name__)

        # ---- manutenzione: GC delle foto orfane (§8.5) ----
        #
        # Stesso processo, lavoro INDIPENDENTE. Il `try` è separato di proposito: un
        # guasto della GC non deve impedire gli avvisi di scadenza, e un guasto
        # degli avvisi non deve fermare la liberazione dello spazio. Per lo stesso
        # motivo la GC ha una tabella di esecuzioni propria e non guarda
        # `notifications.enabled`: spegnere le email non deve riempire il disco.
        try:
            gc_result = photo_gc.run_once(engine,
                                          now_utc=datetime.now(timezone.utc))
            if gc_result.deleted:
                log.info("GC foto: %d cancellate (%s)", gc_result.deleted,
                         gc_result.run_date)
        except Exception:
            log.exception("giro di GC delle foto fallito")

        for _ in range(TICK_SECONDS):
            if _stop:
                break
            time.sleep(1)

    heartbeat(engine, state="stopped")
    release_singleton(lock_conn)
    lock_conn.close()
    log.info("worker delle notifiche terminato")
    return 0
