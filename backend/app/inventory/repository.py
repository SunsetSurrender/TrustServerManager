"""Repository dell'inventario: scritture atomiche e serializzate.

NESSUN endpoint HTTP, nessuna autenticazione: chi è l'attore lo dichiara il
chiamante (`Actor`). Questo modulo sa di SQL e di transazioni, non di richieste.

Ordine della transazione di salvataggio — l'ordine è la sostanza, non una
preferenza (BACKEND-PLAN.md §8.11):

  1. validazione dello schema del documento e del limite di dimensione
  2. canonicalizzazione del candidato
  3. lock e caricamento della testa corrente
  4. no-op canonico: hash del candidato == hash della testa → si risponde
     changed=False, QUALUNQUE sia il baseVersion (idempotenza, §8.18)
  5. confronto con baseVersion → conflitto solo se il contenuto è diverso
  6. validazione della transizione di identità
  7. generazione degli eventi
  8. autorizzazione dell'insieme COMPLETO
  8-bis. esistenza delle foto referenziate dal candidato (§8.5)
  9. inserimento della versione, dell'audit e dei riferimenti alle foto
 10. aggiornamento della testa
 11. commit

I passi 1-2 non toccano il database: rifiutare un documento malformato non deve
prendere un lock. Dal passo 3 in avanti si è dentro una sola transazione, e se
qualsiasi cosa fallisce non sopravvive né la versione, né l'audit, né la testa.

La versione corrente è SEMPRE `inventory_head.version`, mai `MAX(version)`: la
testa è l'unica fonte di verità e l'unico punto di serializzazione.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.authz import authorize_events
from app.identity import (
    CURRENT_SCHEMA_VERSION,
    canonical_sort,
    canonicalise,
    diff_as_dicts,
    diff_documents,
    scopes_touched,
    validate_against_base,
)
from app.inventory.document import (
    MAX_DOCUMENT_BYTES,
    strip_legacy_fields,
    validate_normal_document,
)
from app.inventory.errors import (
    AlreadyBootstrappedError,
    DocumentRejectedError,
    IdentityRejectedError,
    NotAuthorizedError,
    NotBootstrappedError,
    VersionConflictError,
)
from app.photos import refs as photo_refs
from app.photos.refs import photo_ids

#: Il testo che il client allega al salvataggio è solo di comodo per la lettura
#: del registro. Va troncato: è una stringa non attendibile che finisce in una
#: colonna, e non deve poter diventare un vettore di volume.
MAX_CLIENT_HINT_CHARS = 500


@dataclass(frozen=True)
class Actor:
    """Chi sta scrivendo. Istantanea, non riferimento.

    `username` e `role` vengono copiati nella versione e nell'audit perché
    l'audit deve raccontare chi era quella persona *allora*: sopravvivere alla
    disattivazione dell'utenza (§8.6) e a un cambio di ruolo.

    `user_id` è opzionale perché l'autenticazione non esiste ancora: quando
    arriverà, sarà il collegamento alla tabella `users`.
    """

    username: str
    role: str
    user_id: Any = None
    ip: str | None = None


@dataclass(frozen=True)
class SaveResult:
    version: int
    created: bool                 # False = no-op canonico, nessuna storia scritta
    events: tuple[dict, ...] = ()
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class InventorySnapshot:
    version: int
    doc: dict


def canonical_sha256(doc: Any) -> str:
    """SHA-256 deterministico della forma canonica, **identità inclusa**.

    Canonicalizzare (default materializzati, §8.14) e ordinare le chiavi: due
    documenti che l'applicazione considera equivalenti danno lo stesso digest. È
    così che si riconosce un salvataggio a vuoto senza confrontare interi alberi.

    Gli `_uid` fanno parte del digest, e la ragione è precisa. Da quando il
    confronto di hash precede quello del `baseVersion` (§8.18), l'hash è ciò che
    decide se una richiesta è già stata soddisfatta. Se ignorasse l'identità, un
    documento che sostituisce l'`_uid` di un dispositivo lasciando invariato tutto
    il resto avrebbe lo stesso digest della testa e verrebbe accettato come
    no-op: la sostituzione di identità che §8.4 esiste per rifiutare passerebbe
    in silenzio, con un 200 e changed=False.

    L'identità è parte del significato del documento, quindi è parte del suo
    digest. Il caso «solo gli _uid sono diversi» resta contenuto diverso, e
    prosegue verso la validazione della transizione che lo rifiuta.

    NB: la verifica del seed usa un digest DIVERSO, che gli `_uid` li toglie
    (`tools/verify-seed-migration.mjs`). Là lo scopo è confrontare i dati fra
    rigenerazioni con identità casuali; qui è riconoscere una richiesta ripetuta.
    Due scopi diversi, due digest diversi, e vale la pena non confonderli.
    """
    payload = json.dumps(canonical_sort(canonicalise(doc)),
                         ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InventoryRepository:
    """Opera su una `Connection` fornita dal chiamante.

    Il chiamante possiede la transazione: così il repository si compone con altre
    scritture (per esempio l'inserimento di una foto) senza aprire transazioni
    annidate di nascosto.
    """

    def __init__(self, conn: Connection):
        self.conn = conn

    # ------------------------------------------------------------- letture

    def head_version(self) -> int | None:
        """Versione corrente dalla riga di testa. Mai `MAX(version)`."""
        row = self.conn.execute(
            text("SELECT version FROM inventory_head WHERE id IS TRUE")).first()
        return None if row is None else int(row[0])

    def get_current(self) -> InventorySnapshot:
        row = self.conn.execute(text("""
            SELECT v.version, v.doc
              FROM inventory_head h
              JOIN inventory_versions v ON v.version = h.version
             WHERE h.id IS TRUE
        """)).first()
        if row is None:
            raise NotBootstrappedError(
                "nessuna versione in testa: eseguire prima il bootstrap")
        return InventorySnapshot(version=int(row[0]), doc=row[1])

    def get_version(self, version: int) -> InventorySnapshot:
        row = self.conn.execute(
            text("SELECT version, doc FROM inventory_versions WHERE version = :v"),
            {"v": version}).first()
        if row is None:
            raise NotBootstrappedError(f"versione {version} inesistente")
        return InventorySnapshot(version=int(row[0]), doc=row[1])

    def list_versions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(text("""
            SELECT v.version, v.created_at, v.actor_username, v.actor_role,
                   a.scopes, a.client_hint
              FROM inventory_versions v
              LEFT JOIN audit a ON a.inventory_version = v.version
             ORDER BY v.version DESC
             LIMIT :limit OFFSET :offset
        """), {"limit": limit, "offset": offset}).mappings().all()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- bootstrap

    def bootstrap(self, doc: dict, actor: Actor, *,
                  from_legacy: bool = False) -> SaveResult:
        """Inizializzazione una-volta-sola.

        Percorso dedicato e separato dal salvataggio normale, per la stessa
        ragione per cui il backfill degli `_uid` è uno script a parte (§8.4): la
        differenza fra «popolo un database vuoto» e «accetto una scrittura» non
        va affidata a un booleano che qualcuno può passare per sbaglio in una
        richiesta.

        `from_legacy=True` consente di CONSUMARE E TOGLIERE le radici estratte o
        legacy (utenti, registro, notifiche, smtp, versione): è l'unico posto
        dove è previsto, e resta comunque impossibile persisterle.
        """
        if self.head_version() is not None:
            raise AlreadyBootstrappedError(
                "l'inventario è già inizializzato: il bootstrap è una-volta-sola")

        removed: list[str] = []
        if from_legacy and isinstance(doc, dict):
            doc, removed = strip_legacy_fields(doc)

        errors = validate_normal_document(doc)
        if errors:
            raise DocumentRejectedError(
                f"documento di bootstrap rifiutato ({len(errors)} problemi)",
                [e.as_dict() for e in errors])

        canonical = canonicalise(doc)
        # Anche il bootstrap dichiara i suoi riferimenti alle foto. Il seed non ne
        # ha (le foto legacy erano dataURL e lo schema congelato le rifiuta, §8.16),
        # ma non registrarli qui vorrebbe dire che una versione 1 con foto sarebbe
        # invisibile alla GC — cioè byte cancellabili al primo giro.
        refs = photo_ids(canonical)
        photo_refs.require_existing(self.conn, refs)
        version = self._insert_version(canonical, actor)
        self._insert_audit(version, actor, action="bootstrap", scopes=["structure"],
                           events=[], client_hint=None)
        photo_refs.record(self.conn, version, refs)
        self._insert_head(version)
        return SaveResult(version=version, created=True,
                          events=(), scopes=("structure",))

    # ------------------------------------------------------------ scrittura

    def save(self, base_version: int, doc: Any, actor: Actor,
             client_hint: str | None = None) -> SaveResult:
        """Salvataggio normale. Vedi l'ordine in testa al modulo."""

        # --- 1. schema e dimensione, prima di toccare il database ---
        pre = validate_normal_document(doc, max_bytes=MAX_DOCUMENT_BYTES)
        if pre:
            raise DocumentRejectedError(
                f"documento rifiutato ({len(pre)} problemi)", [e.as_dict() for e in pre])

        # --- 2. canonicalizzazione del candidato ---
        candidate = canonicalise(doc)

        # --- 3. lock e caricamento della testa ---
        #
        # DUE query, non una con JOIN, e la ragione è sottile e importante.
        #
        # Sotto READ COMMITTED, quando un `SELECT ... FOR UPDATE` deve aspettare
        # una transazione concorrente, al risveglio Postgres rivaluta la
        # qualificazione della riga BLOCCATA sulla sua versione aggiornata
        # (EvalPlanQual) — ma le altre tabelle del join restano lette con lo
        # snapshot originale. Con `JOIN inventory_versions` il perdente non
        # vedrebbe la versione appena inserita dal vincitore, il join non
        # troverebbe nulla e otterrebbe «non inizializzato» invece di un
        # conflitto pulito.
        #
        # Bloccando la sola riga di testa e leggendo il documento con una query
        # separata, la seconda parte da uno snapshot di comando nuovo e vede
        # quello che il vincitore ha committato.
        locked = self.conn.execute(text("""
            SELECT version FROM inventory_head WHERE id IS TRUE FOR UPDATE
        """)).first()
        if locked is None:
            raise NotBootstrappedError(
                "nessuna versione in testa: eseguire prima il bootstrap")
        current_version = int(locked[0])

        current_row = self.conn.execute(
            text("SELECT doc FROM inventory_versions WHERE version = :v"),
            {"v": current_version}).first()
        if current_row is None:      # impossibile: c'è una FK dalla testa
            raise NotBootstrappedError(
                f"la testa punta alla versione {current_version}, che non esiste")
        current_doc = current_row[0]

        # Lo schema del candidato deve combaciare con quello in testa: un
        # salvataggio non fa evolvere lo schema (§8.13).
        schema_errors = validate_normal_document(
            doc, current_schema_version=(current_doc or {}).get("schemaVersion"))
        if schema_errors:
            raise DocumentRejectedError(
                f"documento rifiutato ({len(schema_errors)} problemi)",
                [e.as_dict() for e in schema_errors])

        current_canonical = canonicalise(current_doc)
        current_sha = canonical_sha256(current_canonical)
        candidate_sha = canonical_sha256(candidate)

        # --- 4. no-op canonico, PRIMA del confronto con baseVersion ---
        #
        # L'ordine conta e non è un dettaglio (§8.18). Se il contenuto in testa è
        # già identico a quello che il client vuole scrivere, la richiesta è già
        # stata soddisfatta: si risponde con la versione corrente e changed=False,
        # qualunque sia il `baseVersion` dichiarato.
        #
        # È ciò che rende il PUT idempotente nel caso reale: il commit è andato a
        # buon fine ma la risposta si è persa (rete, timeout, tab chiusa), il
        # client riprova con il vecchio baseVersion e con lo stesso documento.
        # Confrontando prima il baseVersion gli si restituirebbe un conflitto per
        # una scrittura che è già la sua, e l'utente vedrebbe «modificato da un
        # altro utente» a fronte della propria modifica andata a buon fine.
        #
        # Un `baseVersion` superato con contenuto DIVERSO resta un conflitto: lì
        # il client sta davvero sovrascrivendo il lavoro di qualcun altro.
        if candidate_sha == current_sha:
            return SaveResult(version=current_version, created=False)

        # --- 5. confronto con baseVersion ---
        # Con il lock preso, questo confronto è race-free: chi arriva secondo ha
        # aspettato qui e rilegge la testa aggiornata, quindi ottiene un conflitto
        # corretto invece di una violazione di chiave primaria travestita.
        if int(base_version) != current_version:
            raise VersionConflictError(base_version, current_version, current_sha)

        # --- 6. transizione di identità ---
        id_errors = validate_against_base(current_canonical, candidate)
        if id_errors:
            raise IdentityRejectedError(
                f"transizione di identità rifiutata ({len(id_errors)} problemi)",
                [e.as_dict() for e in id_errors])

        # --- 7. eventi di dominio ---
        events = diff_documents(current_canonical, candidate)
        event_dicts = diff_as_dicts(current_canonical, candidate)

        # --- 8. autorizzazione dell'insieme COMPLETO ---
        decision = authorize_events(actor.role, events)
        if not decision.allowed:
            raise NotAuthorizedError(
                f"modifica non autorizzata per il ruolo '{actor.role}' "
                f"({len(decision.violations)} violazioni)",
                [v.as_dict() for v in decision.violations])

        # --- 8-bis. le foto referenziate devono esistere ---
        #
        # Prima di scrivere qualsiasi cosa. Un documento che punta a una foto
        # inesistente non è un documento a cui manca un pezzo: è un documento non
        # valido, e accettarlo produrrebbe una versione che mostra un riquadro
        # rotto per sempre. Il caricamento del binario e il salvataggio del rack
        # sono due richieste, e questo è il controllo che le lega (§8.5).
        refs = photo_ids(candidate)
        photo_refs.require_existing(self.conn, refs)

        # --- 9. versione, audit e riferimenti, nella stessa transazione ---
        scopes = scopes_touched(events)
        version = self._insert_version(candidate, actor, sha=candidate_sha)
        self._insert_audit(version, actor, action="inventory.save", scopes=scopes,
                           events=event_dicts, client_hint=client_hint)
        # I riferimenti stanno o cadono con la versione che li dichiara: scritti
        # senza di essa autorizzerebbero la GC a cancellare byte ancora usati,
        # oppure a non liberarli mai.
        photo_refs.record(self.conn, version, refs)

        # --- 10. testa ---
        self._update_head(version)

        # --- 11. il commit è del chiamante, che possiede la transazione ---
        return SaveResult(version=version, created=True,
                          events=tuple(event_dicts), scopes=tuple(scopes))

    # --------------------------------------------------------------- interni
    #
    # Metodi separati anche per poter iniettare guasti nei test: sostituendo
    # _insert_audit o _update_head con una funzione che solleva si verifica che
    # non sopravviva nessuno stato parziale.

    def _insert_version(self, canonical_doc: dict, actor: Actor,
                        sha: str | None = None) -> int:
        row = self.conn.execute(text("""
            INSERT INTO inventory_versions
                   (doc, canonical_sha256, actor_username, actor_role, actor_user_id)
            VALUES (:doc, :sha, :username, :role, :user_id)
         RETURNING version
        """), {
            "doc": json.dumps(canonical_doc, ensure_ascii=False),
            "sha": sha or canonical_sha256(canonical_doc),
            "username": actor.username,
            "role": actor.role,
            "user_id": actor.user_id,
        }).first()
        return int(row[0])

    def _insert_audit(self, version: int, actor: Actor, *, action: str,
                      scopes: list[str], events: list[dict],
                      client_hint: str | None) -> None:
        from app.audit.sanitize import sanitize

        self.conn.execute(text("""
            INSERT INTO audit (actor_user_id, actor_username, actor_role, ip,
                               inventory_version, action, result, scopes, events,
                               client_hint)
            VALUES (:user_id, :username, :role, :ip,
                    :version, :action, 'success', :scopes, :events, :hint)
        """), {
            "user_id": actor.user_id,
            "username": actor.username,
            "role": actor.role,
            "ip": actor.ip,
            "version": version,
            "action": action,
            "scopes": scopes,
            # Ripulitura in scrittura (§8.36): gli eventi di dominio non
            # contengono segreti, ma il registro è alimentato da più produttori e
            # la difesa non deve dipendere dal fatto che ognuno si ricordi.
            "events": json.dumps(sanitize(events), ensure_ascii=False),
            "hint": _clip(client_hint),
        })

    def _insert_head(self, version: int) -> None:
        self.conn.execute(
            text("INSERT INTO inventory_head (id, version) VALUES (TRUE, :v)"),
            {"v": version})

    def _update_head(self, version: int) -> None:
        self.conn.execute(
            text("UPDATE inventory_head SET version = :v, updated_at = now() "
                 "WHERE id IS TRUE"),
            {"v": version})


def _clip(hint: str | None) -> str | None:
    """Il testo del client è solo di visualizzazione e non è attendibile: si
    tronca. Non descrive ciò che è cambiato — quello lo dice `audit.events`,
    calcolato dal server (§8.9)."""
    if hint is None:
        return None
    hint = str(hint)
    if len(hint) <= MAX_CLIENT_HINT_CHARS:
        return hint
    return hint[:MAX_CLIENT_HINT_CHARS - 1] + "…"
