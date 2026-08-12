"""Che cosa può diventare una foto di rack: validazione e normalizzazione.

Puro: byte in ingresso, byte in uscita. Nessun database, nessuna richiesta HTTP —
così i casi che contano (un SVG travestito, una bomba di decompressione, un JPEG
troncato) si provano senza tirare su niente.

Tre livelli di sfiducia, e servono tutti e tre
---------------------------------------------
1. **L'estensione del file non si guarda mai.** Non è un controllo: è una stringa
   scelta da chi carica.

2. **Il `Content-Type` del multipart non si crede.** Lo dichiara il client. Si
   usa solo per pretendere che sia COERENTE con ciò che i byte dicono di essere:
   un disaccordo non è un dettaglio da correggere in silenzio, è il sintomo di un
   file che non è quello che sembra.

3. **I byte si annusano e poi si decodificano.** L'intestazione dice il formato;
   una libreria vera dice se l'immagine esiste davvero. Un file che passa il primo
   controllo e non il secondo è malformato, e va rifiutato prima di essere
   conservato — non il giorno in cui un browser prova ad aprirlo.

Perché l'SVG è escluso, e non è pignoleria
------------------------------------------
Un SVG è un documento XML che il browser esegue: può contenere `<script>`, può
fare richieste, e servito dalla nostra origine parlerebbe con la sessione
dell'utente. Serve foto di armadi, e una fotografia non è mai un SVG. Restare su
un elenco chiuso di formati raster è la sola difesa che non dipende dal fatto che
un giorno qualcuno si ricordi di ripulire l'XML.

Perché si ricodifica sempre
---------------------------
I byte conservati sono quelli che ABBIAMO prodotto, non quelli ricevuti. Tre
conseguenze volute:

  - i metadati sparaiscono, GPS compreso: la foto di un rack scattata col
    telefono porta la posizione del CED, che finirebbe in un documento
    scaricabile da chiunque abbia un'utenza di sola lettura;
  - l'orientamento EXIF viene APPLICATO ai pixel, così l'immagine è girata bene
    anche dove quel campo non viene onorato;
  - il tipo dichiarato nella risposta è quello del codificatore che abbiamo
    scelto, quindi non può essere influenzato dal chiamante.

Riferimento: BACKEND-PLAN.md §8.5.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.photos.errors import PhotoRejected, PhotoTooLarge

#: Dimensione massima del file caricato. Dieci megabyte: una fotografia di un
#: armadio scattata con un telefono recente ci sta larga, e il limite tiene fuori
#: chi vuole usare il database come deposito.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: Pixel massimi dell'immagine DECODIFICATA. Il limite sui byte compressi non
#: protegge da niente qui: un PNG di 40 kB può dichiarare 40000×40000 pixel e
#: pretendere sei gigabyte di memoria per essere aperto. Il controllo si fa
#: sull'intestazione, PRIMA di decodificare.
MAX_PIXELS = 40_000_000

#: Formati accettati, per nome di Pillow. Elenco CHIUSO: un formato nuovo si
#: aggiunge di proposito, insieme al codice che lo ricodifica.
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

#: Tipo dichiarabile dal client → formato atteso. Serve solo al confronto di
#: coerenza del punto 2.
DECLARABLE = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",      # scorretto ma diffuso: lo mandano browser e telefoni
    "image/pjpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}

#: Qualità di ricodifica per i formati con perdita. 85 è il punto in cui la
#: differenza non si vede su una foto di rack e il file resta piccolo.
REENCODE_QUALITY = 85


@dataclass(frozen=True)
class NormalisedImage:
    """Il risultato: byte che abbiamo prodotto noi, e come descriverli."""
    mime: str
    data: bytes
    sha256: str
    width: int
    height: int

    @property
    def size_bytes(self) -> int:
        return len(self.data)


# ==================================================================
# 1. i byte, prima di qualunque libreria
# ==================================================================

def sniff(data: bytes) -> str | None:
    """Formato secondo l'INTESTAZIONE dei byte. `None` se non riconosciuta.

    Poche righe fatte a mano invece di affidarsi solo a Pillow: qui si vuole
    riconoscere anche ciò che si rifiuta — un SVG, un HTML — per poterlo dire.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    # RIFF....WEBP: quattro byte di dimensione fra i due marcatori.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in head[:256].lower():
        return "SVG"
    if head[:1] == b"<":
        return "MARKUP"
    return None


