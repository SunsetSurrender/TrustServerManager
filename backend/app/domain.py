"""Il modello semantico UNICO dell'inventario. Fase 2G (§8.50).

Fino alla fase 2E la semantica del prodotto era **il comportamento misurato del
prototipo**, riprodotto fedelmente perché cambiarlo di nascosto durante una
migrazione tecnica sarebbe stato peggio che conservarne i difetti. Quella scelta ha
fatto il suo lavoro e adesso scade: questo modulo è la sede della semantica
INTENZIONALE, e tre implementazioni la condividono invece di dedurla ognuna per sé.

    handoff/domain.js          la stessa semantica in JavaScript, per il frontend
    fixtures/domain/*.json     il CONTRATTO, in dati, indipendente dal linguaggio
    questo modulo              Python, e per estensione SQL

⚠ Chi decide non è il codice, sono le fixture. `fixtures/domain/*.json` porta ingressi
e attese scritte a mano da una decisione di prodotto; la suite Python e
`tools/domain-contract-tests.mjs` le eseguono contro le due implementazioni. Se una
delle due sbaglia, è la fixture che diventa rossa — non l'altra implementazione, che
potrebbe sbagliare allo stesso modo. È l'opposto di `make-query-fixtures.mjs`, che
CALCOLAVA le attese copiando il frontend: là si dimostrava la parità con ciò che
girava, qui si dimostra la conformità a ciò che si è deciso.

Che cosa vive qui, e perché tutto insieme
-----------------------------------------
Non è un modulo di utilità: ogni funzione qui dentro era duplicata, e ogni duplicato
divergeva. Il conto della fase 2E (§8.48): tre definizioni di «U occupate», due
interpreti di data, due elenchi di campi cercabili, due idee di «dismesso».

    stato e presenza     ciclo di vita operativo e presenza FISICA, separati (§1)
    capacità            slot U distinti occupati, una definizione sola (§2)
    percentuale         arrotondamento HALF-UP deterministico (§3)
    fila                identità del gruppo, distinta dall'etichetta mostrata (§4)
    indirizzi           una grammatica sola per IP esatti, CIDR, intervalli, jolly (§5)
    scadenze            un interprete di date solo, `YYYY-MM-DD` (§6)
    idoneità            chi genera un avviso e chi no (§7)
    etichette           nome → codice → «(senza nome)», mai `None` (§9)

Purezza
-------
Nessun database, nessun orologio, nessuna configurazione. «Oggi» arriva sempre da
fuori. È la stessa disciplina di `notifications/expiry.py`, e per la stessa ragione:
una funzione che legge l'orologio non si può provare sul cambio dell'ora legale.

Riferimento: BACKEND-PLAN.md §8.50.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

# ==================================================================
# 1. vocabolari, e i valori predefiniti
# ==================================================================

#: Tipi di dispositivo. Vocabolario dell'interfaccia (`Component.types()`), non un
#: vincolo: un valore fuori elenco si conserva e si segnala (§8.42).
DEVICE_TYPES: tuple[str, ...] = ("server", "rete", "storage", "firewall",
                                 "alimentazione", "altro")

#: **Ciclo di vita OPERATIVO.** Dice se un apparato è in servizio, non dove sta.
DEVICE_STATES: tuple[str, ...] = ("attivo", "manutenzione", "dismissione", "dismesso")

#: **Presenza FISICA.** Dice se l'apparato occupa ancora il suo slot nel rack.
#:
#: ⚠ È la separazione che la fase 2G introduce (§1 del requisito), ed esiste perché
#: le due domande hanno risposte indipendenti: «dismesso» significa fuori servizio, e
#: un apparato fuori servizio può stare fisicamente nel rack per mesi — occupando
#: unità che nessuno può assegnare a qualcos'altro. Dedurre la presenza dallo stato
#: operativo era la radice di due difetti opposti: la vista Capacità contava come
#: occupato ciò che era stato portato via, e chi guardava «dismesso» credeva di
#: vedere spazio libero che non c'era.
DEVICE_PRESENCES: tuple[str, ...] = ("presente", "rimosso")

DEFAULT_TYPE = "altro"
DEFAULT_STATO = "attivo"
DEFAULT_PRESENZA = "presente"
DEFAULT_H = 1

#: Il valore che NON occupa spazio. Scritto così, e non come elenco di quelli che
#: occupano: un domani `presenza` potrebbe avere un terzo valore (per esempio «in
#: transito»), e un elenco positivo lo escluderebbe dal conteggio in silenzio.
PRESENZA_ABSENT = "rimosso"

#: Stati che NON generano più promemoria di rinnovo (§7 del requisito). Un apparato
#: completamente dismesso resta cercabile e visibile in Scadenze — il dato non si
#: perde — ma nessuno deve rinnovare la garanzia di una macchina che non tornerà in
#: servizio. `attivo`, `manutenzione` e `dismissione` restano idonei: «in
#: dismissione» significa che la decisione non è ancora conclusa.
NOTIFY_INELIGIBLE_STATES: tuple[str, ...] = ("dismesso",)

#: I due campi di scadenza del dispositivo. Elenco CHIUSO: un campo nuovo va aggiunto
#: qui consapevolmente, non scoperto da un'euristica sui nomi dei campi.
EXPIRY_KINDS: tuple[str, ...] = ("garanzia", "supporto")

#: Etichette dei due tipi di scadenza. Stanno nel dominio e non nella composizione del
#: messaggio: la lingua dell'avviso è una proprietà del prodotto, e il frontend mostra
#: le stesse due parole nella vista Scadenze.
EXPIRY_LABELS: dict[str, str] = {"garanzia": "Garanzia",
                                 "supporto": "Contratto di supporto"}

#: Etichetta finale quando non c'è né un nome né un codice (§9). Mai `None`, mai
#: `undefined`, mai `null`: sono valori dell'implementazione, e un utente che li legge
#: sta guardando un difetto, non un dato.
NO_NAME = "(senza nome)"

#: Come si MOSTRA una fila non impostata. È un'etichetta, non un'identità: vedi
#: `row_group`.
ROW_UNSET_LABEL = "—"

#: Altezza di un rack: limite DICHIARATO del prodotto (§8.48 voce 16).
#:
#: Il numero è quello dell'`integer` della proiezione, e non è un dettaglio
#: d'implementazione che sia finito qui per sbaglio: un `u` intero fuori da questo
#: intervallo va in `extra` e la colonna resta NULL, quindi la capacità calcolata
#: DALLO SQL vede un rack senza altezza mentre questo modulo — che legge il
#: documento — calcola sul valore vero. Era l'unica divergenza SQL/modello che la
#: 2G non poteva chiudere spostando del codice: si chiude rifiutando il valore in
#: ingresso, che è dove la si può ancora chiamare un dato sbagliato.
#:
#: `1` e non `0` come minimo: un rack alto zero non è un rack, e `slot_span`
#: già non gli assegna nessuno slot. Il documento non deve poter contenere una
#: forma che nessuna delle due implementazioni sa disegnare.
RACK_U_MIN = 1
RACK_U_MAX = 2147483647


def _falsy_string(value: Any, default: str) -> str:
    """`(value || default)` con la falsità di JavaScript, per i campi a vocabolario.

    `''` è falso in JavaScript, quindi `stato: ""` significa «attivo» e non «vuoto».
    Riprodurre quella regola qui non è un omaggio al prototipo: è la SOLA regola, e
    vale in tutte e tre le implementazioni perché sta scritta una volta.

    Un valore non stringa (un `42` finito nel campo da un foglio di calcolo) non è un
    valore del vocabolario: si restituisce com'era, in forma di testo, così
    `validate_model` lo può segnalare invece di vederlo sparire dentro il default.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value if value != "" else default
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def stato_of(device: Any) -> str:
    """Stato operativo di un dispositivo. `attivo` se assente o vuoto."""
    return _falsy_string(_get(device, "stato"), DEFAULT_STATO)


