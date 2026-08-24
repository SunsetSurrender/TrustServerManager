"""La baseline di produzione: l'artefatto, i suoi invarianti, la sua indipendenza.

Suite PURA: nessun database. Quello che si verifica qui è il FILE — che sia una
baseline, che sia QUELLA baseline, e che non cambi da sola quando cambia il seed di
sviluppo. Lo stato del database dopo l'inizializzazione lo verifica
`test_baseline_pg.py`.

Quattro affermazioni, in ordine di importanza:

  1. l'artefatto passa le stesse regole di una scrittura normale di produzione, più
     quelle proprie di una baseline (zero dispositivi, zero foto, forma canonica,
     giro relazionale completo);
  2. il suo digest è quello APPUNTATO nel codice: cambiare la struttura di
     produzione è un atto che passa da una revisione, non un effetto collaterale;
  3. **la baseline non dipende dal seed**: modificare `fixtures/seed.json` non
     cambia l'artefatto, e i due digest non sono lo stesso numero;
  4. la struttura è quella del seed — nessun sito perso, nessun sito inventato —
     e i controlli che dovrebbero prenderla sbagliata la prendono davvero.

⚠ Il punto 4 vale la pena spiegarlo. Un test che dice «zero dispositivi» su un
documento con zero dispositivi passa anche se il contatore è rotto. Quindi ogni
invariante di questa suite ha la sua CONTROPROVA: si inietta il difetto e si
pretende che il controllo lo veda. Senza, questa suite dimostrerebbe che un file
generato è uguale a se stesso.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.identity import canonicalise
from app.identity.model import walk
from app.inventory import baseline as bl
from app.inventory.digest import canonical_sha256
from app.inventory.document import RACK_U_OUT_OF_RANGE, strip_legacy_fields

ROOT = Path(__file__).resolve().parents[2]
ARTEFATTO = ROOT / "deploy" / "baseline" / "production-baseline.json"
MANIFESTO = ROOT / "deploy" / "baseline" / "MANIFEST.md"
SEED = ROOT / "fixtures" / "seed.json"


def _load_generator():
    """Il generatore, importato per PERCORSO: il nome del file ha dei trattini.

    Serve a provare il CANCELLO dei campi ignoti (§3), che è codice del generatore
    e non dell'artefatto: l'artefatto di oggi non ha campi ignoti, quindi
    l'unico modo di verificare che il cancello funzioni è dargliene uno.
    """
    spec = importlib.util.spec_from_file_location(
        "tsm_make_production_baseline", ROOT / "tools" / "make-production-baseline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tsm_make_production_baseline"] = module
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()


@pytest.fixture(scope="module")
def doc() -> dict:
    return bl.load(ARTEFATTO)


@pytest.fixture(scope="module")
def seed() -> dict:
    return json.loads(SEED.read_text(encoding="utf-8"))


# ==================================================================
# 1. l'artefatto è una baseline
# ==================================================================

def test_l_artefatto_esiste_ed_e_json(doc):
    assert isinstance(doc, dict)
    assert set(doc) == {"schemaVersion", "locations"}, (
        "la radice ammette solo le chiavi dello schema congelato, e `manuale` "
        "resta ASSENTE: è la forma canonica del manuale vuoto")


def test_nessun_invariante_violato(doc):
    problemi = bl.invariants(doc)
    assert not problemi, "\n".join(f"[{p.code}] {p.path}: {p.message}"
                                   for p in problemi)


def test_e_gia_in_forma_canonica(doc):
    """Il file è ESATTAMENTE ciò che finisce nel database.

    Se non lo fosse, rivedere il file non sarebbe rivedere ciò che si installa: il
    digest appuntato apparterrebbe a un documento diverso da quello sul disco.
    """
    assert canonicalise(doc) == doc


def test_il_digest_e_quello_appuntato(doc):
    assert canonical_sha256(doc) == bl.BASELINE_CANONICAL_SHA256


def test_il_digest_appuntato_e_scritto_anche_nel_manifesto(doc):
    """Il manifesto è il documento che una persona legge. Se cita un digest vecchio,
    la revisione avviene su una struttura che non è quella installata."""
    assert MANIFESTO.is_file(), "manifesto assente: rigenerare"
    testo = MANIFESTO.read_text(encoding="utf-8")
    assert bl.BASELINE_CANONICAL_SHA256 in testo, (
        "il manifesto cita un digest diverso da quello appuntato: è stato "
        "modificato l'artefatto senza rigenerare il manifesto")


def test_il_file_e_serializzato_in_modo_deterministico(doc):
    """Rigenerare senza cambiare niente non deve produrre un diff.

    Un artefatto che si riscrive diverso a ogni giro rende illeggibile la revisione
    della modifica successiva: il diff sarebbe pieno di riordini.
    """
    atteso = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert ARTEFATTO.read_text(encoding="utf-8") == atteso


# ==================================================================
# 2. zero dispositivi, e i controlli lo vedrebbero
# ==================================================================

def test_zero_dispositivi_contati_camminando_l_albero(doc):
    assert bl.counts(doc)["devices"] == 0


def test_ogni_rack_e_vuoto(doc):
    vuoti = [e.path for e in walk(doc)
             if e.kind == "rack" and e.obj.get("devices")]
    assert not vuoti, f"rack non vuoti: {vuoti[:5]}"


def test_zero_riferimenti_a_foto(doc):
    from app.photos.refs import photo_ids
    assert photo_ids(doc) == []


def test_nessuna_voce_di_manuale(doc):
    assert bl.counts(doc)["manual"] == 0
    assert "manuale" not in doc


def test_nessuna_data_di_garanzia_o_supporto(doc):
    """Non ci sono dispositivi, quindi non ci sono date. Si verifica il RESIDUO."""
    testo = json.dumps(doc, ensure_ascii=False)
    for chiave in ("garanzia", "supporto", "presenza"):
        assert f'"{chiave}"' not in testo, (
            f"'{chiave}' compare nella baseline: è un campo di dispositivo")


# --- le controprove: i controlli prendono il difetto? ---

def test_un_dispositivo_nascosto_viene_PRESO(doc):
    """La controprova di `test_zero_dispositivi`. Senza di lei quel test passerebbe
    anche con un contatore rotto."""
    rotto = copy.deepcopy(doc)
    rack = rotto["locations"][0]["sale"][0]["racks"][0]
    rack["devices"] = [{"_uid": "11111111-1111-4111-8111-111111111111",
                        "id": "intruso", "name": "intruso", "u": 1, "h": 1}]
    assert bl.counts(rotto)["devices"] == 1
    codici = {p.code for p in bl.invariants(rotto)}
    assert bl.BASELINE_HAS_DEVICES in codici


def test_una_garanzia_superstite_viene_PRESA(doc):
    """Un residuo di dispositivo FUORI da `devices`: il contatore non lo vedrebbe.

    È il caso per cui `_device_leftovers` esiste separatamente da `counts`: le due
    domande hanno risposte diverse esattamente quando qualcosa è andato storto.
    """
    rotto = copy.deepcopy(doc)
    rotto["locations"][0]["sale"][0]["racks"][0]["garanzia"] = "2027-01-01"
    assert bl.counts(rotto)["devices"] == 0, "il contatore, da solo, non lo vede"
    codici = {p.code for p in bl.invariants(rotto)}
    assert bl.BASELINE_HAS_DEVICES in codici


def test_un_riferimento_a_foto_viene_PRESO(doc):
    rotto = copy.deepcopy(doc)
    rotto["locations"][0]["sale"][0]["racks"][0]["foto"] = \
        "22222222-2222-4222-8222-222222222222"
    codici = {p.code for p in bl.invariants(rotto)}
    assert bl.BASELINE_HAS_PHOTO_REFS in codici


def test_un_digest_diverso_viene_PRESO(doc):
    rotto = copy.deepcopy(doc)
    rotto["locations"][0]["nome"] = "Un altro nome"
    codici = {p.code for p in bl.invariants(rotto)}
    assert bl.BASELINE_DIGEST_MISMATCH in codici


def test_una_forma_non_canonica_viene_PRESA(doc):
    """Togliere un default materializzato: il documento resta VALIDO e non è più
    l'artefatto che si installa."""
    rotto = copy.deepcopy(doc)
    del rotto["locations"][0]["sale"][0]["racks"][0]["seriali"]
    codici = {p.code for p in bl.invariants(rotto)}
    assert bl.BASELINE_NOT_CANONICAL in codici


