#!/usr/bin/env python3
"""Documenti di prova per la mappa relazionale (fasi 2A e 2B, §8.42).

Si GENERANO invece di stare come JSON nel repository, per la stessa ragione delle
fixture delle scadenze: un file statico smette di provare ciò che dice appena il
codice cambia, e nessuno se ne accorge. Qui ogni documento è costruito da una base
comune con una singola variazione, così è chiaro *cosa* prova.

Ogni documento deve soddisfare l'invariante:

    canonicalise(assemble(normalise(doc))) == canonicalise(doc)

e produrre ZERO eventi di dominio rispetto al proprio giro completo.

Uso:  python fixtures/relational/build.py [--name <nome>]
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy

CURRENT_SCHEMA_VERSION = 1


def uid(group: str, n: int) -> str:
    """UUID v4 deterministico. La versione (4) e la variante (8) sono obbligatorie:
    `UUID_RE` accetta solo la v4 (§8.4), e un identificativo che non potremmo aver
    generato noi non è un'identità valida."""
    return f"{group * 8}-0000-4000-8000-{n:012x}"


L1, L2 = uid("a", 1), uid("a", 2)
R1, R2, R3 = uid("b", 1), uid("b", 2), uid("b", 3)
K1, K2, K3, K4 = uid("c", 1), uid("c", 2), uid("c", 3), uid("c", 4)
D1, D2, D3, D4, D5 = (uid("d", 1), uid("d", 2), uid("d", 3), uid("d", 4), uid("d", 5))
M1, M2 = uid("e", 1), uid("e", 2)

FOTO_A = "3cedd91a-f520-4fac-9a26-6626a7acd68f"
FOTO_B = "c2266c88-4b97-4023-9687-75ddd58df30c"


# ==================================================================
# base
# ==================================================================

# NB: il primo parametro si chiama `entity_uid` e non `u`. `u` è l'unità rack, che
# arriva fra i `**over`: chiamarli allo stesso modo dava «got multiple values for
# argument 'u'» al primo dispositivo con una posizione esplicita.
def _device(entity_uid: str, code: str, name: str, **over) -> dict:
    d = {"_uid": entity_uid, "id": code, "name": name, "type": "server",
         "u": 1, "h": 1}
    d.update(over)
    return d


def _rack(entity_uid: str, code: str, **over) -> dict:
    r = {"_uid": entity_uid, "id": code, "name": code, "row": "A", "u": 45,
         "x": 0.5, "y": 1.25, "w": 0.6, "h": 0.65, "devices": []}
    r.update(over)
    return r


