"""Politica di autorizzazione sugli eventi di dominio.

PURA: consuma la lista di eventi prodotta dal motore di diff (§8.10) e decide.
Nessun FastAPI, nessun database, nessuna sessione: chi è l'utente e quale ruolo
ha lo stabilisce il chiamante.

Perché lavorare sugli eventi e non sul documento: il `PUT` porta l'intero
documento, quindi «questo utente può scrivere?» non basta. Il permesso dipende da
*cosa* è cambiato, e cosa è cambiato lo dice solo un diff che conosce l'identità
delle entità — spostare un dispositivo fra due rack è un'operazione sui
dispositivi, non sulla struttura, anche se tocca due sottoalberi di rack.

Regola di fondo: **tutto o niente**. Si esamina l'insieme *completo* degli
eventi e se anche uno solo è vietato l'intera modifica viene respinta. Applicare
la parte consentita significherebbe scrivere un documento che l'utente non ha
composto, e lasciare l'inventario in uno stato che nessuno ha chiesto.

Riferimento: BACKEND-PLAN.md §8.3, §8.15.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

ROLES = ("view", "edit", "admin")

#: Eventi sui dispositivi che il ruolo `edit` può produrre.
#: `reorder` NON è compreso: riordinare è un'operazione sulla disposizione, che
#: il README assegna alla struttura. Un operatore sposta un dispositivo (`move`),
#: non riordina la collezione.
EDIT_DEVICE_EVENTS = frozenset({"add", "update", "rename", "move", "delete"})

#: Vocabolario CHIUSO degli eventi. Serve a distinguere due situazioni diverse
#: che non vanno confuse:
#:
#:   - evento NOTO ma non concesso al ruolo   → forbidden_for_role, e un admin passa
#:   - evento NON SUPPORTATO                  → unsupported_domain_event, e NESSUNO passa
#:
#: Trattare il secondo caso come «serve admin» sarebbe sbagliato in modo
#: pericoloso: un evento che il server non sa interpretare non diventa
#: interpretabile perché chi lo invia ha più privilegi. Non è una questione di
#: permessi, è una questione di significato.
KNOWN_ENTITIES = frozenset({"location", "room", "rack", "device", "manual", "settings"})
KNOWN_EVENTS = frozenset({"add", "delete", "update", "rename", "move", "reorder"})

FORBIDDEN_FOR_ROLE = "forbidden_for_role"
UNKNOWN_ROLE = "unknown_role"
ROLLBACK_FORBIDDEN = "rollback_forbidden"
UNSUPPORTED_DOMAIN_EVENT = "unsupported_domain_event"


@dataclass(frozen=True)
class Violation:
    """Violazione singola, in forma leggibile dalla macchina.

    `required_role` è il ruolo minimo che avrebbe permesso quell'evento: serve
    al client per dire «serve un amministratore» invece di un generico rifiuto.

    Per `unsupported_domain_event` è la **stringa vuota**, e vuol dire una cosa
    precisa: nessun ruolo rende accettabile quell'evento. Chiedere privilegi più
    alti non aiuta, perché il problema non è il permesso ma il significato.
    """

    code: str
    role: str
    entity: str
    event: str
    scope: str
    required_role: str
    uid: Any = None
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "role": self.role,
            "entity": self.entity,
            "event": self.event,
            "scope": self.scope,
            "requiredRole": self.required_role,
            "uid": self.uid,
            "message": self.message,
        }


@dataclass(frozen=True)
class Decision:
    allowed: bool
    violations: tuple[Violation, ...] = ()

    def as_dict(self) -> dict:
        return {"allowed": self.allowed,
                "violations": [v.as_dict() for v in self.violations]}


def _required_role(entity: str, event: str) -> str:
    """Ruolo minimo per un evento NOTO. `admin` è il predefinito: fra gli eventi
    supportati si concede solo ciò che è esplicitamente previsto, così un tipo di
    entità nuovo ma riconosciuto nasce ristretto invece di essere permesso per
    distrazione."""
    if entity == "device" and event in EDIT_DEVICE_EVENTS:
        return "edit"
    return "admin"


def _permits(role: str, entity: str, event: str) -> bool:
    if role == "admin":
        return True
    if role == "edit":
        return entity == "device" and event in EDIT_DEVICE_EVENTS
    return False        # view: nessuna scrittura sull'inventario


def _unsupported_reason(ev: Any, entity: Any, event: Any) -> str | None:
    """Perché questo evento non è interpretabile? None se lo è."""
    if not isinstance(ev, dict) and not hasattr(ev, "event"):
        return f"evento non è né un dizionario né un oggetto evento: {type(ev).__name__}"
    if not isinstance(entity, str) or not entity:
        return f"campo 'entity' assente o non stringa: {entity!r}"
    if not isinstance(event, str) or not event:
        return f"campo 'event' assente o non stringa: {event!r}"
    if entity not in KNOWN_ENTITIES:
        return (f"tipo di entità non supportato: {entity!r} "
                f"(noti: {', '.join(sorted(KNOWN_ENTITIES))})")
    if event not in KNOWN_EVENTS:
        return (f"tipo di evento non supportato: {event!r} "
                f"(noti: {', '.join(sorted(KNOWN_EVENTS))})")
    return None


def authorize_events(role: str, events: Iterable[Any]) -> Decision:
    """Decide su un insieme di eventi.

    `events` accetta sia oggetti `Event` sia dizionari (la forma `as_dict()`),
    così la politica è utilizzabile anche su eventi deserializzati.

    Un insieme **vuoto** è consentito a qualunque ruolo, `view` compreso: non è
    una scrittura. Un `PUT` che non produce eventi non deve nemmeno creare una
    versione (§8.10), quindi non c'è niente da autorizzare.
    """
    if role not in ROLES:
        return Decision(False, (Violation(
            UNKNOWN_ROLE, role, "", "", "", "admin",
            message=f"ruolo non riconosciuto: {role!r}"),))

    violations: list[Violation] = []
    for ev in events:
        entity, event, scope, uid = _fields(ev)

        # Prima: è un evento che sappiamo interpretare? Se no, nessun ruolo lo
        # rende accettabile — nemmeno admin.
        reason = _unsupported_reason(ev, entity, event)
        if reason is not None:
            violations.append(Violation(
                UNSUPPORTED_DOMAIN_EVENT, role,
                entity if isinstance(entity, str) else "",
                event if isinstance(event, str) else "",
                scope if isinstance(scope, str) else "",
                required_role="", uid=uid,
                message=f"evento di dominio non supportato: {reason}"))
            continue

        if _permits(role, entity, event):
            continue
        need = _required_role(entity, event)
        violations.append(Violation(
            FORBIDDEN_FOR_ROLE, role, entity, event, scope, need, uid,
            f"il ruolo '{role}' non può eseguire '{event}' su '{entity}' "
            f"(ambito {scope}): serve '{need}'"))

    # Ordine deterministico: le violazioni finiscono in una risposta HTTP e in
    # un log, e devono essere confrontabili fra esecuzioni.
    violations.sort(key=lambda v: (v.entity, v.event, str(v.uid or "")))
    return Decision(not violations, tuple(violations))


def authorize_rollback(role: str) -> Decision:
    """Il rollback resta solo per gli amministratori.

    Non è un insieme di eventi ma la sostituzione in blocco dell'inventario con
    una versione precedente: non si può autorizzare per ambito, perché tocca
    tutto. Vale come operazione a sé.
    """
    if role not in ROLES:
        return Decision(False, (Violation(
            UNKNOWN_ROLE, role, "", "", "", "admin",
            message=f"ruolo non riconosciuto: {role!r}"),))
    if role == "admin":
        return Decision(True)
    return Decision(False, (Violation(
        ROLLBACK_FORBIDDEN, role, "inventory", "rollback", "structure", "admin",
        message=f"il ruolo '{role}' non può ripristinare una versione precedente: "
                "serve 'admin'"),))


def _fields(ev: Any) -> tuple[str, str, str, Any]:
    if isinstance(ev, dict):
        return (ev.get("entity", ""), ev.get("event", ""),
                ev.get("scope", ""), ev.get("uid"))
    return (getattr(ev, "entity", ""), getattr(ev, "event", ""),
            getattr(ev, "scope", ""), getattr(ev, "uid", None))
