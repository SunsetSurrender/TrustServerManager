"""La politica delle password e la configurazione di Argon2id. PURO.

Che cosa si fissa qui, e perché in un file solo: la politica è una, e prima era
tre — il cambio chiedeva dieci caratteri, il bootstrap e la creazione nessuno. Un
test per strada avrebbe descritto quella divergenza invece di impedirla, quindi le
regole si provano una volta sul modulo e poi si prova, sui test PG, che ogni strada
ci passi davvero.

⚠ I caratteri non ASCII si scrivono con SEQUENZE DI ESCAPE (`"\\u00e0"`), non
digitati. Vale la stessa ragione del corpus testuale dell'istantanea: in questa
sessione uno script consegnato a Python attraverso una pipe è stato decodificato
con la codepage locale di Windows e un accento è diventato un carattere diverso,
in silenzio. Un test che confronta la cosa sbagliata con la cosa sbagliata è verde.

Le cose che questo file NON può provare, e che stanno nei test PG:

  - che le tre strade chiamino davvero la politica;
  - che un cambio revochi le sessioni;
  - che nel database finisca solo l'hash;
  - che l'hash debole venga riscritto dopo un accesso riuscito.
"""
from __future__ import annotations

import re
import unicodedata

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from app.auth import passwords as P
from app.auth.passwords import (
    ARGON2_MEMORY_COST,
    ARGON2_MIN_MEMORY_COST,
    ARGON2_MIN_PARALLELISM,
    ARGON2_MIN_TIME_COST,
    ARGON2_PARALLELISM,
    ARGON2_PREFIX,
    ARGON2_TIME_COST,
    ARGON2_TYPE,
    MAX_LENGTH,
    MIN_LENGTH,
    PASSWORD_BLOCKLISTED,
    PASSWORD_NOT_ENCODABLE,
    PASSWORD_TOO_LONG,
    PASSWORD_TOO_SHORT,
    PasswordRejected,
    blocklist,
    check_policy,
    generate_temporary_password,
    hash_password,
    is_blocklisted,
    needs_rehash,
    normalise,
    policy_problem,
    verify_password,
)

# --- caratteri, scritti come SEQUENZE DI ESCAPE ---------------------------
#
# Nessuno di questi e' digitato, nemmeno quelli visibili come la a accentata:
# la regola vale per tutti o non vale, perche' e' la regola che protegge quelli
# invisibili. Scritta a meta', la prima stesura di questo file conteneva
# `NUL = " "` -- uno SPAZIO dove doveva esserci il byte zero -- e il test sul
# NUL avrebbe provato che uno spazio non tronca, restando verde per sempre.
A_GRAVE = "\u00e0"           # a con accento grave, forma composta
COMBINING_GRAVE = "\u0300"   # accento grave combinante
HIGH_SURROGATE = "\ud800"    # surrogato alto spaiato
LOW_SURROGATE = "\udc00"     # surrogato basso spaiato
ROCKET = "\U0001f680"        # emoji fuori dal BMP
NUL = "\x00"                 # il byte zero, MAI digitato

#: Una password valida e non in lista, da usare come base. 24 caratteri.
BUONA = "il gatto dorme sul tetto"


# =========================================================== Argon2id: scelta

def test_argon2_id_is_selected_explicitly_and_not_by_default():
    """L'algoritmo è Argon2**id**, scritto nella configurazione.

    Non si controlla «è quello che usa la libreria»: sarebbe una tautologia che
    resta verde il giorno in cui la libreria cambia idea. Si controlla che sia
    esattamente `Type.ID`, cioè la variante che resiste sia agli attacchi con
    accesso al canale laterale sia a quelli con hardware dedicato.
    """
    assert ARGON2_TYPE is Type.ID


