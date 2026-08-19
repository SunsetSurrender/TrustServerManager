"""Inventario: schema congelato del documento, repository atomico, mappa relazionale.

Nessun endpoint HTTP, nessuna autenticazione: le rotte e le sessioni stanno in
`app/api/` (BACKEND-PLAN.md §8.22).

    document.py             schema congelato del percorso normale (§8.16)
    digest.py               il digest canonico di un documento (§8.11)
    repository.py           scritture atomiche e serializzate (§8.11, §8.44)
    relational.py           mappa pura documento ↔ modello relazionale (§8.42)
    relational_validate.py  coerenza del modello, errori e avvisi
    projection.py           sincronizzazione, rilettura e verifica della proiezione

⚠ Dalla fase 2C (§8.44) `repository.py` SCRIVE due rappresentazioni nella stessa
transazione: l'istantanea JSONB immutabile con la sua storia, e la proiezione
relazionale dello stato corrente. Non esiste uno stato committato in cui una è
avanzata e l'altra no.

⚠ Dalla fase 2D (§8.45) la proiezione è anche ciò che si LEGGE: `GET /api/inventory`
restituisce il documento riassemblato dalle tabelle, dentro uno snapshot
`REPEATABLE READ, READ ONLY`, e solo dopo aver dimostrato che il giro torna. Le
tabelle normalizzate sono lo stato operativo corrente; l'istantanea JSONB immutabile
resta la storia e il giudice della coerenza — non un ripiego automatico.

Restano fuori dalla 2D, e non per dimenticanza: lo scheduler delle notifiche legge
ancora il documento e non le colonne data derivate, non esiste nessun endpoint di
ricerca, e il frontend non sa che la proiezione esista — il contratto del frontend è
il documento (§8.22), e la 2D non lo cambia.

⚠ `projection` NON è riesportata qui, e continua a non esserlo. Riesportarla la
renderebbe raggiungibile con un `from app.inventory import projection` scritto per
distrazione da qualunque modulo che importa questo pacchetto. Chi le serve la importa
per nome — `repository.py`, la readiness, e dalla 2D la rotta dell'inventario — e i
controlli statici sono diventati più stretti, non più larghi: la rotta del `GET` deve
leggere la proiezione e NON deve restituire `inventory_versions.doc`, la readiness può
guardare solo lo stato, il worker non può toccare niente.
"""
from .document import (
    ALLOWED_ROOT_KEYS,
    DOCUMENT_TOO_LARGE,
    EMBEDDED_PASSWORD,
    EMBEDDED_PHOTO_DATA,
    EXTRACTED_ROOT_KEYS,
    FORBIDDEN_ROOT_KEY,
    INVALID_PHOTO_REFERENCE,
    MAX_DOCUMENT_BYTES,
    NUMBER_NOT_ROUNDTRIPPABLE,
    SCHEMA_VERSION_CHANGED,
    UNKNOWN_ROOT_KEY,
    DocumentError,
    strip_legacy_fields,
    validate_normal_document,
)
from .errors import (
    AlreadyBootstrappedError,
    DocumentRejectedError,
    IdentityRejectedError,
    InventoryError,
    NotAuthorizedError,
    NotBootstrappedError,
    ProjectionInconsistentError,
    ProjectionNotCurrentError,
    VersionConflictError,
)
from .repository import (
    MAX_CLIENT_HINT_CHARS,
    Actor,
    InventoryRepository,
    InventorySnapshot,
    SaveResult,
    canonical_sha256,
)

__all__ = [
    "Actor", "InventoryRepository", "InventorySnapshot", "SaveResult",
    "canonical_sha256", "MAX_CLIENT_HINT_CHARS",
    "validate_normal_document", "strip_legacy_fields", "DocumentError",
    "ALLOWED_ROOT_KEYS", "EXTRACTED_ROOT_KEYS", "MAX_DOCUMENT_BYTES",
    "FORBIDDEN_ROOT_KEY", "UNKNOWN_ROOT_KEY", "EMBEDDED_PASSWORD",
    "EMBEDDED_PHOTO_DATA", "INVALID_PHOTO_REFERENCE", "SCHEMA_VERSION_CHANGED",
    "DOCUMENT_TOO_LARGE", "NUMBER_NOT_ROUNDTRIPPABLE",
    "InventoryError", "NotBootstrappedError", "AlreadyBootstrappedError",
    "VersionConflictError", "DocumentRejectedError", "IdentityRejectedError",
    "NotAuthorizedError", "ProjectionNotCurrentError",
    "ProjectionInconsistentError",
]
