"""Rotte delle foto.

    POST /api/photos        ← multipart, SOLO admin  → { id, mime, sizeBytes, sha256, url }
    GET  /api/photos/{id}   ← sessione non ristretta → i byte, con il tipo del server

**Non esiste `DELETE`**, e non è una dimenticanza (§8.5). Le versioni storiche
dell'inventario referenziano le foto: cancellare i byte trasformerebbe un rollback
in un riquadro rotto, e permetterebbe a un amministratore di rompere la storia di
un altro. Togliere una foto da un rack significa salvare una versione nuova senza
quel riferimento; i byte li libera la garbage collection nel worker, quando
NESSUNA versione conservata li usa più.

Perché il caricamento è riservato agli amministratori
----------------------------------------------------
Le foto appartengono alla gestione dei rack, che è già amministrativa: un operatore
non può creare o spostare un armadio. Se potesse caricare immagini, potrebbe
consumare spazio nel database senza poterle nemmeno collegare a niente — e le foto
non collegate sono esattamente quelle che nessuno vede e nessuno cancella prima
della finestra di grazia.

Caricare NON aggancia
---------------------
    scegli l'immagine → POST /api/photos → UUID
      → l'UUID entra nel documento del rack
        → PUT /api/inventory normale, versionato
          → solo adesso la modifica del rack è salvata

Due richieste, e l'ordine è obbligato: il documento non può referenziare una foto
che non esiste (lo rifiuta il salvataggio, con `photo_not_found`). Se il `PUT`
fallisce o va in conflitto resta una foto orfana, che è previsto: la raccoglie la
GC dopo la finestra di grazia.
"""
from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.engine import Connection

from app.api.deps import get_connection, require_actor, require_admin
from app.api.errors import NO_STORE
from app.audit.sanitize import sanitize
from app.auth.audit import RESULT_SUCCESS, record_auth_event
from app.identity import UUID_RE
from app.inventory import Actor
from app.photos import MAX_UPLOAD_BYTES, PhotoRejected, PhotoTooLarge, normalise
from app.photos import repository as photos
from app.photos.errors import PhotoNotFound

log = logging.getLogger(__name__)

router = APIRouter()

PHOTO_UPLOADED = "photos.uploaded"
PHOTO_REUSED = "photos.deduplicated"

#: Intestazioni di una risposta con i byte di una foto.
#:
#: `immutable` si può dichiarare perché l'identità È il contenuto: l'UUID di una
#: foto non cambierà mai byte, quindi un anno di cache non può servire una versione
#: vecchia di qualcosa. Sostituire la foto di un rack significa un UUID diverso, e
#: quindi una URL diversa: la cache non va invalidata, va semplicemente ignorata.
#:
#: `private` è la parte che conta: sono fotografie di armadi di rete di un cliente,
#: e un proxy aziendale condiviso non deve conservarne una copia servibile a
#: chiunque passi da lì. `public` con `immutable` sarebbe un anno di
#: infrastruttura fotografata in una cache che non controlliamo.
#:
#: `nosniff` perché il tipo lo dichiariamo noi e nessuno deve indovinarlo: senza,
#: un browser che decide da sé che un file è HTML lo eseguirebbe nella nostra
#: origine.
PHOTO_HEADERS = {
    "Cache-Control": "private, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    # Nessun `filename`: il nome del file caricato non viene conservato e non
    # comparirebbe qui in nessun caso. `inline` senza nome dice al browser di
    # mostrarla, e non offre nessun posto in cui infilare testo del chiamante.
    "Content-Disposition": "inline",
}


class PhotoOut(BaseModel):
    id: str
    mime: str
    sizeBytes: int
    sha256: str
    #: SEMPRE generata dal server e SEMPRE relativa. Il client non compone URL di
    #: foto: se lo facesse, il giorno in cui il percorso cambia si romperebbero due
    #: cose in due posti. Relativa anche perché la stessa origine è l'unica
    #: ammessa — una URL assoluta sarebbe un valore da tenere allineato fra
    #: ambienti e un posto in cui un giorno finirebbe un host esterno.
    url: str


def _photo_url(photo_id: str) -> str:
    return f"/api/photos/{photo_id}"


@router.post("/photos", response_model=PhotoOut,
             status_code=status.HTTP_201_CREATED,
             summary="Carica una foto (solo admin). Non la aggancia a niente")
