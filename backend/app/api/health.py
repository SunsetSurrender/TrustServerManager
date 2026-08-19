"""Liveness e readiness. Sono due cose diverse e vanno tenute separate.

/api/health   liveness  — il processo è vivo e risponde. NON tocca il DB.
                          Se restituisse 503 a DB giù, l'orchestratore riavvierebbe
                          l'API per un guasto che non è dell'API.
/api/ready    readiness — il servizio può servire richieste. Tocca il DB.
                          È questo che il reverse proxy deve guardare.

Da quando esistono le rotte dell'inventario, «pronto» vuol dire quattro cose insieme
(§8.23, §8.44): database raggiungibile, migrazioni al livello atteso, testa
dell'inventario presente, e PROIEZIONE relazionale attuale. Un'istanza che risponde
200 con lo schema vecchio o senza inventario inizializzato manderebbe in errore ogni
richiesta, e sarebbe un guasto molto più difficile da diagnosticare di un 503
sincero.

La quarta condizione è nuova con la fase 2C, e c'è perché da quella fase l'API
PROMETTE di mantenere due rappresentazioni a ogni salvataggio. Se la proiezione non
rispecchia la testa, quella promessa non è mantenibile: ogni `PUT` verrà rifiutato
con 503 `projection_not_current`. Un backend che risponde «pronto» e poi rifiuta
tutte le scritture sta mentendo al reverse proxy, e a chi legge un grafico di
disponibilità.

Dalla fase 2D (§8.45) quella condizione conta il doppio: la proiezione non è più solo
ciò che si scrive, è anche ciò che si LEGGE. Una proiezione non attuale non rende
indisponibili soltanto i salvataggi — rende indisponibile l'inventario. In fase 2C il
`GET` funzionava ancora (leggeva il JSON) e proprio per questo il guasto era difficile
da vedere; adesso è impossibile non vederlo, ed è la readiness a dirlo per prima.

⚠ Si controlla lo STATO, non la fedeltà, e la separazione fra i tre costi è VOLUTA
(§8.45):

    readiness            versione, digest, versione della mappa: tre confronti fra
                         valori già registrati. Gira ogni pochi secondi per sempre
    GET /api/inventory   riassembla e verifica il giro completo, perché sta per
                         servire quel documento. Una volta per richiesta
    project.py --verify   la verifica operativa completa, quando una persona la chiede

Riassemblare l'inventario da SQL a ogni sonda costerebbe quanto un `--verify`
completo, ripetuto per sempre, e trasformerebbe la readiness in carico. La
conseguenza va detta esplicitamente: una colonna corrotta a mano lascia la readiness
VERDE — lo stato dichiara ancora la versione e il digest giusti — e fa cadere il
`GET`. Non è una lacuna: è la divisione del lavoro. Chi vuole la fedeltà la chiede a
`--verify`, e ogni `GET` la verifica per conto proprio.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)
router = APIRouter()

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def expected_head_revision() -> str | None:
    """Revisione attesa, ricavata dai file della migrazione.

    Si legge dal filesystem e non da una costante scritta a mano: una costante si
    dimentica di aggiornare, e allora la readiness direbbe «pronto» con lo schema
    sbagliato — cioè esattamente il caso che deve segnalare.
    """
    files = sorted(_MIGRATIONS.glob("[0-9]*.py"))
    if not files:
        return None
    for line in files[-1].read_text(encoding="utf-8").splitlines():
        if line.startswith("revision:"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


@router.get("/health", summary="Liveness: il processo risponde")
def health() -> dict:
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/ready", summary="Readiness: DB, migrazioni, inventario, proiezione")
def ready(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    checks = {"database": False, "migrations": False, "inventory": False,
              "projection": False}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = True

            expected = expected_head_revision()
            applied = conn.execute(
                text("SELECT version_num FROM alembic_version")).scalar()
            checks["migrations"] = bool(expected) and applied == expected

            head = conn.execute(
                text("SELECT version FROM inventory_head WHERE id IS TRUE")).scalar()
            checks["inventory"] = head is not None

            # Importata per nome e non dal pacchetto: `app.inventory` non riesporta
            # `projection` di proposito, così un `from app.inventory import
            # projection` scritto per sbaglio in una rotta non compila (§8.42).
            # Qui l'import è VOLUTO, e chiede solo lo stato.
            from app.inventory import projection
            checks["projection"] = projection.currency(conn).current
    except Exception as exc:
        # Il dettaglio nei log: la stringa di connessione e la topologia interna
        # non escono dall'API.
        log.warning("readiness fallita: %s", exc)

    if all(checks.values()):
        return {"status": "ready", **{k: "ok" for k in checks}}

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "unavailable",
            **{k: ("ok" if v else "not-ready") for k, v in checks.items()}}
