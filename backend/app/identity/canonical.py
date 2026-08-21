"""Forma canonica del documento di inventario.

Il prototipo tratta l'assenza di certi campi come equivalente a un valore
predefinito: `d.stato || 'attivo'`, `d.h || 1`, `TYPES[d.type] || TYPES.altro`,
`(rk.seriali || [])`. Un dispositivo senza `stato` e uno con `stato: "attivo"`
sono la stessa cosa per l'applicazione e per l'utente.

Senza canonicalizzazione questa equivalenza diventa rumore: un import che
scrive esplicitamente i default produrrebbe un `update` per ogni dispositivo,
un audit pieno di modifiche che non sono modifiche, e uno SHA del seed che
cambia senza che sia cambiato nulla di sostanziale.

Quindi: **canonicalizzare prima di confrontare e prima di calcolare hash**.

Proprietà garantite (verificate dai test):
  - PURA: non modifica l'input, restituisce strutture nuove;
  - IDEMPOTENTE: canonicalise(canonicalise(d)) == canonicalise(d);
  - NON inventa identità: un `_uid` assente resta assente (il backfill è solo
    dello script di migrazione, §8.4);
  - NON inventa `schemaVersion`: un documento senza versione di schema va
    rifiutato, non aggiornato in silenzio (§8.13).

Riferimento: BACKEND-PLAN.md §8.14.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

#: Default per tipo di entità. Ogni voce corrisponde a un `|| default` che
#: l'applicazione già applica in lettura: materializzarlo non cambia il
#: significato del documento, lo rende solo esplicito.
ENTITY_DEFAULTS: dict[str, dict[str, Any]] = {
    "location": {
        "sale": [],
    },
    "room": {
        "racks": [],
        "vani": [],
        "area": "",
        "dim": "",
        "segnaposto": False,
    },
    "rack": {
        "devices": [],
        "seriali": [],
        "u": 45,          # README: "u = unità totali (standard 45)"
        "name": "",
        "row": "",
    },
    "device": {
        "stato": "attivo",   # app: d.stato || 'attivo'
        # ⚠ Aggiunta dalla fase 2G (§8.50). La PRESENZA FISICA è un campo nuovo, e
        # materializzarla come `presente` è la canonicalizzazione dell'assenza
        # richiesta da §10 del requisito: l'inventario di prima non registra le
        # rimozioni, quindi di quelle macchine si sa solo che nessuno ha detto che
        # sono state portate via.
        #
        # Non alza `schemaVersion`, e la ragione è che non ne ha bisogno: un documento
        # senza `presenza` resta INTERPRETABILE, perché l'assenza ha un significato
        # dichiarato. È la stessa condizione di `stato`, `h` e `type`, che hanno
        # sempre avuto un default e non hanno mai richiesto una versione nuova. Alzare
        # `schemaVersion` avrebbe imposto una migrazione del documento a tutti i
        # client per un campo che si può omettere.
        #
        # ⚠ Cambia però il DIGEST canonico del seed: un campo in più per dispositivo.
        # È un cambiamento atteso, e `tools/verify-seed-migration.mjs --update` lo
        # registra dopo che è stato guardato a mano.
        "presenza": "presente",
        "h": 1,              # app: d.h || 1
        "type": "altro",     # app: TYPES[d.type] || TYPES.altro
        "model": "",
        "ip": "",
        "serial": "",
        "owner": "",
        "garanzia": "",
        "supporto": "",
        "note": "",
    },
    "manual": {
        "titolo": "",
        "blocchi": [],
    },
}

#: Default dei sotto-campi delle impostazioni. Si applicano SOLO se l'oggetto
#: esiste già: canonicalizzare non deve inventare `notifiche` o `smtp` in un
#: documento che non li ha mai avuti, altrimenti il primo salvataggio
#: riporterebbe modifiche mai fatte dall'utente.
SETTINGS_DEFAULTS: dict[str, dict[str, Any]] = {
    "notifiche": {"email": "", "giorni": 30, "attive": False},
    # NB: nessun `password`. La password SMTP non vive nel documento (§8.7) e
    # materializzarla come stringa vuota la reintrodurrebbe nello schema.
    "smtp": {"host": "", "porta": 587, "utente": "", "mittente": "", "tls": True},
}


def _apply(obj: Any, defaults: dict[str, Any]) -> dict:
    """Copia con i default applicati ai campi assenti o vuoti.

    «Vuoto» significa None: una stringa vuota o uno zero espliciti sono valori
    dell'utente e restano. `False` resta `False`.
    """
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for key, default in defaults.items():
        if out.get(key) is None:
            out[key] = deepcopy(default)
    return out


def canonicalise(doc: Any) -> Any:
    """Documento in forma canonica. Non modifica l'input."""
    if not isinstance(doc, dict):
        return doc

    out = dict(doc)

    locations = []
    for L in doc.get("locations") or []:
        loc = _apply(L, ENTITY_DEFAULTS["location"])
        rooms = []
        for R in loc.get("sale") or []:
            room = _apply(R, ENTITY_DEFAULTS["room"])
            room["vani"] = [dict(v) for v in (room.get("vani") or [])]
            racks = []
            for K in room.get("racks") or []:
                rack = _apply(K, ENTITY_DEFAULTS["rack"])
                rack["seriali"] = list(rack.get("seriali") or [])
                rack["devices"] = [
                    _apply(V, ENTITY_DEFAULTS["device"]) for V in (rack.get("devices") or [])
                ]
                racks.append(rack)
            room["racks"] = racks
            rooms.append(room)
        loc["sale"] = rooms
        locations.append(loc)
    out["locations"] = locations

    if doc.get("manuale") is not None:
        out["manuale"] = [_apply(M, ENTITY_DEFAULTS["manual"]) for M in doc["manuale"]]

    for key, defaults in SETTINGS_DEFAULTS.items():
        if doc.get(key) is not None:
            out[key] = _apply(doc[key], defaults)

    return out


def canonical_sort(value: Any) -> Any:
    """Ordina ricorsivamente le chiavi. Serve al calcolo di hash stabili."""
    if isinstance(value, list):
        return [canonical_sort(v) for v in value]
    if isinstance(value, dict):
        return {k: canonical_sort(value[k]) for k in sorted(value)}
    return value


def strip_uids(value: Any) -> Any:
    """Rimuove ricorsivamente gli `_uid`. Usato dai controlli sul seed, che
    devono essere indifferenti ai valori casuali generati dalla migrazione."""
    if isinstance(value, list):
        return [strip_uids(v) for v in value]
    if isinstance(value, dict):
        return {k: strip_uids(v) for k, v in value.items() if k != "_uid"}
    return value
