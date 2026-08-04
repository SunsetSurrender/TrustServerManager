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

FORBIDDEN_FOR_ROLE = "forbidden_for_role"
UNKNOWN_ROLE = "unknown_role"
ROLLBACK_FORBIDDEN = "rollback_forbidden"


@dataclass(frozen=True)
class Violation:
    """Violazione singola, in forma leggibile dalla macchina.

    `required_role` è il ruolo minimo che avrebbe permesso quell'evento: serve
    al client per dire «serve un amministratore» invece di un generico rifiuto.
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
    """Ruolo minimo per un evento. `admin` è il predefinito: si concede solo ciò
    che è esplicitamente previsto, così un tipo di entità nuovo nasce ristretto
    invece di essere permesso per distrazione."""
    if entity == "device" and event in EDIT_DEVICE_EVENTS:
        return "edit"
    return "admin"


def _permits(role: str, entity: str, event: str) -> bool:
    if role == "admin":
        return True
    if role == "edit":
        return entity == "device" and event in EDIT_DEVICE_EVENTS
    return False        # view: nessuna scrittura sull'inventario


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
