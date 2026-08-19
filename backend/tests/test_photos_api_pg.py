"""Foto: caricamento, lettura, riferimenti per versione, garbage collection.

PostgreSQL reale, e non è una preferenza. Ciò che questo commit deve dimostrare è
in gran parte comportamento del database:

  - la deduplicazione È un vincolo di unicità su `sha256`;
  - l'atomicità fra versione, audit e riferimenti È una transazione;
  - il fatto che una foto referenziata non si possa cancellare È una chiave
    esterna;
  - il fatto che l'API non possa cancellare byte SONO i privilegi di un ruolo.

Un doppio non proverebbe nessuna delle quattro.
"""
from __future__ import annotations

import io
import os
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth.service import create_user
from app.main import app
from app.photos import gc as photo_gc
from app.photos import refs as photo_refs

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402  (client HTTPS: vedi conftest)

LOC = "aaaaaaaa-0000-4000-8000-0000000000f1"
ROOM = "bbbbbbbb-0000-4000-8000-0000000000f1"
RACK_A = "cccccccc-0000-4000-8000-0000000000fa"
RACK_B = "cccccccc-0000-4000-8000-0000000000fb"

NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)   # 06:00 a Roma

#: UUID sintatticamente valido e inesistente. **Versione 4**, come tutti gli altri
#: identificativi del progetto: `UUID_RE` (§8.4) accetta solo la v4, e le foto le
#: genera `gen_random_uuid()`, che produce v4. Un UUID v1 — per esempio quello
#: dell'esempio di RFC 4122, `6ba7b810-9dad-11d1-…` — viene rifiutato come
#: malformato prima di arrivare al database, ed è coerente: un identificativo che
#: non potremmo aver generato noi non è «non trovato», è sbagliato.
ASSENTE = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
UUID_V1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


# ==================================================================
# materiale
# ==================================================================

def image_bytes(fmt: str = "PNG", colour=(10, 120, 200), size=(30, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt)
    return buf.getvalue()


def document(foto_a=None, foto_b=None, u: int = 42) -> dict:
    """Inventario minimo: due rack, che è quanto serve per sostituire una foto.

    `u` serve a produrre una modifica che NON riguarda le foto: per provocare un
    conflitto di versione servono due salvataggi diversi, e usare la foto stessa
    confonderebbe la causa con l'effetto.
    """
    def rack(uid, ident, x, foto):
        r = {"_uid": uid, "id": ident, "name": ident, "u": u,
             "x": x, "y": 0.2, "w": 0.4, "h": 0.8, "devices": []}
        if foto is not None:
            r["foto"] = foto
        return r

    return {
        "schemaVersion": 1,
        "locations": [{
            "_uid": LOC, "id": "pomezia", "nome": "Pomezia G0",
            "sale": [{
                "_uid": ROOM, "id": "sala-1", "nome": "Sala 1",
                "w": 10, "h": 8, "vani": [],
                "racks": [rack(RACK_A, "R01", 0.1, foto_a),
                          rack(RACK_B, "R02", 0.6, foto_b)],
            }],
        }],
    }


# ==================================================================
# stato
# ==================================================================

@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    eng = create_engine(DSN, future=True)
    command.upgrade(Config("alembic.ini"), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    with engine.begin() as c:
        # `CASCADE` porta via anche `inventory_photo_refs`, che referenzia le
        # versioni: è l'ordine imposto dalle chiavi esterne, non una scorciatoia.
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        # La PROIEZIONE prima delle foto, e l'ordine non è negoziabile (§8.44).
        #
        # Dalla fase 2C il bootstrap popola `inventory_racks`, che ha una chiave
        # esterna verso `photos`: cancellare le foto con i rack ancora in tabella
        # fallisce con una violazione di chiave esterna. Non è un difetto della
        # fixture — è la stessa protezione che impedisce alla GC di cancellare la
        # foto che lo stato corrente sta usando, vista da qui.
        c.execute(text("DELETE FROM inventory_locations"))
        c.execute(text("DELETE FROM inventory_manual_entries"))
        c.execute(text("DELETE FROM photos"))
        c.execute(text("DELETE FROM maintenance_runs"))
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("UPDATE settings SET updated_by = NULL WHERE id = 1"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", "password-lunga-1", "admin", must_change_pw=False)
        create_user(c, "op", "password-lunga-2", "edit", must_change_pw=False)
        create_user(c, "occhi", "password-lunga-3", "view", must_change_pw=False)
        create_user(c, "nuovo", "password-lunga-4", "admin", must_change_pw=True)
    yield engine


def _client(engine, **kw):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    return api_client(app, **kw)


def login(c: TestClient, username: str, password: str) -> None:
    r = c.post("/api/auth/login", headers=ORIGIN,
               json={"username": username, "password": password})
    assert r.status_code == 200, r.text


@pytest.fixture
def anon(db, engine):
    with _client(engine) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, engine):
    with _client(engine) as c:
        login(c, "capo", "password-lunga-1")
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def editor(db, engine):
    with _client(engine) as c:
        login(c, "op", "password-lunga-2")
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def viewer(db, engine):
    with _client(engine) as c:
        login(c, "occhi", "password-lunga-3")
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def temp_password(db, engine):
    with _client(engine) as c:
        login(c, "nuovo", "password-lunga-4")
        yield c
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ aiuti

def upload(c: TestClient, data: bytes, *, mime="image/png", name="rack.png"):
    return c.post("/api/photos", headers=ORIGIN,
                  files={"file": (name, data, mime)})


def upload_ok(c: TestClient, data: bytes, **kw) -> dict:
    r = upload(c, data, **kw)
    assert r.status_code == 201, r.text
    return r.json()


def bootstrap(engine, doc=None) -> int:
    from app.inventory import Actor, InventoryRepository
    with engine.begin() as c:
        return InventoryRepository(c).bootstrap(
            doc or document(), Actor(username="capo", role="admin")).version


def put_inventory(c: TestClient, base_version: int, doc: dict, **kw):
    return c.put("/api/inventory", headers=ORIGIN,
                 json={"baseVersion": base_version, "doc": doc, "action": None},
                 **kw)


def photo_rows(engine) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, mime, size_bytes, sha256, created_at, uploaded_by "
            "FROM photos ORDER BY created_at, id")).mappings()]