def presenza_of(device: Any) -> str:
    """Presenza fisica di un dispositivo. `presente` se assente o vuota.

    ⚠ **Non si deduce dallo stato.** Un `dismesso` senza `presenza` è
    `dismesso + presente`: l'inventario di prima della fase 2G non registra le
    rimozioni, quindi l'unica cosa che si sa di quelle macchine è che nessuno ha detto
    che sono state portate via. Dedurre `rimosso` da `dismesso` libererebbe d'un colpo
    unità rack che in sala sono occupate — e il primo a scoprirlo sarebbe chi arriva
    con un apparato nuovo e non trova posto.
    """
    return _falsy_string(_get(device, "presenza"), DEFAULT_PRESENZA)


def tipo_of(device: Any) -> str:
    """Tipo di un dispositivo. `altro` se assente o vuoto."""
    return _falsy_string(_get(device, "type"), DEFAULT_TYPE)


def occupies_space(device: Any) -> bool:
    """Questo dispositivo occupa unità fisiche del rack? (§2)

    Solo la presenza decide. Lo stato operativo non c'entra: un apparato in
    manutenzione, in dismissione o dismesso che sta ancora nel rack occupa lo spazio
    di un apparato in manutenzione, in dismissione o dismesso che sta ancora nel rack.
    """
    return presenza_of(device) != PRESENZA_ABSENT


