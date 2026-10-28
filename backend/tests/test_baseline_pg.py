"""Baseline di produzione: lo STATO che produce, e i no che dice.

PostgreSQL reale, e qui non è una preferenza. Ciò che si verifica è precisamente
comportamento del database: quale numero prende una colonna identity su una tabella
appena creata, che cosa può fare un ruolo con `SET ROLE`, che cosa resta scritto
quando una transazione si annulla. Un doppio le farebbe come me le immagino io.

Il comando si esegue come COMANDO — `subprocess`, non `import` — e la ragione è che
gran parte di ciò che va provato è la sua superficie: il codice di uscita, la
separazione fra stdout e stderr, il fatto che la password provvisoria compaia una
volta e in nessun altro posto. Chiamare `main()` proverebbe la logica e non il
comando, e il comando è ciò che un operatore lancia alle otto di sera.

Cinque affermazioni:

  1. su un database VERGINE l'inizializzazione produce esattamente lo stato di §24:
     versione 1, testa, proiezione fedele, un amministratore, niente altro;
  2. su qualunque altro database si RIFIUTA, e nel database non cambia niente;
  3. la password provvisoria non è recuperabile da nessuna parte;
  4. i ruoli di runtime non possono fare ciò che la baseline fa;
  5. la prima modifica vera è la versione 2, e la proiezione la segue.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.inventory import baseline as bl
from app.inventory import projection
from app.identity.model import walk
from app.inventory.digest import canonical_sha256

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
COMANDO = BACKEND / "scripts" / "production_baseline.py"
ARTEFATTO = ROOT / "deploy" / "baseline" / "production-baseline.json"

#: Come `MUST_BE_EMPTY` del comando, ma qui serve a RIPULIRE. L'ordine è quello
#: imposto dalle chiavi esterne.
DA_SVUOTARE = (
    "reminders", "reminder_deliveries", "scheduler_runs",
    "notification_test_attempts", "login_attempts", "sessions",
    "maintenance_runs",
)


# ==================================================================
# impianto
# ==================================================================

@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def vergine(engine):
    """Il database COME LO LASCIANO LE MIGRAZIONI, e non «vuoto».

    ⚠ La differenza conta, ed è il primo esito che il comando ha dato in fase di
    sviluppo: `settings` e `worker_heartbeat` nascono con UNA riga, inserita dalla
    migrazione che le crea. Un `TRUNCATE` di tutto produrrebbe uno stato che nessuna
    installazione ha mai avuto, e i test proverebbero il comando contro una
    situazione impossibile.
    """
    from app.settings.schema import DEFAULTS
    with engine.begin() as c:
        c.execute(text("UPDATE reminders SET delivery_id = NULL"))
        for tabella in DA_SVUOTARE:
            c.execute(text(f"DELETE FROM {tabella}"))
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM photos"))
        c.execute(text("UPDATE settings SET updated_by = NULL WHERE id = 1"))
        c.execute(text("DELETE FROM users"))
        # Lo stato iniziale delle due righe singole, ricostruito esplicitamente.
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb), version = 1 "
                       "WHERE id = 1"), {"d": json.dumps(DEFAULTS)})
        c.execute(text("UPDATE worker_heartbeat SET state = 'starting', "
                       "last_run_date = NULL, detail = NULL"))
    return engine


def esegui(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(COMANDO), *args],
        cwd=str(BACKEND), capture_output=True, text=True, encoding="utf-8",
        env=env, timeout=300)


def check(*extra: str, **kw) -> subprocess.CompletedProcess:
    return esegui("--check", "--baseline", str(ARTEFATTO), *extra, **kw)


def init(*extra: str, **kw) -> subprocess.CompletedProcess:
    return esegui("--init", "--baseline", str(ARTEFATTO), *extra, **kw)


def conta(engine, tabella: str) -> int:
    with engine.begin() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {tabella}")).scalar_one())


def estrai_password(stdout: str) -> str:
    trovate = re.findall(r"^password provvisoria di \S+:\s+(\S+)$", stdout,
                         re.MULTILINE)
    assert len(trovate) == 1, (
        f"la provvisoria deve comparire ESATTAMENTE una volta, trovata "
        f"{len(trovate)} volte")
    return trovate[0]


# ==================================================================
# 1. --check: sola lettura, e risponde sì o no
# ==================================================================

def test_check_su_vergine_dice_si(vergine):
    r = check()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AMMESSA" in r.stdout


def test_check_non_scrive_NIENTE(vergine):
    prima = {t: conta(vergine, t) for t in
             ("inventory_versions", "users", "audit", "inventory_racks")}
    check()
    assert {t: conta(vergine, t) for t in prima} == prima


def test_il_manifesto_in_json_ha_i_campi_di_18(vergine):
    r = check("--json")
    assert r.returncode == 0, r.stdout + r.stderr
    m = json.loads(r.stdout)
    assert m["document"]["canonical_sha256"] == bl.BASELINE_CANONICAL_SHA256
    assert m["document"]["counts"]["devices"] == 0
    assert m["document"]["counts"]["racks"] == 102
    assert m["document"]["counts"]["manual"] == 0
    assert m["expected_initial_inventory_version"] == 1
    assert m["expected_user_count"] == 1
    assert m["notifications_enabled"] is False
    assert m["notification_recipients"] == 0
    assert m["photo_count"] == 0
    assert m["mapper_version"] == projection.MAPPER_VERSION
    assert m["alembic_revision_applied"] == m["alembic_revision_expected"]
    assert m["ok"] is True


def test_il_manifesto_NON_contiene_segreti(vergine):
    r = check("--json")
    testo = r.stdout.lower()
    for parola in ("password", "argon2", "hash", "secret", "salt"):
        assert parola not in testo, (
            f"il manifesto nomina {parola!r}: è la cosa che si salva su file")


def test_il_manifesto_e_DETERMINISTICO(vergine):
    """Due esecuzioni consecutive devono dare lo stesso testo.

    Se non lo fossero, confrontare il manifesto di prima e dopo un aggiornamento —
    che è il modo in cui si guarda se è cambiato qualcosa — sarebbe inutile.
    """
    a, b = check("--json").stdout, check("--json").stdout
    assert a == b


def test_json_e_vietato_con_init(vergine):
    r = init("--allow-initialise", "--json")
    assert r.returncode == 2
    assert "solo con --check" in r.stderr
    assert conta(vergine, "inventory_versions") == 0


# ==================================================================
# 2. --init: il consenso, e i rifiuti
# ==================================================================

def test_senza_consenso_si_rifiuta(vergine):
    r = init()
    assert r.returncode == 1
    assert "production_baseline_not_acknowledged" in r.stderr
    assert conta(vergine, "inventory_versions") == 0
    assert conta(vergine, "users") == 0


def test_TSM_BOOTSTRAP_PASSWORD_ferma_il_comando(vergine):
    """Ignorarla sarebbe peggio: l'operatore cercherebbe una password non usata."""
    r = init("--allow-initialise",
             env_extra={"TSM_BOOTSTRAP_PASSWORD": "una-password-lunga-abbastanza"})
    assert r.returncode == 1
    assert "production_baseline_password_from_environment" in r.stderr
    assert conta(vergine, "users") == 0


