#!/usr/bin/env python3
"""Corpora di parità per la fase 2F: la sorgente SQL contro `due_items(doc)`.

L'oracolo è **`expiry.due_items`**, la funzione che il worker usava fino alla 2E. È
pura — nessun database, nessuna colonna derivata, solo il testo del documento (un test
lo pretende in `test_get_from_sql_pg.py`) — quindi confrontarla con la nuova sorgente
SQL è un confronto fra due implementazioni indipendenti, non fra un'implementazione e
sé stessa.

⚠ Perché QUI le attese non sono scritte a mano, al contrario di
`fixtures/identity/`. Là il rischio è che i test verifichino l'implementazione contro
sé stessa, e attese scritte a mano lo evitano. Qui il rischio è l'opposto: attese
scritte a mano dimostrerebbero che lo SQL corrisponde alla mia *lettura* di
`due_items`, che è precisamente ciò di cui non ci si può fidare in una migrazione di
comportamento. E a differenza della fase 2E — dove l'oracolo era JavaScript nel
browser e serviva un generatore in Node — l'oracolo è qui, importabile, e si può
chiamare dentro il test.

⚠ Le date sono RELATIVE a una data di riferimento. Una data fissa in un file smette di
provare ciò che dice il giorno dopo: `2026-09-15` è «fra 30 giorni» soltanto per una
settimana.

Ogni corpus è un documento completo e valido (`schemaVersion`, `_uid` UUIDv4 unici):
deve poter passare per `bootstrap`, perché la proiezione la scrive il percorso normale
di salvataggio e non un popolamento di prova. Un corpus che il repository rifiutasse
non proverebbe niente sulla proiezione.

Riferimento: BACKEND-PLAN.md §8.47, §8.48.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

CURRENT_SCHEMA_VERSION = 1

#: Soglie usate dai test di parità. Le tre della configurazione predefinita più i due
#: estremi che contano: nessuna finestra (`due_items` esce subito) e una sola
#: finestra molto larga (tutto ciò che non è scaduto rientra).
WINDOW_SETS = ([90, 30, 7], [30], [7], [3650], [])


def _uid(prefix: str, n: int) -> str:
    """UUID v4 riconoscibile a occhio. Il nibble di versione e i bit di variante
    devono essere quelli giusti: il validatore dell'identità li pretende (§8.4), e una
    fixture che non passa la validazione non arriva mai nella proiezione."""
    return f"{prefix}-0000-4000-8000-{n:012x}"


def _dev(n: int, **fields) -> dict:
    """Dispositivo. `id` e `name` si passano espliciti quando il caso li riguarda:
    NON hanno un valore predefinito nel documento canonico (§8.14), e la catena
    `name or id or "(senza nome)"` esiste proprio per questo."""
    out = {"_uid": _uid("dddddddd", n)}
    out.update(fields)
    return out


def _rack(n: int, devices: list, **fields) -> dict:
    out = {"_uid": _uid("cccccccc", n), "u": 45, "devices": devices}
    out.update(fields)
    return out


def _room(n: int, racks: list, **fields) -> dict:
    out = {"_uid": _uid("bbbbbbbb", n), "w": 10, "h": 8, "vani": [], "racks": racks}
    out.update(fields)
    return out


def _loc(n: int, rooms: list, **fields) -> dict:
    out = {"_uid": _uid("aaaaaaaa", n), "sale": rooms}
    out.update(fields)
    return out


def _doc(locations: list) -> dict:
    return {"schemaVersion": CURRENT_SCHEMA_VERSION, "locations": locations}


# ==================================================================
# i corpora
# ==================================================================

def corpora(reference: date) -> dict[str, dict]:
    """`nome → documento`, con le scadenze calcolate rispetto a `reference`."""
    def d(offset: int) -> str:
        return (reference + timedelta(days=offset)).isoformat()

    out: dict[str, dict] = {}

    # ---------------------------------------------------------------- finestre
    #
    # I confini delle tre soglie predefinite, e i giorni immediatamente dentro e
    # fuori. Il giorno esatto NON è privilegiato: la regola è
    # `0 <= giorni <= N`, e i giorni 6 e 29 sono il RECUPERO — una macchina spenta
    # il giorno del promemoria non lo perde per sempre.
    out["windows"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="oggi", name="oggi", garanzia=d(0)),
        _dev(2, id="d1", name="d1", garanzia=d(1)),
        _dev(3, id="d6", name="d6", garanzia=d(6)),
        _dev(4, id="d7", name="d7", garanzia=d(7)),
        _dev(5, id="d8", name="d8", garanzia=d(8)),
        _dev(6, id="d29", name="d29", garanzia=d(29)),
        _dev(7, id="d30", name="d30", garanzia=d(30)),
        _dev(8, id="d31", name="d31", garanzia=d(31)),
        _dev(9, id="d89", name="d89", garanzia=d(89)),
        _dev(10, id="d90", name="d90", garanzia=d(90)),
        _dev(11, id="d91", name="d91", garanzia=d(91)),
        _dev(12, id="d365", name="d365", garanzia=d(365)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ---------------------------------------------------------------- scaduti
    #
    # `due_items` li ESCLUDE (`days < 0`). La vista Scadenze li mostra: è la prima
    # delle divergenze fra vista e worker (§8.48), e la fase 2F segue il worker.
    out["expired"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="ieri", name="ieri", garanzia=d(-1)),
        _dev(2, id="scaduto", name="scaduto", garanzia=d(-10)),
        _dev(3, id="vecchio", name="vecchio", garanzia=d(-4000)),
        _dev(4, id="oggi", name="oggi", garanzia=d(0)),
        # Scaduta la garanzia, il supporto ancora dentro: metà del dispositivo
        # è dovuta e metà no. Un filtro per dispositivo invece che per (dispositivo,
        # tipo) sbaglierebbe qui, e solo qui.
        _dev(5, id="misto", name="misto", garanzia=d(-3), supporto=d(5)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ------------------------------------------------------ garanzia e supporto
    #
    # Due righe distinte per lo stesso dispositivo, con soglie che possono cadere in
    # finestre diverse. È il caso che l'`UNION ALL` deve produrre e che un
    # `WHERE garanzia_date … OR supporto_date …` avrebbe schiacciato in una riga sola.
    out["kinds"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="entrambe", name="entrambe", garanzia=d(7), supporto=d(7)),
        _dev(2, id="diverse", name="diverse", garanzia=d(3), supporto=d(88)),
        _dev(3, id="solo-gar", name="solo-gar", garanzia=d(10)),
        _dev(4, id="solo-sup", name="solo-sup", supporto=d(10)),
        _dev(5, id="nessuna", name="nessuna"),
        _dev(6, id="vuote", name="vuote", garanzia="", supporto=""),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ------------------------------------------------------------------ dismessi
    #
    # ⚠ `due_items` NON guarda `stato`: scorre `walk(doc)` e basta. Un dispositivo
    # dismesso con la garanzia in scadenza ha SEMPRE prodotto un promemoria, e la fase
    # 2F lo conserva. La vista Scadenze — e quindi
    # `GET /api/inventory/expiries` — lo salta. La divergenza è deliberata (§8.48).
    out["decommissioned"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="attivo", name="attivo", stato="attivo", garanzia=d(7)),
        _dev(2, id="dismesso", name="dismesso", stato="dismesso", garanzia=d(7)),
        # `stato: ""` significa «attivo» per il frontend (stringa vuota falsa in
        # JavaScript) e la canonicalizzazione lo riempie; qui non cambia niente,
        # perché lo scanner non guarda `stato` in nessun caso.
        _dev(3, id="vuoto", name="vuoto", stato="", garanzia=d(7)),
        _dev(4, id="in-dismissione", name="in-dismissione",
             stato="in dismissione", garanzia=d(7)),
        _dev(5, id="ignoto", name="ignoto", stato="qualcosa-altro",
             garanzia=d(7)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ----------------------------------------------------------- date malformate
    #
    # `parse_expiry` pretende `YYYY-MM-DD` esatto dopo uno `strip()`, e valida il
    # calendario. Tutto il resto è `None` — in silenzio, perché un campo di testo
    # scritto a mano non deve fermare il worker. Le colonne derivate le ha scritte lo
    # stesso parser, quindi qui la parità deve essere esatta per costruzione: è la
    # ragione per cui non si è aggiunto un secondo interprete di date.
    out["broken-dates"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="attesa", name="attesa", garanzia="in attesa"),
        _dev(2, id="mese-13", name="mese-13", garanzia="2027-13-01"),
        _dev(3, id="feb-30", name="feb-30", garanzia="2027-02-30"),
        _dev(4, id="senza-zeri", name="senza-zeri", garanzia="2027-3-15"),
        _dev(5, id="slash", name="slash", garanzia="2027/03/15"),
        _dev(6, id="italiano", name="italiano", garanzia="15/03/2027"),
        _dev(7, id="lungo", name="lungo", garanzia="March 15, 2027"),
        _dev(8, id="iso-ora", name="iso-ora", garanzia="2027-03-15T10:00:00Z"),
        _dev(9, id="parziale", name="parziale", garanzia="2027-03"),
        _dev(10, id="anno", name="anno", garanzia="2027"),
        # ⚠ `parse_expiry` fa `.strip()`: gli spazi attorno NON invalidano la data.
        # Con la data di riferimento davanti, questa scadenza è dovuta.
        _dev(11, id="spazi", name="spazi", garanzia=f"  {d(5)}  "),
        # Non è una stringa: `_is_str` è falso, quindi il valore finisce in `extra`,
        # la colonna di testo è NULL e la data derivata è NULL. `parse_expiry` non è
        # una stringa nemmeno per lui: `None`. Le due strade arrivano allo stesso
        # posto per ragioni diverse, e il test lo verifica invece di presumerlo.
        _dev(12, id="numero", name="numero", garanzia=20270315),
        _dev(13, id="nullo", name="nullo", garanzia=None),
        _dev(14, id="lista", name="lista", garanzia=[d(5)]),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ------------------------------------------------------------ etichette
    #
    # `due_items` compone il nome così: `obj.get("name") or obj.get("id") or
    # "(senza nome)"`, poi `str()`. Ogni riga qui è un ramo di quella catena.
    out["labels"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="con-nome", name="Il Nome", garanzia=d(7)),
        # `name` assente: si scende all'id.
        _dev(2, id="solo-id", garanzia=d(7)),
        # `name` vuoto: la stringa vuota è falsa, si scende all'id.
        _dev(3, id="nome-vuoto", name="", garanzia=d(7)),
        # Né nome né id: «(senza nome)».
        _dev(4, garanzia=d(7)),
        # id vuoto e nome assente: nemmeno l'id regge, «(senza nome)».
        _dev(5, id="", garanzia=d(7)),
        # ⚠ `name` NUMERICO. Non è una stringa, quindi la colonna `name` è NULL e il
        # valore sta in `extra`: chi guardasse solo la colonna mostrerebbe l'id. Per
        # lo scanner questo dispositivo si chiama «42», perché `42 or …` è `42`.
        _dev(6, id="numerico", name=42, garanzia=d(7)),
        # `name: 0` è FALSO in Python come in JavaScript: si scende all'id.
        _dev(7, id="zero", name=0, garanzia=d(7)),
        _dev(8, id="falso", name=False, garanzia=d(7)),
        # Strutture: `str()` di una lista e di un dizionario. Improbabile, e la
        # differenza fra «improbabile» e «impossibile» è che il primo arriva.
        _dev(9, id="lista", name=["a", "b"], garanzia=d(7)),
        _dev(10, id="dizionario", name={"x": 1}, garanzia=d(7)),
        # id numerico con nome assente.
        _dev(11, id=7, garanzia=d(7)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    # ---------------------------------------------------- il contesto strutturale
    #
    # Sito, sala e rack nel digest sono gli **id**, non i nomi: `walk` compone il
    # percorso con `f"{L['id']} / {R['id']} / {K['id']} / {V['id']}"` e `_context` lo
    # rispezza. Da qui i due casi limite: l'id assente, che diventava la stringa
    # «None», e l'id che contiene uno `/`, che il rispezzamento troncava.
    out["context"] = _doc([
        _loc(1, [_room(1, [_rack(1, [
            _dev(1, id="normale", name="normale", garanzia=d(7)),
        ], id="R01", name="R01")], id="sala-1", nome="Sala 1")],
            id="sito-1", nome="Sito 1"),
        # Rack SENZA id: il percorso conteneva «None».
        _loc(2, [_room(2, [_rack(2, [
            _dev(2, id="rack-senza-id", name="rack-senza-id", garanzia=d(7)),
        ], name="anonimo")], id="sala-2", nome="Sala 2")],
            id="sito-2", nome="Sito 2"),
        # Sala senza id, e sito con id numerico.
        _loc(3, [_room(3, [_rack(3, [
            _dev(3, id="sala-senza-id", name="sala-senza-id", garanzia=d(7)),
        ], id="R03", name="R03")], nome="Sala 3")], id=3, nome="Sito 3"),
        # ⚠ La divergenza voluta: id con uno `/` dentro. Il percorso vecchio lo
        # troncava al primo `/`; la JOIN lo restituisce intero.
        _loc(4, [_room(4, [_rack(4, [
            _dev(4, id="rack-con-slash", name="rack-con-slash", garanzia=d(7)),
        ], id="10.0.0.0/24", name="rete")], id="sala-4", nome="Sala 4")],
            id="sito-4", nome="Sito 4"),
        # Uno `/` nel SITO: sposta tutte le parti di un posto, non solo la sua.
        _loc(5, [_room(5, [_rack(5, [
            _dev(5, id="sito-con-slash", name="sito-con-slash", garanzia=d(7)),
        ], id="R05", name="R05")], id="sala-5", nome="Sala 5")],
            id="a/b", nome="Sito 5"),
    ])

    # ------------------------------------------------------------ id duplicati
    #
    # Stesso id di business, `_uid` diversi: DUE entità di promemoria indipendenti.
    # Con gli inventari importati da un foglio di calcolo gli id ripetuti sono la
    # norma, e raggruppare per id manderebbe un avviso solo per due macchine.
    out["duplicates"] = _doc([_loc(1, [_room(1, [
        _rack(1, [
            _dev(1, id="SRV-DUP", name="dup-a", garanzia=d(7)),
            _dev(2, id="SRV-DUP", name="dup-b", garanzia=d(30)),
            # Stesso id E stesso nome, nello stesso rack: distinguibili solo per
            # `_uid`. È il caso in cui un raggruppamento per etichetta perderebbe
            # una riga senza che nessuno lo noti.
            _dev(3, id="GEMELLO", name="gemello", garanzia=d(5)),
            _dev(4, id="GEMELLO", name="gemello", garanzia=d(5)),
        ], id="R01", name="R01"),
        # Stesso id in un altro rack: tre entità con lo stesso id di business.
        _rack(2, [
            _dev(5, id="SRV-DUP", name="dup-c", garanzia=d(60)),
        ], id="R02", name="R02"),
    ], id="sala-1", nome="Sala 1")], id="sito-1", nome="Sito 1")])

    # ------------------------------------------------------- albero su più livelli
    #
    # Due siti, tre sale, quattro rack. Serve all'ORDINAMENTO: la chiave di
    # `due_items` è `(giorni, tipo, sito, sala, rack, nome, uid)`, quindi con un solo
    # sito le quattro componenti di mezzo non sono mai messe alla prova.
    out["tree"] = _doc([
        _loc(1, [
            _room(1, [
                _rack(1, [_dev(1, id="a1", name="a1", garanzia=d(7))],
                      id="R01", name="R01"),
                _rack(2, [_dev(2, id="a2", name="a2", garanzia=d(7))],
                      id="R02", name="R02"),
            ], id="sala-a", nome="Sala A"),
            _room(2, [
                _rack(3, [_dev(3, id="b1", name="b1", garanzia=d(7),
                               supporto=d(7))], id="R01", name="R01"),
            ], id="sala-b", nome="Sala B"),
        ], id="alfa", nome="Alfa"),
        _loc(2, [
            _room(3, [
                _rack(4, [
                    _dev(4, id="c1", name="c1", garanzia=d(7)),
                    # Stesso giorno, stesso tipo, stesso rack: a spareggiare
                    # restano nome e uid.
                    _dev(5, id="c2", name="c1", garanzia=d(7)),
                ], id="R01", name="R01"),
            ], id="sala-a", nome="Sala A"),
        ], id="beta", nome="Beta"),
    ])

    # ------------------------------------------------------ dispositivo spostato
    #
    # Stesso `_uid` sotto un rack diverso, con la stessa data. L'identità del
    # promemoria non deve cambiare: è `_uid`, non la posizione. I due corpora si usano
    # in sequenza.
    _spostato_prima = _doc([_loc(1, [_room(1, [
        _rack(1, [_dev(1, id="viaggiatore", name="viaggiatore", garanzia=d(7))],
              id="R01", name="R01"),
        _rack(2, [], id="R02", name="R02"),
    ], id="sala-1", nome="Sala 1")], id="sito-1", nome="Sito 1")])
    _spostato_dopo = _doc([_loc(1, [_room(1, [
        _rack(1, [], id="R01", name="R01"),
        _rack(2, [_dev(1, id="viaggiatore", name="viaggiatore", garanzia=d(7))],
              id="R02", name="R02"),
    ], id="sala-1", nome="Sala 1")], id="sito-1", nome="Sito 1")])
    out["moved-before"] = _spostato_prima
    out["moved-after"] = _spostato_dopo

    # ------------------------------------------------------- data cambiata
    #
    # Stesso `_uid`, data diversa: un ciclo di vita NUOVO del promemoria, perché la
    # data fa parte dell'identità. I due corpora si usano in sequenza.
    out["redated-before"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="prorogato", name="prorogato", garanzia=d(7)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")],
        id="sito-1", nome="Sito 1")])
    out["redated-after"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="prorogato", name="prorogato", garanzia=d(37)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")],
        id="sito-1", nome="Sito 1")])

    # ----------------------------------------------------------- inventario vuoto
    #
    # Nessun sito. Non è un caso di laboratorio: è lo stato di un'installazione
    # appena inizializzata, e «niente è dovuto» deve restare distinto da un guasto.
    out["empty"] = _doc([])
    # Un sito senza sale, una sala senza rack, un rack senza dispositivi: i tre modi
    # di avere un albero che finisce prima dei dispositivi.
    out["empty-branches"] = _doc([
        _loc(1, [], id="vuoto", nome="Vuoto"),
        _loc(2, [_room(1, [], id="sala-vuota", nome="Sala vuota")],
             id="sito-2", nome="Sito 2"),
        _loc(3, [_room(2, [_rack(1, [], id="R01", name="R01")],
                       id="sala-3", nome="Sala 3")], id="sito-3", nome="Sito 3"),
    ])

    # ------------------------------------------------------- nome ostile
    #
    # `\r\n` in un nome non deve poter aggiungere un destinatario. La difesa sta in
    # `digest.sanitise_field` e non è cambiata; qui serve a verificare che il nome
    # arrivi INTATTO dalla proiezione, così la difesa continui ad avere qualcosa da
    # fare. Sanificarlo qui l'avrebbe resa non verificabile.
    out["hostile"] = _doc([_loc(1, [_room(1, [_rack(1, [
        _dev(1, id="iniezione",
             name="<b>srv-x</b>\r\nBcc: qualcuno@altrove.example",
             garanzia=d(7)),
        _dev(2, id="unicode", name="Núñez — città Ätna 🙂", garanzia=d(7)),
        _dev(3, id="lungo", name="x" * 300, garanzia=d(7)),
    ], id="R01", name="R01")], id="sala-1", nome="Sala 1")], id="sito-1",
        nome="Sito 1")])

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-from", default=date.today().isoformat(),
                    help="data di riferimento, YYYY-MM-DD")
    ap.add_argument("--name", help="un solo corpus invece di tutti")
    args = ap.parse_args()
    all_docs = corpora(date.fromisoformat(args.days_from))
    payload = all_docs[args.name] if args.name else all_docs
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
