"""La fixture di qualificazione delle notifiche: i casi, e la sua separazione.

Chiude la parte VERIFICABILE SENZA DATABASE della voce 14 del registro (§8.48), che
è l'ultima voce aperta e un blocco al rilascio: il seed strutturale di produzione non
ha nessuna scadenza, quindi il cammino degli avvisi non è mai stato percorso su dati
di forma reale. Qui si verifica che lo scenario contenga davvero i casi che dice, e
che l'atteso DICHIARATO combaci con ciò che il dominio calcola.

Il cammino end-to-end — proiezione, candidati, digest, consegna, ritentativi — sta in
`test_qualification_pg.py`, che ha bisogno di PostgreSQL.

⚠ Due affermazioni distinte, e conviene tenerle separate:

  1. **la fixture è quella che dice di essere.** Ogni caso di §15 del requisito c'è,
     compare una volta sola, e l'atteso scritto a mano in `CASES` è quello che il
     dominio produce. Se divergono, una delle due ha torto — e quella è
     l'informazione che serve;
  2. **la fixture non può diventare la baseline.** Tre barriere indipendenti, e ognuna
     provata: il digest appuntato, il conteggio dei dispositivi, il nome del sito.
     Una separazione che nessuno prova è una convenzione, e le convenzioni si
     dimenticano il giorno del rilascio.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from app import domain
from app.identity.model import walk
from app.inventory import baseline as bl
from app.inventory.digest import canonical_sha256
from app.inventory.document import validate_normal_document
from app.notifications.expiry import due_items

ROOT = Path(__file__).resolve().parents[2]
JSON_COMMITTATO = ROOT / "fixtures" / "qualification" / "notification-scenario.json"


def _load(name: str, relative: str):
    """Per PERCORSO, con un nome proprio: i generatori si chiamano tutti `build.py`
    e `sys.modules` è condiviso da tutta la sessione di pytest."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


QUAL = _load("tsm_fixture_qualification", "fixtures/qualification/build.py")


@pytest.fixture(scope="module")
def doc() -> dict:
    return QUAL.build_inventory(QUAL.REFERENCE)


# ==================================================================
# 1. lo scenario è quello che dice di essere
# ==================================================================

def test_ogni_caso_dichiarato_e_PIAZZATO_una_volta_sola(doc):
    """Un caso dichiarato in `CASES` e non messo in nessun rack non partecipa a
    niente, e la suite resterebbe verde: sarebbe un caso di prova che non prova.
    """
    piazzati = Counter(e.uid for e in walk(doc) if e.kind == "device")
    dichiarati = {c.uid for c in QUAL.CASES}
    assert set(piazzati) == dichiarati, (
        f"non piazzati: {sorted(dichiarati - set(piazzati))}; "
        f"piazzati e non dichiarati: {sorted(set(piazzati) - dichiarati)}")
    doppi = {u: n for u, n in piazzati.items() if n > 1}
    assert not doppi, f"piazzati più volte: {doppi}"


def test_ogni_caso_ha_scritto_PERCHE_esiste():
    """Un caso senza motivazione è un caso che nessuno può giudicare sbagliato."""
    muti = [c.ident for c in QUAL.CASES if not c.why.strip()]
    assert not muti, f"casi senza `why`: {muti}"


@pytest.mark.parametrize("descrizione,predicato", [
    ("attivo con garanzia in finestra",
     lambda c: c.stato == "attivo" and c.garanzia is not None
     and 0 <= c.garanzia <= QUAL.WINDOW and c.notifiable >= 1),
    ("manutenzione con supporto valido",
     lambda c: c.stato == "manutenzione" and c.supporto is not None
     and c.notifiable >= 1),
    ("dismissione con scadenza idonea",
     lambda c: c.stato == "dismissione" and c.notifiable >= 1),
    ("dismesso con scadenza FUTURA: visibile e non notificabile",
     lambda c: c.stato == "dismesso" and c.garanzia is not None
     and c.garanzia > 0 and c.notifiable == 0),
    ("già scaduto",
     lambda c: c.garanzia is not None and c.garanzia < 0),
    ("fuori dalla finestra",
     lambda c: c.garanzia is not None and c.garanzia > QUAL.WINDOW),
    ("garanzia E supporto insieme",
     lambda c: c.garanzia is not None and c.supporto is not None),
    ("id di business duplicato con _uid distinti",
     lambda c: sum(1 for o in QUAL.CASES if o.ident == c.ident) > 1),
    ("valore Unicode nel contesto",
     lambda c: any(ord(ch) > 127 for ch in
                   c.name + json.dumps(c.extra, ensure_ascii=False))),
    ("dispositivo fisicamente rimosso",
     lambda c: c.presenza == "rimosso"),
    ("data non interpretabile",
     lambda c: isinstance(c.extra.get("garanzia"), str)
     and c.extra.get("garanzia") not in (None, "")
     and not c.extra["garanzia"][:4].isdigit()),
])
def test_il_caso_richiesto_da_15_e_PRESENTE(descrizione, predicato):
    """Ogni riga dell'elenco di §15 del requisito, verificata una per una.

    Elencati come parametri e non come un `assert` cumulativo: se uno manca, il
    nome del test rosso dice QUALE.
    """
    assert any(predicato(c) for c in QUAL.CASES), f"caso assente: {descrizione}"