def refs(engine) -> list[tuple[int, str]]:
    with engine.begin() as c:
        return [(int(r[0]), str(r[1])) for r in c.execute(text(
            "SELECT inventory_version, photo_id FROM inventory_photo_refs "
            "ORDER BY inventory_version, photo_id")).all()]


def versions(engine) -> list[int]:
    with engine.begin() as c:
        return [int(r[0]) for r in c.execute(text(
            "SELECT version FROM inventory_versions ORDER BY version")).all()]


def audit_rows(engine, like="photos.%") -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT action, actor_username, events FROM audit "
            "WHERE action LIKE :p ORDER BY id"), {"p": like}).mappings()]


def age_photos(engine, hours: int) -> None:
    """Invecchia TUTTE le foto. La finestra di grazia si prova spostando la data
    di creazione, non aspettando ventiquattro ore.

    L'età si calcola rispetto a `NOW`, l'istante iniettato nella GC, e NON rispetto
    a `now()` del database: mescolare un orologio finto e uno vero rende il
    risultato dipendente dall'ora in cui la suite gira, e un test al confine
    diventerebbe verde o rosso a seconda del momento della giornata.
    """
    with engine.begin() as c:
        c.execute(text("UPDATE photos SET created_at = "
                       ":now - make_interval(hours => :h)"),
                  {"now": NOW, "h": hours})


def collect(engine, **kw):
    return photo_gc.run_once(engine, now_utc=NOW, force=True, **kw)


# ==================================================================
# 1. caricamento: chi può
# ==================================================================

def test_an_unauthenticated_upload_is_refused(anon):
    r = upload(anon, image_bytes())
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "not_authenticated"


def test_an_editor_cannot_upload(editor):
    """Un operatore non può creare né spostare un armadio: se potesse caricare
    immagini consumerebbe spazio nel database senza poterle collegare a niente, e
    una foto non collegata è esattamente quella che nessuno vede."""
    r = upload(editor, image_bytes())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden_for_role"


def test_a_viewer_cannot_upload(viewer):
    assert upload(viewer, image_bytes()).status_code == 403


def test_a_temporary_password_session_cannot_upload(temp_password):
    r = upload(temp_password, image_bytes())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


def test_an_admin_uploads_and_gets_a_server_generated_url(admin):
    body = upload_ok(admin, image_bytes("JPEG"), mime="image/jpeg")
    assert body["mime"] == "image/jpeg"
    assert body["sizeBytes"] > 0
    assert len(body["sha256"]) == 64
    # La URL la costruisce il SERVER e è relativa: il client non compone percorsi
    # di foto, e non esiste un host da configurare.
    assert body["url"] == f"/api/photos/{body['id']}"
    assert not body["url"].startswith("http")


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"),
                                      ("PNG", "image/png"),
                                      ("WEBP", "image/webp")])
def test_the_three_formats_round_trip(admin, engine, fmt, mime):
    body = upload_ok(admin, image_bytes(fmt), mime=mime)
    r = admin.get(body["url"])
    assert r.status_code == 200
    assert r.headers["content-type"] == mime
    assert Image.open(io.BytesIO(r.content)).format == fmt


# ==================================================================
# 2. caricamento: che cosa si rifiuta
# ==================================================================

