"""Politica delle password e configurazione di Argon2id. Logica PURA.

Non conosce HTTP né il database: prende una stringa e risponde se è accettabile,
la normalizza, la trasforma in hash o la confronta con un hash memorizzato.

È il posto UNICO in cui si decide che cosa vale come password. Le strade che ne
stabiliscono una sono tre — il bootstrap del primo amministratore, il cambio
fatto dall'utente, la reimpostazione fatta da un amministratore — e prima erano
tre regole diverse: il cambio chiedeva dieci caratteri, le altre due nessuno.
Una politica che vive in tre posti è una politica che vale nel posto più debole.

Tre proprietà di questo modulo sono deliberate e vanno lette prima di modificarlo.

**La normalizzazione non è facoltativa e non è del chiamante.** `hash_password` e
`verify_password` normalizzano DENTRO. Se normalizzare fosse compito di chi
chiama, basterebbe un punto che se ne dimentica per rendere una password
impossibile da riusare: l'utente la scrive con la stessa tastiera, il sistema
operativo consegna la forma decomposta invece di quella composta, e l'accesso
viene negato senza che niente risulti sbagliato da nessuna parte. Il difetto
sarebbe intermittente per piattaforma e invisibile nei test ASCII.

**I parametri di Argon2 sono fissati qui, non lasciati alla libreria.** Coincidono
con i default di `argon2-cffi` 25.1.0, ma coinciderci per scelta e coinciderci per
caso sono cose diverse: i default di una libreria cambiano quando cambiano le
raccomandazioni, e un aggiornamento di dipendenza non deve poter spostare la
sicurezza dell'applicazione — né in basso, né in alto a sorpresa, perché anche un
irrobustimento non voluto trasformerebbe ogni accesso in una riscrittura di hash
non prevista dall'operatore.

**Niente regole di composizione e niente scadenza periodica.** Non si chiedono
maiuscole, cifre o simboli, e non si impone un cambio ogni novanta giorni. Sono
requisiti che spostano il costo sull'utente e la sicurezza da nessuna parte:
producono `Estate2026!` e `Estate2026!!`. Il lavoro lo fanno la lunghezza minima
alta, la lista dei valori noti e Argon2id.

Misurato, non supposto (vedi §8.43 e i test):

  - NUL dentro una password è innocuo per Argon2 e **non** tronca: due password
    che differiscono solo dopo il NUL non si verificano a vicenda. Non serve
    nessun caso speciale, e non si riusa qui la regola di rappresentabilità
    dell'istantanea, che risponde a un'altra domanda (vedi `unusable_reason`).

  - Un surrogato spaiato, invece, non è codificabile in UTF-8 e fa sollevare
    Argon2. Si rifiuta in fase di IMPOSTAZIONE, con un codice stabile.

Riferimento: BACKEND-PLAN.md §8.43.
"""
from __future__ import annotations

import secrets
import unicodedata
from functools import lru_cache
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

# ------------------------------------------------------------------- codici
#
# Stabili e sul filo: il client li usa per scegliere il messaggio, e cambiarne uno
# è un cambio di contratto (§8.21).

PASSWORD_TOO_SHORT = "password_too_short"
PASSWORD_TOO_LONG = "password_too_long"
PASSWORD_BLOCKLISTED = "password_blocklisted"
PASSWORD_NOT_ENCODABLE = "password_not_encodable"
PASSWORD_UNCHANGED = "password_unchanged"

# ----------------------------------------------------------------- Argon2id
#
# Algoritmo: Argon2**id** (non i, non d). Versione dell'algoritmo: 0x13 (v=19),
# quella che `argon2-cffi` 25.1.0 produce e l'unica presente negli hash generati.
#
# I numeri: 64 MiB di memoria, 3 iterazioni, 4 corsie. Sono la seconda
# configurazione raccomandata da RFC 9106 (quella «low memory») e superano il
# minimo richiesto di 19 MiB / t=2 / p=1. Latenza misurata di una verifica in
# container: ~72 ms, che per un accesso interattivo è invisibile e per chi provasse
# a indovinare in massa è proibitivo. Non si scende al minimo solo per stare al
# minimo: la configurazione più forte è già in uso e declassarla riscriverebbe in
# peggio ogni hash esistente al primo accesso.

ARGON2_TYPE = Type.ID
ARGON2_VERSION = 19
ARGON2_MEMORY_COST = 65536      # KiB → 64 MiB
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32            # byte di output
ARGON2_SALT_LEN = 16            # byte di sale, generati dalla libreria