def test_the_parameters_meet_or_exceed_the_required_baseline():
    """Le soglie minime sono costanti separate, non gli stessi numeri riletti.

    È l'unico modo di far fallire un abbassamento: se il test confrontasse
    `ARGON2_MEMORY_COST` con se stesso, cambiare 65536 in 4096 resterebbe verde.
    """
    assert ARGON2_MEMORY_COST >= ARGON2_MIN_MEMORY_COST
    assert ARGON2_TIME_COST >= ARGON2_MIN_TIME_COST
    assert ARGON2_PARALLELISM >= ARGON2_MIN_PARALLELISM
    # E le soglie sono quelle richieste, non numeri che qualcuno ha addolcito.
    assert (ARGON2_MIN_MEMORY_COST, ARGON2_MIN_TIME_COST, ARGON2_MIN_PARALLELISM) \
        == (19456, 2, 1)


def test_the_configuration_is_the_stronger_one_already_in_use():
    """64 MiB / t=3 / p=4: la configurazione forte non è stata declassata al minimo.

    Il requisito consente di tenerla, e tenerla è la scelta giusta: declassare
    riscriverebbe in PEGGIO ogni hash esistente al primo accesso, perché la
    riscrittura scatta su qualunque differenza di parametri, non solo in salita.
    """
    assert (ARGON2_MEMORY_COST, ARGON2_TIME_COST, ARGON2_PARALLELISM) == (65536, 3, 4)


def test_the_encoded_hash_says_argon2id_and_carries_the_parameters():
    """I parametri vivono DENTRO l'hash: è ciò che rende possibile l'aggiornamento.

    Verificarlo qui vale anche come documentazione del formato su cui si appoggia
    `needs_rehash`, e come prova che nel database non finisce un formato diverso.
    """
    h = hash_password(BUONA)
    assert h.startswith(ARGON2_PREFIX)
    assert "$v=19$" in h
    assert f"m={ARGON2_MEMORY_COST},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}" in h


# ================================================================ sale unico

def test_the_same_password_for_two_users_gives_two_different_hashes():
    """Il requisito centrale del sale, provato come lo si chiede.

    Due utenti, la stessa password identica: gli hash devono differire e verificare
    entrambi. Se il sale fosse globale, derivato dallo username o dall'id, gli hash
    sarebbero uguali — e un attaccante che ne rompe uno saprebbe di aver rotto anche
    l'altro senza rifare il lavoro.
    """
    pw = "la stessa password per due"
    hash_a = hash_password(pw)          # utente A
    hash_b = hash_password(pw)          # utente B

    assert hash_a != hash_b
    assert verify_password(hash_a, pw)
    assert verify_password(hash_b, pw)


def test_the_encoded_hashes_prove_the_salts_are_independent():
    """Si guarda dentro: sale e digest sono due campi distinti, e i sali differiscono.

    Non basta `hash_a != hash_b` — differirebbero anche con lo stesso sale se
    cambiasse un parametro. Qui si isola il campo del SALE e si verifica che sia
    quello a essere diverso, a parametri identici.
    """
    pw = "un'altra password uguale"
    a, b = hash_password(pw), hash_password(pw)

    # $argon2id$v=19$m=...,t=...,p=...$<sale>$<digest>
    parti_a, parti_b = a.split("$"), b.split("$")
    assert len(parti_a) == 6 and len(parti_b) == 6
    algo_a, ver_a, params_a, sale_a, digest_a = parti_a[1:]
    algo_b, ver_b, params_b, sale_b, digest_b = parti_b[1:]

    assert (algo_a, ver_a, params_a) == (algo_b, ver_b, params_b)   # stessi parametri
    assert sale_a != sale_b                                        # sali diversi
    assert digest_a != digest_b                                    # quindi digest diversi
    # Il sale è presente e ha la dimensione configurata (base64 senza padding).
    assert len(sale_a) >= 20


def test_a_hundred_hashes_of_one_password_are_a_hundred_different_salts():
    """Nessuna collisione su un campione: il sale viene da un CSPRNG, non da un
    contatore né da un valore derivato."""
    pw = "password ripetuta cento volte"
    sali = {hash_password(pw).split("$")[4] for _ in range(100)}
    assert len(sali) == 100


def test_wrong_password_fails_against_a_valid_hash():
    h = hash_password(BUONA)
    assert verify_password(h, BUONA)
    assert not verify_password(h, BUONA + "x")
    assert not verify_password(h, "del tutto un'altra cosa")


