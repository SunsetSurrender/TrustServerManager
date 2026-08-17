"""La politica delle password sulle strade vere. PostgreSQL reale.

`test_passwords.py` prova la REGOLA; questo file prova che le strade la
attraversino, e le cose che solo un database può mostrare:

  - che ogni strada che stabilisce una password passi dalla politica — cambio
    proprio, creazione da amministratore, reimpostazione, creazione di servizio;
  - che nel database finisca **solo** l'hash, e che non esista nessuna colonna per
    il sale o per un valore in chiaro;
  - che un rifiuto non lasci niente scritto;
  - che un hash con parametri vecchi venga riscritto dopo un accesso riuscito, e
    che l'utente non se ne accorga;
  - che la password provvisoria non compaia in audit, log, risposte o elenchi;
  - che la resistenza all'enumerazione non sia peggiorata aggiungendo la politica.

Niente doppi: il comportamento in prova è quello del database (transazioni,
revoca delle sessioni, `citext` sullo username) e un finto non lo dimostrerebbe.
"""
from __future__ import annotations

import json
import logging
import os

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type
from sqlalchemy import create_engine, text

from app.api.deps import get_connection
from app.auth import users as user_svc
from app.auth.passwords import (
    ARGON2_MEMORY_COST,
    ARGON2_PARALLELISM,
    ARGON2_PREFIX,
    ARGON2_TIME_COST,
    MIN_LENGTH,
    PASSWORD_BLOCKLISTED,
    PASSWORD_NOT_ENCODABLE,
    PASSWORD_TOO_LONG,
    PASSWORD_TOO_SHORT,
    PASSWORD_UNCHANGED,
    PasswordRejected,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.auth.service import InvalidCredentials, change_own_password, create_user
from app.identity import CURRENT_SCHEMA_VERSION
from app.inventory import Actor, InventoryRepository
from app.main import app

DSN = os.environ.get("TSM_DB_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TSM_DB_URL non impostata")

from conftest import ORIGIN, api_client  # noqa: E402

LOC = "aaaaaaaa-0000-4000-8000-000000000031"
ROOM = "bbbbbbbb-0000-4000-8000-000000000031"
RACK = "cccccccc-0000-4000-8000-00000000003a"

# --- password di prova, tutte conformi e nessuna in lista ---------------------
PW_ADMIN = "il gatto dorme sul tetto"
PW_OP = "quattro parole di prova qui"
PW_TEMP = "provvisoria da cambiare ora"

#: Caratteri come SEQUENZE DI ESCAPE, non digitati: vale la ragione documentata
#: in `test_passwords.py`. L'accento combinante e' invisibile in un editor: se
#: scritto direttamente, una riga che sembra corretta puo' contenere un carattere
#: diverso, e il confronto fra le due forme Unicode non proverebbe piu' niente.
A_GRAVE = "\u00e0"           # a con accento grave, forma composta
COMBINING_GRAVE = "\u0300"   # accento grave combinante
ROCKET = "\U0001f680"        # emoji fuori dal BMP
HIGH_SURROGATE = "\ud800"    # surrogato alto spaiato


def base_doc() -> dict:
    return {"schemaVersion": CURRENT_SCHEMA_VERSION,
            "locations": [{"_uid": LOC, "id": "s", "nome": "S", "sale": [
                {"_uid": ROOM, "id": "r", "nome": "R", "w": 6, "h": 5, "vani": [],
                 "racks": [{"_uid": RACK, "id": "R01", "name": "R01", "u": 45,
                            "x": 0.5, "y": 0.5, "w": 0.6, "h": 0.8,
                            "devices": []}]}]}]}


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
        c.execute(text("DELETE FROM login_attempts"))
        c.execute(text("DELETE FROM sessions"))
        c.execute(text("DELETE FROM audit"))
        c.execute(text("TRUNCATE inventory_head, inventory_versions "
                       "RESTART IDENTITY CASCADE"))
        c.execute(text("DELETE FROM users"))
        create_user(c, "capo", PW_ADMIN, "admin", must_change_pw=False)
        create_user(c, "op", PW_OP, "edit", must_change_pw=False)
        create_user(c, "prima-volta", PW_TEMP, "edit", must_change_pw=True)
    with engine.begin() as c:
        InventoryRepository(c).bootstrap(base_doc(),
                                         Actor(username="capo", role="admin"))
    yield engine


@pytest.fixture
def client(db, engine):
    def _dep():
        with engine.connect() as conn:
            with conn.begin():
                yield conn
    app.dependency_overrides[get_connection] = _dep
    with api_client(app) as c:
        yield c
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ aiutanti

def login(client, username: str, password: str):
    return client.post("/api/auth/login", headers=ORIGIN,
                       json={"username": username, "password": password})


def change(client, current: str, new: str):
    return client.post("/api/auth/password", headers=ORIGIN,
                       json={"currentPassword": current, "newPassword": new})


def stored_hash(engine, username: str) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT password_hash FROM users WHERE username = :u"),
                         {"u": username}).scalar_one()


def uid_of(engine, username: str):
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM users WHERE username = :u"),
                         {"u": username}).scalar_one()


def live_sessions(engine, username: str) -> int:
    with engine.connect() as c:
        return c.execute(text("""
            SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id
             WHERE u.username = :u AND s.revoked_at IS NULL
        """), {"u": username}).scalar_one()


def version_count(engine) -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM inventory_versions")).scalar_one()


