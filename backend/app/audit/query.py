"""Lettura del registro di audit: cursore, filtri, ordinamento.

Puro rispetto a HTTP: prende una connessione e restituisce dati. Le rotte non
sanno di SQL e questo modulo non sa di richieste.

Ordinamento: `ts DESC, id DESC`. Il timestamp da solo non basta — più eventi
possono condividere lo stesso istante, e con un cursore basato sul solo `ts` le
righe a cavallo fra due pagine verrebbero saltate o ripetute. Il predicato usa
la stessa coppia, come tupla, così è coerente con l'ordinamento per costruzione.

Riferimento: BACKEND-PLAN.md §8.36.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.audit.sanitize import sanitize

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

#: Versione del cursore. È dentro il valore per poterne cambiare il formato senza
#: che un client con un cursore vecchio riceva risultati sbagliati in silenzio.
CURSOR_VERSION = "v1"

INVALID_CURSOR = "invalid_cursor"
INVALID_PAGE_SIZE = "invalid_page_size"
INVALID_FILTER = "invalid_filter"

RESULTS = ("success", "failure", "denied")


class AuditQueryError(Exception):
    """Parametri non validi. `code` è stabile e va sul filo."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Cursor:
    ts: datetime
    id: int

    def encode(self) -> str:
        """base64url di `v1|<iso-utc>|<id>`. Opaco al confine HTTP: il client non
        deve costruirlo né interpretarlo, e infatti non ne ha bisogno."""
        raw = f"{CURSOR_VERSION}|{self.ts.astimezone(timezone.utc).isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @staticmethod
    def decode(value: str) -> "Cursor":
        """Validazione severa: qualunque cosa non torni è un cursore non valido.

        Un cursore manomesso non deve produrre una pagina «quasi giusta»: deve
        produrre un errore riconoscibile, altrimenti un client che sbaglia a
        costruirlo salta righe senza accorgersene.
        """
        # NB: la stringa vuota non arriva qui — la rotta la tratta come «nessun
        # cursore», che è ciò che intende un client che invia il parametro vuoto.
        if not isinstance(value, str) or not value or len(value) > 512:
            raise AuditQueryError(INVALID_CURSOR, "cursore assente o troppo lungo")
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise AuditQueryError(INVALID_CURSOR, "cursore non decodificabile") from None

        parts = raw.split("|")
        if len(parts) != 3:
            raise AuditQueryError(INVALID_CURSOR, "cursore malformato")
        version, ts_raw, id_raw = parts
        if version != CURSOR_VERSION:
            raise AuditQueryError(
                INVALID_CURSOR, f"versione di cursore non supportata: {version!r}")
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            raise AuditQueryError(INVALID_CURSOR, "timestamp del cursore non valido") from None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            ident = int(id_raw)
        except ValueError:
            raise AuditQueryError(INVALID_CURSOR, "id del cursore non valido") from None
        if ident < 0:
            raise AuditQueryError(INVALID_CURSOR, "id del cursore negativo")
        return Cursor(ts=ts, id=ident)


@dataclass(frozen=True)
class Filters:
    frm: datetime | None = None
    to: datetime | None = None
    username: str | None = None
    event: str | None = None
    result: str | None = None