def test_a_spoofed_content_type_is_refused(admin, engine):
    r = upload(admin, image_bytes("PNG"), mime="image/jpeg", name="foto.jpg")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "photo_type_mismatch"
    assert photo_rows(engine) == [], "un rifiuto non deve lasciare byte"


def test_an_svg_is_refused(admin, engine):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'
    r = upload(admin, svg, mime="image/svg+xml", name="disegno.svg")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "photo_format_not_allowed"
    assert detail["detected"] == "svg"
    assert photo_rows(engine) == []


def test_a_malformed_image_is_refused(admin):
    whole = image_bytes("PNG", size=(200, 200))
    r = upload(admin, whole[:len(whole) // 3])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "photo_malformed"


def test_an_oversize_upload_is_refused_with_413(admin, engine):
    from app.photos import MAX_UPLOAD_BYTES
    r = upload(admin, b"\xff\xd8\xff" + b"\x00" * MAX_UPLOAD_BYTES,
               mime="image/jpeg")
    assert r.status_code == 413
    assert r.json()["detail"]["code"] in ("photo_too_large", "request_too_large")
    assert photo_rows(engine) == []


def test_a_decompression_bomb_is_refused(admin, engine):
    import struct
    import zlib
    base = image_bytes("PNG", size=(1, 1))
    length = struct.unpack(">I", base[8:12])[0]
    ihdr = bytearray(base[16:16 + length])
    ihdr[0:4] = struct.pack(">I", 30_000)
    ihdr[4:8] = struct.pack(">I", 30_000)
    chunk = (struct.pack(">I", length) + b"IHDR" + bytes(ihdr)
             + struct.pack(">I", zlib.crc32(b"IHDR" + bytes(ihdr)) & 0xFFFFFFFF))
    bomb = base[:8] + chunk + base[16 + length + 4:]

    r = upload(admin, bomb)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "photo_too_many_pixels"
    assert photo_rows(engine) == []


def test_a_malicious_filename_cannot_reach_headers_or_storage(admin, engine):
    """Il nome del file non si conserva, non si riflette e non costruisce percorsi.

    Il nome contiene un tentativo di attraversamento di directory, un `\\r\\n` per
    spezzare un'intestazione e del markup. Nessuno dei tre ha un posto dove
    arrivare, perché il nome non viene letto: l'identità della foto è un UUID
    generato dal database.
    """
    ostile = '../../etc/passwd\r\nX-Iniettato: si<script>alert(1)</script>'
    body = upload_ok(admin, image_bytes(), name=ostile)

    r = admin.get(body["url"])
    assert r.status_code == 200
    assert "x-iniettato" not in {k.lower() for k in r.headers}
    for value in r.headers.values():
        assert "passwd" not in value and "script" not in value
    # `Content-Disposition` c'è, e non contiene un nome: `inline` non offre nessun
    # posto in cui infilare testo del chiamante.
    assert r.headers["content-disposition"] == "inline"
    assert "filename" not in r.headers["content-disposition"]

    # Né nel database, né nel registro.
    assert all("passwd" not in str(v) for row in photo_rows(engine)
               for v in row.values())
    assert all("passwd" not in str(row["events"]) for row in audit_rows(engine))


# ==================================================================
# 3. deduplicazione
# ==================================================================

def test_the_same_image_twice_is_stored_once(admin, engine):
    data = image_bytes("JPEG", colour=(7, 7, 7))
    first = upload_ok(admin, data, mime="image/jpeg")
    second = upload_ok(admin, data, mime="image/jpeg")

    assert first["id"] == second["id"], "l'identità applicativa è la stessa riga"
    assert first["sha256"] == second["sha256"]
    assert len(photo_rows(engine)) == 1, "caricare due volte non raddoppia i byte"


def test_deduplication_is_audited_as_a_different_action(admin, engine):
    """«Caricata» e «già presente, riusata» sono due fatti diversi. Un'azione sola
    direbbe che sono stati conservati byte nuovi anche quando non è vero, e chi
    guarda la crescita del database ne trarrebbe la conclusione sbagliata."""
    data = image_bytes("PNG", colour=(3, 4, 5))
    upload_ok(admin, data)
    upload_ok(admin, data)
    assert [r["action"] for r in audit_rows(engine)] == ["photos.uploaded",
                                                         "photos.deduplicated"]


def test_the_audit_records_only_server_derived_data(admin, engine):
    body = upload_ok(admin, image_bytes(), name="IMG_20260811_GPS.jpg")
    row = audit_rows(engine)[0]
    assert row["actor_username"] == "capo"
    events = str(row["events"])
    assert body["id"] in events and body["sha256"] in events
    # Mai il nome del file, mai i byte.
    assert "IMG_2026" not in events
    assert "\\x89PNG" not in events and "iVBOR" not in events


def test_two_different_images_are_two_rows(admin, engine):
    a = upload_ok(admin, image_bytes(colour=(1, 1, 1)))
    b = upload_ok(admin, image_bytes(colour=(250, 250, 250)))
    assert a["id"] != b["id"]
    assert len(photo_rows(engine)) == 2


# ==================================================================
# 4. lettura
# ==================================================================

def test_an_unauthenticated_get_is_refused(admin, anon):
    body = upload_ok(admin, image_bytes())
    r = anon.get(body["url"])
    assert r.status_code == 401


def test_a_view_user_may_read(admin, viewer):
    body = upload_ok(admin, image_bytes())
    r = viewer.get(body["url"])
    assert r.status_code == 200
    assert len(r.content) == body["sizeBytes"]


def test_a_temporary_password_session_may_not_read(admin, temp_password):
    body = upload_ok(admin, image_bytes())
    r = temp_password.get(body["url"])
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"


def test_a_nonexistent_uuid_is_404(admin):
    r = admin.get(f"/api/photos/{ASSENTE}")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "photo_not_found"


@pytest.mark.parametrize("bad", ["non-un-uuid", "6ba7b810-9dad-11d1-80b4",
                                 "00000000-0000-0000-0000-00000000000g"])
def test_a_malformed_id_is_rejected_before_the_database(admin, bad):
    """Un identificativo che non è un UUID si rifiuta con un codice proprio.

    Senza questo controllo il valore arriverebbe a psycopg, che solleverebbe un
    errore di tipo: il client riceverebbe 503 «servizio non disponibile» per una
    richiesta malformata sua, cioè un'informazione falsa su chi ha sbagliato.
    """
    r = admin.get(f"/api/photos/{bad}")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "photo_id_malformed"


@pytest.mark.parametrize("attempt", ["%2e%2e%2fetc%2fpasswd", "..%2f..%2fetc%2fpasswd",
                                     "etc%00passwd"])
def test_a_path_traversal_attempt_reads_no_file(admin, attempt):
    """Nessuna stringa del chiamante viene trattata come altro che un
    identificativo: non esiste un percorso di filesystem derivato da questo valore,
    perché i byte stanno in una colonna. Il codice di stato può variare con la
    normalizzazione della URL; ciò che non varia è che non si serve un file.
    """
    r = admin.get(f"/api/photos/{attempt}")
    assert r.status_code != 200, r.text
    assert b"root:" not in r.content


def test_the_response_is_privately_cacheable_and_not_sniffable(admin):
    body = upload_ok(admin, image_bytes())
    r = admin.get(body["url"])
    cache = r.headers["cache-control"]
    # `immutable` si può dichiarare perché l'identità È il contenuto: sostituire la
    # foto di un rack significa un UUID diverso, quindi una URL diversa.
    assert "immutable" in cache and "max-age=31536000" in cache
    # `private` è la parte che conta: un proxy aziendale condiviso non deve
    # conservare una copia servibile a chiunque passi da lì.
    assert "private" in cache and "public" not in cache
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_content_type_ignores_the_uploaded_filename(admin):
    """Byte PNG, dichiarati PNG, ma con estensione `.jpeg` nel nome del file. Il
    tipo della risposta viene dalla colonna — cioè dal codificatore che ha prodotto
    quei byte — e l'estensione non ha nessun percorso per arrivarci."""
    body = upload_ok(admin, image_bytes("PNG"), mime="image/png",
                     name="in-realta-un-jpeg.jpeg")
    assert body["mime"] == "image/png"
    assert admin.get(body["url"]).headers["content-type"] == "image/png"


# ==================================================================
# 5. riferimenti: l'inventario dichiara che cosa usa
# ==================================================================

def test_a_document_referencing_a_missing_photo_is_refused_atomically(admin, engine):
    v1 = bootstrap(engine)
    doc = document(foto_a=ASSENTE)
    r = put_inventory(admin, v1, doc)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "photo_not_found"
    # Nessuna versione, nessun riferimento, nessun audit di salvataggio.
    assert versions(engine) == [v1]
    assert refs(engine) == []
    assert audit_rows(engine, "inventory.save") == []


def test_a_photo_id_that_we_could_not_have_generated_is_a_document_error(admin, engine):
    """Un UUID v1 nel documento non è «foto non trovata»: è un riferimento
    malformato, e il codice lo dice.

    La distinzione non è pedanteria — le due situazioni portano a due azioni
    diverse per chi la vede: «carica prima l'immagine» contro «questo valore non
    viene dalla nostra applicazione». `UUID_RE` accetta solo la v4 (§8.4), e le foto
    le genera `gen_random_uuid()`, che produce v4.
    """
    v1 = bootstrap(engine)
    r = put_inventory(admin, v1, document(foto_a=UUID_V1))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "document_rejected"
    assert any(p["code"] == "invalid_photo_reference" for p in detail["problems"]), detail
    assert versions(engine) == [v1]


def test_saving_a_document_writes_the_reference_rows(admin, engine):
    v1 = bootstrap(engine)
    photo = upload_ok(admin, image_bytes())
    r = put_inventory(admin, v1, document(foto_a=photo["id"]))
    assert r.status_code == 200, r.text
    v2 = r.json()["version"]
    assert refs(engine) == [(v2, photo["id"])]


def test_a_conflicting_save_writes_no_reference_rows(admin, engine):
    """Un `PUT` in conflitto non lascia riferimenti: se li lasciasse, la GC
    riterrebbe viva una foto che nessuna versione non usa — o, peggio, esisterebbero
    riferimenti senza la versione che li dichiara."""
    v1 = bootstrap(engine)
    photo = upload_ok(admin, image_bytes())

    # Qualcun altro salva prima, e cambia qualcosa che con le foto non c'entra.
    v2 = put_inventory(admin, v1, document(u=43)).json()["version"]
    assert v2 > v1

    # Il nostro salvataggio parte da una versione superata: conflitto.
    r = put_inventory(admin, v1, document(foto_a=photo["id"]))
    assert r.status_code == 409, r.text
    assert refs(engine) == []
    assert len(photo_rows(engine)) == 1, "la foto orfana resta, la raccoglie la GC"


def test_a_failed_save_leaves_the_orphan_upload_alone(admin, engine):
    """Il caricamento e il salvataggio sono due richieste. Se la seconda fallisce
    resta una foto orfana, ed è previsto: non si cancella niente qui — lo fa la GC
    dopo la finestra di grazia, che è il solo modo di distinguere «orfana per
    sempre» da «orfana per trenta secondi»."""
    v1 = bootstrap(engine)
    photo = upload_ok(admin, image_bytes())
    r = put_inventory(admin, v1 + 99, document(foto_a=photo["id"]))
    assert r.status_code == 409, r.text
    assert len(photo_rows(engine)) == 1, "la foto resta"
    assert refs(engine) == []


def test_replacing_a_photo_keeps_both(admin, engine):
    """v20 → A, v21 → B. Entrambe vive, perché entrambe servono a una versione
    conservata. È il caso che una GC basata sul solo inventario CORRENTE
    romperebbe."""
    v1 = bootstrap(engine)
    a = upload_ok(admin, image_bytes(colour=(200, 10, 10)))
    b = upload_ok(admin, image_bytes(colour=(10, 10, 200)))

    v2 = put_inventory(admin, v1, document(foto_a=a["id"])).json()["version"]
    v3 = put_inventory(admin, v2, document(foto_a=b["id"])).json()["version"]

    assert refs(engine) == sorted([(v2, a["id"]), (v3, b["id"])])
    with engine.begin() as c:
        assert photo_refs.versions_using(c, a["id"]) == [v2]
        assert photo_refs.versions_using(c, b["id"]) == [v3]


def test_a_rollback_to_the_old_version_still_shows_its_photo(admin, engine):
    """Tornare indietro non deve ripristinare né ricostruire niente: i byte non
    sono mai stati toccati, e la versione vecchia continua a puntarci."""
    v1 = bootstrap(engine)
    a = upload_ok(admin, image_bytes(colour=(200, 10, 10)))
    b = upload_ok(admin, image_bytes(colour=(10, 10, 200)))
    v2 = put_inventory(admin, v1, document(foto_a=a["id"])).json()["version"]
    v3 = put_inventory(admin, v2, document(foto_a=b["id"])).json()["version"]

    # Rollback: si salva di nuovo il contenuto della v2 come versione nuova.
    v4 = put_inventory(admin, v3, document(foto_a=a["id"])).json()["version"]
    assert v4 > v3

    r = admin.get("/api/inventory")
    rack = r.json()["doc"]["locations"][0]["sale"][0]["racks"][0]
    assert rack["foto"] == a["id"]
    # E i byte si servono ancora.
    assert admin.get(f"/api/photos/{a['id']}").status_code == 200


def test_the_reference_walk_finds_photos_anywhere_in_the_document():
    """La camminata è generica di proposito: un riferimento non registrato
    significa una foto viva che diventa cancellabile, e la direzione dell'errore
    non è simmetrica."""
    uid = ASSENTE
    assert photo_refs.photo_ids({"a": {"b": [{"foto": uid}]}}) == [uid]
    assert photo_refs.photo_ids({"foto": None}) == []
    assert photo_refs.photo_ids({"foto": "data:image/png;base64,AA"}) == []
    # Duplicati collassati: le righe finiscono in una chiave primaria.
    assert photo_refs.photo_ids({"x": {"foto": uid}, "y": {"foto": uid}}) == [uid]


# ==================================================================
# 6. garbage collection
# ==================================================================

def test_an_orphan_younger_than_the_grace_period_survives(admin, engine):
    """La finestra di grazia esiste per questo: una foto caricata è legittimamente
    orfana fino al `PUT` che la referenzia. Senza, la GC cancellerebbe gli upload
    appena fatti."""
    photo = upload_ok(admin, image_bytes())
    result = collect(engine)
    assert result.deleted == 0
    assert result.examined == 0, "troppo giovane per essere nemmeno esaminata"
    assert len(photo_rows(engine)) == 1
    assert admin.get(photo["url"]).status_code == 200


def test_an_orphan_older_than_the_grace_period_is_deleted(admin, engine):
    photo = upload_ok(admin, image_bytes())
    age_photos(engine, 48)
    result = collect(engine)
    assert result.deleted == 1
    assert result.deleted_ids == [photo["id"]]
    assert photo_rows(engine) == []
    assert admin.get(photo["url"]).status_code == 404


def test_a_photo_used_only_by_an_old_version_survives(admin, engine):
    """La testa non la usa più; una versione conservata sì. È viva."""
    v1 = bootstrap(engine)
    a = upload_ok(admin, image_bytes(colour=(200, 10, 10)))
    b = upload_ok(admin, image_bytes(colour=(10, 10, 200)))
    v2 = put_inventory(admin, v1, document(foto_a=a["id"])).json()["version"]
    put_inventory(admin, v2, document(foto_a=b["id"]))

    age_photos(engine, 72)
    result = collect(engine)

    assert result.examined == 2
    assert result.deleted == 0, "la GC non guarda solo l'inventario corrente"
    assert admin.get(f"/api/photos/{a['id']}").status_code == 200


def test_a_photo_removed_from_the_head_still_cannot_be_collected(admin, engine):
    """Togliere la foto da un rack NON la cancella: la versione precedente la
    referenzia ancora, e un rollback deve continuare a funzionare."""
    v1 = bootstrap(engine)
    a = upload_ok(admin, image_bytes(colour=(30, 200, 30)))
    v2 = put_inventory(admin, v1, document(foto_a=a["id"])).json()["version"]
    v3 = put_inventory(admin, v2, document()).json()["version"]
    assert v3 > v2

    head = admin.get("/api/inventory").json()["doc"]
    assert "foto" not in head["locations"][0]["sale"][0]["racks"][0]

    age_photos(engine, 72)
    assert collect(engine).deleted == 0
    assert admin.get(f"/api/photos/{a['id']}").status_code == 200


def test_the_foreign_key_refuses_the_deletion_even_for_the_schema_owner(admin, engine):
    """⚠ La difesa che regge se la query della GC viene riscritta male.

    Il vincolo non dipende dal fatto che chi la riscrive si ricordi del problema:
    è il database a rifiutare, e lo fa anche al proprietario dello schema.
    """
    v1 = bootstrap(engine)
    a = upload_ok(admin, image_bytes())
    put_inventory(admin, v1, document(foto_a=a["id"]))

    with engine.connect() as c:
        with pytest.raises(Exception) as err:
            c.execute(text("DELETE FROM photos WHERE id = CAST(:p AS uuid)"),
                      {"p": a["id"]})
        c.rollback()
    assert "inventory_photo_refs" in str(err.value)
    assert len(photo_rows(engine)) == 1


def test_the_gc_writes_one_audit_row_per_run(admin, engine):
    for colour in ((1, 1, 1), (2, 2, 2), (3, 3, 3)):
        upload_ok(admin, image_bytes(colour=colour))
    age_photos(engine, 48)
    collect(engine)

    rows = audit_rows(engine, "photos.gc.%")
    assert len(rows) == 1, "una riga per giro, non una per foto"
    row = rows[0]
    assert row["action"] == "photos.gc.collected"
    assert row["actor_username"] == "(worker manutenzione)"
    # `audit.events` è una LISTA: `record_auth_event` avvolge il dettaglio, perché
    # la colonna è la stessa che per un salvataggio contiene l'elenco degli eventi
    # di dominio. Una colonna, una forma.
    detail = row["events"][0]
    assert detail["deleted"] == 3 and detail["examined"] == 3
    assert detail["graceHours"] == 24
    assert len(detail["ids"]) == 3


def test_a_run_that_deletes_nothing_writes_no_audit_row(admin, engine):
    upload_ok(admin, image_bytes())
    collect(engine)
    assert audit_rows(engine, "photos.gc.%") == []


# ==================================================================
# 7. la GC è un lavoro indipendente
# ==================================================================

def test_the_gc_has_its_own_run_identity(admin, engine):
    upload_ok(admin, image_bytes())
    age_photos(engine, 48)
    first = collect(engine)
    assert first.ran and first.deleted == 1

    # Secondo giro nello stesso giorno locale: non si ripete.
    second = collect(engine)
    assert second.reason == "already_ran_today"
    assert not second.ran

    with engine.begin() as c:
        rows = [dict(r) for r in c.execute(text(
            "SELECT job, run_date, deleted_count, outcome FROM maintenance_runs"
        )).mappings()]
    assert len(rows) == 1
    assert rows[0]["job"] == "photo_gc"
    assert rows[0]["deleted_count"] == 1
    # E NON ha toccato il registro degli avvisi di scadenza.
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM scheduler_runs "
                              "WHERE run_date = :d"),
                         {"d": first.run_date}).scalar_one() == 0


