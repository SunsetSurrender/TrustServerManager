#!/usr/bin/env python3
"""Smoke test dello scheletro backend.

Verifica sullo stack in esecuzione, senza dipendenze esterne (solo stdlib, così gira
anche in rete chiusa):

  1. db e api sono `healthy` secondo gli healthcheck di Compose
  2. la migrazione Alembic è arrivata a head (alembic_version popolata)
  3. /api/health risponde 200 (liveness)
  4. /api/ready risponde 200 (readiness: l'API raggiunge il DB)
  5. Postgres NON è pubblicato sull'host — quattro controlli indipendenti
  6. i container applicativi girano non-root
  7. il secret è montato in sola lettura, il filesystem è read-only

Nota sul controllo 5, che è il motivo per cui questo file esiste
----------------------------------------------------------------
La verifica NON può essere «una connessione a 127.0.0.1:5432 dall'host fallisce».
Su una macchina di sviluppo quella porta può essere occupata da un altro Postgres
— altro progetto, altro container, servizio dell'host — e in quel caso il test
fallirebbe pur essendo la nostra configurazione corretta. È esattamente quello che
è successo la prima volta che è stato eseguito qui: un container `portale-postgres`
di un altro progetto pubblicava `0.0.0.0:5432`.

Quindi la domanda giusta non è «la porta 5432 è chiusa?» ma «il NOSTRO Postgres è
pubblicato?». Si risponde interrogando la configurazione del nostro progetto, non
sondando una porta condivisa. Un eventuale listener estraneo viene segnalato come
informazione, non come fallimento.

Uso:
    docker compose up -d --build --wait
    python tools/smoke-test.py
"""
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000"
PG_PORT = 5432

results: list[tuple[str, bool, str]] = []
notes: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args],
                          cwd=ROOT, capture_output=True, text=True)


def exec_in(service: str, *cmd: str) -> subprocess.CompletedProcess:
    return compose("exec", "-T", service, *cmd)


def http_get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


# ---- 1. healthcheck di Compose -------------------------------------------
services: dict[str, dict] = {}
for line in compose("ps", "--format", "json").stdout.strip().splitlines():
    if line.strip():
        try:
            row = json.loads(line)
            services[row.get("Service")] = row
        except json.JSONDecodeError:
            pass

for svc in ("db", "api"):
    row = services.get(svc, {})
    health = row.get("Health") or ""
    check(f"{svc}: healthcheck Compose = healthy", health == "healthy",
          f"State={row.get('State', 'assente')} Health={health or '(nessuno)'}")

# ---- 2. migrazione Alembic ----------------------------------------------
# La revisione attesa si ricava dai file, non è scritta a mano: altrimenti ogni
# nuova migrazione farebbe fallire lo smoke test per un motivo che non è un guasto.
versions_dir = ROOT / "backend" / "migrations" / "versions"
migration_files = sorted(versions_dir.glob("[0-9]*.py"))
expected_rev = ""
if migration_files:
    for line in migration_files[-1].read_text(encoding="utf-8").splitlines():
        if line.startswith("revision:"):
            expected_rev = line.split("=", 1)[1].strip().strip("\"'")
            break

rev = exec_in("db", "psql", "-U", "tsm", "-d", "tsm", "-tAc",
              "SELECT version_num FROM alembic_version").stdout.strip()
check(f"alembic: revisione applicata = {expected_rev or '(nessuna)'}",
      bool(expected_rev) and rev == expected_rev,
      f"version_num={rev!r}, atteso {expected_rev!r} da {migration_files[-1].name if migration_files else '-'}")

# Le tabelle della migrazione dell'inventario devono esserci davvero.
tables = exec_in("db", "psql", "-U", "tsm", "-d", "tsm", "-tAc",
                 "SELECT tablename FROM pg_tables WHERE schemaname='public'").stdout.split()
for t in ("inventory_versions", "inventory_head", "audit"):
    check(f"tabella {t} presente", t in tables, f"trovate: {sorted(tables)}")

# ---- 3-4. endpoint operativi -------------------------------------------
code, body = http_get("/api/health")
check("GET /api/health = 200", code == 200, f"{code} {body[:120]}")
check("/api/health riporta status ok", '"status":"ok"' in body.replace(" ", ""), body[:120])

code, body = http_get("/api/ready")
check("GET /api/ready = 200", code == 200, f"{code} {body[:120]}")
check("/api/ready conferma il DB", '"database":"ok"' in body.replace(" ", ""), body[:120])

# ---- 5. il NOSTRO Postgres non è pubblicato -----------------------------

