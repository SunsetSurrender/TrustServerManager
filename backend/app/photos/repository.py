"""Persistenza delle foto. Immutabili, indirizzate dal contenuto.

Opera su una `Connection` fornita dal chiamante, come il repository
dell'inventario: così l'inserimento di una foto e la scrittura dell'audit stanno
nella stessa transazione senza che questo modulo apra transazioni di nascosto.

Nessun `UPDATE` e nessun `DELETE` qui dentro. La cancellazione fisica esiste in un
solo posto — `app/photos/gc.py`, che gira nel worker con un ruolo di database
diverso — e non c'è nessuna rotta HTTP che la raggiunga (§8.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.photos.errors import PhotoError, PhotoNotFound
from app.photos.validate import NormalisedImage


@dataclass(frozen=True)
class Photo:
    """Metadati di una foto. **Senza i byte**: si leggono solo quando si servono.

    Tenerli fuori non è una micro-ottimizzazione: `list`/`store` finiscono in
    risposte JSON e in righe di audit, e un campo `bytes` in una struttura che
    passa da lì è un incidente che aspetta un `str()` di troppo.
    """
    id: str
    mime: str
    size_bytes: int
    sha256: str
    created_at: datetime


def _photo(row) -> Photo:
    return Photo(id=str(row["id"]), mime=row["mime"],
                 size_bytes=int(row["size_bytes"]), sha256=row["sha256"],
                 created_at=row["created_at"])


def store(conn: Connection, image: NormalisedImage, *,
          uploaded_by=None) -> tuple[Photo, bool]:
    """Conserva l'immagine. Restituisce `(foto, creata)`.

    `creata=False` significa che quei byte c'erano già e si è riusata la riga
    esistente: caricare due volte la stessa foto non raddoppia lo spazio, e non è
    una gentilezza — la stessa immagine di un armadio viene ricaricata ogni volta
    che qualcuno rifà un giro di controllo.

    La deduplicazione la impone il vincolo di unicità su `sha256`, non un
    controllo preventivo: fra il «esiste già?» e l'`INSERT` ci sta un'altra
    richiesta, e allora sarebbe il database a rifiutare con un errore
    incomprensibile.
    """
    row = conn.execute(text("""
        INSERT INTO photos (mime, bytes, sha256, size_bytes, uploaded_by)
        VALUES (:mime, :bytes, :sha, :size, :who)
        ON CONFLICT (sha256) DO NOTHING
     RETURNING id, mime, size_bytes, sha256, created_at
    """), {"mime": image.mime, "bytes": image.data, "sha": image.sha256,
           "size": image.size_bytes, "who": uploaded_by}).mappings().first()
    if row is not None:
        return _photo(row), True

    # Conflitto: la riga c'è. `ON CONFLICT DO NOTHING` ha già atteso l'eventuale
    # transazione concorrente, quindi questa lettura — che sotto READ COMMITTED
    # parte da uno snapshot nuovo — la vede committata.
    existing = conn.execute(text("""
        SELECT id, mime, size_bytes, sha256, created_at
          FROM photos WHERE sha256 = :sha
    """), {"sha": image.sha256}).mappings().first()
    if existing is None:                                    # pragma: no cover
        # Non deve poter accadere: il conflitto dice che la riga esiste. Se accade,
        # è un difetto e va detto, non aggirato inserendo di nuovo.
        raise PhotoError(
            "conflitto su sha256 senza riga corrispondente: stato incoerente",
            code="photo_store_inconsistent")
    return _photo(existing), False


def get_bytes(conn: Connection, photo_id: str) -> tuple[str, bytes]:
    """`(mime, byte)` da servire. Solleva `PhotoNotFound`.

    Il `mime` viene dalla COLONNA, che a sua volta viene dal codificatore che ha
    prodotto quei byte: non c'è nessun percorso per cui il chiamante possa
    influenzare il tipo dichiarato nella risposta (§8.5).
    """
    row = conn.execute(text("SELECT mime, bytes FROM photos WHERE id = :id"),
                       {"id": photo_id}).first()
    if row is None:
        raise PhotoNotFound("foto inesistente")
    return row[0], bytes(row[1])


def get(conn: Connection, photo_id: str) -> Photo:
    row = conn.execute(text("""
        SELECT id, mime, size_bytes, sha256, created_at
          FROM photos WHERE id = :id
    """), {"id": photo_id}).mappings().first()
    if row is None:
        raise PhotoNotFound("foto inesistente")
    return _photo(row)


def existing_ids(conn: Connection, ids: Iterable[str]) -> set[str]:
    """Quali fra questi UUID esistono davvero.

    Il `CAST(... AS uuid[])` è necessario e non decorativo: un elenco di stringhe
    Python arriva come `text[]`, e `uuid = ANY(text[])` non è un operatore che
    esista — l'errore sarebbe un 503 al posto di una validazione.

    Chi chiama garantisce che siano UUID sintatticamente validi: nel percorso
    dell'inventario lo impone `validate_normal_document` (§8.16), nelle rotte il
    controllo sul parametro di percorso.
    """
    wanted: Sequence[str] = list({str(i) for i in ids})
    if not wanted:
        return set()
    rows = conn.execute(
        text("SELECT id FROM photos WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": wanted}).all()
    return {str(r[0]) for r in rows}