def test_the_gc_runs_even_with_notifications_disabled(admin, engine):
    """Spegnere gli avvisi non deve riempire il disco: sono due lavori diversi, e
    la GC non guarda `notifications.enabled`."""
    import json as _json
    from app.settings.schema import DEFAULTS
    data = _json.loads(_json.dumps(DEFAULTS))
    data["notifications"]["enabled"] = False
    with engine.begin() as c:
        c.execute(text("UPDATE settings SET data = CAST(:d AS jsonb) WHERE id = 1"),
                  {"d": _json.dumps(data)})

    upload_ok(admin, image_bytes())
    age_photos(engine, 48)
    assert collect(engine).deleted == 1


def test_an_interrupted_run_can_be_retried_the_same_day(engine, db):
    """Una riga rimasta non conclusa si riprende: un giro interrotto a metà non
    deve far saltare la giornata."""
    with engine.begin() as c:
        assert photo_gc.claim_run(c, date(2026, 8, 11), "Europe/Rome") is True
    with engine.begin() as c:
        assert photo_gc.claim_run(c, date(2026, 8, 11), "Europe/Rome") is True
        photo_gc.finish_run(c, date(2026, 8, 11), examined=0, deleted=0,
                            outcome="nothing_to_collect")
    with engine.begin() as c:
        assert photo_gc.claim_run(c, date(2026, 8, 11), "Europe/Rome") is False