def test_un_rack_fuori_intervallo_viene_PRESO(doc):
    """La voce 16 del registro vale anche per la baseline: è lo stesso cancello."""
    rotto = copy.deepcopy(doc)
    rotto["locations"][0]["sale"][0]["racks"][0]["u"] = 3_000_000_000
    codici = {p.code for p in bl.invariants(rotto)}
    assert RACK_U_OUT_OF_RANGE in codici


def test_il_controllo_del_GIRO_RELAZIONALE_funziona(doc, monkeypatch):
    """⚠ MUTAZIONE SFUGGITA al primo giro: togliere il confronto del giro completo non
    rendeva rosso niente.

    E la ragione e' legittima: sul documento vero il giro torna sempre, perche' la
    rappresentabilita' e `validate_model` coprono i casi naturali. Quel controllo e' una
    difesa in profondita', e una difesa in profondita' non si prova cercando un input
    che la attivi — se ne esistesse uno facile, non sarebbe in profondita'.

    Si prova guastando cio' che verifica: si fa restituire ad `assemble` un documento
    diverso, e si pretende che il controllo lo veda. Prova la LOGICA del controllo, che
    e' l'unica cosa che si puo' provare di lui.
    """
    from app.inventory import baseline as modulo

    # ⚠ L'originale si cattura PRIMA di sostituirlo: dentro la funzione finta,
    # `modulo.assemble` sarebbe già la funzione finta, e la chiamata ricorrerebbe
    # all'infinito. Il primo tentativo lo faceva.
    assemble_vero = modulo.assemble

    def assemble_bugiardo(model):
        vero = assemble_vero(model)
        rotto = copy.deepcopy(vero)
        # Si perde una sala: il tipo di guasto che un errore nella mappa produrrebbe.
        rotto["locations"][0]["sale"].pop()
        return rotto

    monkeypatch.setattr(modulo, "assemble", assemble_bugiardo)
    codici = {p.code for p in bl.invariants(doc)}
    assert bl.BASELINE_ROUNDTRIP_FAILED in codici, (
        "il giro completo non torna e il controllo non se ne accorge: l'invariante "
        "di §5 del requisito non sarebbe verificata da nessuno")


