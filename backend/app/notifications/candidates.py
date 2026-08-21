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

⚠ NON È L'ENDPOINT `/api/inventory/expiries`, e dalla 2G la ragione è più netta
------------------------------------------------------------------------------
Le due domande sono diverse, e la fase 2G le ha rese diverse **di proposito** invece
di lasciarle divergere per caso (§7 del requisito, §8.50):

    vista Scadenze   «quali informazioni di scadenza può ispezionare un operatore?»
                     → tutte quelle valide: scadute, di oggi, future. **Compresi i
                       dismessi**, che restano ispezionabili e cercabili.
    questo modulo    «quale scadenza ATTUALMENTE AZIONABILE richiede un'email?»
                     → `0 <= giorni <= finestra più larga`, e **non i dismessi**.

⚠ Il filtro sui dismessi è NUOVO nella 2G, e cambia il comportamento del worker.

Fino alla 2F lo scanner non guardava `stato`: una macchina dismessa con la garanzia in
scadenza produceva un promemoria. La 2F l'ha conservato di proposito — era il
comportamento misurato, e correggerlo durante una migrazione tecnica avrebbe mescolato
due cose. La 2G lo decide: nessuno deve rinnovare la garanzia di un apparato che non
tornerà in servizio. `attivo`, `manutenzione` e `dismissione` restano idonei, perché
«in dismissione» significa che la decisione non è ancora conclusa.

⚠ La PRESENZA FISICA non c'entra con l'idoneità. Un apparato portato in un altro sito
ha la garanzia che scade comunque, e chi la rinnova ha bisogno di saperlo. La presenza
decide l'occupazione dello spazio (la vista Capacità), non gli avvisi.

Resta la scelta di NON importare niente da `app/inventory/queries.py`: quel modulo
risponde all'altra domanda, e condividerne un pezzo — anche solo le JOIN — è l'inizio
di condividerne il resto. Ciò che i due condividono è `app/domain.py`, cioè le
DECISIONI, non le query.

Le date le legge chi le ha scritte
----------------------------------
Si usano le colonne DERIVATE `garanzia_date` / `supporto_date`. Non è una comodità:
quelle colonne le ha calcolate `domain.parse_expiry`, l'unico interprete di date del
prodotto (§8.50). Interpretare qui il testo grezzo significherebbe avere due idee di
«data valida» nello stesso processo, e due idee divergono sui casi limite — che sono
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

from app import domain
from app.inventory import projection
from app.notifications.expiry import EXPIRY_KINDS, DueItem

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
# etichette: quelle del contratto, ricavate dalle colonne
# ==================================================================

def _value(column: Any, extra: Any) -> Any:
    """Il valore del documento per un campo, da colonna **o** da `extra`.

    La mappa relazionale mette ogni chiave in ESATTAMENTE uno dei due (§8.44,
    `relational._split`): nella colonna se il tipo ci sta, in `extra` altrimenti. Un
    `name: 42` non è una stringa, quindi la colonna è NULL e il 42 sta in `extra` — e
    per l'utente quel dispositivo si chiama «42», perché è così che l'interfaccia lo
    mostra.

    Guardare solo la colonna avrebbe fatto sparire quel nome e mostrato l'id al suo
    posto: una divergenza silenziosa, invisibile in ogni inventario ben formato e
    visibile solo in quelli importati da un foglio di calcolo.

    ⚠ `extra -> 'chiave'` restituisce `None` sia per «chiave assente» sia per
    «chiave presente con valore JSON null». Le due cose sono diverse nel documento e
    identiche per il contratto (`domain.label_candidate` scarta entrambe), quindi qui
    la coincidenza è corretta e non va «sistemata».
    """
    return column if column is not None else extra