#: Soglie MINIME accettabili, contro cui i test confrontano la configurazione
#: sopra. Servono a far fallire un abbassamento futuro: senza, cambiare 65536 in
#: 4096 passerebbe tutti i test, perché ogni test si limiterebbe a rileggere la
#: costante appena modificata.
ARGON2_MIN_MEMORY_COST = 19456
ARGON2_MIN_TIME_COST = 2
ARGON2_MIN_PARALLELISM = 1

#: Prefisso dell'hash codificato. È anche ciò che i test usano per verificare che
#: nel database ci sia Argon2id e non altro.
ARGON2_PREFIX = "$argon2id$"

#: Unico esemplare. Costruirlo a ogni chiamata non sarebbe sbagliato, ma renderebbe
#: più facile costruirne uno CON PARAMETRI DIVERSI in un punto lontano.
_hasher = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LEN,
    salt_len=ARGON2_SALT_LEN,
    type=ARGON2_TYPE,
)

# ---------------------------------------------------------------- lunghezze
#
# 15 code point come minimo: è la lunghezza che rende inutile un attacco a
# dizionario senza chiedere all'utente di ricordare caratteri strani. Le
# passphrase con spazi sono il caso previsto, non tollerato.
#
# 128 come massimo, e si RIFIUTA invece di troncare. Troncare farebbe una cosa che
# nessuno si aspetta: renderebbe equivalenti due password diverse, e l'utente che
# ne digita 200 crederebbe di averne una da 200.

MIN_LENGTH = 15
MAX_LENGTH = 128

#: Byte casuali di una password provvisoria: 24 byte = **192 bit** di entropia,
#: oltre il minimo richiesto di 128. `token_urlsafe` li rappresenta in 32 caratteri
#: URL-safe, che si copiano e si incollano senza ambiguità e superano `MIN_LENGTH`
#: senza casi speciali.
TEMP_PASSWORD_BYTES = 24

#: File della lista locale. Accanto al modulo di proposito: in rete chiusa non
#: esiste un servizio da interrogare, e un percorso configurabile sarebbe un
#: percorso che in produzione può puntare a un file assente — cioè un controllo
#: che si disattiva in silenzio.
BLOCKLIST_PATH = Path(__file__).with_name("password-blocklist.txt")