def audit_dump(engine) -> str:
    """Tutto il registro come testo: la rete finale per «non c'è la password»."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT actor_username, actor_role, action, result, events::text, "
            "       client_hint FROM audit ORDER BY id")).all()
    return json.dumps([[str(v) for v in r] for r in rows], ensure_ascii=False)


# ==========================================================================
# 1. Ogni strada passa dalla politica
# ==========================================================================

@pytest.mark.parametrize("pw,code", [
    ("a" * 14, PASSWORD_TOO_SHORT),
    ("corta", PASSWORD_TOO_SHORT),
    ("", PASSWORD_TOO_SHORT),
    ("x" * 129, PASSWORD_TOO_LONG),
    ("passwordpassword", PASSWORD_BLOCKLISTED),
    ("trustservermanager", PASSWORD_BLOCKLISTED),
])
def test_the_change_route_applies_the_whole_policy(client, engine, pw, code):
    """422 con il codice stabile, e la password vecchia resta quella buona.

    La seconda metà conta quanto la prima: un rifiuto che avesse comunque
    aggiornato l'hash lascerebbe l'utente fuori con un errore in mano.
    """
    login(client, "op", PW_OP)
    prima = stored_hash(engine, "op")

    r = change(client, PW_OP, pw)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == code

    assert stored_hash(engine, "op") == prima
    assert verify_password(prima, PW_OP)


def raw_change(client, body: str):
    """POST del cambio password con un CORPO GREZZO.

    Serve per il surrogato spaiato, che non si può inviare con `json=`: httpx
    serializza con `ensure_ascii=False` e poi codifica in UTF-8, quindi solleva
    prima di partire — lo stesso muro che il modulo descrive, incontrato dal lato
    del client. Il corpo si costruisce qui con `ensure_ascii=True`, così il
    surrogato viaggia come sequenza di escape e il server lo riceve davvero.

    Il `Content-Type` è ESPLICITO e non è un dettaglio: senza, FastAPI non prova
    nemmeno a interpretare il corpo e risponde **422** — lo stesso stato che il
    test si aspetta, per un motivo completamente diverso. È una trappola già
    scattata una volta in questo progetto, e il rimedio è il caso di controllo
    qui sotto.
    """
    return client.post("/api/auth/password", content=body.encode("utf-8"),
                       headers={**ORIGIN, "Content-Type": "application/json"})


def test_over_http_a_lone_surrogate_is_stopped_before_the_policy(client, engine):
    """MISURATO, e diverso da quello che avevo previsto: su HTTP non arriva.

    `pydantic-core` è scritto in Rust, e una `str` di Rust deve essere UTF-8 valido:
    un surrogato spaiato non è rappresentabile, quindi la validazione del corpo
    rifiuta prima che la politica veda qualcosa. La risposta è il 422 generico
    `invalid_body` (§8.21), NON `password_not_encodable`.

    Il test dice questo invece di pretendere il codice della politica, perché
    pretenderlo avrebbe significato spostare un controllo dove non serve per far
    tornare un'aspettativa sbagliata. Le due cose che contano sono comunque vere:
    non è un 503, e non è cambiato niente.

    La strada in cui il controllo della politica serve davvero è un'altra, e non
    passa da pydantic: vedi i due test seguenti.
    """
    login(client, "op", PW_OP)
    prima = stored_hash(engine, "op")

    corpo = json.dumps({"currentPassword": PW_OP,
                        "newPassword": "una password lunga" + HIGH_SURROGATE},
                       ensure_ascii=True)
    assert "\\ud800" in corpo and corpo.isascii()      # il surrogato è una escape

    r = raw_change(client, corpo)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "invalid_body", r.text
    assert r.status_code != 503
    assert stored_hash(engine, "op") == prima          # niente è cambiato


def test_at_the_service_level_a_lone_surrogate_is_rejected_by_the_policy(engine, db):
    """La strada che pydantic non protegge: una chiamata di servizio.

    Qui il controllo è l'unica difesa, e serve per due motivi misurati:

      1. senza, `hash_password` solleva `UnicodeEncodeError`, che nessuno mappa e
         che in una rotta diventerebbe un 503 — un errore del server per un dato
         ricevuto;

      2. se un hash nascesse comunque, l'utenza sarebbe **inaccessibile per
         sempre**, perché `verify_password` intercetta quella stessa eccezione e
         risponde «credenziali errate» a ogni accesso futuro.
    """
    uid = uid_of(engine, "op")
    prima = stored_hash(engine, "op")

    with engine.begin() as c:
        with pytest.raises(PasswordRejected) as info:
            change_own_password(c, uid, PW_OP, "una password lunga" + HIGH_SURROGATE)
    assert info.value.code == PASSWORD_NOT_ENCODABLE
    assert stored_hash(engine, "op") == prima

    with engine.begin() as c:
        with pytest.raises(PasswordRejected) as info:
            create_user(c, "mai.creata", "una password lunga" + HIGH_SURROGATE, "view")
    assert info.value.code == PASSWORD_NOT_ENCODABLE


def test_an_environment_variable_really_can_carry_a_lone_surrogate(engine, db):
    """Perché la strada del bootstrap è concreta e non teorica.

    `os.environ` decodifica i byte dell'ambiente con `surrogateescape`: un byte non
    valido in UTF-8 — 0x80, tipico di un testo Latin-1 — diventa un surrogato
    spaiato nella stringa Python. Basta una password incollata da un terminale con
    la codepage sbagliata in un file di unit systemd, ed è esattamente il valore
    che farebbe sollevare Argon2.

    Senza il controllo, `bootstrap.py` morirebbe con un traceback su
    `UnicodeEncodeError`; con il controllo dice che cosa non va, con un codice.
    """
    dai_byte = b"password-da-latin1-\x80-lunga".decode("utf-8", "surrogateescape")
    assert any(0xD800 <= ord(ch) <= 0xDFFF for ch in dai_byte)

    from app.auth.passwords import policy_problem
    problem = policy_problem(dai_byte)
    assert problem is not None and problem[0] == PASSWORD_NOT_ENCODABLE

    with engine.begin() as c:
        with pytest.raises(PasswordRejected):
            create_user(c, "da.ambiente", dai_byte, "admin")


def test_the_raw_body_control_case_proves_the_previous_test_can_fail(client, engine):
    """Il controllo: lo stesso corpo grezzo, con una password BUONA, deve riuscire.

    Se questo non passasse, il 422 del test precedente non direbbe niente sulla
    politica — direbbe soltanto che il corpo grezzo non arriva.
    """
    login(client, "op", PW_OP)
    corpo = json.dumps({"currentPassword": PW_OP,
                        "newPassword": "una password buona e lunga"},
                       ensure_ascii=True)
    r = raw_change(client, corpo)
    assert r.status_code == 204, r.text
    assert verify_password(stored_hash(engine, "op"), "una password buona e lunga")


def test_the_change_route_accepts_a_conforming_password(client, engine):
    """Il controllo positivo di tutti i casi sopra: la rotta funziona.

    Senza, i 422 di prima potrebbero venire da qualunque cosa — un `Origin`
    sbagliato, una sessione caduta — e il file sembrerebbe verde per il motivo
    sbagliato.
    """
    login(client, "op", PW_OP)
    nuova = "una frase nuova e lunga per me"
    assert change(client, PW_OP, nuova).status_code == 204
    assert verify_password(stored_hash(engine, "op"), nuova)


@pytest.mark.parametrize("length,expected", [
    (14, 422), (15, 204), (128, 204), (129, 422),
])
def test_the_length_boundaries_through_http(client, engine, length, expected):
    """14 no, 15 sì, 128 sì, 129 no — attraverso la rotta, non solo nel modulo.

    La base non è `"a" * length`: `aaaaaaaaaaaaaaa` è in lista e il caso «15
    passa» sarebbe fallito per un motivo diverso da quello in prova.
    """
    login(client, "op", PW_OP)
    pw = ("frase lunga per il test " * 20)[:length]
    assert len(pw) == length
    r = change(client, PW_OP, pw)
    assert r.status_code == expected, r.text
    if expected == 204:
        assert verify_password(stored_hash(engine, "op"), pw)


def test_no_truncation_happens_anywhere_in_the_http_path(client, engine):
    """Una password da 128 caratteri è memorizzata INTERA.

    Se qualcosa la tagliasse — pydantic, il driver, una colonna — la sua testa
    verificherebbe, e questo test diventerebbe rosso.
    """
    login(client, "op", PW_OP)
    pw = ("passphrase distinta e molto lunga " * 8)[:128]
    assert change(client, PW_OP, pw).status_code == 204

    h = stored_hash(engine, "op")
    assert verify_password(h, pw)
    assert not verify_password(h, pw[:127])
    assert not verify_password(h, pw[:64])


def test_the_service_level_create_user_applies_the_policy(engine, db):
    """La strada del BOOTSTRAP, che non passa da HTTP.

    È quella che crea il PRIMO amministratore: validare solo nell'adattatore HTTP
    la lascerebbe scoperta, ed è la più importante di tutte. Un rifiuto non deve
    lasciare l'utenza a metà.
    """
    for pw, code in [("admin", PASSWORD_TOO_SHORT),
                     ("passwordpassword", PASSWORD_BLOCKLISTED),
                     ("trusttechnologies", PASSWORD_BLOCKLISTED)]:
        with engine.begin() as c:
            with pytest.raises(PasswordRejected) as info:
                create_user(c, "nuovo-servizio", pw, "admin")
            assert info.value.code == code

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users WHERE username = :u"),
                         {"u": "nuovo-servizio"}).scalar_one() == 0


def test_a_service_user_cannot_have_the_username_as_password(engine, db):
    """Prevedibile, e non elencabile in un file: dipende dall'utenza."""
    with engine.begin() as c:
        with pytest.raises(PasswordRejected) as info:
            create_user(c, "amministratore.tsm", "amministratore.tsm", "admin")
        assert info.value.code == PASSWORD_BLOCKLISTED