def _reject_by_header(sniffed: str | None) -> None:
    """Rifiuti che si possono spiegare bene, con un messaggio per la persona."""
    if sniffed == "SVG":
        raise PhotoRejected(
            "Un SVG non è una fotografia: è un documento che il browser esegue, e "
            "servito dal nostro indirizzo potrebbe agire con la sessione di chi lo "
            "apre. Carica un JPEG, un PNG o un WebP.",
            code="photo_format_not_allowed", detected="svg")
    if sniffed == "MARKUP":
        raise PhotoRejected(
            "Il file caricato è un documento di testo o markup, non un'immagine.",
            code="photo_format_not_allowed", detected="markup")
    if sniffed == "GIF":
        # Non c'è un motivo di prodotto per le GIF su una foto di armadio, e
        # ammetterle vorrebbe dire decidere che farne dei fotogrammi.
        raise PhotoRejected(
            "Le GIF non sono ammesse per le foto dei rack. Usa JPEG, PNG o WebP.",
            code="photo_format_not_allowed", detected="gif")
    if sniffed is None:
        raise PhotoRejected(
            "Il file caricato non è un'immagine in un formato riconosciuto "
            "(ammessi JPEG, PNG, WebP).",
            code="photo_format_not_allowed", detected="unknown")


def _target_mode(img: "Image.Image") -> str:
    """Modalità in cui ricostruire l'immagine: `RGBA`, `L` oppure `RGB`.

    Tre e non dieci: sono quelle che tutti e tre i codificatori sanno scrivere,
    e ridurre a un insieme noto evita di scoprire in produzione che un PNG in
    `CMYK` o in `I;16` non si salva.
    """
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P"
                                            and "transparency" in img.info):
        return "RGBA"
    if img.mode in ("L", "1"):
        return "L"
    return "RGB"


# ==================================================================
# 2. tutto insieme
# ==================================================================

