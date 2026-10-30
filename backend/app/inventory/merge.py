"""Unione a tre vie di due salvataggi concorrenti (0.5.0).

Il caso: Anna e Bruno hanno caricato la versione 41. Anna salva il rack R01 →
versione 42. Bruno salva il rack R07 partendo dalla 41: fino alla 0.4 riceveva un
conflitto e perdeva la modifica, anche se R07 non l'aveva toccato nessuno.

Qui si confrontano tre documenti, tutti canonicalizzati:

    base   la versione da cui è partito chi salva (41)
    mine   quello che manda adesso
    theirs la testa (42)

e si lavora per CHIAVI, ognuna con un valore finale per parte:

    ("e", uid)              un'entità: genitore + attributi propri (senza figli),
                            oppure ASSENTE se cancellata
    ("g", tipo, genitore)   l'elenco ordinato dei figli di quel genitore
    ("r", chiave)           le chiavi della radice che non sono entità

Una chiave è «toccata» da una parte se il suo valore differisce dalla base.
Toccata da entrambe con valori DIVERSI → conflitto, come prima. Toccata da
entrambe con lo STESSO valore → nessun conflitto (succede quando chi salva
rimanda una modifica che la testa contiene già). Altrimenti vince chi l'ha
toccata, e si ricostruisce il documento.

È volutamente prudente: due persone che aggiungono rack alla stessa sala toccano
entrambe l'elenco dei rack di quella sala, e vanno in conflitto. Il caso che si
vuole risolvere — rack diversi, dispositivi in rack diversi — passa.

Il risultato non è fidato per costruzione: il repository lo valida come
qualunque documento in arrivo (identità, modello relazionale, autorizzazione).

Puro: nessuna dipendenza dal database.
"""
from __future__ import annotations

import copy
from typing import Any

from app.identity.model import CHILD_KEYS, sibling_groups, walk

ABSENT = object()

#: Collezione di figli per tipo di genitore, e tipo dei figli.
_CHILD_COLLECTION = {"location": "sale", "room": "racks", "rack": "devices"}
_CHILD_KIND = {"location": "room", "room": "rack", "rack": "device"}
_ROOT_COLLECTION = {"location": "locations", "manual": "manuale"}


class MergeConflict(Exception):
    """Le due modifiche toccano la stessa cosa in modo diverso."""

    def __init__(self, keys: list):
        super().__init__(f"{len(keys)} punti in conflitto")
        self.keys = keys


def _own(kind: str, obj: dict) -> dict:
    skip = set(CHILD_KEYS.get(kind, ()))
    return {k: v for k, v in obj.items() if k not in skip}


def _state(doc: dict) -> tuple[dict, dict]:
    """(valori per chiave, entità per uid con tipo e oggetto)."""
    values: dict = {}
    ents: dict = {}
    for e in walk(doc):
        if e.uid is None:
            raise MergeConflict([("senza-uid", e.kind, e.path)])
        values[("e", e.uid)] = (e.kind, e.parent_uid, _own(e.kind, e.obj))
        ents[e.uid] = e
    for kind, parent, order in sibling_groups(doc):
        values[("g", kind, parent)] = tuple(order)
    for k, v in (doc or {}).items():
        if k not in ("locations", "manuale"):
            values[("r", k)] = v
    return values, ents


def merge(base: dict, mine: dict, theirs: dict) -> dict:
    """Documento con le modifiche di `mine` applicate sopra `theirs`.

    Solleva `MergeConflict` se le due parti toccano la stessa chiave in modo
    diverso o se il risultato non sta in piedi (un figlio senza genitore).
    """
    b, _ = _state(base)
    m, _ = _state(mine)
    t, _ = _state(theirs)

    chosen: dict = {}
    conflicts = []
    for key in set(b) | set(m) | set(t):
        bv, mv, tv = b.get(key, ABSENT), m.get(key, ABSENT), t.get(key, ABSENT)
        mine_touched = mv != bv
        theirs_touched = tv != bv
        if mine_touched and theirs_touched and mv != tv:
            conflicts.append(key)
            continue
        chosen[key] = mv if mine_touched else tv
    if conflicts:
        raise MergeConflict(sorted(conflicts, key=repr))

    # ---- ricostruzione ----
    ents = {k[1]: v for k, v in chosen.items() if k[0] == "e" and v is not ABSENT}

    def figli(kind: str, parent: Any) -> list[dict]:
        order = chosen.get(("g", kind, parent), ABSENT)
        order = list(order) if order is not ABSENT else []
        attesi = {uid for uid, (k, p, _) in ents.items() if k == kind and p == parent}
        if set(order) != attesi or len(order) != len(attesi):
            raise MergeConflict([("figli", kind, parent)])
        out = []
        for uid in order:
            k, _, own = ents[uid]
            obj = copy.deepcopy(own)
            coll = _CHILD_COLLECTION.get(k)
            if coll:
                obj[coll] = figli(_CHILD_KIND[k], uid)
            out.append(obj)
        return out

    doc: dict = {}
    for key, v in chosen.items():
        if key[0] == "r" and v is not ABSENT:
            doc[key[1]] = copy.deepcopy(v)
    for kind, coll in _ROOT_COLLECTION.items():
        items = figli(kind, None)
        if items or coll in (theirs or {}) or coll in (mine or {}):
            doc[coll] = items
    usati = sum(1 for _ in walk(doc))
    if usati != len(ents):
        raise MergeConflict([("orfani", len(ents) - usati)])
    return doc


def touched_racks(base: dict, new: dict) -> set:
    """Rack il cui contenuto cambia fra `base` e `new`: il rack stesso (attributi,
    posizione, cancellazione) o un suo dispositivo (aggiunto, tolto, modificato,
    spostato dentro o fuori). Serve al controllo dei blocchi di modifica."""
    bv, be = _state(base)
    nv, ne = _state(new)
    out: set = set()
    for key in set(bv) | set(nv):
        if bv.get(key, ABSENT) == nv.get(key, ABSENT):
            continue
        if key[0] == "e":
            for side in (bv.get(key, ABSENT), nv.get(key, ABSENT)):
                if side is ABSENT:
                    continue
                kind, parent, _ = side
                if kind == "rack":
                    out.add(key[1])
                elif kind == "device" and parent is not None:
                    out.add(parent)
        elif key[0] == "g" and key[1] == "device" and key[2] is not None:
            out.add(key[2])
    return out
