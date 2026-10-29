"""Qualificazione al rilascio: il cammino COMPLETO degli avvisi, su dati di forma reale.

Chiude la voce 14 del registro (§8.48), che è l'ultima aperta e un **blocco al
rilascio**. La lacuna era precisa: il seed strutturale di produzione non contiene
nessuna scadenza, quindi la funzione più delicata del prodotto — gli avvisi di
rinnovo — non è mai stata percorsa dall'inizio alla fine su un inventario che
somigli a quello vero. Le suite del worker la esercitano su fixture costruite per
provare il worker; qui si esercita su uno scenario costruito per somigliare a una
sala.

Il cammino, tutto, in un test dietro l'altro:

    documento → proiezione SQL → selezione dei candidati → filtro sullo stato
    → composizione del digest → consegna → ritentativi → idempotenza

⚠ Questo NON tocca la baseline di produzione. Lo scenario si installa in un database
di prova con `bootstrap`, che è il percorso generico; il comando della baseline lo
RIFIUTA, e `test_baseline_pg.py` lo verifica. Le due cose vivono nello stesso
repository e non possono scambiarsi di posto.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import importlib.util
import json
import os
import smtplib
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app import domain
from app.db import read_snapshot
from app.identity.model import walk
from app.inventory import Actor, InventoryRepository, projection
from app.notifications import candidates
from app.notifications import worker as wk
from app.settings.schema import DEFAULTS

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


QUAL = _load("tsm_qualification_pg", "fixtures/qualification/build.py")

#: La data rispetto a cui lo scenario è costruito in questa suite. FISSA: il worker
#: riceve `now_utc` iniettato, quindi non serve l'orologio vero e un rosso resta
#: riproducibile fra sei mesi.
OGGI = QUAL.REFERENCE
DESTINATARI = ["ced@example.internal", "acquisti@example.internal"]
ADMIN = Actor(username="capo", role="admin")


def at(day: date, hour_utc: int = 7) -> datetime:
    """08:00 a Roma = 06:00 UTC d'estate; 07:00 UTC è comunque dopo l'ora dovuta."""
    return datetime(day.year, day.month, day.day, hour_utc, 0, tzinfo=timezone.utc)


# ==================================================================
# finto server di posta
# ==================================================================

class FakeSMTP:
    sent: list = []
    fail_with: Exception | None = None
    connections: int = 0

    def __init__(self, host, port, timeout=None, context=None):
        type(self).connections += 1
        if type(self).fail_with is not None:
            raise type(self).fail_with

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def ehlo(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        type(self).sent.append(msg)

    @classmethod
    def reset(cls):
        cls.sent = []
        cls.fail_with = None
        cls.connections = 0


@pytest.fixture
def smtp(monkeypatch):
    import app.notifications.smtp as mod
    from app.config import get_settings
    FakeSMTP.reset()
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", FakeSMTP)
    cfg = get_settings()
    monkeypatch.setattr(cfg, "smtp_host", "relay.interno", raising=False)
    monkeypatch.setattr(cfg, "smtp_sender", "ced@example.internal", raising=False)
    monkeypatch.setattr(cfg, "smtp_username", "", raising=False)
    monkeypatch.setattr(cfg, "smtp_tls_mode", "starttls", raising=False)
    yield FakeSMTP
    FakeSMTP.reset()


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
def scenario(engine):
    """Lo scenario di qualificazione installato come versione 1, notifiche accese."""
    doc = QUAL.build_inventory(OGGI)
    dati = json.loads(json.dumps(DEFAULTS))
    dati["notifications"].update({
        "enabled": True, "recipients": DESTINATARI,
        "warningDays": [QUAL.WINDOW], "timezone": "Europe/Rome",
        "schedule": {"hour": 8, "minute": 0},
    })
    with engine.begin() as c:
        c.execute(text("UPDATE reminders SET delivery_id = NULL"))
        for t in ("reminders", "reminder_deliveries", "scheduler_runs",
                  "notification_test_attempts", "login_attempts", "sessions",
                  "maintenance_runs"):
            c.execute(text(f"DELETE FROM {t}"))
        c.execute(text("TRUNCATE audit, inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("UPDATE settings SET updated_by = NULL WHERE id = 1"))
        c.execute(text("DELETE FROM users"))
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb), "
                       "version = version + 1 WHERE id = 1"),
                  {"d": json.dumps(dati)})
        c.execute(text("UPDATE worker_heartbeat SET state = 'starting', "
                       "last_run_date = NULL, detail = NULL"))
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(doc, ADMIN)
    return doc


def candidati(engine, *, today: date = OGGI, finestre=None):
    with read_snapshot() as conn:
        return candidates.due_items_from_projection(
            conn, today=today, warning_days=finestre or [QUAL.WINDOW])


def righe_promemoria(engine):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT entity_uid, expiry_kind, expiry_date, threshold_days, state, "
            "delivery_id FROM reminders ORDER BY expiry_date, entity_uid")).mappings()]


def consegne(engine):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, message_id, state, attempts, recipients_count, "
            "reminder_count, failure_category, next_attempt_after "
            "FROM reminder_deliveries ORDER BY id")).mappings()]


def corpi(smtp):
    return [m.get_body(("plain",)).get_content() for m in smtp.sent]


def _ripulito(valore: str) -> str:
    """Il valore come il corpo del digest lo contiene: senza caratteri di controllo.

    Non riusa la funzione di `digest.py` di proposito: confrontare l'uscita di una
    funzione con l'uscita della stessa funzione proverebbe che è d'accordo con sé.
    Qui la regola è riscritta — «via tutto ciò che non è stampabile su una riga» — e
    se le due divergono il test lo dice.
    """
    # I caratteri di controllo diventano uno SPAZIO, non sparono: togliendoli
    # `</b>Bcc:` si attaccherebbe, e il confronto cercherebbe una stringa che il
    # corpo non contiene. La prima stesura li toglieva, e il test era rosso su un
    # comportamento giusto.
    import re as _re
    return _re.sub(r"[\x00-\x1f\x7f]", " ", valore).strip()


# ==================================================================
# 1. la proiezione regge lo scenario
# ==================================================================

def test_lo_scenario_si_installa_come_versione_1(scenario, engine):
    with engine.begin() as c:
        assert c.execute(text(
            "SELECT version FROM inventory_head WHERE id IS TRUE")).scalar() == 1
        esito = projection.verify(c)
    assert esito.ok, esito.details


def test_le_date_finiscono_nelle_COLONNE_derivate(scenario, engine):
    """Le colonne data derivate sono ciò su cui la selezione interroga.

    Se una data valida restasse fuori, la selezione non la vedrebbe — e l'avviso
    non partirebbe senza che nessun errore lo dica.
    """
    with engine.begin() as c:
        righe = list(c.execute(text(
            "SELECT code, garanzia, garanzia_date, supporto, supporto_date "
            "FROM inventory_devices ORDER BY code")).mappings())
    per_codice = {r["code"]: r for r in righe}
    # Un caso con garanzia valida: testo E data derivata.
    valido = per_codice["SRV-CORE-01"]
    assert valido["garanzia"]
    assert valido["garanzia_date"] == OGGI + timedelta(days=12)
    # Il caso con la data ILLEGGIBILE: il testo resta, la data derivata è NULL.
    rotto = per_codice["SRV-BADDATE-01"]
    assert rotto["garanzia"] == "in attesa di conferma"
    assert rotto["garanzia_date"] is None
    assert rotto["supporto_date"] is None


def test_il_contesto_con_la_BARRA_arriva_intatto_nelle_tabelle(scenario, engine):
    with engine.begin() as c:
        nomi = [r[0] for r in c.execute(text("SELECT name FROM inventory_racks"))]
    assert QUAL.RACK_SLASH_NAME in nomi
    assert "/" in QUAL.RACK_SLASH_NAME


def test_il_contesto_UNICODE_arriva_intatto_nelle_tabelle(scenario, engine):
    with engine.begin() as c:
        nomi = [r[0] for r in c.execute(text("SELECT nome FROM inventory_rooms"))]
        owner = c.execute(text(
            "SELECT owner FROM inventory_devices WHERE code = 'SRV-UNI-01'")).scalar()
        note = c.execute(text(
            "SELECT note FROM inventory_devices WHERE code = 'SRV-UNI-01'")).scalar()
    assert QUAL.ROOM_B_NAME in nomi
    assert "Öztürk" in owner
    assert "在机架顶部" in note


def test_una_sala_con_vani_VUOTI_sopravvive_al_giro(scenario, engine):
    """Il difetto della fase 2H, in una tabella: `vani: []` deve restare `[]` e non
    diventare NULL o assente, perché è il default canonico e il digest lo conta."""
    with engine.begin() as c:
        vani = c.execute(text(
            "SELECT vani FROM inventory_rooms WHERE code = 'sala-b'")).scalar()
    assert vani == []


# ==================================================================
# 2. selezione dei candidati: SQL contro l'atteso DICHIARATO
# ==================================================================

def test_i_candidati_dallo_SQL_combaciano_con_l_atteso_dichiarato(scenario, engine):
    """Il test centrale della qualificazione.

    A sinistra ciò che la proiezione seleziona; a destra ciò che `CASES` dichiara,
    scritto a mano leggendo il requisito. Sono due strade indipendenti verso lo
    stesso numero, e se divergono una delle due ha torto.
    """
    trovati = Counter(i.entity_uid for i in candidati(engine).items)
    atteso = {u: n for u, n in QUAL.expected_notifiable(QUAL.WINDOW).items() if n}
    per_uid = {c.uid: c for c in QUAL.CASES}
    divergenze = {u: (atteso.get(u, 0), trovati.get(u, 0))
                  for u in set(atteso) | set(trovati)
                  if atteso.get(u, 0) != trovati.get(u, 0)}
    if divergenze:
        righe = "\n".join(
            f"  {per_uid[u].ident}: atteso {a}, dallo SQL {t}\n"
            f"      perché: {per_uid[u].why}"
            for u, (a, t) in divergenze.items())
        pytest.fail(f"{len(divergenze)} divergenze SQL/atteso:\n{righe}")


def test_lo_SQL_e_l_ORACOLO_puro_sono_d_accordo(scenario, engine):
    """La parità della fase 2F, misurata su questo scenario.

    `due_items(doc, …)` resta come seconda implementazione dello stesso contratto,
    scritta in un modo diverso e sopra una rappresentazione diversa. Su dati di forma
    reale le due devono dare la stessa risposta, chiave per chiave.
    """
    from app.notifications.expiry import due_items
    doc = QUAL.build_inventory(OGGI)
    da_sql = sorted((i.entity_uid, i.kind, i.expiry, i.days_remaining)
                    for i in candidati(engine).items)
    da_doc = sorted((i.entity_uid, i.kind, i.expiry, i.days_remaining)
                    for i in due_items(doc, today=OGGI,
                                       warning_days=[QUAL.WINDOW]))
    assert da_sql == da_doc


def test_un_DISMESSO_non_e_candidato(scenario, engine):
    """Il filtro sullo stato, sul lato SQL."""
    dismessi = {c.uid for c in QUAL.CASES if c.stato == "dismesso"}
    assert dismessi
    trovati = {i.entity_uid for i in candidati(engine).items}
    assert not (dismessi & trovati)


def test_un_dismesso_e_comunque_VISIBILE_nella_vista_Scadenze(scenario, engine):
    """Ispettivo contro azionabile, dalle due interrogazioni che li servono.

    Lo stesso dato: assente fra i candidati del worker, presente fra le righe della
    vista. È la distinzione della fase 2G, provata qui su dati di forma reale.
    """
    from app.inventory import queries
    with read_snapshot() as conn:
        pagina = queries.expiries(conn, today=OGGI, warning_days=3650, limit=200)
    visti = {r["device"]["uid"] for r in pagina.items}
    for c in QUAL.CASES:
        if c.stato == "dismesso":
            assert c.uid in visti, f"{c.ident} non compare in Scadenze"
    # ⚠ E la riga PORTA la spiegazione: `notifiable: false`. È l'informazione che
    # rende la distinzione leggibile senza che il client conosca la regola — un
    # dismesso compare, e dice di sé che non produce avvisi.
    per_uid = {r["device"]["uid"]: r for r in pagina.items}
    for c in QUAL.CASES:
        if c.stato == "dismesso":
            assert per_uid[c.uid]["notifiable"] is False


def test_uno_SCADUTO_e_visibile_e_non_e_candidato(scenario, engine):
    """La voce 10 del registro: nessun avviso retroattivo, e il dato non si perde."""
    from app.inventory import queries
    scaduti = {c.uid for c in QUAL.CASES
               if c.garanzia is not None and c.garanzia < 0}
    assert scaduti
    assert not (scaduti & {i.entity_uid for i in candidati(engine).items})
    with read_snapshot() as conn:
        pagina = queries.expiries(conn, today=OGGI, warning_days=3650, limit=200)
    livelli = {r["device"]["uid"]: r["level"] for r in pagina.items}
    notificabili = {r["device"]["uid"]: r["notifiable"] for r in pagina.items}
    for uid in scaduti:
        assert uid in livelli, "uno scaduto non compare nella vista"
        assert livelli[uid] == "expired"
        assert notificabili[uid] is False


def test_un_RIMOSSO_ancora_attivo_e_candidato(scenario, engine):
    """La presenza fisica non toglie l'idoneità: la garanzia scade comunque."""
    caso = next(c for c in QUAL.CASES
                if c.presenza == "rimosso" and c.stato == "attivo")
    assert caso.uid in {i.entity_uid for i in candidati(engine).items}


