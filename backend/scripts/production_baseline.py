#!/usr/bin/env python3
"""Baseline di produzione: ispeziona, e inizializza una installazione NUOVA.

FUORI da HTTP, come il bootstrap, e per la stessa ragione moltiplicata: l'API non
ha nemmeno il privilegio di database per inserire la riga di testa (§8.19), e la
differenza fra «popolo un database vuoto» e «accetto una scrittura» non va affidata
a un parametro di una richiesta. Non è una rotta, non lo diventerà, e i ruoli di
runtime `tsm_api` e `tsm_worker` non possono eseguirla nemmeno se qualcuno gliela
mettesse davanti: glielo impediscono i privilegi, non un controllo nel codice.

Che cosa produce
----------------
Da un database VERGINE (migrato e nient'altro):

    inventory_versions          una riga, versione 1
    inventory_head              punta a quella
    proiezione relazionale      rispecchia quella, verificata riassemblando
    inventory_projection_state  versione, digest, versione della mappa, schema
    inventory_photo_refs        vuota
    users                       UN amministratore, password provvisoria da CSPRNG
    audit                       una riga: il bootstrap dell'inventario
    tutto il resto              vuoto

⚠ Con una eccezione, che la camera bianca ha scoperto: `maintenance_runs` puo' avere
dei giri di garbage collection a vuoto, lasciati dal worker che parte insieme allo
stack. Si guardano nel merito invece di contarli — vedi MAINTENANCE_MUST_BE_IDLE.

Nessuna storia di sviluppo viene importata. La storia di produzione COMINCIA qui:
la versione 1 è la baseline, e la versione 2 sarà la prima modifica di un operatore
vero.

Perché non «ripulire il database che c'è»
-----------------------------------------
La tentazione ovvia era `DELETE FROM inventory_devices; DELETE FROM audit; …`
contro il database esistente. È stata scartata, e non per eleganza: uno script così
è indistinguibile, nel momento in cui lo si lancia, fra un ambiente di prova e
un'installazione con sei mesi di lavoro dentro. Qui la baseline si COSTRUISCE da un
database vuoto, e su un database non vuoto il comando si RIFIUTA — vedi i cancelli.

I cancelli, e perché sono sei e non uno
---------------------------------------
    0. lo SCHEMA            le tabelle attese esistono (altrimenti si ferma qui)
    1. il DOCUMENTO         invarianti dello schema congelato + digest appuntato
    2. i PRIVILEGI          chi esegue deve essere il proprietario dello schema
    3. le MIGRAZIONI        lo schema applicato deve essere quello atteso
    4. l'AMBIENTE           TSM_ENV deve essere un valore riconosciuto
    5. lo STATO             tabella per tabella: niente storia, niente utenze
    6. il CONSENSO          --allow-initialise, dichiarato ogni volta

⚠ Il cancello 4 non è una prova di sicurezza, ed è importante dirlo. `TSM_ENV=test`
NON rende sacrificabile un database popolato, e `TSM_ENV=production` non autorizza
niente da solo: una variabile d'ambiente si sbaglia, si eredita da una shell, si
dimentica in un file. La prova sta nel cancello 5, che GUARDA il database. Gli altri
cinque servono a rendere difficile arrivarci per sbaglio; solo il 5 sa dire se il
bersaglio è quello che si crede.

⚠ Nessuna via di fuga per un bersaglio non vuoto. §16 del requisito la permetterebbe
per un «ambiente di ricostruzione esplicitamente supportato», e non ne è stata
aggiunta nessuna: un database nuovo costa un comando, e un rifiuto è più sicuro di
un consenso in più da ricordarsi di non dare (§21). Chi vuole ricominciare cancella
il database, non lo svuota da qui.

La password provvisoria
-----------------------
Generata da CSPRNG (192 bit), conservata solo come hash Argon2id, con
`must_change_pw` impostato: il primo accesso obbliga a cambiarla. Si stampa UNA
volta sul terminale di chi esegue, e non esiste nessun altro posto da cui rileggerla
— non i log, non l'audit, non un file di rapporto. `--json` è ammesso SOLO con
`--check`, appunto per non avere un artefatto in cui potrebbe finire.

⚠ Questo comando NON legge `TSM_BOOTSTRAP_PASSWORD`, a differenza di
`scripts/bootstrap.py`. Una password passata dall'ambiente finisce
nell'automazione che la passa, cioè in un file, cioè in un repository o in un log
di CI. E se la variabile è impostata il comando si FERMA invece di ignorarla: un
operatore che l'ha impostata andrebbe a cercare una password che non è stata usata.

Uso:
    python scripts/production_baseline.py --check  --baseline /baseline/production-baseline.json
    python scripts/production_baseline.py --check  --json --baseline …
    python scripts/production_baseline.py --init   --allow-initialise --baseline …

In Compose, dal servizio che è già proprietario dello schema:

    docker compose run --rm -v /opt/tsm/deploy/baseline:/baseline:ro migrate \\
        python scripts/production_baseline.py --check \\
            --baseline /baseline/production-baseline.json

Codici di uscita:
    --check   0 se l'inizializzazione sarebbe ammessa, 1 altrimenti. È un
              preflight, quindi fallisce chiuso: a differenza di
              `project.py --status`, che è un rapporto, questo risponde a una
              domanda con un sì o un no.
    --init    0 se inizializzata e verificata, 1 se rifiutata o abortita. In caso
              di rifiuto nel database non è cambiato niente.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text                     # noqa: E402
from sqlalchemy.engine import Connection                       # noqa: E402

from app.api.health import expected_head_revision              # noqa: E402
from app.auth.passwords import generate_temporary_password      # noqa: E402
from app.auth.service import create_user                        # noqa: E402
from app.config import get_settings                             # noqa: E402
from app.inventory import Actor, InventoryRepository            # noqa: E402
from app.inventory import baseline as bl                        # noqa: E402
from app.inventory import projection                            # noqa: E402

# ------------------------------------------------------------------ codici
#
# Stabili, e citati nella documentazione operativa: chi automatizza deve poter
# distinguere «già inizializzata» — che è una condizione normale, per esempio a un
# secondo tentativo — da «bersaglio sbagliato», che non lo è.
ALREADY_INITIALIZED = "production_baseline_already_initialized"
TARGET_NOT_EMPTY = "production_baseline_target_not_empty"
INSUFFICIENT_PRIVILEGE = "production_baseline_insufficient_privilege"
SCHEMA_MISMATCH = "production_baseline_schema_mismatch"
DOCUMENT_INVALID = "production_baseline_document_invalid"
NOT_ACKNOWLEDGED = "production_baseline_not_acknowledged"
PASSWORD_FROM_ENVIRONMENT = "production_baseline_password_from_environment"
UNKNOWN_ENVIRONMENT = "production_baseline_unknown_environment"

#: Tabelle che in un database vergine sono VUOTE. Elencate a mano e non ricavate da
#: `information_schema`: un elenco automatico includerebbe le tabelle nuove senza
#: che nessuno ci pensi, e «senza che nessuno ci pensi» è il modo in cui una tabella
#: con dei dati dentro passa il controllo il giorno che viene aggiunta.
#:
#: L'ordine è quello del rapporto: prima l'inventario, poi le utenze, poi il worker.
MUST_BE_EMPTY = (
    ("inventory_versions", "storia dell'inventario"),
    ("inventory_head", "testa dell'inventario"),
    ("audit", "registro"),
    ("inventory_locations", "proiezione: siti"),
    ("inventory_rooms", "proiezione: sale"),
    ("inventory_racks", "proiezione: rack"),
    ("inventory_devices", "proiezione: dispositivi"),
    ("inventory_manual_entries", "proiezione: manuale"),
    ("inventory_projection_state", "stato della proiezione"),
    ("inventory_photo_refs", "riferimenti alle foto"),
    ("photos", "foto"),
    ("users", "utenze"),
    ("sessions", "sessioni"),
    ("login_attempts", "tentativi di accesso"),
    ("reminders", "promemoria inviati"),
    ("reminder_deliveries", "consegne dei promemoria"),
    ("scheduler_runs", "giri dello scheduler"),
    ("notification_test_attempts", "invii di prova"),
)

#: ⚠ `maintenance_runs` NON e' in `MUST_BE_EMPTY`, e la ragione l'ha trovata la camera
#: bianca — nessuna suite poteva trovarla, perche' nessuna suite avvia il worker.
#:
#: Il worker e' un servizio Compose: parte insieme a tutto il resto, e la sua prima
#: cosa e' un giro di garbage collection delle foto. Su un database appena migrato quel
#: giro trova zero foto, non cancella niente, e lascia una riga
#: (`outcome=nothing_to_collect`) PRIMA che qualcuno esegua la baseline.
#:
#: Pretendere la tabella vuota vorrebbe dire chiedere all'operatore di fermare il
#: worker prima di inizializzare — e un cancello che dice no sul cammino normale
#: insegna a scavalcare i cancelli. Quindi si guarda nel MERITO: un giro che ha
#: esaminato e cancellato zero e' un giro su un database vuoto; un giro che ha
#: esaminato o cancellato qualcosa e' la prova che delle foto sono esistite, cioe' che
#: questo database ha una storia. La richiesta di §14 resta soddisfatta, e la si
#: soddisfa guardando il dato invece di contare le righe.
MAINTENANCE_MUST_BE_IDLE = (
    "SELECT count(*), coalesce(sum(examined_count), 0), "
    "coalesce(sum(deleted_count), 0) FROM maintenance_runs"
)


#: Tabelle che in un database vergine hanno UNA riga, messa là dalla migrazione che
#: le crea. Non si contano: si guardano nel merito.
#:
#: ⚠ Pretenderle vuote sarebbe pretendere uno stato che le migrazioni non producono,
#: e il cancello direbbe no a ogni installazione sana. È il primo esito che questo
#: comando ha dato: «il bersaglio contiene dati: battito del worker (1)» su un
#: database appena migrato, che è un rifiuto giusto per una ragione sbagliata.
SINGLETONS = ("settings", "worker_heartbeat")


@dataclass
class Gate:
    """Un cancello: nome, esito, dettaglio leggibile, codice se ha detto no."""
    name: str
    ok: bool
    detail: str
    code: str = ""


def _count(conn: Connection, table: str) -> int:
    return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


# ------------------------------------------------------------------ cancelli

def gate_schema(conn: Connection) -> Gate:
    """Cancello 0: le tabelle attese esistono.

    ⚠ Va PRIMA di ogni altra interrogazione, e la ragione è del database e non
    dell'ordine estetico: in PostgreSQL una query su una tabella inesistente aborta
    la TRANSAZIONE, non solo se stessa. Senza questo cancello, `--check` contro un
    database non migrato sollevava su `alembic_version` e poi ogni cancello
    successivo falliva a cascata: l'operatore riceveva una traccia di stack, che è
    il modo peggiore di dire «mancano le migrazioni».
    """
    attese = {"alembic_version", "maintenance_runs", *SINGLETONS,
              *(t for t, _ in MUST_BE_EMPTY)}
    # ⚠ `pg_catalog.pg_class` e NON `information_schema.tables`, e la differenza e' un
    # difetto vero che questo cancello ha avuto.
    #
    # `information_schema` e' FILTRATA DAI PRIVILEGI: elenca solo le tabelle su cui
    # chi interroga ha almeno un permesso. Eseguito come `tsm_api` — che non ha nessun
    # grant su `reminders`, `scheduler_runs`, `worker_heartbeat`,
    # `reminder_deliveries`, `maintenance_runs` — il cancello dichiarava «5 tabelle
    # attese assenti: applicare le migrazioni».
    #
    # Quel messaggio e' peggio dell'errore: manda a eseguire migrazioni che sono gia'
    # applicate, mentre il problema e' il ruolo sbagliato. E il cancello dei privilegi,
    # che l'avrebbe detto, non veniva mai raggiunto perche' questo lo precede.
    #
    # `pg_class` risponde «la tabella ESISTE», che e' la domanda di questo cancello.
    # Chi puo' farci qualcosa e' la domanda del cancello 2.
    presenti = {
        str(r[0]) for r in conn.execute(text("""
            SELECT c.relname
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
        """))
    }
    mancanti = sorted(attese - presenti)
    if mancanti:
        return Gate("schema", False,
                    f"{len(mancanti)} tabelle attese assenti "
                    f"({', '.join(mancanti[:5])}"
                    f"{', …' if len(mancanti) > 5 else ''}). Applicare le "
                    f"migrazioni: `python scripts/migrate.py`.", SCHEMA_MISMATCH)
    return Gate("schema", True, f"{len(attese)} tabelle attese presenti")


def gate_document(doc_path: Path | None) -> tuple[Gate, dict | None]:
    """Cancello 1: il documento è UNA baseline, e proprio QUELLA."""
    try:
        doc = bl.load(doc_path)
    except FileNotFoundError:
        return Gate("documento", False,
                    f"artefatto non trovato: {doc_path or bl.DEFAULT_BASELINE_PATH}. "
                    f"In produzione va montato: vedi deploy/README.md.",
                    DOCUMENT_INVALID), None
    except json.JSONDecodeError as exc:
        return Gate("documento", False, f"JSON non valido: {exc}",
                    DOCUMENT_INVALID), None

    problemi = bl.invariants(doc)
    if problemi:
        righe = "; ".join(f"[{p.code}] {p.path or '(radice)'}" for p in problemi[:6])
        return Gate("documento", False,
                    f"{len(problemi)} invarianti violati: {righe}",
                    DOCUMENT_INVALID), doc
    c = bl.counts(doc)
    return Gate("documento", True,
                f"digest appuntato, {c['locations']} siti, {c['rooms']} sale, "
                f"{c['racks']} rack, 0 dispositivi"), doc


def gate_privilege(conn: Connection) -> Gate:
    """Cancello 2: chi esegue è il proprietario dello schema (o suo membro).

    Non si confronta con la stringa `"tsm"`: il nome del proprietario è una scelta
    dell'installazione. Si chiede al catalogo chi possiede `inventory_head` — la
    tabella che solo il proprietario può popolare — e si verifica l'appartenenza.
    Un controllo per nome passerebbe su un'installazione che ha chiamato il ruolo
    diversamente, e passerebbe per il motivo sbagliato.
    """
    row = conn.execute(text("""
        SELECT current_user,
               pg_catalog.pg_get_userbyid(c.relowner) AS owner
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relname = 'inventory_head' AND n.nspname = current_schema()
    """)).first()
    if row is None:
        return Gate("privilegi", False,
                    "tabella inventory_head assente: schema non migrato",
                    SCHEMA_MISMATCH)
    utente, proprietario = str(row[0]), str(row[1])
    membro = bool(conn.execute(
        text("SELECT pg_has_role(current_user, :owner, 'MEMBER')"),
        {"owner": proprietario}).scalar())
    if not membro:
        return Gate("privilegi", False,
                    f"in esecuzione come {utente!r}, che non è {proprietario!r} né "
                    f"un suo membro. La baseline si crea come proprietario dello "
                    f"schema: i ruoli di runtime non ne hanno il privilegio, di "
                    f"proposito (§8.19).", INSUFFICIENT_PRIVILEGE)
    return Gate("privilegi", True,
                f"{utente} è {'il proprietario' if utente == proprietario else f'membro di {proprietario}'}")


def gate_migrations(conn: Connection) -> Gate:
    """Cancello 3: lo schema applicato è quello che questo codice si aspetta."""
    atteso = expected_head_revision()
    applicato = conn.execute(
        text("SELECT version_num FROM alembic_version")).scalar()
    if not atteso:
        return Gate("migrazioni", False,
                    "impossibile determinare la revisione attesa dai file",
                    SCHEMA_MISMATCH)
    if applicato != atteso:
        return Gate("migrazioni", False,
                    f"applicata {applicato!r}, attesa {atteso!r}. Eseguire le "
                    f"migrazioni prima della baseline.", SCHEMA_MISMATCH)
    return Gate("migrazioni", True, f"{applicato} (attesa)")


def gate_environment() -> Gate:
    """Cancello 4: TSM_ENV riconosciuto. NON è una prova: vedi la testa del modulo."""
    try:
        env = get_settings().env
    except Exception as exc:                                    # pragma: no cover
        return Gate("ambiente", False, f"configurazione rifiutata: {exc}",
                    UNKNOWN_ENVIRONMENT)
    return Gate("ambiente", True,
                f"TSM_ENV={env} (riconosciuto; non è una prova che il bersaglio "
                f"sia sacrificabile — quella la dà lo stato)")


def gate_state(conn: Connection) -> tuple[Gate, dict]:
    """Cancello 5: il bersaglio è vergine. È il cancello che PROVA qualcosa."""
    conteggi = {t: _count(conn, t) for t, _ in MUST_BE_EMPTY}

    # L'inventario prima di tutto, con un codice suo: «già inizializzata» è una
    # condizione normale — un secondo tentativo, un playbook rieseguito — e chi
    # automatizza deve poterla distinguere da «bersaglio sbagliato».
    if conteggi["inventory_head"] or conteggi["inventory_versions"]:
        head = conn.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar()
        return Gate("stato", False,
                    f"inventario già inizializzato (testa: versione {head}, "
                    f"{conteggi['inventory_versions']} versioni in storia). La "
                    f"baseline è una-volta-sola: non si riesegue su una "
                    f"installazione avviata.", ALREADY_INITIALIZED), conteggi

    piene = [(t, d, conteggi[t]) for t, d in MUST_BE_EMPTY if conteggi[t]]
    if piene:
        elenco = ", ".join(f"{d} ({t}): {n}" for t, d, n in piene)
        return Gate("stato", False,
                    f"il bersaglio contiene dati: {elenco}. La baseline si "
                    f"costruisce su un database vuoto, e non svuota nulla: se "
                    f"questi dati sono sacrificabili, il modo di dirlo è "
                    f"ricreare il database.", TARGET_NOT_EMPTY), conteggi

    # I giri di manutenzione: si guardano nel merito. Vedi MAINTENANCE_MUST_BE_IDLE.
    giri, esaminate, cancellate = conn.execute(
        text(MAINTENANCE_MUST_BE_IDLE)).one()
    if esaminate or cancellate:
        return Gate("stato", False,
                    f"la garbage collection delle foto ha gia' lavorato su questo "
                    f"database ({giri} giri, {esaminate} foto esaminate, "
                    f"{cancellate} cancellate): delle foto sono esistite, quindi "
                    f"questo database ha una storia.", TARGET_NOT_EMPTY), conteggi
    conteggi["maintenance_runs"] = int(giri)

    # Le due righe che DEVONO esistere, controllate nel merito e non contate.
    #
    # §14: il worker non deve sembrare aver già girato. Un `last_run_date` di oggi
    # — lasciato da un worker di prova sullo stesso database — farebbe saltare il
    # primo giro vero per idempotenza, e nessuno cercherebbe la causa in una riga
    # di battito.
    battito = conn.execute(text(
        "SELECT state, last_run_date FROM worker_heartbeat")).first()
    if battito is None:
        return Gate("stato", False,
                    "worker_heartbeat senza riga: la migrazione 0008 ne inserisce "
                    "una, quindi questo schema non è quello atteso",
                    SCHEMA_MISMATCH), conteggi
    if battito[1] is not None:
        return Gate("stato", False,
                    f"il worker risulta aver già girato il {battito[1]} (stato "
                    f"{battito[0]!r}). Un giro registrato fa saltare il primo giro "
                    f"vero per idempotenza: il bersaglio non è vergine.",
                    TARGET_NOT_EMPTY), conteggi

    # §12: le notifiche partono DISATTIVE, senza destinatari. Non si scrivono qui —
    # le stabilisce la migrazione 0007 — si verificano: se qualcuno le ha già
    # accese, questo database ha una storia.
    riga = conn.execute(text(
        "SELECT version, data FROM settings WHERE id = 1")).first()
    if riga is None:
        return Gate("stato", False,
                    "settings senza riga: la migrazione 0007 ne inserisce una",
                    SCHEMA_MISMATCH), conteggi
    dati = riga[1] if not isinstance(riga[1], str) else json.loads(riga[1])
    notifiche = (dati or {}).get("notifications") or {}
    if riga[0] != 1 or notifiche.get("enabled") or notifiche.get("recipients"):
        return Gate("stato", False,
                    f"le impostazioni sono già state modificate (revisione "
                    f"{riga[0]}, notifiche "
                    f"{'attive' if notifiche.get('enabled') else 'disattive'}, "
                    f"{len(notifiche.get('recipients') or [])} destinatari): "
                    f"questo database ha una storia.", TARGET_NOT_EMPTY), conteggi

    return Gate("stato", True,
                f"{len(MUST_BE_EMPTY)} tabelle vuote, {len(SINGLETONS)} righe "
                f"singole al valore iniziale (worker mai girato, notifiche "
                f"disattive), {giri} giri di GC a vuoto"), conteggi


def gate_no_environment_password() -> Gate:
    """Cancello 6-bis: `TSM_BOOTSTRAP_PASSWORD` impostata → si ferma.

    Ignorarla sarebbe peggio: l'operatore andrebbe a cercare una password che non è
    stata usata, e la troverebbe scritta da qualche parte a fare da esca.
    """
    if os.environ.get("TSM_BOOTSTRAP_PASSWORD"):
        return Gate("password", False,
                    "TSM_BOOTSTRAP_PASSWORD è impostata. Questo comando genera "
                    "sempre la provvisoria da CSPRNG e non legge l'ambiente: una "
                    "password passata così finisce nell'automazione che la passa. "
                    "Togliere la variabile e rieseguire.", PASSWORD_FROM_ENVIRONMENT)
    return Gate("password", True, "sarà generata da CSPRNG (192 bit), mostrata una volta")


# ------------------------------------------------------------------ rapporto

def _manifest(gates: list[Gate], doc: dict | None, conteggi: dict,
              conn: Connection) -> dict:
    """Il manifesto di §18: senza segreti, e deterministico a meno dei metadati."""
    impostazioni = conn.execute(text(
        "SELECT version, data FROM settings WHERE id = 1")).first()
    dati = impostazioni[1] if impostazioni else {}
    if isinstance(dati, str):                                   # pragma: no cover
        dati = json.loads(dati)
    notifiche = (dati or {}).get("notifications") or {}

    return {
        # --- il documento ---
        "document": bl.document_manifest(doc) if doc else None,
        # --- lo schema ---
        "alembic_revision_applied": conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar(),
        "alembic_revision_expected": expected_head_revision(),
        "mapper_version": projection.MAPPER_VERSION,
        # --- ciò che ci sarà DOPO l'inizializzazione ---
        "expected_initial_inventory_version": 1,
        "expected_user_count": 1,
        # --- ciò che c'è ORA ---
        "current_row_counts": conteggi,
        "notifications_enabled": bool(notifiche.get("enabled")),
        "notification_recipients": len(notifiche.get("recipients") or []),
        "photo_count": conteggi.get("photos"),
        # --- l'esito ---
        "gates": [{"name": g.name, "ok": g.ok, "code": g.code or None,
                   "detail": g.detail} for g in gates],
        "ok": all(g.ok for g in gates),
    }


def _print_report(gates: list[Gate], manifesto: dict) -> None:
    print("baseline di produzione — ispezione")
    d = manifesto.get("document") or {}
    c = d.get("counts") or {}
    print(f"  artefatto    digest {(d.get('canonical_sha256') or '?')[:16]}…")
    print(f"  schema       documento v{d.get('schemaVersion')}, "
          f"migrazioni {manifesto['alembic_revision_applied']} "
          f"(attesa {manifesto['alembic_revision_expected']}), "
          f"mappa v{manifesto['mapper_version']}")
    print(f"  struttura    {c.get('locations')} siti, {c.get('rooms')} sale, "
          f"{c.get('racks')} rack, {c.get('rack_serials')} seriali di armadio, "
          f"{c.get('vani')} vani")
    print(f"  dispositivi  {c.get('devices')}   voci di manuale {c.get('manual')}   "
          f"riferimenti a foto {d.get('photo_refs')}")
    print(f"  dopo         versione {manifesto['expected_initial_inventory_version']}, "
          f"{manifesto['expected_user_count']} utenza, "
          f"notifiche {'attive' if manifesto['notifications_enabled'] else 'disattive'}, "
          f"{manifesto['notification_recipients']} destinatari")
    print("  cancelli")
    for g in gates:
        segno = "OK  " if g.ok else "NO  "
        print(f"    {segno} {g.name:<12} {g.detail}")
        if not g.ok and g.code:
            print(f"         codice: {g.code}")


# ------------------------------------------------------------------ comando

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--check", action="store_true",
                      help="ispezione in SOLA LETTURA: non tocca niente")
    modo.add_argument("--init", action="store_true",
                      help="inizializza una installazione NUOVA (richiede "
                           "--allow-initialise)")
    ap.add_argument("--allow-initialise", action="store_true",
                    help="obbligatorio con --init: conferma che questo database è "
                         "una installazione nuova")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="percorso dell'artefatto (in produzione: montato)")
    ap.add_argument("--admin", default="admin",
                    help="username del primo amministratore")
    ap.add_argument("--json", action="store_true",
                    help="manifesto in JSON, senza segreti. Solo con --check.")
    args = ap.parse_args()

    # ⚠ `--json` con `--init` è VIETATO, e non è pedanteria: il manifesto è la
    # cosa che si redirige in un file, e la provvisoria è la cosa che non deve
    # finire in un file. Tenerli su due comandi diversi è ciò che rende
    # l'affermazione «non esiste un artefatto con la password» verificabile a
    # colpo d'occhio invece che per lettura del codice.
    if args.json and args.init:
        print("--json è ammesso solo con --check: --init stampa una password "
              "provvisoria, e un manifesto è la cosa che si salva su file.",
              file=sys.stderr)
        return 2

    gate_doc, doc = gate_document(args.baseline)

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        with engine.begin() as conn:
            gates = [gate_doc]
            manifesto = None

            # ⚠ SCHEMA e PRIVILEGI sono PREREQUISITI, non altri due cancelli: sono le
            # condizioni per poter PORRE le altre domande. Senza le tabelle, o senza
            # il permesso di leggerle, ogni cancello successivo fallisce per una causa
            # che non è la sua — e in PostgreSQL una query non permessa ABORTA LA
            # TRANSAZIONE, quindi il fallimento è a cascata.
            #
            # È il difetto che il comando ha avuto due volte: eseguito su un database
            # non migrato dava una traccia di stack su `alembic_version`, ed eseguito
            # come `tsm_api` ne dava una su `reminders` — la tabella più irrilevante
            # fra tutte — al posto di «stai usando il ruolo sbagliato».
            prerequisiti_ok = True
            for prerequisito in (gate_schema(conn), gate_privilege(conn)):
                gates.append(prerequisito)
                if not prerequisito.ok:
                    prerequisiti_ok = False
                    break

            if prerequisiti_ok:
                # Gli altri sono domande INDIPENDENTI, e si valutano tutte: chi
                # diagnostica ha bisogno di sapere se ne ha una o quattro. È lo stesso
                # ragionamento di `project.py --verify`, che stampa sempre entrambe le
                # sue cause invece della prima.
                gates += [gate_migrations(conn), gate_environment()]
                gate_st, conteggi = gate_state(conn)
                gates.append(gate_st)
                if args.init:
                    gates.append(gate_no_environment_password())
                manifesto = _manifest(gates, doc, conteggi, conn)

        if manifesto is None:
            if args.json:
                print(json.dumps({"gates": [{"name": g.name, "ok": g.ok,
                                             "code": g.code or None,
                                             "detail": g.detail} for g in gates],
                                  "ok": False}, ensure_ascii=False, indent=2))
            else:
                print("baseline di produzione — ispezione")
                for g in gates:
                    print(f"    {'OK  ' if g.ok else 'NO  '}{g.name:<12} {g.detail}")
                    if not g.ok and g.code:
                        print(f"         codice: {g.code}")
                print("  esito        RIFIUTATA")
            return 1

        if args.json:
            print(json.dumps(manifesto, ensure_ascii=False, indent=2, default=str))
            return 0 if manifesto["ok"] else 1

        _print_report(gates, manifesto)

        if args.check:
            print(f"  esito        {'AMMESSA' if manifesto['ok'] else 'RIFIUTATA'}")
            return 0 if manifesto["ok"] else 1

        # ---------------------------------------------------------- --init
        if not args.allow_initialise:
            print(f"\nRIFIUTATO [{NOT_ACKNOWLEDGED}]: --init crea lo stato "
                  f"iniziale di una installazione di produzione.\n"
                  f"Aggiungere --allow-initialise per confermare che questo "
                  f"database è una installazione NUOVA.", file=sys.stderr)
            return 1

        if not manifesto["ok"]:
            primo = next(g for g in gates if not g.ok)
            print(f"\nRIFIUTATO [{primo.code}]: {primo.detail}\n"
                  f"Nel database non è cambiato niente.", file=sys.stderr)
            return 1

        # Da qui si scrive. UNA transazione: se qualunque passo solleva, non resta
        # né un amministratore senza inventario né un inventario senza
        # amministratore — che sono i due stati a metà da cui è difficile uscire.
        password = generate_temporary_password()
        print("\ninizializzazione")
        with engine.begin() as conn:
            user_id = create_user(conn, args.admin, password, "admin",
                                  must_change_pw=True)
            actor = Actor(username=args.admin, role="admin", user_id=user_id)
            result = InventoryRepository(conn).bootstrap(doc, actor)
            print(f"  amministratore  {args.admin} (deve cambiare la password)")
            print(f"  inventario      versione {result.version}")
            stato = projection.status(conn)
            print(f"  proiezione      versione {stato.projected_version}, "
                  f"mappa v{stato.mapper_version}")
            print(f"  righe           " + ", ".join(
                f"{n} {k}" for k, n in stato.counts.items()))

        # La verifica DOPO il commit, con una connessione nuova: verificare dentro
        # la stessa transazione proverebbe che ciò che ho scritto è ciò che ho
        # scritto. Questo prova che ciò che è COMMITTATO riassembla la versione
        # che dichiara.
        with engine.begin() as conn:
            esito = projection.verify(conn)
        if not esito.ok:
            print(f"\n⚠ VERIFICA FALLITA dopo il commit: {esito.reason}. "
                  f"L'inventario è scritto ma la proiezione non lo riassembla: "
                  f"eseguire `project.py --verify` e NON mettere in servizio.",
                  file=sys.stderr)
            return 1
        print("  verifica        la proiezione riassembla la versione 1")

        # ⚠ La provvisoria per ULTIMA, e da sola. Se qualcosa fosse andato storto
        # sopra, non c'è nessuna password stampata da cercare di capire se valga
        # ancora. E si stampa qui e in nessun altro posto: non nei log, non
        # nell'audit, non in un file.
        print("\n" + "=" * 66)
        print(f"password provvisoria di {args.admin}:  {password}")
        print("=" * 66)
        print("Va cambiata al primo accesso, e NON verrà mostrata di nuovo:")
        print("non è scritta in chiaro da nessuna parte. Se si perde, si ricrea")
        print("l'amministratore dal database (deploy/README.md).")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