def test_il_contesto_con_la_BARRA_esiste_e_arriva_al_dispositivo(doc):
    """La barra è il carattere con cui si costruiscono i percorsi: una
    concatenazione sbagliata la rende ambigua, e lo si vede solo se c'è."""
    rack = next(e for e in walk(doc) if e.kind == "rack" and "/" in (e.obj.get("name") or ""))
    assert rack.obj["devices"], "il rack con la barra non contiene nessun dispositivo"


def test_c_e_una_sala_con_vani_VUOTI(doc):
    """`vani: []` è il default canonico, ed è la forma che nella fase 2H faceva
    morire il render di tutta l'applicazione. Deve stare in ogni scenario."""
    assert any(e.obj.get("vani") == [] for e in walk(doc) if e.kind == "room")


# ==================================================================
# 2. l'atteso dichiarato combacia con ciò che il dominio calcola
# ==================================================================

def test_l_atteso_DICHIARATO_combacia_col_dominio(doc):
    """Il cuore della qualificazione.

    `CASES` dichiara a mano quante righe di avviso deve produrre ogni dispositivo;
    `due_items` le calcola. Se le due cose divergono, o la fixture descrive male il
    prodotto o il prodotto fa una cosa diversa da quella descritta — e in entrambi i
    casi si vuole saperlo prima del rilascio, non dopo.

    Non è circolare: l'atteso è stato scritto leggendo il requisito, non eseguendo
    il codice.
    """
    trovati = Counter(i.entity_uid for i in due_items(
        doc, today=QUAL.REFERENCE, warning_days=[QUAL.WINDOW]))
    atteso = QUAL.expected_notifiable(QUAL.WINDOW)
    divergenze = {uid: (atteso[uid], trovati.get(uid, 0))
                  for uid in atteso if atteso[uid] != trovati.get(uid, 0)}
    if divergenze:
        per_uid = {c.uid: c for c in QUAL.CASES}
        righe = "\n".join(
            f"  {per_uid[u].ident} ({per_uid[u].name!r}): atteso {a}, calcolato {t}"
            f"\n      perché: {per_uid[u].why}"
            for u, (a, t) in divergenze.items())
        pytest.fail(f"{len(divergenze)} divergenze fra l'atteso e il dominio:\n{righe}")


def test_il_totale_delle_righe_di_avviso(doc):
    """Il numero che l'anteprima delle impostazioni mostra all'amministratore."""
    calcolato = len(due_items(doc, today=QUAL.REFERENCE,
                             warning_days=[QUAL.WINDOW]))
    assert calcolato == QUAL.total_notifiable(QUAL.WINDOW)
    assert calcolato > 0, "uno scenario che non avvisa nessuno non qualifica niente"


def test_un_dismesso_e_VISIBILE_ma_non_notificabile(doc):
    """Le due affermazioni della voce 10 del registro, sullo stesso dato."""
    dismessi = [e for e in walk(doc) if e.kind == "device"
                and domain.stato_of(e.obj) == "dismesso"]
    assert dismessi, "nessun dismesso nello scenario"
    for e in dismessi:
        assert not domain.notifies(e.obj)
        # «Visibile» = ha una data che la vista Scadenze interpreta. Che la vista la
        # mostri lo prova il test del browser; qui si prova che il dato c'è.
        assert e.obj.get("garanzia") or e.obj.get("supporto")


def test_uno_scaduto_non_produce_avvisi_RETROATTIVI(doc):
    scaduti = [c for c in QUAL.CASES if c.garanzia is not None and c.garanzia < 0]
    assert scaduti
    for c in scaduti:
        assert c.notifiable == 0
    trovati = {i.entity_uid for i in due_items(doc, today=QUAL.REFERENCE,
                                       warning_days=[QUAL.WINDOW])}
    assert not ({c.uid for c in scaduti} & trovati)


def test_un_rimosso_ancora_attivo_AVVISA_comunque(doc):
    """§7 della fase 2G: la presenza fisica non toglie l'idoneità agli avvisi.

    Un apparato portato in un altro sito e non ancora registrato altrove ha la
    garanzia che scade lo stesso, e chi la rinnova ha bisogno di saperlo.
    """
    caso = next(c for c in QUAL.CASES
                if c.presenza == "rimosso" and c.stato == "attivo")
    assert caso.notifiable >= 1
    trovati = {i.entity_uid for i in due_items(doc, today=QUAL.REFERENCE,
                                       warning_days=[QUAL.WINDOW])}
    assert caso.uid in trovati


# ==================================================================
# 3. determinismo delle date
# ==================================================================

def test_due_costruzioni_con_la_STESSA_data_sono_identiche():
    a = QUAL.build_inventory(date(2026, 9, 1))
    b = QUAL.build_inventory(date(2026, 9, 1))
    assert a == b
    assert canonical_sha256(a) == canonical_sha256(b)