def test_un_rimosso_non_occupa_UNITA(scenario, engine):
    """L'altra metà della fase 2G, sulla stessa riga: non avvisa di meno e non
    occupa spazio. Si guarda la capacità dalla proiezione."""
    from app.inventory import queries
    rimossi = [c for c in QUAL.CASES if c.presenza == "rimosso"]
    assert rimossi, "nessun rimosso nello scenario"

    with read_snapshot() as conn:
        cap = queries.capacity(conn)
    dallo_sql = {r["uid"]: r["usedU"]
                 for L in cap.locations for room in L["rooms"]
                 for r in room["racks"]}

    # L'atteso dal DOMINIO puro, rack per rack: è la parità della fase 2G misurata su
    # dati di forma reale, non solo sui corpora.
    doc = QUAL.build_inventory(OGGI)
    atteso = {e.uid: domain.rack_capacity(e.obj.get("u"),
                                          e.obj.get("devices") or []).used_u
              for e in walk(doc) if e.kind == "rack"}
    assert dallo_sql == atteso

    # E la controprova: se i rimossi contassero, il numero sarebbe più alto. Senza
    # questa riga il confronto passerebbe anche in un mondo in cui nessuno è rimosso.
    con_i_rimossi = {}
    for e in walk(doc):
        if e.kind != "rack":
            continue
        finti = [dict(d, presenza="presente") for d in (e.obj.get("devices") or [])]
        con_i_rimossi[e.uid] = domain.rack_capacity(e.obj.get("u"), finti).used_u
    assert sum(con_i_rimossi.values()) > sum(atteso.values()), (
        "contare i rimossi non cambierebbe il totale: lo scenario non esercita la "
        "distinzione")