def test_the_admin_create_route_never_produces_a_weak_temporary(client, engine):
    """L'amministratore non SCEGLIE la provvisoria: la genera il server.

    Non c'è nessun campo `password` nel contratto della rotta, e quindi nessun
    modo di indebolirla dall'esterno. Si verifica anche che quella generata
    rispetti la politica: è la stessa che l'utente dovrà ridigitare.
    """
    login(client, "capo", PW_ADMIN)
    r = client.post("/api/users", headers=ORIGIN,
                    json={"username": "nuova.persona", "role": "view",
                          "password": "corta"})     # campo estraneo: ignorato
    assert r.status_code == 201, r.text
    temp = r.json()["temporaryPassword"]

    assert len(temp) >= MIN_LENGTH
    from app.auth.passwords import policy_problem
    assert policy_problem(temp) is None
    # E la provvisoria funziona davvero, con l'obbligo di cambio.
    assert login(client, "nuova.persona", temp).json()["mustChangePassword"] is True


# ==========================================================================
# 2. Unicode: la stessa normalizzazione su tutte le strade
# ==========================================================================

def test_a_password_set_composed_logs_in_decomposed(client, engine):
    """La prova che la normalizzazione è la STESSA all'impostazione e all'accesso.

    È il difetto che si sarebbe visto solo in produzione e solo su una
    piattaforma: la stessa persona, la stessa tastiera, e un sistema operativo che
    consegna la forma decomposta. Senza NFC condiviso l'accesso verrebbe negato e
    niente risulterebbe sbagliato.
    """
    composta = "la citt" + A_GRAVE + " di Pomezia va"
    decomposta = "la citta" + COMBINING_GRAVE + " di Pomezia va"
    assert composta != decomposta

    login(client, "op", PW_OP)
    assert change(client, PW_OP, composta).status_code == 204

    assert login(client, "op", decomposta).status_code == 200
    assert login(client, "op", composta).status_code == 200


def test_a_password_set_decomposed_logs_in_composed(client, engine):
    """E nell'altro verso: la normalizzazione non ha un lato privilegiato."""
    composta = "la citt" + A_GRAVE + " di Roma va bene"
    decomposta = "la citta" + COMBINING_GRAVE + " di Roma va bene"

    login(client, "op", PW_OP)
    assert change(client, PW_OP, decomposta).status_code == 204
    assert login(client, "op", composta).status_code == 200


def test_a_non_ascii_password_survives_the_database_round_trip(client, engine):
    """Accenti ed emoji: la password non finisce nel database, ma il suo hash sì.

    L'hash è ASCII per costruzione, quindi non c'è nessun problema di
    rappresentabilità — e questo test lo dimostra invece di darlo per scontato.
    """
    pw = "perimetrale gi" + A_GRAVE + " verificata " + ROCKET
    login(client, "op", PW_OP)
    assert change(client, PW_OP, pw).status_code == 204

    h = stored_hash(engine, "op")
    assert h.isascii(), "l'hash memorizzato non è ASCII"
    assert login(client, "op", pw).status_code == 200