def test_due_costruzioni_con_date_DIVERSE_differiscono():
    """Se non differissero, le date non sarebbero relative alla data di riferimento
    e lo scenario proverebbe la finestra sbagliata al secondo giorno."""
    a = QUAL.build_inventory(date(2026, 9, 1))
    b = QUAL.build_inventory(date(2026, 10, 1))
    assert a != b


def test_lo_scenario_non_legge_MAI_l_orologio():
    """`date.today()` dentro la fixture renderebbe un rosso non riproducibile.

    Si guarda il codice e non il comportamento: un `today()` in un ramo raro
    passerebbe qualunque prova a campione.
    """
    sorgente = (ROOT / "fixtures" / "qualification" / "build.py").read_text(
        encoding="utf-8")
    # ⚠ Si tolgono le DOCSTRING oltre ai commenti, ed è la stessa trappola dei
    # controlli statici della fase 2G: la testa del generatore SPIEGA perché
    # `date.today()` non si usa, e quella spiegazione contiene la stringa cercata.
    # Un controllo che combacia con la propria prosa è rosso quando il codice è
    # giusto — e diventa verde il giorno che qualcuno riscrive il commento.
    import ast
    albero = ast.parse(sorgente)
    for nodo in ast.walk(albero):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(nodo):
            nodo.body = nodo.body[1:] or [ast.Pass()]
    codice = ast.unparse(albero)

    assert "date.today()" not in codice, (
        "il generatore legge l'orologio: un rosso non sarebbe riproducibile")
    assert "datetime.now" not in codice


def test_il_json_committato_e_quello_della_data_di_riferimento(doc):
    """Il file su disco serve alla revisione: se non corrisponde, chi lo legge sta
    guardando uno scenario diverso da quello che i test eseguono."""
    assert JSON_COMMITTATO.is_file(), (
        "JSON assente: rigenerare con `python fixtures/qualification/build.py`")
    su_disco = json.loads(JSON_COMMITTATO.read_text(encoding="utf-8"))
    assert su_disco == doc


# ==================================================================
# 4. NON può diventare la baseline di produzione
# ==================================================================

def test_e_un_documento_VALIDO_di_produzione(doc):
    """Deve essere installabile: una qualificazione su un documento che il `PUT`
    rifiuterebbe non qualificherebbe il cammino vero."""
    errori = validate_normal_document(doc)
    assert not errori, "\n".join(f"[{e.code}] {e.path}: {e.message}" for e in errori)


def test_barriera_1_il_DIGEST_non_e_quello_appuntato(doc):
    assert canonical_sha256(doc) != bl.BASELINE_CANONICAL_SHA256


def test_barriera_2_HA_dispositivi(doc):
    assert bl.counts(doc)["devices"] == len(QUAL.CASES) > 0


def test_barriera_3_il_nome_del_sito_e_RICONOSCIBILE(doc):
    nome = doc["locations"][0]["nome"]
    assert "QUALIFICAZIONE" in nome
    assert "prova" in nome.lower(), (
        "il nome deve contenere una parola che il generatore della baseline "
        "segnala: è la terza barriera, e serve nel caso in cui qualcuno provasse "
        "a generare una baseline da questo documento")


def test_gli_invarianti_della_baseline_la_RIFIUTANO(doc):
    """Le tre barriere, viste dal cancello che le applica.

    È il test che conta di più di questa sezione: non «il documento è diverso», ma
    «il comando che installa dice no».
    """
    codici = {p.code for p in bl.invariants(doc)}
    assert bl.BASELINE_HAS_DEVICES in codici
    assert bl.BASELINE_DIGEST_MISMATCH in codici


def test_gli_UID_non_si_sovrappongono_a_quelli_della_baseline(doc):
    """Se un uid comparisse in entrambi, un documento mescolato ai due sarebbe
    ambiguo — e l'audit racconterebbe la storia di due oggetti diversi come se
    fosse quella di uno."""
    della_baseline = {e.uid for e in walk(bl.load()) if e.uid}
    della_fixture = {e.uid for e in walk(doc) if e.uid}
    assert not (della_baseline & della_fixture)


def test_la_struttura_e_di_FORMA_reale(doc):
    """§15 chiede rack e sale «di forma reale». Non è estetica: una qualificazione
    su una sala 10×10 senza porte non esercita il render della planimetria, e una
    su un rack senza altezza non esercita la capacità."""
    sale = [e.obj for e in walk(doc) if e.kind == "room"]
    assert any(r.get("vani") for r in sale), "nessuna sala con geometria"
    assert any(v.get("porta") for r in sale for v in (r.get("vani") or [])), \
        "nessuna porta: la planimetria non viene esercitata"
    altezze = {e.obj.get("u") for e in walk(doc) if e.kind == "rack"}
    assert 45 in altezze, "nessun rack dell'altezza standard"
    assert len(altezze) > 1, (
        "tutti i rack della stessa altezza: la capacità di un armadio basso è un "
        "caso diverso, e nella struttura vera esiste (Armadio UPS, 12 U)")
