"""Da dove il worker prende le scadenze correnti: la PROIEZIONE, non il documento.

Fase 2F. Cambia **soltanto la sorgente** dei candidati (§8.47):

    prima    documento canonico → `expiry.due_items(doc)` → precedenza/consegna
    dopo     proiezione         → questo modulo           → precedenza/consegna

Tutto ciò che viene dopo la selezione dei candidati — soglia più urgente, soglie
superate, identità del promemoria, idempotenza, ritentativi, `Message-ID`,
destinatari, `scheduler_runs`, audit — è rimasto dov'era e come era. Questo modulo non
sa niente di promemoria: risponde a una domanda sola, «quali scadenze del dispositivo
possono interessare a questo giro?», ed è la stessa domanda a cui rispondeva
`due_items` leggendo il documento.

⚠ NON È L'ENDPOINT `/api/inventory/expiries`
--------------------------------------------
Quell'endpoint riproduce di proposito la **vista Scadenze** del frontend, e la vista
Scadenze e il worker non sono d'accordo (§8.48). Le due differenze che contano:

  - la vista **salta i dispositivi dismessi**; il worker **no** — `due_items` scorre
    `walk(doc)` e non guarda `stato`, quindi una macchina dismessa con la garanzia in
    scadenza produce un promemoria oggi e deve continuare a produrlo;
  - la vista elenca **anche gli scaduti** e i futuri fuori finestra; il worker manda
    solo `0 <= giorni <= soglia più larga`.

Usare l'endpoint come sorgente avrebbe cambiato entrambe le cose in silenzio,
travestendo una modifica di prodotto da migrazione tecnica. Da qui la scelta di NON
importare niente da `app/inventory/queries.py`: quel modulo ha semantica propria e
condividerne un pezzo — anche solo le JOIN — è l'inizio di condividerne il resto.

Le date le legge chi le ha scritte
----------------------------------
Si usano le colonne DERIVATE `garanzia_date` / `supporto_date`. Non è una comodità:
quelle colonne le ha calcolate `parse_expiry`, cioè **questo stesso parser** (§8.44,
`relational.DERIVED`). Interpretare qui il testo grezzo significherebbe avere due idee
di «data valida» nello stesso processo, e due idee divergono sui casi limite — che sono
esattamente i casi che un inventario compilato a mano produce.

Il testo grezzo `garanzia` / `supporto` resta il dato autorevole e non si tocca: la
colonna data è la sua *interpretazione*, e il `CHECK`
`ck_device_garanzia_date_needs_text` impedisce che esista una data senza il testo da cui
è stata derivata.

Riferimento: BACKEND-PLAN.md §8.47, §8.41.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.inventory import projection
from app.notifications.expiry import EXPIRY_KINDS, DueItem

#: Nome dato dallo scanner a un dispositivo senza nome e senza id. Sta qui perché è
#: parte della semantica riprodotta, non una stringa di comodo: `due_items` scrive
#: `entity.obj.get("name") or entity.obj.get("id") or "(senza nome)"`.
NO_NAME = "(senza nome)"


@dataclass(frozen=True)
class Candidates:
    """I candidati di UN giro, con la revisione da cui provengono.

    La revisione viaggia insieme alle voci di proposito (§8.47): chi sta per creare
    una consegna deve poter chiedere «l'inventario è ancora quello che ho letto?»
    senza rileggere il set di candidati. Portarla in un campo a parte, e non dedurla
    più tardi, è ciò che rende possibile il controllo — un secondo `SELECT` sulla
    testa risponderebbe sulla testa di *adesso*, che è la domanda sbagliata.
    """

    version: int
    sha256: str
    items: list[DueItem]
    #: Riscontri NON bloccanti di `validate_model`: `garanzia = "in attesa"` e simili.
    #: Si portano fino al chiamante perché siano registrabili — un avviso che non
    #: esce da qui è un avviso che nessuno vedrà mai, e questi sono precisamente i
    #: valori che spiegano perché una scadenza attesa non compare nel digest.
    warnings: list = field(default_factory=list)


# ==================================================================
# etichette: le stesse stringhe che componeva `walk`, ricavate dalle colonne
# ==================================================================

def _value(column: Any, extra: Any) -> Any:
    """Il valore del documento per un campo, da colonna **o** da `extra`.

    La mappa relazionale mette ogni chiave in ESATTAMENTE uno dei due (§8.44,
    `relational._split`): nella colonna se il tipo ci sta, in `extra` altrimenti. Un
    `name: 42` non è una stringa, quindi la colonna è NULL e il 42 sta in `extra` — e
    per lo scanner quel dispositivo si chiamava «42», perché in Python `42 or …` è
    `42`.

    Guardare solo la colonna avrebbe fatto sparire quel nome e mostrato l'id al suo
    posto: una divergenza silenziosa, invisibile in ogni inventario ben formato e
    visibile solo in quelli importati da un foglio di calcolo.

    ⚠ `extra -> 'chiave'` restituisce `None` sia per «chiave assente» sia per
    «chiave presente con valore JSON null». Le due cose sono diverse nel documento e
    identiche per lo scanner (`.get()` dà `None` in entrambi i casi), quindi qui la
    coincidenza è corretta e non va «sistemata».
    """
    return column if column is not None else extra


def device_label(name: Any, name_extra: Any, code: Any, code_extra: Any) -> str:
    """`obj.get("name") or obj.get("id") or "(senza nome)"`, poi `str()`.

    Riprodotto alla lettera, con la falsità di Python che è quella che lo scanner ha
    sempre applicato: `None`, `""`, `0`, `False`, `[]`, `{}` fanno passare al
    candidato successivo. Una stringa vuota NON diventa il nome, e un `name: 0`
    nemmeno — sono gli stessi due casi in cui il documento canonico non riempie
    niente, perché `name` e `id` non hanno un valore predefinito (§8.14).
    """
    for candidate in (_value(name, name_extra), _value(code, code_extra)):
        if candidate:
            return str(candidate)
    return NO_NAME


def path_label(code: Any, code_extra: Any) -> str:
    """L'`id` di sito/sala/rack come lo scriveva il percorso di `walk`.

    `walk` compone `f"{L['id']} / {R['id']} / {K['id']} / {V['id']}"` e `_context` lo
    rispezza sugli `/`: il valore che arrivava nel digest era quindi `str(id)`, **con
    `str(None)` che vale `"None"`** quando l'id manca. Non è un difetto da correggere
    qui: `id` non è obbligatorio nello schema del documento, e un sito senza id
    mostrava «None» nell'avviso. Riprodurlo costa una riga e mantiene la parità
    esatta; «correggerlo» in questo commit cambierebbe il testo di un avviso reale
    senza che nessuno l'abbia chiesto.

    ⚠ L'UNICA divergenza voluta della fase 2F sta qui, e riguarda gli id che
    contengono `/`. Il percorso era una stringa sola e `_context` la spezzava su ogni
    `/`: un rack di codice `10.0.0.0/24` arrivava nel digest come `10.0.0.0`, e un
    **sito** con uno `/` nel codice spostava di un posto sito, sala e rack. La JOIN ha
    il valore intero e lo restituisce intero (§7 della fase 2F: il contesto si ottiene
    dalle JOIN). Riprodurre il troncamento vorrebbe dire scrivere codice nuovo il cui
    unico scopo è corrompere un valore che il database ha già giusto. La differenza è
    misurata in `test_worker_sql_pg.py` — che fissa ENTRAMBI i valori — e registrata
    in §8.48; non cambia MAI quali scadenze sono dovute, solo come si legge la
    posizione nel corpo del messaggio.
    """
    return str(_value(code, code_extra))


# ==================================================================
# i candidati di un giro
# ==================================================================

#: Colonne dell'etichetta. Si prendono colonna **e** `extra` per ogni campo: vedi
#: `_value`. `extra -> 'chiave'` e non `->>`: `->>` darebbe il testo JSON, quindi
#: `"42"` con le virgolette per una stringa e nessun modo di distinguere i tipi, e
#: l'etichetta è `str()` del VALORE, non del suo JSON.
_LABEL_COLUMNS = """
       d.name AS dev_name,  d.extra -> 'name' AS dev_name_extra,
       d.code AS dev_code,  d.extra -> 'id'   AS dev_code_extra,
       k.code AS rack_code, k.extra -> 'id'   AS rack_code_extra,
       r.code AS room_code, r.extra -> 'id'   AS room_code_extra,
       l.code AS loc_code,  l.extra -> 'id'   AS loc_code_extra
