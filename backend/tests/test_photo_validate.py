"""Che cosa può diventare una foto: formati, limiti, metadati.

Puro, senza database e senza HTTP. Le immagini si COSTRUISCONO qui invece di stare
come file binari nel repository: un file binario di fixture non si può leggere in
una revisione, e non si sa più che cosa dimostri.

Ogni caso che riguarda un rifiuto verifica prima che il materiale sia davvero
quello che dice di essere — un test sui metadati che parte da un file senza
metadati passerebbe senza provare niente.
"""
from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from app.photos.errors import PhotoRejected, PhotoTooLarge
from app.photos.validate import (
    MAX_PIXELS,
    MAX_UPLOAD_BYTES,
    normalise,
    sniff,
)


# ==================================================================
# materiale
# ==================================================================

def make_image(fmt: str, size=(40, 30), colour=(200, 30, 30), mode="RGB",
               **save) -> bytes:
    img = Image.new(mode, size, colour if mode in ("RGB", "RGBA") else 128)
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    return buf.getvalue()


def png_declaring(width: int, height: int) -> bytes:
    """Un PNG di poche centinaia di byte che DICHIARA dimensioni enormi.

    È la forma reale di una bomba di decompressione: il file è minuscolo, l'IHDR
    annuncia 40000×40000, e decodificarlo pretenderebbe alcuni gigabyte. Il CRC va
    ricalcolato, altrimenti il decodificatore rifiuta il file per il motivo
    sbagliato e il test passerebbe senza provare il controllo sui pixel.
    """
    base = make_image("PNG", size=(1, 1))
    sig, rest = base[:8], base[8:]
    length = struct.unpack(">I", rest[:4])[0]
    assert rest[4:8] == b"IHDR", "il primo chunk di un PNG è sempre IHDR"
    ihdr = bytearray(rest[8:8 + length])
    ihdr[0:4] = struct.pack(">I", width)
    ihdr[4:8] = struct.pack(">I", height)
    chunk = (struct.pack(">I", length) + b"IHDR" + bytes(ihdr)
             + struct.pack(">I", zlib.crc32(b"IHDR" + bytes(ihdr)) & 0xFFFFFFFF))
    return sig + chunk + rest[8 + length + 4:]


SVG = (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
       b'width="10" height="10"><script>fetch("/api/users")</script></svg>')
HTML = b"<!doctype html>\n<html><body><script>alert(1)</script></body></html>"
GIF = (b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
       b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
       b"\x02\x02D\x01\x00;")


# ==================================================================
# 1. i formati ammessi
# ==================================================================

@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"),
                                      ("PNG", "image/png"),
                                      ("WEBP", "image/webp")])
def test_the_three_allowed_formats_are_accepted(fmt, mime):
    result = normalise(make_image(fmt), mime)
    assert result.mime == mime
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    # I byte conservati sono USCITI da un codificatore nostro, quindi si devono
    # poter riaprire: se non si riaprono, abbiamo salvato spazzatura.
    assert Image.open(io.BytesIO(result.data)).format == fmt


def test_the_stored_mime_comes_from_the_bytes_not_from_the_client():
    """Nessun `declared`: il tipo si ricava comunque, e correttamente."""
    assert normalise(make_image("PNG"), None).mime == "image/png"
    assert normalise(make_image("JPEG"), None).mime == "image/jpeg"


def test_dimensions_are_reported_and_preserved():
    result = normalise(make_image("PNG", size=(64, 48)))
    assert (result.width, result.height) == (64, 48)


# ==================================================================
# 2. i rifiuti
# ==================================================================

def test_an_svg_is_rejected_even_though_it_is_an_image_to_a_browser():
    """Un SVG è un documento che il browser ESEGUE. Servito dalla nostra origine
    parlerebbe con la sessione di chi lo apre, e una fotografia di un armadio non
    è mai un SVG."""
    assert b"<script>" in SVG, "la fixture deve contenere ciò che rende l'SVG pericoloso"
    with pytest.raises(PhotoRejected) as err:
        normalise(SVG, "image/svg+xml")
    assert err.value.code == "photo_format_not_allowed"
    assert err.value.extra.get("detected") == "svg"


