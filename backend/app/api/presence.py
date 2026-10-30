"""Chi è collegato, e chi sta modificando quale rack (0.5.0).

    POST   /api/presence             segnale del browser, ogni ~30 s
    POST   /api/locks/{rack_uid}/release   sblocco forzato (solo amministratori)

Nessun DELETE, come in tutta l'API: sbloccare vuol dire far scadere il blocco.

Il segnale dice «sono qui» e, facoltativamente, «sto modificando questo rack».
La risposta riporta chi è collegato e quali rack sono bloccati, così una sola
chiamata periodica tiene aggiornata l'interfaccia.

Due scelte che contano:

  - il segnale NON aggiorna `last_seen_at` (vedi `resolve_session(touch=False)`):
    una scheda lasciata aperta deve poter scadere per inattività;
  - un blocco SCADE da solo (`LOCK_TTL`) se non viene rinnovato. Il browser chiuso,
    il portatile in sospensione o la rete persa liberano il rack entro un paio di
    minuti, senza che nessuno debba intervenire. Il controllo che conta è nel
    salvataggio (`InventoryRepository._require_unlocked`): l'interfaccia mostra il
    blocco, il server lo fa rispettare.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.auth.audit import record_auth_event
from app.api.deps import NO_STORE, SESSION_COOKIE, get_connection, require_admin
from app.auth.service import NotAuthenticated, SessionIdle, _token_hash, resolve_session
from app.inventory import Actor

router = APIRouter()

# I rack si riportano per `uid`, senza il codice: il client lo ha già nel documento
# che sta guardando, e la proiezione relazionale ha lettori dichiarati
# (tools/storage-config-test.py) — questo modulo non è fra quelli.

LOCK_TTL_SECONDS = 90
ONLINE_WINDOW_SECONDS = 120
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

LOCK_FORCED = "inventory.lock.released_by_admin"


class PresenceIn(BaseModel):
    rackUid: str | None = None


def _display():
    return ("coalesce(nullif(trim(concat_ws(' ', u.nome, u.cognome)), ''), u.username)")


def state(conn: Connection, *, user_id=None, session_id=None) -> dict:
    online = conn.execute(text(f"""
        SELECT DISTINCT ON (u.id) u.username, {_display()}, s.presence_rack, NULL,
               extract(epoch FROM now() - s.presence_at)::int, u.id::text
          FROM sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.revoked_at IS NULL AND s.expires_at > now() AND u.disabled_at IS NULL
           AND s.presence_at > now() - make_interval(secs => :w)
         ORDER BY u.id, s.presence_at DESC
    """), {"w": ONLINE_WINDOW_SECONDS}).all()
    locks = conn.execute(text(f"""
        SELECT l.rack_uid::text, u.username, {_display()}, NULL,
               extract(epoch FROM l.expires_at - now())::int, u.id::text,
               l.session_id::text
          FROM edit_locks l
          JOIN users u ON u.id = l.user_id
         WHERE l.expires_at > now()
         ORDER BY l.acquired_at
    """)).all()
    me = str(user_id) if user_id is not None else None
    return {
        "online": sorted(({"username": r[0], "display": r[1],
                           "rackUid": r[2], "secondsAgo": max(0, r[4] or 0),
                           "you": r[5] == me} for r in online),
                         key=lambda x: (not x["you"], x["display"].lower())),
        "locks": [{"rackUid": r[0], "username": r[1], "display": r[2],
                   "expiresIn": r[4], "mine": r[5] == me,
                   "thisSession": session_id is not None and r[6] == str(session_id)}
                  for r in locks],
    }


@router.post("/presence", summary="Segnale di presenza e blocco del rack in modifica")
def presence(payload: PresenceIn, request: Request, response: Response,
             conn: Connection = Depends(get_connection)) -> dict:
    response.headers.update(NO_STORE)
    token = request.cookies.get(SESSION_COOKIE)
    try:
        user = resolve_session(conn, token, touch=False)
    except (NotAuthenticated, SessionIdle) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"code": exc.code, "message": "autenticazione richiesta"},
                            headers=NO_STORE) from None
    sid = conn.execute(text("SELECT id::text FROM sessions WHERE token_hash = :th"),
                       {"th": _token_hash(token)}).scalar()

    rack = payload.rackUid if payload.rackUid and _UUID.match(payload.rackUid) else None
    can_edit = user.role in ("edit", "admin") and not user.must_change_pw
    conn.execute(text("""
        UPDATE sessions SET presence_at = now(), presence_rack = :r WHERE id::text = :sid
    """), {"r": rack, "sid": sid})

    # Si liberano i blocchi di QUESTA sessione su altri rack: chi passa da R01 a
    # R02 non deve tenere occupato R01 fino alla scadenza.
    conn.execute(text("""
        UPDATE edit_locks SET expires_at = now()
         WHERE session_id::text = :sid AND expires_at > now()
           AND (CAST(:r AS text) IS NULL OR rack_uid::text <> :r)
    """), {"sid": sid, "r": rack if can_edit else None})

    lock = None
    if rack and can_edit:
        got = conn.execute(text("""
            INSERT INTO edit_locks (rack_uid, user_id, session_id, acquired_at, expires_at)
            VALUES (CAST(:r AS uuid), :u, CAST(:sid AS uuid), now(),
                    now() + make_interval(secs => :ttl))
            ON CONFLICT (rack_uid) DO UPDATE
               SET user_id = EXCLUDED.user_id, session_id = EXCLUDED.session_id,
                   acquired_at = CASE WHEN edit_locks.user_id = EXCLUDED.user_id
                                       AND edit_locks.expires_at > now()
                                      THEN edit_locks.acquired_at ELSE now() END,
                   expires_at = EXCLUDED.expires_at
             WHERE edit_locks.user_id = EXCLUDED.user_id OR edit_locks.expires_at <= now()
            RETURNING rack_uid
        """), {"r": rack, "u": user.id, "sid": sid, "ttl": LOCK_TTL_SECONDS}).first()
        if got:
            lock = {"rackUid": rack, "granted": True, "heldBy": None}
        else:
            chi = conn.execute(text(f"""
                SELECT {_display()} FROM edit_locks l JOIN users u ON u.id = l.user_id
                 WHERE l.rack_uid::text = :r
            """), {"r": rack}).scalar()
            lock = {"rackUid": rack, "granted": False, "heldBy": chi}

    out = state(conn, user_id=user.id, session_id=sid)
    out["lock"] = lock
    out["lockTtlSeconds"] = LOCK_TTL_SECONDS
    return out


@router.post("/locks/{rack_uid}/release", summary="Sblocco forzato di un rack (amministratori)")
def force_unlock(rack_uid: str, response: Response,
                 conn: Connection = Depends(get_connection),
                 actor: Actor = Depends(require_admin)) -> dict:
    response.headers.update(NO_STORE)
    if not _UUID.match(rack_uid):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"code": "lock_not_found", "message": "blocco inesistente"},
                            headers=NO_STORE)
    row = conn.execute(text("""
        UPDATE edit_locks l SET expires_at = now()
          FROM users u
         WHERE l.rack_uid::text = :r AND l.expires_at > now() AND u.id = l.user_id
        RETURNING u.username
    """), {"r": rack_uid}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail={"code": "lock_not_found",
                                    "message": "il rack non è bloccato"},
                            headers=NO_STORE)
    record_auth_event(conn, LOCK_FORCED, username=actor.username, user_id=actor.user_id,
                      role=actor.role, ip=actor.ip,
                      detail={"rackUid": rack_uid, "heldBy": row[0]})
    return {"released": True, "heldBy": row[0]}
