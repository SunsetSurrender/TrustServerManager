"""Scansione delle scadenze e composizione del digest. Suite pura.

Nessun database, nessun SMTP: qui si provano le regole di calendario e la
composizione del messaggio, che sono le due cose che si possono sbagliare in
silenzio.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fixtures" / "expiry"))
from build import NOME_OSTILE, build_inventory  # noqa: E402

from app.notifications.digest import build_digest, sanitise_field  # noqa: E402
from app.notifications.expiry import (  # noqa: E402
    applicable_thresholds,
    due_items,
    local_today,
    parse_expiry,
)

TODAY = date(2026, 8, 10)
WINDOWS = [90, 30, 7]


def inv():
    return build_inventory(TODAY)


def scan(warning_days=None, today=TODAY):
    # `if warning_days is None`, non `warning_days or ...`: un elenco VUOTO è un
    # caso da provare («nessuna finestra configurata») e con `or` diventerebbe il
    # default, facendo passare il test per il motivo sbagliato.
    windows = WINDOWS if warning_days is None else warning_days
    return due_items(inv(), today=today, warning_days=windows)


def by_device(items):
    return {i.device: i for i in items}


# ==================================================================
# calendario locale, non mezzanotte UTC
# ==================================================================

def test_local_date_differs_from_utc_date_late_in_the_evening():
    """A Roma, le 23:30 del 10 agosto UTC sono già l'11 agosto locale.

    Confrontare le scadenze con la data UTC sposterebbe il confine di un giorno
    per una parte dell'anno, e un promemoria a 30 giorni scatterebbe il giorno
    sbagliato."""
    instant = datetime(2026, 8, 10, 23, 30, tzinfo=timezone.utc)
    assert local_today(instant, "Europe/Rome") == date(2026, 8, 11)
    assert local_today(instant, "UTC") == date(2026, 8, 10)


def test_local_date_uses_the_configured_zone():
    instant = datetime(2026, 8, 10, 3, 30, tzinfo=timezone.utc)
    assert local_today(instant, "Europe/Rome") == date(2026, 8, 10)
    assert local_today(instant, "America/New_York") == date(2026, 8, 9)


def test_naive_instant_is_treated_as_utc():
    assert local_today(datetime(2026, 8, 10, 12, 0), "UTC") == date(2026, 8, 10)


# ==================================================================
# lettura delle date
# ==================================================================

@pytest.mark.parametrize("raw,expected", [
    ("2026-08-10", date(2026, 8, 10)),
    ("  2026-08-10  ", date(2026, 8, 10)),
])
def test_valid_dates_are_parsed(raw, expected):
    assert parse_expiry(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", None, 20260810, "10/08/2026", "2026-8-1", "in attesa",
    "2026-13-45", "2026-02-30", "2026-08-10T00:00:00Z", [], {},
])
def test_unparseable_values_are_ignored_not_fatal(raw):
    """Il campo è testo scritto a mano: una data impossibile o una nota
    («in attesa») sono casi normali, e non devono far cadere il giro del worker.
    Si notano nella vista Scadenze, non in un avviso via posta."""
    assert parse_expiry(raw) is None


# ==================================================================
# finestre: la disuguaglianza, non il giorno esatto
# ==================================================================

def test_item_expiring_today_is_in_window():
    """`days_remaining == 0` è dentro: la garanzia scade oggi, l'avviso serve."""
    assert "srv-oggi" in by_device(scan())


def test_expired_items_are_excluded():
    """Escluso per scelta esplicita: un avviso su una scadenza già passata è un
    prodotto diverso (si ripete ogni giorno? mai?) e questo commit non lo decide."""
    got = by_device(scan())
    assert "srv-scaduto" not in got
    assert "srv-scaduto-ieri" not in got, "il confine -1 deve essere fuori"


def test_item_beyond_the_widest_window_is_excluded():
    assert "srv-91" not in by_device(scan())


def test_item_at_the_widest_window_is_included():
    assert "srv-90" in by_device(scan())