def normalise(data: bytes, declared_mime: str | None = None) -> NormalisedImage:
    """Valida, applica l'orientamento, toglie i metadati, ricodifica.

    Solleva `PhotoTooLarge` o `PhotoRejected`; non restituisce mai byte che non
    siano usciti da un codificatore nostro.
    """
    if len(data) == 0:
        raise PhotoRejected("Il file caricato è vuoto.", code="photo_empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoTooLarge(
            f"L'immagine supera il limite di {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    sniffed = sniff(data)
    if sniffed not in ALLOWED_FORMATS:
        _reject_by_header(sniffed)

    # Coerenza fra ciò che il client dichiara e ciò che i byte sono. Un
    # disaccordo si RIFIUTA invece di essere corretto: correggerlo in silenzio
    # significherebbe accettare volentieri un file mascherato, e l'unico caso in
    # cui la maschera serve è quello in cui non la si vuole.
    if declared_mime:
        expected = DECLARABLE.get(declared_mime.split(";")[0].strip().lower())
        if expected is not None and expected != sniffed:
            raise PhotoRejected(
                "Il tipo dichiarato dal browser non corrisponde al contenuto del "
                "file. Rinominare un file non ne cambia il formato: riesporta "
                "l'immagine nel formato giusto.",
                code="photo_type_mismatch")
        if expected is None:
            raise PhotoRejected(
                "Tipo dichiarato non ammesso: sono accettati JPEG, PNG e WebP.",
                code="photo_format_not_allowed", detected="declared")

    try:
        img = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        # ⚠ Pillow ha una soglia PROPRIA e la applica dentro `Image.open`, prima
        # che il nostro controllo sui pixel possa dire la sua: oltre il doppio del
        # suo `MAX_IMAGE_PIXELS` (circa 179 milioni) solleva qui. Senza questo ramo
        # l'eccezione uscirebbe da `normalise` e diventerebbe un 503 «servizio non
        # disponibile» — cioè un'immagine assurda verrebbe segnalata come un guasto
        # del server invece che come un file rifiutato. Trovato da un test con un
        # PNG che dichiarava 40000×40000.
        raise PhotoRejected(
            f"L'immagine dichiara più di {MAX_PIXELS // 1_000_000} megapixel: "
            "ridimensionala prima di caricarla.",
            code="photo_too_many_pixels") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise PhotoRejected("L'immagine è illeggibile o danneggiata.",
                            code="photo_malformed") from None

    # Pillow legge il formato dall'intestazione: qui i due controlli devono
    # concordare, altrimenti c'è un file con due nature.
    if img.format not in ALLOWED_FORMATS or img.format != sniffed:
        raise PhotoRejected(
            "Il contenuto del file non è un'immagine in un formato ammesso.",
            code="photo_format_not_allowed", detected="decoded")

    # ⚠ PRIMA di decodificare. `Image.open` legge solo l'intestazione, quindi
    # `img.size` è disponibile senza avere ancora allocato niente: è l'unico
    # momento in cui rifiutare una bomba di decompressione costa zero.
    width, height = img.size
    if width <= 0 or height <= 0:
        raise PhotoRejected("L'immagine dichiara dimensioni non valide.",
                            code="photo_malformed")
    if width * height > MAX_PIXELS:
        raise PhotoRejected(
            f"L'immagine dichiara {width}×{height} pixel, oltre il limite di "
            f"{MAX_PIXELS // 1_000_000} megapixel. Ridimensionala prima di "
            "caricarla.",
            code="photo_too_many_pixels")

    # Il formato è quello concordato fra intestazione e decodificatore, fissato
    # ADESSO: `exif_transpose` restituisce un'immagine nuova con `format` a None, e
    # rileggerlo dopo darebbe una risposta diversa.
    fmt = sniffed

    try:
        # L'orientamento EXIF si applica ai PIXEL. Lasciarlo come campo
        # significherebbe dipendere dal fatto che chi visualizza lo onori — e
        # subito dopo lo si cancella insieme al resto dei metadati, quindi
        # nessuno potrebbe più raddrizzare l'immagine.
        img = ImageOps.exif_transpose(img)
        # La modalità si riporta a una delle tre prevedibili prima di ricostruire.
        # Non è ordine: `frombytes` ricostruisce dai byte grezzi e per un'immagine
        # con TAVOLOZZA (`P`, tipica dei PNG) la tavolozza non è nei byte — si
        # otterrebbe un'immagine con gli stessi indici e colori diversi, cioè una
        # foto dai colori sbagliati e nessun errore da nessuna parte.
        target = _target_mode(img)
        img = img.convert(target)
        # Ricostruzione dai soli PIXEL: `frombytes` non porta con sé `info`, che è
        # dove Pillow tiene EXIF, profili ICC, commenti e i blocchi di testo dei
        # PNG. Copiare l'immagine e svuotare `info` sarebbe una cancellazione da
        # ricordarsi; partire dai pixel è una cancellazione per costruzione.
        clean = Image.frombytes(target, img.size, img.tobytes())
    except (OSError, ValueError, MemoryError, Image.DecompressionBombError):
        # Qui ci finisce il file troncato: l'intestazione era buona, i dati no.
        raise PhotoRejected("L'immagine è illeggibile o danneggiata.",
                            code="photo_malformed") from None

    out = io.BytesIO()
    try:
        if fmt == "JPEG":
            # Il JPEG non ha canale alfa: le modalità con trasparenza o con
            # tavolozza vanno convertite, altrimenti il salvataggio solleva.
            if clean.mode not in ("RGB", "L"):
                clean = clean.convert("RGB")
            clean.save(out, format="JPEG", quality=REENCODE_QUALITY, optimize=True)
        elif fmt == "PNG":
            clean.save(out, format="PNG", optimize=True)
        else:
            # WebP: la ricodifica è con perdita anche se l'originale era senza.
            # Per una fotografia è irrilevante, e la scelta resta una: i byte
            # conservati sono i nostri.
            clean.save(out, format="WEBP", quality=REENCODE_QUALITY)
    except (OSError, ValueError) as exc:
        raise PhotoRejected(f"Ricodifica dell'immagine non riuscita: "
                            f"{type(exc).__name__}", code="photo_malformed") from None

    payload = out.getvalue()
    if not payload:
        raise PhotoRejected("Ricodifica dell'immagine non riuscita.",
                            code="photo_malformed")
    # Il limite vale anche in uscita: un PNG senza perdita può crescere
    # ricodificato, e i byte che finiscono nel database sono questi.
    if len(payload) > MAX_UPLOAD_BYTES:
        raise PhotoTooLarge(
            "L'immagine ricodificata supera il limite consentito: riducila o "
            "salvala come JPEG.")

    return NormalisedImage(
        mime=ALLOWED_FORMATS[fmt],
        data=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        width=clean.width,
        height=clean.height,
    )
