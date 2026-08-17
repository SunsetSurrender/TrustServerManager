"""Quali numeri JSON il magazzino delle istantanee può conservare. Puro.

L'invariante che questo modulo difende, e che prima era falsa:

    documento canonico accettato == documento riletto da PostgreSQL

`inventory_versions.doc` è **JSONB**, e JSONB non conserva ogni numero JSON: i
numeri li tiene in `numeric`, che è decimale, non ha il segno dello zero e stampa
sempre in forma piana. Un valore che cambia forma nel viaggio produce un digest
diverso da quello REGISTRATO al salvataggio, e da lì in avanti:

  - il rilevamento del no-op canonico (§8.18) non riconosce più un documento
    identico, e un secondo invio identico crea una versione nuova;
  - il motore di diff (§8.10) attribuisce all'utente una modifica che ha fatto
    PostgreSQL;
  - il confronto dei digest della fase 2B (§8.42) trova l'incoerenza e si rifiuta
    di costruire la proiezione, perché non ha un riferimento di cui fidarsi.

La risposta NON è ricalcolare il digest dopo che PostgreSQL ha cambiato il valore:
sarebbe registrare come «accettato» un documento diverso da quello inviato. Se il
magazzino non può conservare un valore, il documento si **rifiuta prima di
persisterlo**.

Misurato, non ragionato
----------------------
La regola è pura per necessità — gira nel percorso della richiesta, prima di
qualunque accesso al database — ma non è un'approssimazione scritta a mano: un test
su PostgreSQL reale la confronta con il database su un corpus di valori, e se la
regola e PostgreSQL dissentono su uno solo, quel test è rosso. La regola è una
PREVISIONE; l'oracolo è il database.

Che cosa succede a un numero, misurato:

    1e+16, 1e+20, 1.5e+300  →  numeric con scala 0  →  torna INTERO
    -0.0                    →  numeric senza segno  →  torna 0.0
    Infinity, -Infinity, NaN→  jsonb li RIFIUTA (non sono JSON valido)
    10.0                    →  numeric scala 1      →  torna 10.0   ✔
    0.30000000000000004     →  cifre conservate     →  torna uguale ✔
    1e-09, 2.5e-05, 5e-324  →  la scala resta       →  tornano uguali ✔
    interi di ogni misura   →  scala 0              →  tornano interi ✔

Il confronto è sulla SERIALIZZAZIONE, non sul valore. `-0.0 == 0.0` è vero in
Python, e `json.dumps` scrive `-0.0` e `0.0`: due digest diversi. Una verifica
scritta con `==` avrebbe dichiarato fedele proprio il caso che non lo è — ed è
successo, alla prima versione della sonda.

Perché la proiezione può usare `extra` e l'istantanea non può
------------------------------------------------------------
Sono due promesse diverse.

La proiezione (§8.42) è una rappresentazione **derivata**: se una colonna tipizzata
non può contenere un valore, quel valore viaggia in `extra` (JSONB) e il documento
si riassembla identico. `extra` è un ripiego lossless, e la proiezione si può
ricostruire da zero in qualunque momento.

L'istantanea è il **documento stesso**, ed è immutabile e per sempre. Non ha un
`extra` in cui mettere ciò che non entra: è l'unica copia. Un ripiego qui vorrebbe
dire conservare accanto al documento una correzione da riapplicare a ogni lettura —
cioè un secondo formato canonico, e due formati canonici divergono. Perciò per
l'istantanea l'unica risposta corretta è rifiutare in ingresso.

Riferimento: BACKEND-PLAN.md §8.16 (schema congelato) e §8.42.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

#: Codice di validazione stabile. Non cambia: i client lo confrontano.
JSON_NUMBER_NOT_ROUNDTRIPPABLE = "json_number_not_roundtrippable"

#: `numeric` di PostgreSQL arriva a 131072 cifre prima della virgola. Il confronto
#: si fa con una potenza di dieci e NON con `len(str(v))`, perché `str()` su un
#: intero enorme è proprio l'operazione che CPython limita: da Python 3.11
#: convertire un intero di più di 4300 cifre solleva `ValueError`, e la regola
#: sarebbe crollata invece di rispondere. Trovato dalla sonda, sul primo intero
#: mostruoso che le ho dato.
#:
#: In pratica il limite non si raggiunge: `json.loads` rifiuta già un letterale di
#: più di 4300 cifre, quindi un intero così non arriva nemmeno a essere un
#: documento. Il controllo c'è perché questa funzione deve essere TOTALE — non
#: sollevare mai, per nessun valore Python — e un test lo fissa.
_MAX_INT = 10 ** 131072

#: ⚠ La visita del documento e il limite di quanti problemi si elencano stanno in
#: `representable.py`, non qui: sono in comune con la regola sul TESTO
#: (`json_strings.py`). Una visita per regola vorrebbe dire un documento percorso due
#: volte in due modi diversi, e prima o poi una delle due metà scoperta — che è
#: esattamente com'è nato questo problema: la ricerca delle password percorreva solo i
#: valori dei dizionari, e gli elementi delle liste non li guardava nessuno.
#:
#: Qui resta la REGOLA sui numeri, che è la parte specifica.


def is_number(value: Any) -> bool:
    """`True` per i numeri JSON: interi e float, MAI booleani.

    `isinstance(True, int)` è vero in Python, e senza questa distinzione un
    `segnaposto: false` sarebbe un numero da esaminare — con l'esito assurdo di un
    booleano rifiutato come «non rappresentabile». I booleani JSONB li conserva.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def unrepresentable_reason(value: Any) -> str | None:
    """Perché JSONB non può conservare questo numero. `None` se può.

    Totale: qualunque valore Python, nessuna eccezione. Un valore che non è un
    numero non è un problema di questo modulo e risponde `None`.
    """
    if not is_number(value):
        return None

    if isinstance(value, int):
        if abs(value) >= _MAX_INT:
            return ("intero oltre le 131072 cifre che `numeric` può contenere")
        return None

    if not math.isfinite(value):
        # `json.dumps` scrive `Infinity`/`NaN`, che non sono JSON valido: PostgreSQL
        # li rifiuta a sua volta. Ci si arriva davvero, perché `json.loads` ACCETTA
        # quei letterali e converte `1e400` in `inf`. Senza questo ramo il rifiuto
        # arriverebbe dal database, cioè come un 500 al momento dell'inserimento
        # invece di un errore di validazione.
        return "valore non finito: JSONB accetta solo numeri JSON"

    if value == 0.0 and math.copysign(1.0, value) < 0:
        return ("zero negativo: `numeric` non ha il segno dello zero e "
                "restituirebbe 0.0")

    if Decimal(repr(value)).as_tuple().exponent >= 0:
        return ("float senza cifre decimali in notazione esponenziale: `numeric` lo "
                "scrive per esteso con scala 0 e restituirebbe un intero")

    return None


def is_representable(value: Any) -> bool:
    """Comodità: il numero sopravvive al giro attraverso JSONB?"""
    return unrepresentable_reason(value) is None


def describe(value: Any) -> str:
    """Il numero come lo scriverebbe JSON, senza far esplodere niente.

    Serve nel messaggio d'errore: dire *quale* valore è il problema è ciò che rende
    l'errore azionabile, e un numero non è il documento. Un intero enorme però non
    si può nemmeno stampare (il limite di CPython sulle cifre), quindi in quel caso
    si descrive invece di trascriverlo.
    """
    try:
        return repr(value)
    except ValueError:
        return f"<intero da circa {int(value.bit_length() * 0.30103) + 1} cifre>"