def test_verify_never_raises_on_a_broken_stored_hash():
    """Un hash illeggibile è «no», non un'eccezione: chi chiama non deve avvolgere
    ogni verifica in un try, perché prima o poi qualcuno se ne dimentica."""
    for rotto in ["", "non-un-hash", "$argon2id$rotto", "$2b$12$" + "x" * 53]:
        assert verify_password(rotto, BUONA) is False


# ============================================================== lunghezze

@pytest.mark.parametrize("length,expected", [
    (0, PASSWORD_TOO_SHORT),
    (1, PASSWORD_TOO_SHORT),
    (13, PASSWORD_TOO_SHORT),
    (14, PASSWORD_TOO_SHORT),      # il confine, dal lato che rifiuta
    (15, None),                    # il confine, dal lato che passa
    (16, None),
    (127, None),
    (128, None),                   # il massimo, incluso
    (129, PASSWORD_TOO_LONG),      # uno oltre
    (1000, PASSWORD_TOO_LONG),
])
def test_the_length_boundaries(length, expected):
    """14 no, 15 sì, 128 sì, 129 no.

    La password di prova NON è `"a" * length`: `aaaaaaaaaaaaaaa` è in lista, e il
    test avrebbe riportato `password_blocklisted` dove si aspettava `None`. Se ne è
    accorta la sonda, non il test — motivo per cui la base è una sequenza variata.
    """
    pw = ("frase lunga per il test " * 50)[:length]
    assert len(pw) == length
    problem = policy_problem(pw)
    assert (problem[0] if problem else None) == expected


def test_a_long_password_is_rejected_and_never_truncated():
    """Rifiutare, non tagliare.

    Troncare renderebbe equivalenti due password diverse: chi ne digita 200
    crederebbe di averne 200, e chiunque ne conosca le prime 128 entrerebbe. Si
    verifica proprio quello: la password lunga non si può usare, e la sua testa
    non è un sostituto.
    """
    lunga = ("passphrase interminabile " * 20)[:200]
    with pytest.raises(PasswordRejected) as info:
        check_policy(lunga)
    assert info.value.code == PASSWORD_TOO_LONG

    # E la prova che non c'è troncamento da nessuna parte nella catena: l'hash
    # della testa non verifica la password intera.
    testa = lunga[:MAX_LENGTH]
    assert not verify_password(hash_password(testa), lunga)


def test_the_error_message_says_the_limit_and_never_the_password():
    """Il messaggio nomina il limite; il valore rifiutato non compare.

    Una password rifiutata è comunque un segreto — spesso è quella *quasi* giusta
    di quella persona — e questo messaggio passa dai log e dalla risposta HTTP.
    """
    segreta = "zqx-segretissima-particolare-9182"
    for pw in ["corta", segreta * 10, "passwordpassword"]:
        problem = policy_problem(pw)
        assert problem is not None
        code, message = problem
        assert pw not in message
        assert pw[:12] not in message
    assert str(MIN_LENGTH) in policy_problem("corta")[1]
    assert str(MAX_LENGTH) in policy_problem("x" * 500)[1]


# ============================================================ composizione

@pytest.mark.parametrize("pw", [
    "tutto minuscolo senza altro",          # nessuna maiuscola
    "TUTTO MAIUSCOLO SENZA ALTRO",          # nessuna minuscola
    "nessuna cifra qui presente",           # nessuna cifra
    "nessun simbolo qui presente",          # nessun simbolo
    "solo lettere e spazi va bene",         # niente di niente
])
def test_no_composition_rule_is_imposed(pw):
    """Niente maiuscole/cifre/simboli obbligatori.

    Sono requisiti che spostano il costo sull'utente e la sicurezza da nessuna
    parte: producono `Estate2026!`. Il lavoro lo fanno la lunghezza e la lista.
    """
    assert policy_problem(pw) is None