def test_a_password_with_spaces_at_the_edges_is_not_trimmed(client, engine):
    """Lo spazio fa parte della password, e resta.

    Se qualcosa lo togliesse in impostazione ma non all'accesso — o viceversa —
    l'utente non entrerebbe più con ciò che ha scritto.
    """
    pw = "  con spazi ai bordi qui  "
    login(client, "op", PW_OP)
    assert change(client, PW_OP, pw).status_code == 204

    assert login(client, "op", pw).status_code == 200
    assert login(client, "op", pw.strip()).status_code == 401


# ==========================================================================
# 3. Il primo cambio obbligatorio
# ==========================================================================

def test_the_forced_change_cannot_reuse_the_temporary_password(client, engine):
    """Rimettere la provvisoria non è un cambio.

    Senza questo controllo `must_change_pw` si azzererebbe lasciando in uso
    esattamente il valore che l'amministratore ha comunicato a voce o scritto in
    un messaggio — e che quindi conosce anche lui. Il codice è stabile, così il
    client può spiegarlo.
    """
    login(client, "prima-volta", PW_TEMP)
    r = change(client, PW_TEMP, PW_TEMP)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == PASSWORD_UNCHANGED

    # L'obbligo è ancora in piedi e la provvisoria è ancora quella.
    with engine.connect() as c:
        assert c.execute(text("SELECT must_change_pw FROM users WHERE username = :u"),
                         {"u": "prima-volta"}).scalar_one() is True


def test_the_forced_change_refuses_the_same_password_in_another_unicode_form(client):
    """Lo stesso valore in forma decomposta è lo STESSO valore.

    Si confronta con l'hash memorizzato, non con la stringa inviata: è ciò che
    rende inutile provare ad aggirare il controllo cambiando forma Unicode.
    """
    composta = "la citt" + A_GRAVE + " di Pomezia va"
    decomposta = "la citta" + COMBINING_GRAVE + " di Pomezia va"

    login(client, "prima-volta", PW_TEMP)
    assert change(client, PW_TEMP, composta).status_code == 204
    assert login(client, "prima-volta", composta).status_code == 200

    r = change(client, composta, decomposta)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == PASSWORD_UNCHANGED


def test_the_forced_change_applies_the_full_policy_not_a_lighter_one(client):
    """Il ramo «provvisoria» non è una scorciatoia.

    È quello che si scrive in fretta, e quello dove un controllo si dimentica:
    qui si prova che una password corta o in lista viene rifiutata anche mentre
    l'obbligo di cambio è attivo.
    """
    login(client, "prima-volta", PW_TEMP)
    for pw, code in [("corta", PASSWORD_TOO_SHORT),
                     ("passwordpassword", PASSWORD_BLOCKLISTED)]:
        r = change(client, PW_TEMP, pw)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == code


def test_the_forced_change_revokes_three_concurrent_sessions(client, engine):
    """Più sessioni contemporanee, come richiesto.

    Cadono tutte, compresa quella che sta facendo il cambio: chi cambia password
    si aspetta che le altre cadano, e la propria ripartirà da un accesso nuovo.
    """
    login(client, "prima-volta", PW_TEMP)
    with api_client(app) as due, api_client(app) as tre:
        login(due, "prima-volta", PW_TEMP)
        login(tre, "prima-volta", PW_TEMP)
        assert live_sessions(engine, "prima-volta") == 3
        assert due.get("/api/auth/me").status_code == 200
        assert tre.get("/api/auth/me").status_code == 200

        nuova = "adesso la scelgo io stesso"
        assert change(client, PW_TEMP, nuova).status_code == 204

        assert client.get("/api/auth/me").status_code == 401
        assert due.get("/api/auth/me").status_code == 401
        assert tre.get("/api/auth/me").status_code == 401

    assert live_sessions(engine, "prima-volta") == 0
    r = login(client, "prima-volta", nuova)
    assert r.status_code == 200 and r.json()["mustChangePassword"] is False


def test_a_restricted_session_still_reaches_only_the_three_endpoints(client):
    """La restrizione non è stata allentata aggiungendo la politica.

    Non è un doppione del test in `test_hardening_pg.py`: lì si prova la
    restrizione, qui si prova che il nuovo codice di rifiuto non abbia aperto una
    strada — un 422 sulla rotta della password non deve valere come «cambio
    avvenuto».
    """
    login(client, "prima-volta", PW_TEMP)
    change(client, PW_TEMP, "corta")            # rifiutata

    assert client.get("/api/inventory").status_code == 403
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/auth/me").json()["mustChangePassword"] is True


# ==========================================================================
# 4. Il cambio ordinario
# ==========================================================================

def test_the_normal_change_requires_the_current_password(client, engine):
    """Senza, chi si impossessa di una sessione aperta — un portatile lasciato
    sbloccato — cambierebbe la password senza conoscere quella vecchia, e
    l'accesso diventerebbe suo."""
    login(client, "op", PW_OP)
    prima = stored_hash(engine, "op")

    r = change(client, "non e la mia password", "una password nuova e buona")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"
    assert stored_hash(engine, "op") == prima


def test_a_wrong_current_password_reveals_nothing_extra(client, engine):
    """La risposta a «password attuale errata» è la stessa di «utenza inesistente
    o disabilitata»: un solo codice, un solo messaggio, nessun campo in più."""
    login(client, "op", PW_OP)
    r = change(client, "sbagliata ma lunga assai", "una password nuova e buona")
    detail = r.json()["detail"]
    assert set(detail) == {"code", "message"}
    assert detail["code"] == "invalid_credentials"
    testo = json.dumps(detail, ensure_ascii=False).lower()
    for parola in ["hash", "argon", "utente", "esist", "disabilit", "ruolo"]:
        assert parola not in testo, detail


def test_the_normal_change_cannot_reuse_the_current_password(client):
    login(client, "op", PW_OP)
    r = change(client, PW_OP, PW_OP)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == PASSWORD_UNCHANGED