"""

#: L'albero. `JOIN` e non `LEFT JOIN`: le tre chiavi esterne sono `NOT NULL` e
#: puntano a righe esistenti, quindi ogni dispositivo ha un rack, una sala e un sito
#: — e non potrebbe essere altrimenti, perché nel documento un dispositivo esiste
#: solo dentro un rack. Un test lo pretende dallo schema invece di fidarsi di questa
#: frase: se un domani una di quelle colonne diventasse annullabile, una `JOIN`
#: interna farebbe sparire dei promemoria in silenzio.
_TREE = """
  FROM inventory_devices d
  JOIN inventory_racks     k ON k.uid = d.rack_uid
  JOIN inventory_rooms     r ON r.uid = k.room_uid
  JOIN inventory_locations l ON l.uid = r.location_uid
"""

#: Un ramo per tipo di scadenza, unite con `UNION ALL`.
#:
#: ⚠ NON `WHERE garanzia_date BETWEEN … OR supporto_date BETWEEN …`. Con l'`OR`
#: PostgreSQL non può usare nessuno dei due indici parziali e scansiona la tabella;
#: con due rami ne usa uno per ramo. E l'`UNION ALL` produce già la forma giusta —
#: una riga per (dispositivo, tipo) — che è esattamente ciò che
#: `devices_with_expiries` restituiva con il suo ciclo su `EXPIRY_KINDS`.
#:
#: ⚠ NESSUN filtro su `stato`. È la differenza con l'endpoint (§8.48): lo scanner non
#: ha mai guardato `stato`, e un dispositivo dismesso con la garanzia in scadenza ha
#: sempre prodotto un promemoria. Aggiungere qui il filtro della vista Scadenze
#: sarebbe una modifica di prodotto mascherata da migrazione.
_CANDIDATE_BRANCH = """
    SELECT '{kind}'::text AS kind, d.uid AS uid, d.{kind}_date AS expiry,
{labels}
    {tree}
     WHERE d.{kind}_date >= CAST(:today AS date)
       AND d.{kind}_date <= CAST(:until AS date)
