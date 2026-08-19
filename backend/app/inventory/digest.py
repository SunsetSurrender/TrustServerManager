"""Il digest canonico di un documento. Funzione PURA, un modulo suo.

Viveva in `repository.py`, e la fase 2C l'ha dovuta spostare: `projection.py` ne ha
bisogno, e da quando è il repository a sincronizzare la proiezione i due si
importerebbero a vicenda. Il ciclo si poteva rompere con un import differito dentro
una funzione — c'è già un precedente nel progetto — ma qui la causa era un'altra:
il digest di un documento non è una cosa del repository. È una proprietà del
documento, e la sua casa è un modulo che non sa né di SQL né di transazioni.

Chi importava `canonical_sha256` da `app.inventory.repository` continua a poterlo
fare: il repository la riesporta.

Riferimento: BACKEND-PLAN.md §8.11, §8.18.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.identity import canonical_sort, canonicalise


def canonical_sha256(doc: Any) -> str:
    """SHA-256 deterministico della forma canonica, **identità inclusa**.

    Canonicalizzare (default materializzati, §8.14) e ordinare le chiavi: due
    documenti che l'applicazione considera equivalenti danno lo stesso digest. È
    così che si riconosce un salvataggio a vuoto senza confrontare interi alberi.

    Gli `_uid` fanno parte del digest, e la ragione è precisa. Da quando il
    confronto di hash precede quello del `baseVersion` (§8.18), l'hash è ciò che
    decide se una richiesta è già stata soddisfatta. Se ignorasse l'identità, un
    documento che sostituisce l'`_uid` di un dispositivo lasciando invariato tutto
    il resto avrebbe lo stesso digest della testa e verrebbe accettato come
    no-op: la sostituzione di identità che §8.4 esiste per rifiutare passerebbe
    in silenzio, con un 200 e changed=False.

    L'identità è parte del significato del documento, quindi è parte del suo
    digest. Il caso «solo gli _uid sono diversi» resta contenuto diverso, e
    prosegue verso la validazione della transizione che lo rifiuta.

    NB: la verifica del seed usa un digest DIVERSO, che gli `_uid` li toglie
    (`tools/verify-seed-migration.mjs`). Là lo scopo è confrontare i dati fra
    rigenerazioni con identità casuali; qui è riconoscere una richiesta ripetuta.
    Due scopi diversi, due digest diversi, e vale la pena non confonderli.
    """
    payload = json.dumps(canonical_sort(canonicalise(doc)),
                         ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