# (a) i publisher che Compose dichiara per il servizio db: PublishedPort deve essere
#     0/assente. È la fonte autorevole, ed è limitata al nostro progetto.
publishers = (services.get("db", {}) or {}).get("Publishers") or []
published = [p for p in publishers if p.get("PublishedPort")]
check("db: nessun publisher con porta host", not published,
      f"Publishers={json.dumps(publishers)[:200]}")

# (b) `compose port` non risolve una mappatura.
#     Compose stampa "invalid IP:0" quando la mappatura non esiste.
r = compose("port", "db", str(PG_PORT))
out = r.stdout.strip()
unmapped = (out == "" or "invalid ip" in out.lower() or out.endswith(":0") or r.returncode != 0)
check("db: `compose port` non mappa 5432", unmapped, f"stdout={out!r} rc={r.returncode}")

# (c) le port bindings del container sono vuote.
cid = (services.get("db", {}) or {}).get("ID", "")
bindings_empty, bindings_detail = False, "container non trovato"
if cid:
    raw = subprocess.run(
        ["docker", "inspect", cid, "--format", "{{json .NetworkSettings.Ports}}"],
        capture_output=True, text=True).stdout.strip()
    try:
        ports = json.loads(raw) if raw and raw != "null" else {}
    except json.JSONDecodeError:
        ports = {}
    bindings_empty = not {k: v for k, v in (ports or {}).items() if v}
    bindings_detail = f"Ports={raw[:160]}"
check("db: nessun port binding verso l'host", bindings_empty, bindings_detail)

# (d) la rete del db è `internal`: nessun gateway verso l'esterno.
#     Proprietà strutturale, non dipende da chi occupa le porte dell'host.
net_raw = subprocess.run(
    ["docker", "network", "inspect", "tsm_internal", "--format", "{{.Internal}}"],
    capture_output=True, text=True).stdout.strip()
check("rete `internal`: Internal=true", net_raw == "true", f"Internal={net_raw!r}")

# (e) informativo: se qualcuno ascolta su 5432, dire chi è. Non è un fallimento:
#     abbiamo già provato sopra che non siamo noi.
try:
    with socket.create_connection(("127.0.0.1", PG_PORT), timeout=3):
        listener = True
except OSError:
    listener = False

if listener:
    ours = {c for c in (row.get("Name", "") for row in services.values()) if c}
    foreign = []
    for line in subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
        capture_output=True, text=True).stdout.splitlines():
        name, _, ports = line.partition("\t")
        if f":{PG_PORT}->" in ports and name not in ours:
            foreign.append(f"{name} ({ports.strip()})")
    notes.append(
        f"127.0.0.1:{PG_PORT} risulta in ascolto, ma NON per opera nostra: "
        + (f"lo pubblica {', '.join(foreign)}" if foreign
           else "nessun container di questo progetto lo pubblica (servizio dell'host?)")
    )
else:
    notes.append(f"127.0.0.1:{PG_PORT} non è in ascolto sull'host.")

# ---- 6. non-root --------------------------------------------------------
uid = exec_in("api", "id", "-u").stdout.strip()
check("api: gira non-root", uid.isdigit() and uid != "0", f"uid={uid!r}")

# Per db non si forza `user:` (romperebbe initdb sul volume, creato di proprietà di
# root): si verifica che il processo del server abbia comunque lasciato i privilegi.
# `stat -c %U /proc/1` e non `ps -o user=`: il ps di busybox non supporta -o.
pg_user = exec_in("db", "stat", "-c", "%U", "/proc/1").stdout.strip()
check("db: il processo del server è non-root", pg_user not in ("", "root"),
      f"utente di PID 1 = {pg_user!r}")

# ---- 7. secret e filesystem --------------------------------------------
r = exec_in("api", "sh", "-c",
            "test -r /run/secrets/postgres_password && echo READABLE || echo UNREADABLE")
check("api: il secret è leggibile", "READABLE" in r.stdout, r.stdout.strip())

r = exec_in("api", "sh", "-c",
            "echo x >> /run/secrets/postgres_password 2>&1 && echo WRITABLE || echo READONLY")
check("api: il secret NON è scrivibile", "READONLY" in r.stdout, r.stdout.strip()[:120])

r = exec_in("api", "sh", "-c", "touch /app/prova 2>&1 && echo WRITABLE || echo READONLY")
check("api: filesystem read-only", "READONLY" in r.stdout, r.stdout.strip()[:120])

# ---- report ------------------------------------------------------------
print("=" * 74)
ok = True
for name, passed, detail in results:
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    if detail and not passed:
        print(f"         → {detail}")
    ok &= passed
if notes:
    print("-" * 74)
    for n in notes:
        print(f"  [nota] {n}")
print("=" * 74)
print("RISULTATO:", "TUTTI I CONTROLLI PASSATI" if ok else "CI SONO FALLIMENTI")
sys.exit(0 if ok else 1)
