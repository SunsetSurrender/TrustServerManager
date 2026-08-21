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

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from app import domain
from app.domain import EXPIRY_KINDS, EXPIRY_LABELS, parse_expiry

#: ⚠ `EXPIRY_KINDS` e le etichette sono RIESPORTATE da `app/domain.py` dalla fase 2G.
#: Erano definite qui, e «qui» era il posto giusto finché lo scanner del documento era
#: l'unico che le usava. Adesso le usano anche la colonna derivata, l'interrogazione
#: SQL e il frontend: due elenchi di tipi di scadenza in due moduli divergono, e il
#: giorno in cui divergessero il worker cercherebbe un campo che la proiezione non
#: interpreta.
#:
#: Etichette per il digest. Riesportate da `app/domain.py` dalla fase 2G: la lingua
#: dell'avviso è una proprietà del dominio, e il frontend mostra le stesse due parole.
KIND_LABEL = EXPIRY_LABELS

#: ⚠ `parse_expiry` NON è più definita qui: dalla fase 2G vive in `app/domain.py`, che
#: è la sede unica della semantica di business (§8.50), e questo modulo la RIESPORTA.
#:
#: Non è un riordino di file. Prima esistevano due interpreti di data — questo e
#: `new Date` del frontend — e sette forme erano visibili nella vista Scadenze e
#: invisibili al worker (§8.48 voce 9). Adesso la funzione è UNA, e i tre chiamanti
#: (lo scanner, la colonna derivata delle date, il frontend) non possono
#: divergere perché non hanno niente da cui divergere. Un test lo pretende
#: sull'IDENTITÀ dell'oggetto, non sul comportamento: due funzioni equivalenti oggi
#: divergono domani, e divergono sui casi limite.
#:
#: Lo spostamento ha anche chiuso un difetto che era qui: `\d` in Python combacia con
#: le cifre decimali UNICODE, quindi `２０２７-０３-１５` era una data per il backend e
#: non per il frontend. Ora la classe è `[0-9]` in entrambi.
__all_reexported__ = ("parse_expiry", "KIND_LABEL")


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


def devices_with_expiries(doc: dict | None) -> Iterator[tuple]:
    """(uid, tipo, data, dispositivo, rack, sala, sito) per ogni scadenza valorizzata.

    ⚠ Scende l'albero, e NON usa il percorso impacchettato di `walk`.

    Fino alla fase 2F questa funzione leggeva `entity.path` — la stringa
    «sito / sala / rack / dispositivo» — e la rispezzava sugli `/`. Da qui due difetti
    che il registro (§8.48) portava alle voci 11 e 12: un id che contiene uno `/`
    veniva troncato (`10.0.0.0/24` diventava `10.0.0.0`) e ogni pezzo dopo di lui
    scalava di un posto; un id ASSENTE arrivava nel digest come la stringa `"None"`,
    perché il percorso era una f-string.

    La fase 2G li chiude entrambi, e il modo di chiuderli è **non costruire mai quella
    stringa**: il contesto è tre valori separati che restano separati fino a chi li
    mostra (§9). È anche ciò che rende questa funzione un ORACOLO utilizzabile per lo
    SQL, che ottiene gli stessi tre valori da tre JOIN.

    ⚠ `notifies` filtra qui, non nel chiamante: un dispositivo `dismesso` non genera
    più promemoria di rinnovo (§7). La vista Scadenze continua a mostrarlo — sono due
    domande diverse, e questa è quella del worker.
    """
    for L in (doc or {}).get("locations") or []:
        location = domain.location_label(L)
        for R in L.get("sale") or []:
            room = domain.room_label(R)
            for K in R.get("racks") or []:
                rack = domain.rack_label(K)
                for V in K.get("devices") or []:
                    uid = V.get("_uid")
                    if not uid or not domain.notifies(V):
                        continue
                    device = domain.device_label(V)
                    for kind in EXPIRY_KINDS:
                        expiry = parse_expiry(V.get(kind))
                        if expiry is not None:
                            yield (str(uid), kind, expiry, device, rack, room, location)


def due_items(doc: dict | None, *, today: date,
              warning_days: list[int]) -> list[DueItem]:
    """Scadenze che rientrano in almeno una finestra di preavviso.

    Regola: `0 <= giorni_rimanenti <= N` per almeno una soglia N. Non
    `giorni_rimanenti == N`, di proposito: pretendere il giorno esatto significherebbe
    che una macchina spenta il giorno del promemoria lo perde per sempre. Il recupero è
    una conseguenza della disuguaglianza, non un meccanismo a parte (§8.41).

    Gli elementi GIÀ SCADUTI restano esclusi: un avviso per una scadenza passata si
    ripeterebbe ogni giorno per sempre, oppure una volta sola e allora quale — è un
    prodotto diverso, e resta fuori. Si guardano nella vista Scadenze.

    ⚠ Dalla fase 2G questa funzione NON è la sorgente del worker: quella è
    `candidates.due_items_from_projection`, che interroga la proiezione (§8.47). Resta
    perché è l'ORACOLO indipendente con cui la parità dello SQL si misura — una
    seconda implementazione dello stesso contratto, scritta in un modo diverso e sopra
    una rappresentazione diversa. Se le due divergono, una delle due ha torto, e il
    test lo dice invece di lasciarlo scoprire a un cliente.
    """
    if not warning_days:
        return []

    out: list[DueItem] = []
    for uid, kind, expiry, device, rack, room, location in devices_with_expiries(doc):
        days = (expiry - today).days
        if not domain.notification_due(days, warning_days):
            continue
        out.append(DueItem(entity_uid=uid, kind=kind, expiry=expiry,
                           days_remaining=days, device=device, rack=rack,
                           room=room, location=location))
    # Ordine deterministico: due esecuzioni sullo stesso inventario devono produrre lo
    # stesso digest, riga per riga.
    out.sort(key=lambda i: (i.days_remaining, i.kind, i.location, i.room,
                            i.rack, i.device, i.entity_uid))
    return out


def applicable_thresholds(days_remaining: int, warning_days: list[int]) -> list[int]:
    """Soglie che coprono questo numero di giorni, dalla più urgente."""
    return sorted(n for n in warning_days if 0 <= days_remaining <= n)
