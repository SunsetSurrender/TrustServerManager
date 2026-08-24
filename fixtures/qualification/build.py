#!/usr/bin/env python3
"""Fixture di QUALIFICAZIONE delle notifiche: forma di produzione, dati di prova.

Perché esiste
-------------
La voce 14 del registro del debito semantico (§8.48) è un **blocco al rilascio**: il
seed strutturale di produzione non contiene nessuna scadenza, quindi la funzione più
delicata del prodotto — gli avvisi di rinnovo — non è mai stata esercitata su dati di
forma reale lungo il cammino completo. Questa fixture è ciò su cui esercitarla.

⚠ **Non è la baseline di produzione, e non deve poterlo diventare.**
`deploy/baseline/production-baseline.json` è la struttura vera con i rack VUOTI;
questa è una struttura di forma reale con dei dispositivi FINTI dentro. Le due cose
si assomigliano di proposito — una qualificazione su dati di forma diversa non
qualificherebbe niente — e per questo la separazione non è affidata alla memoria di
chi lancia un comando:

  1. il comando della baseline accetta solo il documento il cui digest canonico è
     quello APPUNTATO in `app/inventory/baseline.py`. Questo documento ha un digest
     diverso, quindi `--init` lo rifiuta;
  2. lo stesso comando pretende zero dispositivi, e qui ce ne sono;
  3. il sito si chiama «QUALIFICAZIONE (dati di prova)», quindi il generatore della
     baseline lo segnalerebbe fra i nomi da confermare;
  4. `test_qualification_fixture.py` verifica tutti e tre, perché una separazione che
     nessuno prova è una convenzione.

Le date
-------
Calcolate rispetto a una data di riferimento ESPLICITA, passata dal chiamante. Mai
`date.today()` dentro la fixture: una fixture che legge l'orologio al momento
dell'import produce un documento diverso a ogni esecuzione, e allora un test rosso
non si può riprodurre. `REFERENCE` è la data del JSON committato, che serve alla
revisione; il cammino end-to-end genera con `reference = oggi`, perché il worker
calcola le finestre dall'orologio vero.

I casi
------
Ognuno dei casi che §15 del requisito nomina, più quelli che il resto della fase ha
reso interessanti. L'atteso NON si ricalcola nei test: sta scritto in `CASES`, che è
il punto. Un test che ricalcola l'atteso con la stessa funzione che verifica
dimostra che la funzione è d'accordo con se stessa.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta

CURRENT_SCHEMA_VERSION = 1

#: Data del JSON committato. Non è «oggi» e non deve esserlo: è la data rispetto a
#: cui il file su disco è leggibile da una persona.
REFERENCE = date(2026, 9, 1)

#: Finestra di avviso su cui è tarato l'atteso di `CASES`. È quella predefinita
#: delle impostazioni (`warningDays: [30]`).
WINDOW = 30

# ---------------------------------------------------------------- identità
#
# UID fissi e riconoscibili: `9999` in testa. Non sono quelli della baseline — che
# sono UUID veri, generati dalla migrazione degli `_uid` — così un documento mescolato
# ai due si riconosce a occhio, in un log come in una tabella.
LOC = "99999999-0000-4000-8000-00000000000a"
ROOM_A = "99999999-0000-4000-8000-00000000000b"
ROOM_B = "99999999-0000-4000-8000-00000000000c"
RACK_A = "99999999-0000-4000-8000-0000000000a1"
RACK_B = "99999999-0000-4000-8000-0000000000a2"
RACK_SLASH = "99999999-0000-4000-8000-0000000000a3"
RACK_UPS = "99999999-0000-4000-8000-0000000000a4"

#: Il nome del sito, che rende impossibile confondere questo documento con la
#: produzione guardando qualunque schermata. Contiene «prova» di proposito: il
#: generatore della baseline segnala quella sottostringa, quindi se un giorno
#: qualcuno provasse a generare una baseline da qui, il manifesto lo direbbe.
SITE_NAME = "QUALIFICAZIONE (dati di prova) — NON è produzione"

#: Contesto con una barra: il rack si chiama così, e la barra deve sopravvivere
#: intatta dal documento alla riga del digest via SQL. È il carattere con cui si
#: costruiscono i percorsi, quindi è il carattere che una concatenazione sbagliata
#: rende ambiguo.
RACK_SLASH_NAME = "Rack RO4495 / B — doppio"

#: Contesto Unicode: em dash, lettere accentate, un grado e un segno di spunta. Non è
#: folklore — «Röntgen» e «°C» stanno nei nomi delle sale di un CED vero, e una
#: codifica sbagliata nel digest della posta si vede solo su caratteri come questi.
ROOM_B_NAME = "Sala Röntgen — 21 °C ✓"


@dataclass(frozen=True)
class Case:
    """Un dispositivo e l'esito ATTESO, dichiarato e non ricalcolato."""
    uid: str
    ident: str
    name: str
    #: offset in giorni rispetto alla data di riferimento, o None
    garanzia: int | None = None
    supporto: int | None = None
    stato: str = "attivo"
    presenza: str = "presente"
    #: quante righe di avviso deve produrre il worker con finestra WINDOW
    notifiable: int = 0
    #: compare nella vista Scadenze? (ispettiva: mostra anche scaduti e dismessi)
    visible: bool = True
    #: perché questo caso esiste
    why: str = ""
    extra: dict = field(default_factory=dict)

    #: Altezza in unità rack. Due dei casi sono alti 2 U come un server vero: un
    #: inventario in cui tutto è alto 1 U non esercita né la sovrapposizione né il
    #: blocco contiguo più ampio.
    h: int = 1

    def device(self, ref: date, slot: int) -> dict:
        """Il dispositivo, posizionato allo slot `slot` del suo rack.

        ⚠ La posizione la decide il rack e non il caso, e ogni dispositivo ne ha una
        DIVERSA. La prima stesura scriveva `"u": 1` per tutti: otto apparati nello
        stesso rack occupavano lo stesso slot, la capacità di ogni rack risultava 1 U,
        e lo scenario non esercitava la capacità che diceva di esercitare. L'ha preso
        la controprova del test — rendere «presenti» i rimossi non cambiava il totale.
        """
        def d(offset: int | None) -> str:
            return "" if offset is None else (ref + timedelta(days=offset)).isoformat()
        dev = {"_uid": self.uid, "id": self.ident, "name": self.name,
               "u": slot, "h": self.h, "stato": self.stato,
               "presenza": self.presenza,
               "garanzia": d(self.garanzia), "supporto": d(self.supporto)}
        dev.update(self.extra)
        return dev