class PasswordRejected(Exception):
    """La password non è accettabile. `code` è stabile, il messaggio è per gli umani.

    Non contiene, e non deve contenere, la password rifiutata: questo errore
    attraversa i log e l'audit, e il valore rifiutato è comunque un segreto —
    spesso è la password *quasi* giusta di quella persona.

    Volutamente NON discende da `AuthError`: quella si traduce in 403, e qui il
    problema non è un permesso ma il contenuto inviato. La mappa su 422 sta in
    `app.api.errors`.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------- normalizzazione

def normalise(plain: str) -> str:
    """Forma canonica di una password: **NFC**, e nient'altro.

    NFC e non NFD perché è la forma che le tastiere e i sistemi operativi
    producono più spesso, quindi è quella che lascia invariata la maggior parte
    degli input reali.

    Non si tolgono gli spazi ai bordi. Non è una dimenticanza: uno spazio iniziale
    o finale può far parte della password, e rimuoverlo significherebbe accettare
    all'accesso una password diversa da quella impostata — oppure, peggio,
    impostarne una che l'utente non ha scelto. Chi incolla per sbaglio uno spazio
    riceve un errore di credenziali, che è recuperabile; una ripulitura silenziosa
    non lo è.

    Non si tocca nient'altro: niente casefold, niente rimozione di caratteri di
    controllo, niente sostituzioni. Una password è una sequenza di code point
    scelta dall'utente, non un campo da ripulire.
    """
    return unicodedata.normalize("NFC", plain)


def unusable_reason(plain: str) -> str | None:
    """Motivo per cui la password non è UTILIZZABILE, o None.

    Una sola causa, misurata: un surrogato spaiato non è codificabile in UTF-8, e
    Argon2 codifica in UTF-8 prima di calcolare. Senza questo controllo il difetto
    si manifesterebbe due volte e male:

      1. all'impostazione, `hash_password` farebbe sollevare `UnicodeEncodeError`,
         che nessuno mappa e che diventa un 503 «servizio non disponibile» — un
         errore del server per un dato del client;

      2. se un hash fosse comunque nato, l'utenza sarebbe **inaccessibile per
         sempre**: `verify_password` intercetta `ValueError`, di cui
         `UnicodeEncodeError` è sottoclasse, quindi ogni accesso successivo
         risponderebbe «credenziali errate» senza che niente sia errato.

    NON si riusa qui `app.inventory.json_strings.is_representable_text`, che è la
    regola di rappresentabilità dell'istantanea. Le due domande sembrano la stessa
    e non lo sono: quella chiede se PostgreSQL conserva una stringa in `text`/
    `jsonb`, e comprende il NUL; una password non finisce in nessuna colonna —
    ci finisce il suo hash, che è ASCII — e per Argon2 il NUL è **misuratamente**
    innocuo, non tronca e non confonde due password diverse. Riusare la regola
    dell'istantanea rifiuterebbe password legittime per un motivo che qui non
    esiste, il che è il modo in cui una regola condivisa a torto fa danni.
    """
    try:
        plain.encode("utf-8")
    except UnicodeEncodeError:
        return ("contiene un surrogato spaiato: la password non è codificabile "
                "in UTF-8 e non potrebbe più essere verificata")
    return None


# ------------------------------------------------------------ lista locale

@lru_cache(maxsize=1)
def blocklist() -> frozenset[str]:
    """Le voci della lista locale, normalizzate una volta sola.

    Il confronto avviene su NFC + `casefold()`: `casefold` solo qui, e solo per il
    confronto, perché `Password` e `password` sono la stessa scelta debole mentre
    restano due password diverse per Argon2.

    Il file manca solo se il pacchetto è stato assemblato male; in quel caso si
    solleva. Un elenco vuoto sarebbe un controllo disattivato in silenzio, cioè la
    forma di guasto peggiore fra le due.
    """
    text = BLOCKLIST_PATH.read_text(encoding="utf-8")
    voci = set()
    for riga in text.splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#"):
            continue
        voci.add(normalise(riga).casefold())
    if not voci:                                    # pragma: no cover
        raise RuntimeError(f"lista delle password vietate vuota: {BLOCKLIST_PATH}")
    return frozenset(voci)


def is_blocklisted(plain: str, *, username: str | None = None) -> bool:
    """Vero se la password è nella lista locale, o se coincide con l'utenza.

    Il confronto è di UGUAGLIANZA sull'intera password, non di inclusione. Cercare
    le voci come sottostringa sembra più severo ed è invece semplicemente
    sbagliato: rifiuterebbe `il cane dorme sul tappeto` perché contiene `cane`,
    cioè punirebbe esattamente le passphrase lunghe che si vogliono incoraggiare.
    Chi sceglie una voce della lista la sceglie intera.

    Coincidere con il proprio username è la stessa categoria di scelta prevedibile,
    e non si può elencare in un file perché dipende dall'utenza.
    """
    candidate = normalise(plain).casefold()
    if username and candidate == normalise(username).casefold():
        return True
    return candidate in blocklist()


# ----------------------------------------------------------------- politica

def policy_problem(plain: str, *,
                   username: str | None = None) -> tuple[str, str] | None:
    """`(codice, messaggio)` del primo problema, oppure None. Non solleva.

    L'ordine dei controlli non è casuale: prima ciò che rende la password
    inutilizzabile, poi la lunghezza, infine la lista. Così chi invia una password
    da 200 caratteri riceve «troppo lunga» e non un'informazione sulla lista.

    La lunghezza si misura **sulla forma normalizzata**, in code point. NFC può
    accorciare (`a` + accento combinante → `à`: misurato, 27 → 26 code point), e
    misurare prima renderebbe accettabile in impostazione una password che
    all'accesso risulterebbe di lunghezza diversa. Si misura una volta sola, dopo
    la normalizzazione, in tutte le strade.
    """
    reason = unusable_reason(plain)
    if reason is not None:
        return PASSWORD_NOT_ENCODABLE, f"password non utilizzabile: {reason}"

    candidate = normalise(plain)
    if len(candidate) < MIN_LENGTH:
        return (PASSWORD_TOO_SHORT,
                f"la password deve avere almeno {MIN_LENGTH} caratteri; "
                "una frase con spazi va benissimo")
    if len(candidate) > MAX_LENGTH:
        # Si dice quanto è il limite, non quanto era la password: la lunghezza di
        # un segreto è un'informazione sul segreto.
        return (PASSWORD_TOO_LONG,
                f"la password non può superare i {MAX_LENGTH} caratteri")
    if is_blocklisted(candidate, username=username):
        # Il messaggio non dice DOVE è stata trovata. «Compare in una raccolta di
        # credenziali diffuse» direbbe a chi prova che quel valore è vero da
        # qualche altra parte, e la stessa persona riusa le password altrove.
        return (PASSWORD_BLOCKLISTED,
                "questa password è troppo comune o prevedibile: scegliere un'altra")
    return None


def check_policy(plain: str, *, username: str | None = None) -> str:
    """Applica la politica e restituisce la password NORMALIZZATA.

    Restituire la forma normalizzata, invece di None, è il modo di rendere
    difficile lo sbaglio: chi valida ha già in mano l'unico valore da usare dopo.
    """
    problem = policy_problem(plain, username=username)
    if problem is not None:
        raise PasswordRejected(*problem)
    return normalise(plain)


# ------------------------------------------------------------------- hashing

def hash_password(plain: str) -> str:
    """Hash Argon2id di una password, con sale casuale generato dalla libreria.

    Il sale NON si passa e non si sceglie: `PasswordHasher.hash` ne genera uno
    nuovo di `ARGON2_SALT_LEN` byte da un CSPRNG per ogni chiamata, e lo scrive
    nell'hash codificato. Non è un segreto e non ha bisogno di una colonna sua.
    Un sale derivato dallo username o dall'id, o uno globale, renderebbe
    confrontabili fra loro gli hash di utenti diversi — che è precisamente ciò che
    il sale serve a impedire.
    """
    return _hasher.hash(normalise(plain))


def verify_password(stored_hash: str, plain: str) -> bool:
    """Vero se la password corrisponde all'hash. Non solleva mai.

    Normalizza con la stessa funzione di `hash_password`: è ciò che rende una
    password impostata su una piattaforma verificabile su un'altra.

    Le eccezioni intercettate sono tre famiglie, e ognuna per un motivo:

      - `VerificationError` copre sia la non corrispondenza (`VerifyMismatchError`,
        che ne discende) sia un hash che la libreria non riesce a DECODIFICARE. La
        seconda non era intercettata, e il difetto era vero: un `password_hash`
        illeggibile — corrotto, troncato da una migrazione, di un altro algoritmo —
        faceva sollevare, e la rotta di accesso rispondeva 503 invece di negare.
        Peggio: l'eccezione arrivava PRIMA della registrazione del tentativo,
        quindi quei tentativi non venivano contati dal limitatore.

      - `InvalidHashError` (che è una `ValueError`) per un hash sintatticamente
        inaccettabile.

      - `ValueError` comprende `UnicodeEncodeError`: una password non codificabile
        inviata all'ACCESSO è un tentativo sbagliato, non un guasto del server. In
        impostazione la stessa stringa viene invece rifiutata con un codice, perché
        lì produrrebbe un'utenza inaccessibile per sempre.

    In tutti i casi la risposta è «no». Un hash che non si può leggere non
    autentica nessuno, ed è l'unica risposta conservativa possibile.
    """
    try:
        return _hasher.verify(stored_hash, normalise(plain))
    except (VerificationError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Vero se l'hash è stato calcolato con parametri diversi da quelli attuali.

    Lo decide la libreria leggendo i parametri DENTRO l'hash codificato, non una
    nostra ispezione della stringa: è l'unica fonte che resta corretta se un
    giorno cambiassimo `ARGON2_*`.

    Un hash illeggibile non è «da riscrivere», è rotto: riscriverlo richiederebbe
    la password in chiaro, che a quel punto non si è potuta verificare. Si
    risponde False e la verifica avrà già negato l'accesso.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):          # pragma: no cover
        return False


def generate_temporary_password() -> str:
    """Password provvisoria da CSPRNG: 192 bit, 32 caratteri URL-safe.

    `secrets`, non `random`: il secondo è un generatore deterministico e prevedibile
    da chi ne osservi l'uscita, e non va mai vicino a una credenziale.

    Il risultato passa dalla politica normale prima di essere restituito. Non è
    teatro: lega per costruzione le due cose che devono restare d'accordo, cioè
    quanto è lunga una provvisoria e quanto deve essere lunga una password. Se un
    domani `MIN_LENGTH` superasse la lunghezza generata, questo solleverebbe subito
    invece di creare utenze con una provvisoria che l'utente non può ridigitare.
    """
    temp = secrets.token_urlsafe(TEMP_PASSWORD_BYTES)
    check_policy(temp)
    return temp