def parse_page_size(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_PAGE_SIZE
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise AuditQueryError(INVALID_PAGE_SIZE,
                              f"pageSize non è un intero: {value!r}") from None
    if size <= 0:
        raise AuditQueryError(INVALID_PAGE_SIZE, "pageSize deve essere positivo")
    if size > MAX_PAGE_SIZE:
        raise AuditQueryError(
            INVALID_PAGE_SIZE,
            f"pageSize {size} oltre il massimo di {MAX_PAGE_SIZE}")
    return size


def parse_instant(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise AuditQueryError(
                INVALID_FILTER, f"{field} non è una data ISO-8601 valida") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_filters(*, frm=None, to=None, username=None, event=None,
                  result=None) -> Filters:
    """Filtri TIPIZZATI. Niente espressioni libere, niente JSON arbitrario: la
    superficie è chiusa, e ciò che non è previsto viene rifiutato."""
    f = parse_instant(frm, "from")
    t = parse_instant(to, "to")
    if f and t and f > t:
        raise AuditQueryError(INVALID_FILTER, "'from' è successivo a 'to'")

    if username is not None:
        username = str(username).strip()
        if len(username) > 200:
            raise AuditQueryError(INVALID_FILTER, "username troppo lungo")
        username = username or None

    if event is not None:
        event = str(event).strip()
        if len(event) > 100:
            raise AuditQueryError(INVALID_FILTER, "event troppo lungo")
        # Categoria o azione completa: `auth`, `auth.login`, `inventory.save`.
        # Si ammettono solo i caratteri che le azioni usano davvero, così il
        # valore non può diventare altro.
        if event and not all(c.isalnum() or c in "._-" for c in event):
            raise AuditQueryError(INVALID_FILTER,
                                  "event contiene caratteri non ammessi")
        event = event or None

    if result is not None:
        result = str(result).strip().lower()
        if result and result not in RESULTS:
            raise AuditQueryError(
                INVALID_FILTER,
                f"result non valido: atteso uno fra {', '.join(RESULTS)}")
        result = result or None

    return Filters(frm=f, to=t, username=username, event=event, result=result)


def _row_to_item(row) -> dict:
    return {
        "id": int(row["id"]),
        "ts": row["ts"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "actor": {
            # Istantanea storica: chi era quella persona ALLORA (§8.30). Un utente
            # rinominato, retrocesso o disattivato resta attribuibile.
            "userId": str(row["actor_user_id"]) if row["actor_user_id"] else None,
            "username": row["actor_username"],
            "role": row["actor_role"],
        },
        "event": row["action"],
        "result": row["result"],
        "scopes": list(row["scopes"] or []),
        "inventoryVersion": row["inventory_version"],
        # Testo del client: NON attendibile, e non è la descrizione autorevole
        # dell'evento. Quella è `event` più `detail`, che li calcola il server.
        "clientHint": row["client_hint"],
        # Seconda ripulitura, in serializzazione: vedi sanitize.py.
        "detail": sanitize(row["events"]),
        "ip": str(row["ip"]) if row["ip"] else None,
    }


def query_audit(conn: Connection, *, filters: Filters | None = None,
                cursor: Cursor | None = None,
                page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Una pagina di registro, dalla più recente."""
    filters = filters or Filters()
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": page_size + 1}

    if cursor is not None:
        # Confronto fra TUPLE: è la forma che corrisponde esattamente a
        # `ORDER BY ts DESC, id DESC`, e l'unica che non salta né ripete righe
        # quando più eventi condividono lo stesso istante.
        where.append("(a.ts, a.id) < (:cur_ts, :cur_id)")
        params["cur_ts"] = cursor.ts
        params["cur_id"] = cursor.id

    if filters.frm is not None:
        where.append("a.ts >= :frm")
        params["frm"] = filters.frm
    if filters.to is not None:
        where.append("a.ts <= :to")
        params["to"] = filters.to
    if filters.username:
        where.append("a.actor_username = :username")
        params["username"] = filters.username
    if filters.event:
        # Categoria o azione esatta: `auth` prende `auth.login.success` e simili.
        where.append("(a.action = :event OR a.action LIKE :event_prefix)")
        params["event"] = filters.event
        params["event_prefix"] = filters.event + ".%"
    if filters.result:
        where.append("a.result = :result")
        params["result"] = filters.result

    sql = f"""
        SELECT a.id, a.ts, a.actor_user_id, a.actor_username, a.actor_role,
               a.ip, a.inventory_version, a.action, a.result, a.scopes,
               a.events, a.client_hint
          FROM audit a
         WHERE {' AND '.join(where)}
         ORDER BY a.ts DESC, a.id DESC
         LIMIT :limit
    """
    rows = conn.execute(text(sql), params).mappings().all()

    # Si chiede una riga in più del necessario: la sua esistenza dice che c'è
    # un'altra pagina, senza un secondo COUNT che su una tabella che cresce
    # costerebbe quanto la query stessa.
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    items = [_row_to_item(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = Cursor(ts=last["ts"], id=int(last["id"])).encode()

    return {"items": items, "nextCursor": next_cursor}