def test_the_normal_change_revokes_every_session(client, engine):
    login(client, "op", PW_OP)
    with api_client(app) as altra:
        login(altra, "op", PW_OP)
        assert live_sessions(engine, "op") == 2
        assert change(client, PW_OP, "la mia nuova frase segreta").status_code == 204
        assert altra.get("/api/auth/me").status_code == 401
    assert live_sessions(engine, "op") == 0


def test_the_current_password_is_verified_before_the_new_one_is_judged(client):
    """L'ordine conta: prima la password attuale, poi la politica sulla nuova.

    Al contrario, chi possiede una sessione ma non la password potrebbe SONDARE la
    politica — e soprattutto la lista — inviando password nuove e leggendo i codici,
    senza avere alcun diritto di essere lì. Con l'ordine giusto riceve 401 e non
    impara niente.
    """
    login(client, "op", PW_OP)
    r = change(client, "password attuale sbagliata", "passwordpassword")
    assert r.status_code == 401                      # non 422 password_blocklisted
    assert r.json()["detail"]["code"] == "invalid_credentials"


def test_change_own_password_is_atomic_on_rejection(engine, db):
    """Un rifiuto non lascia niente scritto: né hash, né sessioni revocate, né audit.

    Si guarda dal livello del servizio, dentro una transazione controllata dal
    test: dall'esterno un 422 e un 422 con effetti collaterali si somigliano.
    """
    uid = uid_of(engine, "op")
    prima = stored_hash(engine, "op")
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO sessions (user_id, token_hash, expires_at)
            VALUES (:u, 'sessione-finta-per-la-prova', now() + interval '1 hour')
        """), {"u": uid})

    with engine.begin() as c:
        with pytest.raises(PasswordRejected):
            change_own_password(c, uid, PW_OP, "passwordpassword")

    assert stored_hash(engine, "op") == prima
    assert live_sessions(engine, "op") == 1                 # non revocata
    assert audit_dump(engine).count("auth.password.changed") == 0


def test_a_wrong_current_password_raises_invalid_credentials_not_a_policy_error(engine, db):
    uid = uid_of(engine, "op")
    with engine.begin() as c:
        with pytest.raises(InvalidCredentials):
            change_own_password(c, uid, "non e quella giusta", "una frase nuova e buona")


# ==========================================================================
# 5. Reimpostazione da amministratore
# ==========================================================================

def test_an_admin_reset_returns_a_temporary_once_and_revokes_sessions(client, engine):
    """Il reset: provvisoria nuova, obbligo di cambio, sessioni revocate.

    Revocare è il punto: senza, chi aveva la sessione aperta continuerebbe a
    operare come se niente fosse, e un reset chiesto perché la password è
    compromessa non servirebbe a nulla.
    """
    with api_client(app) as vittima:
        login(vittima, "op", PW_OP)
        assert vittima.get("/api/auth/me").status_code == 200

        login(client, "capo", PW_ADMIN)
        r = client.post(f"/api/users/{uid_of(engine, 'op')}/reset-password",
                        headers=ORIGIN)
        assert r.status_code == 200, r.text
        temp = r.json()["temporaryPassword"]

        assert vittima.get("/api/auth/me").status_code == 401

    assert live_sessions(engine, "op") == 0
    assert login(client, "op", PW_OP).status_code == 401          # la vecchia non vale
    r = login(client, "op", temp)
    assert r.status_code == 200 and r.json()["mustChangePassword"] is True


def test_an_admin_reset_never_needs_or_exposes_the_previous_password(client, engine):
    """L'amministratore non conosce la password precedente e non la riceve.

    Nella risposta c'è solo la provvisoria NUOVA: si verifica che il valore vecchio
    non compaia da nessuna parte, nemmeno nell'oggetto dell'utenza.
    """
    login(client, "capo", PW_ADMIN)
    r = client.post(f"/api/users/{uid_of(engine, 'op')}/reset-password", headers=ORIGIN)
    corpo = json.dumps(r.json(), ensure_ascii=False)

    assert PW_OP not in corpo
    assert ARGON2_PREFIX not in corpo
    assert "password_hash" not in corpo
    assert set(r.json()) == {"user", "temporaryPassword"}


def test_two_resets_never_give_the_same_temporary(client, engine):
    login(client, "capo", PW_ADMIN)
    uid = uid_of(engine, "op")
    primi = set()
    for _ in range(5):
        r = client.post(f"/api/users/{uid}/reset-password", headers=ORIGIN)
        primi.add(r.json()["temporaryPassword"])
    assert len(primi) == 5


def test_two_users_given_the_same_temporary_value_still_get_different_hashes(engine, db):
    """Il caso richiesto: stessa provvisoria imposta a due utenti, hash diversi.

    Si forza il valore, cosa che la rotta non permette, proprio per escludere che
    il sale dipenda dalla password — un errore invisibile se ogni prova usa un
    valore diverso.
    """
    identica = "la stessa provvisoria per due"
    actor = Actor(username="capo", role="admin", user_id=uid_of(engine, "capo"))
    with engine.begin() as c:
        user_svc.create_user(c, username="tizio", role="view", actor=actor,
                             password=identica)
        user_svc.create_user(c, username="caio", role="view", actor=actor,
                             password=identica)

    a, b = stored_hash(engine, "tizio"), stored_hash(engine, "caio")
    assert a != b
    assert a.split("$")[4] != b.split("$")[4]              # sali diversi
    assert verify_password(a, identica) and verify_password(b, identica)


def test_a_reset_audits_the_fact_and_never_the_generated_password(client, engine):
    """Nel registro c'è che un reset è avvenuto, e chi lo ha fatto. Non il valore."""
    login(client, "capo", PW_ADMIN)
    r = client.post(f"/api/users/{uid_of(engine, 'op')}/reset-password", headers=ORIGIN)
    temp = r.json()["temporaryPassword"]

    dump = audit_dump(engine)
    assert "users.password_reset" in dump
    assert temp not in dump
    assert ARGON2_PREFIX not in dump


# ==========================================================================
# 6. Aggiornamento degli hash vecchi
# ==========================================================================

