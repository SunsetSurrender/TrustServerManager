"""Quali foto usa una versione dell'inventario, dichiarato esplicitamente.

Il documento contiene solo UUID; questa tabella dice **quale versione** contiene
**quale UUID**. Sembra ridondante — l'informazione è nel documento — e non lo è,
per due motivi che hanno conseguenze opposte se si sbaglia:

1. **La GC deve poter chiedere «serve ancora?» in modo esatto.** Con una scansione
   del testo dei documenti (`doc::text LIKE '%uuid%'`) la risposta dipende dalla
   serializzazione, costa una lettura di tutte le versioni, e sbaglia in silenzio
   il giorno in cui un UUID compare in un campo di testo qualsiasi. Con una chiave
   è un `NOT EXISTS` su un indice.

2. **Le versioni STORICHE contano.** Guardare solo l'inventario corrente
   sembrerebbe naturale e sarebbe il difetto peggiore possibile qui:

       v20 → foto A          v21 → foto B (sostituita)

   Con la sola testa, A «non serve più» e la GC ne cancella i byte. Poi qualcuno
   torna alla v20 e trova un riquadro rotto — e i byte non si ricostruiscono. I
   riferimenti storici sono intenzionali, quindi si registrano, e restano
   registrati finché quella versione è conservata.

La camminata è GENERICA, non strutturale
----------------------------------------
Si cerca qualunque chiave `foto` a qualsiasi profondità, non solo
`locations[].sale[].racks[].foto`. La direzione dell'errore lo impone: una
chiave dimenticata dalla camminata strutturale significherebbe un riferimento
non registrato, quindi una foto ancora usata che diventa eleggibile alla
cancellazione. Un riferimento in più costa una riga; uno in meno costa i byte.

Riferimento: BACKEND-PLAN.md §8.5.
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.identity import UUID_RE
from app.photos import repository as photo_repo
from app.photos.errors import PhotoNotFound


def photo_ids(doc: Any) -> list[str]:
    """UUID di foto referenziati dal documento, in ordine deterministico.

    Ordine e unicità servono a rendere confrontabili due chiamate sullo stesso
    documento: le righe che ne derivano finiscono in una chiave primaria, e un
    duplicato sarebbe un errore di scrittura invece di un no-op.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                if (key == "foto" and isinstance(sub, str)
                        and UUID_RE.match(sub)):
                    lowered = sub.lower()
                    if lowered not in seen:
                        seen.add(lowered)
                        found.append(lowered)
                else:
                    walk(sub)
        elif isinstance(value, list):
            for sub in value:
                walk(sub)

    walk(doc)
    return sorted(found)


def require_existing(conn: Connection, ids: Iterable[str]) -> list[str]:
    """Verifica che ogni foto referenziata esista. Solleva `PhotoNotFound`.

    Si controlla PRIMA di scrivere la versione, così il caso normale non dipende
    dal rollback per essere corretto — anche se il rollback c'è comunque: la
    verifica e l'inserimento stanno nella stessa transazione del salvataggio, e un
    candidato che referenzia una foto inesistente non lascia né versione, né
    audit, né testa spostata (§8.11).
    """
    wanted = list(ids)
    if not wanted:
        return []
    present = photo_repo.existing_ids(conn, wanted)
    missing = [i for i in wanted if i not in present]
    if missing:
        raise PhotoNotFound(
            f"il documento referenzia {len(missing)} foto inesistenti: "
            "carica l'immagine prima di salvare il rack",
            missing=missing)
    return wanted


def record(conn: Connection, version: int, ids: Iterable[str]) -> int:
    """Registra i riferimenti di una versione. Restituisce quante righe.

    Nella STESSA transazione della versione, dell'audit e dello spostamento della
    testa: se una qualsiasi delle quattro scritture non riesce, non ne sopravvive
    nessuna. Riferimenti scritti senza la versione (o viceversa) renderebbero la
    GC autorizzata a cancellare byte ancora usati, oppure a non liberarli mai.
    """
    rows = list(ids)
    if not rows:
        return 0
    conn.execute(text("""
        INSERT INTO inventory_photo_refs (inventory_version, photo_id)
        SELECT :v, CAST(x AS uuid) FROM unnest(CAST(:ids AS text[])) AS x
    """), {"v": version, "ids": rows})
    return len(rows)


def versions_using(conn: Connection, photo_id: str) -> list[int]:
    """Le versioni che referenziano questa foto. Per la diagnostica e i test."""
    return [int(r[0]) for r in conn.execute(text("""
        SELECT inventory_version FROM inventory_photo_refs
         WHERE photo_id = CAST(:p AS uuid) ORDER BY inventory_version
    """), {"p": photo_id}).all()]