def notifies(device: Any) -> bool:
    """Questo dispositivo può generare NUOVI avvisi di scadenza? (§7)

    Solo lo stato operativo decide. La presenza fisica non c'entra: un apparato
    portato in un altro sito e non ancora registrato altrove ha la garanzia che scade
    comunque, e chi la rinnova ha bisogno di saperlo.
    """
    return stato_of(device) not in NOTIFY_INELIGIBLE_STATES


# ==================================================================
# 2. capacità: gli slot U DISTINTI occupati
# ==================================================================

def _as_int(value: Any) -> int | None:
    """Interi soltanto. `True` non è 1, `"3"` non è 3, `3.0` non è 3.

    Nel documento `u` e `h` sono interi (`_is_int` della mappa relazionale li mette in
    colonna solo se lo sono); un valore di altra forma viaggia in `extra` e la colonna
    resta NULL. Accettare qui `"3"` significherebbe che il conteggio in Python vede
    uno slot che lo SQL non vede, cioè la divergenza che questa fase esiste per
    chiudere.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def rack_height_supported(value: Any) -> bool:
    """`u` di un rack sta dentro il limite dichiarato?

    Tre risposte «sì» che meritano di essere lette, perché sono la ragione per cui
    questa non è una convalida di tipo:

      - **assente / `None`**: sì. Il default canonico mette 45, e rifiutare qui
        vorrebbe dire rifiutare ogni rack creato senza toccare il campo;
      - **non intero** (`"45"`, `4.5`, `True`): sì, e non è indulgenza. `_as_int`
        lo rifiuta e la colonna lo lascia a NULL: SQL e modello puro vedono
        ENTRAMBI un rack senza altezza. Nessuna divergenza, quindi niente da
        rifiutare in nome di questa regola — l'avviso `carried_verbatim` resta a
        dire che quel campo non risponde a una query;
      - **intero dentro l'intervallo**: sì, ovviamente.

    Il «no» è uno solo: un intero fuori dall'intervallo, che è esattamente il caso
    in cui le due implementazioni darebbero due numeri diversi.
    """
    if value is None:
        return True
    n = _as_int(value)
    if n is None:
        return True
    return RACK_U_MIN <= n <= RACK_U_MAX


def slot_span(u: Any, h: Any, rack_u: Any) -> tuple[int, int] | None:
    """Intervallo `[lo, hi]` di slot occupati da un dispositivo, o `None`.

    Le cinque regole fisiche, in un posto solo (§2):

      - `h` assente o `0` vale 1. È il `d.h || 1` che l'applicazione applica da
        sempre: un dispositivo senza altezza dichiarata è alto una unità;
      - `h` NEGATIVO non occupa niente. Non è una scelta estetica: `h = -3` a partire
        da U10 significherebbe «da U10 a U8», cioè un intervallo rovesciato, e
        inventargli un verso vorrebbe dire decidere al posto di chi ha sbagliato a
        digitare;
      - lo slot iniziale `<= 0` sta FUORI dal rack: i rack si contano da 1, quindi la
        parte che sporge sotto non esiste, e un dispositivo interamente sotto lo zero
        non occupa niente;
      - la sporgenza oltre la cima del rack si TAGLIA. Un 4U montato a U44 di un rack
        da 45 occupa due unità, perché sono due quelle che esistono;
      - `h` o `u` non interi non occupano niente: non si arrotonda un dato che non è
        un numero di slot.
    """
    height = _as_int(rack_u)
    start = _as_int(u)
    if height is None or height < 1 or start is None:
        return None
    units = _as_int(h)
    if units is None or units == 0:
        units = DEFAULT_H
    lo = max(start, 1)
    hi = min(start + units - 1, height)
    if lo > hi:
        return None
    return lo, hi


def _spans(rack_u: Any, devices: Iterable[Any]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for device in devices or ():
        if not occupies_space(device):
            continue
        span = slot_span(_get(device, "u"), _get(device, "h"), rack_u)
        if span is not None:
            out.append(span)
    out.sort()
    return out


def occupied_slots(rack_u: Any, devices: Iterable[Any]) -> set[int]:
    """L'insieme degli slot occupati. Insieme, non somma: è tutta la differenza.

    ⚠ `SUM(h)` è la risposta sbagliata, e la fase 2E l'ha trovata in due
    implementazioni su tre (§8.48). Sbaglia in tre modi, tutti reali:

      - due dispositivi **sovrapposti** — cosa che l'applicazione permette e che nei
        dati veri esiste — contano due volte lo stesso slot, e un rack da 45 U risulta
        occupato per 48;
      - un dispositivo che **sporge** oltre la cima conta unità che non esistono;
      - un dispositivo `rimosso` conta comunque, perché la somma non guarda niente
        oltre l'altezza.

    L'insieme risponde alla domanda fisica: quali unità di questo rack non sono
    assegnabili a un apparato nuovo.

    ⚠ Da usare solo su rack di altezza plausibile: materializza uno slot per unità.
    Il conteggio per la vista Capacità passa da `rack_capacity`, che lavora sugli
    ESTREMI degli intervalli e non teme il rack da tre miliardi di unità del corpus
    `oversized-integers`. Questa funzione esiste perché è la definizione, e le fixture
    la usano come oracolo indipendente su rack piccoli.
    """
    slots: set[int] = set()
    for lo, hi in _spans(rack_u, devices):
        slots.update(range(lo, hi + 1))
    return slots


@dataclass(frozen=True)
class RackCapacity:
    total_u: int
    used_u: int
    free_u: int
    largest_free_run: int


def rack_capacity(rack_u: Any, devices: Iterable[Any]) -> RackCapacity:
    """Capacità di un rack: totale, occupato, libero, blocco contiguo più ampio.

    ⚠ Costa quanto i DISPOSITIVI, non quanto l'altezza del rack, e la differenza non
    è teorica: `rack.u` è un intero senza massimo e il corpus `oversized-integers` ne
    contiene uno da 3 000 000 000. Enumerare gli slot sarebbe la traduzione ovvia e
    sarebbe un guasto — nel browser esaurisce la memoria della scheda, in una
    richiesta HTTP produce tre miliardi di righe.
    """
    height = _as_int(rack_u)
    if height is None or height < 1:
        return RackCapacity(total_u=max(height or 0, 0), used_u=0, free_u=0,
                            largest_free_run=0)

    # Fusione degli intervalli: qui le sovrapposizioni smettono di contare due volte.
    # Si fondono anche quelli ADIACENTI (`lo <= hi_prec + 1`), così fra due isole
    # rimaste distinte c'è sempre almeno uno slot libero — che è ciò che rende il
    # calcolo dei buchi qui sotto una sottrazione e non una ricerca.
    islands: list[list[int]] = []
    for lo, hi in _spans(height, devices):
        if islands and lo <= islands[-1][1] + 1:
            islands[-1][1] = max(islands[-1][1], hi)
        else:
            islands.append([lo, hi])

    used = sum(hi - lo + 1 for lo, hi in islands)

    largest = 0
    cursor = 1
    for lo, hi in islands:
        if lo - cursor > largest:
            largest = lo - cursor
        cursor = hi + 1
    if height + 1 - cursor > largest:
        largest = height + 1 - cursor

    return RackCapacity(total_u=height, used_u=used, free_u=max(0, height - used),
                        largest_free_run=largest)


def percent(used: Any, total: Any) -> int:
    """Percentuale intera di occupazione, arrotondata HALF-UP. (§3)

    ⚠ Aritmetica INTERA, e non `round(used / total * 100)`. Tre linguaggi, tre
    risposte diverse sulla metà esatta:

        JavaScript   Math.round(0.5)  =  1     (metà verso l'alto)
        Python       round(0.5)       =  0     (metà al pari, «del banchiere»)
        PostgreSQL   round(0.5)       =  1     (metà lontano da zero)

    Un rack da 8 U con 1 U occupata è al 12,5%: il frontend mostrava 13 e Python
    avrebbe detto 12. Nessuno dei due è sbagliato in sé; averli entrambi lo è.

        floor(used * 100 / total + 1/2)  ==  (used * 200 + total) // (total * 2)

    La forma a destra non contiene divisioni in virgola mobile, quindi non contiene
    nemmeno il loro arrotondamento: le tre implementazioni danno lo stesso intero per
    costruzione, non per fortuna. Un totale nullo o negativo dà 0 — un rack alto zero
    unità non è occupato al 100%, non ha unità.
    """
    u, t = _as_int(used), _as_int(total)
    if u is None or t is None or t <= 0 or u <= 0:
        return 0
    return (u * 200 + t) // (t * 2)


# ==================================================================
# 3. la fila del rack: identità del gruppo ≠ etichetta mostrata
# ==================================================================

@dataclass(frozen=True)
class RowGroup:
    """Il gruppo «fila» di un rack.

    ⚠ `key` NON è `label`, e distinguerle è tutto il punto di §4. Il prototipo
    raggruppava per `rk.row || '—'`: una SENTINELLA che collide col dato, perché nel
    seed di produzione esiste un rack la cui fila è letteralmente «—» (CS-Q01). Quel
    rack finiva nel gruppo «senza fila» insieme a tutti quelli che non hanno una fila,
    e il totale di unità libere della fila «—» era la somma di due cose diverse.

    Da qui due campi: `key` identifica il gruppo e contiene un byte NUL, che nessun
    valore di documento può contenere (`json_strings.is_representable_text` lo rifiuta,
    §8.31) — è la stessa tecnica dei separatori di chiave in `identity.js`. `label` è
    ciò che si mostra, e per una fila non impostata resta «—»: l'interfaccia non
    cambia aspetto, cambia soltanto ciò che considera lo stesso gruppo.
    """
    assigned: bool
    value: str | None
    key: str
    label: str


def row_group(rack: Any) -> RowGroup:
    """Gruppo di una fila. Non impostata ≠ impostata al valore «—».

    Accetta un rack (oggetto o riga) oppure direttamente il valore della fila, perché
    lo SQL ha in mano la colonna `row_label` e non l'entità.
    """
    raw = rack if isinstance(rack, str) or rack is None else _get(rack, "row")
    value: str | None = None
    if isinstance(raw, str):
        value = raw if raw != "" else None
    elif raw is not None and not isinstance(raw, bool):
        value = str(raw)
    if value is None:
        return RowGroup(assigned=False, value=None, key="\x00none",
                        label=ROW_UNSET_LABEL)
    return RowGroup(assigned=True, value=value, key="\x00row\x00" + value,
                    label=value)


def row_sort_key(group: RowGroup) -> tuple:
    """Ordine dei gruppi: prima le file dichiarate, in ordine, poi «senza fila».

    Il gruppo senza fila va per ULTIMO di proposito: è il residuo, non una fila che si
    chiama «—», e metterlo in testa lo farebbe leggere come la prima fila della sala.
    """
    return (1, "") if not group.assigned else (0, group.value or "")


# ==================================================================
# 4. scadenze: un interprete di date solo
# ==================================================================

#: ⚠ `[0-9]` e non `\d`, e la differenza non è cosmetica.
#:
#: In Python `\d` combacia con OGNI cifra decimale Unicode, quindi
#: `２０２７-０３-１５` (cifre a larghezza intera) passava il controllo e `int()` la
#: convertiva in 2027: il backend interpretava quella data e il frontend no. È un
#: difetto che esisteva già in `notifications/expiry.py` prima di questa fase, ed è
#: stato il confronto fra le due implementazioni a scoprirlo — nessuna rilettura del
#: codice ci sarebbe arrivata.
#:
#: In JavaScript `\d` è già solo ASCII, quindi il contratto è quello e la forma
#: esplicita lo dice in entrambi i linguaggi.
_ISO_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")


def parse_expiry(value: Any) -> date | None:
    """`YYYY-MM-DD` → data. Qualunque altra cosa → `None`, in silenzio. (§6)

    ⚠ Questa è l'UNICA interpretazione di una data di scadenza nel prodotto, e la
    fase 2G la impone anche al frontend, che usava `new Date(v)`. Sette forme erano
    visibili nella vista Scadenze e invisibili al worker e all'endpoint (§8.48):

        2027-3-15              mese e giorno a una cifra
        2027/03/15             barre
        March 15, 2027         nome del mese
        2027-03-15T10:00:00Z   istante, non data di business
        2027-03                anno e mese
        2027                   anno
        2027-02-30             rollover: V8 la fa scorrere al 2 marzo

    L'ultima è la ragione per cui `new Date` non può essere una validazione:
    trasforma silenziosamente una data inesistente in una che esiste, e chi gestisce
    il contratto scoprirebbe la differenza il 2 marzo.

    Gli spazi intorno si tollerano — un valore incollato da un foglio di calcolo ne
    porta spesso — ma niente di più: nessuna forma nuova, nessuna euristica.

    ⚠ **Il valore grezzo non si riscrive MAI.** `supporto = "March 15, 2027"` resta
    nell'inventario esattamente com'è, e si limita a non essere una scadenza
    riconosciuta. Riscriverlo in `2027-03-15` sarebbe interpretare al posto
    dell'utente un dato che l'utente può ancora correggere; cancellarlo sarebbe
    perdere l'unica informazione disponibile su quel contratto.
    """
    if not isinstance(value, str):
        return None
    m = _ISO_DATE.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        # `2027-02-30`: sintatticamente giusta, nel calendario inesistente.
        return None


def expiry_level(days_remaining: int, warning_days: int) -> str:
    """`expired` / `warning` / `future` per la vista Scadenze. (§7)

    È il livello ISPETTIVO, non l'idoneità a un avviso: la vista mostra tutto — ciò
    che è scaduto, ciò che scade oggi, ciò che scadrà — perché la domanda a cui
    risponde è «cosa posso guardare», non «cosa devo spedire».
    """
    if days_remaining < 0:
        return "expired"
    if days_remaining <= warning_days:
        return "warning"
    return "future"


def notification_due(days_remaining: int, warning_days: Sequence[int]) -> bool:
    """La regola del worker: `0 <= giorni <= almeno una soglia`. (§7)

    Non `giorni == N`: pretendere il giorno esatto significherebbe che una macchina
    spenta il giorno del promemoria lo perde per sempre. Il recupero è una conseguenza
    della disuguaglianza, non un meccanismo a parte (§8.41).

    Gli SCADUTI restano esclusi. Non è una dimenticanza: un avviso su una scadenza già
    passata si ripeterebbe ogni giorno per sempre, oppure una volta sola e allora
    quale — è un prodotto diverso, e resta fuori. Si guardano nella vista Scadenze.
    """
    if not warning_days:
        return False
    return 0 <= days_remaining <= max(warning_days)


# ==================================================================
# 5. indirizzi: una grammatica sola
# ==================================================================
#
# ⚠ La fase 2E riproduceva `parseIpQuery` del frontend e con essa i suoi due limiti:
# nessun IPv6, e nessuna nozione di indirizzo ESATTO. Il secondo era un difetto vero e
# visibile: `10.0.0.1` non era una forma riconosciuta, quindi finiva nella ricerca
# testuale, e la sottostringa `10.0.0.1` sta dentro `10.0.0.100`. Chi cercava una
# macchina precisa riceveva la sua vicina di sottorete.

_OCTET = r"\d{1,3}"
_IPV4_TEXT = rf"{_OCTET}\.{_OCTET}\.{_OCTET}\.{_OCTET}"
_RE_IPV4 = re.compile(rf"^{_IPV4_TEXT}$")
_RE_V4_RANGE = re.compile(rf"^({_IPV4_TEXT})\s*-\s*({_IPV4_TEXT})$")
_RE_V4_WILDCARD = re.compile(rf"^((?:{_OCTET}\.){{1,3}})\*$")
_RE_CIDR = re.compile(r"^(\S+)/(\d{1,3})$")

#: Caratteri ammessi in una forma IPv6 testuale, prima di darla a `ipaddress`. Serve
#: a escludere ciò che `ipaddress` accetterebbe e che non è un indirizzo di questo
#: prodotto: gli identificatori di zona (`fe80::1%eth0`) e gli spazi interni.
_RE_IPV6_SHAPE = re.compile(r"^[0-9A-Fa-f:.]+$")


@dataclass(frozen=True)
class Address:
    """Un indirizzo host, normalizzato. `family` è 4 o 6."""
    family: int
    value: int
    #: Forma canonica: è questa che si passa a PostgreSQL, mai il testo dell'utente.
    text: str


@dataclass(frozen=True)
class AddressQuery:
    """Un intervallo di indirizzi della STESSA famiglia, estremi compresi."""
    family: int
    lo: Address
    hi: Address
    #: exact | cidr | range | wildcard. Non cambia il confronto: serve a spiegare
    #: all'utente che cosa è stato riconosciuto.
    kind: str


def parse_address(value: Any) -> Address | None:
    """Testo → indirizzo host, o `None`. IPv4 e IPv6, niente prefissi.

    ⚠ È l'UNICO punto in cui il prodotto decide se un testo è un indirizzo, e da qui
    viene anche la colonna derivata `ip_addr` della proiezione: PostgreSQL non
    interpreta mai il testo dell'utente, riceve solo la forma canonica che questa
    funzione ha prodotto. È il motivo per cui `inet` è diventato utilizzabile senza
    portarsi dietro la sua grammatica — che accetta `10.1` come `10.0.0.1` e
    `10.0.0.0/8` come indirizzo, due cose che questo prodotto non vuole.

    Gli zeri iniziali negli ottetti IPv4 si accettano (`010.0.0.1` è `10.0.0.1`),
    perché `ipToNum` li accettava e togliere una forma che funzionava è una
    regressione. `ipaddress.IPv4Address` li rifiuta, quindi l'IPv4 lo si calcola qui.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    if _RE_IPV4.match(text):
        parts = [int(p) for p in text.split(".")]
        if any(p > 255 for p in parts):
            return None
        number = ((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3]
        return Address(family=4, value=number,
                       text=".".join(str(p) for p in parts))

    if ":" in text and _RE_IPV6_SHAPE.match(text):
        try:
            addr = ipaddress.IPv6Address(text)
        except ValueError:
            return None
        return Address(family=6, value=int(addr), text=str(addr))

    return None


def _address_at(family: int, number: int) -> Address:
    if family == 4:
        return Address(family=4, value=number,
                       text=str(ipaddress.IPv4Address(number)))
    return Address(family=6, value=number, text=str(ipaddress.IPv6Address(number)))


def parse_address_query(raw: Any) -> AddressQuery | None:
    """Query → intervallo di indirizzi, o `None` («non è un indirizzo, cercalo come
    testo»). (§5)

    Le forme, e solo queste:

        10.0.0.1                    esatto IPv4
        2001:db8::1                 esatto IPv6
        10.0.2.0/24                 CIDR IPv4
        2001:db8::/32               CIDR IPv6
        10.0.0.1 - 10.0.0.99        intervallo IPv4
        10.0.*                      jolly IPv4

    ⚠ NON esistono intervalli né jolly IPv6, e non si inventano: `2001:db8::*`
    dovrebbe voler dire «un gruppo qualsiasi» o «il resto dell'indirizzo»? Ogni
    risposta è una grammatica nuova che nessuno ha chiesto, e sbagliarla vorrebbe dire
    mostrare all'utente una rete diversa da quella che ha cercato. Restano testo, e
    come testo trovano ciò che contiene quella sottostringa.

    ⚠ Il ritorno `None` significa «cercalo come testo», NON «nessun risultato». La
    differenza si vede su `10.0.0`: non è un indirizzo, e come testo trova `10.0.0.1`,
    `10.0.0.2`… che è ciò che uno si aspetta scrivendo mezzo indirizzo.
    """
    if not isinstance(raw, str):
        return None
    q = raw.strip()
    if not q:
        return None

    m = _RE_CIDR.match(q)
    if m:
        base = parse_address(m.group(1))
        bits = int(m.group(2))
        if base is None:
            return None
        width = 32 if base.family == 4 else 128
        if bits > width:
            return None
        size = 1 << (width - bits)
        start = (base.value // size) * size
        return AddressQuery(family=base.family,
                            lo=_address_at(base.family, start),
                            hi=_address_at(base.family, start + size - 1),
                            kind="cidr")

    m = _RE_V4_RANGE.match(q)
    if m:
        a, b = parse_address(m.group(1)), parse_address(m.group(2))
        if a is None or b is None:
            return None
        lo, hi = (a, b) if a.value <= b.value else (b, a)
        return AddressQuery(family=4, lo=lo, hi=hi, kind="range")

    m = _RE_V4_WILDCARD.match(q)
    if m:
        parts = [int(p) for p in m.group(1).split(".") if p]
        if any(p > 255 for p in parts):
            return None
        low = parse_address(".".join(str(p) for p in parts + [0] * (4 - len(parts))))
        high = parse_address(".".join(str(p) for p in parts + [255] * (4 - len(parts))))
        if low is None or high is None:
            return None
        return AddressQuery(family=4, lo=low, hi=high, kind="wildcard")

    exact = parse_address(q)
    if exact is not None:
        return AddressQuery(family=exact.family, lo=exact, hi=exact, kind="exact")

    return None


def address_matches(ip_text: Any, query: AddressQuery | None) -> bool:
    """L'indirizzo di un dispositivo cade in questo intervallo?

    ⚠ La FAMIGLIA deve combaciare. Un jolly `10.0.*` non trova `::a00:1` anche se
    quell'IPv6 ha lo stesso valore numerico dell'IPv4 corrispondente: sono due spazi
    di indirizzamento, e confonderli farebbe comparire in una ricerca di rete privata
    IPv4 macchine che non ci sono. È anche l'ordinamento che PostgreSQL dà al tipo
    `inet` — prima la famiglia, poi l'indirizzo — quindi la regola è la stessa nei tre
    posti in cui viene applicata.
    """
    if query is None:
        return False
    addr = parse_address(ip_text)
    if addr is None or addr.family != query.family:
        return False
    return query.lo.value <= addr.value <= query.hi.value


# ==================================================================
# 6. ricerca testuale
# ==================================================================

#: Campi del DISPOSITIVO su cui cerca la barra globale. (§5)
#:
#: ⚠ `note` NON c'è, per decisione di questa fase: sono testo libero e lungo, e
#: includerle renderebbe qualunque parola comune un risultato di massa. È una scelta
#: rivedibile, non una dimenticanza — la differenza sta scritta qui.
#:
#: `tipo`, `stato` e `presenza` si cercano nel VALORE MEMORIZZATO (`server`, `attivo`,
#: `rimosso`), non nell'etichetta tradotta: le etichette vivono nell'interfaccia e
#: cambiano con la lingua, i valori sono il dato. E si cercano PASSANDO DAL DEFAULT:
#: un dispositivo senza `stato` è `attivo` e va trovato cercando «attivo», perché è
#: così che l'interfaccia lo mostra e così che la proiezione lo memorizza (§8.14).
DEVICE_SEARCH_FIELDS: tuple[str, ...] = ("id", "name", "model", "ip", "serial",
                                         "owner", "tipo", "stato", "presenza")

#: Campi del RACK. `seriali` è un elenco: combacia se combacia almeno un elemento.
RACK_SEARCH_FIELDS: tuple[str, ...] = ("id", "name", "seriali")


def contains(haystack: Any, needle: str) -> bool:
    """Sottostringa LETTERALE, senza distinzione di maiuscole. (§5)

    ⚠ Letterale: `%` e `_` sono caratteri normali in una casella di ricerca. Con un
    `LIKE` una query contenente `%` troverebbe tutto, e l'utente non ha scritto un
    modello, ha scritto un carattere.
    """
    if haystack is None or isinstance(haystack, bool):
        return False
    return needle in str(haystack).lower()


def device_search_value(device: Any, field: str) -> Any:
    """Il valore cercabile di un campo del dispositivo."""
    if field == "tipo":
        return tipo_of(device)
    if field == "stato":
        return stato_of(device)
    if field == "presenza":
        return presenza_of(device)
    return _get(device, field)


def device_matches(device: Any, needle: str) -> bool:
    """Il dispositivo combacia con la query TESTUALE? (modalità indirizzo esclusa)"""
    if not needle:
        return False
    return any(contains(device_search_value(device, f), needle)
               for f in DEVICE_SEARCH_FIELDS)


def rack_matches(rack: Any, needle: str) -> bool:
    """Il rack combacia con la query testuale?

    ⚠ I rack NON partecipano alla modalità indirizzo (§5): un rack che si chiama
    «10.0.0.1» non è una macchina con quell'indirizzo, e restituirlo a chi cerca un
    host sarebbe un falso positivo di un genere particolarmente fastidioso — sembra
    una risposta.
    """
    if not needle:
        return False
    if contains(_get(rack, "id"), needle) or contains(_get(rack, "name"), needle):
        return True
    seriali = _get(rack, "seriali")
    if isinstance(seriali, (list, tuple)):
        return any(contains(s, needle) for s in seriali)
    return False


# ==================================================================
# 7. etichette: mai un valore dell'implementazione
# ==================================================================

def label_candidate(value: Any) -> str | None:
    """Questo valore può essere un'etichetta per una persona? (§9)

    Regola esplicita, perché le due implementazioni non hanno la stessa idea di
    «vuoto» e la differenza si vede sui dati importati da un foglio di calcolo:

      - una STRINGA non vuota sì, anche di soli spazi: è ciò che l'utente ha scritto;
      - un NUMERO diverso da zero sì, in forma decimale. `name: 42` diventa «42», che
        è come lo scanner delle scadenze l'ha sempre mostrato;
      - zero, `false`, `null`, elenchi e oggetti NO. `String([])` in JavaScript è la
        stringa vuota e `str([])` in Python è `"[]"`: due etichette diverse per lo
        stesso dato, cioè esattamente ciò che questa fase elimina. Nessuno dei due
        valori è un'etichetta, e la risposta giusta è passare al candidato successivo.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value != "" else None
    if isinstance(value, int):
        return None if value == 0 else str(value)
    if isinstance(value, float):
        if value == 0:
            return None
        # `String(42.0)` in JavaScript è «42»; `str(42.0)` in Python è «42.0». Si
        # scrive la forma di JavaScript, perché è quella che l'utente ha visto
        # nell'interfaccia da sempre.
        return str(int(value)) if value.is_integer() else repr(value)
    return None


def label(*candidates: Any) -> str:
    """Primo candidato utilizzabile, altrimenti «(senza nome)». (§9)

    L'ordine è quello del requisito: **nome mostrabile**, poi **codice di business**,
    poi il ripiego. Mai `None`, mai `undefined`, mai `null`: la fase 2F conservava
    deliberatamente `str(None) == "None"` nel contesto degli avvisi, perché correggerlo
    lì avrebbe cambiato il testo di un messaggio reale senza che nessuno l'avesse
    chiesto. Adesso l'ha chiesto (§9), e «None» in un'email a un cliente non è un
    dato: è un difetto che si legge.
    """
    for candidate in candidates:
        usable = label_candidate(candidate)
        if usable is not None:
            return usable
    return NO_NAME


def device_label(device: Any) -> str:
    return label(_get(device, "name"), _get(device, "id"))


def rack_label(rack: Any) -> str:
    return label(_get(rack, "name"), _get(rack, "id"))


def room_label(room: Any) -> str:
    return label(_get(room, "nome"), _get(room, "id"))


def location_label(location: Any) -> str:
    return label(_get(location, "nome"), _get(location, "id"))
