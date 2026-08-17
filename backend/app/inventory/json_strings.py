"""Quali stringhe JSON il magazzino delle istantanee può conservare. Puro.

Metà testuale dell'invariante di §8.16, gemella di `json_numbers.py`:

    Ogni valore E ogni chiave accettati dal `PUT` normale devono essere
    rappresentabili senza perdite da PostgreSQL JSONB, secondo la semantica del
    digest canonico del repository.

⚠ Il guasto testuale NON somiglia a quello numerico, e la differenza conta
------------------------------------------------------------------------
Con i numeri PostgreSQL **cambiava il valore in silenzio**: `-0.0` diventava `0.0` e
il digest registrato non corrispondeva più. Era un difetto di FEDELTÀ, invisibile
fino al confronto dei digest.

Con le stringhe PostgreSQL **rifiuta**: l'`INSERT` non riesce. Quindi non esistono e
non possono esistere versioni storiche con una stringa così — a differenza dei numeri,
dove i dati scritti prima della correzione restano. Ma senza questa validazione il
rifiuto arriva dal database a metà della transazione di salvataggio, cioè come un
**500** invece di un errore di validazione: l'utente vede «errore del server» per un
carattere in un nome che potrebbe correggere.

Misurato, non ragionato
----------------------
Su un corpus di 34 stringhe provate sia come VALORE sia come CHIAVE (ASCII, italiano
accentato, greco, cirillico, CJK, arabo, emoji BMP e non-BMP, sequenze combinanti,
newline, CR, CRLF, tab, controlli U+0001/U+001F/U+007F, U+2028, BOM, noncaratteri
U+FFFE/U+FFFF/U+FDD0, coppie surrogate valide, piano 16) **sopravvive tutto tranne
due famiglie**:

    "a\\u0000b"   NUL, in qualsiasi posizione
                 → PostgreSQL: DataError / UntranslatableCharacter
                   «unsupported Unicode escape sequence»
    "a\\ud800b"   surrogato spaiato (alto o basso)
                 → psycopg: UnicodeEncodeError, la stringa non è codificabile in UTF-8

I due meccanismi sono diversi e vale la pena saperlo: il NUL lo rifiuta il DATABASE,
il surrogato spaiato lo rifiuta la CODIFICA prima di arrivarci. Per chi salva è la
stessa cosa — il documento non si può conservare — e la regola li tratta insieme.

PostgreSQL **non normalizza** Unicode: una `e` più un accento combinante torna in due
code point, non precomposta. Verificato, perché una normalizzazione silenziosa sarebbe
esattamente il tipo di modifica che questo invariante esiste per escludere.

Due cose che NON sono un problema di rappresentabilità
-----------------------------------------------------
JSONB **riordina le chiavi** e **collassa i duplicati** (`{"b":1,"a":2,"b":3}` torna
`{"a":2,"b":3}`). Nessuna delle due rompe l'invariante: il digest canonico ordina le
chiavi (§8.14), e un oggetto JSON con chiavi duplicate in Python è già collassato dal
parser prima di arrivare qui. Un test lo fissa, perché «il documento torna diverso» e
«il documento torna con un significato diverso» sono due cose distinte.

Non si corregge, non si normalizza, non si ripulisce
---------------------------------------------------
Questo modulo **non tocca** il testo: né `strip`, né normalizzazione Unicode, né
sostituzione dei caratteri non rappresentabili. Ripulire vorrebbe dire salvare un
documento diverso da quello inviato (§8.16), e per il testo sarebbe peggio che per i
numeri: cambiare un nome è una modifica che l'utente vede nel registro attribuita a sé.

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

from typing import Any

#: Codice di validazione stabile. Non cambia: i client lo confrontano.
JSON_STRING_NOT_ROUNDTRIPPABLE = "json_string_not_roundtrippable"

#: Il carattere che PostgreSQL non accetta né in `text` né in `jsonb`.
NUL = "\x00"


def is_text(value: Any) -> bool:
    """`True` per le stringhe JSON. Solo `str`: `bytes` non esiste in JSON."""
    return isinstance(value, str)


def unrepresentable_text_reason(value: Any) -> str | None:
    """Perché il magazzino non può conservare questa stringa. `None` se può.

    Totale: qualunque valore Python, nessuna eccezione, nessuna modifica del testo.
    Un valore che non è una stringa non è un problema di questo modulo e risponde
    `None`.

    Il motivo NON contiene il testo esaminato — solo la sua misura e la posizione del
    carattere colpevole, che sono metadati e non contenuto. È la regola di §8.16: il
    percorso identifica il campo, il valore non torna indietro.
    """
    if not is_text(value):
        return None

    index = value.find(NUL)
    if index >= 0:
        return (f"contiene il byte NUL in posizione {index} di {len(value)} "
                "caratteri: PostgreSQL non lo accetta né in `text` né in `jsonb`")

    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        # Surrogato spaiato: `json.loads('"\\ud800"')` lo produce senza protestare,
        # quindi ci si arriva da una richiesta vera. Qui non si arriva nemmeno al
        # database: è psycopg che non può codificare la stringa in UTF-8.
        return (f"contiene un surrogato spaiato in posizione {exc.start} di "
                f"{len(value)} caratteri: la stringa non è codificabile in UTF-8")

    return None


def is_representable_text(value: Any) -> bool:
    """Comodità: la stringa sopravvive al giro attraverso JSONB?"""
    return unrepresentable_text_reason(value) is None
