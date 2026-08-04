"""Motore di diff identity-aware.

Confronta due documenti **per `_uid`**, mai per indice di array né per percorso
JSON né per codice di business, e produce eventi di dominio: `add`, `delete`,
`update`, `rename`, `move`, `reorder` (BACKEND-PLAN.md §8.10).

Perché non un diff generico: un diff per percorso vedrebbe uno spostamento di
dispositivo come modifiche sotto due sottoalberi di rack e lo classificherebbe
`structure`, negando a un operatore un'azione che deve poter fare. L'ambito si
può decidere solo conoscendo l'intento, e l'intento lo dà l'identità.

L'output è **deterministico**: gli eventi sono ordinati per
(tipo di entità, tipo di evento, uid, uid del genitore) e i dizionari di
modifiche hanno le chiavi ordinate. Serve all'audit, che deve essere
riproducibile, e ai test.

Puro: nessuna dipendenza da FastAPI o dal database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import (
    EVENT_RANK,
    KIND_RANK,
    SCOPE_BY_KIND,
    SETTINGS_KEYS,
    Entity,
    sibling_groups,
    walk,
)


def _canon(value: Any) -> Any:
    """Ordina ricorsivamente le chiavi dei dizionari.

    I payload degli eventi incorporano sotto-documenti presi dall'input (per
    esempio i `vani` dentro un update di sala). Senza questa normalizzazione
    l'ordine delle chiavi dell'input finirebbe nell'output, e due richieste
    equivalenti produrrebbero JSON di audit diversi. «Deterministico» deve voler
    dire identico byte per byte, non solo semanticamente uguale.
    """
    if isinstance(value, list):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value)}
    return value


@dataclass(frozen=True)
class Event:
    event: str
    entity: str
    scope: str
    uid: Any = None
    parent_uid: Any = None
    path: str | None = None
    changes: dict | None = None
    from_parent_uid: Any = None
    to_parent_uid: Any = None
    from_pos: dict | None = None
    to_pos: dict | None = None
    order_from: list | None = None
    order_to: list | None = None

    def as_dict(self) -> dict:
        """Forma di confronto/serializzazione. `path` è informativo e resta fuori:
        dipende dai codici al momento del diff, quindi non è un'aspettativa
        stabile per i test."""
        d: dict[str, Any] = {"event": self.event, "entity": self.entity,
                             "scope": self.scope, "uid": self.uid}
        if self.event in ("add", "delete"):
            d["parentUid"] = self.parent_uid
        if self.changes is not None:
            d["changes"] = {k: _canon(self.changes[k]) for k in sorted(self.changes)}
        if self.event == "move":
            d["fromParentUid"] = self.from_parent_uid
            d["toParentUid"] = self.to_parent_uid
            d["fromPos"] = _canon(self.from_pos)
            d["toPos"] = _canon(self.to_pos)
        if self.event == "reorder":
            d["parentUid"] = self.parent_uid
            d["from"] = self.order_from
            d["to"] = self.order_to
        return d


def _sort_key(e: Event) -> tuple:
    return (
        KIND_RANK.get(e.entity, 99),
        EVENT_RANK.get(e.event, 99),
        str(e.uid or ""),
        str(e.parent_uid or ""),
    )


def _changed(before: dict, after: dict) -> dict:
    """Differenze campo per campo fra due mappe di attributi.

    Un campo assente da una parte è trattato come None: aggiungere un campo o
    rimuoverlo è una modifica, non un evento a sé.
    """
    out: dict[str, list] = {}
    for k in set(before) | set(after):
        b, a = before.get(k), after.get(k)
        if b != a:
            out[k] = [b, a]
    return out


def diff_documents(base_doc: dict | None, next_doc: dict | None) -> list[Event]:
    """Eventi di dominio fra due documenti, in ordine deterministico.

    Presuppone che `next_doc` abbia superato la validazione dell'identità: senza
    `_uid` univoci il confronto non è definito. Vedi validator.validate_against_base.
    """
    events: list[Event] = []

    base_by_uid = {e.uid: e for e in walk(base_doc) if e.uid is not None}
    next_by_uid = {e.uid: e for e in walk(next_doc) if e.uid is not None}

    # ---- add ----
    for uid, e in next_by_uid.items():
        if uid not in base_by_uid:
            events.append(Event("add", e.kind, e.scope, uid=uid,
                                parent_uid=e.parent_uid, path=e.path))

    # ---- delete ----
    for uid, e in base_by_uid.items():
        if uid not in next_by_uid:
            events.append(Event("delete", e.kind, e.scope, uid=uid,
                                parent_uid=e.parent_uid, path=e.path))

    # ---- entità presenti in entrambi ----
    for uid, new in next_by_uid.items():
        old = base_by_uid.get(uid)
        if old is None:
            continue

        # rename: codice di business o etichetta.
        # Tenuto distinto da update anche se tecnicamente è un cambio di
        # attributo, perché è il caso che rompe l'identità basata sul codice:
        # come evento a sé l'audit dice "è lo stesso oggetto, ha cambiato nome".
        rename_changes: dict[str, list] = {}
        if old.code != new.code:
            rename_changes["id"] = [old.code, new.code]
        if old.label != new.label:
            from .model import LABEL_FIELD
            rename_changes[LABEL_FIELD[new.kind]] = [old.label, new.label]
        if rename_changes:
            events.append(Event("rename", new.kind, new.scope, uid=uid,
                                changes=rename_changes, path=new.path))

        # move: genitore cambiato, oppure posizione cambiata a genitore invariato.
        # Entrambe si leggono come "spostato"; fromPos/toPos distinguono i casi
        # senza moltiplicare i tipi di evento.
        old_pos, new_pos = old.position(), new.position()
        if old.parent_uid != new.parent_uid or (old_pos is not None and old_pos != new_pos):
            events.append(Event("move", new.kind, new.scope, uid=uid,
                                from_parent_uid=old.parent_uid, to_parent_uid=new.parent_uid,
                                from_pos=old_pos, to_pos=new_pos, path=new.path))

        # update: tutto il resto.
        attr_changes = _changed(old.attributes(), new.attributes())
        if attr_changes:
            events.append(Event("update", new.kind, new.scope, uid=uid,
                                changes=attr_changes, path=new.path))

    # ---- reorder ----
    # Si emette SOLO se l'insieme dei fratelli è identico: se ci sono add o
    # delete, l'ordine è cambiato come conseguenza e segnalarlo è rumore.
    base_groups = {(kind, parent): order for kind, parent, order in sibling_groups(base_doc)}
    for kind, parent, new_order in sibling_groups(next_doc):
        old_order = base_groups.get((kind, parent))
        if old_order is None or old_order == new_order:
            continue
        if set(old_order) != set(new_order):
            continue
        events.append(Event("reorder", kind, SCOPE_BY_KIND[kind], uid=None,
                            parent_uid=parent, order_from=list(old_order),
                            order_to=list(new_order)))

    # ---- impostazioni: senza identità, confronto per valore ----
    settings_changes: dict[str, list] = {}
    b, n = base_doc or {}, next_doc or {}
    for key in SETTINGS_KEYS:
        bv, nv = b.get(key), n.get(key)
        if bv == nv:
            continue
        if isinstance(bv, dict) and isinstance(nv, dict):
            for f, pair in _changed(bv, nv).items():
                settings_changes[f"{key}.{f}"] = pair
        else:
            settings_changes[key] = [bv, nv]
    if settings_changes:
        events.append(Event("update", "settings", "settings", uid=None,
                            changes=settings_changes))

    events.sort(key=_sort_key)
    return events


def diff_as_dicts(base_doc: dict | None, next_doc: dict | None) -> list[dict]:
    return [e.as_dict() for e in diff_documents(base_doc, next_doc)]


def scopes_touched(events: list[Event]) -> list[str]:
    """Ambiti coinvolti, ordinati. È ciò che l'autorizzazione confronterà con il
    ruolo (§8.3) e che finisce in audit.scopes (§8.9)."""
    return sorted({e.scope for e in events})