def test_una_radice_legacy_viene_PRESA(doc):
    rotto = copy.deepcopy(doc)
    rotto["utenti"] = [{"email": "admin", "ruolo": "admin"}]
    codici = {p.code for p in bl.invariants(rotto)}
    assert "forbidden_root_key" in codici


# ==================================================================
# 3. la baseline NON dipende dal seed di sviluppo
# ==================================================================

def test_i_due_digest_sono_numeri_DIVERSI(doc, seed):
    """Se combaciassero, la baseline sarebbe il seed e non ci sarebbe niente da
    appuntare: è il controllo che rende non vacua l'affermazione «sono indipendenti».
    """
    digest_seed = canonical_sha256(strip_legacy_fields(copy.deepcopy(seed))[0])
    assert digest_seed != bl.BASELINE_CANONICAL_SHA256


def test_modificare_il_seed_non_cambia_l_artefatto(doc, seed):
    """L'artefatto è un FILE, non una vista sul seed.

    La prova è diretta: si modifica il seed in memoria, si rilegge l'artefatto dal
    disco, e deve essere identico. Se un domani qualcuno facesse dipendere la
    baseline dal seed a runtime, questo test resterebbe verde — quindi c'è anche
    quello sotto, che guarda il CODICE.
    """
    alterato = copy.deepcopy(seed)
    alterato["locations"][0]["nome"] = "SITO DI PROVA AGGIUNTO A MANO"
    assert bl.load(ARTEFATTO) == doc
    assert canonical_sha256(doc) == bl.BASELINE_CANONICAL_SHA256


