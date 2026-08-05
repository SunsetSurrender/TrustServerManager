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

Riferimento: BACKEND-PLAN.md §8.16.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.identity import CURRENT_SCHEMA_VERSION, UUID_RE, validate_document
from app.identity.schema import check_schema_version

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

    # ---- dimensione ----
    try:
        size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        return [DocumentError(NOT_AN_OBJECT, f"documento non serializzabile: {exc}")]
    if size > max_bytes:
        errors.append(DocumentError(
            DOCUMENT_TOO_LARGE,
            f"documento di {size} byte, limite {max_bytes}. Ogni versione è una riga "
            f"in append-only: le foto vanno fuori dal documento (§8.5)."))

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