def test_the_gc_waits_for_its_own_hour(engine, db):
    """Senza `force` il giro parte solo passata la sua ora locale — la sua, non
    quella degli avvisi."""
    early = datetime(2026, 8, 11, 0, 15, tzinfo=timezone.utc)   # 02:15 a Roma
    assert photo_gc.run_once(engine, now_utc=early).reason == "not_yet_scheduled"
    late = datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc)     # 07:00 a Roma
    assert photo_gc.run_once(engine, now_utc=late).ran is True


def test_the_grace_period_boundary(admin, engine):
    """23 ore sopravvive, 25 no. Il confine è quello dichiarato, non un'idea
    approssimativa di «recente»."""
    photo = upload_ok(admin, image_bytes())
    age_photos(engine, 23)
    assert collect(engine).deleted == 0
    with engine.begin() as c:
        c.execute(text("DELETE FROM maintenance_runs"))
    age_photos(engine, 25)
    assert collect(engine).deleted == 1
    assert admin.get(photo["url"]).status_code == 404


# ==================================================================
# 8. privilegi: l'API non può cancellare byte
# ==================================================================

def _privilege(engine, role: str, table: str, priv: str) -> bool:
    with engine.begin() as c:
        return bool(c.execute(
            text("SELECT has_table_privilege(:r, :t, :p)"),
            {"r": role, "t": table, "p": priv}).scalar_one())