def test_un_bersaglio_con_UTENZE_si_rifiuta(vergine):
    from app.auth.service import create_user
    with vergine.begin() as c:
        create_user(c, "qualcuno", "password-lunga-1", "admin",
                    must_change_pw=False)
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_target_not_empty" in r.stderr
    assert "utenze" in r.stderr
    assert conta(vergine, "inventory_versions") == 0


def test_un_bersaglio_con_un_WORKER_che_ha_girato_si_rifiuta(vergine):
    """§14: un giro registrato fa saltare il primo giro vero per idempotenza.

    È il caso più insidioso dell'elenco, perché una riga di battito non somiglia a
    «dati»: nessuno la cerca, e l'effetto — nessun avviso il primo giorno — si
    scopre non ricevendo una mail.
    """
    with vergine.begin() as c:
        c.execute(text("UPDATE worker_heartbeat SET state = 'running', "
                       "last_run_date = CURRENT_DATE"))
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_target_not_empty" in r.stderr
    assert "già girato" in r.stderr


def test_un_bersaglio_con_NOTIFICHE_ATTIVE_si_rifiuta(vergine):
    """§12: le notifiche partono disattive. Se sono accese, questo database ha una
    storia — e la baseline non ci va sopra."""
    with vergine.begin() as c:
        c.execute(text("""
            UPDATE settings
               SET data = jsonb_set(data, '{notifications,enabled}', 'true'),
                   version = version + 1
             WHERE id = 1"""))
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_target_not_empty" in r.stderr
    assert "impostazioni" in r.stderr