def upload_photo(response: Response,
                 file: UploadFile = File(...),
                 conn: Connection = Depends(get_connection),
                 actor: Actor = Depends(require_admin)) -> PhotoOut:
    response.headers.update(NO_STORE)

    # Il nome del file NON si legge: `file.filename` è testo scelto da chi carica.
    # Non finisce in una colonna, non finisce in un'intestazione, non finisce in un
    # messaggio d'errore e non viene usato per costruire nessun percorso —
    # l'identità della foto è un UUID generato dal database (§8.5).
    #
    # Si legge invece il tipo dichiarato, ma solo per pretendere che sia coerente
    # con i byte: vedi `app/photos/validate.py`.
    declared = (file.content_type or "").strip() or None

    # Lettura con un tetto: `read(n)` invece di `read()`. Una richiesta senza
    # `Content-Length` (chunked) scavalca il controllo del middleware, e senza
    # questo limite il corpo finirebbe tutto in memoria prima di poter essere
    # rifiutato. Si legge UN byte più del massimo, che è quanto basta per sapere
    # che è troppo.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, detail={"code": "photo_too_large",
                         "message": f"l'immagine supera "
                                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"},
            headers=NO_STORE)

    try:
        image = normalise(data, declared)
    except PhotoTooLarge as exc:
        raise HTTPException(413, detail={"code": exc.code, "message": exc.message},
                            headers=NO_STORE) from None
    except PhotoRejected as exc:
        detail = {"code": exc.code, "message": exc.message}
        # `detected` è ricavato dai byte ricevuti, quindi restituirlo non rivela
        # niente del server: dice alla persona *perché* il file è stato rifiutato.
        if exc.extra.get("detected"):
            detail["detected"] = exc.extra["detected"]
        raise HTTPException(422, detail=detail, headers=NO_STORE) from None

    photo, created = photos.store(conn, image, uploaded_by=actor.user_id)

    # Due azioni distinte nel registro: «caricata» e «già presente, riusata». Una
    # sola direbbe che sono stati conservati byte nuovi anche quando non è vero, e
    # chi guarda la crescita del database ne trarrebbe la conclusione sbagliata.
    record_auth_event(
        conn, PHOTO_UPLOADED if created else PHOTO_REUSED,
        username=actor.username, user_id=actor.user_id, role=actor.role,
        ip=actor.ip, result=RESULT_SUCCESS,
        # Solo dati derivati dal server: identificativo, tipo scelto dal nostro
        # codificatore, dimensione dei byte conservati, impronta. Mai il nome del
        # file, mai i metadati EXIF, mai i byte (§8.5).
        detail=sanitize({"photoId": photo.id, "mime": photo.mime,
                         "sizeBytes": photo.size_bytes,
                         "sha256": photo.sha256}))

    log.info("foto %s (%s, %d byte, %s)", photo.id, photo.mime, photo.size_bytes,
             "nuova" if created else "deduplicata")
    return PhotoOut(id=photo.id, mime=photo.mime, sizeBytes=photo.size_bytes,
                    sha256=photo.sha256, url=_photo_url(photo.id))


@router.get("/photos/{photo_id}",
            summary="I byte di una foto (sessione autenticata non ristretta)")
def get_photo(photo_id: str,
              conn: Connection = Depends(get_connection),
              actor: Actor = Depends(require_actor)) -> Response:
    """Qualunque ruolo autenticato può leggere; nessuno può leggere senza sessione.

    `require_actor` porta con sé la restrizione della password provvisoria (§8.26):
    una sessione che deve ancora cambiare password non arriva qui, come non arriva
    a nessun altro endpoint che faccia qualcosa.

    Non esiste una URL pubblica: sono fotografie dell'infrastruttura di un cliente,
    e «tanto l'UUID non è indovinabile» non è controllo d'accesso — è un segreto
    che finisce nella cronologia del browser, nei log di un proxy e in un
    messaggio inoltrato.
    """
    # Il parametro si convalida come UUID PRIMA di andare al database, per due
    # motivi: un valore non-UUID darebbe un errore di tipo di psycopg (503, cioè
    # «colpa nostra» per una richiesta malformata del client), e soprattutto perché
    # nessuna stringa fornita dal chiamante deve poter essere trattata come altro
    # che un identificativo. Qui non si apre nessun file: non esiste un percorso di
    # filesystem derivato da questo valore, e questo controllo lo mantiene vero
    # anche per chi legga il codice domani.
    if not UUID_RE.match(photo_id):
        raise HTTPException(422, detail={"code": "photo_id_malformed",
                                        "message": "identificativo di foto non valido"},
                            headers=NO_STORE)
    try:
        mime, payload = photos.get_bytes(conn, photo_id)
    except PhotoNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"code": "photo_not_found",
                                    "message": "foto inesistente"},
                            headers=NO_STORE) from None

    # `media_type` viene dalla colonna, che viene dal codificatore che ha prodotto
    # questi byte. Non c'è nessun percorso in cui il chiamante scelga il tipo della
    # risposta: è la differenza fra servire un'immagine e servire ciò che qualcuno
    # ha chiamato immagine.
    return Response(content=payload, media_type=mime, headers=dict(PHOTO_HEADERS))