def test_html_is_rejected():
    with pytest.raises(PhotoRejected) as err:
        normalise(HTML, "text/html")
    assert err.value.code == "photo_format_not_allowed"


def test_a_gif_is_rejected_because_no_product_reason_admits_it():
    # La GIF è una vera GIF: se non lo fosse, il rifiuto arriverebbe dal ramo
    # «formato ignoto» e il test non proverebbe l'esclusione deliberata.
    assert sniff(GIF) == "GIF"
    with pytest.raises(PhotoRejected) as err:
        normalise(GIF, "image/gif")
    assert err.value.extra.get("detected") == "gif"


def test_arbitrary_binary_is_rejected():
    with pytest.raises(PhotoRejected) as err:
        normalise(b"\x00\x01\x02" * 500, "application/octet-stream")
    assert err.value.code == "photo_format_not_allowed"


def test_an_empty_file_is_rejected():
    with pytest.raises(PhotoRejected) as err:
        normalise(b"", "image/jpeg")
    assert err.value.code == "photo_empty"


def test_a_truncated_image_is_rejected_as_malformed():
    """L'intestazione è buona, i dati no: è il file interrotto a metà upload, non
    un tentativo. Va rifiutato adesso e non il giorno in cui un browser prova ad
    aprirlo."""
    whole = make_image("PNG", size=(200, 200))
    cut = whole[:len(whole) // 3]
    assert sniff(cut) == "PNG", "il troncamento deve conservare l'intestazione"
    with pytest.raises(PhotoRejected) as err:
        normalise(cut, "image/png")
    assert err.value.code == "photo_malformed"


def test_a_file_over_the_size_limit_is_rejected_before_being_decoded():
    payload = b"\xff\xd8\xff" + b"\x00" * MAX_UPLOAD_BYTES
    with pytest.raises(PhotoTooLarge):
        normalise(payload, "image/jpeg")


@pytest.mark.parametrize("side,chi_rifiuta", [
    # 1,6 miliardi di pixel: oltre il doppio del limite interno di Pillow, che
    # solleva dentro `Image.open`. Il ramo che intercetta quel caso esiste perché
    # senza di lui l'eccezione diventava un 503 «servizio non disponibile».
    (40_000, "pillow"),
    # 100 milioni: sotto la soglia di Pillow, sopra la nostra. Qui è il nostro
    # controllo sull'intestazione a rifiutare.
    (10_000, "noi"),
])
def test_a_decompression_bomb_is_rejected_from_the_header(side, chi_rifiuta):
    """Il limite sui byte compressi non protegge da questo: il file è piccolo, ed è
    la DECODIFICA a pretendere gigabyte. Il controllo è sull'intestazione, prima di
    allocare qualsiasi cosa — e i due rami danno lo stesso codice, perché a chi
    carica non interessa quale libreria ha detto no."""
    bomb = png_declaring(side, side)
    assert len(bomb) < 10_000, "una bomba è piccola: se non lo è, non è questo il test"
    assert side * side > MAX_PIXELS
    with pytest.raises(PhotoRejected) as err:
        normalise(bomb, "image/png")
    assert err.value.code == "photo_too_many_pixels", chi_rifiuta


def test_the_pixel_limit_is_a_threshold_not_a_dislike_of_big_images(monkeypatch):
    """Il confine si prova con immagini VERE, abbassando la soglia.

    Costruire un'immagine da 40 megapixel per provare il limite reale costerebbe
    più della soglia stessa; abbassarla prova la disuguaglianza, che è ciò che può
    essere scritto male (`>` invece di `>=`).
    """
    from app.photos import validate as mod
    monkeypatch.setattr(mod, "MAX_PIXELS", 1000)

    # Esattamente al limite: ammessa. Il controllo rifiuta ciò che lo SUPERA.
    assert normalise(make_image("PNG", size=(40, 25)), "image/png").mime == "image/png"

    # Un pixel oltre: rifiutata.
    with pytest.raises(PhotoRejected) as err:
        normalise(make_image("PNG", size=(40, 26)), "image/png")
    assert err.value.code == "photo_too_many_pixels"


# ==================================================================
# 3. il tipo dichiarato non si crede
# ==================================================================

def test_a_spoofed_content_type_is_rejected_not_silently_corrected():
    """Byte PNG dichiarati JPEG. Correggere in silenzio significherebbe accettare
    volentieri un file mascherato, e l'unico caso in cui la maschera serve è
    quello in cui non la si vuole."""
    with pytest.raises(PhotoRejected) as err:
        normalise(make_image("PNG"), "image/jpeg")
    assert err.value.code == "photo_type_mismatch"


def test_a_declared_type_outside_the_allowlist_is_rejected():
    with pytest.raises(PhotoRejected) as err:
        normalise(make_image("PNG"), "image/gif")
    assert err.value.code == "photo_format_not_allowed"


def test_the_common_image_jpg_spelling_is_accepted():
    """`image/jpg` non esiste come tipo registrato, e lo mandano browser e
    telefoni: rifiutarlo sarebbe pedanteria pagata dall'utente."""
    assert normalise(make_image("JPEG"), "image/jpg").mime == "image/jpeg"


def test_a_content_type_with_parameters_is_accepted():
    assert normalise(make_image("PNG"), "image/png; charset=binary").mime == "image/png"


@pytest.mark.parametrize("payload,expected", [
    (b"\xff\xd8\xff\xe0", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "WEBP"),
    (b"GIF87a", "GIF"),
    (b"  <svg ", "SVG"),
    (b"<!doctype html>", "MARKUP"),
    (b"\x00\x00\x00\x00", None),
])
def test_sniff_reads_the_header(payload, expected):
    assert sniff(payload) == expected


# ==================================================================
# 4. metadati e orientamento
# ==================================================================

def _jpeg_with_metadata() -> bytes:
    """Un JPEG con orientamento EXIF 6 (ruotato di 90°) e metadati identificativi.

    L'orientamento 6 significa «ruota di 90° in senso orario per vederla giusta»:
    è quello che produce un telefono tenuto in verticale, ed è il motivo per cui le
    foto arrivano coricate quando qualcuno ignora quel campo.
    """
    img = Image.new("RGB", (40, 20), (10, 200, 10))
    exif = Image.Exif()
    exif[0x0112] = 6                                    # Orientation
    exif[0x010E] = "CED piano -1, armadio 3"            # ImageDescription
    exif[0x0131] = "Fotocamera di prova 1.0"            # Software
    exif[0x013B] = "mario.rossi"                        # Artist
    # GPSInfo come IFD annidato: è il metadato che conta davvero, perché la foto di
    # un rack scattata col telefono porta la posizione del CED.
    exif[0x8825] = {1: "N", 2: (41.0, 54.0, 0.0), 3: "E", 4: (12.0, 29.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_the_fixture_really_carries_exif_and_gps():
    """Prima di provare che i metadati vengono tolti, si prova che c'erano.

    Senza questo, un errore nella costruzione della fixture renderebbe verde il
    test successivo senza che nulla venga mai ripulito.
    """
    original = Image.open(io.BytesIO(_jpeg_with_metadata()))
    exif = original.getexif()
    assert exif[0x0112] == 6
    assert "armadio" in exif[0x010E]
    assert exif.get_ifd(0x8825), "la fixture deve portare dati GPS"


def test_metadata_is_stripped_and_orientation_is_applied():
    result = normalise(_jpeg_with_metadata(), "image/jpeg")
    out = Image.open(io.BytesIO(result.data))

    exif = out.getexif()
    assert dict(exif) == {}, f"metadati sopravvissuti: {dict(exif)}"
    assert not exif.get_ifd(0x8825), "posizione GPS sopravvissuta"

    # L'orientamento è stato applicato AI PIXEL: l'originale è 40×20 con
    # orientamento 6, quindi l'immagine giusta è 20×40. Se restasse un campo,
    # dipenderebbe da chi la visualizza — e il campo lo abbiamo appena cancellato.
    assert (out.width, out.height) == (20, 40)
    assert (result.width, result.height) == (20, 40)


def test_png_text_chunks_do_not_survive():
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "nota interna da non pubblicare")
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="PNG", pnginfo=info)
    original = buf.getvalue()
    assert b"nota interna" in original, "la fixture deve contenere il testo"

    result = normalise(original, "image/png")
    assert b"nota interna" not in result.data


# ==================================================================
# 5. ricodifica: le trappole
# ==================================================================

def test_a_palette_png_keeps_its_colours():
    """⚠ La trappola della ricostruzione dai byte grezzi.

    Un PNG con TAVOLOZZA conserva nei byte degli indici, non dei colori: la
    tavolozza è altrove. Ricostruire l'immagine dai byte senza convertirla prima
    produce gli stessi indici con una tavolozza diversa, cioè una foto dai colori
    sbagliati — e nessun errore da nessuna parte, che è il modo peggiore di
    sbagliare.
    """
    rosso = (220, 20, 60)
    palette = Image.new("P", (16, 16))
    palette.putpalette(list(rosso) + [0, 0, 0] * 255)
    palette.paste(0, (0, 0, 16, 16))
    buf = io.BytesIO()
    palette.save(buf, format="PNG")
    assert Image.open(io.BytesIO(buf.getvalue())).mode == "P"

    result = normalise(buf.getvalue(), "image/png")
    out = Image.open(io.BytesIO(result.data)).convert("RGB")
    r, g, b = out.getpixel((8, 8))
    assert abs(r - rosso[0]) < 12 and abs(g - rosso[1]) < 12 and abs(b - rosso[2]) < 12, \
        f"colore alterato: {(r, g, b)} invece di {rosso}"


def test_transparency_survives_a_png_with_alpha():
    src = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    result = normalise(buf.getvalue(), "image/png")
    out = Image.open(io.BytesIO(result.data))
    assert out.mode in ("RGBA", "LA", "P")
    assert out.getpixel((6, 6))[3] == 0


def test_a_greyscale_jpeg_stays_readable():
    result = normalise(make_image("JPEG", mode="L"), "image/jpeg")
    assert Image.open(io.BytesIO(result.data)).mode in ("L", "RGB")


def test_a_jpeg_with_alpha_source_is_flattened_not_refused():
    """Un PNG con alfa salvato come JPEG solleverebbe: il JPEG non ha canale alfa.
    Qui il formato non cambia (PNG resta PNG), ma la conversione di modalità deve
    reggere comunque il caso."""
    src = Image.new("RGBA", (10, 10), (255, 0, 0, 128))
    buf = io.BytesIO()
    src.save(buf, format="WEBP")
    result = normalise(buf.getvalue(), "image/webp")
    assert result.mime == "image/webp"


# ==================================================================
# 6. contenuto = identità
# ==================================================================

def test_the_same_input_gives_the_same_digest():
    """È ciò su cui poggia la deduplicazione: se la ricodifica non fosse
    deterministica, la stessa foto caricata due volte occuperebbe due righe."""
    src = make_image("PNG", size=(50, 50))
    assert normalise(src).sha256 == normalise(src).sha256


def test_different_images_give_different_digests():
    a = normalise(make_image("PNG", colour=(10, 10, 10)))
    b = normalise(make_image("PNG", colour=(240, 240, 240)))
    assert a.sha256 != b.sha256


def test_the_digest_is_of_the_stored_bytes_not_of_the_upload():
    """L'impronta descrive ciò che c'è nel database. Calcolarla sui byte ricevuti
    farebbe sì che due upload diversi della stessa immagine — uno con metadati, uno
    senza — risultassero due foto distinte con byte identici."""
    import hashlib
    src = _jpeg_with_metadata()
    result = normalise(src, "image/jpeg")
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    assert result.sha256 != hashlib.sha256(src).hexdigest()
    assert result.data != src, "i byte conservati devono essere i nostri"