def test_spaces_and_passphrases_are_allowed_including_at_the_edges():
    """Gli spazi sono ammessi, anche iniziali e finali, e NON si tolgono.

    Rimuoverli significherebbe accettare all'accesso una password diversa da quella
    impostata. Chi incolla uno spazio di troppo riceve «credenziali errate», che si
    recupera; una ripulitura silenziosa no.
    """
    con_spazi = "  quattro parole con spazi  "
    assert policy_problem(con_spazi) is None
    assert normalise(con_spazi) == con_spazi          # invariata, bordi compresi

    h = hash_password(con_spazi)
    assert verify_password(h, con_spazi)
    assert not verify_password(h, con_spazi.strip())   # sono due password diverse


def test_no_routine_expiry_exists_anywhere_in_the_policy():
    """Nessuna scadenza periodica: non esiste il concetto nel modulo.

    Un cambio si impone dopo un reset amministrativo, per una provvisoria o per un
    sospetto di compromissione — cioè per un EVENTO, non per il calendario. Se un
    domani qualcuno aggiungesse `MAX_AGE_DAYS` qui, questo test lo segnala.
    """
    nomi = dir(P)
    assert not [n for n in nomi
                if re.search(r"expiry|expire|scaden|max_age|age_days", n, re.I)]


# =============================================================== Unicode

def test_non_ascii_passwords_work():
    pw = "perimetrale gi" + A_GRAVE + " verificata"
    assert policy_problem(pw) is None
    assert verify_password(hash_password(pw), pw)


def test_emoji_passwords_work_when_otherwise_valid():
    """Un emoji è fuori dal BMP: due unità UTF-16, un code point.

    La lunghezza si misura in code point, quindi un emoji conta uno — ed è giusto
    così, perché è un carattere per chi lo digita.
    """
    pw = "la mia passphrase " + ROCKET + ROCKET
    assert policy_problem(pw) is None
    assert verify_password(hash_password(pw), pw)
    # E uno troppo corto resta troppo corto: l'emoji non vale due.
    corta = ROCKET * 14
    assert len(corta) == 14
    assert policy_problem(corta)[0] == PASSWORD_TOO_SHORT


def test_nfc_makes_the_two_canonical_forms_the_same_password():
    """Composta e decomposta sono la STESSA password, in ogni verso.

    È la ragione per cui la normalizzazione esiste: la stessa persona, la stessa
    tastiera, e un sistema operativo che consegna una forma invece dell'altra. Senza
    NFC l'accesso verrebbe negato e niente risulterebbe sbagliato da nessuna parte.
    """
    composta = "citt" + A_GRAVE + " di Pomezia ok"
    decomposta = "citta" + COMBINING_GRAVE + " di Pomezia ok"
    assert composta != decomposta                     # stringhe diverse
    assert normalise(composta) == normalise(decomposta)

    assert verify_password(hash_password(composta), decomposta)
    assert verify_password(hash_password(decomposta), composta)


def test_normalisation_is_nfc_and_not_nfd():
    """La forma scelta è NFC, dichiarata: è quella che lascia invariati più input reali."""
    decomposta = "citta" + COMBINING_GRAVE
    assert normalise(decomposta) == unicodedata.normalize("NFC", decomposta)
    assert normalise(decomposta) != unicodedata.normalize("NFD", decomposta)


def test_length_is_measured_after_normalisation_consistently():
    """NFC può ACCORCIARE, e allora la password è corta per davvero.

    15 code point decomposti che diventano 14 composti sono 14: misurare prima della
    normalizzazione accetterebbe in impostazione una password che poi vive come una
    di lunghezza diversa. Misurato: 15 → 14.
    """
    decomposta = "aaaaaaaaaaaaaa" + COMBINING_GRAVE       # 15 code point
    assert len(decomposta) == 15
    assert len(normalise(decomposta)) == 14               # NFC la accorcia
    assert policy_problem(decomposta)[0] == PASSWORD_TOO_SHORT

    # E dall'altro lato: 16 decomposti che diventano 15 vanno bene.
    quasi = "aaaaaaaaaaaaaaa" + COMBINING_GRAVE           # 16 → 15
    assert len(normalise(quasi)) == 15
    assert policy_problem(quasi) is None


