#!/usr/bin/env python3
"""Archiviazione e avvio: controlli sulle DICHIARAZIONI (§8.40).

Questa suite non avvia niente. Verifica ciò che si può verificare leggendo la
configurazione resa da Compose, l'unità systemd e il preflight — cioè le
proprietà che devono valere *prima* di accendere la macchina, e che si
romperebbero in silenzio con una modifica distratta a compose.yaml.

Il comportamento a runtime — il mount che manca, PostgreSQL che scrive sul disco
sbagliato — sta in `tools/storage-e2e-test.sh`, che ha bisogno di un filesystem
vero e di root.

Uso:
    python tools/storage-config-test.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_MOUNT = "/srv/tsm-data"
PG_DIR = f"{DATA_MOUNT}/postgres"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, bool(passed), detail))


def report() -> int:
    print("=" * 76)
    ok = True
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        if detail and not passed:
            print(f"         -> {detail}")
        ok &= passed
    print("=" * 76)
    print("RISULTATO:", "TUTTI I CONTROLLI PASSATI" if ok else "CI SONO FALLIMENTI")
    return 0 if ok else 1


def compose_config(*extra: str) -> dict:
    """Configurazione RESA, in JSON. È quella che Compose userà davvero."""
    cmd = ["docker", "compose"]
    for f in extra or ("compose.yaml",):
        cmd += ["-f", f]
    cmd += ["config", "--format", "json"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:400])
    return json.loads(out.stdout)


def published(svc: dict) -> list[str]:
    out = []
    for p in svc.get("ports") or []:
        out.append(str(p.get("published", p)) if isinstance(p, dict) else str(p))
    return out


# ==================================================================
# 1. il volume dei dati è ancorato al secondo disco
# ==================================================================
cfg = compose_config()
pgdata = (cfg.get("volumes") or {}).get("pgdata") or {}
opts = pgdata.get("driver_opts") or {}

check("pgdata è un volume con nome, non un bind mount di servizio",
      "pgdata" in (cfg.get("volumes") or {}))
check("pgdata usa il driver local", pgdata.get("driver") == "local",
      str(pgdata.get("driver")))
check(f"pgdata è ancorato a {PG_DIR}", opts.get("device") == PG_DIR,
      str(opts.get("device")))
check("il bind è dichiarato con type=none e o=bind",
      opts.get("type") == "none" and opts.get("o") == "bind", json.dumps(opts))

# Il percorso NON deve essere parametrizzato: una variabile dimenticata in un
# `.env` sposterebbe il database in silenzio.
raw_compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
device_line = [l for l in raw_compose.splitlines() if "device:" in l]
check("il percorso del device è letterale, senza sostituzioni di variabili",
      bool(device_line) and all("${" not in l for l in device_line),
      " | ".join(device_line))

db = cfg["services"]["db"]
mounts = [v for v in (db.get("volumes") or [])]
targets = [m.get("target") if isinstance(m, dict) else str(m) for m in mounts]
check("il servizio db monta pgdata sul percorso dati di PostgreSQL",
      any("/var/lib/postgresql/data" in str(t) for t in targets), str(targets))
check("il servizio db monta pgdata per NOME (non un percorso dell'host)",
      any((m.get("source") == "pgdata" and m.get("type") == "volume")
          for m in mounts if isinstance(m, dict)), json.dumps(mounts)[:300])

# ==================================================================
# 2. niente porte pubblicate dove non devono esserci
# ==================================================================
check("PostgreSQL non pubblica porte sull'host",
      published(cfg["services"]["db"]) == [], str(published(cfg["services"]["db"])))
check("l'API non pubblica porte sull'host",
      published(cfg["services"]["api"]) == [], str(published(cfg["services"]["api"])))
check("il web pubblica SOLO 80 e 443 (porte standard, §8.31)",
      sorted(published(cfg["services"]["web"])) == ["443", "80"],
      str(published(cfg["services"]["web"])))

# ------------------------------------------------------------------ worker
# Il worker delle notifiche (§8.41) è un servizio a parte con la stessa immagine
# dell'API. Le proprietà che contano sono dichiarative, quindi si verificano qui.
worker = cfg["services"].get("worker") or {}
check("esiste il servizio worker", bool(worker))
check("il worker non pubblica porte", published(worker) == [],
      str(published(worker)))
check("il worker usa la stessa immagine dell'API",
      worker.get("image") == cfg["services"]["api"].get("image"),
      f"{worker.get('image')} vs {cfg['services']['api'].get('image')}")
check("il worker esegue lo script del worker, non uvicorn",
      "scripts/worker.py" in " ".join(worker.get("command") or []),
      str(worker.get("command")))
worker_networks = set((worker.get("networks") or {}).keys()) \
    if isinstance(worker.get("networks"), dict) else set(worker.get("networks") or [])
check("il worker è SOLO sulla rete interna, quella di PostgreSQL",
      worker_networks == {"internal"}, str(worker_networks))
check("il worker dichiara una sola replica",
      ((worker.get("deploy") or {}).get("replicas")) == 1,
      str((worker.get("deploy") or {}).get("replicas")))
check("il worker ha un healthcheck proprio",
      "worker_health.py" in json.dumps(worker.get("healthcheck") or {}),
      json.dumps(worker.get("healthcheck") or {})[:200])
check("il worker riceve il secret SMTP",
      "smtp_password" in json.dumps(worker.get("secrets") or []),
      json.dumps(worker.get("secrets") or []))
check("il worker NON riceve la password del proprietario dello schema",
      "postgres_password" not in json.dumps(worker.get("secrets") or []))

# ---- ruolo di database del worker, distinto da quello dell'API (§8.5) ----
#
# La GC delle foto ha bisogno di `DELETE` su `photos`, che è l'unico privilegio di
# cancellazione dello schema. Con un ruolo unico finirebbe anche a chi serve
# richieste HTTP, e un difetto in una rotta potrebbe cancellare byte che una
# versione storica dell'inventario referenzia.
worker_secrets = json.dumps(worker.get("secrets") or [])
check("il worker gira con il PROPRIO ruolo di database, non con quello dell'API",
      (worker.get("environment") or {}).get("TSM_DB_USER") == "tsm_worker",
      str((worker.get("environment") or {}).get("TSM_DB_USER")))
check("il worker riceve il secret del proprio ruolo",
      "worker_db_password" in worker_secrets, worker_secrets)
check("il worker NON riceve la password del ruolo dell'API",
      "api_db_password" not in worker_secrets, worker_secrets)
check("il worker legge la password dal secret del proprio ruolo",
      (worker.get("environment") or {}).get("TSM_DB_PASSWORD_FILE")
      == "/run/secrets/worker_db_password",
      str((worker.get("environment") or {}).get("TSM_DB_PASSWORD_FILE")))

api_secrets = json.dumps(cfg["services"]["api"].get("secrets") or [])
check("l'API NON riceve la password del ruolo del worker",
      "worker_db_password" not in api_secrets, api_secrets)

# Il servizio `migrate` è l'unico che vede tutte e tre le password: è lui a
# impostare quelle dei ruoli di runtime, e gira come proprietario dello schema.
migrate_secrets = json.dumps(cfg["services"]["migrate"].get("secrets") or [])
for s in ("postgres_password", "api_db_password", "worker_db_password"):
    check(f"migrate riceve {s}", s in migrate_secrets, migrate_secrets)

check("il secret worker_db_password è dichiarato",
      "worker_db_password" in (cfg.get("secrets") or {}),
      str(list((cfg.get("secrets") or {}).keys())))
check("il worker ha il filesystem in sola lettura",
      worker.get("read_only") is True)

# `/api/ready` non deve dipendere dal worker: l'API resta pronta con il worker
# fermo. Il controllo è che `api` non lo aspetti in avvio.
api_deps = cfg["services"]["api"].get("depends_on") or {}
check("l'API non dipende dal worker per partire",
      "worker" not in api_deps, str(list(api_deps)))
check("il worker dipende dal database e dalle migrazioni",
      set(worker.get("depends_on") or {}) == {"db", "migrate"},
      str(list(worker.get("depends_on") or {})))

# ==================================================================
# 3. le immagini restano sul disco di sistema
# ==================================================================
# Nessun servizio e nessun volume deve portare dati di Docker sul disco 2.
all_devices = [
    ((v.get("driver_opts") or {}).get("device") or "")
    for v in (cfg.get("volumes") or {}).values()
]
check("solo pgdata punta al disco dei dati",
      [d for d in all_devices if d.startswith(DATA_MOUNT)] == [PG_DIR],
      str(all_devices))
check("nessun servizio monta /var/lib/docker",
      not any("/var/lib/docker" in json.dumps(s.get("volumes") or [])
              for s in cfg["services"].values()))

try:
    root_dir = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"],
                              capture_output=True, text=True).stdout.strip()
    check("la radice di Docker di questa macchina non è sul disco dei dati",
          bool(root_dir) and not root_dir.startswith(DATA_MOUNT), root_dir)
except Exception as exc:                                    # pragma: no cover
    check("la radice di Docker di questa macchina non è sul disco dei dati",
          False, str(exc))

# ==================================================================
# 4. l'override di sviluppo esiste, disancora il volume, e non tocca altro
# ==================================================================
dev = compose_config("compose.yaml", "compose.storage-dev.yaml")
dev_opts = ((dev.get("volumes") or {}).get("pgdata") or {}).get("driver_opts")
check("compose.storage-dev.yaml rimuove l'ancoraggio al disco",
      not dev_opts, json.dumps(dev_opts))
check("l'override di archiviazione NON cambia l'ambiente",
      dev["services"]["api"]["environment"].get("TSM_ENV")
      == cfg["services"]["api"]["environment"].get("TSM_ENV"))
check("l'override di archiviazione NON pubblica la porta dell'API",
      published(dev["services"]["api"]) == [], str(published(dev["services"]["api"])))

# ==================================================================
# 5. l'unità systemd
# ==================================================================
unit_path = ROOT / "deploy" / "tsm.service"
check("l'unità systemd esiste", unit_path.is_file())
unit = unit_path.read_text(encoding="utf-8") if unit_path.is_file() else ""

check("RequiresMountsFor punta al disco dei dati",
      f"RequiresMountsFor={DATA_MOUNT}" in unit)
check("dipende da docker.service", "Requires=docker.service" in unit)
check("ordinata dopo docker e la rete",
      "After=docker.service network-online.target" in unit
      and "Wants=network-online.target" in unit)
check("RemainAfterExit=yes (up -d esce subito lasciando i container attivi)",
      "RemainAfterExit=yes" in unit)
check("ExecStartPre esegue il preflight",
      re.search(r"ExecStartPre=/opt/tsm/deploy/preflight\.sh", unit) is not None)
check("ExecStart usa un percorso assoluto e --remove-orphans",
      "ExecStart=/usr/bin/docker compose up -d --remove-orphans" in unit)
check("ExecStop ferma lo stack",
      "ExecStop=/usr/bin/docker compose down" in unit)
check("WorkingDirectory esplicita", "WorkingDirectory=/opt/tsm" in unit)

# Il punto che il committente ha chiesto di provare esplicitamente: l'unità di
# produzione non può selezionare per sbaglio una configurazione di sviluppo.
#
# Si guardano le DIRETTIVE, non tutto il file: l'unità *nomina* gli override di
# sviluppo in un commento, per dire che non si usano. Cercare la stringa nel
# testo intero vieterebbe la spiegazione insieme alla cosa spiegata — ed è
# esattamente l'errore che la prima versione di questo test commetteva.
directives = [l.strip() for l in unit.splitlines()
              if l.strip() and not l.strip().startswith("#")]
exec_lines = [l for l in directives if l.startswith("Exec")]

for forbidden in ("compose.dev.yaml", "compose.storage-dev.yaml",
                  "TSM_COOKIE_SECURE", "TSM_ENV=development"):
    check(f"nessuna direttiva Exec seleziona «{forbidden}»",
          not any(forbidden in l for l in exec_lines),
          " | ".join(l for l in exec_lines if forbidden in l))
check("nessuna direttiva dell'unità cita configurazioni di sviluppo",
      not any(("compose.dev" in l or "storage-dev" in l) for l in directives),
      " | ".join(l for l in directives if "dev" in l))

# Nessun secret nel file dell'unità né sulla riga di comando: di nuovo solo le
# direttive, perché il commento spiega proprio perché non ci sono.
check("nessuna direttiva contiene password o EnvironmentFile",
      not any(re.search(r"(?i)(password=|secret=|EnvironmentFile)", l)
              for l in directives),
      " | ".join(l for l in directives
                 if re.search(r"(?i)(password|secret|EnvironmentFile)", l)))
check("l'unità non ritenta l'avvio in loop", "Restart=no" in unit)

# ==================================================================
# 6. il preflight: forma, codici, e ciò che NON fa
# ==================================================================
pf_path = ROOT / "deploy" / "preflight.sh"
check("il preflight esiste", pf_path.is_file())
pf = pf_path.read_text(encoding="utf-8") if pf_path.is_file() else ""

check("il preflight ha lo shebang bash", pf.startswith("#!/usr/bin/env bash"))
check("il preflight usa terminatori di riga LF",
      "\r\n" not in pf_path.read_text(encoding="utf-8", newline="") if pf_path.is_file() else False,
      "CRLF su Linux dà 'bad interpreter'")
check("il preflight fallisce su variabili non impostate ed errori",
      "set -euo pipefail" in pf)

# I codici di uscita sono un contratto: un'automazione li distingue.
for code, what in [("20", "mount assente"), ("21", "filesystem di root"),
                   ("22", "directory dati assente"), ("23", "proprietario"),
                   ("24", "permessi"), ("25", "non scrivibile"),
                   ("26", "spazio"), ("27", "SELinux"), ("28", "SELinux persistente"),
                   ("30", "secret"), ("31", "TLS"), ("40", "deroga di sviluppo"),
                   ("41", "porte pubblicate"), ("42", "configurazione non di produzione")]:
    check(f"il preflight usa il codice {code} ({what})",
          re.search(rf"fail {code} ", pf) is not None)

# Il preflight NON deve creare la directory dei dati. Attenzione: `install -d`
# compare nei MESSAGGI, per dire all'amministratore quale comando eseguire, e
# quello va bene. Si cercano quindi solo i comandi VERI: le righe che iniziano
# con il comando, non quelle che sono corpo di una stringa fra virgolette.
pf_commands = [l for l in pf.splitlines()
               if re.match(r"^\s*(mkdir|install)\b", l)]
check("il preflight NON crea la directory dei dati",
      not any(("PG_DIR" in l or "DATA_MOUNT" in l) for l in pf_commands),
      " | ".join(pf_commands) or "un mount assente è un guasto, non da aggirare")
check("il preflight suggerisce comunque il comando giusto nel messaggio",
      "install -d -o $PG_UID -g $PG_GID -m 0700 $PG_DIR" in pf)
check("il preflight non formatta e non partiziona",
      not re.search(r"\b(mkfs|parted|fdisk|sgdisk|wipefs)\b", pf))
check("il preflight non modifica /etc/fstab", "/etc/fstab" not in pf)
check("il preflight non scarica immagini",
      not re.search(r"docker (compose )?pull", pf))

check("l'uid di PostgreSQL è dedotto dall'immagine, non scritto a mano",
      "id_in_image" in pf and re.search(r"-u postgres", pf) is not None)
check("nessun uid numerico assunto per PostgreSQL",
      not re.search(r"PG_UID=(\"|')?(999|70)\b", pf))
check("la soglia di spazio libero è 5 GiB",
      "MIN_FREE_BYTES=$((5 * 1024 * 1024 * 1024))" in pf)
check("le soglie di monitoraggio 70/85 sono presenti come avvisi",
      "-ge 85" in pf and "-ge 70" in pf)
check("verifica la persistenza SELinux con semanage, non solo chcon",
      "semanage fcontext" in pf and "restorecon" in pf)
check("il preflight non dipende da un interprete esterno per leggere la config",
      "python" not in pf.replace("# ", "").split("cfg_service_block")[0]
      or "awk" in pf)

# ==================================================================
# 7. la documentazione dice le cose che devono essere dette
# ==================================================================
doc_path = ROOT / "deploy" / "README.md"
check("il runbook di produzione esiste", doc_path.is_file())
doc = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else ""

for needle, why in [
    ("tsm-prd-01", "hostname della macchina"),
    ("blkid", "scoperta dell'UUID"),
    ("UUID=", "fstab per UUID"),
    ("mount -a", "applicazione di fstab"),
    ("findmnt", "verifica del mount"),
    ("mkfs.xfs", "filesystem XFS"),
    ("parted", "partizionamento"),
    ("semanage fcontext", "etichetta SELinux persistente"),
    ("restorecon", "applicazione dell'etichetta"),
    ("-u postgres", "uid letto dall'immagine, non assunto"),
    ("swapfile", "swap sul disco 1"),
    ("Veeam", "backup a livello di VM"),
    ("thin", "avvertenza sul thin provisioning"),
    ("xfs_growfs", "crescita del filesystem"),
]:
    check(f"il runbook documenta {why}", needle in doc, f"manca «{needle}»")

check("il runbook avverte di NON usare /dev/sdb1 come identità stabile",
      "/dev/sdb1" in doc and "non è un'identità stabile" in doc)
check("il runbook dice che entrambi i dischi devono essere nel job di backup",
      "Entrambi i dischi" in doc or "entrambi i dischi" in doc)
check("il runbook descrive il comportamento a disco scomparso a caldo",
      "disco sparisce a servizio avviato" in doc)
check("il runbook non promette recupero automatico",
      "nessun recupero automatico" in doc or "non tenta alcun recupero" in doc)
check("il runbook indica le soglie di monitoraggio 70-75 e 85-90",
      "70" in doc and "85" in doc)

# ==================================================================
# 8. le terminazioni di riga degli artefatti di deployment
# ==================================================================
for rel in ("deploy/preflight.sh", "deploy/tsm.service", "tools/storage-e2e-test.sh"):
    raw = (ROOT / rel).read_bytes()
    check(f"{rel} ha terminatori LF", b"\r\n" not in raw,
          "un file CRLF letto da Linux non parte o confronta valori con \\r in coda")

check("esiste .gitattributes che impone LF agli script",
      (ROOT / ".gitattributes").is_file()
      and "*.sh    text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8"))


# ==================================================================
# 9. foto: i due limiti di dimensione e le rotte che NON esistono (§8.5)
# ==================================================================
#
# Nginx ha un limite grossolano, l'applicazione quelli precisi. Se il primo fosse
# più stretto del secondo, il caricamento di un'immagine ammessa dall'applicazione
# fallirebbe nel proxy con un errore che non spiega niente — e solo per chi passa
# dal proxy, cioè per tutti tranne chi prova dalla rete interna.
nginx_conf = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
nginx_dev = (ROOT / "web" / "nginx.dev.conf").read_text(encoding="utf-8")
check("nginx accetta un corpo abbastanza grande per una foto da 10 MB",
      "client_max_body_size 11m" in nginx_conf,
      [l for l in nginx_conf.splitlines() if "client_max_body_size" in l])
check("la configurazione di sviluppo ha lo STESSO limite di quella di produzione",
      "client_max_body_size 11m" in nginx_dev,
      "le due configurazioni devono differire solo nei listener "
      "(tools/sync-nginx-dev.py)")

def code_only(path) -> str:
    """Il sorgente Python senza commenti E senza docstring.

    ⚠ Cercare una parola nel testo grezzo di un file è la trappola in cui questo
    controllo è già caduto DUE volte, e per lo stesso motivo: la prosa spiega
    proprio ciò che il codice NON fa, quindi contiene le parole che si stanno
    cercando. «nessun `filename` nelle intestazioni» falliva per il commento che
    dice perché non c'è; «la mappa è pura: nessun SQLAlchemy» falliva per la
    docstring che dice «Nessun database, nessun SQLAlchemy».

    Quindi via i commenti (`ast.unparse` li perde) e via le docstring (che invece
    sopravvivono, perché sono letterali). Le altre stringhe restano — sono codice —
    quindi i confronti su di esse vanno scritti senza le virgolette esterne, perché
    la ricostruzione può normalizzarle.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            del body[0]
    return ast.unparse(tree)


