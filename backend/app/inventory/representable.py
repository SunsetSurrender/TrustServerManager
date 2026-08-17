"""L'invariante del magazzino delle istantanee, e l'unica visita che lo verifica.

    Ogni valore E ogni chiave accettati dal `PUT` normale devono essere
    rappresentabili senza perdite da PostgreSQL JSONB, secondo la semantica del
    digest canonico del repository.

Cioè: `documento accettato == documento riletto da PostgreSQL`. Il repository la dava
per vera e non lo era, per due famiglie di valori diverse:

    json_numbers.py   i NUMERI che `numeric` non restituisce come sono arrivati
                      (`-0.0` → `0.0`, `1e+20` → intero): PostgreSQL cambiava il
                      valore in SILENZIO, e il digest registrato non corrispondeva
                      più al documento riletto
    json_strings.py   le STRINGHE che PostgreSQL non accetta affatto (NUL, surrogati
                      spaiati): l'`INSERT` falliva, cioè un 500 a metà del
                      salvataggio invece di un errore di validazione

Non sono due correzioni indipendenti: sono **due implementazioni dello stesso
invariante**, e questo modulo è il posto dove si applicano insieme. Le regole restano
separate perché rispondono a domande diverse su tipi diversi; la visita del documento
è una sola, perché un documento visitato due volte in due modi diversi è un documento
di cui una metà è coperta e l'altra no.

⚠ La visita comprende le CHIAVI
------------------------------
Il modello delle entità è deliberatamente APERTO (§8.42): un campo ignoto sopravvive
al salvataggio e finirà in `extra`. Le chiavi ignote sono **dati dell'utente** come i
valori, e una chiave non rappresentabile fa fallire l'inserimento esattamente come un
valore. Questo passerebbe una validazione che guarda solo i valori:

    {"_uid": "…", "campoNormale": "va bene", "chiave\\u0000rotta": "va bene anche questo"}

⚠ E comprende gli elementi delle liste
-------------------------------------
`document.py::_walk_raw` produce coppie (chiave, valore) e non scende negli elementi
di una lista di scalari: `seriali: ["ok", "a\\u0000b"]` gli sfuggirebbe. Questa visita
scende in tutto: dizionari, liste, liste di liste, e le chiavi a qualsiasi profondità.

⚠ Il percorso non ripete mai il valore incriminato
-------------------------------------------------
Il percorso serve a trovare il campo, e i nomi delle chiavi ci compaiono — come in
tutti gli altri errori di validazione (`unknown_root_key` dice quale chiave). Ma una
chiave NON rappresentabile non si può nemmeno scrivere in un messaggio: al suo posto
va `<chiave n.N>`, che dice dov'è senza riprodurla. Vale anche per i valori annidati
sotto una chiave così.

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from app.inventory.json_numbers import (
    JSON_NUMBER_NOT_ROUNDTRIPPABLE,
    describe,
    is_number,
    unrepresentable_reason as _number_reason,
)
from app.inventory.json_strings import (
    JSON_STRING_NOT_ROUNDTRIPPABLE,
    is_text,
    unrepresentable_text_reason as _text_reason,
)

__all__ = [
    "JSON_NUMBER_NOT_ROUNDTRIPPABLE", "JSON_STRING_NOT_ROUNDTRIPPABLE",
    "MAX_REPORTED", "Unrepresentable", "key_segment", "root_path",
    "unrepresentable_items", "walk_scalars",
]

#: Al massimo tanti problemi elencati in un rifiuto. Un documento con mille valori
#: sbagliati produrrebbe mille errori, cioè il documento inviato restituito
#: all'incontrario: l'errore indica i campi, non ristampa i dati.
MAX_REPORTED = 20

#: Come si chiama la radice in un percorso, quando il valore è il documento stesso.
root_path = "(radice)"

VALUE, KEY = "value", "key"


@dataclass(frozen=True)
class Unrepresentable:
    """Un problema di rappresentabilità, già ripulito per poter essere mostrato.

    `message` non contiene mai il valore esaminato quando è una stringa; per i numeri
    lo contiene, perché un numero non è contenuto dell'utente nello stesso senso — ed
    è ciò che rende l'errore azionabile.
    """

    path: str
    kind: str            # VALUE oppure KEY
    code: str
    message: str

    @property
    def is_key(self) -> bool:
        return self.kind == KEY


def key_segment(index: int) -> str:
    """Il segmento che sostituisce una chiave che non si può scrivere.

    Posizione 1-based nell'oggetto: i dizionari Python conservano l'ordine di
    inserimento, che per un documento appena deserializzato è l'ordine del JSON
    inviato. Dice *quale* chiave senza riprodurla.
    """
    return f"<chiave n.{index + 1}>"


def _join(parent: str, segment: str) -> str:
    return f"{parent}.{segment}" if parent else segment


def _reason(value: Any) -> tuple[str, str] | None:
    """(codice, motivo) se il magazzino non può conservare questo scalare."""
    if is_number(value):
        motivo = _number_reason(value)
        if motivo is not None:
            return JSON_NUMBER_NOT_ROUNDTRIPPABLE, motivo
    elif is_text(value):
        motivo = _text_reason(value)
        if motivo is not None:
            return JSON_STRING_NOT_ROUNDTRIPPABLE, motivo
    return None


def walk_scalars(value: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    """(percorso, VALUE|KEY, scalare) per ogni foglia E ogni chiave del documento.

    Deterministica: due visite dello stesso documento danno la stessa sequenza.

    Il percorso di un valore usa il nome della chiave che lo contiene, tranne quando
    quella chiave non è rappresentabile — in quel caso usa `<chiave n.N>`, così il
    percorso di un valore innocente non finisce per riprodurre una chiave che non si
    può scrivere.
    """
    if isinstance(value, dict):
        for index, (key, sub) in enumerate(value.items()):
            # Una chiave che non si può scrivere non compare nel percorso, e non
            # compare nemmeno nei percorsi dei valori che contiene.
            unwritable = not is_text(key) or _reason(key) is not None
            segment = key_segment(index) if unwritable else key
            yield _join(path, segment), KEY, key
            yield from walk_scalars(sub, _join(path, segment))
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            yield from walk_scalars(sub, f"{path}[{index}]")
    else:
        yield (path or root_path), VALUE, value


def unrepresentable_items(doc: Any) -> tuple[list[Unrepresentable], int]:
    """(problemi, quanti altri non elencati). L'ordine è quello del documento."""
    found: list[Unrepresentable] = []
    remaining = 0

    for path, kind, value in walk_scalars(doc):
        outcome = _reason(value)
        if outcome is None:
            continue
        code, motivo = outcome
        if len(found) >= MAX_REPORTED:
            remaining += 1
            continue

        if code == JSON_NUMBER_NOT_ROUNDTRIPPABLE:
            # Un numero si può nominare: dire *quale* è ciò che rende l'errore
            # azionabile, e non è contenuto dell'utente nel senso in cui lo è un nome.
            dove = "la chiave" if kind == KEY else "il numero"
            message = (
                f"{dove} {describe(value)} non sopravvive al magazzino delle "
                f"istantanee ({motivo}). Un documento accettato deve poter essere "
                f"riletto identico: il digest registrato al salvataggio è ciò che "
                f"riconosce una richiesta ripetuta (§8.18).")
        else:
            # Una stringa NON si nomina mai: né nel messaggio, né nel percorso.
            dove = ("una chiave di oggetto" if kind == KEY
                    else "un valore di testo")
            message = (
                f"{dove} non è rappresentabile dal magazzino delle istantanee "
                f"({motivo}). Il valore non viene riportato di proposito; il percorso "
                f"indica il campo. PostgreSQL rifiuterebbe la scrittura, quindi il "
                f"documento non può essere conservato così com'è (§8.16).")

        found.append(Unrepresentable(path=path, kind=kind, code=code,
                                     message=message))

    return found, remaining
