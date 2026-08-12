"""Quali scadenze sono in finestra, e con quanti giorni di anticipo.

Puro: prende un documento e una data, restituisce dati. Niente database, niente
SMTP, niente orologio proprio — la data «di oggi» arriva da fuori, ed è l'unico
modo per poter provare il cambio dell'ora legale senza aspettarlo.

Il calendario è LOCALE
----------------------
`garanzia` e `supporto` sono valori di business con la sola data. Confrontarli
con la mezzanotte UTC sposterebbe il confine di un giorno per una parte
dell'anno: a Roma (UTC+2 in estate) le 00:30 locali sono ancora «ieri» in UTC, e
un promemoria a 30 giorni scatterebbe il giorno sbagliato. Si usa quindi la data
di calendario nel fuso configurato (§8.41).

Riferimento: BACKEND-PLAN.md §8.41.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from app.identity.model import walk

#: I due campi di scadenza del dispositivo. Sono un elenco chiuso: un campo
#: nuovo va aggiunto qui consapevolmente, non scoperto da un'euristica sui nomi.
EXPIRY_KINDS = ("garanzia", "supporto")

#: Etichette per il digest. Stanno qui perché la lingua dell'avviso è una
#: proprietà del dominio, non della composizione del messaggio.
KIND_LABEL = {"garanzia": "Garanzia", "supporto": "Contratto di supporto"}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass(frozen=True)
class DueItem:
    """Una scadenza in finestra, con il contesto che serve a chi legge l'avviso."""

    entity_uid: str
    kind: str
    expiry: date
    days_remaining: int
    device: str
    rack: str
    room: str
    location: str

    @property
    def key(self) -> tuple:
        """Identità del promemoria, senza la soglia: è il gruppo su cui si
        applica la precedenza fra soglie."""
        return (self.entity_uid, self.kind, self.expiry)


def local_today(now_utc: datetime, tz_name: str) -> date:
    """Data di calendario nel fuso configurato."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(ZoneInfo(tz_name)).date()


def parse_expiry(value: object) -> date | None:
    """`YYYY-MM-DD` → data. Qualunque altra cosa → None, in silenzio.

    Il documento è validato altrove (§8.16) ma questi campi sono testo libero
    dell'utente: una data scritta a mano, un campo vuoto o un residuo di un
    import sono casi normali. Un promemoria non deve fermare il worker perché
    qualcuno ha scritto «in attesa» nel campo garanzia; il posto dove quel
    valore si nota è la vista Scadenze, non un avviso via posta.
    """
    if not isinstance(value, str):
        return None
    m = _ISO_DATE.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _context(path: str) -> tuple[str, str, str]:
    """(location, room, rack) dal percorso costruito da `walk`.

    `walk` compone il percorso come «loc / sala / rack / dispositivo»; qui si
    riusa quello invece di rifare la discesa nell'albero, così i due non possono
    divergere.
    """
    parts = [p.strip() for p in str(path).split("/")]
    while len(parts) < 4:
        parts.append("")
    return parts[0], parts[1], parts[2]


def devices_with_expiries(doc: dict | None) -> Iterator[tuple]:
    """(uid, kind, data, nome, rack, sala, sito) per ogni scadenza valorizzata."""
    for entity in walk(doc):
        if entity.kind != "device" or not entity.uid:
            continue
        location, room, rack = _context(entity.path)
        name = entity.obj.get("name") or entity.obj.get("id") or "(senza nome)"
        for kind in EXPIRY_KINDS:
            expiry = parse_expiry(entity.obj.get(kind))
            if expiry is not None:
                yield (str(entity.uid), kind, expiry, str(name), rack, room, location)


def due_items(doc: dict | None, *, today: date,
              warning_days: list[int]) -> list[DueItem]:
    """Scadenze che rientrano in almeno una finestra di preavviso.

    Regola: `0 <= giorni_rimanenti <= N` per almeno una soglia N. Non
    `giorni_rimanenti == N`, di proposito: pretendere il giorno esatto
    significherebbe che una macchina spenta il giorno del promemoria lo perde per
    sempre. Il recupero è una conseguenza della disuguaglianza, non un
    meccanismo a parte (§8.41).

    Gli elementi GIÀ SCADUTI (`giorni_rimanenti < 0`) sono esclusi. Non è una
    dimenticanza: un avviso per una scadenza già passata è un prodotto diverso —
    si ripete ogni giorno per sempre, o non si ripete? — e questo commit non lo
    decide. Restano visibili nella vista Scadenze, che è dove si guardano.
    """
    if not warning_days:
        return []
    widest = max(warning_days)

    out: list[DueItem] = []
    for uid, kind, expiry, name, rack, room, location in devices_with_expiries(doc):
        days = (expiry - today).days
        if days < 0 or days > widest:
            continue
        out.append(DueItem(entity_uid=uid, kind=kind, expiry=expiry,
                           days_remaining=days, device=name, rack=rack,
                           room=room, location=location))
    # Ordine deterministico: due esecuzioni sullo stesso inventario devono
    # produrre lo stesso digest, riga per riga.
    out.sort(key=lambda i: (i.days_remaining, i.kind, i.location, i.room,
                            i.rack, i.device, i.entity_uid))
    return out


def applicable_thresholds(days_remaining: int, warning_days: list[int]) -> list[int]:
    """Soglie che coprono questo numero di giorni, dalla più urgente."""
    return sorted(n for n in warning_days if 0 <= days_remaining <= n)