def test_una_data_ILLEGGIBILE_non_fa_cadere_il_giro(scenario, engine):
    """Un inventario reale ne contiene sempre qualcuna: «in attesa di conferma»
    scritto al posto di una data. Non deve far fallire la selezione."""
    esito = candidati(engine)
    assert esito.items                      # il giro ha prodotto qualcosa
    rotto = next(c for c in QUAL.CASES if c.ident == "SRV-BADDATE-01")
    assert rotto.uid not in {i.entity_uid for i in esito.items}


# ==================================================================
# 3. il digest, la consegna, i ritentativi
# ==================================================================

def test_un_giro_manda_UN_digest_con_tutte_le_scadenze(scenario, engine, smtp):
    esito = wk.run_once(engine, now_utc=at(OGGI))
    # ⚠ `sent` conta i PROMEMORIA, non i messaggi: un digest solo che ne porta
    # quattordici. È la forma del prodotto — un'email al giorno, non una per
    # scadenza — e vale la pena che il test dica entrambi i numeri.
    assert esito.due == QUAL.total_notifiable(QUAL.WINDOW), esito
    assert esito.sent == QUAL.total_notifiable(QUAL.WINDOW), esito
    assert len(smtp.sent) == 1, "un giro deve produrre UN messaggio"
    righe = righe_promemoria(engine)
    assert len(righe) == QUAL.total_notifiable(QUAL.WINDOW)
    assert all(r["state"] == "sent" for r in righe)
    c = consegne(engine)
    assert len(c) == 1
    assert c[0]["state"] == "sent"
    assert c[0]["recipients_count"] == len(DESTINATARI)
    assert c[0]["reminder_count"] == QUAL.total_notifiable(QUAL.WINDOW)


