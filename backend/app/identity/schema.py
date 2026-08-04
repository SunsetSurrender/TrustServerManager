"""Versione di schema del documento.

`schemaVersion` è **la forma del documento**: quali campi esistono e cosa
significano. È una cosa diversa dalla revisione ottimistica dell'inventario
(`inventory_head.version`, §8.11), che conta le modifiche ai *dati*.

Confonderle è un errore facile e costoso: la revisione cresce a ogni salvataggio
e non dice niente su come interpretare il contenuto; la versione di schema
cambia solo quando cambia la struttura, e determina se il documento è
interpretabile senza migrazione.

Regole:
  - un `PUT` normale deve dichiarare `schemaVersion == CURRENT_SCHEMA_VERSION`;
  - un documento senza `schemaVersion`, o con una versione più vecchia, è
    **legacy**: viene rifiutato dal percorso normale e trattato solo da una
    migrazione esplicita (come il backfill degli `_uid`, §8.4);
  - una versione più recente di quella conosciuta viene rifiutata: significa che
    il client è più nuovo del server e non si può indovinare cosa comprende.

Nota sul campo `versione` presente nel seed: era un contatore informale del
prototipo, senza semantica applicata da nessuna parte. Resta dov'è per non
alterare i dati, ma NON è la versione di schema: l'unica autorevole è
`schemaVersion`.

Riferimento: BACKEND-PLAN.md §8.13.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Versione di schema corrente. Da incrementare quando cambia la FORMA del
#: documento, accompagnandola con una migrazione.
CURRENT_SCHEMA_VERSION = 1

#: Versioni per cui esiste una migrazione. `None` rappresenta i documenti che
#: non dichiarano affatto la versione (tutto ciò che precede questo commit).
MIGRATABLE_FROM: tuple[Any, ...] = (None,)

SCHEMA_VERSION_MISSING = "schema_version_missing"
SCHEMA_VERSION_TOO_OLD = "schema_version_too_old"
SCHEMA_VERSION_TOO_NEW = "schema_version_too_new"
SCHEMA_VERSION_INVALID = "schema_version_invalid"


@dataclass(frozen=True)
class SchemaError:
    code: str
    message: str
    found: Any = None
    expected: int = CURRENT_SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "found": self.found, "expected": self.expected}


def check_schema_version(doc: dict | None) -> list[SchemaError]:
    """Errori sulla versione di schema. Lista vuota = documento accettabile dal
    percorso normale."""
    found = (doc or {}).get("schemaVersion")

    if found is None:
        return [SchemaError(
            SCHEMA_VERSION_MISSING,
            "documento senza schemaVersion: precede l'introduzione della versione di "
            "schema. Va migrato una volta con lo script dedicato; il percorso normale "
            "non aggiorna lo schema in silenzio.",
            found=None)]

    if isinstance(found, bool) or not isinstance(found, int):
        return [SchemaError(
            SCHEMA_VERSION_INVALID,
            f"schemaVersion non è un intero: {found!r}", found=found)]

    if found < CURRENT_SCHEMA_VERSION:
        return [SchemaError(
            SCHEMA_VERSION_TOO_OLD,
            f"schemaVersion {found} è più vecchia di {CURRENT_SCHEMA_VERSION}: "
            "serve una migrazione esplicita.", found=found)]

    if found > CURRENT_SCHEMA_VERSION:
        return [SchemaError(
            SCHEMA_VERSION_TOO_NEW,
            f"schemaVersion {found} è più recente di {CURRENT_SCHEMA_VERSION}: il client "
            "conosce una forma di documento che questo server non sa interpretare.",
            found=found)]

    return []


def is_migratable(doc: dict | None) -> bool:
    """Esiste un percorso di migrazione per questo documento?"""
    found = (doc or {}).get("schemaVersion")
    if found == CURRENT_SCHEMA_VERSION:
        return True
    return found in MIGRATABLE_FROM