def _label(*pairs: tuple) -> str:
    """`domain.label` sulle coppie (colonna, extra).

    ⚠ È qui che la fase 2G chiude le voci 11 e 12 del registro (§8.48).

    Fino alla 2F il contesto veniva dal PERCORSO impacchettato di `walk` — la stringa
    «sito / sala / rack / dispositivo» — rispezzata sugli `/`. Da qui due difetti: un
    id che contiene uno `/` veniva troncato (`10.0.0.0/24` diventava `10.0.0.0`) e ogni
    pezzo dopo di lui scalava di un posto; un id ASSENTE arrivava nel digest come la
    stringa **«None»**, perché il percorso era una f-string.

    La 2F aveva già risolto il primo — le JOIN restituiscono il valore intero — e
    conservato il secondo di proposito, perché correggerlo avrebbe cambiato il testo di
    un avviso reale senza che nessuno l'avesse chiesto. Adesso l'ha chiesto (§9): «None»
    in un'email a un cliente non è un dato, è un difetto che si legge.

    ⚠ E cambia l'ORDINE dei candidati: `_label` preferisce il nome mostrabile al
    codice, quindi un sito che si chiama «Pomezia G0» con codice `pomezia-g0` ordina
    sotto P e non sotto p. L'ordinamento resta TOTALE e deterministico, che è ciò che
    il digest richiede; cambia il testo del messaggio, e cambia in meglio.
    """
    return domain.label(*(_value(col, extra) for col, extra in pairs))


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
       k.name AS rack_name, k.extra -> 'name' AS rack_name_extra,
       k.code AS rack_code, k.extra -> 'id'   AS rack_code_extra,
       r.nome AS room_nome, r.extra -> 'nome' AS room_nome_extra,
       r.code AS room_code, r.extra -> 'id'   AS room_code_extra,
       l.nome AS loc_nome,  l.extra -> 'nome' AS loc_nome_extra,
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
#: ⚠ Il filtro sui DISMESSI, e dove sta scritto perché.
#:
#: `NOT IN ('dismesso')` con il default applicato: un dispositivo senza `stato` è
#: `attivo`, e senza `nullif` una stringa vuota resterebbe vuota — diversa da
#: `'dismesso'`, quindi per caso la risposta giusta, e per il motivo sbagliato.
#:
#: La condizione la decide `domain.NOTIFY_INELIGIBLE_STATES`, non questa stringa: un
#: test pretende che l'elenco in SQL e quello del contratto combacino, così aggiungere
#: uno stato inidoneo al contratto e dimenticarlo qui diventa rosso.
#:
#: ⚠ NESSUN filtro sulla PRESENZA, e non è una dimenticanza. Un apparato `rimosso` ha
#: la garanzia che scade comunque: è a magazzino, è stato spostato in un altro sito,
#: o è in riparazione — e chi rinnova il contratto ha bisogno di saperlo. La presenza
#: decide l'occupazione fisica dello spazio, che è la domanda della vista Capacità.
_NOT_DISMESSO = ("coalesce(nullif(d.stato, ''), '{}') NOT IN ({})".format(
    domain.DEFAULT_STATO,
    ", ".join(f"'{s}'" for s in domain.NOTIFY_INELIGIBLE_STATES)))

_CANDIDATE_BRANCH = """
    SELECT '{kind}'::text AS kind, d.uid AS uid, d.{kind}_date AS expiry,
{labels}
    {tree}
     WHERE d.{kind}_date >= CAST(:today AS date)
       AND d.{kind}_date <= CAST(:until AS date)
       AND {eligible}
"""

_CANDIDATES_SQL = " UNION ALL ".join(
    _CANDIDATE_BRANCH.format(kind=kind, labels=_LABEL_COLUMNS, tree=_TREE,
                             eligible=_NOT_DISMESSO)
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
        device=_label((row.dev_name, row.dev_name_extra),
                      (row.dev_code, row.dev_code_extra)),
        rack=_label((row.rack_name, row.rack_name_extra),
                    (row.rack_code, row.rack_code_extra)),
        room=_label((row.room_nome, row.room_nome_extra),
                    (row.room_code, row.room_code_extra)),
        location=_label((row.loc_nome, row.loc_nome_extra),
                        (row.loc_code, row.loc_code_extra)))


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

    La FINESTRA è quella del contratto: `0 <= giorni <= max(warning_days)`
    (`domain.notification_due`). Si interroga solo quell'intervallo invece di leggere
    tutte le date e scartarle in Python — che è ciò che faceva la scansione del
    documento.

    L'IDONEITÀ per stato la applica la query (`_NOT_DISMESSO`), non un filtro in
    Python dopo: un dispositivo dismesso non deve nemmeno arrivare qui, e farlo
    arrivare per poi scartarlo significherebbe leggere righe che non servono e avere
    due posti in cui la regola è scritta.
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
       AND {_NOT_DISMESSO}
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

    ⚠ Dalla 2G si applica anche l'IDONEITÀ, non solo l'esistenza della data. Se un
    dispositivo è stato messo `dismesso` fra la creazione del promemoria e il
    ritentativo, la sua voce esce dal digest — come esce quella di chi ha corretto la
    garanzia. È la stessa regola: non si manda un avviso su qualcosa che nel frattempo
    ha smesso di richiederlo. Se il digest resta vuoto, `_attempt_delivery` chiude la
    consegna senza inviare, che è la conclusione giusta.
    """
    projection.require_valid_model(conn)
    if not uids:
        return {}

    out: dict = {}
    rows = conn.execute(text(_CONTEXT_SQL), {"uids": list(uids)}).all()
    for row in rows:
        label = (_label((row.dev_name, row.dev_name_extra),
                        (row.dev_code, row.dev_code_extra)),
                 _label((row.rack_name, row.rack_name_extra),
                        (row.rack_code, row.rack_code_extra)),
                 _label((row.room_nome, row.room_nome_extra),
                        (row.room_code, row.room_code_extra)),
                 _label((row.loc_nome, row.loc_nome_extra),
                        (row.loc_code, row.loc_code_extra)))
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