def test_il_modulo_della_baseline_non_nomina_il_seed():
    """Il codice che l'API e il comando usano non deve nemmeno sapere che il seed
    esiste: la dipendenza sta nel generatore, che si esegue a mano."""
    sorgente = (ROOT / "backend" / "app" / "inventory" / "baseline.py").read_text(
        encoding="utf-8")
    # Si guardano le RIGHE DI CODICE, non i commenti: la testa del modulo parla del
    # seed di proposito, per spiegare perché non ne dipende.
    codice = [r for r in sorgente.splitlines()
              if r.strip() and not r.strip().startswith("#")]
    codice = "\n".join(codice).split('"""')
    # fuori dalle docstring: gli indici pari sono codice
    fuori = "".join(codice[::2])
    assert "seed" not in fuori.lower(), (
        "il modulo della baseline nomina il seed nel codice: la baseline "
        "tornerebbe a dipendere da un file di sviluppo")


# ==================================================================
# 4. la struttura è quella del seed: niente perso, niente inventato
# ==================================================================

def _struttura(d: dict) -> dict[str, list[tuple]]:
    """(uid, codice, nome) per tipo, in ordine di documento."""
    out: dict[str, list[tuple]] = {"location": [], "room": [], "rack": []}
    for e in walk(d):
        if e.kind in out:
            out[e.kind].append((e.uid, e.obj.get("id"),
                                e.obj.get("nome") or e.obj.get("name")))
    return out


def test_gli_uid_strutturali_sono_CONSERVATI(doc, seed):
    """§2: gli `_uid` non si rigenerano a ogni build.

    Non è un dettaglio: l'`_uid` è l'identità con cui l'audit, il diff e i
    riferimenti alle foto parlano di un rack. Rigenerarli farebbe di ogni
    reinstallazione un inventario di oggetti nuovi che si chiamano come i vecchi.
    """
    atteso = _struttura(seed)
    ottenuto = _struttura(doc)
    for tipo in ("location", "room", "rack"):
        assert [x[0] for x in ottenuto[tipo]] == [x[0] for x in atteso[tipo]], (
            f"gli uid dei {tipo} non combaciano con quelli del seed")


def test_nomi_codici_e_ordine_sono_CONSERVATI(doc, seed):
    assert _struttura(doc) == _struttura(seed)


def test_le_altezze_dei_rack_sono_CONSERVATE(doc, seed):
    da_seed = [e.obj.get("u") for e in walk(seed) if e.kind == "rack"]
    da_base = [e.obj.get("u") for e in walk(doc) if e.kind == "rack"]
    assert da_base == da_seed


def test_i_seriali_degli_ARMADI_sono_CONSERVATI(doc, seed):
    """§6: un seriale su un RACK non è un dato di dispositivo.

    Sono i numeri di inventario degli armadi — cespiti fisici con la targhetta. È
    esattamente l'errore contro cui il requisito mette in guardia, e toglierli
    avrebbe buttato metadato di produzione vero.
    """
    da_seed = {e.uid: tuple(e.obj.get("seriali") or [])
               for e in walk(seed) if e.kind == "rack"}
    da_base = {e.uid: tuple(e.obj.get("seriali") or [])
               for e in walk(doc) if e.kind == "rack"}
    assert da_base == da_seed
    assert sum(len(v) for v in da_base.values()) == 18, (
        "18 seriali di armadio nel seed: se il numero cambia, è cambiata la "
        "struttura e va rivisto il manifesto")