photos_api = code_only(ROOT / "backend" / "app" / "api" / "photos.py")
check("non esiste una rotta di cancellazione delle foto",
      "router.delete" not in photos_api,
      "le versioni storiche referenziano le foto: cancellarne i byte da HTTP "
      "romperebbe il ripristino di un'altra persona (§8.5)")
check("il caricamento è riservato agli amministratori",
      "Depends(require_admin)" in photos_api)
check("la lettura pretende una sessione non ristretta",
      "Depends(require_actor)" in photos_api)
check("le foto si servono con cache privata e immutabile",
      "private, max-age=31536000, immutable" in photos_api)
check("le foto si servono con nosniff",
      "X-Content-Type-Options" in photos_api and "nosniff" in photos_api)
check("nessun nome di file del chiamante finisce nelle intestazioni",
      "filename" not in photos_api,
      "né Content-Disposition né altro devono offrire un posto per il testo del "
      "chiamante, e `file.filename` non deve essere letto affatto")

gc_src = code_only(ROOT / "backend" / "app" / "photos" / "gc.py")
check("la GC pretende ENTRAMBE le condizioni: nessun riferimento e età",
      "NOT EXISTS" in gc_src and "created_at < :cutoff" in gc_src)
check("la GC non guarda soltanto l'inventario corrente",
      "inventory_photo_refs" in gc_src and "inventory_head" not in gc_src,
      "una foto referenziata solo da una versione vecchia è viva (§8.5)")