def test_devices_without_dates_are_absent():
    got = by_device(scan())
    assert "srv-senza-date" not in got
    assert "srv-data-rotta" not in got


def test_both_expiry_kinds_are_reported_for_the_same_device():
    """Un dispositivo con garanzia E supporto in scadenza produce DUE voci: sono
    due scadenze diverse, e accorparle perderebbe una delle due date."""
    kinds = {i.kind for i in scan() if i.device == "srv-30"}
    assert kinds == {"garanzia", "supporto"}


def test_days_remaining_is_computed_from_the_given_today():
    got = by_device(scan())
    assert got["srv-7"].days_remaining == 7
    assert got["srv-6"].days_remaining == 6
    assert got["srv-oggi"].days_remaining == 0


def test_same_business_id_different_uid_are_two_items():
    """L'identità è l'`_uid`, non il codice scritto dall'utente. Raggruppando per
    `id`, uno dei due dispositivi non riceverebbe l'avviso — e negli inventari
    importati da fogli di calcolo gli id ripetuti sono la norma."""
    dups = [i for i in scan() if i.device in ("dup-a", "dup-b")]
    assert len(dups) == 2
    assert len({i.entity_uid for i in dups}) == 2


def test_context_comes_from_the_document_tree():
    """⚠ Le ETICHETTE, non i codici, dalla fase 2G (§9 del requisito, §8.50.9).

    La catena è nome mostrabile → codice → «(senza nome)», quindi il sito `pomezia`
    che si chiama «Pomezia G0» compare col nome. Per un'email a una persona è la forma
    giusta; il codice resta disponibile a chi legge l'API, che restituisce `code`,
    `name` e `label` separati.

    Il rack `R01` ha `name: "R01"` — nome e codice coincidono — quindi il valore non
    cambia. È un caso in cui la differenza non si vede, e per questo il test fissa
    anche i due dove si vede.
    """
    got = by_device(scan())
    assert got["srv-7"].location == "Pomezia G0"   # prima: «pomezia» (il codice)
    assert got["srv-7"].room == "Sala 1"           # prima: «sala-1»
    assert got["srv-7"].rack == "R01"              # nome == codice: invariato
    assert got["dup-a"].rack == "R02"

    # ⚠ E la proprietà che regge tutto: sono TRE VALORI separati, non una stringa
    # spezzata dopo. Nessuno dei tre contiene il separatore che il vecchio percorso
    # impacchettato usava.
    voce = got["srv-7"]
    assert " / " not in voce.location + voce.room + voce.rack


def test_ordering_is_deterministic():
    """Due esecuzioni sullo stesso inventario devono produrre lo stesso digest,
    riga per riga: senza un ordine stabile il messaggio cambierebbe da un giorno
    all'altro senza che sia cambiato niente."""
    assert [(i.entity_uid, i.kind) for i in scan()] \
        == [(i.entity_uid, i.kind) for i in scan()]
    assert [i.days_remaining for i in scan()] == sorted(i.days_remaining
                                                        for i in scan())


def test_no_windows_means_nothing_due():
    assert scan(warning_days=[]) == []


def test_narrow_window_excludes_far_items():
    got = by_device(scan(warning_days=[7]))
    assert "srv-7" in got and "srv-6" in got and "srv-oggi" in got
    assert "srv-30" not in got and "srv-90" not in got


# ==================================================================
# soglie applicabili e precedenza
# ==================================================================

@pytest.mark.parametrize("days,expected", [
    (0, [7, 30, 90]),
    (5, [7, 30, 90]),
    (7, [7, 30, 90]),
    (8, [30, 90]),
    (30, [30, 90]),
    (31, [90]),
    (90, [90]),
    (91, []),
])
def test_applicable_thresholds(days, expected):
    """Tutte le soglie che coprono quel numero di giorni, dalla più urgente.
    Restituirne una sola qui impedirebbe di sapere quali marcare superate."""
    assert applicable_thresholds(days, WINDOWS) == expected


def test_negative_days_have_no_applicable_threshold():
    assert applicable_thresholds(-1, WINDOWS) == []