# ==================================================================
# i casi, uno per riga di §15 del requisito
# ==================================================================
CASES: tuple[Case, ...] = (
    # --- 1. attivo + garanzia valida DENTRO la finestra ---
    Case("99999999-1000-4000-8000-000000000001", "SRV-CORE-01",
         "srv-core-01", garanzia=12, notifiable=1, h=2,
         why="attivo con garanzia in finestra: il caso normale, deve avvisare"),
    # Il bordo esatto della finestra: `<=` e non `<`.
    Case("99999999-1000-4000-8000-000000000002", "SRV-CORE-02",
         "srv-core-02", garanzia=WINDOW, notifiable=1,
         why="esattamente al bordo della finestra: incluso"),
    # Oggi: zero giorni residui è dentro, e non è ovvio.
    Case("99999999-1000-4000-8000-000000000003", "SRV-CORE-03",
         "srv-core-03", garanzia=0, notifiable=1,
         why="scade oggi: 0 giorni residui è dentro la finestra"),

    # --- 2. manutenzione + supporto valido ---
    Case("99999999-1000-4000-8000-000000000010", "SW-ACCESS-01",
         "sw-access-01", supporto=5, stato="manutenzione", notifiable=1,
         why="manutenzione: lo stato non toglie l'idoneità, il contratto scade"),

    # --- 3. dismissione + scadenza idonea ---
    Case("99999999-1000-4000-8000-000000000011", "STG-OLD-01",
         "stg-old-01", garanzia=20, stato="dismissione", notifiable=1, h=2,
         why="in dismissione: la decisione non è conclusa, quindi avvisa ancora"),

    # --- 4. dismesso + scadenza FUTURA: visibile ma NON notificabile ---
    Case("99999999-1000-4000-8000-000000000020", "SRV-DISM-01",
         "srv-dism-01", garanzia=15, stato="dismesso", notifiable=0,
         why="dismesso: resta ispezionabile in Scadenze e non genera avvisi (§7)"),
    # Dismesso E rimosso: la combinazione della fase 2G. Non occupa U, non avvisa,
    # e va comunque trovato dalla ricerca e dalla vista Dismessi.
    Case("99999999-1000-4000-8000-000000000021", "SRV-DISM-02",
         "srv-dism-02", garanzia=10, stato="dismesso", presenza="rimosso",
         notifiable=0,
         why="dismesso e portato via: né spazio né avvisi, ma il dato resta"),

    # --- 5. già scaduto: visibile, NON avvisato retroattivamente ---
    Case("99999999-1000-4000-8000-000000000030", "SRV-EXP-01",
         "srv-exp-01", garanzia=-3, notifiable=0,
         why="scaduto tre giorni fa: la voce 10 del registro dice che non esiste "
             "nessun avviso retroattivo"),
    Case("99999999-1000-4000-8000-000000000031", "SRV-EXP-02",
         "srv-exp-02", garanzia=-1, notifiable=0,
         why="scaduto ieri: il bordo dall'altra parte"),

    # --- 6. fuori dalla finestra ---
    Case("99999999-1000-4000-8000-000000000040", "SRV-FAR-01",
         "srv-far-01", garanzia=WINDOW + 1, notifiable=0,
         why="un giorno oltre la finestra: escluso"),
    Case("99999999-1000-4000-8000-000000000041", "SRV-FAR-02",
         "srv-far-02", garanzia=200, supporto=365, notifiable=0,
         why="lontano: non deve comparire in nessun digest"),

    # --- 7. garanzia E supporto, entrambi in finestra: DUE avvisi ---
    Case("99999999-1000-4000-8000-000000000050", "SRV-BOTH-01",
         "srv-both-01", garanzia=8, supporto=25, notifiable=2,
         why="due scadenze distinte sullo stesso apparato: due promemoria, non uno"),
    # Uno dentro e uno fuori: la selezione è per SCADENZA, non per dispositivo.
    Case("99999999-1000-4000-8000-000000000051", "SRV-BOTH-02",
         "srv-both-02", garanzia=9, supporto=400, notifiable=1,
         why="una in finestra e una fuori: si seleziona la scadenza, non la macchina"),

    # --- 8. id di business DUPLICATO, _uid distinti ---
    Case("99999999-1000-4000-8000-000000000060", "SRV-DUP", "dup-in-sala-a",
         garanzia=11, notifiable=1,
         why="id ripetuto (normale negli inventari importati da fogli di calcolo): "
             "l'identità è l'_uid, quindi due promemoria distinti"),
    Case("99999999-1000-4000-8000-000000000061", "SRV-DUP", "dup-in-sala-b",
         garanzia=18, notifiable=1,
         why="il gemello, in un'altra sala"),

    # --- 9. contesto Unicode ---
    Case("99999999-1000-4000-8000-000000000070", "SRV-UNI-01",
         "srv-röntgen-01 — µ", garanzia=13, notifiable=1,
         extra={"owner": "Reparto Diagnostica — Dott.ssa Öztürk",
                "model": "Modèle Ω-3000", "note": "在机架顶部"},
         why="accenti, greco, cinese e un em dash nei campi che finiscono nel "
             "digest della posta"),

    # --- 10. contesto con una BARRA ---
    Case("99999999-1000-4000-8000-000000000080", "SRV-SLASH-01",
         "srv-slash-01", garanzia=14, notifiable=1,
         why="sta nel rack il cui NOME contiene una barra: il carattere con cui si "
             "costruiscono i percorsi deve arrivare intatto"),

    # --- 11. dispositivo fisicamente RIMOSSO ma ancora attivo ---
    Case("99999999-1000-4000-8000-000000000090", "SRV-MOVED-01",
         "srv-moved-01", garanzia=6, presenza="rimosso", notifiable=1,
         why="portato via e non ancora registrato altrove: NON occupa U, e la "
             "garanzia scade comunque — chi la rinnova deve saperlo (§7 della 2G)"),

    # --- extra: i casi che la fase 2G ha reso interessanti ---
    Case("99999999-1000-4000-8000-0000000000a0", "SRV-NODATE-01",
         "srv-nodate-01", notifiable=0,
         why="nessuna data: il caso più comune nell'inventario reale"),
    Case("99999999-1000-4000-8000-0000000000a1", "SRV-BADDATE-01",
         "srv-baddate-01", notifiable=0,
         extra={"garanzia": "in attesa di conferma", "supporto": "2026-13-45"},
         why="testo non interpretabile come data: si ignora senza far cadere il "
             "giro, e si VEDE nella vista Scadenze come non interpretabile"),
    Case("99999999-1000-4000-8000-0000000000a2", "SRV-INJECT-01",
         "<b>srv-x</b>\r\nBcc: qualcuno@altrove.example", garanzia=4, notifiable=1,
         why="un CRLF in un nome non deve poter aggiungere un destinatario al "
             "digest: il nome lo scrive una persona, spesso incollandolo"),
)


