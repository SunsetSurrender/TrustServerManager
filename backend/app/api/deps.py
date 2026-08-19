"""Dipendenze delle rotte: connessione e ATTORE AUTENTICATO OBBLIGATORIO.

Non esiste un ripiego di sviluppo che conceda `admin` senza autenticazione. Quel
ripiego è pericoloso proprio perché funziona: sopravvive ai refactoring, non fa
fallire nessun test, e il giorno in cui una variabile d'ambiente è impostata male
diventa un accesso amministrativo anonimo. Il prototipo aveva già un difetto della
stessa forma — `_doLogin` concedeva `admin` quando l'elenco utenze era vuoto — e va
rimosso, non riprodotto sul server.

`require_actor` in esecuzione richiede una sessione valida e risponde 401
altrimenti. Nei test si sostituisce con `app.dependency_overrides`, che è
esplicito, locale al test e impossibile da attivare per errore in produzione.

Riferimento: BACKEND-PLAN.md §8.20, §8.26.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.engine import Connection

from app.api.request_context import client_ip
from app.auth.service import (
    AuthenticatedUser,
    NotAuthenticated,
    resolve_session,
)
from app.db import get_engine, get_read_engine
from app.inventory import Actor

SESSION_COOKIE = "tsm_session"

NO_STORE = {"Cache-Control": "no-store"}

#: Codice sul filo. Tutti i codici di errore sono snake_case minuscolo:
#: un vocabolario con due convenzioni si sbaglia a scrivere (§8.21).
PASSWORD_CHANGE_REQUIRED = "password_change_required"


def get_connection() -> Iterator[Connection]:
    """Una connessione con una transazione per richiesta.

    La transazione è della richiesta: se l'handler solleva, non resta scritto
    niente. È ciò che rende atomico il salvataggio dell'inventario senza che il
    repository debba gestire il commit.
    """
    engine = get_engine()
    with engine.connect() as conn:
        with conn.begin():
            yield conn


# ==================================================================
# lo SNAPSHOT di lettura dell'inventario (fase 2D, §8.45)
# ==================================================================

@contextmanager
def snapshot_connection() -> Iterator[Connection]:
    """Connessione dedicata alla lettura dell'inventario: REPEATABLE READ, READ ONLY.

    Dalla fase 2D `GET /api/inventory` non legge un documento: ne riassembla uno da
    sette letture (testa, stato, siti, sale, rack, dispositivi, voci di manuale, più
    gli identificativi delle foto). Sotto READ COMMITTED ogni `SELECT` vede un
    istante diverso del database, quindi un `PUT` che committa nel mezzo produrrebbe
    un documento fatto di due versioni — o, più spesso, un 503 «proiezione
    incoerente» a fronte di attività perfettamente normale. Non è un caso di
    laboratorio: due persone che lavorano sullo stesso CED lo producono da sole.

    ⚠ Perché una connessione SUA e non quella della richiesta.

    Non è una preferenza: la connessione della richiesta è inutilizzabile per questo,
    per tre ragioni indipendenti.

      - `get_connection` ha già aperto la transazione, e l'autenticazione ci ha già
        eseguito degli statement dentro. L'isolamento si dichiara PRIMA del primo
        statement: dopo, PostgreSQL rifiuta `SET TRANSACTION`, e SQLAlchemy rifiuta
        di cambiare `isolation_level` su una connessione con una transazione in corso;
      - `READ ONLY` la escluderebbe comunque: `resolve_session` **scrive**
        (`UPDATE sessions SET last_seen_at = now()`), a ogni richiesta e per progetto,
        perché il ruolo e la disattivazione si rileggono ogni volta (§8.26);
      - promuovere TUTTE le richieste a REPEATABLE READ sarebbe la soluzione
        apparentemente elegante, e romperebbe il salvataggio: il `PUT` si serializza
        con `SELECT ... FOR UPDATE` sulla riga di testa e traduce il perdente in un
        409 pulito. In REPEATABLE READ il perdente prenderebbe invece un
        «could not serialize access due to concurrent update», cioè un errore del
        database al posto del conflitto di versione che il client sa gestire (§8.11).

    Quindi due connessioni per un `GET`, che è il costo dichiarato di questa scelta.
    In compenso `READ ONLY` non è decorativo: è PostgreSQL a rifiutare qualunque
    scrittura su questa transazione, quindi un difetto futuro che provasse a scrivere
    mentre serve una lettura verrebbe fermato dal database, non dalle buone intenzioni.

    ⚠ La transazione comincia col primo statement, non con `BEGIN`: in REPEATABLE
    READ lo snapshot si acquisisce alla prima lettura. Va bene, e non è una
    scappatoia — l'unica cosa che conta è che tutte le letture stiano DENTRO la stessa
    transazione, e `current_document` non ne fa nessuna prima.

    ⚠ `get_read_engine()`, non `get_engine()`, e la differenza impedisce uno STALLO.
    Un `GET` tiene due connessioni insieme: prendere la seconda dallo stesso pool della
    prima è un'acquisizione a due fasi, e con quindici `GET` simultanei si blocca per
    trenta secondi e poi scade. Il ragionamento completo, con i numeri, è in testa a
    `app/db.py`, e c'è un test che lo DIMOSTRA facendo fallire un `GET` con un pool
    solo.
    """
    engine = get_read_engine()
    conn = engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True)
    try:
        with conn.begin():
            yield conn
    finally:
        conn.close()


def get_snapshot_reader() -> Callable[[], ContextManager[Connection]]:
    """Dipendenza che fornisce la FABBRICA dello snapshot, non lo snapshot.

    Restituire la fabbrica invece della connessione non è un giro inutile: fa
    scegliere alla rotta *quando* aprire la transazione, e la rotta la apre nel
    proprio corpo — cioè dopo che le dipendenze di autenticazione sono state
    risolte. Con un `Depends` che restituisce la connessione, l'ordine dipenderebbe
    dall'ordine dei parametri nella firma, e una richiesta anonima aprirebbe una
    transazione sul database prima di scoprire di essere un 401. Un 401 non deve
    costare una connessione: è la richiesta che arriva a raffica quando qualcuno
    sonda il servizio.

    Resta una dipendenza, e non una funzione chiamata direttamente, perché così i
    test la sostituiscono con `app.dependency_overrides` — esplicito, locale al test
    e impossibile da attivare per sbaglio in produzione, come per `get_connection`.
    """
    return snapshot_connection


def current_user(request: Request,
                 conn: Connection = Depends(get_connection)) -> AuthenticatedUser:
    """Utente della sessione, con lo stato RILETTO dal database (§8.26).

    `resolve_session` non si fida di nulla che sia stato memorizzato al login:
    ruolo, disattivazione e obbligo di cambio password vengono riletti a ogni
    richiesta. La sessione dice *chi* è; non dice cosa può fare.
    """
    token = request.cookies.get(SESSION_COOKIE)
    try:
        return resolve_session(conn, token)
    except NotAuthenticated as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": "autenticazione richiesta"},
            headers=NO_STORE,
        ) from None


def require_actor(request: Request,
                  user: AuthenticatedUser = Depends(current_user)) -> Actor:
    """L'attore per le operazioni normali. 401 se non autenticato.

    Con una password provvisoria la sessione è **valida ma ristretta** (§8.26):
    esiste, `/auth/me` la vede, e serve solo a cambiare la password. Qualunque
    altro endpoint risponde 403 `PASSWORD_CHANGE_REQUIRED`.

    La restrizione è strutturale, non un elenco di percorsi: gli unici tre
    endpoint raggiungibili sono quelli che NON dipendono da `require_actor`
    (`/auth/me`, `/auth/password`, `/auth/logout`). Un endpoint nuovo è ristretto
    per costruzione, perché per fare qualcosa gli serve un attore.
    """
    if user.must_change_pw:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": PASSWORD_CHANGE_REQUIRED,
                    "message": "cambiare la password provvisoria prima di procedere"},
            headers=NO_STORE,
        )
    return user.to_actor(ip=client_ip(request))


def require_admin(actor: Actor = Depends(require_actor)) -> Actor:
    """Attore con ruolo admin. Il ruolo è quello riletto adesso, non quello del
    login: una revoca di privilegi ha effetto dalla richiesta successiva."""
    if actor.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden_for_role",
                    "message": "operazione riservata agli amministratori",
                    "requiredRole": "admin"},
            headers=NO_STORE,
        )
    return actor