def base() -> dict:
    """Due siti, tre sale, quattro rack, cinque dispositivi, due voci di manuale.

    Contiene di proposito: una foto su un rack e non sull'altro, geometria di sala
    non banale con due vani e due porte, un array di seriali, valori vuoti e zeri
    espliciti, date presenti e assenti.
    """
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [
            {
                "_uid": L1, "id": "pomezia", "nome": "Pomezia G0",
                "sale": [
                    {
                        "_uid": R1, "id": "sala-1", "nome": "Sala 1",
                        "w": 8.5, "h": 6.25,
                        "area": "53.13 m²", "dim": "8.50 × 6.25 m",
                        # Geometria non banale: due vani, uno con DUE porte. È il
                        # caso che una tabella `vani` più una tabella `porte`
                        # renderebbe due join per disegnare una pianta.
                        "vani": [
                            {"x": 0, "y": 0, "w": 4.25, "h": 6.25,
                             "porta": {"lato": "bottom", "x": 0.35, "w": 0.84}},
                            {"x": 4.25, "y": 0, "w": 4.25, "h": 6.25,
                             "porta": {"lato": "left", "y": 1.2, "w": 0.9},
                             "porta2": {"lato": "top", "x": 2.0, "w": 1.1}},
                        ],
                        "racks": [
                            _rack(K1, "R01", foto=FOTO_A,
                                  seriali=["2006004084", "2006004085"],
                                  devices=[
                                      _device(D1, "srv-web", "srv-web",
                                              ip="10.0.2.11", serial="SN-1",
                                              owner="Rossi",
                                              garanzia="2027-03-14",
                                              supporto="2027-03-14"),
                                      _device(D2, "srv-db", "srv-db", u=3, h=2,
                                              type="storage",
                                              # Data di garanzia assente, supporto
                                              # presente: il caso opzionale.
                                              supporto="2026-12-31"),
                                  ]),
                            # Rack SENZA foto, e con valori vuoti/zero espliciti.
                            _rack(K2, "R02", x=0, y=0, row="", name="",
                                  devices=[
                                      _device(D3, "sw-core", "sw-core",
                                              type="rete", stato="manutenzione",
                                              model="", ip="", note=""),
                                  ]),
                        ],
                    },
                    {
                        # Sala segnaposto: `segnaposto: True` esplicito, e la sala
                        # non ha rack. Il booleano vero è il caso simmetrico del
                        # `False` che la canonicalizzazione materializza.
                        "_uid": R2, "id": "sala-2", "nome": "Sala 2",
                        "w": 3, "h": 3, "segnaposto": True,
                        "vani": [], "racks": [],
                    },
                ],
            },
            {
                "_uid": L2, "id": "oriolo", "nome": "Oriolo Romano A0",
                "sale": [
                    {
                        "_uid": R3, "id": "sala-a", "nome": "Sala A",
                        "w": 5, "h": 4, "vani": [{"x": 0, "y": 0, "w": 5, "h": 4,
                                                  "porta": None}],
                        "racks": [
                            _rack(K3, "R01", foto=FOTO_B,
                                  devices=[
                                      # STESSO identificativo di business di un
                                      # dispositivo in un altro rack, `_uid`
                                      # diverso: caso normale con gli inventari
                                      # importati da fogli di calcolo.
                                      _device(D4, "srv-web", "srv-web-oriolo",
                                              owner="Bianchi"),
                                  ]),
                            _rack(K4, "R02", seriali=[], devices=[
                                _device(D5, "ups-1", "ups-1",
                                        type="alimentazione", u=0),
                            ]),
                        ],
                    },
                ],
            },
        ],
        "manuale": [
            {"_uid": M1, "id": "man-avvio", "titolo": "Avvio",
             "blocchi": [{"titolo": "", "paragrafi": ["Primo paragrafo.",
                                                      "Secondo paragrafo."]}]},
            {"_uid": M2, "id": "man-backup", "titolo": "Backup", "custom": True,
             "blocchi": [{"titolo": "Nastri", "paragrafi": ["Testo."]}]},
        ],
    }


# ==================================================================
# varianti
# ==================================================================

def _find_rack(doc: dict, rack_uid: str) -> dict:
    for L in doc["locations"]:
        for R in L["sale"]:
            for K in R["racks"]:
                if K["_uid"] == rack_uid:
                    return K
    raise KeyError(rack_uid)


def variant_renamed() -> dict:
    """Rack e dispositivo RINOMINATI: cambiano codice ed etichetta, `_uid` no.

    È il caso che un'identità basata sul codice romperebbe: dopo la rinomina il
    rack non sarebbe più «lo stesso rack» e la storia si spezzerebbe (§8.4).
    """
    doc = base()
    rack = _find_rack(doc, K1)
    rack["id"] = "R01-NUOVO"
    rack["name"] = "Rack 1 rinominato"
    rack["devices"][0]["id"] = "srv-web-2"
    rack["devices"][0]["name"] = "srv-web (rinominato)"
    return doc


def variant_moved_device() -> dict:
    """Dispositivo SPOSTATO in un altro rack, con lo stesso `_uid`."""
    doc = base()
    src = _find_rack(doc, K1)
    dst = _find_rack(doc, K2)
    moved = src["devices"].pop(0)
    moved["u"] = 10
    dst["devices"].append(moved)
    return doc


def variant_reordered() -> dict:
    """Siti, sale e rack deliberatamente riordinati.

    L'ordine è semanticamente rilevante — i tab delle sale seguono quello, e un
    riordino è un evento di dominio (§8.10). Se la mappa perdesse l'ordine, il
    primo salvataggio dopo la fase 2D produrrebbe eventi `reorder` che nessuno ha
    causato.
    """
    doc = base()
    doc["locations"].reverse()
    for L in doc["locations"]:
        L["sale"].reverse()
        for R in L["sale"]:
            R["racks"].reverse()
            for K in R["racks"]:
                K["devices"].reverse()
    doc["manuale"].reverse()
    return doc