"""

_CANDIDATES_SQL = " UNION ALL ".join(
    _CANDIDATE_BRANCH.format(kind=kind, labels=_LABEL_COLUMNS, tree=_TREE)
    for kind in EXPIRY_KINDS)


def _warnings(valid) -> list[dict]:
    """Gli avvisi di `validate_model`, in forma leggibile. NON bloccano.

    `garanzia = "in attesa"` è un valore che una persona ha scritto in una casella di
    testo, non un guasto dello schema: fermare gli avvisi di scadenza per quello
    significherebbe che un campo compilato male in un rack spegne le notifiche di tutti
    gli altri. Bloccano solo gli ERRORI, e quelli li solleva `require_valid_model`.
    """
    from app.inventory.relational_validate import warnings as _w

    return [f.as_dict() for f in _w(valid.findings)]


def _item(row: Any, *, today: date) -> DueItem:
    return DueItem(
        entity_uid=str(row.uid),
        kind=row.kind,
        expiry=row.expiry,
        days_remaining=(row.expiry - today).days,
        device=device_label(row.dev_name, row.dev_name_extra,
                            row.dev_code, row.dev_code_extra),
        rack=path_label(row.rack_code, row.rack_code_extra),
        room=path_label(row.room_code, row.room_code_extra),
        location=path_label(row.loc_code, row.loc_code_extra))


def due_items_from_projection(conn: Connection, *, today: date,
                              warning_days: list[int]) -> Candidates:
    """Le scadenze in finestra, dalla proiezione. Sostituisce `due_items(doc, …)`.

    Precondizione, e non è negoziabile: la proiezione deve rispecchiare la testa.
    `require_current_head` pretende tutte e quattro le condizioni della fase 2F §4 —
    lo stato esiste, dichiara la versione della testa, dichiara il digest della testa,
    ed è stato scritto dalla mappa che gira adesso — e solleva
    `ProjectionNotCurrentError` altrimenti. **Nessun ripiego** su
    `inventory_versions.doc`: un ripiego funzionerebbe, nessuno aprirebbe un ticket, e
    coprirebbe esattamente il difetto di coerenza che la fase 2 esiste per scoprire
    (§8.45).

    `conn` deve essere uno SNAPSHOT stabile (`REPEATABLE READ, READ ONLY`,
    `db.read_snapshot`). Sotto READ COMMITTED la testa, lo stato e le quattro tabelle
    si leggerebbero in cinque istanti diversi, e un `PUT` che committa nel mezzo
    darebbe candidati di due versioni con la revisione di una terza.

    La FINESTRA è quella dello scanner: `0 <= giorni <= max(warning_days)`. Si
    interroga solo quell'intervallo invece di leggere tutte le date e scartarle in
    Python — che è ciò che faceva la scansione del documento.
    """
    # ⚠ VALIDAZIONE DEL MODELLO, e non solo attualità. `require_valid_model` fa i
    # quattro passi: testa, dichiarazione della proiezione, lettura del modello,
    # `validate_model`. Il quarto è quello che conta qui, ed è l'unico che vede le
    # colonne DERIVATE.
    #
    # Senza, restava un modo di tacere: `garanzia_date` azzerata a mano lascia il
    # documento identico e il digest uguale — le date derivate non tornano nel
    # documento — quindi versione, digest e versione della mappa combaciano tutti, la
    # guardia è soddisfatta, e la query non trova niente. Nessun avviso parte e niente
    # lo dice. È il punto cieco della fase 2B, e per il worker è il peggiore possibile:
    # un sistema di allerta che non allerta e si dichiara sano.
    #
    # Costa una lettura completa della proiezione. È un costo consapevole: il worker
    # gira una volta al giorno, e tacere su una scadenza costa di più (§8.47.4).
    valid = projection.require_valid_model(conn)
    version, sha256 = valid.version, valid.recorded

    # Elenco vuoto = nessuna finestra configurata = niente è dovuto, e non c'è
    # nessuna query da fare. Stessa uscita anticipata di `due_items`, per lo stesso
    # motivo: `max(())` solleverebbe. La validazione è già avvenuta: «non ho guardato»
    # e «ho guardato e non c'era niente» devono restare distinguibili.
    if not warning_days:
        return Candidates(version=version, sha256=sha256, items=[],
                          warnings=_warnings(valid))

    until = today + timedelta(days=max(warning_days))
    rows = conn.execute(text(_CANDIDATES_SQL),
                        {"today": today, "until": until}).all()

    items = [_item(row, today=today) for row in rows]
    # Stesso ordinamento di `due_items`, con la stessa chiave: due giri sullo stesso
    # inventario devono produrre lo stesso digest riga per riga, e la chiave è totale
    # (lo stesso `_uid` compare due volte, ma con `kind` diverso).
    items.sort(key=lambda i: (i.days_remaining, i.kind, i.location, i.room,
                              i.rack, i.device, i.entity_uid))
    return Candidates(version=version, sha256=sha256, items=items,
                      warnings=_warnings(valid))


# ==================================================================
# il contesto di un digest da ritentare
# ==================================================================

#: Come sopra, ma per `_uid` noti e SENZA finestra: un ritentativo ricompone le voci
#: dai promemoria agganciati alla consegna, e i promemoria hanno la loro data. Il
#: filtro sulla finestra qui sarebbe sbagliato — un promemoria creato ieri per una
#: soglia larga può essere ritentato oggi senza che la data sia cambiata.
_CONTEXT_SQL = f"""
    SELECT d.uid AS uid, d.garanzia_date, d.supporto_date,
{_LABEL_COLUMNS}
    {_TREE}
     WHERE d.uid = ANY(CAST(:uids AS uuid[]))