def test_normalisation_does_not_clean_the_password():
    """NFC e nient'altro: niente casefold, niente strip, niente sostituzioni.

    Una password è una sequenza di code point scelta dall'utente, non un campo da
    ripulire. Il maiuscolo/minuscolo conta per Argon2, e deve continuare a contare.
    """
    assert normalise("Password Con Maiuscole") == "Password Con Maiuscole"
    assert normalise("\ttab e newline\n interni ") == "\ttab e newline\n interni "
    assert normalise("doppi  spazi  interni  qui") == "doppi  spazi  interni  qui"

    h = hash_password("Maiuscole E Minuscole")
    assert not verify_password(h, "maiuscole e minuscole")


def test_a_nul_inside_a_password_is_accepted_and_does_not_truncate():
    """Misurato: per Argon2 il NUL è innocuo e non tronca.

    Non si riusa qui la regola di rappresentabilità dell'istantanea, che rifiuta il
    NUL: quella risponde a un'altra domanda — se PostgreSQL conserva una stringa in
    `text`/`jsonb` — e una password non finisce in nessuna colonna, ci finisce il
    suo hash, che è ASCII. Riusarla rifiuterebbe password legittime per un motivo
    che qui non esiste.

    La parte che conta è la seconda asserzione: se il NUL troncasse — come fa
    qualche implementazione C di bcrypt — due password diverse diventerebbero la
    stessa, e questo test diventerebbe rosso.
    """
    pw = "prima parte" + NUL + "seconda parte"
    assert policy_problem(pw) is None

    h = hash_password(pw)
    assert verify_password(h, pw)
    assert not verify_password(h, "prima parte" + NUL + "TERZA parte")
    assert not verify_password(h, "prima parte")


@pytest.mark.parametrize("surrogato", [HIGH_SURROGATE, LOW_SURROGATE])
def test_a_lone_surrogate_is_rejected_with_a_stable_code(surrogato):
    """Un surrogato spaiato non è codificabile in UTF-8: si rifiuta all'IMPOSTAZIONE.

    Senza questo controllo il difetto si manifesta due volte e male: `hash_password`
    solleva `UnicodeEncodeError`, che nessuno mappa e diventa un 503 «servizio non
    disponibile» per un dato del client; e se un hash nascesse comunque, l'utenza
    sarebbe **inaccessibile per sempre**, perché `verify_password` intercetta
    `ValueError` — di cui `UnicodeEncodeError` è sottoclasse — e risponderebbe per
    sempre «credenziali errate» senza che niente sia errato.
    """
    pw = "una password lunga" + surrogato
    problem = policy_problem(pw)
    assert problem is not None and problem[0] == PASSWORD_NOT_ENCODABLE
    assert pw not in problem[1]

    with pytest.raises(PasswordRejected):
        check_policy(pw)


def test_the_lone_surrogate_gap_was_real_not_theoretical():
    """La prova che il controllo serve: senza, questo è ciò che accadeva.

    `hash_password` solleva davvero — un errore del server per un dato del client —
    e `verify_password` risponde davvero False, che è il meccanismo del blocco
    permanente. Se un giorno argon2-cffi accettasse i surrogati, questo test
    diventerebbe rosso e il controllo si potrebbe rimuovere con cognizione.
    """
    pw = "una password lunga" + HIGH_SURROGATE
    with pytest.raises(UnicodeEncodeError):
        PasswordHasher().hash(pw)
    assert isinstance(UnicodeEncodeError("utf-8", "", 0, 1, ""), ValueError)
    assert verify_password(hash_password("password buona e lunga"), pw) is False


# ================================================================== lista

def test_the_blocklist_is_local_and_not_empty():
    """Nessuna dipendenza da un servizio: in rete chiusa non ce ne sono.

    Un file assente farebbe sollevare, non passare in silenzio: un elenco vuoto è un
    controllo disattivato di cui nessuno si accorge.
    """
    voci = blocklist()
    assert len(voci) > 200
    assert P.BLOCKLIST_PATH.is_file()