def test_un_bersaglio_con_FOTO_si_rifiuta(vergine):
    import hashlib
    payload = b"\x89PNG\r\n\x1a\n\x00"
    with vergine.begin() as c:
        c.execute(text("""
            INSERT INTO photos (id, mime, bytes, sha256, size_bytes)
            VALUES ('55555555-5555-4555-8555-555555555555', 'image/png',
                    :b, :s, :n)"""),
                  {"b": payload, "s": hashlib.sha256(payload).hexdigest(),
                   "n": len(payload)})
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_target_not_empty" in r.stderr


def test_un_giro_di_GC_A_VUOTO_non_blocca(vergine):
    """⚠ La riga che la camera bianca ha trovato, e che nessuna suite poteva trovare.

    Il worker e' un servizio Compose: parte insieme allo stack e fa subito un giro di
    garbage collection delle foto. Su un database appena migrato quel giro esamina
    zero foto e ne cancella zero, e lascia una riga PRIMA che qualcuno esegua la
    baseline.

    La prima stesura pretendeva `maintenance_runs` vuota, e su una installazione vera
    diceva: «il bersaglio contiene dati: giri di manutenzione (1)». Un rifiuto giusto
    per una ragione sbagliata, sul cammino NORMALE — cioe' il tipo di cancello che
    insegna a scavalcare i cancelli.
    """
    with vergine.begin() as c:
        c.execute(text("""
            INSERT INTO maintenance_runs
                   (job, run_date, timezone, started_at, finished_at,
                    examined_count, deleted_count, outcome)
            VALUES ('photo_gc', CURRENT_DATE, 'Europe/Rome', now(), now(),
                    0, 0, 'nothing_to_collect')"""))
    r = check()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AMMESSA" in r.stdout
    assert "giri di GC a vuoto" in r.stdout


def test_un_giro_di_GC_che_ha_CANCELLATO_blocca(vergine):
    """L'altro verso, e senza di lui la regola sarebbe solo permissiva.

    Se la GC ha esaminato o cancellato qualcosa, delle foto sono esistite — e delle
    foto esistono solo se qualcuno ha usato l'applicazione. Questo database ha una
    storia, e la baseline non ci va sopra.
    """
    with vergine.begin() as c:
        c.execute(text("""
            INSERT INTO maintenance_runs
                   (job, run_date, timezone, started_at, finished_at,
                    examined_count, deleted_count, outcome)
            VALUES ('photo_gc', CURRENT_DATE - 1, 'Europe/Rome', now(), now(),
                    12, 3, 'collected')"""))
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_target_not_empty" in r.stderr
    assert "foto esaminate" in r.stderr
    assert conta(vergine, "inventory_versions") == 0


def test_un_artefatto_SBAGLIATO_si_rifiuta(vergine, tmp_path):
    """Il digest appuntato: un altro documento, anche valido, non è LA baseline."""
    finto = tmp_path / "finto.json"
    doc = bl.load(ARTEFATTO)
    doc["locations"][0]["nome"] = "Sito inventato"
    finto.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = esegui("--init", "--allow-initialise", "--baseline", str(finto))
    assert r.returncode == 1
    assert "production_baseline_document_invalid" in r.stderr
    assert conta(vergine, "inventory_versions") == 0


def test_la_fixture_di_QUALIFICAZIONE_non_puo_diventare_baseline(vergine, tmp_path):
    """La barriera che §15 chiede per nome, provata sul comando vero.

    Lo scenario delle notifiche è un documento valido, di forma reale, e con dei
    dispositivi dentro: è esattamente ciò che non deve finire in produzione.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tsm_qual_pg", ROOT / "fixtures" / "qualification" / "build.py")
    qual = importlib.util.module_from_spec(spec)
    # ⚠ In `sys.modules` PRIMA di eseguire: il modulo definisce una dataclass, e
    # `dataclasses` risolve le annotazioni cercando `cls.__module__` nel registro.
    # Senza, l'esecuzione muore con un AttributeError che non parla di niente.
    sys.modules["tsm_qual_pg"] = qual
    spec.loader.exec_module(qual)

    percorso = tmp_path / "qualificazione.json"
    percorso.write_text(json.dumps(qual.build_inventory(date(2026, 9, 1)),
                                   ensure_ascii=False), encoding="utf-8")
    r = esegui("--init", "--allow-initialise", "--baseline", str(percorso))
    assert r.returncode == 1
    assert "production_baseline_document_invalid" in r.stderr
    assert conta(vergine, "inventory_versions") == 0
    assert conta(vergine, "users") == 0


def test_un_artefatto_ASSENTE_si_rifiuta_con_un_messaggio_utile(vergine, tmp_path):
    r = esegui("--check", "--baseline", str(tmp_path / "non-esiste.json"))
    assert r.returncode == 1
    assert "non trovato" in r.stdout or "non trovato" in r.stderr


# ==================================================================
# 3. --init sul bersaglio giusto: lo stato di §24
# ==================================================================

@pytest.fixture
def inizializzata(vergine):
    """Database inizializzato dal comando vero, con la provvisoria catturata."""
    r = init("--allow-initialise")
    assert r.returncode == 0, r.stdout + r.stderr
    return vergine, estrai_password(r.stdout), r


def test_la_versione_e_UNO(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        assert c.execute(text("SELECT version FROM inventory_versions")).scalar() == 1
        assert c.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM inventory_versions")).scalar() == 1


def test_il_digest_registrato_e_quello_APPUNTATO(inizializzata):
    """L'istantanea nel database è il documento revisionato, non una sua variante."""
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        sha = c.execute(text(
            "SELECT canonical_sha256 FROM inventory_versions WHERE version = 1")
        ).scalar()
    assert sha == bl.BASELINE_CANONICAL_SHA256


