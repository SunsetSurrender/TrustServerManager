"""Validazione dell'identità. Rispecchia handoff/identity.js, stessi codici.

Puro: nessuna dipendenza da FastAPI o dal database.

Un `_uid` sconosciuto è ammesso SOLO se corrisponde a un add autentico. Viene
rifiutato quando sostituisce un'entità esistente, riusa il codice di business di
una ancora presente, accompagna la sparizione inspiegata del vecchio `_uid`, o
realizza una sostituzione di identità delete+add (BACKEND-PLAN.md §8.4).

I quattro casi collassano su un unico test verificabile: esiste un'entità della
base, dello stesso tipo e con lo stesso codice, scomparsa o ancora presente?
Se sì, non è un add: è una sostituzione travestita.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Entity, walk

#: Codici di errore stabili. Sono un contratto: le fixture in fixtures/identity/
#: li usano e li usa anche il frontend (handoff/identity.js).
MISSING_UID = "missing_uid"
MALFORMED_UID = "malformed_uid"
DUPLICATE_UID = "duplicate_uid"
IDENTITY_REPLACEMENT = "identity_replacement"
BUSINESS_KEY_REUSE = "business_key_reuse"
AMBIGUOUS_REPLACEMENT = "ambiguous_replacement"


@dataclass(frozen=True)
class IdentityError:
    code: str
    kind: str
    path: str
    message: str
    uid: Any = None
    replaced_uid: Any = None

    def as_dict(self) -> dict:
        d = {"code": self.code, "kind": self.kind, "path": self.path, "message": self.message}
        if self.uid is not None:
            d["uid"] = self.uid
        if self.replaced_uid is not None:
            d["replacedUid"] = self.replaced_uid
        return d


def _key(e: Entity) -> tuple:
    """Chiave di business: tipo + genitore + codice."""
    return (e.kind, e.parent_uid, e.code)


def validate_document(doc: dict | None) -> list[IdentityError]:
    """Controlli che non richiedono un documento di confronto: ogni entità deve
    avere un `_uid` presente, conforme a UUID v4 e univoco."""
    from .model import is_uid

    errors: list[IdentityError] = []
    seen: dict[Any, Entity] = {}

    for e in walk(doc):
        if e.uid is None or e.uid == "":
            errors.append(IdentityError(
                MISSING_UID, e.kind, e.path, f'{e.kind} "{e.path}" senza _uid'))
            continue
        if not is_uid(e.uid):
            errors.append(IdentityError(
                MALFORMED_UID, e.kind, e.path,
                f'{e.kind} "{e.path}" ha un _uid non conforme a UUID: {e.uid}', uid=e.uid))
            continue
        if e.uid in seen:
            errors.append(IdentityError(
                DUPLICATE_UID, e.kind, e.path,
                f'_uid duplicato {e.uid}: "{seen[e.uid].path}" e "{e.path}"', uid=e.uid))
            continue
        seen[e.uid] = e
    return errors


def validate_against_base(base_doc: dict | None, next_doc: dict | None) -> list[IdentityError]:
    """Validazione differenziale: `next_doc` è ammissibile rispetto a `base_doc`?

    Se il documento nuovo non è internamente coerente, si riportano quegli
    errori e si smette: senza identità affidabile il confronto produrrebbe solo
    rumore.
    """
    errors = validate_document(next_doc)
    if errors:
        return errors

    base_entities = walk(base_doc)
    base_by_uid = {e.uid: e for e in base_entities if e.uid is not None}
    base_by_key: dict[tuple, Entity] = {}
    base_by_kind_code: dict[tuple, list[Entity]] = {}
    for e in base_entities:
        base_by_key.setdefault(_key(e), e)
        base_by_kind_code.setdefault((e.kind, e.code), []).append(e)

    next_entities = walk(next_doc)
    next_uids = {e.uid for e in next_entities}

    for e in next_entities:
        if e.uid in base_by_uid:
            continue  # entità già nota: non è un add

        same_key = base_by_key.get(_key(e))
        if same_key is not None:
            old_survives = same_key.uid in next_uids
            if old_survives:
                errors.append(IdentityError(
                    BUSINESS_KEY_REUSE, e.kind, e.path,
                    f'{e.kind} "{e.path}": _uid nuovo {e.uid} su un codice già usato '
                    f"da {same_key.uid}", uid=e.uid, replaced_uid=same_key.uid))
            else:
                errors.append(IdentityError(
                    IDENTITY_REPLACEMENT, e.kind, e.path,
                    f'{e.kind} "{e.path}": sostituzione di identità — {same_key.uid} è '
                    f"scomparso e al suo posto c'è {e.uid} con lo stesso codice. "
                    "Una rinomina deve conservare l'_uid.",
                    uid=e.uid, replaced_uid=same_key.uid))
            continue

        # Stesso codice sotto un altro genitore, con il vecchio _uid svanito:
        # è la stessa sostituzione, mascherata da spostamento.
        gone = [b for b in base_by_kind_code.get((e.kind, e.code), [])
                if b.uid not in next_uids]
        if len(gone) == 1:
            errors.append(IdentityError(
                IDENTITY_REPLACEMENT, e.kind, e.path,
                f'{e.kind} "{e.path}": _uid nuovo {e.uid} mentre {gone[0].uid} '
                f'("{gone[0].path}") con lo stesso codice è scomparso. '
                "Uno spostamento deve conservare l'_uid.",
                uid=e.uid, replaced_uid=gone[0].uid))
        elif len(gone) > 1:
            errors.append(IdentityError(
                AMBIGUOUS_REPLACEMENT, e.kind, e.path,
                f'{e.kind} "{e.path}": _uid nuovo con codice "{e.code}" che corrisponde '
                f"a {len(gone)} entità scomparse. Corrispondenza ambigua: rifiutata.",
                uid=e.uid))
        # nessuna corrispondenza: add autentico, ammesso.

    return errors