@pytest.mark.parametrize("pw", [
    "passwordpassword",             # la corta scritta due volte
    "qwertyuiopasdfgh",             # camminata sulla tastiera
    "123456789012345",              # sequenza
    "administrator123",
    "trustservermanager",           # il nome dell'applicazione
    "trusttechnologies",            # il nome dell'azienda
    "saleserverpomezia",            # il contesto
    "changethispassword",
    "passwordprovvisoria",
    "correcthorsebatterystaple",    # la passphrase più famosa del mondo
])
def test_a_blocklisted_password_is_rejected(pw):
    """Voci lunghe: sono quelle che contano.

    Con un minimo di 15 caratteri, `password` e `123456` cadono già per lunghezza.
    Il lavoro della lista è l'altra cosa — che cosa scrive una persona a cui è stato
    appena chiesto di digitarne quindici.
    """
    assert len(pw) >= MIN_LENGTH, "questa voce cadrebbe già per lunghezza"
    problem = policy_problem(pw)
    assert problem is not None and problem[0] == PASSWORD_BLOCKLISTED


def test_the_blocklist_comparison_ignores_case_and_unicode_form():
    """`Password` e `password` sono la stessa scelta debole.

    Il casefold vive SOLO nel confronto con la lista: per Argon2 restano due
    password diverse, e devono restarlo.
    """
    for variante in ["PASSWORDPASSWORD", "PasswordPassword", "pAsSwOrDpAsSwOrD"]:
        assert is_blocklisted(variante)
    # E la forma Unicode non permette di aggirare la lista.
    assert is_blocklisted(normalise("trusttechnologies"))


@pytest.mark.parametrize("pw", [
    "il mio cane si chiama password e dorme",   # contiene "password"
    "una casa in campagna con il gatto",        # contiene "casa"
    "vado a roma in treno domani mattina",      # contiene "roma"
    "la prova del nove e sempre la prova",      # contiene "prova"
    "ammiro molto l'amministratore di sistema",  # contiene "amministratore"
])
def test_a_strong_passphrase_containing_a_listed_word_is_accepted(pw):
    """Il confronto è di UGUAGLIANZA, non di inclusione.

    Cercare le voci come sottostringa sembra più severo ed è controproducente:
    colpirebbe esattamente le passphrase lunghe che la politica incoraggia,
    lasciando passare `Estate2026!` perché non contiene nessuna voce. Chi sceglie
    una voce della lista la sceglie intera.
    """
    assert policy_problem(pw) is None


def test_a_password_equal_to_the_username_is_rejected():
    """Prevedibile come una voce di lista, ma non elencabile in un file.

    Dipende dall'utenza, quindi la regola è di uguaglianza con lo username, non una
    riga di `password-blocklist.txt`.
    """
    problem = policy_problem("mario.rossi.sistemi", username="mario.rossi.sistemi")
    assert problem is not None and problem[0] == PASSWORD_BLOCKLISTED
    # Anche con maiuscole diverse: `username` è citext nel database.
    assert is_blocklisted("Mario.Rossi.Sistemi", username="mario.rossi.sistemi")
    # Ma una password che CONTIENE lo username va bene.
    assert policy_problem("mario.rossi.sistemi e il suo gatto",
                          username="mario.rossi.sistemi") is None


def test_the_blocklist_error_does_not_say_where_the_password_was_found():
    """«Compare in una raccolta di credenziali diffuse» direbbe a chi prova che quel
    valore è vero altrove, e la stessa persona riusa le password."""
    message = policy_problem("passwordpassword")[1]
    for parola in ["breach", "violazion", "raccolta", "elenco", "lista", "database",
                   "compromess", "diffus", "trovata"]:
        assert parola not in message.lower(), message
    assert "comune" in message or "prevedibile" in message