def test_il_digest_NOMINA_le_macchine_che_scadono(scenario, engine, smtp):
    wk.run_once(engine, now_utc=at(OGGI))
    testo = "\n".join(corpi(smtp))
    # ⚠ Il digest nomina il dispositivo per NOME e non per codice di business, ed è
    # la scelta giusta: chi legge l'avviso cerca la macchina come la chiama in sala.
    #
    # Si cerca il nome RIPULITO dai caratteri di controllo, perché è ciò che il corpo
    # contiene: `digest.py` li toglie prima di comporre. Due affermazioni in una: il
    # dispositivo compare — l'avviso non si perde — e il CRLF non c'è più.
    for c in QUAL.CASES:
        if c.notifiable:
            atteso = _ripulito(c.name)
            assert atteso in testo, f"{atteso!r} non compare nel digest"
    assert "\r" not in testo, "un ritorno a capo sopravvissuto nel corpo"
    # E la parte che conta del caso ostile: il tentativo di iniezione è nel corpo
    # come TESTO INNOCUO, su una riga sola, e non come intestazione. Le intestazioni
    # le verifica `test_un_CRLF_in_un_nome_non_aggiunge_destinatari`.
    ostile = next(c for c in QUAL.CASES if "\r\n" in c.name)
    assert "\r\nBcc:" not in testo
    assert _ripulito(ostile.name) in testo