def variant_implicit_defaults() -> dict:
    """Default canonici IMPLICITI: i campi che l'applicazione tratta come
    predefiniti sono assenti invece di espliciti.

    Insieme a `explicit_defaults` è la coppia che prova che la mappa non confonde
    «assente» con «vuoto»: dopo la canonicalizzazione i due documenti devono
    essere lo stesso documento.
    """
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{
            "_uid": L1, "id": "pomezia", "nome": "Pomezia G0",
            "sale": [{
                "_uid": R1, "id": "sala-1", "nome": "Sala 1", "w": 5, "h": 4,
                "racks": [{
                    "_uid": K1, "id": "R01", "x": 0.1, "y": 0.1, "w": 0.6, "h": 0.65,
                    "devices": [{"_uid": D1, "id": "srv", "name": "srv", "u": 1}],
                }],
            }],
        }],
    }


def variant_explicit_defaults() -> dict:
    """Gli stessi dati, con ogni default scritto a mano."""
    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{
            "_uid": L1, "id": "pomezia", "nome": "Pomezia G0", "sale": [{
                "_uid": R1, "id": "sala-1", "nome": "Sala 1", "w": 5, "h": 4,
                "area": "", "dim": "", "segnaposto": False, "vani": [],
                "racks": [{
                    "_uid": K1, "id": "R01", "name": "", "row": "", "u": 45,
                    "x": 0.1, "y": 0.1, "w": 0.6, "h": 0.65, "seriali": [],
                    "devices": [{
                        "_uid": D1, "id": "srv", "name": "srv", "u": 1, "h": 1,
                        "type": "altro", "stato": "attivo", "model": "", "ip": "",
                        "serial": "", "owner": "", "garanzia": "", "supporto": "",
                        "note": "",
                    }],
                }],
            }],
        }],
    }


def variant_empty_zero_false() -> dict:
    """Stringhe vuote, zeri e `False` ESPLICITI.

    Sono valori dell'utente, non assenze: la canonicalizzazione sostituisce solo
    `None` (§8.14). Una mappa che li trattasse come «vuoto» li sostituirebbe con i
    default e la differenza comparirebbe nell'audit come una modifica mai fatta.
    """
    doc = variant_explicit_defaults()
    room = doc["locations"][0]["sale"][0]
    room["w"] = 0
    room["h"] = 0
    room["segnaposto"] = False
    rack = room["racks"][0]
    rack["x"] = 0
    rack["y"] = 0
    rack["u"] = 0
    rack["name"] = ""
    device = rack["devices"][0]
    device["u"] = 0
    device["h"] = 0
    device["note"] = ""
    return doc


def variant_no_manual() -> dict:
    """`manuale` ASSENTE, non vuoto.

    `canonicalise` conserva la differenza fra «assente» e «lista vuota»: senza il
    booleano nel modello, il primo salvataggio dopo la migrazione aggiungerebbe una
    radice che nessuno ha creato.
    """
    doc = base()
    doc.pop("manuale")
    return doc


def variant_empty_manual() -> dict:
    """`manuale: []`, cioè presente e vuoto."""
    doc = base()
    doc["manuale"] = []
    return doc


def variant_no_photos() -> dict:
    """Nessun rack con foto: il caso di partenza di ogni installazione."""
    doc = base()
    for L in doc["locations"]:
        for R in L["sale"]:
            for K in R["racks"]:
                K.pop("foto", None)
    return doc


def variant_explicit_null_photo() -> dict:
    """`foto: null` ESPLICITO su un rack.

    Non è la stessa cosa di `foto` assente, e lo schema congelato lo accetta
    (§8.16). È il caso che dimostra perché la colonna `photo_id` non può
    rappresentare tutto: un `null` esplicito non è un UUID, e viaggia in `extra`.
    """
    doc = base()
    _find_rack(doc, K2)["foto"] = None
    return doc


def variant_unknown_fields() -> dict:
    """Campi che le colonne NON conoscono, a ogni livello.

    Il documento è aperto: lo schema congelato vincola le chiavi di radice, non i
    campi delle entità, e il frontend deriva ogni entità dall'esistente proprio
    perché i campi ignoti sopravvivano (§8.4). Una mappa che li perdesse sarebbe
    lossy per costruzione, e lo si scoprirebbe in produzione.
    """
    doc = base()
    doc["locations"][0]["etichettaFutura"] = "qualcosa"
    doc["locations"][0]["sale"][0]["temperaturaMax"] = 27.5
    rack = _find_rack(doc, K1)
    rack["cablaggio"] = {"patch": 24, "fibra": 4}
    rack["devices"][0]["tagArbitrari"] = ["a", "b"]
    doc["manuale"][0]["revisione"] = 3
    return doc


