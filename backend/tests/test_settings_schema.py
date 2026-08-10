"""Schema tipizzato delle impostazioni: validazione e forma canonica.

Suite pura: nessun database, nessun HTTP. Le regole vivono in un modulo che non
dipende da niente, e questi test le esercitano direttamente — senza dover
passare per una richiesta, che introdurrebbe altri modi di fallire.
"""
from __future__ import annotations

import copy

import pytest

from app.settings.schema import (
    DEFAULTS,
    MAX_RECIPIENTS,
    MAX_WARNING_DAYS,
    SettingsValidationError,
    canonicalise,
    default_document,
)


def body(**over) -> dict:
    """Un corpo valido, con i campi indicati sostituiti."""
    n = copy.deepcopy(DEFAULTS["notifications"])
    n["recipients"] = ["team@example.internal"]
    n.update(over)
    return {"notifications": n}


# ==================================================================
# forma canonica
# ==================================================================

def test_default_document_is_its_own_canonical_form():
    """La configurazione iniziale deve attraversare la canonicalizzazione
    invariata. Se non lo facesse, la prima PUT identica alla GET risulterebbe
    una modifica e la revisione salirebbe senza che nessuno abbia cambiato
    niente."""
    assert canonicalise(default_document()) == default_document()


def test_default_document_is_a_copy():
    d = default_document()
    d["notifications"]["recipients"].append("x@y.zz")
    assert default_document()["notifications"]["recipients"] == []


def test_canonical_form_is_stable_under_reordering():
    a = canonicalise(body(warningDays=[90, 7, 30]))
    b = canonicalise(body(warningDays=[7, 30, 90]))
    assert a == b, "due scritture della stessa configurazione devono coincidere"


def test_notifications_disabled_survives_canonicalisation():
    """`enabled: false` è un valore esplicito, non un campo assente.

    È il caso che una canonicalizzazione scritta con `or` o con `bool(...)`
    sbaglia in silenzio, e lo sbaglia nella direzione peggiore: le notifiche si
    riaccendono da sole."""
    out = canonicalise(body(enabled=False))
    assert out["notifications"]["enabled"] is False


def test_notifications_enabled_true_survives():
    assert canonicalise(body(enabled=True))["notifications"]["enabled"] is True


def test_empty_recipients_list_survives():
    """Elenco vuoto: valore legittimo, non «campo da riempire con il default»."""
    assert canonicalise(body(recipients=[]))["notifications"]["recipients"] == []


# ==================================================================
# campi sconosciuti, di sola lettura, segreti
# ==================================================================

def test_unknown_top_level_field_is_rejected():
    payload = body()
    payload["colore"] = "rosso"
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "unknown_field"


def test_unknown_nested_field_is_rejected():
    payload = body()
    payload["notifications"]["ritardo"] = 5
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "unknown_field"
    assert e.value.field == "notifications.ritardo"


def test_missing_field_is_rejected_rather_than_defaulted():
    """PUT sostituisce. Un campo mancante NON prende il default: se lo prendesse,
    un client che dimentica `enabled` spegnerebbe le notifiche senza saperlo."""
    payload = body()
    del payload["notifications"]["enabled"]
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "missing_field"


@pytest.mark.parametrize("field", ["version", "smtp", "updatedAt", "updatedBy"])
def test_read_only_fields_are_rejected_with_their_own_code(field):
    """Rimandare indietro il documento della GET non è un errore qualsiasi: il
    client non ha inventato un campo, ne ha rispedito uno di sola lettura, e
    merita un messaggio che glielo dica."""
    payload = body()
    payload[field] = 1
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "read_only_field"


@pytest.mark.parametrize("payload", [
    {"notifications": {}, "password": "x"},
    {"notifications": {}, "smtpPassword": "x"},
    {"notifications": {"apiKey": "x"}},
    {"notifications": {"schedule": {"secret": "x"}}},
    {"notifications": {"schedule": {"nested": {"deep": {"token": "x"}}}}},
    {"notifications": {"recipients": [{"credential": "x"}]}},
    {"notifications": {"AUTHORIZATION": "Bearer x"}},
    {"notifications": {"session_hash": "x"}},
])
def test_secret_like_keys_are_rejected_at_any_depth(payload):
    """A qualunque profondità, e prima di ogni altro controllo.

    Un campo `password` sarebbe comunque rifiutato come sconosciuto: il codice
    dedicato esiste perché il messaggio dica la cosa giusta, e perché il giorno
    in cui lo schema crescerà di un sotto-oggetto quel sotto-oggetto nasca già
    protetto invece di dipendere dall'attenzione di chi lo aggiunge."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "secret_field_rejected", e.value.code


def test_secret_check_precedes_structural_validation():
    """Il corpo è strutturalmente sbagliato IN PIÙ MODI: deve vincere il segreto."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise({"password": "x", "quisquiglia": 1})
    assert e.value.code == "secret_field_rejected"