def test_il_digest_NON_nomina_i_dismessi_ne_gli_scaduti(scenario, engine, smtp):
    wk.run_once(engine, now_utc=at(OGGI))
    testo = "\n".join(corpi(smtp))
    for c in QUAL.CASES:
        if c.notifiable == 0 and (c.stato == "dismesso" or
                                  (c.garanzia is not None and c.garanzia < 0)):
            # ⚠ Sul NOME ripulito, come il test positivo. Cercare il codice qui era
            # peggio che inutile: il codice non compare mai nel corpo, quindi
            # l'asserzione era vera per costruzione e avrebbe taciuto su un dismesso
            # finito nel digest.
            assert _ripulito(c.name) not in testo, (
                f"{c.name!r} compare nel digest: {c.why}")


def test_il_digest_porta_la_BARRA_e_l_UNICODE_intatti(scenario, engine, smtp):
    """Il contesto serve a chi legge l'avviso: «quale macchina, in quale rack».

    Una barra mangiata da una concatenazione o un accento rotto dalla codifica
    rendono l'avviso inutile proprio nel momento in cui serve.
    """
    wk.run_once(engine, now_utc=at(OGGI))
    testo = "\n".join(corpi(smtp))
    assert QUAL.RACK_SLASH_NAME in testo
    assert "Röntgen" in testo or "röntgen" in testo.lower()
    assert "µ" in testo or "srv-röntgen-01" in testo


def test_un_CRLF_in_un_nome_non_aggiunge_destinatari(scenario, engine, smtp):
    """Il caso ostile: un nome incollato che contiene `\\r\\nBcc:`.

    Si guardano le INTESTAZIONI del messaggio, non il corpo: nel corpo quel testo è
    innocuo, nelle intestazioni sarebbe un destinatario in più.
    """
    wk.run_once(engine, now_utc=at(OGGI))
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg.get("Bcc") is None
    assert msg.get("Cc") is None
    destinatari = [a.strip() for a in (msg.get("To") or "").split(",")]
    assert sorted(destinatari) == sorted(DESTINATARI)
    assert "altrove.example" not in (msg.get("To") or "")