def weak_hash(plain: str) -> str:
    """Un hash Argon2id STORICO: parametri più debolli di quelli correnti.

    Fabbricato di proposito con `PasswordHasher` diretto: è l'unico modo di avere
    in prova ciò che in produzione arriverà dal passato, cioè hash nati con la
    configurazione di ieri.
    """
    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1,
                          type=Type.ID).hash(plain)


def test_a_weaker_hash_is_rewritten_after_a_successful_login(client, engine):
    """Il percorso di aggiornamento, dal database al database.

    L'utente non se ne accorge: accede normalmente, non gli si chiede niente, e
    l'hash sul disco è nuovo. È l'unico momento in cui la password in chiaro
    esiste insieme a un hash verificato, quindi l'unico in cui si può ricalcolare.
    """
    pw = "vecchia password ma lunga"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": weak_hash(pw)})
    prima = stored_hash(engine, "op")
    assert needs_rehash(prima) is True

    r = login(client, "op", pw)
    assert r.status_code == 200                       # nessun cambio visibile
    assert r.json()["mustChangePassword"] is False    # e nessun obbligo imposto

    dopo = stored_hash(engine, "op")
    assert dopo != prima
    assert needs_rehash(dopo) is False
    assert f"m={ARGON2_MEMORY_COST},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}" in dopo
    assert verify_password(dopo, pw)                  # la password è ancora quella


def test_the_rewrite_happens_once_and_then_stops(client, engine):
    """Il secondo accesso non riscrive: `needs_rehash` è falso e non si tocca niente.

    Senza questa proprietà ogni accesso pagherebbe un hash in più per sempre, e la
    colonna `updated_at` cambierebbe a ogni login.
    """
    pw = "vecchia password ma lunga"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": weak_hash(pw)})

    login(client, "op", pw)
    dopo_uno = stored_hash(engine, "op")
    login(client, "op", pw)
    assert stored_hash(engine, "op") == dopo_uno


def test_a_failed_login_does_not_rewrite_the_hash(client, engine):
    """La riscrittura è legata a una verifica RIUSCITA.

    Se avvenisse comunque, chi prova password a caso contro un'utenza con hash
    vecchio farebbe lavorare il server il doppio, e — peggio — un errore in quel
    ramo potrebbe scrivere un hash che non corrisponde a niente.
    """
    pw = "vecchia password ma lunga"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": weak_hash(pw)})
    prima = stored_hash(engine, "op")

    assert login(client, "op", "questa non e quella giusta").status_code == 401
    assert stored_hash(engine, "op") == prima


def test_the_rewrite_is_audited_without_any_hash_material(client, engine):
    """Si registra il FATTO. Né l'hash vecchio, né il nuovo, né i parametri di
    partenza — che direbbero a chi legge il registro quanto era debole quell'utenza
    fino a un istante prima."""
    pw = "vecchia password ma lunga"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": weak_hash(pw)})
    login(client, "op", pw)

    dump = audit_dump(engine)
    assert "auth.password.rehashed" in dump
    assert pw not in dump
    assert ARGON2_PREFIX not in dump
    assert "m=8" not in dump                    # nessun parametro di partenza


def test_the_rewrite_and_the_session_are_one_transaction(client, engine):
    """Non esiste lo stato «hash nuovo, sessione mancante».

    Dopo l'accesso ci sono entrambi. Sono nella stessa transazione della richiesta,
    quindi un guasto a metà non lascia l'uno senza l'altro.
    """
    pw = "vecchia password ma lunga"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": weak_hash(pw)})

    assert login(client, "op", pw).status_code == 200
    assert needs_rehash(stored_hash(engine, "op")) is False
    assert live_sessions(engine, "op") == 1


# ==========================================================================
# 7. Che cosa c'è nel database
# ==========================================================================

def test_the_users_table_stores_only_a_hash(engine, db):
    """Nessuna colonna per il sale, nessuna per un valore in chiaro.

    Il sale sta dentro l'hash codificato: non è un segreto e non ha bisogno di una
    colonna. Una colonna in più sarebbe un posto dove qualcuno, un giorno, scrive
    la cosa sbagliata.
    """
    with engine.connect() as c:
        colonne = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_name = 'users'"))}

    assert "password_hash" in colonne
    sospette = [n for n in colonne
                if ("password" in n and n != "password_hash")
                or "salt" in n or "sale" in n or "plain" in n
                or "chiaro" in n or "pepper" in n]
    assert not sospette, f"colonne sospette in users: {sospette}"


def test_every_stored_hash_is_argon2id_with_the_current_parameters(engine, db):
    with engine.connect() as c:
        hashes = [r[0] for r in c.execute(text("SELECT password_hash FROM users"))]
    assert len(hashes) == 3
    for h in hashes:
        assert h.startswith(ARGON2_PREFIX), h[:20]
        assert "$v=19$" in h
        assert f"m={ARGON2_MEMORY_COST},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}" in h
        assert needs_rehash(h) is False


def test_no_plaintext_password_exists_anywhere_in_the_database(client, engine):
    """Il verso che conta per un dump: si cerca il valore in chiaro in tutte le
    colonne testuali di tutte le tabelle.

    Non è teatro. Un dump di PostgreSQL è la cosa che esce dal perimetro più
    facilmente — un backup, una copia per il collaudo — e deve contenere solo
    funzioni a senso unico.
    """
    login(client, "capo", PW_ADMIN)
    r = client.post("/api/users", headers=ORIGIN,
                    json={"username": "persona.nuova", "role": "edit"})
    temp = r.json()["temporaryPassword"]
    login(client, "persona.nuova", temp)
    change(client, temp, "la mia password personale")

    with engine.connect() as c:
        colonne = c.execute(text("""
            SELECT table_name, column_name FROM information_schema.columns
             WHERE table_schema = 'public'
               AND data_type IN ('text','character varying','jsonb','json','citext')
        """)).all()
        for tabella, colonna in colonne:
            trovati = c.execute(text(
                f'SELECT count(*) FROM "{tabella}" '
                f'WHERE "{colonna}"::text LIKE :pat'), {"pat": f"%{temp}%"}).scalar_one()
            assert trovati == 0, f"provvisoria in chiaro in {tabella}.{colonna}"
            trovati = c.execute(text(
                f'SELECT count(*) FROM "{tabella}" '
                f'WHERE "{colonna}"::text LIKE :pat'),
                {"pat": "%la mia password personale%"}).scalar_one()
            assert trovati == 0, f"password in chiaro in {tabella}.{colonna}"


