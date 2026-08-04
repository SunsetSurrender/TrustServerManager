"""Attraversamento del documento di inventario e vocabolario delle entità.

Puro: nessuna dipendenza da FastAPI, SQLAlchemy o database. Il documento è un
dict come arriva dal client (o come lo produce il seed migrato).

Riferimento: BACKEND-PLAN.md §8.4, §8.10, §8.12.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: Entità con identità propria. I `vani` NON sono qui: sono value object
#: posseduti dalla sala (§8.12). Le voci di manuale invece sì.
KINDS = ("location", "room", "rack", "device", "manual")

#: Ambito di autorizzazione per tipo di entità (§8.3).
SCOPE_BY_KIND = {
    "location": "structure",
    "room": "structure",
    "rack": "structure",
    "device": "devices",
    "manual": "manuale",
    "settings": "settings",
}

#: Campo che porta l'etichetta leggibile, per tipo. Un suo cambiamento è un
#: `rename`, non un `update`: è il caso che romperebbe un'identità per codice.
LABEL_FIELD = {
    "location": "nome",
    "room": "nome",
    "rack": "name",
    "device": "name",
    "manual": "titolo",
}

#: Campi che descrivono la POSIZIONE. Un loro cambiamento è un `move`.
#: Le dimensioni (w, h, u di un rack; h di un dispositivo) NON sono posizione:
#: sono attributi e quindi `update`.
POSITION_FIELDS = {
    "rack": ("x", "y"),
    "device": ("u",),
}

#: Collezioni di figli, escluse dal confronto per attributi del genitore.
CHILD_KEYS = {"location": ("sale",), "room": ("racks",), "rack": ("devices",)}

#: Chiavi di primo livello trattate come impostazioni (senza identità).
SETTINGS_KEYS = ("notifiche", "smtp")

#: Ordinamento deterministico degli eventi.
KIND_RANK = {k: i for i, k in enumerate(("location", "room", "rack", "device", "manual", "settings"))}
EVENT_RANK = {e: i for i, e in enumerate(("add", "delete", "rename", "update", "move", "reorder"))}


def is_uid(value: Any) -> bool:
    return isinstance(value, str) and bool(UUID_RE.match(value))


@dataclass(frozen=True)
class Entity:
    """Una entità identificata, con il contesto che serve al diff."""

    kind: str
    uid: Any
    code: Any
    parent_uid: Any
    obj: dict
    path: str
    index: int          # posizione fra i fratelli, serve al reorder

    @property
    def scope(self) -> str:
        return SCOPE_BY_KIND[self.kind]

    @property
    def label(self) -> Any:
        return self.obj.get(LABEL_FIELD[self.kind])

    def position(self) -> dict | None:
        fields = POSITION_FIELDS.get(self.kind)
        if not fields:
            return None
        return {f: self.obj.get(f) for f in fields}

    def attributes(self) -> dict:
        """Attributi confrontabili: né identità, né codice/etichetta, né
        posizione, né collezioni di figli."""
        skip = {"_uid", "id", LABEL_FIELD[self.kind]}
        skip.update(POSITION_FIELDS.get(self.kind, ()))
        skip.update(CHILD_KEYS.get(self.kind, ()))
        return {k: v for k, v in self.obj.items() if k not in skip}


def walk(doc: dict | None) -> list[Entity]:
    """Tutte le entità identificate, in ordine di documento."""
    out: list[Entity] = []
    d = doc or {}

    for li, L in enumerate(d.get("locations") or []):
        out.append(Entity("location", L.get("_uid"), L.get("id"), None, L,
                          str(L.get("id")), li))
        for ri, R in enumerate(L.get("sale") or []):
            out.append(Entity("room", R.get("_uid"), R.get("id"), L.get("_uid"), R,
                              f"{L.get('id')} / {R.get('id')}", ri))
            for ki, K in enumerate(R.get("racks") or []):
                out.append(Entity("rack", K.get("_uid"), K.get("id"), R.get("_uid"), K,
                                  f"{L.get('id')} / {R.get('id')} / {K.get('id')}", ki))
                for di, V in enumerate(K.get("devices") or []):
                    out.append(Entity(
                        "device", V.get("_uid"), V.get("id"), K.get("_uid"), V,
                        f"{L.get('id')} / {R.get('id')} / {K.get('id')} / {V.get('id')}", di))

    for mi, M in enumerate(d.get("manuale") or []):
        out.append(Entity("manual", M.get("_uid"), M.get("id"), None, M,
                          f"manuale / {M.get('titolo') or M.get('id')}", mi))
    return out


def by_uid(doc: dict | None) -> dict[Any, Entity]:
    return {e.uid: e for e in walk(doc) if e.uid is not None}


def sibling_groups(doc: dict | None) -> Iterator[tuple[str, Any, list[Any]]]:
    """(tipo dei figli, uid del genitore, uid dei figli in ordine).

    `parent_uid` è None per le collezioni di primo livello (locations, manuale).
    """
    d = doc or {}
    yield "location", None, [L.get("_uid") for L in d.get("locations") or []]
    yield "manual", None, [M.get("_uid") for M in d.get("manuale") or []]
    for L in d.get("locations") or []:
        yield "room", L.get("_uid"), [R.get("_uid") for R in L.get("sale") or []]
        for R in L.get("sale") or []:
            yield "rack", R.get("_uid"), [K.get("_uid") for K in R.get("racks") or []]
            for K in R.get("racks") or []:
                yield "device", K.get("_uid"), [V.get("_uid") for V in K.get("devices") or []]