def test_un_secondo_giro_lo_stesso_giorno_non_ripete(scenario, engine, smtp):
    """Idempotenza durevole: lo stato «già inviato» vive nel database."""
    wk.run_once(engine, now_utc=at(OGGI))
    esito = wk.run_once(engine, now_utc=at(OGGI, 9))
    assert esito.sent == 0
    assert len(smtp.sent) == 1
    assert len(consegne(engine)) == 1


def test_un_guasto_del_relay_lascia_la_consegna_da_RITENTARE(scenario, engine, smtp):
    smtp.fail_with = smtplib.SMTPConnectError(421, b"non ora")
    esito = wk.run_once(engine, now_utc=at(OGGI))
    assert esito.sent == 0
    c = consegne(engine)
    assert len(c) == 1
    assert c[0]["state"] == "pending"
    assert c[0]["attempts"] == 1
    assert c[0]["next_attempt_after"] is not None
    # I promemoria NON risultano inviati: sarebbe la perdita silenziosa di un avviso.
    assert all(r["state"] != "sent" for r in righe_promemoria(engine))


def test_il_ritentativo_manda_lo_STESSO_messaggio(scenario, engine, smtp):
    """Il `Message-ID` si conserva: un ritentativo non deve diventare un secondo
    avviso nella casella di chi legge."""
    smtp.fail_with = smtplib.SMTPConnectError(421, b"non ora")
    wk.run_once(engine, now_utc=at(OGGI))
    primo = consegne(engine)[0]

    smtp.fail_with = None
    # Oltre il momento del prossimo tentativo.
    wk.run_once(engine, now_utc=at(OGGI) + timedelta(hours=2))
    dopo = consegne(engine)
    assert len(dopo) == 1, "un ritentativo ha creato una seconda consegna"
    assert dopo[0]["message_id"] == primo["message_id"]
    assert dopo[0]["state"] == "sent"
    assert len(smtp.sent) == 1
    assert all(r["state"] == "sent" for r in righe_promemoria(engine))


def test_con_le_notifiche_SPENTE_non_parte_niente(scenario, engine, smtp):
    """§12: una installazione nuova parte spenta, e spenta non deve mandare nulla."""
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = jsonb_set("
                       "data, '{notifications,enabled}', 'false'), "
                       "version = version + 1 WHERE id = 1"))
    esito = wk.run_once(engine, now_utc=at(OGGI))
    assert esito.sent == 0
    assert not smtp.sent
    assert righe_promemoria(engine) == []


def test_l_audit_registra_il_digest(scenario, engine, smtp):
    wk.run_once(engine, now_utc=at(OGGI))
    with engine.begin() as c:
        azioni = [r[0] for r in c.execute(text(
            "SELECT action FROM audit WHERE action LIKE 'notifications%' "
            "ORDER BY id"))]
    assert azioni, "il digest inviato non compare nel registro"


# ==================================================================
# 4. la guardia sulla revisione
# ==================================================================

def test_una_proiezione_NON_attuale_ferma_la_selezione(scenario, engine):
    """§8.47: nessun ripiego sull'istantanea JSON.

    Un ripiego funzionerebbe, nessuno aprirebbe un ticket, e coprirebbe
    esattamente il difetto di coerenza che la fase 2 esiste per scoprire.
    """
    from app.inventory.errors import ProjectionNotCurrentError
    with engine.begin() as c:
        # Una mappa VECCHIA, non uno zero: `mapper_version` ha un CHECK
        # (`NULL OR > 0`), e soprattutto la mappa 1 è il caso reale — è ciò che si
        # trova dopo un aggiornamento a cui è mancato `project.py --rebuild`.
        c.execute(text("UPDATE inventory_projection_state SET mapper_version = 1"))
    with pytest.raises(ProjectionNotCurrentError):
        candidati(engine)


def test_e_il_worker_non_manda_niente_in_quello_stato(scenario, engine, smtp):
    with engine.begin() as c:
        c.execute(text("UPDATE inventory_projection_state SET mapper_version = 1"))
    esito = wk.run_once(engine, now_utc=at(OGGI))
    assert esito.sent == 0
    assert not smtp.sent