def test_lo_stato_della_proiezione_e_completo(inizializzata):
    """§7: versione, digest, versione della mappa, metadati dello schema."""
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        riga = c.execute(text("""
            SELECT head_version, head_sha256, mapper_version, schema_version,
                   has_manual, root_extra, synchronised_at
              FROM inventory_projection_state""")).mappings().one()
    assert riga["head_version"] == 1
    assert riga["head_sha256"] == bl.BASELINE_CANONICAL_SHA256
    assert riga["mapper_version"] == projection.MAPPER_VERSION
    assert riga["schema_version"] == 1
    assert riga["has_manual"] is False
    assert riga["root_extra"] == {}
    assert riga["synchronised_at"] is not None


def test_la_proiezione_e_FEDELE_e_ATTUALE(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        esito = projection.verify(c)
    assert esito.faithful, esito.details
    assert esito.current
    assert esito.ok


def test_il_documento_riassemblato_da_SQL_e_la_baseline(inizializzata):
    """Il giro completo attraverso il database vero: JSONB, numeric, text[], date.

    La suite pura prova lo stesso invariante in memoria; questo lo prova dopo che
    PostgreSQL ha avuto la sua occasione di cambiare i valori.
    """
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        corrente = projection.current_document(c)
    assert corrente.version == 1
    assert corrente.sha256 == bl.BASELINE_CANONICAL_SHA256
    assert canonical_sha256(corrente.doc) == bl.BASELINE_CANONICAL_SHA256
    # E il documento riassemblato è IDENTICO all'artefatto sul disco, non solo di
    # digest uguale: è la forma che il frontend riceve.
    assert corrente.doc == bl.load(ARTEFATTO)


def test_i_rack_sono_TUTTI_vuoti_anche_nelle_tabelle(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_racks")).scalar() == 102
        assert c.execute(text("SELECT count(*) FROM inventory_devices")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM inventory_locations")).scalar() == 3
        assert c.execute(text("SELECT count(*) FROM inventory_rooms")).scalar() == 6
        assert c.execute(text(
            "SELECT count(*) FROM inventory_manual_entries")).scalar() == 0


def test_i_seriali_degli_armadi_sono_arrivati_nella_COLONNA(inizializzata):
    """`seriali` è `text[]`: se fosse finito in `extra`, la ricerca non lo
    troverebbe — e sono i numeri con cui si identifica un armadio a magazzino."""
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        totale = c.execute(text(
            "SELECT coalesce(sum(cardinality(seriali)), 0) FROM inventory_racks"
        )).scalar()
        in_extra = c.execute(text(
            "SELECT count(*) FROM inventory_racks WHERE extra ? 'seriali'")).scalar()
    assert totale == 18
    assert in_extra == 0


def test_un_solo_amministratore_e_deve_cambiare_la_password(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        righe = list(c.execute(text(
            "SELECT username, role, must_change_pw, disabled_at, password_hash "
            "FROM users")).mappings())
    assert len(righe) == 1
    u = righe[0]
    assert u["username"] == "admin"
    assert u["role"] == "admin"
    assert u["must_change_pw"] is True
    assert u["disabled_at"] is None
    assert u["password_hash"].startswith("$argon2id$")


def test_l_audit_ha_UNA_riga_e_nessuna_storia_di_sviluppo(inizializzata):
    """§13: la storia di produzione comincia qui.

    Una riga, quella del bootstrap dell'inventario, che è ciò che il contratto di
    bootstrap già scriveva. La creazione dell'amministratore non ne aggiunge una, e
    non è una dimenticanza: `bootstrap.py` si comporta allo stesso modo, e §13 chiede
    di restare coerenti col contratto esistente invece di inventarne un altro.
    """
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        righe = list(c.execute(text(
            "SELECT action, result, actor_username, actor_role, "
            "inventory_version, events FROM audit")).mappings())
    assert len(righe) == 1
    a = righe[0]
    assert a["action"] == "bootstrap"
    assert a["result"] == "success"
    assert a["actor_username"] == "admin"
    assert a["inventory_version"] == 1
    assert a["events"] == []


def test_tutto_il_resto_e_VUOTO(inizializzata):
    engine, _pw, _r = inizializzata
    for tabella in ("sessions", "photos", "inventory_photo_refs", "reminders",
                    "reminder_deliveries", "scheduler_runs", "maintenance_runs",
                    "login_attempts", "notification_test_attempts"):
        assert conta(engine, tabella) == 0, f"{tabella} non è vuota"


def test_il_worker_non_risulta_aver_MAI_girato(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        riga = c.execute(text(
            "SELECT state, last_run_date FROM worker_heartbeat")).mappings().one()
    assert riga["last_run_date"] is None
    assert riga["state"] == "starting"


def test_le_notifiche_partono_DISATTIVE(inizializzata):
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        dati = c.execute(text("SELECT data FROM settings WHERE id = 1")).scalar()
    if isinstance(dati, str):
        dati = json.loads(dati)
    n = dati["notifications"]
    assert n["enabled"] is False
    assert n["recipients"] == []


def test_la_readiness_e_VERDE(inizializzata):
    """Le quattro condizioni di §8.23, dalla rotta vera."""
    engine, _pw, _r = inizializzata
    from app.main import app
    from conftest import api_client
    with api_client(app) as c:
        r = c.get("/api/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"status": "ready", "database": "ok", "migrations": "ok",
                    "inventory": "ok", "projection": "ok"}


# ==================================================================
# 4. la password provvisoria
# ==================================================================

def test_la_provvisoria_e_lunga_e_compare_UNA_volta(inizializzata):
    _engine, pw, r = inizializzata
    assert len(pw) >= 32, "meno di 32 caratteri: non è la provvisoria da 192 bit"
    assert r.stdout.count(pw) == 1


def test_la_provvisoria_NON_e_nel_database(inizializzata):
    """La ricerca la si fa su TUTTE le colonne di testo delle tabelle interessate,
    non solo su quelle in cui verrebbe in mente di guardare."""
    engine, pw, _r = inizializzata
    with engine.begin() as c:
        for tabella, colonna in (("users", "password_hash"),
                                 ("users", "username"),
                                 ("audit", "audit::text"),
                                 ("inventory_versions", "doc::text"),
                                 ("settings", "data::text")):
            n = c.execute(
                text(f"SELECT count(*) FROM {tabella} WHERE {colonna} LIKE :p"),
                {"p": f"%{pw}%"}).scalar()
            assert n == 0, f"la provvisoria compare in {tabella}.{colonna}"


def test_la_provvisoria_non_e_nello_STDERR(inizializzata):
    """stderr è quello che finisce nei log di sistema quando si redirige."""
    _engine, pw, r = inizializzata
    assert pw not in r.stderr


def test_la_provvisoria_FUNZIONA_e_impone_il_cambio(inizializzata):
    """Una password che non entra sarebbe peggio di nessuna password: l'operatore
    non avrebbe modo di accorgersene se non provando."""
    engine, pw, _r = inizializzata
    from app.auth.service import login
    with engine.begin() as c:
        _token, utente = login(c, "admin", pw)
    assert utente.must_change_pw is True
    assert utente.role == "admin"


def test_due_esecuzioni_generano_provvisorie_DIVERSE(vergine):
    """CSPRNG, non un valore derivato da qualcosa."""
    prima = init("--allow-initialise")
    pw1 = estrai_password(prima.stdout)
    # si torna vergine a mano: la fixture è per test
    from app.settings.schema import DEFAULTS
    with vergine.begin() as c:
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM users"))
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb), version = 1 "
                       "WHERE id = 1"), {"d": json.dumps(DEFAULTS)})
    seconda = init("--allow-initialise")
    pw2 = estrai_password(seconda.stdout)
    assert pw1 != pw2


# ==================================================================
# 5. il secondo giro: rifiuta, non ricrea
# ==================================================================

def test_il_secondo_giro_RIFIUTA_con_il_codice_stabile(inizializzata):
    engine, _pw, _r = inizializzata
    r = init("--allow-initialise")
    assert r.returncode == 1
    assert "production_baseline_already_initialized" in r.stderr


def test_il_secondo_giro_non_crea_una_versione_2(inizializzata):
    engine, _pw, _r = inizializzata
    init("--allow-initialise")
    assert conta(engine, "inventory_versions") == 1
    with engine.begin() as c:
        assert c.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar() == 1


def test_il_secondo_giro_non_crea_un_secondo_amministratore(inizializzata):
    engine, _pw, _r = inizializzata
    init("--allow-initialise")
    assert conta(engine, "users") == 1


def test_il_secondo_giro_non_azzera_la_password(inizializzata):
    """Se la reimpostasse, un playbook rieseguito per distrazione taglierebbe fuori
    l'amministratore da una installazione in servizio."""
    engine, pw, _r = inizializzata
    with engine.begin() as c:
        hash_prima = c.execute(text("SELECT password_hash FROM users")).scalar()
    r = init("--allow-initialise")
    assert r.returncode == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT password_hash FROM users")).scalar() == hash_prima
    from app.auth.service import login
    with engine.begin() as c:
        login(c, "admin", pw)          # la vecchia funziona ancora


def test_il_secondo_giro_non_TOCCA_i_dati_dell_operatore(inizializzata):
    """Il caso vero: una installazione con del lavoro dentro."""
    engine, _pw, _r = inizializzata
    doc = bl.load(ARTEFATTO)
    rack = doc["locations"][0]["sale"][0]["racks"][0]
    rack["devices"] = [{"_uid": "66666666-6666-4666-8666-666666666666",
                        "id": "SRV-VERO", "name": "srv-vero", "u": 1, "h": 1}]
    from app.inventory import Actor, InventoryRepository
    with engine.begin() as c:
        repo = InventoryRepository(c)
        repo.save(repo.head_version(), doc, Actor(username="admin", role="admin"))

    r = init("--allow-initialise")
    assert r.returncode == 1
    assert conta(engine, "inventory_devices") == 1
    with engine.begin() as c:
        nomi = [x[0] for x in c.execute(text("SELECT name FROM inventory_devices"))]
    assert nomi == ["srv-vero"]


# ==================================================================
# 6. la prima modifica vera è la versione 2
# ==================================================================

def test_la_prima_modifica_e_la_versione_2(inizializzata):
    engine, _pw, _r = inizializzata
    doc = bl.load(ARTEFATTO)
    doc["locations"][0]["sale"][0]["racks"][0]["devices"] = [
        {"_uid": "77777777-7777-4777-8777-777777777777", "id": "SRV-01",
         "name": "srv-01", "u": 1, "h": 2, "garanzia": "2027-03-01"}]
    from app.inventory import Actor, InventoryRepository
    with engine.begin() as c:
        repo = InventoryRepository(c)
        esito = repo.save(repo.head_version(), doc,
                          Actor(username="admin", role="admin"))
    assert esito.version == 2

    with engine.begin() as c:
        assert c.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar() == 2
        # La storia è append-only: la versione 1 è ancora là, ed è la baseline.
        sha1 = c.execute(text("SELECT canonical_sha256 FROM inventory_versions "
                              "WHERE version = 1")).scalar()
        assert sha1 == bl.BASELINE_CANONICAL_SHA256
        esito_v = projection.verify(c)
    assert esito_v.ok
    assert conta(engine, "inventory_devices") == 1


def test_la_baseline_resta_nella_storia_dopo_la_modifica(inizializzata):
    """La versione 1 non si riscrive: è il punto da cui si può sempre confrontare."""
    engine, _pw, _r = inizializzata
    doc = bl.load(ARTEFATTO)
    doc["locations"][0]["nome"] = "Pomezia — G0 (rinominata)"
    from app.inventory import Actor, InventoryRepository
    with engine.begin() as c:
        repo = InventoryRepository(c)
        repo.save(repo.head_version(), doc, Actor(username="admin", role="admin"))
    with engine.begin() as c:
        v1 = c.execute(text("SELECT doc FROM inventory_versions WHERE version = 1")
                       ).scalar()
    if isinstance(v1, str):
        v1 = json.loads(v1)
    assert canonical_sha256(v1) == bl.BASELINE_CANONICAL_SHA256


# ==================================================================
# 7. i privilegi: chi NON può creare una baseline
# ==================================================================

# ⚠ `SET LOCAL ROLE`, non `SET ROLE`, e la differenza ha rotto questa suite una
# volta. `SET ROLE` vale per la SESSIONE: `engine.begin()` chiude la transazione ma
# non la connessione, che torna nel pool ancora impersonando `tsm_api`. Il test
# successivo la ripesca e la sua ripulitura muore con «permission denied for table
# reminders» — un fallimento che parla di una tabella che non c'entra niente, in un
# test che non ha fatto nulla di sbagliato. `LOCAL` finisce col commit.

@pytest.mark.parametrize("ruolo", ["tsm_api", "tsm_worker"])
def test_un_ruolo_di_runtime_non_puo_INSERIRE_la_testa(vergine, ruolo):
    """Il privilegio che rende impossibile una baseline dall'applicazione.

    Non è un controllo nel codice: è un `GRANT` che non c'è. Un difetto in una
    rotta non può aggirarlo, e nemmeno un'iniezione.
    """
    from sqlalchemy.exc import ProgrammingError
    with pytest.raises(ProgrammingError) as exc:
        with vergine.begin() as c:
            c.execute(text(f"SET LOCAL ROLE {ruolo}"))
            c.execute(text("INSERT INTO inventory_head (id, version) "
                           "VALUES (TRUE, 1)"))
    assert "permission denied" in str(exc.value).lower()


@pytest.mark.parametrize("ruolo,sql", [
    ("tsm_api", "DELETE FROM inventory_versions"),
    ("tsm_api", "UPDATE inventory_versions SET doc = '{}'::jsonb"),
    ("tsm_api", "DELETE FROM audit"),
    ("tsm_api", "DELETE FROM users"),
    ("tsm_api", "TRUNCATE inventory_racks"),
    ("tsm_worker", "DELETE FROM inventory_versions"),
    ("tsm_worker", "DELETE FROM audit"),
    ("tsm_worker", "DELETE FROM inventory_racks"),
    ("tsm_worker", "SELECT count(*) FROM users"),
])
def test_un_ruolo_di_runtime_non_puo_riscrivere_la_STORIA(vergine, ruolo, sql):
    from sqlalchemy.exc import ProgrammingError
    with pytest.raises(ProgrammingError):
        with vergine.begin() as c:
            c.execute(text(f"SET LOCAL ROLE {ruolo}"))
            c.execute(text(sql))


def test_tsm_api_PUO_fare_il_suo_lavoro(inizializzata):
    """La controprova dei test di sopra: un privilegio negato per eccesso
    romperebbe l'applicazione, e questi test non lo direbbero.

    `tsm_api` deve poter sostituire lo stato corrente — la proiezione si mantiene
    cancellando e riscrivendo (voce 15 del registro) — e appendere alla storia.
    """
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        c.execute(text("SET LOCAL ROLE tsm_api"))
        c.execute(text("SELECT count(*) FROM inventory_racks"))
        c.execute(text("DELETE FROM inventory_devices"))     # stato corrente: sì
        c.execute(text("SELECT version FROM inventory_head WHERE id IS TRUE"))


def test_il_comando_RIFIUTA_se_eseguito_da_un_ruolo_di_runtime(vergine):
    """Il cancello dei privilegi, visto dal comando.

    Il `GRANT` mancante lo fermerebbe comunque, a metà. Il cancello lo ferma PRIMA, con
    un messaggio che dice quale ruolo serve, e senza aver aperto una transazione di
    scrittura.

    ⚠ MUTAZIONE SFUGGITA al primo giro. La prima stesura accettava «un rifiuto
    qualsiasi» — `returncode != 0` — perché il ruolo di runtime potrebbe non avere una
    password in questo database di prova. Con il cancello disattivato il rifiuto
    arrivava comunque, dalla CONNESSIONE che non si apriva: il test era verde e non
    provava niente del cancello.

    Ora la password si imposta, la connessione riesce, e si pretende il CODICE. È la
    differenza fra «qualcosa è andato male» e «il cancello ha detto no».
    """
    from sqlalchemy import text as _text
    with vergine.begin() as c:
        # Come fa `scripts/migrate.py`: la password non sta in una migrazione.
        c.execute(_text("ALTER ROLE tsm_api WITH LOGIN PASSWORD 'prova-cancello'"))

    dsn = DSN.replace("//tsm:", "//tsm_api:").replace(
        ":testpw@", ":prova-cancello@")
    r = init("--allow-initialise", env_extra={"TSM_DB_URL": dsn})
    assert r.returncode != 0, r.stdout
    assert "production_baseline_insufficient_privilege" in (r.stdout + r.stderr), (
        f"atteso il codice del cancello dei privilegi, ottenuto:\n"
        f"{(r.stdout + r.stderr)[:600]}")
    assert conta(vergine, "inventory_versions") == 0
    assert conta(vergine, "users") == 0


def test_il_comando_RIFIUTA_una_revisione_di_schema_SBAGLIATA(vergine):
    """⚠ MUTAZIONE SFUGGITA al primo giro: il cancello delle migrazioni non era provato.

    Il test che c'era usava uno schema VUOTO, e su quello il cancello 0 (le tabelle
    esistono?) risponde prima. Il caso vero è diverso e più insidioso: tutte le tabelle
    ci sono, ma la revisione applicata non è quella che questo codice si aspetta —
    cioè un aggiornamento a metà, o un'immagine più nuova del database.
    """
    from sqlalchemy import text as _text
    with vergine.begin() as c:
        c.execute(_text("UPDATE alembic_version SET version_num = '0012_dual_write'"))
    try:
        r = init("--allow-initialise")
        assert r.returncode == 1
        assert "production_baseline_schema_mismatch" in r.stderr, r.stderr[:600]
        assert "0012_dual_write" in (r.stdout + r.stderr), (
            "il messaggio deve dire QUALE revisione ha trovato: senza, l'operatore "
            "non sa se aggiornare il codice o il database")
        assert conta(vergine, "inventory_versions") == 0
    finally:
        with vergine.begin() as c:
            # La testa vera, letta dai file: scritta a mano diventava falsa alla
            # migrazione successiva e lasciava il database a metà per i test dopo.
            from app.api.health import expected_head_revision
            c.execute(_text("UPDATE alembic_version SET version_num = :v"),
                      {"v": expected_head_revision()})


# ==================================================================
# 8. lo schema
# ==================================================================

def test_il_comando_rifiuta_uno_schema_NON_migrato(vergine, tmp_path):
    """Difetto vero, trovato eseguendo il comando contro un database vuoto: la
    prima interrogazione sollevava e l'operatore riceveva una traccia di stack.

    Si riproduce con un database che non ha nessuna tabella. Si usa uno SCHEMA
    dedicato e vuoto invece di cancellare quello vero — così il test non può
    distruggere il database su cui gira il resto della suite.
    """
    with vergine.begin() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS baseline_vuoto"))
    dsn = DSN + ("&" if "?" in DSN else "?") + \
        "options=-csearch_path%3Dbaseline_vuoto"
    r = esegui("--check", "--baseline", str(ARTEFATTO),
               env_extra={"TSM_DB_URL": dsn})
    assert r.returncode == 1
    testo = r.stdout + r.stderr
    assert "Traceback" not in testo, "una traccia di stack non è un messaggio"
    assert "migrazioni" in testo.lower()
    with vergine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS baseline_vuoto CASCADE"))


# ==================================================================
# 9. la struttura, riletta dalle tabelle
# ==================================================================

def test_la_struttura_nelle_tabelle_e_quella_del_manifesto(inizializzata):
    """Il manifesto che una persona rivede descrive le righe che ci sono davvero."""
    engine, _pw, _r = inizializzata
    with engine.begin() as c:
        siti = [dict(r) for r in c.execute(text(
            "SELECT nome, code FROM inventory_locations ORDER BY ordinal")).mappings()]
        sale = [dict(r) for r in c.execute(text(
            "SELECT nome, code FROM inventory_rooms ORDER BY ordinal")).mappings()]
    doc = bl.load(ARTEFATTO)
    attesi_siti = [{"nome": L["nome"], "code": L["id"]} for L in doc["locations"]]
    assert siti == attesi_siti
    attese_sale = [{"nome": e.obj["nome"], "code": e.obj["id"]}
                   for e in walk(doc) if e.kind == "room"]
    assert sorted(sale, key=lambda x: x["code"]) == \
        sorted(attese_sale, key=lambda x: x["code"])
    manifesto = (ROOT / "deploy" / "baseline" / "MANIFEST.md").read_text(
        encoding="utf-8")
    for s in attesi_siti:
        assert s["code"] in manifesto