def _rack(uid: str, code: str, name: str, u: int, x: float, y: float,
          devices: list) -> dict:
    return {"_uid": uid, "id": code, "name": name, "u": u, "row": "A",
            "x": x, "y": y, "w": 0.6, "h": 1.0, "seriali": [], "devices": devices}


def build_inventory(reference: date) -> dict:
    """Il documento, con le date calcolate rispetto a `reference`.

    Nessuna lettura dell'orologio: la data la passa il chiamante, sempre.
    """
    per_uid = {c.uid: c for c in CASES}

    def devs(*uids: str) -> list:
        """Gli apparati del rack, ognuno allo slot successivo.

        Gli slot si distanziano di 3 U: lascia dei buchi fra gli apparati, che è
        come stanno i rack veri, ed è ciò che rende non banale il calcolo del blocco
        contiguo più ampio.
        """
        return [per_uid[u].device(reference, 1 + i * 3)
                for i, u in enumerate(uids)]

    rack_a = _rack(RACK_A, "R01", "Rack R01 — Core", 45, 0.2, 0.2, devs(
        "99999999-1000-4000-8000-000000000001",
        "99999999-1000-4000-8000-000000000002",
        "99999999-1000-4000-8000-000000000003",
        "99999999-1000-4000-8000-000000000010",
        "99999999-1000-4000-8000-000000000050",
        "99999999-1000-4000-8000-000000000051",
        "99999999-1000-4000-8000-000000000060",
        "99999999-1000-4000-8000-0000000000a2",
    ))
    rack_b = _rack(RACK_B, "R02", "Rack R02 — Storage", 45, 0.9, 0.2, devs(
        "99999999-1000-4000-8000-000000000011",
        "99999999-1000-4000-8000-000000000020",
        "99999999-1000-4000-8000-000000000021",
        "99999999-1000-4000-8000-000000000030",
        "99999999-1000-4000-8000-000000000031",
        "99999999-1000-4000-8000-000000000040",
        "99999999-1000-4000-8000-000000000041",
        "99999999-1000-4000-8000-000000000090",
    ))
    rack_slash = _rack(RACK_SLASH, "RO4495", RACK_SLASH_NAME, 45, 1.6, 0.2, devs(
        "99999999-1000-4000-8000-000000000080",
        "99999999-1000-4000-8000-0000000000a0",
        "99999999-1000-4000-8000-0000000000a1",
    ))
    # Un armadio da 12 U: nella struttura vera esiste («Armadio UPS»), e la
    # capacità di un rack basso è un caso diverso da quella di uno da 45.
    rack_ups = _rack(RACK_UPS, "UPS", "Armadio UPS", 12, 2.3, 0.2, devs(
        "99999999-1000-4000-8000-000000000070",
        "99999999-1000-4000-8000-000000000061",
    ))

    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{
            "_uid": LOC, "id": "qualificazione", "nome": SITE_NAME,
            "sale": [
                {
                    "_uid": ROOM_A, "id": "sala-a", "nome": "Sala A — Backend",
                    # Misure e geometria di forma reale: copiate dalla struttura
                    # vera (Sala Backend, 4.25 × 4.99, una porta in basso). Una
                    # qualificazione su una sala 10×10 senza porte non
                    # qualificherebbe il render della planimetria.
                    "w": 4.25, "h": 4.99, "area": "", "dim": "", "segnaposto": False,
                    "vani": [{"x": 0, "y": 0, "w": 4.25, "h": 4.99,
                              "porta": {"lato": "bottom", "x": 0.35, "w": 0.84}}],
                    "racks": [rack_a, rack_b, rack_slash],
                },
                {
                    "_uid": ROOM_B, "id": "sala-b", "nome": ROOM_B_NAME,
                    "w": 9.2, "h": 6, "area": "", "dim": "", "segnaposto": False,
                    # ⚠ `vani: []` di proposito: è il default canonico, ed è la
                    # forma che ha fatto morire il render di tutta l'applicazione
                    # nella fase 2H. Una sala così deve stare in ogni scenario di
                    # qualificazione, per sempre.
                    "vani": [],
                    "racks": [rack_ups],
                },
            ],
        }],
    }


# ==================================================================
# l'atteso, ricavato da CASES e non ricalcolato dal dominio
# ==================================================================

def expected_notifiable(window: int = WINDOW) -> dict[str, int]:
    """uid → quante righe di avviso, per la finestra su cui `CASES` è tarato."""
    if window != WINDOW:
        raise ValueError(
            f"CASES dichiara l'atteso per una finestra di {WINDOW} giorni. "
            f"Per un'altra finestra l'atteso va dichiarato, non dedotto: dedurlo "
            f"con la stessa funzione che si verifica non proverebbe niente.")
    return {c.uid: c.notifiable for c in CASES}


def total_notifiable(window: int = WINDOW) -> int:
    return sum(expected_notifiable(window).values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reference", default=REFERENCE.isoformat(),
                    help="data di riferimento, YYYY-MM-DD (default: la data del "
                         "JSON committato)")
    args = ap.parse_args()
    doc = build_inventory(date.fromisoformat(args.reference))
    json.dump(doc, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