def test_the_blocklist_is_not_consulted_by_verification():
    """La lista si applica quando una password si IMPOSTA, non quando si verifica.

    Chi ha già una password che oggi finisce in lista non deve scoprirlo durante un
    accesso: da lì non c'è via d'uscita. Aggiungere una voce non invalida nessun
    hash esistente, ed è ciò che rende manutenibile il file.
    """
    debole = "passwordpassword"
    h = hash_password(debole)              # come se fosse stata impostata ieri
    assert verify_password(h, debole) is True
    with pytest.raises(PasswordRejected):
        check_policy(debole)               # ma oggi non si può più impostare


def test_the_blocklist_file_has_no_duplicate_or_stray_entries():
    """Igiene del file: niente spazi ai bordi, niente righe già normalizzate male.

    Una voce con uno spazio finale non corrisponderebbe mai a niente, e sarebbe una
    riga che sembra proteggere e non protegge.
    """
    righe = [r for r in P.BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
             if r.strip() and not r.strip().startswith("#")]
    assert all(r == r.strip() for r in righe), "voce con spazi ai bordi"
    normalizzate = [normalise(r).casefold() for r in righe]
    duplicati = {v for v in normalizzate if normalizzate.count(v) > 1}
    assert not duplicati, f"voci duplicate: {sorted(duplicati)[:5]}"


def test_the_test_fixtures_used_across_the_suite_are_not_blocklisted():
    """Rete di sicurezza per il resto della suite.

    Le password delle fixture PG e dei runner delle prove UI devono passare la
    politica, altrimenti decine di test fallirebbero per un motivo che non c'entra
    con quello che provano — e la diagnosi sarebbe lunga.
    """
    for pw in ["password-lunga-1", "password-lunga-2", "password-lunga-3",
               "password-nuova-lunga", "admin-iniziale-1", "admin-definitiva-1"]:
        assert policy_problem(pw) is None, pw


# ==================================================== password provvisorie

def test_a_temporary_password_has_at_least_128_bits_of_entropy():
    """24 byte da CSPRNG = 192 bit, oltre il minimo richiesto di 128.

    Si controlla il NUMERO DI BYTE della sorgente, non la lunghezza della stringa:
    32 caratteri di un alfabeto a 64 simboli non sono 192 bit di entropia se la
    sorgente ne ha meno, e contare i caratteri misurerebbe la codifica invece del
    segreto.
    """
    assert P.TEMP_PASSWORD_BYTES * 8 >= 128
    assert P.TEMP_PASSWORD_BYTES * 8 == 192


def test_a_temporary_password_satisfies_the_normal_policy():
    """Per costruzione, non per coincidenza: il generatore la valida prima di darla.

    Se un domani `MIN_LENGTH` superasse la lunghezza generata, il generatore
    solleverebbe subito invece di creare utenze con una provvisoria che l'utente non
    riesce a ridigitare.
    """
    for _ in range(20):
        temp = generate_temporary_password()
        assert policy_problem(temp) is None
        assert len(temp) >= MIN_LENGTH


def test_temporary_passwords_are_url_safe_and_easy_to_copy():
    """Alfabeto URL-safe: niente caratteri che si perdono incollando, niente
    ambiguità fra `l` maiuscola e `1` in un messaggio scritto a mano."""
    temp = generate_temporary_password()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", temp), temp


def test_two_temporary_passwords_are_never_the_same():
    """CSPRNG, non un contatore né `random`.

    `random` è deterministico e prevedibile da chi ne osservi l'uscita: non va mai
    vicino a una credenziale. Su mille estrazioni non ci sono collisioni.
    """
    assert len({generate_temporary_password() for _ in range(1000)}) == 1000


def test_two_users_forced_to_the_same_temporary_value_still_get_different_hashes():
    """Il caso richiesto esplicitamente: stesso valore generato, hash diversi.

    Serve a escludere che il sale dipenda dal valore della password — un errore che
    non si vede se ogni prova usa una password diversa.
    """
    temp = generate_temporary_password()
    hash_a, hash_b = hash_password(temp), hash_password(temp)
    assert hash_a != hash_b
    assert hash_a.split("$")[4] != hash_b.split("$")[4]     # sali diversi
    assert verify_password(hash_a, temp) and verify_password(hash_b, temp)


# ============================================== aggiornamento dei parametri

