"""Schema congelato del documento di inventario per il percorso normale.

Il `PUT` normale accetta **soltanto** la forma corrente del documento. Tutto ciò
che è stato estratto dal documento verso tabelle proprie — utenze, audit,
impostazioni, SMTP — e tutto ciò che non deve mai finire in un JSONB versionato
— password, foto in base64 — viene **rifiutato**, non ignorato.

Perché rifiutare invece di ripulire in silenzio: uno scarto silenzioso nasconde
un client vecchio, una migrazione dimenticata o un tentativo, e in tutti e tre i
casi si vuole saperlo. Ripulire vorrebbe anche dire salvare un documento diverso
da quello inviato, cioè far divergere ciò che il client crede di avere salvato da
ciò che c'è nel database.

La migrazione legacy **può** consumare e togliere quei campi: è il suo lavoro.
Il repository normale non li persiste mai.

L'invariante del magazzino
--------------------------
    Ogni VALORE e ogni CHIAVE accettati dal `PUT` normale devono essere
    rappresentabili senza perdite da PostgreSQL JSONB, secondo la semantica del
    digest canonico del repository.

Cioè: `documento accettato == documento riletto da PostgreSQL`. Era falsa, in due
modi diversi, e le due correzioni sono due implementazioni dello stesso invariante
(`representable.py` le applica in una visita sola):

    numeri   PostgreSQL cambiava il valore in SILENZIO (`-0.0` → `0.0`,
             `1e+20` → intero) e il digest registrato non corrispondeva più
    testo    PostgreSQL RIFIUTA (NUL, surrogati spaiati), quindi l'`INSERT`
             falliva a metà del salvataggio: un 500 invece di un 422

Da qui il rifiuto in ingresso invece del ricalcolo del digest a posteriori — che
avrebbe registrato come «accettato» un documento diverso da quello inviato — e
invece della ripulitura del testo, che è la stessa cosa fatta a un nome.

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app import domain
from app.identity import CURRENT_SCHEMA_VERSION, UUID_RE, validate_document
from app.identity.model import walk
from app.identity.schema import check_schema_version
from app.inventory.representable import (
    JSON_NUMBER_NOT_ROUNDTRIPPABLE,
    JSON_STRING_NOT_ROUNDTRIPPABLE,
    MAX_REPORTED,
    unrepresentable_items,
)

#: Chiavi ammesse alla radice del documento. Allowlist, non denylist: una chiave
#: nuova va aggiunta di proposito, insieme al codice che la gestisce.
ALLOWED_ROOT_KEYS = frozenset({"schemaVersion", "locations", "manuale"})

#: Radici estratte verso tabelle proprie o legacy del prototipo. Elencate a parte
#: dalle chiavi semplicemente ignote per poter dare un messaggio utile: qui il
#: problema non è «non so cos'è», è «questo non vive più qui».
EXTRACTED_ROOT_KEYS = {
    "users": "le utenze vivono nella tabella users (§8.6)",
    "utenti": "le utenze vivono nella tabella users (§8.6)",
    "audit": "l'audit è lato server, tabella audit (§8.9)",
    "registro": "l'audit è lato server, tabella audit (§8.9)",
    "settings": "le impostazioni vivono nella tabella settings (§8.7)",
    "notifiche": "le impostazioni vivono nella tabella settings (§8.7)",
    "smtp": "la configurazione SMTP vive nella tabella settings; la password in un secret (§8.7)",
    "versione": "contatore informale del prototipo, sostituito da schemaVersion (§8.13)",
}

#: Limite di dimensione del documento serializzato. Non è una micro-ottimizzazione:
#: ogni versione è una riga in append-only, quindi un documento gonfio si moltiplica
#: per il numero di salvataggi. Le foto fuori dal documento (§8.5) tengono questo
#: numero basso.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024

FORBIDDEN_ROOT_KEY = "forbidden_root_key"
UNKNOWN_ROOT_KEY = "unknown_root_key"
EMBEDDED_PASSWORD = "embedded_password"
EMBEDDED_PHOTO_DATA = "embedded_photo_data"
INVALID_PHOTO_REFERENCE = "invalid_photo_reference"
SCHEMA_VERSION_CHANGED = "schema_version_changed"
DOCUMENT_TOO_LARGE = "document_too_large"
NOT_AN_OBJECT = "not_an_object"
#: Fase 2G, chiusura della voce 16 del registro (§8.48). Codice PROPRIO e non un
#: `number_not_roundtrippable` riusato: il numero fa perfettamente il giro in JSONB —
#: è la COLONNA della proiezione che non lo tiene, e chi legge l'errore deve capire
#: che il limite è del prodotto, non del formato.
RACK_U_OUT_OF_RANGE = "rack_u_out_of_range"
#: Riesportati: i codici vivono accanto alle regole che li producono
#: (`json_numbers.py`, `json_strings.py`) e si nominano da qui perché sono errori
#: dello schema congelato come gli altri. Sono due implementazioni dello STESSO
#: invariante — vedi `representable.py`.
NUMBER_NOT_ROUNDTRIPPABLE = JSON_NUMBER_NOT_ROUNDTRIPPABLE
STRING_NOT_ROUNDTRIPPABLE = JSON_STRING_NOT_ROUNDTRIPPABLE

_DATA_URL = re.compile(r"^\s*data:", re.IGNORECASE)


@dataclass(frozen=True)
class DocumentError:
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


def _walk_raw(value: Any, path: str = ""):
    """Ogni coppia (percorso, chiave, valore) del documento, a qualsiasi
    profondità. Serve ai controlli che non dipendono dalla struttura nota:
    una password o una foto in base64 vanno trovate anche se nascoste in un
    ramo che lo schema non prevede."""
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else k
            yield here, k, v
            yield from _walk_raw(v, here)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            here = f"{path}[{i}]"
            yield from _walk_raw(v, here)


def validate_normal_document(
    doc: Any,
    *,
    current_schema_version: int | None = None,
    max_bytes: int = MAX_DOCUMENT_BYTES,
) -> list[DocumentError]:
    """Errori che impediscono di persistere `doc` dal percorso normale.

    `current_schema_version` è la versione dichiarata dal documento attualmente
    in testa: se fornita, il candidato deve dichiarare la stessa. Il client non
    fa evolvere lo schema con un salvataggio (§8.13).
    """
    errors: list[DocumentError] = []

    if not isinstance(doc, dict):
        return [DocumentError(NOT_AN_OBJECT,
                              f"il documento non è un oggetto: {type(doc).__name__}")]

    # ---- rappresentabilità: numeri e testo, valori E chiavi ----
    #
    # ⚠ PRIMA di tutto il resto, compresa la misura, e la ragione è precisa: il
    # calcolo della dimensione serializza il documento in UTF-8, e una stringa con un
    # surrogato spaiato **non è codificabile**. Quel `try` catturerebbe
    # l'`UnicodeEncodeError` (che è una `ValueError`) e restituirebbe
    # `not_an_object` — «il documento non è un oggetto» per un documento che è un
    # oggetto, con la causa vera persa per strada.
    #
    # ⚠ E prima di qualunque accesso al database: un documento rifiutato non deve
    # lasciare stato né aspettare il lock della testa. `save` chiama questa funzione
    # al passo 1 (§8.11), e un test lo prova tenendo il lock da un'altra transazione.
    unrepresentable, remaining = unrepresentable_items(doc)
    for item in unrepresentable:
        errors.append(DocumentError(item.code, item.message, path=item.path))
    if remaining:
        errors.append(DocumentError(
            unrepresentable[0].code,
            f"e altri {remaining} valori non rappresentabili: elencati i primi "
            f"{MAX_REPORTED}. L'errore indica i campi, non ristampa il documento "
            f"inviato."))

    # ---- dimensione ----
    #
    # ⚠ Si misura SOLO se il documento è rappresentabile, e non è pigrizia: misurare
    # vuol dire serializzare in UTF-8, e una stringa con un surrogato spaiato non è
    # codificabile. Su un documento così questo blocco aggiungeva `not_an_object`
    # accanto al codice giusto — «il documento non è un oggetto» per un documento che
    # è un oggetto: due codici per una causa, e uno dei due falso.
    #
    # Un documento che non si può nemmeno serializzare non ha una dimensione da
    # confrontare, e il motivo per cui non si può è già stato riportato con precisione.
    if not unrepresentable:
        try:
            size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            # Resta per i casi che le due regole non coprono: un valore che non è
            # JSON affatto (un `set`, un oggetto qualsiasi), possibile solo da un
            # chiamante interno.
            errors.append(DocumentError(
                NOT_AN_OBJECT,
                f"documento non serializzabile: {type(exc).__name__}"))
        else:
            if size > max_bytes:
                errors.append(DocumentError(
                    DOCUMENT_TOO_LARGE,
                    f"documento di {size} byte, limite {max_bytes}. Ogni versione è "
                    f"una riga in append-only: le foto vanno fuori dal documento "
                    f"(§8.5)."))

    # ---- chiavi di radice ----
    for key in doc:
        if key in ALLOWED_ROOT_KEYS:
            continue
        if key in EXTRACTED_ROOT_KEYS:
            errors.append(DocumentError(
                FORBIDDEN_ROOT_KEY,
                f"la chiave di radice '{key}' non appartiene al documento: "
                f"{EXTRACTED_ROOT_KEYS[key]}", path=key))
        else:
            errors.append(DocumentError(
                UNKNOWN_ROOT_KEY,
                f"chiave di radice non prevista: '{key}'. Ammesse: "
                f"{', '.join(sorted(ALLOWED_ROOT_KEYS))}", path=key))

    # ---- versione di schema ----
    errors.extend(DocumentError(e.code, e.message, path="schemaVersion")
                  for e in check_schema_version(doc))
    if current_schema_version is not None:
        declared = doc.get("schemaVersion")
        if declared != current_schema_version:
            errors.append(DocumentError(
                SCHEMA_VERSION_CHANGED,
                f"il documento dichiara schemaVersion {declared!r} mentre la versione in "
                f"testa è {current_schema_version!r}. Un salvataggio non fa evolvere lo "
                f"schema: serve una migrazione esplicita (§8.13).", path="schemaVersion"))

    # ---- password e foto, a qualsiasi profondità ----
    for path, key, value in _walk_raw(doc):
        lowered = key.lower()
        if lowered == "password" or lowered.endswith("password"):
            errors.append(DocumentError(
                EMBEDDED_PASSWORD,
                f"campo '{key}' nel documento: nessuna credenziale può stare in un JSONB "
                f"versionato, che viene servito ai client e conservato per sempre (§8.7).",
                path=path))
            continue
        if lowered == "foto":
            if isinstance(value, str) and _DATA_URL.match(value):
                errors.append(DocumentError(
                    EMBEDDED_PHOTO_DATA,
                    f"'{key}' contiene un dataURL: le foto stanno nella tabella photos e nel "
                    f"documento si mette il loro id (§8.5). Con il versionamento ogni "
                    f"versione ne duplicherebbe i byte.", path=path))
            elif value is not None and not (isinstance(value, str) and UUID_RE.match(value)):
                errors.append(DocumentError(
                    INVALID_PHOTO_REFERENCE,
                    f"'{key}' deve essere l'id UUID di una foto oppure assente, trovato "
                    f"{type(value).__name__}", path=path))

    # ---- altezza dei rack: il limite dichiarato (§8.48 voce 16) ----
    #
    # QUI e non in `validate_model`, e la differenza non è di comodità.
    # `validate_model` serve a due cose: fare da cancello al `PUT` (passo 9 di
    # `save`) e provare l'INTEGRITÀ della proiezione (`project.py --verify`, la
    # guardia del worker). Un `u` fuori intervallo non è una proiezione rotta — la
    # proiezione conserva il valore in `extra`, fedelmente — è un DOCUMENTO che
    # chiede una cosa che il prodotto non sostiene. Metterlo là avrebbe fatto
    # dichiarare «incoerente» una proiezione sana, e un dato storico avrebbe potuto
    # far rispondere 503 a delle letture che funzionano.
    #
    # Quindi: il cancello vieta di SCRIVERNE di nuovi, l'avviso `carried_verbatim`
    # continua a descrivere quelli che esistessero già.
    for e in walk(doc):
        if e.kind != "rack":
            continue
        u = e.obj.get("u")
        if not domain.rack_height_supported(u):
            errors.append(DocumentError(
                RACK_U_OUT_OF_RANGE,
                f"rack \"{e.path}\": u={u!r} fuori dall'intervallo sostenuto "
                f"[{domain.RACK_U_MIN}, {domain.RACK_U_MAX}]. La colonna della "
                f"proiezione è un `integer`: un valore fuori da lì resterebbe nel "
                f"documento ma non nella tabella, e la capacità calcolata dallo SQL "
                f"non corrisponderebbe più a quella calcolata dal documento "
                f"(§8.48 voce 16).",
                path=e.path))

    # ---- identità (§8.4) ----
    errors.extend(DocumentError(e.code, e.message, path=e.path)
                  for e in validate_document(doc))

    return errors


def strip_legacy_fields(doc: dict) -> tuple[dict, list[str]]:
    """Toglie le radici estratte/legacy. **Solo per la migrazione.**

    Restituisce (documento ripulito, chiavi rimosse). Il repository normale non
    chiama questa funzione: rifiuta. Qui la rimozione è il lavoro previsto,
    perché un documento legacy per definizione contiene quei campi.
    """
    removed = [k for k in doc if k in EXTRACTED_ROOT_KEYS or
               (k not in ALLOWED_ROOT_KEYS and k not in EXTRACTED_ROOT_KEYS)]
    cleaned = {k: v for k, v in doc.items() if k in ALLOWED_ROOT_KEYS}
    cleaned["schemaVersion"] = CURRENT_SCHEMA_VERSION
    return cleaned, sorted(removed)
