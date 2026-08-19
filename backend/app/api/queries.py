"""Rotte di interrogazione dell'inventario: ricerca, capacità, scadenze (§8.46).

    GET /api/inventory/search     ?q=&limit=&cursor=
    GET /api/inventory/capacity
    GET /api/inventory/expiries   ?warningDays=&limit=&cursor=

SOLA LETTURA, e tutte e tre sull'istantanea relazionale corrente. Nessuna scrive,
nessuna accetta un corpo, nessuna esegue una query fornita dal client — non esiste un
endpoint «esegui questo SQL», e non è una dimenticanza: tre domande con un significato
si possono autorizzare, verificare e misurare; una domanda arbitraria no.

⚠ Perché un file a parte e non in `inventory.py`. Quel modulo è il DOCUMENTO: due
rotte, un contratto congelato dal prototipo. Queste sono tre domande sul contenuto, con
paginazione e parametri propri, e la loro semantica è la vista del frontend che
riproducono. Tenerle separate rende visibile che il documento non è cambiato.

Autorizzazione (§2)
------------------
Sono letture, quindi `view`, `edit` e `admin` allo stesso modo: passano da
`require_actor`, che nega l'accesso anonimo (401) e le sessioni con password
provvisoria (403). Nessun ruolo minimo: nel frontend la barra di ricerca, la vista
Capacità e la vista Scadenze le vede chiunque abbia una sessione, e renderle
amministrative qui sarebbe restringere una funzione esistente.

`Cache-Control: no-store` come `GET /api/inventory`: è inventario di un cliente, e non
deve restare in nessuna cache intermedia.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Callable, ContextManager

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from app.api.deps import get_snapshot_reader, require_actor
from app.api.errors import NO_STORE, http_error_for
from app.inventory import Actor
from app.inventory import queries as q

log = logging.getLogger(__name__)
router = APIRouter()


# ==================================================================
# forme delle risposte
# ==================================================================
#
# `version` e `sha256` in ognuna, e non per simmetria (§4): dicono al client SE il
# risultato appartiene all'inventario che ha sullo schermo. Senza, una ricerca fatta
# mentre un collega salva restituisce righe corrette per una revisione che il client
# non ha, e il client non ha modo di accorgersene.
#
# I due valori sono letti nello STESSO snapshot dei risultati, quindi non descrivono
# «più o meno lo stesso momento»: descrivono lo stesso momento.

class RevisionOut(BaseModel):
    version: int
    sha256: str


class SearchOut(RevisionOut):
    query: str
    #: Presente solo quando la query è stata riconosciuta come forma IP. Serve al
    #: client per spiegare all'utente che ha cercato una rete e non un testo — è la
    #: distinzione che il frontend rende visibile illuminando i rack sulla pianta.
    ipRange: list[int] | None = None
    results: list[dict]
    nextCursor: str | None = None


class CapacityOut(RevisionOut):
    locations: list[dict]


class ExpiriesOut(RevisionOut):
    today: str
    warningDays: int
    totals: dict
    items: list[dict]
    nextCursor: str | None = None


# ==================================================================
# rotte
# ==================================================================

@router.get("/inventory/search", response_model=SearchOut,
            summary="Ricerca globale: dispositivi e rack")
def search_inventory(
    response: Response,
    q_: str = Query(..., alias="q",
                    description="testo, oppure una forma IP: 10.0.0.1, "
                                "10.0.0.0/24, 10.0.0.1-10.0.0.99, 10.0.*",
                    max_length=200),
    limit: int | None = Query(None, ge=1, le=q.SEARCH_MAX_LIMIT),
    cursor: str | None = Query(None, max_length=2048),
    actor: Actor = Depends(require_actor),
    reader: Callable[[], ContextManager[Connection]] = Depends(get_snapshot_reader),
) -> SearchOut:
    """Ricerca con la semantica della barra del frontend (§8.46).

    `q` è OBBLIGATORIO: un endpoint di ricerca senza query non ha una risposta
    sensata, e restituire l'inventario intero sarebbe la risposta comoda e sbagliata.
    Una `q` vuota o di soli spazi è invece legittima e dà zero risultati — è
    esattamente lo stato della casella vuota nel frontend, che non cerca.
    """
    response.headers.update(NO_STORE)
    try:
        with reader() as conn:
            page = q.search(conn, q=q_, limit=limit, cursor=cursor)
    except Exception as exc:
        raise http_error_for(exc) from None

    return SearchOut(
        version=page.revision.version, sha256=page.revision.sha256,
        query=page.query,
        ipRange=(None if page.ip_range is None else list(page.ip_range)),
        results=page.results, nextCursor=page.next_cursor,
    )


@router.get("/inventory/capacity", response_model=CapacityOut,
            summary="Capacità: unità rack occupate, libere, blocco contiguo")
def capacity_inventory(
    response: Response,
    actor: Actor = Depends(require_actor),
    reader: Callable[[], ContextManager[Connection]] = Depends(get_snapshot_reader),
) -> CapacityOut:
    """Capacità con la semantica della vista Capacità (§8.46).

    Non è paginata, e la ragione è la scala: la risposta ha una riga per rack, e i rack
    sono una manciata di centinaia (102 nel seed reale). Paginare una gerarchia
    sito → sala → rack costringerebbe il client a ricomporla, che è lavoro in più per
    risolvere un problema che non c'è. Se un giorno i rack fossero decine di migliaia,
    il posto dove aggiungere un filtro per sala è questa firma.
    """
    response.headers.update(NO_STORE)
    try:
        with reader() as conn:
            report = q.capacity(conn)
    except Exception as exc:
        raise http_error_for(exc) from None

    return CapacityOut(version=report.revision.version,
                       sha256=report.revision.sha256,
                       locations=report.locations)


@router.get("/inventory/expiries", response_model=ExpiriesOut,
            summary="Scadenze: garanzia e supporto, con il livello")
def expiries_inventory(
    response: Response,
    warningDays: int = Query(q.DEFAULT_WARNING_DAYS, ge=0,
                             le=q.MAX_WARNING_DAYS,
                             description="soglia del livello «entro N giorni»; "
                                         "90 è la costante del frontend"),
    limit: int | None = Query(None, ge=1, le=q.EXPIRY_MAX_LIMIT),
    cursor: str | None = Query(None, max_length=2048),
    actor: Actor = Depends(require_actor),
    reader: Callable[[], ContextManager[Connection]] = Depends(get_snapshot_reader),
) -> ExpiriesOut:
    """Scadenze con la semantica della vista Scadenze (§8.46).

    ⚠ `today` è la data di calendario nel fuso CONFIGURATO (§8.38), non l'istante
    della richiesta e non la data del client. Il fuso è quello che usa già lo scanner
    delle notifiche, così «scaduto» significa la stessa cosa in un promemoria via posta
    e in questa risposta. La data usata esce nella risposta: un conteggio di giorni
    senza la data da cui è calcolato non è verificabile da chi lo legge.

    ⚠ Questo endpoint NON è la sorgente del worker delle notifiche. Il worker continua
    a leggere il documento (§19): il passaggio è un commit isolato, con queste stesse
    fixture di parità.
    """
    response.headers.update(NO_STORE)
    try:
        with reader() as conn:
            # Il fuso si legge DENTRO lo snapshot, come tutto il resto: sta nelle
            # impostazioni (§8.38), non nella configurazione del processo, e leggerlo
            # fuori significherebbe che «oggi» viene da un istante diverso da quello
            # che ha prodotto le righe.
            today = _local_today(conn)
            page = q.expiries(conn, today=today, warning_days=warningDays,
                              limit=limit, cursor=cursor)
    except Exception as exc:
        raise http_error_for(exc) from None

    return ExpiriesOut(
        version=page.revision.version, sha256=page.revision.sha256,
        today=page.today.isoformat(), warningDays=page.warning_days,
        totals=page.totals, items=page.items, nextCursor=page.next_cursor,
    )


def _local_today(conn: Connection) -> date:
    """Oggi nel fuso CONFIGURATO, con lo stesso codice dello scanner delle scadenze.

    Il fuso viene dalle impostazioni (`/api/settings`, §8.38), che è la stessa sorgente
    che usa il worker (`worker.py`: `tz_name = notif["timezone"]`). Riusare
    `local_today` invece di riscriverlo è ciò che fa sì che «scaduto» significhi la
    stessa cosa in un promemoria via posta e in questa risposta.

    ⚠ NON si usa `notifications.warningDays` delle impostazioni. Quelle sono le
    finestre di PREAVVISO del worker — un elenco, e serve la più ampia — mentre qui la
    soglia è quella della vista Scadenze, che nel frontend è la costante 90 e non
    guarda le impostazioni. Sono due concetti con lo stesso nome, e confonderli
    cambierebbe i livelli mostrati appena qualcuno modifica le notifiche.

    ⚠ Se le impostazioni non sono leggibili non si inventa un fuso: si lascia
    propagare. Un conteggio di giorni calcolato con un fuso di ripiego è un numero
    plausibile e sbagliato, che è peggio di un errore.
    """
    from datetime import datetime, timezone

    from app.notifications.expiry import local_today
    from app.settings import repository as settings_repo

    row = settings_repo.load(conn)
    notif = settings_repo.copy_notifications(row.data)
    return local_today(datetime.now(timezone.utc), notif["timezone"])