"""


def context_by_key(conn: Connection, uids: Sequence[str]) -> dict:
    """`(uid, tipo, data) → (nome, rack, sala, sito)`, per i soli `_uid` chiesti.

    È il rimpiazzo dell'indice che `_rebuild_selection` costruiva con
    `devices_with_expiries(doc)`. La chiave a tre elementi non è ridondante, ed è la
    parte che va conservata: un promemoria si ricompone solo se il dispositivo esiste
    ANCORA e ha ANCORA quella data per quel tipo. Se qualcuno ha corretto la garanzia
    nel frattempo, la voce esce dal digest e il promemoria viene chiuso col resto —
    non si manda un avviso su una scadenza che non c'è più.

    Anche qui la proiezione deve essere attuale **e coerente**: un ritentativo che
    leggesse nomi da una proiezione vecchia comporrebbe un digest con posizioni
    sbagliate sotto un `Message-ID` già usato, e uno che leggesse da un modello
    incoerente potrebbe non trovare la chiave e chiudere la consegna dichiarando
    inviati promemoria che nessuno ha ricevuto.
    """
    projection.require_valid_model(conn)
    if not uids:
        return {}

    out: dict = {}
    rows = conn.execute(text(_CONTEXT_SQL), {"uids": list(uids)}).all()
    for row in rows:
        label = (device_label(row.dev_name, row.dev_name_extra,
                              row.dev_code, row.dev_code_extra),
                 path_label(row.rack_code, row.rack_code_extra),
                 path_label(row.room_code, row.room_code_extra),
                 path_label(row.loc_code, row.loc_code_extra))
        for kind in EXPIRY_KINDS:
            expiry = getattr(row, f"{kind}_date")
            if expiry is not None:
                out[(str(row.uid), kind, expiry)] = label
    return out


def unchanged(conn: Connection, *, version: int, sha256: str) -> bool:
    """L'inventario è ancora la revisione da cui vengono i candidati?

    Il controllo della fase 2F §5. Si fa nella transazione di SCRITTURA, appena prima
    di prenotare promemoria e consegna: i candidati arrivano da uno snapshot che si è
    già chiuso, e fra quel momento e questo un `PUT` può aver cambiato l'inventario.
    Mandare un avviso calcolato su una revisione che non esiste più significa
    annunciare una scadenza che qualcuno ha appena corretto.

    Confronta la testa **e** l'attualità della proiezione: fra i due momenti può essere
    cambiata la testa (un salvataggio) oppure può essere caduta la proiezione (un
    `--rebuild` in corso), e in entrambi i casi i candidati non sono più fondati.

    ⚠ NON blocca `inventory_head`. Un `SELECT … FOR UPDATE` qui terrebbe la riga di
    testa bloccata per tutta la consegna SMTP — cioè per un timeout di rete — e
    fermerebbe ogni salvataggio degli utenti nel frattempo. La finestra che resta è
    quella fra questo controllo e il `commit` dopo l'invio: un `PUT` che committa lì
    dentro fa partire un avviso vecchio di una revisione. È il costo dichiarato di non
    bloccare, ed è comunque molto più stretto della finestra che c'era prima, quando
    nessuno controllava niente.
    """
    found = projection.currency(conn)
    return (found.current
            and found.head_version == version
            and found.head_sha256 == sha256)