def test_a_weaker_historical_hash_is_flagged_for_rehash():
    """Un hash con parametri di ieri si riconosce, e si verifica ancora.

    Sono le due metà che rendono possibile l'aggiornamento senza disturbare
    nessuno: se non verificasse, l'utente sarebbe bloccato fuori; se non fosse
    segnalato, resterebbe debole per sempre.
    """
    vecchio = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1,
                             type=Type.ID).hash(BUONA)
    assert verify_password(vecchio, BUONA) is True
    assert needs_rehash(vecchio) is True

    nuovo = hash_password(BUONA)
    assert needs_rehash(nuovo) is False


def test_needs_rehash_reads_the_parameters_inside_the_hash():
    """Lo decide la libreria, non una nostra ispezione della stringa.

    È l'unica fonte che resta corretta se cambiassimo `ARGON2_*`: un confronto
    scritto a mano su `m=65536` continuerebbe a dire «va bene» dopo un cambio di
    configurazione.
    """
    # Solo la memoria è diversa: un controllo grossolano sul prefisso non lo vedrebbe.
    quasi = PasswordHasher(time_cost=ARGON2_TIME_COST, memory_cost=32768,
                          parallelism=ARGON2_PARALLELISM, type=Type.ID).hash(BUONA)
    assert quasi.startswith(ARGON2_PREFIX)
    assert needs_rehash(quasi) is True


def test_a_broken_hash_is_not_reported_as_needing_rehash():
    """Un hash illeggibile è rotto, non «da riscrivere»: riscriverlo richiederebbe la
    password in chiaro, che a quel punto non si è potuta verificare."""
    for rotto in ["", "non-un-hash", "$argon2id$rotto"]:
        assert needs_rehash(rotto) is False


# ====================================================== forma degli errori

def test_check_policy_returns_the_normalised_password():
    """Restituire la forma normalizzata, invece di None, rende difficile lo sbaglio:
    chi valida ha già in mano l'unico valore da usare dopo."""
    decomposta = "citta" + COMBINING_GRAVE + " di Pomezia ok"
    assert check_policy(decomposta) == normalise(decomposta)
    assert check_policy(decomposta) != decomposta


def test_password_rejected_is_not_an_auth_error():
    """403 e 422 sono risposte a domande diverse.

    `AuthError` significa «non ti è permesso»; qui il problema è il VALORE inviato.
    Se `PasswordRejected` ne discendesse, la mappa in `app.api.errors` la
    tradurrebbe in 403 e il client non potrebbe distinguere «rifà il login» da
    «scegli un'altra password».
    """
    from app.auth.service import AuthError
    assert not issubclass(PasswordRejected, AuthError)


def test_every_stable_code_is_snake_case_and_distinct():
    """I codici sono contratto: minuscoli, snake_case, tutti diversi (§8.21)."""
    codici = [PASSWORD_TOO_SHORT, PASSWORD_TOO_LONG, PASSWORD_BLOCKLISTED,
              PASSWORD_NOT_ENCODABLE, P.PASSWORD_UNCHANGED]
    assert len(set(codici)) == len(codici)
    for c in codici:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", c), c


def test_no_pepper_is_used_and_none_is_hidden_in_the_module():
    """Nessun pepper: la scelta è documentata (§8.43), e questo la fissa.

    Un pepper è un segreto operativo, e un segreto operativo senza una storia di
    rotazione è un debito: il giorno in cui va cambiato, ogni hash esistente
    diventa inservibile. Se un domani se ne introducesse uno, va introdotto con la
    sua procedura — e questo test va aggiornato di proposito, non per caso.
    """
    sorgente = (P.__file__.replace(".pyc", ".py"))
    from pathlib import Path
    testo = Path(sorgente).read_text(encoding="utf-8")
    codice = "\n".join(r for r in testo.splitlines()
                       if not r.strip().startswith("#"))
    assert "pepper" not in codice.lower()
    # E nessun segreto di applicazione che entri nel calcolo dell'hash.
    assert "secret_key" not in codice.lower()
    assert "getenv" not in codice and "environ" not in codice