def test_the_probe_for_plaintext_can_actually_find_something(engine, db):
    """Il controllo di sanità del test precedente.

    Una ricerca che non trova niente perché è scritta male sembra identica a una
    che non trova niente perché non c'è niente. Qui si scrive di proposito una
    stringa in una colonna testuale e si verifica che la stessa ricerca la trovi.
    """
    marcatore = "MARCATORE-DI-PROVA-9182"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET team = :m WHERE username = 'op'"),
                  {"m": marcatore})
    with engine.connect() as c:
        trovati = c.execute(text(
            'SELECT count(*) FROM "users" WHERE "team"::text LIKE :pat'),
            {"pat": f"%{marcatore}%"}).scalar_one()
    assert trovati == 1


# ==========================================================================
# 8. Niente fughe in audit, log, API
# ==========================================================================

def test_the_audit_never_contains_a_password_on_any_path(client, engine):
    """Creazione, accesso, cambio, reset, tentativo fallito: il registro completo.

    Si esercitano tutte le strade e poi si guarda tutto il registro in una volta,
    invece di un evento per volta: è così che si accorge di un produttore nuovo
    che scrive un dettaglio di troppo.
    """
    login(client, "capo", PW_ADMIN)
    r = client.post("/api/users", headers=ORIGIN,
                    json={"username": "persona.audit", "role": "edit"})
    temp = r.json()["temporaryPassword"]

    login(client, "persona.audit", temp)
    nuova = "la password che ho scelto io"
    change(client, temp, nuova)
    login(client, "persona.audit", nuova)
    login(client, "persona.audit", "questa e sbagliata di brutto")

    dump = audit_dump(engine)
    for segreto in [temp, nuova, PW_ADMIN, "questa e sbagliata di brutto"]:
        assert segreto not in dump, "un segreto è finito nel registro"
    assert ARGON2_PREFIX not in dump

    from app.audit.sanitize import contains_secret
    assert not contains_secret(dump)
    # E gli eventi che DEVONO esserci ci sono: il test non passa per silenzio.
    for azione in ["users.created", "auth.login.success", "auth.password.changed",
                   "auth.login.failure"]:
        assert azione in dump, azione


def test_the_audit_api_never_returns_password_material(client, engine):
    """La seconda ripulitura, in lettura (§8.36): quella che regge se la prima è
    stata aggirata."""
    login(client, "capo", PW_ADMIN)
    temp = client.post("/api/users", headers=ORIGIN,
                       json={"username": "persona.api", "role": "view"}
                       ).json()["temporaryPassword"]

    r = client.get("/api/audit?limit=200")
    assert r.status_code == 200
    corpo = json.dumps(r.json(), ensure_ascii=False)
    assert temp not in corpo
    assert ARGON2_PREFIX not in corpo


def test_no_password_reaches_the_logs(client, engine, caplog):
    """Nei log del server non finisce nessuna password.

    Si cattura a livello DEBUG, cioè il più verboso: è lì che una traccia di
    diagnosi lasciata da qualcuno comparirebbe.
    """
    with caplog.at_level(logging.DEBUG):
        login(client, "capo", PW_ADMIN)
        temp = client.post("/api/users", headers=ORIGIN,
                           json={"username": "persona.log", "role": "view"}
                           ).json()["temporaryPassword"]
        login(client, "persona.log", temp)
        change(client, temp, "una password nuova per i log")
        login(client, "persona.log", "sbagliata di proposito")
        change(client, "sbagliata", "passwordpassword")

    testo = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    for segreto in [temp, PW_ADMIN, "una password nuova per i log",
                    "sbagliata di proposito", "passwordpassword"]:
        assert segreto not in testo, "un segreto è finito nei log"
    assert ARGON2_PREFIX not in testo


def test_the_user_list_never_exposes_hash_or_internals(client, engine):
    login(client, "capo", PW_ADMIN)
    r = client.get("/api/users?includeDisabled=true")
    assert r.status_code == 200
    corpo = json.dumps(r.json(), ensure_ascii=False)

    assert ARGON2_PREFIX not in corpo
    for chiave in ["password_hash", "passwordHash", "salt", "sale", "hash",
                   "temporaryPassword", "password"]:
        assert f'"{chiave}"' not in corpo, chiave
    # E i campi che DEVONO esserci ci sono, altrimenti passerebbe su una lista vuota.
    assert len(r.json()) == 3
    assert set(r.json()[0]) == {"id", "username", "role", "mustChangePassword",
                                "disabled", "nome", "cognome", "telefono", "team",
                                "lastLoginAt", "createdAt"}


def test_the_sanitiser_still_treats_argon2_material_as_sensitive():
    """La ripulitura dell'audit riconosce ancora un hash Argon2 e le chiavi
    sensibili. Se qualcuno cambiasse i suoi schemi, questa è la rete."""
    from app.audit.sanitize import REDACTED, contains_secret, sanitize
    h = hash_password("una password qualunque lunga")
    assert sanitize({"qualcosa": h})["qualcosa"] == REDACTED
    assert sanitize({"passwordNuova": "in chiaro"})["passwordNuova"] == REDACTED
    assert sanitize({"tempPasswordHash": "x"})["tempPasswordHash"] == REDACTED
    assert contains_secret(json.dumps({"x": h}))


# ==========================================================================
# 9. La resistenza all'enumerazione non è peggiorata
# ==========================================================================

def test_unknown_user_and_wrong_password_are_indistinguishable(client):
    """Stesso stato, stesso corpo. La politica non ha aggiunto un canale."""
    a = login(client, "op", "questa e sbagliata assai")
    b = login(client, "non-esiste-affatto", "questa e sbagliata assai")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_a_blocklisted_password_at_login_looks_like_any_wrong_password(client):
    """La lista NON si consulta all'accesso, ed è importante che non si veda.

    Se l'accesso rispondesse `password_blocklisted`, chi prova imparerebbe due
    cose: che quel valore è in lista, e — dal fatto che il controllo è avvenuto —
    che l'utenza esiste. Deve essere un 401 identico a tutti gli altri.
    """
    a = login(client, "op", "passwordpassword")
    b = login(client, "op", "una password sbagliata qualunque")
    c = login(client, "non-esiste-affatto", "passwordpassword")
    assert a.status_code == b.status_code == c.status_code == 401
    assert a.json() == b.json() == c.json()
    assert "blocklist" not in json.dumps(a.json())