def variant_untyped_values() -> dict:
    """Valori del tipo «sbagliato» per una colonna tipizzata.

    Nessuna validazione oggi rifiuta `u: "45"`: un import può produrlo. La mappa
    li conserva in `extra` e la validazione lo segnala come `carried_verbatim` —
    integro, ma non interrogabile.
    """
    doc = base()
    rack = _find_rack(doc, K1)
    rack["u"] = "45"                    # intero come stringa
    rack["seriali"] = ["ok", 12345]     # array non omogeneo
    rack["devices"][0]["h"] = 1.5       # non intero
    return doc


def variant_broken_dates() -> dict:
    """Date di garanzia/supporto non leggibili, come nell'inventario reale."""
    doc = base()
    rack = _find_rack(doc, K1)
    rack["devices"][0]["garanzia"] = "in attesa"
    rack["devices"][0]["supporto"] = "2026-13-45"
    rack["devices"][1]["garanzia"] = ""
    return doc


def variant_unknown_enums() -> dict:
    """Tipo e stato fuori dal vocabolario noto."""
    doc = base()
    rack = _find_rack(doc, K1)
    rack["devices"][0]["type"] = "quantistico"
    rack["devices"][0]["stato"] = "in prestito"
    return doc


def variant_same_code_same_rack() -> dict:
    """Due dispositivi con lo STESSO identificativo NELLO STESSO rack.

    Ammesso: il validatore di identità lo tollera da sempre e l'import tabellare
    lo produce. Serve a fissare che il vincolo di unicità con ambito NON esiste per
    i dispositivi — metterlo farebbe rifiutare alla fase 2C documenti che la fase 1
    accetta.
    """
    doc = base()
    rack = _find_rack(doc, K1)
    rack["devices"].append(_device(uid("d", 90), "srv-web", "srv-web-bis"))
    return doc


def variant_swapped_codes() -> dict:
    """Due rack che si SCAMBIANO il codice, conservando l'`_uid`.

    È il caso per cui i vincoli di unicità con ambito devono essere `DEFERRABLE`:
    a metà transazione i due codici collidono, e alla fine no.
    """
    doc = base()
    a, b = _find_rack(doc, K1), _find_rack(doc, K2)
    a["id"], b["id"] = b["id"], a["id"]
    return doc


def variant_deep_room_geometry() -> dict:
    """Geometria di sala volutamente complicata: vani con e senza porta, porte su
    tutti i lati, numeri con molti decimali."""
    doc = base()
    room = doc["locations"][0]["sale"][0]
    room["vani"] = [
        {"x": 0, "y": 0, "w": 2.5, "h": 3.125, "porta": {"lato": "top", "x": 0.5, "w": 0.9}},
        {"x": 2.5, "y": 0, "w": 2.5, "h": 3.125, "porta": {"lato": "right", "y": 0.25, "w": 1.05}},
        {"x": 0, "y": 3.125, "w": 5, "h": 3.125},
        {"x": 5, "y": 0, "w": 3.5, "h": 6.25,
         "porta": {"lato": "left", "y": 2.5, "w": 0.8},
         "porta2": {"lato": "bottom", "x": 1.25, "w": 1.2},
         "note": "vano tecnico"},
    ]
    return doc


def variant_hostile_numbers() -> dict:
    """I numeri che si rompevano legando il float al posto del `Decimal`.

    Misurati contro PostgreSQL vero, non supposti (§8.42):

      10.0                 tornava `10` — intero, cioè `json.dumps` diverso
      0.30000000000000004  tornava `0.3`
      1e-9                 esponente negativo: la scala regge, e deve tornare

    Tutti e tre ATTRAVERSANO anche JSONB senza danni, quindi questo documento deve
    superare il giro completo dal database vero come tutti gli altri.
    """
    doc = base()
    room = doc["locations"][0]["sale"][0]
    room["w"] = 10.0                       # deve tornare 10.0, non 10
    room["h"] = 0.30000000000000004        # deve tornare tutte le cifre
    rack = _find_rack(doc, K1)
    rack["w"] = 1e-9
    return doc