# ==================================================================
# digest: testo non attendibile
# ==================================================================

def entries(items):
    return [{"reminder_id": n, "threshold_days": 30, "item": i}
            for n, i in enumerate(items, start=1)]


def digest_of(items, recipients=("a@example.internal",)):
    return build_digest(entries(items), sender="ced@example.internal",
                        recipients=list(recipients),
                        message_id="<fisso@tsm.local>",
                        now=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
                        today=TODAY)


def test_hostile_device_name_does_not_create_a_header():
    """Il caso che conta: un nome con `\\r\\nBcc:` non deve aggiungere un
    destinatario. I nomi li scrive una persona, spesso incollandoli."""
    hostile = [i for i in scan() if i.entity_uid.endswith("30")]
    assert hostile, "la fixture deve contenere il dispositivo ostile"
    msg = digest_of(hostile)
    assert msg["Bcc"] is None
    assert "Bcc" not in [k for k in msg.keys()]
    assert msg["To"] == "a@example.internal"


def test_hostile_name_survives_as_plain_body_text():
    """Non si censura il nome: un dispositivo che si chiama `<b>` si chiama così.
    Si neutralizzano i CARATTERI DI CONTROLLO, non i caratteri sospetti."""
    msg = digest_of([i for i in scan() if i.entity_uid.endswith("30")])
    body = msg.get_content()
    assert "<b>srv-x</b>" in body
    assert "\r" not in body.replace("\r\n", "\n")


def test_control_characters_are_replaced():
    assert "\n" not in sanitise_field("a\nb")
    assert "\r" not in sanitise_field("a\r\nb")
    assert sanitise_field("a\x00b") == "a b"


def test_empty_field_becomes_a_dash():
    assert sanitise_field("") == "—"
    assert sanitise_field(None) == "—"


def test_overlong_field_is_truncated():
    out = sanitise_field("x" * 500)
    assert len(out) <= 60 and out.endswith("…")


def test_subject_is_server_defined_and_carries_a_count():
    """L'oggetto è un'intestazione: contiene un CONTEGGIO, mai un nome che venga
    dall'inventario."""
    items = scan()
    msg = digest_of(items)
    assert msg["Subject"] == f"Trust Server Manager — scadenze: {len(items)} in avvicinamento"
    assert NOME_OSTILE.split("\r")[0] not in msg["Subject"]


def test_message_id_comes_from_the_caller():
    """Generarlo dentro la composizione darebbe un identificativo nuovo a ogni
    ritentativo, cioè un secondo avviso agli occhi di chi lo riceve."""
    assert digest_of(scan())["Message-ID"] == "<fisso@tsm.local>"


def test_digest_groups_by_kind():
    body = digest_of(scan()).get_content()
    assert "GARANZIA" in body
    assert "CONTRATTO DI SUPPORTO" in body
    assert body.index("GARANZIA") < body.index("CONTRATTO DI SUPPORTO")


def test_digest_contains_only_safe_fields():
    """Nome, posizione, data e giorni. NON le note: sono testo libero che può
    contenere qualunque cosa e nessuno ha chiesto di spedirlo."""
    body = digest_of(scan()).get_content()
    # Etichette dalla 2G: il sito e la sala compaiono col nome (§8.50.9).
    assert "srv-7" in body and "R01" in body and "Sala 1" in body
    assert "note" not in body.lower()
    assert "serial" not in body.lower() and "ip" not in body.lower().split()


def test_digest_says_expired_items_are_elsewhere():
    body = digest_of(scan()).get_content()
    assert "già scadut" in body


def test_all_recipients_appear_in_the_to_header():
    msg = digest_of(scan(), recipients=("a@e.internal", "b@e.internal",
                                        "c@e.internal", "d@e.internal"))
    assert msg["To"] == "a@e.internal, b@e.internal, c@e.internal, d@e.internal"


def test_auto_submitted_header_prevents_autoresponder_loops():
    assert digest_of(scan())["Auto-Submitted"] == "auto-generated"