@pytest.mark.parametrize("role,table,priv,expected", [
    # L'API legge e accoda. Una foto è immutabile e i suoi byte li referenzia la
    # storia: né UPDATE né DELETE.
    ("tsm_api", "photos", "SELECT", True),
    ("tsm_api", "photos", "INSERT", True),
    ("tsm_api", "photos", "UPDATE", False),
    ("tsm_api", "photos", "DELETE", False),
    ("tsm_api", "inventory_photo_refs", "INSERT", True),
    ("tsm_api", "inventory_photo_refs", "DELETE", False),
    # La GC delle foto è l'UNICO privilegio di cancellazione dello schema.
    ("tsm_worker", "photos", "SELECT", True),
    ("tsm_worker", "photos", "DELETE", True),
    # Il worker non carica foto, e non può modificarne una.
    ("tsm_worker", "photos", "INSERT", False),
    ("tsm_worker", "photos", "UPDATE", False),
    ("tsm_worker", "inventory_photo_refs", "SELECT", True),
    ("tsm_worker", "inventory_photo_refs", "INSERT", False),
    # Storia: il worker legge e accoda l'audit, non lo riscrive.
    ("tsm_worker", "audit", "INSERT", True),
    ("tsm_worker", "audit", "UPDATE", False),
    ("tsm_worker", "inventory_versions", "SELECT", True),
    ("tsm_worker", "inventory_versions", "INSERT", False),
    # Le esecuzioni di manutenzione non sono affare dell'API.
    ("tsm_api", "maintenance_runs", "SELECT", False),
    ("tsm_api", "maintenance_runs", "INSERT", False),
    ("tsm_worker", "maintenance_runs", "INSERT", True),
    ("tsm_worker", "maintenance_runs", "DELETE", False),
    # Lo stato del worker non è più dell'API: la 0009 le ritira i privilegi che la
    # 0008 le aveva dato quando il worker condivideva il suo ruolo.
    ("tsm_api", "reminders", "UPDATE", False),
    ("tsm_api", "worker_heartbeat", "UPDATE", False),
    ("tsm_worker", "reminders", "UPDATE", True),
    ("tsm_worker", "worker_heartbeat", "UPDATE", True),
    # --- fase 2C: l'API mantiene la proiezione, quindi la scrive (§8.44) ---
    #
    # È il privilegio che la 0010 aveva NEGATO scrivendo «li concede la fase 2C, con
    # il codice che li usa». Adesso quel codice esiste. `TRUNCATE` resta escluso: la
    # sincronizzazione usa `DELETE`, per non prendere un lock che bloccherebbe anche
    # i lettori della fase 2D.
    ("tsm_api", "inventory_locations", "SELECT", True),
    ("tsm_api", "inventory_locations", "INSERT", True),
    ("tsm_api", "inventory_locations", "UPDATE", True),
    ("tsm_api", "inventory_locations", "DELETE", True),
    ("tsm_api", "inventory_locations", "TRUNCATE", False),
    ("tsm_api", "inventory_racks", "INSERT", True),
    ("tsm_api", "inventory_racks", "DELETE", True),
    ("tsm_api", "inventory_racks", "TRUNCATE", False),
    ("tsm_api", "inventory_devices", "INSERT", True),
    ("tsm_api", "inventory_devices", "TRUNCATE", False),
    ("tsm_api", "inventory_projection_state", "INSERT", True),
    ("tsm_api", "inventory_projection_state", "UPDATE", True),
    ("tsm_api", "inventory_projection_state", "DELETE", True),
    ("tsm_api", "inventory_projection_state", "TRUNCATE", False),
    # L'istantanea immutabile resta immutabile, e in fase 2C acquista un secondo
    # mestiere: è il riferimento contro cui la proiezione si verifica. Poterla
    # riscrivere renderebbe quella verifica una tautologia.
    ("tsm_api", "inventory_versions", "SELECT", True),
    ("tsm_api", "inventory_versions", "INSERT", True),
    ("tsm_api", "inventory_versions", "UPDATE", False),
    ("tsm_api", "inventory_versions", "DELETE", False),
    ("tsm_api", "inventory_versions", "TRUNCATE", False),
    ("tsm_api", "audit", "UPDATE", False),
    ("tsm_api", "audit", "DELETE", False),
    # Il worker non scrive la proiezione: le colonne data derivate esistono per le
    # query, e il passaggio dello scanner è una decisione successiva.
    ("tsm_worker", "inventory_locations", "SELECT", True),
    ("tsm_worker", "inventory_locations", "INSERT", False),
    ("tsm_worker", "inventory_locations", "UPDATE", False),
    ("tsm_worker", "inventory_locations", "DELETE", False),
    ("tsm_worker", "inventory_devices", "UPDATE", False),
    ("tsm_worker", "inventory_projection_state", "SELECT", True),
    ("tsm_worker", "inventory_projection_state", "UPDATE", False),
])
def test_the_privilege_matrix(engine, role, table, priv, expected):
    assert _privilege(engine, role, table, priv) is expected


def test_the_api_role_really_cannot_delete_a_photo(admin, engine):
    """Non solo `has_table_privilege`: si prova a cancellare davvero con quel
    ruolo. Il privilegio dichiarato e il comportamento sono due cose, e la seconda
    è quella che conta."""
    photo = upload_ok(admin, image_bytes())
    with engine.connect() as c:
        c.execute(text("SET ROLE tsm_api"))
        with pytest.raises(Exception) as err:
            c.execute(text("DELETE FROM photos"))
        c.rollback()
    assert "permission denied" in str(err.value).lower()
    assert len(photo_rows(engine)) == 1
    assert admin.get(photo["url"]).status_code == 200


def test_the_worker_role_cannot_insert_a_photo(engine, db):
    with engine.connect() as c:
        c.execute(text("SET ROLE tsm_worker"))
        with pytest.raises(Exception) as err:
            c.execute(text("INSERT INTO photos (mime, bytes, sha256, size_bytes) "
                           "VALUES ('image/png', '\\x00'::bytea, 'x', 1)"))
        c.rollback()
    assert "permission denied" in str(err.value).lower()