def variant_jsonb_hostile_numbers() -> dict:
    """⚠ Numeri che nemmeno JSONB conserva — quindi il confine NON è la proiezione.

    Trovati dal confronto dei digest della fase 2B, e vale la pena essere precisi su
    dove sta il problema. Misurato:

      1e+20  →  jsonb  →  100000000000000000000   (int: `json.dumps` diverso)
      -0.0   →  jsonb  →  0.0                     (`numeric` non ha il segno dello
                                                    zero, e `jsonb` usa `numeric`)

    `inventory_versions.doc` È jsonb: un documento con questi valori viene salvato,
    ma il digest REGISTRATO al salvataggio non corrisponde più al documento che si
    rilegge. Non è un difetto introdotto dalla proiezione — è una proprietà del
    magazzino delle istantanee, che il controllo dei digest ha reso visibile.

    La mappa, di suo, li porta in `extra` e li restituisce identici: l'invariante in
    memoria vale. È il giro attraverso il database che no, e per questo il documento
    è escluso dalla passata su PostgreSQL, dove ha un test suo che pretende l'abort.
    """
    doc = base()
    rack = _find_rack(doc, K1)
    rack["x"] = 1e20
    rack["y"] = -0.0
    return doc


def variant_oversized_integers() -> dict:
    """Interi più grandi di una colonna `integer`.

    `u` e `h` sono `integer`, cioè int32. Un `u: 3_000_000_000` non è un valore da
    difendersi in teoria: è un `INSERT` che fallisce con «integer out of range» a
    metà del popolamento, cioè una migrazione che aborta per un dato che la fase 1
    ha sempre accettato. Deve viaggiare in `extra`, dove non ha limiti.
    """
    doc = base()
    rack = _find_rack(doc, K1)
    rack["u"] = 3_000_000_000
    rack["devices"][0]["u"] = -3_000_000_000
    return doc


def variant_dated_devices() -> dict:
    """Ogni dispositivo con date di garanzia e supporto leggibili.

    Serve alle colonne derivate: il seed di produzione non ha nessuna data, quindi
    senza questo documento i test sulle date girerebbero tutti su colonne vuote e
    non proverebbero niente.
    """
    doc = base()
    date_per_uid = {
        D1: ("2026-08-31", "2027-01-15"),
        D2: ("2026-09-01", "2026-09-01"),
        D3: ("2030-12-31", "2020-01-01"),   # una molto lontana, una già passata
        D4: ("2026-02-29", "2026-2-3"),     # 29 febbraio inesistente; non ISO
        D5: (" 2026-10-10 ", "2026-10-10"),  # spazi: il parser li tollera
    }
    for location in doc["locations"]:
        for room in location["sale"]:
            for rack in room["racks"]:
                for device in rack["devices"]:
                    pair = date_per_uid.get(device["_uid"])
                    if pair:
                        device["garanzia"], device["supporto"] = pair
    return doc


VARIANTS = {
    "base": base,
    "renamed": variant_renamed,
    "moved-device": variant_moved_device,
    "reordered": variant_reordered,
    "implicit-defaults": variant_implicit_defaults,
    "explicit-defaults": variant_explicit_defaults,
    "empty-zero-false": variant_empty_zero_false,
    "no-manual": variant_no_manual,
    "empty-manual": variant_empty_manual,
    "no-photos": variant_no_photos,
    "explicit-null-photo": variant_explicit_null_photo,
    "unknown-fields": variant_unknown_fields,
    "untyped-values": variant_untyped_values,
    "broken-dates": variant_broken_dates,
    "unknown-enums": variant_unknown_enums,
    "same-code-same-rack": variant_same_code_same_rack,
    "swapped-codes": variant_swapped_codes,
    "deep-room-geometry": variant_deep_room_geometry,
    "hostile-numbers": variant_hostile_numbers,
    "jsonb-hostile-numbers": variant_jsonb_hostile_numbers,
    "oversized-integers": variant_oversized_integers,
    "dated-devices": variant_dated_devices,
}


def documents() -> dict[str, dict]:
    """Tutti i documenti di prova, per nome."""
    return {name: deepcopy(fn()) for name, fn in VARIANTS.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="", help="un solo documento, per nome")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for name in VARIANTS:
            print(name)
        return 0
    docs = documents()
    payload = docs[args.name] if args.name else docs
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