migration = (ROOT / "backend" / "migrations" / "versions"
             / "0009_photos.py").read_text(encoding="utf-8")
check("il ruolo dell'API non può cancellare foto",
      "REVOKE UPDATE, DELETE, TRUNCATE ON photos FROM {API_ROLE}" in migration
      or 'REVOKE UPDATE, DELETE, TRUNCATE ON photos FROM {API_ROLE}"' in migration
      or "photos FROM {API_ROLE}" in migration)
check("il ruolo del worker non può inserire foto",
      "REVOKE INSERT, UPDATE, TRUNCATE ON photos FROM {WORKER_ROLE}" in migration)
check("la chiave esterna dei riferimenti è senza ON DELETE",
      'sa.ForeignKey("photos.id")' in migration,
      "senza ON DELETE il database rifiuta di cancellare una foto referenziata: "
      "è la difesa che regge se la query della GC viene riscritta male")


# ==================================================================
# 10. fase 2A: lo schema normalizzato esiste e NESSUNO lo scrive (§8.42)
# ==================================================================
#
# L'affermazione centrale del commit della fase 2A. I test su PostgreSQL provano che
# un salvataggio reale non tocca le tabelle; questo controllo prova la stessa cosa
# un livello più su, sul SORGENTE: nessun modulo dell'applicazione le scrive. È il
# controllo che regge quando qualcuno, fra un mese, aggiunge una `INSERT` «tanto per
# provare» — e i test di comportamento la vedrebbero solo se passasse dal
# salvataggio.
NORMALISED_TABLES = ("inventory_locations", "inventory_rooms", "inventory_racks",
                     "inventory_devices", "inventory_manual_entries",
                     "inventory_state")