def test_la_geometria_delle_sale_e_CONSERVATA(doc, seed):
    """`vani`, porte, misure: è la planimetria, e serve agli operatori."""
    def geometria(d):
        return {e.uid: (e.obj.get("w"), e.obj.get("h"), e.obj.get("area"),
                        e.obj.get("dim"), json.dumps(e.obj.get("vani"),
                                                     sort_keys=True))
                for e in walk(d) if e.kind == "room"}
    # Il seed va canonicalizzato: `area`/`dim` assenti diventano stringhe vuote, e
    # confrontare un assente con una stringa vuota direbbe che la geometria è
    # cambiata quando non è cambiato niente.
    assert geometria(doc) == geometria(canonicalise(seed))


def test_il_flag_segnaposto_e_CONSERVATO(doc, seed):
    """`segnaposto` è una COLONNA tipizzata, non un campo di prova: dice che una
    planimetria è indicativa, e l'applicazione ci disegna un banner."""
    da_base = {e.uid: e.obj.get("segnaposto") for e in walk(doc) if e.kind == "room"}
    da_seed = {e.uid: e.obj.get("segnaposto")
               for e in walk(canonicalise(seed)) if e.kind == "room"}
    assert da_base == da_seed


# ==================================================================
# 5. il cancello dei campi ignoti (§3)
# ==================================================================

def test_oggi_non_ci_sono_campi_STRUTTURALI_ignoti(seed):
    """La classificazione di §3 su questi dati dà l'insieme vuoto.

    Vale la pena registrarlo come test e non come nota: il giorno che qualcuno
    aggiunge un campo alla struttura, questo diventa rosso e la decisione va presa.
    """
    doc, rimozioni, conteggi, _sospetti = GEN.build(copy.deepcopy(seed))
    strutturali = [r for r in rimozioni if "dispositivi" not in r and "radice" not in r]
    assert strutturali == [], (
        f"campi strutturali rimossi senza che nessuno lo sappia: {strutturali}")


def test_un_campo_ignoto_FERMA_il_generatore(seed):
    """Il cancello, provato dandogli un campo che non conosce.

    Senza questo test, «zero campi ignoti» dimostrerebbe soltanto che il seed non
    ne ha — non che il generatore li prenderebbe.
    """
    alterato = copy.deepcopy(seed)
    alterato["locations"][0]["sale"][0]["racks"][0]["colore_adesivo"] = "verde"
    with pytest.raises(GEN.Ignota) as exc:
        GEN.build(alterato)
    assert "colore_adesivo" in str(exc.value)
    assert "non si scarta in silenzio" in str(exc.value)


def test_un_foto_su_un_rack_viene_TOLTO_e_dichiarato(seed):
    """Non è «ignoto»: è conosciuto e non deve sopravvivere. La differenza fra le
    due categorie è che questa non ferma il generatore, ma finisce nell'elenco."""
    alterato = copy.deepcopy(seed)
    alterato["locations"][0]["sale"][0]["racks"][0]["foto"] = \
        "33333333-3333-4333-8333-333333333333"
    doc, rimozioni, _c, _s = GEN.build(alterato)
    assert any("foto" in r for r in rimozioni)
    from app.photos.refs import photo_ids
    assert photo_ids(doc) == []


def test_il_generatore_riproduce_ESATTAMENTE_l_artefatto_committato(seed, doc):
    """La prova che il file sul disco viene da questo codice e da questo seed.

    Se un domani qualcuno modificasse l'artefatto a mano — per aggiungere una sala
    «solo per adesso» — questo test lo prenderebbe.
    """
    generato, _r, _c, _s = GEN.build(copy.deepcopy(seed))
    assert generato == doc


# ==================================================================
# 6. i nomi da confermare (§19)
# ==================================================================