def test_error_never_repeats_the_rejected_value():
    """Il messaggio nomina il campo, mai il valore: un valore rifiutato può
    essere proprio il segreto che si sta cercando di tenere fuori."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise({"notifications": {"password": "correct-horse-battery"}})
    assert "correct-horse-battery" not in e.value.message
    assert "correct-horse-battery" not in str(e.value.field or "")


def test_deeply_nested_payload_is_rejected():
    deep: dict = {"a": 1}
    for _ in range(20):
        deep = {"a": deep}
    with pytest.raises(SettingsValidationError) as e:
        canonicalise({"notifications": deep})
    assert e.value.code == "document_too_complex"


# ==================================================================
# destinatari
# ==================================================================

@pytest.mark.parametrize("address", [
    "team@example.internal",
    "nome.cognome@par-tec.it",
    "ced+scadenze@example.co.uk",
    "a@b.cd",
])
def test_valid_recipients_are_accepted(address):
    out = canonicalise(body(recipients=[address]))
    assert out["notifications"]["recipients"] == [address]


@pytest.mark.parametrize("address", [
    "", "   ", "senza-chiocciola", "@example.it", "utente@",
    "utente@@example.it", "utente@dominio-senza-punto",
    "utente@-example.it", "utente@example-.it", "utente@.example.it",
    "utente@example..it", ".utente@example.it", "utente.@example.it",
    "due indirizzi@example.it", "a@b.it,c@d.it", "a@b.it;c@d.it",
    "a@b.it\nc@d.it",
])
def test_invalid_recipients_are_rejected(address):
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=[address]))
    assert e.value.code == "invalid_recipient", address


def test_recipient_is_trimmed():
    out = canonicalise(body(recipients=["  team@example.internal  "]))
    assert out["notifications"]["recipients"] == ["team@example.internal"]


def test_recipient_domain_is_lowercased_but_local_part_is_not():
    """Il dominio non distingue le maiuscole, la parte locale sì (RFC 5321).
    Abbassare anche quella significherebbe consegnare a un indirizzo diverso da
    quello che l'amministratore ha scritto."""
    out = canonicalise(body(recipients=["Mario.Rossi@EXAMPLE.INTERNAL"]))
    assert out["notifications"]["recipients"] == ["Mario.Rossi@example.internal"]


def test_duplicate_recipients_are_rejected():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=["a@b.it", "a@b.it"]))
    assert e.value.code == "duplicate_recipient"


def test_duplicates_are_detected_after_normalisation():
    """Due scritture diverse dello stesso indirizzo restano un duplicato: se
    passassero, ogni avviso arriverebbe in due copie."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=["  A@Example.IT ", "a@example.it"]))
    assert e.value.code == "duplicate_recipient"


def test_recipient_count_is_bounded():
    many = [f"u{i}@example.it" for i in range(MAX_RECIPIENTS + 1)]
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=many))
    assert e.value.code == "too_many_recipients"


def test_recipient_at_the_limit_is_accepted():
    many = [f"u{i}@example.it" for i in range(MAX_RECIPIENTS)]
    assert len(canonicalise(body(recipients=many))["notifications"]["recipients"]) \
        == MAX_RECIPIENTS


def test_recipient_length_is_bounded():
    long_local = "a" * 300 + "@example.it"
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=[long_local]))
    assert e.value.code == "invalid_recipient"


def test_long_local_part_is_bounded_separately():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients=["a" * 65 + "@example.it"]))
    assert e.value.code == "invalid_recipient"


def test_recipients_must_be_a_list():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(recipients="a@b.it"))
    assert e.value.code == "invalid_type"


def test_recipient_order_is_preserved():
    """L'ordine dei destinatari è informazione dell'amministratore: la
    canonicalizzazione serve a confrontare due documenti, non a riordinarli."""
    addresses = ["z@example.it", "a@example.it", "m@example.it"]
    assert canonicalise(body(recipients=addresses))["notifications"]["recipients"] \
        == addresses


# ==================================================================
# finestre di preavviso
# ==================================================================

def test_warning_days_are_deduplicated_and_sorted():
    out = canonicalise(body(warningDays=[30, 7, 30, 90, 7]))
    assert out["notifications"]["warningDays"] == [7, 30, 90]


@pytest.mark.parametrize("days", [[0], [-1], [-30]])
def test_non_positive_warning_days_are_rejected(days):
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(warningDays=days))
    assert e.value.code == "invalid_warning_day"


@pytest.mark.parametrize("days", [["30"], [7.5], [None], [True]])
def test_non_integer_warning_days_are_rejected(days):
    """`True` compreso: in Python è un `int`, e senza il controllo esplicito
    sul tipo entrerebbe come il giorno 1."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(warningDays=days))
    assert e.value.code == "invalid_warning_day"