_WRITE = ("insert into ", "update ", "delete from ", "truncate ")
app_sources = sorted((ROOT / "backend" / "app").rglob("*.py"))
check("i sorgenti dell'applicazione sono leggibili", len(app_sources) > 20,
      f"trovati {len(app_sources)} file")

scritture = []
for path in app_sources:
    lowered = path.read_text(encoding="utf-8").lower()
    for table in NORMALISED_TABLES:
        for verb in _WRITE:
            if f"{verb}{table}" in lowered:
                scritture.append(f"{path.name}: {verb}{table}")
check("nessun modulo dell'applicazione scrive le tabelle normalizzate",
      not scritture,
      f"la sincronizzazione è la fase 2C: {scritture}")

rel = code_only(ROOT / "backend" / "app" / "inventory" / "relational.py")
check("la mappa relazionale è pura: nessun SQL, nessun SQLAlchemy",
      "sqlalchemy" not in rel.lower() and "text(" not in rel,
      "la parte che va provata è la mappa, e provarla contro un database "
      "significherebbe provarla insieme a un database")
check("la mappa canonicalizza in ingresso",
      "canonicalise(doc)" in rel,
      "senza, i default (§8.14) non sarebbero materializzati e le colonne "
      "resterebbero vuote per campi già valorizzati")

mig10 = (ROOT / "backend" / "migrations" / "versions"
         / "0010_normalised.py").read_text(encoding="utf-8")
check("i vincoli con ambito sono differibili",
      mig10.count("deferrable=True") >= 9,
      "scambiare due codici in una transazione è legittimo e a metà collide")
check("i ruoli di runtime hanno solo lettura sulle tabelle normalizzate",
      "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {table} " in mig10
      and "GRANT SELECT ON {table} TO {role}" in mig10,
      "i privilegi di scrittura li concede la fase 2C, con il codice che li usa")
check("i dispositivi NON hanno un vincolo di unicità sul codice",
      "uq_device_code" not in mig10,
      "l'import tabellare produce identificativi ripetuti nello stesso rack, e "
      "vincolarli farebbe rifiutare alla fase 2C documenti che la fase 1 accetta")
check("i vani restano JSONB e non una tabella",
      'sa.Column("vani", pg.JSONB)' in mig10
      and "create_table(\n        \"inventory_vani" not in mig10)


if __name__ == "__main__":
    sys.exit(report())