def test_i_nomi_sospetti_finiscono_nel_manifesto(seed):
    """Il rack «Test» esiste, ed è una domanda per una persona, non un difetto.

    Il generatore non lo toglie — in un CED vero un rack di collaudo è un rack vero
    — ma lo mette in una sezione del manifesto che va spuntata a mano. Questo test
    pretende che quel meccanismo funzioni: se domani sparisse, un rack di sviluppo
    rimasto dentro passerebbe senza che nessuno lo guardi.
    """
    _doc, _r, _c, sospetti = GEN.build(copy.deepcopy(seed))
    assert sospetti, "nessun nome segnalato: il rilevamento non funziona più"
    assert any("R09" in s for s in sospetti)
    testo = MANIFESTO.read_text(encoding="utf-8")
    assert "Da confermare" in testo
    assert "R09" in testo


def test_le_FILE_INTERROTTE_finiscono_nel_manifesto(seed):
    """L'anomalia che ha trovato la lettura a mano del manifesto.

    In Saletta Nuova i rack sono A×6, B×10, C×6, e poi SN-R23 è di nuovo «B» e SN-R24
    di nuovo «C»: l'ordine del documento e le etichette di fila non concordano. Sono
    due rack aggiunti dopo e accodati all'array, che è come cresce un inventario vero
    — e dai dati è indistinguibile da un «C» digitato «B», che §19 nomina fra le cose
    da prendere.

    Si segnala e non si tocca: correggerlo vorrebbe dire inventare struttura di
    produzione.
    """
    _doc, _r, _c, sospetti = GEN.build(copy.deepcopy(seed))
    file_rotte = [x for x in sospetti if x.startswith("FILA")]
    assert file_rotte, "il rilevamento delle file interrotte non funziona più"
    assert any("SN-R23" in x for x in file_rotte)
    # La sequenza per intero: a colpo d'occhio dice più di qualunque frase.
    assert any("AAAAAABBBBBBBBBBCCCCCCBC" in x for x in file_rotte)
    testo = MANIFESTO.read_text(encoding="utf-8")
    assert "SN-R23" in testo


def test_una_fila_RAGGRUPPATA_non_viene_segnalata(seed):
    """La controprova: senza di lei il rilevamento potrebbe segnalare tutto.

    Cinque sale su sei hanno le file raggruppate, compresi i tre armadi tecnici che
    hanno la fila VUOTA e stanno in fondo — la fila vuota è un gruppo come gli altri
    (§8.50), e non deve produrre un falso allarme.
    """
    _doc, _r, _c, sospetti = GEN.build(copy.deepcopy(seed))
    file_rotte = [x for x in sospetti if x.startswith("FILA")]
    sale_segnalate = {x.split("/")[1].split(" —")[0] for x in file_rotte}
    assert sale_segnalate == {"Saletta Nuova"}, (
        f"segnalate anche: {sale_segnalate - {'Saletta Nuova'}}. Le altre cinque "
        f"sale hanno le file raggruppate, e tre di loro hanno un armadio con la fila "
        f"VUOTA in fondo: se quello venisse segnalato, il rilevamento sarebbe rumore")


def test_una_fila_DIGITATA_MALE_viene_presa(seed):
    """Il caso che §19 nomina: un «rack typo»."""
    alterato = copy.deepcopy(seed)
    racks = alterato["locations"][1]["sale"][0]["racks"]     # sala H0, A×8 B×8 C×8
    racks[2]["row"] = "C"                                    # un «A» digitato «C»
    _doc, _r, _c, sospetti = GEN.build(alterato)
    assert any("FILA" in x and "H0" in x for x in sospetti), (
        f"un'etichetta di fila fuori posto non viene segnalata: {sospetti}")


def test_il_rilevamento_prende_un_sito_di_prova(seed):
    """La controprova: il caso che §19 nomina per primo."""
    alterato = copy.deepcopy(seed)
    alterato["locations"].append({
        "_uid": "44444444-4444-4444-8444-444444444444",
        "id": "sito-demo", "nome": "Sito DEMO da cancellare", "sale": [],
    })
    _doc, _r, _c, sospetti = GEN.build(alterato)
    assert any("Sito DEMO" in s for s in sospetti)