def test_absurd_warning_day_is_rejected():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(warningDays=[100000]))
    assert e.value.code == "invalid_warning_day"


def test_warning_day_count_is_bounded():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(warningDays=list(range(1, MAX_WARNING_DAYS + 2))))
    assert e.value.code == "too_many_warning_days"


def test_empty_warning_days_is_allowed():
    """Nessuna finestra: le notifiche non hanno nulla da segnalare. È una
    configurazione strana ma coerente, e non tocca a questo livello vietarla."""
    assert canonicalise(body(warningDays=[]))["notifications"]["warningDays"] == []


# ==================================================================
# fuso orario
# ==================================================================

@pytest.mark.parametrize("tz", ["Europe/Rome", "UTC", "America/New_York"])
def test_valid_timezones_are_accepted(tz):
    assert canonicalise(body(timezone=tz))["notifications"]["timezone"] == tz


@pytest.mark.parametrize("tz", [
    "", "   ", "Europa/Roma", "CET+1", "Mars/Olympus", "europe/rome",
    "../../etc/passwd", "/etc/localtime", "Europe/Rome\x00",
])
def test_invalid_timezones_are_rejected(tz):
    """`europe/rome` compreso: il database dei fusi distingue le maiuscole, e
    accettarlo qui significherebbe salvarlo e vederlo fallire allo scheduler.

    I due percorsi verificano che il nome non possa diventare una lettura di
    file: `ZoneInfo` li rifiuta, e il test lo fissa."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(timezone=tz))
    assert e.value.code in ("invalid_timezone", "invalid_type"), tz


def test_timezone_must_be_a_string():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(timezone=2))
    assert e.value.code == "invalid_type"


# ==================================================================
# orario di invio
# ==================================================================

def test_valid_schedule_is_accepted():
    out = canonicalise(body(schedule={"hour": 23, "minute": 59}))
    assert out["notifications"]["schedule"] == {"hour": 23, "minute": 59}


def test_midnight_is_accepted():
    """`{hour: 0, minute: 0}`: due zeri espliciti, non «campo non impostato»."""
    out = canonicalise(body(schedule={"hour": 0, "minute": 0}))
    assert out["notifications"]["schedule"] == {"hour": 0, "minute": 0}


@pytest.mark.parametrize("schedule", [
    {"hour": 24, "minute": 0}, {"hour": -1, "minute": 0},
    {"hour": 0, "minute": 60}, {"hour": 0, "minute": -1},
    {"hour": "8", "minute": 0}, {"hour": 8, "minute": None},
    {"hour": True, "minute": 0},
])
def test_invalid_schedule_is_rejected(schedule):
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(schedule=schedule))
    assert e.value.code == "invalid_schedule"


@pytest.mark.parametrize("schedule", [{"hour": 8}, {"minute": 0}, {}])
def test_incomplete_schedule_is_rejected(schedule):
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(schedule=schedule))
    assert e.value.code == "missing_field"


def test_schedule_must_be_an_object():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(schedule="08:00"))
    assert e.value.code == "invalid_type"


# ==================================================================
# tipi di primo livello
# ==================================================================

@pytest.mark.parametrize("payload", [None, [], "x", 3, True])
def test_non_object_payload_is_rejected(payload):
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(payload)
    assert e.value.code == "invalid_type"


def test_notifications_must_be_an_object():
    with pytest.raises(SettingsValidationError) as e:
        canonicalise({"notifications": []})
    assert e.value.code == "invalid_type"


@pytest.mark.parametrize("value", ["true", 1, 0, None, "false"])
def test_enabled_must_be_a_real_boolean(value):
    """`"false"` è la stringa non vuota che `bool(...)` trasformerebbe in `True`:
    le notifiche si accenderebbero da sole."""
    with pytest.raises(SettingsValidationError) as e:
        canonicalise(body(enabled=value))
    assert e.value.code == "invalid_type"