def test_a_too_short_password_at_login_is_not_rejected_by_policy(client):
    """Anche una password che la politica non accetterebbe deve dare 401 all'accesso.

    Chi ha una password di prima della politica deve poter entrare — e non deve
    scoprire di essere fuori regola durante un accesso, da dove non c'è via
    d'uscita. La lunghezza non è una condizione di accesso.
    """
    r = login(client, "op", "corta")
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_credentials"


def test_a_pre_policy_short_password_still_works_for_login(client, engine):
    """Il caso concreto: un'utenza con una password da 10 caratteri, come le
    ammetteva il contratto precedente. Entra, e le viene riscritto l'hash se serve,
    ma non le si sbarra la strada."""
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE username = 'op'"),
                  {"h": hash_password("vecchia10c")})

    assert login(client, "op", "vecchia10c").status_code == 200
    # Ma non la può RIMETTERE: la politica vale all'impostazione.
    r = change(client, "vecchia10c", "vecchia10c")
    assert r.status_code == 422


def test_the_dummy_hash_still_costs_a_real_verification(client, engine):
    """L'hash di confronto per le utenze inesistenti esiste ancora ed è Argon2id con
    i parametri correnti.

    Se fosse rimasto indietro rispetto alla configurazione, un'utenza inesistente
    costerebbe MENO di una esistente e la differenza di tempo tornerebbe a essere
    misurabile — l'enumerazione che il dummy serve a impedire.
    """
    from app.auth.service import _DUMMY_HASH
    assert _DUMMY_HASH.startswith(ARGON2_PREFIX)
    assert needs_rehash(_DUMMY_HASH) is False
    assert (f"m={ARGON2_MEMORY_COST},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}"
            in _DUMMY_HASH)


def test_rate_limiting_still_applies_to_failed_logins(client, engine):
    """Il limitatore non è stato disturbato: dopo la soglia arriva 429.

    Non è un doppione: aggiungere la politica ha toccato il ramo dell'accesso
    (la riscrittura dell'hash), e questo prova che il conteggio dei tentativi
    continua a funzionare come prima.
    """
    esiti = [login(client, "op", f"sbagliata numero {i}").status_code
             for i in range(12)]
    assert 429 in esiti, esiti
    # E la finestra riguarda i tentativi FALLITI, non l'utenza in sé: la risposta
    # resta 429 anche con la password giusta, finché il blocco è attivo.
    assert login(client, "op", PW_OP).status_code == 429


def test_a_policy_rejection_does_not_count_as_a_failed_login(client, engine):
    """Un 422 sulla rotta della password non deve consumare il budget dei tentativi.

    Sono due cose diverse: sbagliare la password attuale è un tentativo, scegliere
    una password nuova non conforme no. Confonderle bloccherebbe fuori un utente
    legittimo che sta solo cercando una password che vada bene.
    """
    login(client, "op", PW_OP)
    with engine.connect() as c:
        prima = c.execute(text("SELECT count(*) FROM login_attempts")).scalar_one()
    for _ in range(8):
        assert change(client, PW_OP, "corta").status_code == 422
    with engine.connect() as c:
        dopo = c.execute(text("SELECT count(*) FROM login_attempts")).scalar_one()
    assert dopo == prima
    # E l'utente può ancora cambiarla per davvero.
    assert change(client, PW_OP, "adesso una che va bene").status_code == 204


# ==========================================================================
# 10. Un rifiuto non lascia stato
# ==========================================================================

def test_a_rejected_password_leaves_no_trace_at_all(client, engine):
    """Nessun hash cambiato, nessuna sessione revocata, nessun audit, nessuna
    versione di inventario: il rifiuto è un non-evento."""
    login(client, "op", PW_OP)
    prima_hash = stored_hash(engine, "op")
    prima_sessioni = live_sessions(engine, "op")
    prima_versioni = version_count(engine)
    with engine.connect() as c:
        prima_audit = c.execute(text("SELECT count(*) FROM audit")).scalar_one()

    for pw in ["corta", "x" * 200, "passwordpassword", PW_OP]:
        assert change(client, PW_OP, pw).status_code == 422

    assert stored_hash(engine, "op") == prima_hash
    assert live_sessions(engine, "op") == prima_sessioni
    assert version_count(engine) == prima_versioni
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit")).scalar_one() == prima_audit


def test_a_rejected_creation_leaves_no_user_and_no_audit(client, engine):
    """Il rifiuto di una creazione non lascia l'utenza a metà né una riga di
    registro che dica che è stata creata."""
    login(client, "capo", PW_ADMIN)
    actor = Actor(username="capo", role="admin", user_id=uid_of(engine, "capo"))
    with engine.begin() as c:
        with pytest.raises(PasswordRejected):
            user_svc.create_user(c, username="mai.nata", role="view", actor=actor,
                                 password="corta")

    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users WHERE username = :u"),
                         {"u": "mai.nata"}).scalar_one() == 0
    assert "mai.nata" not in audit_dump(engine)


def test_the_response_to_a_rejection_never_echoes_the_password(client):
    """Il messaggio nomina il limite, mai il valore.

    Passa dai log del client, dalla console del browser e — se qualcuno lo copia
    in un ticket — da lì. Una password rifiutata è comunque un segreto.
    """
    login(client, "op", PW_OP)
    segreta = "zqx-quasi-giusta-particolare"
    for pw in [segreta[:10], segreta * 8, "passwordpassword"]:
        r = change(client, PW_OP, pw)
        corpo = r.text
        assert pw not in corpo
        assert pw[:10] not in corpo
        # E nemmeno la password attuale, che il client ha inviato nella stessa richiesta.
        assert PW_OP not in corpo
