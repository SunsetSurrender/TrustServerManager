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

    # `utf-8-sig` e non `utf-8`: alcuni sorgenti hanno il BOM (li ha scritti
    # PowerShell), e `ast.parse` lo rifiuta con «invalid non-printable character
    # U+FEFF» — un errore che parla di un carattere invisibile e non del fatto che
    # il file si legge con la codifica sbagliata. Python, importandoli, il BOM lo
    # gestisce da sé.
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
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
# 10. fase 2A: lo schema normalizzato, e UN SOLO scrittore (§8.42)
# ==================================================================
#
# I test su PostgreSQL provano che un salvataggio reale non tocca le tabelle; questi
# controlli provano la stessa cosa un livello più su, sul SORGENTE. È ciò che regge
# quando qualcuno, fra un mese, aggiunge una `INSERT` «tanto per provare» — e i test
# di comportamento la vedrebbero solo se passasse dal salvataggio.
NORMALISED_TABLES = ("inventory_locations", "inventory_rooms", "inventory_racks",
                     "inventory_devices", "inventory_manual_entries",
                     "inventory_projection_state")

#: L'UNICO modulo dell'applicazione autorizzato a scrivere la proiezione. Non è
#: raggiungibile dal percorso delle richieste: lo usa `scripts/project.py`, che gira
#: come proprietario dello schema.
ONLY_WRITER = "projection.py"

_WRITE = ("insert into ", "update ", "delete from ", "truncate ")
app_sources = sorted((ROOT / "backend" / "app").rglob("*.py"))
check("i sorgenti dell'applicazione sono leggibili", len(app_sources) > 20,
      f"trovati {len(app_sources)} file")

scritture = []
for path in app_sources:
    if path.name == ONLY_WRITER:
        continue
    lowered = path.read_text(encoding="utf-8").lower()
    for table in NORMALISED_TABLES:
        for verb in _WRITE:
            if f"{verb}{table}" in lowered:
                scritture.append(f"{path.name}: {verb}{table}")
check(f"soltanto {ONLY_WRITER} scrive le tabelle normalizzate",
      not scritture,
      f"la sincronizzazione al salvataggio è la fase 2C: {scritture}")

rel = code_only(ROOT / "backend" / "app" / "inventory" / "relational.py")
# ⚠ Si cerca `sqlalchemy` e `.execute(`, non `text(`.
#
# `text(` sembrava il segno di una query e non lo è: da quando la mappa condivide la
# regola sul testo, il sorgente contiene `is_representable_text(` — che contiene
# `text(`. Il controllo falliva su una funzione pura per una sottostringa, cioè
# rispondeva alla domanda sbagliata.
check("la mappa relazionale è pura: nessun SQL, nessun SQLAlchemy",
      "sqlalchemy" not in rel.lower() and ".execute(" not in rel,
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


# ==================================================================
# 11. fase 2B: si costruisce la proiezione, e NESSUNO la consuma (§8.42)
# ==================================================================
#
# Il requisito di questo commit è asimmetrico: la proiezione si può SCRIVERE (con un
# comando esplicito) e non si può LEGGERE da nessun percorso di servizio. La seconda
# metà è quella che un test di comportamento non sa provare: un `import` aggiunto
# oggi «solo per un contatore» non fa fallire niente, e il giorno in cui la
# proiezione è vecchia diventa una risposta sbagliata servita a un utente.

#: Chi non deve nemmeno poter raggiungere la proiezione: le rotte, l'avvio, il
#: worker. Lo scheduler sta in `app/notifications/worker.py`.
#:
#: ⚠ Due file sono ESCLUSI dal divieto generale, e ognuno ha un controllo PROPRIO più
#: stretto qui sotto. Il divieto non si è allentato di fase in fase: si è specializzato.
#:
#:   - `app/api/health.py`, dalla fase 2C: la readiness deve sapere se la proiezione
#:     rispecchia la testa, perché da quella fase un backend con una proiezione
#:     vecchia rifiuta tutte le scritture (§8.44). Può guardare lo STATO, non può
#:     riassemblare;
#:   - `app/api/inventory.py`, dalla fase 2D: il `GET` LEGGE la proiezione, ed è il
#:     senso della fase (§8.45). Deve chiamare `current_document` e NON deve
#:     restituire l'istantanea immutabile — che è il controllo che un test di
#:     comportamento non sa fare, perché un ripiego sul JSON produrrebbe la risposta
#:     giusta proprio nei casi in cui è sbagliato averla.
#:   - `app/notifications/candidates.py` e `worker.py`, dalla fase 2F: lo scanner
#:     delle scadenze prende i candidati dalla proiezione (§8.47). Sono DUE moduli e
#:     non uno perché il worker orchestra (snapshot, guardia sulla revisione, esiti)
#:     e `candidates` interroga: tenere l'interrogazione in un modulo suo è ciò che
#:     rende possibile il controllo statico «la sorgente non filtra i dismessi».
#:
#: ⚠ ALLARGATO nella fase 2F. Fino alla 2E l'elenco diceva «lo scheduler delle
#: notifiche no», ed era giusto: il worker leggeva il documento. Adesso è un lettore
#: legittimo, e l'elenco lo registra invece di essere aggirato — la differenza fra un
#: controllo che accompagna le decisioni e uno che qualcuno finisce per cancellare.
READINESS = ROOT / "backend" / "app" / "api" / "health.py"
INVENTORY_ROUTE = ROOT / "backend" / "app" / "api" / "inventory.py"
WORKER_SOURCE = ROOT / "backend" / "app" / "notifications" / "candidates.py"
WORKER_LOOP = ROOT / "backend" / "app" / "notifications" / "worker.py"
CONSUMERS_ALLOWED = (READINESS, INVENTORY_ROUTE, WORKER_SOURCE, WORKER_LOOP)
CONSUMERS_FORBIDDEN = [
    p for p in (
        sorted((ROOT / "backend" / "app" / "api").rglob("*.py"))
        + sorted((ROOT / "backend" / "app" / "notifications").rglob("*.py"))
        + [ROOT / "backend" / "app" / "main.py"]
    ) if p not in CONSUMERS_ALLOWED
]

consumatori = []
for path in CONSUMERS_FORBIDDEN:
    source = code_only(path)
    if "projection" in source:
        consumatori.append(f"{path.name}: nomina `projection`")
    for table in NORMALISED_TABLES:
        if table in source:
            consumatori.append(f"{path.name}: nomina {table}")
check("solo i quattro lettori dichiarati raggiungono la proiezione",
      not consumatori,
      f"la readiness, il `GET`, e dalla 2F i due moduli del worker. L'avvio e le "
      f"altre rotte no: {consumatori}")
check("i quattro lettori dichiarati la raggiungono davvero",
      all("projection" in code_only(p) for p in CONSUMERS_ALLOWED),
      "un elenco di eccezioni che contiene un file che non le userebbe mai è un "
      "elenco che si allarga senza che nessuno se ne accorga")

# --- la readiness: può chiedere lo STATO, non può ricostruire ---
readiness_src = code_only(READINESS)
check("la readiness controlla lo stato della proiezione",
      "projection.currency(conn).current" in readiness_src,
      "dalla fase 2C un backend con una proiezione vecchia rifiuta ogni scrittura: "
      "dire «pronto» sarebbe mentire al reverse proxy")
check("la readiness NON riassembla l'inventario a ogni sonda",
      all(t not in readiness_src for t in ("read_model", "assemble", "verify(",
                                           "rebuild(", "synchronise")),
      "riassemblare costerebbe quanto un `--verify` completo, ripetuto ogni pochi "
      "secondi per sempre: la fedeltà la dimostrano la verifica transazionale dopo "
      "ogni scrittura e `project.py --verify` (§8.44)")
check("la readiness non nomina le tabelle della proiezione",
      all(t not in readiness_src for t in NORMALISED_TABLES
          if t != "inventory_projection_state"),
      "chiede a `projection` di rispondere, non interroga le tabelle per conto suo")

inv_init = code_only(ROOT / "backend" / "app" / "inventory" / "__init__.py")
check("il pacchetto inventory non riesporta `projection`",
      "projection" not in inv_init,
      "`app/api/inventory.py` importa questo pacchetto: riesportarla la renderebbe "
      "raggiungibile dal percorso delle richieste con un import scritto per sbaglio")

proj = code_only(ROOT / "backend" / "app" / "inventory" / "projection.py")
check("la ricostruzione prende il lock della testa",
      "FOR UPDATE" in proj,
      "«atomicamente sotto la testa bloccata» deve significare che un PUT "
      "concorrente aspetta, non che si spera che non arrivi")
check("la ricostruzione non fa commit: la transazione è del chiamante",
      ".commit()" not in proj,
      "un fallimento deve SOLLEVARE e lasciare che il chiamante annulli tutto: "
      "non esiste un esito «proiezione a metà»")
# ⚠ Due frammenti in due funzioni diverse dopo il rifattorizzamento della fase 2C:
# `rebuild` verifica che il digest registrato nella versione valga, `synchronise`
# confronta il riassemblato con quello atteso. Cercare `digest != recorded` — come
# faceva questo controllo — falliva perché la seconda metà si è spostata, non perché
# la proprietà fosse venuta meno.
check("la ricostruzione confronta il digest REGISTRATO, non solo il ricalcolato",
      "recorded != recomputed" in proj and "digest != sha256" in proj,
      "confrontare col ricalcolato sarebbe confrontare il codice con sé stesso")
check("la verifica guarda anche la coerenza del modello",
      "validate_model" in proj,
      "il digest è cieco alle colonne derivate: `garanzia_date` non torna nel "
      "documento, quindi una data sbagliata lascia il digest identico")
check("la rilettura converte gli uuid in stringhe",
      "str(value)" in proj,
      "un `uuid` letto con una query testuale torna come oggetto, e il sintomo "
      "sarebbe «il digest non torna» — che non fa pensare a un tipo")

# ⚠ ROVESCIATO nella 2G: era «usano il parser dello SCANNER». Il parser si è spostato
# in `app/domain.py`, che è la sede unica della semantica di business, e da lì lo
# prendono tutti e tre i chiamanti — lo scanner, la colonna derivata, il frontend.
check("le colonne data derivate usano il parser del DOMINIO",
      "domain.parse_expiry(value)" in rel
      and "from app.notifications.expiry import parse_expiry" not in rel,
      "un secondo parser sarebbe una seconda idea di «data valida», e divergerebbe "
      "sui casi limite — che sono i valori che l'inventario reale contiene")
check("lo scanner delle scadenze RIESPORTA il parser invece di averne uno proprio",
      "from app.domain import" in code_only(ROOT / "backend" / "app"
                                            / "notifications" / "expiry.py")
      and "_ISO_DATE" not in code_only(ROOT / "backend" / "app" / "notifications"
                                       / "expiry.py"),
      "finché la definizione stava qui, il frontend ne aveva una seconda e sette "
      "forme di data erano visibili nella vista Scadenze e invisibili al worker")
check("la classe di cifre del parser è ASCII, non Unicode",
      "[0-9]{4}" in code_only(ROOT / "backend" / "app" / "domain.py")
      and "\\d{4}" not in code_only(ROOT / "backend" / "app" / "domain.py"),
      "in Python `\\d` combacia con OGNI cifra decimale Unicode: `２０２７-０３-１５` era "
      "una data per il backend e non per il frontend. Difetto reale, trovato dal "
      "confronto fra le due implementazioni")
check("la mappa dichiara i limiti delle colonne intere",
      "INT32_MIN" in rel and "2147483647" in rel,
      "`u: 3000000000` non è un caso teorico: è un INSERT che fallisce a metà del "
      "popolamento per un dato che la fase 1 ha sempre accettato")
check("la mappa dichiara il contratto di legatura dei numeric",
      "to_column_number" in rel and "from_column_number" in rel
      and "Decimal(repr(value))" in rel,
      "legando il float invece del Decimal, 10.0 torna 10 e 0.30000000000000004 "
      "torna 0.3: misurato contro PostgreSQL, non supposto")

mig11 = (ROOT / "backend" / "migrations" / "versions"
         / "0011_projection.py").read_text(encoding="utf-8")
check("lo stato della proiezione ha un nome che dice la verità",
      'op.rename_table("inventory_state"' in mig11,
      "«stato dell'inventario» era falso: lo stato dell'inventario è la testa")
check("lo stato registra ANCHE il digest verificato",
      '"head_sha256"' in mig11,
      "la versione dice quale istantanea; il digest dice che cosa si è verificato")
check("una riga di stato scritta a metà è impossibile",
      'alter_column(STATE_TABLE, "head_version", nullable=False)' in mig11
      and 'alter_column(STATE_TABLE, "head_sha256", nullable=False)' in mig11)
check("le colonne data derivate esistono con il loro CHECK",
      "garanzia_date" in mig11 and "supporto_date" in mig11
      and "IS NULL OR" in mig11,
      "una data interpretata non può esistere senza il testo da cui è stata "
      "interpretata")
check("gli indici sulle scadenze sono parziali",
      "postgresql_where" in mig11,
      "la domanda implica IS NOT NULL, e nel seed reale la maggior parte dei "
      "dispositivi non ha date")
check("la 0011 non concedeva scrittura ai ruoli di runtime",
      "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {STATE_TABLE} " in mig11
      and "GRANT INSERT" not in mig11 and "GRANT ALL" not in mig11,
      "il popolamento girava come proprietario; i privilegi di scrittura li concede "
      "la 0012, con il codice che li usa (§8.44)")
# ⚠ Sul CODICE, non sul testo grezzo: la docstring della 0011 spiega perché il
# popolamento non sta lì e cita `scripts/project.py --rebuild`. È la terza volta che
# questo controllo cade nella stessa trappola — la prosa contiene le parole che si
# stanno cercando proprio perché descrive ciò che il codice non fa.
mig11_code = code_only(ROOT / "backend" / "migrations" / "versions"
                       / "0011_projection.py")
#: Le CHIAMATE che una migrazione di dati dovrebbe fare. Si cercano queste e non le
#: parole «rebuild»/«normalise» sciolte: `down_revision = '0010_normalised'` contiene
#: la seconda, e un controllo che fallisce per il nome della migrazione precedente
#: non sta controllando quello che dice di controllare.
check("il popolamento NON è una migrazione di dati",
      "normalise(" not in mig11_code and "rebuild(" not in mig11_code
      and "insert into inventory_" not in mig11_code.lower(),
      "una migrazione si esegue una volta sola, senza che nessuno la guardi, e se "
      "aborta ferma il deployment")

cli = code_only(ROOT / "backend" / "scripts" / "project.py")
check("il comando pretende un'azione esplicita",
      "required=True" in cli and "--rebuild" in cli and "--status" in cli
      and "--verify" in cli,
      "una ricostruzione non deve poter partire perché qualcuno ha lanciato il "
      "comando senza argomenti")
check("il comando riporta fedeltà e attualità separatamente",
      "faithful" in cli and "result.current" in cli,
      "sono cause diverse — un difetto del codice contro un comando mancante — e "
      "riportarne solo la prima costringerebbe a rieseguire il comando dopo ogni "
      "rimedio per scoprire la successiva")
check("dalla fase 2C `--verify` fallisce anche su una proiezione vecchia",
      "result.ok" in cli,
      "una proiezione fedele a una versione vecchia è lo stato in cui l'API rifiuta "
      "le scritture: in 2B era normale, adesso è un guasto (§8.44)")

handoff = ""
for path in sorted((ROOT / "handoff").rglob("*.js")):
    handoff += path.read_text(encoding="utf-8")
check("il frontend non sa che la proiezione esista",
      all(t not in handoff for t in NORMALISED_TABLES)
      and "garanzia_date" not in handoff,
      "il contratto del frontend è il documento (§8.22), e questo commit non lo "
      "cambia di una riga")


# ==================================================================
# 12. fedeltà numerica dell'istantanea (§8.16)
# ==================================================================
#
#     Ogni documento accettato dal PUT normale deve essere rappresentabile senza
#     perdite dal magazzino delle istantanee, secondo la semantica del digest
#     canonico del repository.
#
# JSONB tiene i numeri in `numeric`: `-0.0` torna `0.0` e `1e+20` torna intero. La
# risposta è rifiutare in ingresso, non ricalcolare il digest dopo che PostgreSQL ha
# cambiato il valore — che vorrebbe dire registrare come «accettato» un documento
# diverso da quello inviato.


def function_source(path, name):
    """Il codice di UNA funzione, senza commenti né docstring.

    Serve per verificare un ORDINE dentro una funzione: cercare due stringhe nel
    file intero direbbe soltanto che esistono entrambe, e la prima occorrenza di
    `validate_normal_document` sta in `bootstrap`, che viene prima di `save` — quindi
    un controllo sul file darebbe verde qualunque cosa faccia `save`.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                del body[0]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    return ""


strings = code_only(ROOT / "backend" / "app" / "inventory" / "json_strings.py")
shared = code_only(ROOT / "backend" / "app" / "inventory" / "representable.py")
check("la regola sul testo è pura: nessun database",
      "sqlalchemy" not in strings.lower() and ".execute(" not in strings)
check("il codice di rifiuto del testo è quello stabile",
      "json_string_not_roundtrippable" in strings)
check("la regola sul testo non modifica il testo",
      all(t not in strings
          for t in ("unicodedata", "normalize", ".replace(", ".strip()", ".casefold(")),
      "ripulire vorrebbe dire salvare un documento diverso da quello inviato, e per "
      "un nome è una modifica che l'utente vede attribuita a sé. `encode` c'è, ma "
      "serve a CHIEDERE se la stringa è codificabile, non a cambiarla")
check("le due regole condividono UNA visita del documento",
      "walk_scalars" in shared and "json_numbers" in shared
      and "json_strings" in shared,
      "un documento percorso due volte in due modi diversi lascia scoperta una metà")
check("la visita comprende le CHIAVI degli oggetti",
      "KEY" in shared and "key_segment" in shared,
      "il modello è aperto: una chiave ignota è un dato dell'utente come un valore")
check("una chiave non scrivibile non finisce nel percorso",
      "unwritable" in shared,
      "il percorso serve a trovare il campo, non a ripetere ciò che non si può "
      "nemmeno scrivere")
check("la mappa relazionale non riscrive la regola sul testo",
      "is_representable_text" in rel
      and all(t not in rel for t in ("encode(", "\\x00")),
      "«PostgreSQL conserva questa stringa?» ha una risposta sola, e la conseguenza "
      "per la proiezione è più stretta: non entra nemmeno in `extra`")

numbers = code_only(ROOT / "backend" / "app" / "inventory" / "json_numbers.py")
check("la regola sui numeri è pura: nessun database",
      "sqlalchemy" not in numbers.lower() and ".execute(" not in numbers,
      "gira nel percorso della richiesta, prima di qualunque accesso al database")
check("il codice di rifiuto è quello stabile",
      "json_number_not_roundtrippable" in numbers,
      "i client lo confrontano: non cambia")
check("i booleani non sono numeri, per questa regola",
      "isinstance(value, bool)" in numbers,
      "`isinstance(True, int)` è vero in Python, e JSONB i booleani li conserva")
check("il limite degli interi non passa da str()",
      "10 ** 131072" in numbers and "len(str(" not in numbers,
      "convertire in stringa un intero di più di 4300 cifre solleva ValueError da "
      "Python 3.11: la regola crollava invece di rispondere")

doc_src = code_only(ROOT / "backend" / "app" / "inventory" / "document.py")
check("lo schema congelato applica ENTRAMBE le regole, in una visita sola",
      "unrepresentable_items(doc)" in doc_src,
      "numeri e testo sono due implementazioni dello stesso invariante (§8.16), non "
      "due controlli indipendenti")
# Dentro la FUNZIONE, non nel file: `max_bytes` compare già nella firma, e un
# controllo sul modulo intero direbbe che la misura viene prima qualunque cosa faccia
# il corpo. È lo stesso inganno del controllo sull'ordine in `save`.
validate_src = function_source(
    ROOT / "backend" / "app" / "inventory" / "document.py",
    "validate_normal_document")
check("la misura del documento non precede la rappresentabilità",
      "unrepresentable_items(doc)" in validate_src
      and validate_src.index("unrepresentable_items(doc)")
      < validate_src.index("max_bytes"),
      "misurare vuol dire serializzare, e un surrogato spaiato non è codificabile: "
      "il documento risultava «non un oggetto» con la causa vera persa per strada")

save_src = function_source(ROOT / "backend" / "app" / "inventory" / "repository.py",
                           "save")
check("la validazione del documento precede il lock della testa",
      "validate_normal_document" in save_src and "FOR UPDATE" in save_src
      and save_src.index("validate_normal_document") < save_src.index("FOR UPDATE"),
      "un documento rifiutato non deve lasciare stato NÉ aspettare un lock")

check("la mappa relazionale non riscrive la regola",
      "isfinite" not in rel and "copysign" not in rel
      and "is_representable" in rel,
      "due idee di «numero rappresentabile» divergerebbero sui casi limite, cioè "
      "proprio dove la regola serve")

digest_updates = []
for path in app_sources:
    lowered = path.read_text(encoding="utf-8-sig").lower()
    if "update inventory_versions" in lowered or "set canonical_sha256" in lowered:
        digest_updates.append(path.name)
check("nessuno riscrive il digest di una versione già registrata",
      not digest_updates,
      f"ricalcolarlo dopo che PostgreSQL ha cambiato il valore vorrebbe dire "
      f"registrare come «accettato» un documento diverso da quello inviato: "
      f"{digest_updates}")


# ==================================================================
# 13. password e Argon2id (§8.43)
# ==================================================================
#
#     Le password sono l'unico fattore di autenticazione: la loro politica e i
#     parametri di Argon2 sono dichiarazioni, e le dichiarazioni si controllano
#     leggendo il codice.
#
# Ciò che i test non possono provare, e che sta qui, è il NEGATIVO: che nessun
# altro punto costruisca un hasher con parametri propri, che la lista non venga
# consultata all'accesso, che non ricompaia un numero di lunghezza duplicato in
# un altro file. Sono tutte cose che un test verifica solo dove guarda, mentre
# qui si guarda ovunque.

pw_src = code_only(ROOT / "backend" / "app" / "auth" / "passwords.py")
pw_path = ROOT / "backend" / "app" / "auth" / "passwords.py"
service_src = code_only(ROOT / "backend" / "app" / "auth" / "service.py")

check("Argon2id è scelto esplicitamente, non per default",
      "Type.ID" in pw_src and "type=ARGON2_TYPE" in pw_src,
      "un default di libreria cambia quando cambiano le raccomandazioni, e un "
      "aggiornamento di dipendenza non deve poter spostare la sicurezza")
check("i parametri di Argon2 sono fissati nel codice",
      all(f"ARGON2_{n}" in pw_src for n in
          ("MEMORY_COST", "TIME_COST", "PARALLELISM", "HASH_LEN", "SALT_LEN"))
      and "memory_cost=ARGON2_MEMORY_COST" in pw_src,
      "pinnati, non ereditati")
check("i parametri superano il minimo richiesto (19456 KiB / t=2 / p=1)",
      "ARGON2_MIN_MEMORY_COST = 19456" in pw_src
      and "ARGON2_MIN_TIME_COST = 2" in pw_src
      and "ARGON2_MIN_PARALLELISM = 1" in pw_src,
      "le soglie sono costanti SEPARATE: se il test confrontasse i valori con se "
      "stessi, abbassarli resterebbe verde")

hashers = [p.name for p in app_sources
           if p.name != "passwords.py" and "PasswordHasher(" in code_only(p)]
check("un solo posto costruisce l'hasher delle password",
      not hashers,
      f"un secondo hasher significa due configurazioni, e la seconda è quella che "
      f"nessuno aggiorna: {hashers}")

check("il sale non viene scelto a mano",
      "salt=" not in pw_src and "token_bytes" not in pw_src,
      "lo genera la libreria per ogni hash e lo scrive dentro l'hash codificato: "
      "un sale globale o derivato dall'utenza renderebbe confrontabili fra loro gli "
      "hash di utenti diversi, che è ciò che il sale serve a impedire")
check("nessun pepper e nessun segreto di ambiente entra nel calcolo",
      "pepper" not in pw_src.lower() and "environ" not in pw_src
      and "getenv" not in pw_src,
      "un segreto operativo senza procedura di rotazione è un debito: il giorno in "
      "cui va cambiato, ogni hash esistente diventa inservibile (§8.43)")

hash_fn = function_source(pw_path, "hash_password")
verify_fn = function_source(pw_path, "verify_password")
check("la normalizzazione avviene DENTRO hash e verifica",
      "normalise(plain)" in hash_fn and "normalise(plain)" in verify_fn,
      "se fosse compito del chiamante, basterebbe un punto che se ne dimentica per "
      "rendere una password impossibile da riusare su un'altra piattaforma — e il "
      "difetto sarebbe intermittente e invisibile nei test ASCII")
# Dentro le funzioni che toccano la password, NON nel modulo: `blocklist()` fa
# `riga.strip()` sulle righe del FILE della lista, che è un'altra cosa. Cercare
# `.strip()` nel modulo intero è di nuovo il proxy che risponde alla domanda
# sbagliata — la stessa trappola documentata in `code_only`.
normalise_fn = function_source(pw_path, "normalise")
check("la regola non ripulisce la password",
      all(".strip()" not in fn and ".casefold()" not in fn
          for fn in (normalise_fn, hash_fn, verify_fn)),
      "uno spazio ai bordi può far parte della password: toglierlo vorrebbe dire "
      "accettare all'accesso una password diversa da quella impostata. Il casefold "
      "esiste SOLO nel confronto con la lista")
check("nessuna regola di composizione",
      not re.search(r"isupper|islower|isdigit|[Aa]-[Zz].*0-9", pw_src),
      "maiuscole e cifre obbligatorie producono `Estate2026!`: il lavoro lo fanno "
      "la lunghezza minima e la lista")
check("nessuna scadenza periodica delle password",
      not re.search(r"expiry|expires_days|max_age|scadenza", pw_src, re.I),
      "un cambio si impone dopo un evento — reset, provvisoria, sospetto di "
      "compromissione — non per calendario")

# --- la lista locale ---
blocklist_path = ROOT / "backend" / "app" / "auth" / "password-blocklist.txt"
check("la lista delle password vietate è un file locale",
      blocklist_path.is_file(),
      "in rete chiusa non c'è nessun servizio da interrogare")
check("la lista viaggia con l'immagine",
      blocklist_path.parent.name == "auth"
      and "COPY --chown=root:root app ./app" in
          (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8-sig"),
      "sta dentro `app/`, quindi la COPY che c'è già la porta: un percorso "
      "configurabile sarebbe un percorso che in produzione può puntare a un file "
      "assente, cioè un controllo che si disattiva in silenzio")
if blocklist_path.is_file():
    voci = [r.strip() for r in blocklist_path.read_text(encoding="utf-8").splitlines()
            if r.strip() and not r.strip().startswith("#")]
    check("la lista ha abbastanza voci lunghe da essere utile",
          len([v for v in voci if len(v) >= 15]) >= 150,
          f"con un minimo di 15 caratteri le classiche corte cadono già per "
          f"lunghezza: il lavoro lo fanno le voci lunghe, e ce ne sono "
          f"{len([v for v in voci if len(v) >= 15])}")
    check("la lista non ha voci duplicate dopo il casefold",
          len({v.casefold() for v in voci}) == len(voci),
          "una riga che non può mai corrispondere sembra proteggere e non protegge")

check("il confronto con la lista è di uguaglianza, non di sottostringa",
      " in blocklist()" in pw_src and "for voce in blocklist" not in pw_src,
      "cercare le voci DENTRO la password rifiuterebbe «il gatto dorme sul tetto» "
      "perché contiene «gatto»: colpirebbe le passphrase che la politica incoraggia")

# --- ordine dei controlli nel cambio password ---
change_fn = function_source(ROOT / "backend" / "app" / "auth" / "service.py",
                            "change_own_password")
check("il cambio verifica la password ATTUALE prima di giudicare la nuova",
      change_fn.index("verify_password") < change_fn.index("check_policy"),
      "al contrario, chi possiede una sessione ma non la password potrebbe sondare "
      "la politica — e la lista — leggendo i codici di errore")
check("il cambio applica la politica prima di scrivere",
      change_fn.index("check_policy") < change_fn.index("UPDATE users"),
      "una password rifiutata non deve lasciare nessuno stato")
check("il cambio rifiuta la password identica a quella attuale",
      "PASSWORD_UNCHANGED" in change_fn,
      "rimettere la provvisoria azzererebbe `must_change_pw` lasciando in uso il "
      "valore che l'amministratore ha comunicato a voce")
check("il cambio revoca tutte le sessioni",
      "revoke_all_sessions" in change_fn)

# --- l'accesso non consulta la lista ---
login_fn = function_source(ROOT / "backend" / "app" / "auth" / "service.py", "login")
check("l'accesso NON consulta la lista né la politica",
      "is_blocklisted" not in login_fn and "check_policy" not in login_fn,
      "risponderebbe «password_blocklisted» a chi prova, dicendogli due cose: che "
      "quel valore è in lista e che l'utenza esiste. E chi ha una password di prima "
      "della politica resterebbe bloccato fuori, senza via d'uscita")
check("la riscrittura dell'hash avviene solo dopo una verifica riuscita",
      "needs_rehash" in login_fn
      and login_fn.index("raise InvalidCredentials") < login_fn.index("needs_rehash"),
      "è l'unico momento in cui la password in chiaro esiste insieme a un hash "
      "verificato, e farlo prima significherebbe lavorare per chi prova a caso")
check("la riscrittura sta nella transazione della richiesta",
      "_out_of_band" not in login_fn.split("needs_rehash")[1],
      "o l'accesso riesce e l'hash è aggiornato, o non è cambiato niente: non "
      "esiste lo stato «hash nuovo, sessione mancante»")

# --- provvisorie ---
users_src = code_only(ROOT / "backend" / "app" / "auth" / "users.py")
check("le password provvisorie vengono da un CSPRNG con almeno 128 bit",
      "TEMP_PASSWORD_BYTES = 24" in pw_src and "secrets.token_urlsafe" in pw_src,
      "24 byte = 192 bit. `random` è deterministico e non va mai vicino a una "
      "credenziale")
check("la generazione della provvisoria vive accanto alla politica",
      "TEMP_PASSWORD_BYTES" not in users_src
      and "generate_temporary_password" in users_src,
      "erano 12 byte — 96 bit, sotto il minimo — proprio perché quel numero stava "
      "lontano dalla lunghezza minima delle password")
check("la provvisoria generata passa dalla politica",
      "check_policy(temp)" in pw_src,
      "lega per costruzione quanto è lunga una provvisoria e quanto deve essere "
      "lunga una password")

def class_source(path, name):
    """Il corpo di UNA classe, senza docstring. Come `function_source`.

    Serve per i modelli di ingresso: cercare `password` nel file di `api/users.py`
    trova `temporaryPassword` nella risposta e `reset-password` nel percorso di una
    rotta, che sono entrambi corretti. La domanda è un'altra — se un modello di
    INGRESSO abbia un campo password — e si può porre solo sulla classe.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                del body[0]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    return ""


users_api_path = ROOT / "backend" / "app" / "api" / "users.py"
ingressi = {n: class_source(users_api_path, n) for n in ("CreateIn", "UpdateIn")}
check("le rotte non accettano una password scelta dall'amministratore",
      all(src and "password" not in src.lower() for src in ingressi.values()),
      "la genera il server: nessun campo del contratto può indebolirla")

# --- il numero della lunghezza minima esiste in un posto solo ---
min_length = int(re.search(r"^MIN_LENGTH = (\d+)", pw_src, re.M).group(1))
check("la lunghezza minima è 15 code point",
      min_length == 15)
# Sulla CLASSE, non sul file: `LoginIn` usa `min_length=1` sullo username, che è
# un limite di dimensione e non una regola di politica.
password_in = class_source(ROOT / "backend" / "app" / "api" / "auth.py", "PasswordIn")
check("la rotta non duplica la lunghezza minima",
      bool(password_in) and "min_length" not in password_in
      and "max_length" in password_in,
      "pydantic risponderebbe con la propria forma invece del codice stabile, e i "
      "due numeri divergerebbero — il 10 rimasto nella rotta ne era la prova. "
      "`max_length` resta: è un limite di dimensione su un input non attendibile, "
      "molto sopra il massimo consentito, perché a rifiutare deve essere la politica")
frontend = (ROOT / "handoff" / "Sala Server v2.dc.html").read_text(encoding="utf-8")
check("il suggerimento del frontend coincide con la politica",
      f"min. {min_length} caratteri" in frontend
      and f"nuova.length < {min_length}" in frontend
      and "min. 10 caratteri" not in frontend and "(min 8)" not in frontend,
      f"un numero diverso nell'interfaccia fa vedere all'utente un errore che il "
      f"client non aveva previsto (atteso {min_length})")

# --- che cosa si conserva ---
migrations = "\n".join(
    p.read_text(encoding="utf-8-sig")
    for p in sorted((ROOT / "backend" / "migrations" / "versions").glob("*.py")))
colonne_sospette = re.findall(
    r'Column\(\s*"((?:[a-z_]*(?:salt|pepper|plain)[a-z_]*|password(?!_hash)[a-z_]*))"',
    migrations)
check("nel database c'è solo password_hash",
      not colonne_sospette,
      f"il sale sta dentro l'hash codificato e non è un segreto: una colonna in più "
      f"sarebbe un posto dove qualcuno, un giorno, scrive la cosa sbagliata "
      f"{colonne_sospette}")
sanitize_src = code_only(ROOT / "backend" / "app" / "audit" / "sanitize.py")
# Senza le virgolette esterne: `ast.unparse` normalizza i letterali di stringa, e
# `"password"` nel sorgente riappare come `'password'`. È scritto nella docstring di
# `code_only` e ci sono cascato comunque.
check("la ripulitura dell'audit tratta ancora password e hash come sensibili",
      all(t in sanitize_src for t in ("password", "hash", "argon2")),
      "un hash in un registro consultabile è attaccabile offline")
check("il bootstrap valida la password che riceve dall'ambiente",
      "check_policy" in code_only(ROOT / "backend" / "scripts" / "bootstrap.py"),
      "è la strada che crea il PRIMO amministratore, e `os.environ` decodifica con "
      "surrogateescape: un byte non UTF-8 diventa un surrogato spaiato")
check("il bootstrap non contiene una password predefinita",
      not re.search(r'(admin|password)\s*=\s*["\'](admin|password)',
                    code_only(ROOT / "backend" / "scripts" / "bootstrap.py")),
      "il database di produzione non deve nascere con `admin / admin` (§8.43)")


# ==================================================================
# 14. fase 2C: la scrittura è doppia, la lettura no (§8.44)
# ==================================================================
#
#     Dopo ogni PUT riuscito le due rappresentazioni sono allineate, e non esiste uno
#     stato committato in cui una è avanzata e l'altra no.
#
# I test lo provano dal comportamento, su PostgreSQL vero, con nove mutazioni che
# dimostrano che sanno fallire. Qui si copre il NEGATIVO — che nessun altro percorso
# scriva la proiezione, che `GET` non sia passato a SQL, che il worker non sia passato
# alle colonne derivate — cioè le cose che un test verifica solo dove guarda.

repo_src = code_only(ROOT / "backend" / "app" / "inventory" / "repository.py")
proj_src = code_only(ROOT / "backend" / "app" / "inventory" / "projection.py")

check("il repository sincronizza la proiezione nel salvataggio",
      "projection.synchronise" in repo_src,
      "è il senso della fase 2C: le due rappresentazioni si muovono insieme")
check("il repository pretende una proiezione attuale PRIMA di scrivere",
      "projection.require_current" in repo_src,
      "una proiezione vecchia non si ripara di nascosto al primo salvataggio di un "
      "utente qualunque: quello cancellerebbe l'unica traccia del disallineamento")

# L'ORDINE dentro la funzione, non nel file: `save` contiene sia la precondizione sia
# il ritorno del no-op, e cercarli nel modulo direbbe solo che esistono entrambi.
save_fn = function_source(ROOT / "backend" / "app" / "inventory" / "repository.py",
                          "save")


def ordered(source: str, first: str, second: str) -> bool:
    """`first` compare prima di `second`, e ENTRAMBI compaiono.

    ⚠ Il guardiano di presenza non è pedanteria. Con `source.index(...)` nudo, un
    frammento SPARITO fa sollevare `ValueError` e lo strumento muore con un traceback
    invece di stampare un `[FAIL]` leggibile: l'invariante è violata e chi legge
    l'output vede un guasto della sonda. Trovato mutando `require_current` via da
    `repository.py` — il controllo «reagiva», ma nel modo sbagliato.
    """
    return first in source and second in source \
        and source.index(first) < source.index(second)


check("la precondizione precede il ritorno del no-op",
      ordered(save_fn, "require_current", "created=False"),
      "un no-op è una risposta di SUCCESSO: restituirla con la proiezione vecchia "
      "direbbe «tutto in ordine» da un backend che ha smesso di mantenerla")
check("la precondizione segue il lock della testa",
      ordered(save_fn, "FOR UPDATE", "require_current"),
      "senza il lock, la domanda «la proiezione rispecchia la testa?» riguarderebbe "
      "una testa che può cambiare prima della scrittura")
check("la sincronizzazione segue l'inserimento della versione",
      ordered(save_fn, "_insert_version", "synchronise"),
      "la proiezione dichiara una versione, e quella versione deve esistere")
check("la testa si sposta per ULTIMA",
      ordered(save_fn, "synchronise", "_update_head"),
      "la testa è il punto di serializzazione: spostarla prima renderebbe visibile "
      "una versione la cui proiezione non è ancora stata dimostrata")
check("il repository non fa commit: la transazione è del chiamante",
      ".commit()" not in repo_src,
      "è il ROLLBACK il meccanismo di atomicità, non l'ordine degli statement")

# --- un solo corpo di verifica per i due scrittori ---
sync_fn = function_source(ROOT / "backend" / "app" / "inventory" / "projection.py",
                          "synchronise")
rebuild_fn = function_source(ROOT / "backend" / "app" / "inventory" / "projection.py",
                             "rebuild")
check("la sincronizzazione rilegge da SQL e confronta tutto",
      all(t in sync_fn for t in ("read_model", "model_differences", "validate_model",
                                 "assemble", "canonical_sha256")),
      "un popolamento «che sembra andato bene» non vale niente")
check("la ricostruzione usa lo STESSO corpo del salvataggio",
      "synchronise(" in rebuild_fn
      and all(t not in rebuild_fn for t in ("model_differences", "read_model")),
      "due verifiche separate divergono, e quella che resta indietro è quella sul "
      "percorso delle richieste — cioè quella che protegge i dati degli utenti")
check("la sincronizzazione svuota invece di troncare",
      "clear(conn)" in sync_fn and "TRUNCATE" not in proj_src,
      "`TRUNCATE` prende un lock ACCESS EXCLUSIVE che bloccherebbe anche i lettori "
      "della fase 2D, e richiede un privilegio che non si è concesso")

# --- la versione della mappa ---
rel_src = code_only(ROOT / "backend" / "app" / "inventory" / "relational.py")
check("la mappa dichiara la propria versione",
      "MAPPER_VERSION" in rel_src,
      "la proiezione è una derivata, e una derivata è valida solo rispetto al codice "
      "che l'ha prodotta: se un campo passasse da `extra` a una colonna, le righe "
      "vecchie riassemblerebbero lo stesso documento — stesso digest, nessun "
      "allarme — con i dati nel posto sbagliato")
check("lo stato registra la versione della mappa, e la si controlla",
      "mapper_version" in proj_src and "MAPPER_VERSION" in proj_src,
      "registrarla senza controllarla sarebbe una dichiarazione senza garanzia")

mig12_path = ROOT / "backend" / "migrations" / "versions" / "0012_dual_write.py"
check("esiste la migrazione della fase 2C", mig12_path.is_file())
mig12 = mig12_path.read_text(encoding="utf-8") if mig12_path.is_file() else ""
mig12_code = code_only(mig12_path) if mig12_path.is_file() else ""
check("la 0012 concede all'API la DML sulla proiezione",
      "GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {API_ROLE}" in mig12,
      "è il privilegio che la 0010 aveva rimandato a questa fase")
check("la 0012 NON concede TRUNCATE",
      "REVOKE TRUNCATE ON {table} FROM {API_ROLE}" in mig12
      and "GRANT TRUNCATE" not in mig12 and "GRANT ALL" not in mig12,
      "un privilegio che non serve è un privilegio che può essere sfruttato (§8.19)")
# ⚠ Sul CODICE e non sul testo, e senza scorciatoie: la prima stesura di questo
# controllo aveva un `or "FROM {WORKER_ROLE}" in mig12` come ripiego, che lo rendeva
# quasi sempre vero — la stringa compare anche nella `downgrade`. Un controllo che
# passa perché una parola esiste da qualche parte non è un controllo.
worker_grants = [riga for riga in mig12_code.splitlines()
                 if "GRANT" in riga and "WORKER_ROLE" in riga]
check("la 0012 concede al worker solo SELECT sulla proiezione",
      worker_grants and all("GRANT SELECT ON" in riga and "INSERT" not in riga
                            and "UPDATE" not in riga and "DELETE" not in riga
                            for riga in worker_grants),
      f"il worker non scrive l'inventario, e in fase 2C nemmeno la sua proiezione: "
      f"{worker_grants}")
check("la 0012 revoca esplicitamente la scrittura al worker",
      "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON " in mig12
      and "WORKER_ROLE" in mig12,
      "le REVOKE esplicite mettono l'intenzione nello schema invece che in un "
      "commento")
check("la 0012 ribadisce che l'istantanea immutabile resta immutabile",
      "REVOKE UPDATE, DELETE, TRUNCATE ON inventory_versions" in mig12,
      "in fase 2C acquista un secondo mestiere — è il riferimento contro cui la "
      "proiezione si verifica — e poterla riscrivere renderebbe quella verifica una "
      "tautologia")
check("la 0012 aggiunge `mapper_version` senza inventare un valore",
      'sa.Column("mapper_version", sa.Integer)' in mig12
      and "server_default" not in mig12.split("mapper_version")[1][:400],
      "le righe della fase 2B non dichiarano nessuna mappa e noi non sappiamo quale "
      "le ha scritte: NULL è la verità, e fa fallire chiuso il controllo")
check("la 0012 non è una migrazione di dati",
      all(t not in mig12_code.lower() for t in ("normalise(", "rebuild(", "synchronise(",
                                                "insert into inventory_")),
      "la ricostruzione resta un comando esplicito del proprietario, che una persona "
      "esegue e di cui LEGGE l'esito")

# --- ciò che la fase 2C NON faceva, e che la 2D fa (vedi §15) ---
inventory_route = code_only(INVENTORY_ROUTE)
worker_sources = "".join(
    code_only(p) for p in sorted((ROOT / "backend" / "app" / "notifications").rglob("*.py")))
# ⚠ ROVESCIATO nella fase 2F. Diceva: «lo scanner delle scadenze non è passato alle
# colonne derivate», e nella 2C e nella 2D era la verifica giusta — il passaggio non
# doveva essere un effetto collaterale. Adesso è avvenuto, di proposito e col suo
# commit (§8.47), quindi la domanda utile si è invertita: le colonne derivate esistono
# perché QUALCUNO le usi, e la 2C le aveva create per questo. Un controllo che
# pretendesse ancora il contrario terrebbe in vita una decisione già presa.
check("le colonne data derivate della 2C hanno trovato il loro lettore",
      "garanzia_date" in worker_sources and "supporto_date" in worker_sources,
      "erano state introdotte nella 2C senza nessun lettore, e un dato che nessuno "
      "legge è un dato che si scopre sbagliato tardi. Dalla 2F le legge il worker")
check("nessun endpoint di query è stato aggiunto",
      not [p.name for p in sorted((ROOT / "backend" / "app" / "api").rglob("*.py"))
           if "query" in p.name or "search" in p.name],
      "fuori dallo scopo di questo commit")
check("il frontend continua a non sapere che la proiezione esista",
      all(t not in handoff for t in NORMALISED_TABLES)
      and "garanzia_date" not in handoff and "mapper_version" not in handoff,
      "il contratto del frontend è il documento (§8.22)")

check("l'errore della precondizione ha un codice stabile e diventa 503",
      "projection_not_current" in code_only(
          ROOT / "backend" / "app" / "inventory" / "errors.py")
      and "ProjectionNotCurrentError" in code_only(
          ROOT / "backend" / "app" / "api" / "errors.py"),
      "la richiesta era valida: è il backend che si rifiuta di operare, e chi legge "
      "il log di un 503 deve poter sapere quale comando lo risolve")


# ==================================================================
# 15. fase 2D: la LETTURA viene da SQL, e non c'è ripiego (§8.45)
# ==================================================================
#
#     `GET /api/inventory` restituisce il documento riassemblato dalle tabelle
#     normalizzate. L'istantanea JSONB resta la storia e il GIUDICE, non un ripiego.
#
# I test lo provano dal comportamento su PostgreSQL vero, manomettendo l'istantanea e
# corrompendo le tabelle. Qui si copre ciò che un test non sa provare, e in questa
# fase la differenza è particolarmente netta: un ripiego sull'istantanea produrrebbe
# la risposta GIUSTA in tutti i casi normali. Nessun test di comportamento
# distinguerebbe «legge le tabelle» da «legge le tabelle, e se qualcosa non torna
# legge il JSON» — l'unico modo è guardare il codice.

def signature_source(path, name) -> str:
    """Solo la FIRMA di una funzione. `function_source` restituisce il corpo.

    Serve perché due proprietà di questa fase stanno nella firma e non nel corpo: da
    quali dipendenze la rotta dipende, e in che ordine sono dichiarate.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.unparse(node.args)
    return ""


def handlers_never_return(path, name) -> bool | None:
    """Nessun `except` di questa funzione RESTITUISCE qualcosa. `None` se non c'è.

    ⚠ È il controllo che un test di comportamento non sa fare, e in questa fase è il
    più importante di tutti. Cercare la parola `except` non serve: il gestore che c'è
    è la mappa degli errori (`raise http_error_for`), e deve esserci. Ciò che non deve
    esistere è un gestore che, invece di sollevare, RESTITUISCE un documento — cioè un
    ripiego sull'istantanea. Quel ripiego produrrebbe la risposta giusta in tutti i
    casi normali, quindi nessun test lo distinguerebbe dal comportamento corretto.

    `None` invece di `True` quando la funzione non esiste: un controllo che passa
    perché non ha trovato niente da guardare è il modo di non accorgersi di un
    rinominamento.
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            for handler in (h for n in ast.walk(node)
                            if isinstance(n, ast.ExceptHandler) for h in [n]):
                if any(isinstance(s, ast.Return) for s in ast.walk(handler)):
                    return False
            return True
    return None


errors_src = code_only(ROOT / "backend" / "app" / "inventory" / "errors.py")
api_errors_src = code_only(ROOT / "backend" / "app" / "api" / "errors.py")
deps_src = code_only(ROOT / "backend" / "app" / "api" / "deps.py")
get_fn = function_source(INVENTORY_ROUTE, "get_inventory")
get_sig = signature_source(INVENTORY_ROUTE, "get_inventory")
current_fn = function_source(
    ROOT / "backend" / "app" / "inventory" / "projection.py", "current_document")

# --- la fonte è la proiezione ---
#
# ⚠ Due condizioni, non una: la rotta la CHIAMA e la funzione ESISTE. Cercare soltanto
# la chiamata lasciava passare la rinominazione della funzione — il controllo restava
# verde perché la rotta continuava a nominare qualcosa che non c'era più. Gli altri
# controlli di questa sezione fallivano (leggono `current_fn`, che diventa vuoto),
# quindi lo strumento nel suo insieme reagiva; ma il controllo che dice «la lettura
# viene dalla proiezione» diceva sì. È lo stesso difetto del guardiano di presenza in
# `ordered()`, trovato allo stesso modo: mutando.
check("il GET dell'inventario riassembla dalla proiezione",
      bool(current_fn) and "projection.current_document" in get_fn,
      "è il senso della fase 2D: le tabelle normalizzate sono lo stato corrente "
      "autorevole, e il documento si costruisce da loro")
check("il GET non legge più il documento immutabile",
      all(t not in get_fn for t in ("get_current(", "get_version(", "snapshot.doc",
                                    "InventoryRepository")),
      "restituire l'istantanea era la fase 2C. Lasciarne la strada aperta "
      "significherebbe poterci ripiegare in caso di dubbio, e il ripiego nasconde "
      "esattamente il difetto che la fase 2 esiste per scoprire")
# ⚠ Dalla fase 2E la lettura della testa sta in `require_current_head`, che
# `current_document` e le tre interrogazioni condividono: il frammento cercato si è
# SPOSTATO, e cercarlo solo in `current_document` faceva fallire il controllo per un
# rifattorizzamento che non ha cambiato nessuna proprietà. Si guardano entrambe.
head_fn = function_source(ROOT / "backend" / "app" / "inventory" / "projection.py",
                         "require_current_head")
#: ⚠ SPOSTATO ancora, nella fase 2F: attualità, `read_model` e `validate_model` sono
#: usciti da `current_document` ed entrati in `require_valid_model`, che adesso il `GET`
#: e il **worker** condividono (§8.47.4). I controlli qui sotto guardano la funzione
#: dove il codice VIVE, più la delega da `current_document`: cercarlo solo nel
#: chiamante farebbe fallire un controllo per un rifattorizzamento che non ha cambiato
#: nessuna proprietà — è già successo due volte, ed è la ragione di questa nota.
valid_fn = function_source(ROOT / "backend" / "app" / "inventory" / "projection.py",
                           "require_valid_model")
precondizione = current_fn + chr(10) + valid_fn
check("nemmeno la funzione di lettura deserializza l'istantanea",
      bool(head_fn)
      and "SELECT canonical_sha256 FROM inventory_versions" in head_fn
      and all("doc FROM inventory_versions" not in f for f in (current_fn, head_fn))
      and all(".doc" not in f for f in (current_fn, head_fn)),
      "di `inventory_versions` legge il DIGEST, che è metadato e serve da giudice. "
      "Il documento no: se lo avesse in mano potrebbe restituirlo")
check("il riassemblaggio passa dalla mappa già provata",
      all(t in precondizione for t in ("read_model", "assemble", "canonical_sha256",
                                       "validate_model", "require_current")),
      "nessuna seconda implementazione della lettura: quella che c'è è provata da "
      "`test_relational_mapper.py` e dalla scrittura doppia")

# L'ORDINE dentro la funzione: la precondizione di attualità costa tre query, il
# riassemblaggio costa tutto il resto. Ma soprattutto è ciò che separa i due codici
# d'errore, e chi opera legge quello per sapere se `--rebuild` è la risposta.
check("l'attualità si pretende PRIMA di leggere le righe",
      ordered(valid_fn, "require_current", "read_model"),
      "farlo dopo confonderebbe «la proiezione non è mantenuta» (rimedio: "
      "`--rebuild`) con «la proiezione mente» (rimedio: indagare, NON ricostruire)")
check("la coerenza del modello si controlla PRIMA di servire",
      ordered(valid_fn, "read_model", "validate_model")
      and ordered(current_fn, "require_valid_model", "return CurrentDocument"),
      "è l'unico controllo che vede le colonne DERIVATE, a cui il digest è cieco: "
      "senza, una `garanzia_date` sbagliata uscirebbe indisturbata")
check("il GET delega la precondizione invece di riscriverla",
      "require_valid_model" in current_fn
      and "validate_model(" not in current_fn
      and "require_current_head" not in current_fn,
      "dalla 2F la stessa sequenza serve al worker: due copie divergono, e a divergere "
      "sarà quella che nessuno guarda (§8.47.4)")
check("il digest si confronta con DUE riferimenti indipendenti",
      "!= recorded" in current_fn and "!= declared.projected_sha256" in current_fn,
      "quello registrato nell'istantanea e quello che la proiezione dichiara di aver "
      "verificato: se un giorno l'attualità si allentasse, questo reggerebbe da solo")

# --- nessun ripiego, in nessuna forma ---
check("nessun gestore d'errore del GET restituisce un documento",
      handlers_never_return(INVENTORY_ROUTE, "get_inventory") is True,
      "il gestore che c'è DEVE esserci — è la mappa degli errori — ma se invece di "
      "sollevare restituisse l'istantanea, l'applicazione sembrerebbe funzionante e "
      "la fase 2 sarebbe inutile: l'utente vedrebbe l'inventario giusto e nessuno "
      "aprirebbe un ticket. Nessun test di comportamento lo distinguerebbe")
check("il pacchetto inventory continua a NON riesportare `projection`",
      "projection" not in code_only(
          ROOT / "backend" / "app" / "inventory" / "__init__.py"),
      "chi le serve la importa per nome, così un import scritto per distrazione "
      "resta impossibile da un modulo qualunque")

# --- lo snapshot coerente, che è il cuore della concorrenza ---
# ⚠ Senza le virgolette esterne: `ast.unparse` normalizza i letterali di stringa, e
# cercare `isolation_level="REPEATABLE READ"` falliva perché la ricostruzione scrive
# gli apici singoli. È la trappola documentata in `code_only`, e ci sono cascato di
# nuovo — quindi vale la pena che resti scritto accanto al controllo.
#: ⚠ SPOSTATO nella fase 2F: l'isolamento non si dichiara più nella dipendenza
#: FastAPI ma in `db.read_snapshot`, perché i lettori della proiezione sono diventati
#: due processi (l'API e il worker) e due dichiarazioni divergono in silenzio. La
#: dipendenza resta, e resta controllata: è il pezzo che l'API ha in più, cioè il ciclo
#: di vita legato alla richiesta.
snapshot_fn = function_source(ROOT / "backend" / "app" / "db.py",
                              "read_snapshot")
snapshot_dep = function_source(ROOT / "backend" / "app" / "api" / "deps.py",
                               "snapshot_connection")
check("la lettura ha una connessione dedicata in REPEATABLE READ, READ ONLY",
      "isolation_level=" in snapshot_fn and "REPEATABLE READ" in snapshot_fn
      and "postgresql_readonly=True" in snapshot_fn,
      "il GET fa sette letture: sotto READ COMMITTED un PUT che committa nel mezzo "
      "produrrebbe un documento fatto di due versioni, o un 503 spurio a fronte di "
      "attività normale")
check("la rotta apre lo snapshot NEL CORPO, dopo l'autenticazione",
      "with reader() as" in get_fn
      and "Depends(get_snapshot_reader)" in get_sig
      and "Depends(get_connection)" not in get_sig,
      "una richiesta anonima non deve aprire una transazione sul database prima di "
      "scoprire di essere un 401: è la richiesta che arriva a raffica. E la rotta non "
      "dipende più dalla connessione della richiesta, che sarebbe inutilizzabile")
check("l'attore è dichiarato PRIMA della fabbrica dello snapshot",
      ordered(get_sig, "require_actor", "get_snapshot_reader"),
      "FastAPI risolve le dipendenze nell'ordine della firma: invertirle farebbe "
      "risolvere la fabbrica prima dell'autenticazione")
check("la lettura NON prende lock sulla testa",
      "FOR UPDATE" not in current_fn,
      "una lettura che bloccasse le scritture trasformerebbe l'apertura di una "
      "pagina in un ritardo per chi sta salvando")
check("lo snapshot prende la connessione da un POOL SEPARATO",
      "get_read_engine()" in snapshot_fn and "get_engine()" not in snapshot_fn,
      "un GET tiene due connessioni insieme: prenderle dallo stesso pool è "
      "un'acquisizione a due fasi, e con quindici GET simultanei si blocca trenta "
      "secondi e poi scade. Vedi l'aritmetica in testa a `app/db.py`")
read_engine_fn = function_source(ROOT / "backend" / "app" / "db.py",
                                "get_read_engine")
check("i due engine sono davvero due, con memoizzazioni separate",
      "create_engine(" in read_engine_fn and "_read_engine" in read_engine_fn
      and "get_engine()" not in read_engine_fn,
      "restituire lo stesso oggetto da entrambe le funzioni riporterebbe lo stallo "
      "lasciando intatti i nomi — che è il modo in cui una protezione muore senza "
      "che nessuno la veda morire")
check("la connessione della richiesta non viene promossa a REPEATABLE READ",
      "isolation_level" not in function_source(
          ROOT / "backend" / "app" / "api" / "deps.py", "get_connection"),
      "il PUT si serializza con `SELECT ... FOR UPDATE` e traduce il perdente in un "
      "409 pulito; in REPEATABLE READ prenderebbe invece un errore di "
      "serializzazione del database (§8.11)")

# --- i due codici d'errore ---
check("l'incoerenza ha un codice STABILE e distinto",
      "projection_inconsistent" in errors_src
      and "ProjectionInconsistentError" in api_errors_src,
      "non attuale e incoerente hanno rimedi opposti: il primo si risolve con "
      "`--rebuild`, il secondo NO — ricostruire cancellerebbe le prove")
check("entrambi i rifiuti sono 503 e non 4xx",
      ordered(api_errors_src, "ProjectionNotCurrentError",
              "HTTP_503_SERVICE_UNAVAILABLE")
      and ordered(api_errors_src, "ProjectionInconsistentError",
                  "HTTP_503_SERVICE_UNAVAILABLE"),
      "la richiesta era valida: è il backend a non essere in grado di servirla")
check("i dettagli dell'incoerenza restano nei log",
      "log.error" in api_errors_src.split("ProjectionInconsistentError")[-1][:800],
      "i dettagli portano i digest e, in caso di modello incoerente, i nomi dei "
      "campi e gli `uid`: frammenti dell'inventario di un cliente (§8.21)")

# --- la readiness NON è diventata il GET ---
check("la readiness resta il confronto fra valori registrati",
      "projection.currency(conn).current" in readiness_src
      and all(t not in readiness_src for t in ("current_document", "read_model",
                                               "assemble")),
      "la sonda gira ogni pochi secondi per sempre; il GET una volta per richiesta. "
      "La separazione dei tre costi è voluta (§12)")

# --- gli strumenti del proprietario restano indipendenti ---
project_src = code_only(ROOT / "backend" / "scripts" / "project.py")
check("`project.py --verify` non è «chiama il GET»",
      "current_document" not in project_src
      and all(t not in project_src for t in ("requests", "urllib", "httpx",
                                             "/api/inventory")),
      "lo strumento operativo deve funzionare anche quando il GET non funziona: se "
      "dipendesse dalla rotta, nel guasto che conta non si potrebbe usare")
check("la verifica controlla anche l'ORACOLO, non solo l'imputato",
      "recomputed != snapshot[1]" in proj_src,
      "dalla fase 2D l'istantanea non è solo storia: è il riferimento contro cui "
      "ogni GET si misura. Un `UPDATE` a mano su `doc` che lasciasse intatto il "
      "digest passerebbe una verifica che non guarda il giudice")

# --- ciò che la fase 2D NON fa ---
# ⚠ ROVESCIATO nella fase 2F, come il gemello della §14. Adesso il worker NON deve
# più leggere il documento, e questo è il controllo che lo pretende.
check("lo scanner del worker non legge più l'istantanea immutabile",
      "get_current" not in worker_sources
      and "InventoryRepository" not in worker_sources,
      "dalla 2F la sorgente è la proiezione (§8.47); un ripiego sul documento "
      "coprirebbe il difetto di coerenza che la fase 2 esiste per scoprire")
check("non è stato aggiunto nessun endpoint di ricerca o di capacità",
      not [p.name for p in sorted((ROOT / "backend" / "app" / "api").rglob("*.py"))
           if any(t in p.name for t in ("query", "search", "capacity", "expiry"))],
      "fuori dallo scopo di questo commit")
check("il frontend continua a non sapere che la proiezione esista",
      all(t not in handoff for t in NORMALISED_TABLES)
      and all(t not in handoff for t in ("garanzia_date", "supporto_date",
                                         "mapper_version", "projection")),
      "il contratto del frontend è il documento (§8.22), e la 2D non lo cambia")
check("la risposta del GET ha ancora le quattro chiavi di sempre",
      all(t in class_source(INVENTORY_ROUTE, "InventoryOut")
          for t in ("version", "schemaVersion", "sha256", "doc")),
      "cambiare la fonte non è cambiare il contratto")


# ==================================================================
# 16. fase 2E: tre interrogazioni, e la semantica di prima (§8.46)
# ==================================================================
#
# I test di parità provano che lo SQL risponde come il frontend, su 29 corpora. Qui si
# copre ciò che un test di comportamento non vede: che non sia comparso un endpoint che
# esegue query arbitrarie, che il worker e il frontend non siano stati toccati, che le
# attese di parità vengano davvero dal JavaScript che gira, e che il modulo delle query
# non abbia preso scorciatoie che cambiano il risultato.

QUERIES = ROOT / "backend" / "app" / "inventory" / "queries.py"
QUERY_ROUTES = ROOT / "backend" / "app" / "api" / "queries.py"
queries_src = code_only(QUERIES)
query_routes_src = code_only(QUERY_ROUTES)
DOMAIN = ROOT / "backend" / "app" / "domain.py"


def _assign_literal(path, name):
    """Il valore di un'assegnazione letterale a livello di modulo, letto con `ast`.

    ⚠ Serve perché questo strumento NON importa l'applicazione: gira senza database,
    senza dipendenze e prima di qualunque cosa. Leggere l'elenco dei campi con una
    regex avrebbe funzionato finché nessuno lo riformatta; `ast` legge il VALORE.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    return None


def _domain_fields():
    """I campi cercabili dichiarati dal CONTRATTO."""
    return sorted(_assign_literal(DOMAIN, "DEVICE_SEARCH_FIELDS") or ())


def _sql_search_fields():
    """I campi che la traduzione SQL sa tradurre.

    ⚠ Si confrontano i due INSIEMI, e non si elencano i nomi qui: un elenco copiato in
    questo file sarebbe un terzo elenco, e la fase 2G esiste per non averne due.
    """
    import ast

    tree = ast.parse(QUERIES.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DEVICE_SEARCH_SQL"
                for t in node.targets):
            return [k.value for k in node.value.keys]
    return []

check("esistono le tre interrogazioni, e sono tre",
      all(f"def {nome}(" in queries_src
          for nome in ("search", "capacity", "expiries")),
      "ricerca, capacità e scadenze: le tre famiglie che la fase 2 doveva sostenere")

# --- la semantica della ricerca ---
search_fn = function_source(QUERIES, "search")
rows_fn = function_source(QUERIES, "_search_rows")
# ⚠ Su `queries_src` e non su `rows_fn`: dalla 2G il confronto è in `_text_match`,
# che è una funzione di supporto. Cercarlo nel corpo di `_search_rows` lo dichiarava
# assente — un controllo rosso perché il codice è stato riordinato, non perché è
# sbagliato, è un controllo che qualcuno finisce per cancellare.
check("la ricerca testuale usa `strpos`, non `LIKE`",
      "strpos(lower(" in queries_src and "LIKE" not in queries_src,
      "`LIKE` attribuisce un significato a `%` e `_`, che in una casella di ricerca "
      "sono caratteri normali: una query contenente `%` troverebbe tutto")
check("la ricerca NON è tokenizzata",
      all(t not in queries_src for t in ("to_tsvector", "to_tsquery", "tsquery",
                                         "plainto_", "websearch_", "@@")),
      "la ricerca a testo pieno di PostgreSQL non trova `SRV-Web-01` cercando `web`: "
      "sarebbe una ricerca «migliore» che perde risultati")
# ⚠ ROVESCIATO nella 2G: erano CINQUE, e la voce 4 del registro diceva che `id`,
# `type` e `stato` non si cercavano «e nessuna interfaccia lo dice». Adesso sono NOVE.
# Le `note` restano fuori PER DECISIONE: testo libero e lungo, che renderebbe qualunque
# parola comune un risultato di massa.
#
# Il controllo non elenca i nomi: li prende dal CONTRATTO. Un elenco copiato qui
# sarebbe un secondo elenco, e questa fase esiste per non averne due.
check("i campi cercati sui dispositivi sono quelli del contratto",
      _domain_fields() == sorted(_sql_search_fields()),
      f"il contratto dichiara {_domain_fields()} e la traduzione SQL copre "
      f"{sorted(_sql_search_fields())}: un campo dichiarato e non tradotto non si "
      f"cerca, e nessuno se ne accorge finché un utente non lo prova")
check("le note NON sono fra i campi cercabili",
      "note" not in _domain_fields() and "d.note" not in rows_fn,
      "decisione della 2G: sono testo libero e lungo, e includerle renderebbe "
      "qualunque parola comune un risultato di massa")
check("stato, presenza e tipo si cercano PASSANDO DAL DEFAULT",
      "_falsy_sql(" in queries_src
      and all(f'"d.{c}"' in queries_src or f"'d.{c}'" in queries_src
              for c in ("stato", "presenza", "type")),
      "un dispositivo senza `stato` è `attivo` e va trovato cercando «attivo»: senza "
      "il default lo troverebbe solo chi ha scritto la parola nel documento, cioè una "
      "parte arbitraria dell'inventario")
check("i rack si cercano su codice, nome e seriali",
      "k.code" in queries_src and "k.name" in queries_src
      and "k.seriali" in queries_src)
check("i seriali si cercano anche quando sono finiti in `extra`",
      "jsonb_array_elements" in queries_src and "extra -> 'seriali'" in queries_src,
      "un rack i cui `seriali` contengono un numero porta l'intero array in `extra`, e "
      "nella 2E quei seriali non si trovavano — mentre l'utente li vedeva sullo "
      "schermo. Una risposta sbagliata, non una stranezza")
# Senza virgolette esterne: `ast.unparse` normalizza i letterali (la trappola
# documentata in `code_only`, e ci sono ricascato).
check("in modalità intervallo IP i rack non partecipano",
      "rack_where = " in rows_fn and "FALSE" in rows_fn,
      "il frontend scrive `if (!ipRange && (rk.id...))`: quando la query è una rete, i "
      "rack sono esclusi per costruzione")

# --- nessun `inet`, che è la scorciatoia più tentatrice ---
# ⚠ ROVESCIATI nella 2G, tutti tre, e vale la pena dire perché il divieto era giusto
# allora e sbagliato adesso.
#
# La 2E vietava `inet` perché ha una GRAMMATICA PROPRIA — accetta `10.1` come
# `10.0.0.1` e `10.0.0.0/8` come indirizzo — e usarla avrebbe aggiunto semantica in un
# solo posto dei tre. Quell'obiezione riguardava l'idea di far interpretare a
# PostgreSQL il testo dell'utente, e resta valida.
#
# Nella 2G non succede: la colonna `ip_addr` la scrive `domain.parse_address`, l'unico
# interprete del prodotto, e in PostgreSQL arriva solo la forma canonica. Il database
# CONFRONTA indirizzi già normalizzati, che è quello che sa fare meglio di qualunque
# espressione — e con `>=`/`<=` su `inet` le famiglie non si mescolano per costruzione.
# ⚠ Apostrofi e non virgolette: `code_only` passa da `ast.unparse`, che NORMALIZZA i
# letterali di stringa. È la trappola documentata in `code_only`, e ci sono ricascato
# una terza volta.
check("la colonna dell'indirizzo è DERIVATA dal parser del dominio",
      "('ip_addr', 'ip', _parse_address)" in rel
      and "domain.parse_address(value)" in rel,
      "PostgreSQL non deve mai interpretare il testo dell'utente: riceve la forma "
      "canonica che il parser unico ha prodotto")
check("la ricerca per indirizzo confronta `inet`, non ricostruisce l'aritmetica",
      "CAST(:lo AS inet)" in rows_fn
      and "split_part" not in queries_src,
      "l'espressione della 2E — nove `btrim` e otto `split_part` per riga, valutata a "
      "ogni ricerca — è sostituita da due confronti su una colonna indicizzata")
check("un indirizzo ESATTO è una forma riconosciuta",
      "exact" in code_only(ROOT / "backend" / "app" / "domain.py"),
      "era IL difetto: `10.0.0.1` non era una forma di rete, finiva nella ricerca "
      "testuale, e `10.0.0.1` è una sottostringa di `10.0.0.100`")
check("IPv6 è supportato, e senza inventare jolly o intervalli",
      "IPv6Address" in code_only(ROOT / "backend" / "app" / "domain.py")
      and "_RE_V4_WILDCARD" in code_only(ROOT / "backend" / "app" / "domain.py")
      and "_RE_V6_WILDCARD" not in code_only(ROOT / "backend" / "app" / "domain.py"),
      "`2001:db8::*` dovrebbe voler dire «un gruppo qualsiasi» o «il resto "
      "dell'indirizzo»? Ogni risposta è una grammatica nuova che nessuno ha chiesto")
check("la versione della mappa È stata alzata, con la sua migrazione",
      "MAPPER_VERSION = 2" in code_only(ROOT / "backend" / "app" / "inventory"
                                        / "relational.py")
      and (ROOT / "backend" / "migrations" / "versions" / "0013_domain.py").is_file(),
      "`presenza` passa da `extra` a una colonna tipizzata e `ip_addr` è nuova: le "
      "righe vecchie riassemblerebbero lo stesso documento — quindi lo stesso digest — "
      "mentre la capacità non troverebbe la presenza e la ricerca non troverebbe "
      "l'indirizzo. È il caso per cui quel numero esiste (§8.44)")

# --- la semantica della capacità ---
capacity_fn = function_source(QUERIES, "capacity")
check("la capacità NON somma le altezze",
      "SUM(h)" not in queries_src and "sum(d.h)" not in queries_src,
      "`used_u` è il conteggio degli SLOT DISTINTI occupati: due dispositivi "
      "sovrapposti contano una volta, uno che sporge viene tagliato")
check("la capacità non enumera gli slot",
      "generate_series" not in queries_src,
      "`rack.u` è un `integer` senza massimo e il documento `oversized-integers` ne "
      "contiene uno da tre miliardi: un `generate_series` su quello produrrebbe tre "
      "miliardi di righe dentro una richiesta HTTP")
check("la capacità unisce gli intervalli con funzioni finestra",
      all(t in queries_src for t in ("OVER (PARTITION BY", "lag(", "lead(")),
      "il costo cresce col numero di DISPOSITIVI, non con l'altezza dichiarata del rack")
# ⚠ ROVESCIATO nella 2G. Il controllo pretendeva la SENTINELLA — `rk.row || '—'` —
# perché la 2E riproduceva il frontend e il frontend collideva col dato: nel seed reale
# esiste un rack la cui fila È «—» (CS-Q01), e finiva nel gruppo «senza fila».
check("il raggruppamento per fila NON usa una sentinella",
      "ROW_SENTINEL" not in queries_src and "domain.row_group(" in queries_src,
      "una sentinella stampabile collide col dato: `domain.row_group` tiene separata "
      "la CHIAVE del gruppo dall'ETICHETTA mostrata, e la chiave contiene un byte NUL "
      "che nessun valore di documento può contenere")
check("la chiave del gruppo è impossibile da imitare con un valore del documento",
      "\\x00" in code_only(ROOT / "backend" / "app" / "domain.py"),
      "con un separatore stampabile un rack la cui fila valesse esattamente quel "
      "separatore ricreerebbe il difetto: la stessa storia, con un carattere diverso")
check("la percentuale è HALF-UP intera, in un posto solo",
      "domain.percent(" in queries_src
      and "(u * 200 + t) // (t * 2)" in code_only(ROOT / "backend" / "app"
                                                  / "domain.py"),
      "Math.round di JavaScript, round() di Python e round() di SQL non sono "
      "d'accordo sulla metà esatta: un rack da 8 U con 1 occupata è 13 per il "
      "frontend e 12 per Python. L'aritmetica intera li fa combaciare per costruzione")

# --- la semantica delle scadenze ---
expiries_fn = function_source(QUERIES, "expiries")
# ⚠ I nomi delle colonne non sono più letterali: `_expiry_sql` li compone da
# `domain.EXPIRY_KINDS` (`d.{kind}_date`), che è ciò che impedisce a questo modulo e al
# contratto di avere due elenchi di tipi di scadenza. Il controllo segue quel cambio.
check("le scadenze leggono le colonne DERIVATE, e i tipi vengono dal contratto",
      "_date AS expiry" in queries_src
      and "domain.EXPIRY_KINDS" in queries_src,
      "sono la sorgente interrogabile, e le ha scritte `domain.parse_expiry`")
# ⚠ `to_date(` con la parentesi: senza, il frammento combacia con `suppor·to_date`,
# cioè con il nome di una colonna che DEVE esserci. Un controllo che fallisce perché ha
# trovato la cosa giusta è un controllo scritto male.
check("le scadenze non reinterpretano il testo",
      all(t not in queries_src.lower()
          for t in ("to_date(", "to_timestamp(", "date_parse", "::date)")),
      "un secondo interprete di date divergerebbe dal primo, e divergerebbe sui casi "
      "limite — che sono l'unico posto dove la differenza si vede")
# ⚠ ROVESCIATO nella 2G, e nei DUE versi. La vista Scadenze saltava i dismessi e il
# worker no; adesso la vista li MOSTRA (è ispettiva) e il worker NON manda loro avvisi
# (§7). Le due domande restano diverse, ma non sono più in disaccordo: prima ognuna
# faceva l'opposto dell'altra senza che nessuno l'avesse deciso.
check("la vista Scadenze NON esclude i dismessi",
      "<> 'dismesso'" not in expiries_fn
      and "NOT IN ('dismesso')" not in expiries_fn,
      "un apparato dismesso ha un contratto che scade, e chi fa l'inventario dei "
      "contratti deve poterlo vedere (§8): resta ispezionabile, cercabile e nello "
      "storico. Non riceve avvisi — quella è l'altra domanda")
check("la vista Scadenze ha i filtri per stato e presenza",
      "stato" in expiries_fn and "presenza" in expiries_fn
      and "_reject_unknown" in queries_src,
      "`?stato=dismesso&presenza=rimosso` è l'elenco dei contratti di ciò che è stato "
      "portato via: il riscontro incrociato per cui i dismessi si conservano")
check("un filtro fuori vocabolario è un RIFIUTO, non un elenco vuoto",
      "QueryRejected" in function_source(QUERIES, "_reject_unknown"),
      "`?stato=dismessi` al plurale darebbe zero righe, e chi le legge concluderebbe "
      "che non ci sono apparati dismessi: plausibile e falsa")
check("il worker ESCLUDE i dismessi, e dal vocabolario del contratto",
      "NOTIFY_INELIGIBLE_STATES" in code_only(ROOT / "backend" / "app"
                                              / "notifications" / "candidates.py"),
      "l'elenco degli stati inidonei sta nel contratto: copiarlo in SQL darebbe due "
      "elenchi, e il giorno in cui divergessero il worker manderebbe avvisi che la "
      "vista dichiara non azionabili")
check("il worker NON guarda la presenza fisica",
      "presenza" not in function_source(
          ROOT / "backend" / "app" / "notifications" / "candidates.py",
          "due_items_from_projection"),
      "un apparato portato in un altro sito ha la garanzia che scade comunque, e chi "
      "la rinnova ha bisogno di saperlo: la presenza decide l'occupazione dello "
      "spazio, non gli avvisi")
check("«oggi» viene dal fuso configurato, con il codice del worker",
      "local_today" in query_routes_src,
      "così «scaduto» significa la stessa cosa in un promemoria via posta e in questa "
      "risposta")

# --- il contratto delle rotte ---
check("le tre rotte sono di sola lettura",
      query_routes_src.count("@router.get(") == 3
      and all(t not in query_routes_src for t in ("@router.post(", "@router.put(",
                                                  "@router.delete(",
                                                  "@router.patch(")),
      "sono interrogazioni: non scrivono, non accettano un corpo")
check("nessun endpoint esegue una query fornita dal client",
      all(t not in query_routes_src for t in ("order_by", "orderBy", "raw_sql",
                                              "sql=", "where=", "column=")),
      "tre domande con un significato si possono autorizzare, verificare e misurare; "
      "una domanda arbitraria no")
check("le rotte non impongono un ruolo minimo",
      "require_admin" not in query_routes_src and "require_actor" in query_routes_src,
      "nel frontend la ricerca, la capacità e le scadenze le vede chiunque abbia una "
      "sessione: renderle amministrative restringerebbe una funzione esistente (§2)")
check("le rotte usano lo SNAPSHOT di sola lettura della fase 2D",
      "get_snapshot_reader" in query_routes_src
      and "get_connection" not in query_routes_src,
      "una risposta deve descrivere un solo istante del database, testa e digest "
      "compresi — e la connessione della richiesta non è utilizzabile (§8.45)")
check("nessun terzo engine",
      "create_engine" not in queries_src and "create_engine" not in query_routes_src
      and code_only(ROOT / "backend" / "app" / "db.py").count("create_engine(") == 2,
      "il requisito dice esplicitamente di non introdurne un terzo: i due pool sono "
      "quello delle richieste e quello di lettura (§14)")
check("ogni risposta porta la revisione che descrive",
      query_routes_src.count("version=") >= 3 and "sha256=" in query_routes_src,
      "senza, una ricerca fatta mentre un collega salva restituisce righe corrette per "
      "una revisione che il client non ha, e il client non se ne accorge (§4)")
check("le interrogazioni NON riassemblano il documento",
      all(t not in queries_src for t in ("current_document", "assemble(",
                                         "read_model(", "canonical_sha256")),
      "la fedeltà completa costa il 70% Python misurato in §8.45.1: pagarla su ogni "
      "ricerca sarebbe carico senza una ragione. Le query pretendono l'ATTUALITÀ (§12)")
check("le interrogazioni pretendono comunque una proiezione attuale",
      "require_current_head" in queries_src,
      "e non ripiegano sul filtraggio del JSON, che darebbe la risposta giusta "
      "nascondendo il difetto")

# --- la paginazione ---
check("il cursore è una CHIAVE, non un offset",
      "OFFSET" not in queries_src and "encode_cursor" in queries_src,
      "un `OFFSET` su un insieme che cambia salta o ripete righe, e lo fa proprio "
      "quando qualcuno sta salvando (§8)")
check("l'ordinamento ha l'`uid` come ultimo spareggio",
      "sort_uid" in queries_src and "ORDER BY l_ord, r_ord, k_ord, kind_rank, d_ord, "
      "sort_uid" in queries_src,
      "l'ordine deve restare totale anche quando ordinali e nomi collidono")
check("il cursore rotto ha un codice stabile",
      "invalid_cursor" in queries_src
      and "CursorRejected" in code_only(ROOT / "backend" / "app" / "api" / "errors.py"),
      "422 e non 503: è un difetto della richiesta, e riprovare non lo risolve")

# --- le fixture di parità vengono dal frontend VERO ---
GENERATOR = ROOT / "tools" / "make-query-fixtures.mjs"
check("esiste il generatore delle fixture di parità", GENERATOR.is_file())
generator = GENERATOR.read_text(encoding="utf-8") if GENERATOR.is_file() else ""
fixtures_dir = ROOT / "fixtures" / "query"
corpora = sorted(p.name for p in fixtures_dir.glob("*.json")
                 if not p.name.startswith("_")) if fixtures_dir.is_dir() else []
check("i corpora di parità sono committati",
      len(corpora) >= 25,
      f"sono il contratto fra il JavaScript che gira e lo SQL nuovo: {len(corpora)}")

# ⚠ IL MECCANISMO È CAMBIATO NELLA 2G, ed è il cambio più importante di questo file.
#
# Fino alla 2F qui c'era un elenco `VERBATIM`: righe di JavaScript copiate ALLA LETTERA
# dal frontend nel generatore delle fixture, e un controllo che verificava che esistessero
# ancora identiche nell'HTML. Aveva senso finché ciò che andava dimostrato era la PARITÀ
# con il comportamento che girava: il frontend era il riferimento semantico, e se una di
# quelle righe cambiava, le fixture andavano rigenerate.
#
# La 2G rovescia la direzione. Il comportamento del prototipo non è più il contratto —
# è ciò che la 2G sostituisce — e il riferimento sono `fixtures/domain/*.json`, con le
# attese scritte a mano da una decisione di prodotto. Le righe copiate non esistono più
# perché non c'è più niente da copiare: il frontend CHIAMA il modello di dominio invece
# di contenerne una versione.
#
# Quindi i controlli qui sotto non cercano più frammenti identici. Cercano che le tre
# implementazioni PARTANO dallo stesso posto, e che nessuna se ne sia fatta una propria.
def js_code_only(text: str) -> str:
    """Il sorgente JavaScript SENZA commenti.

    ⚠ Esiste perché ci sono ricascato due volte, e la seconda in questa stessa fase.
    I controlli qui sotto cercano frammenti che NON devono esistere — `rk.row || '—'`,
    `=== 'dismesso') {}` — e quei frammenti compaiono nei COMMENTI che spiegano perché
    sono stati rimossi. Un controllo che trova la propria spiegazione è un controllo
    che dichiara un difetto dove c'è la sua descrizione.

    Nella 2F la soluzione fu `ast` su Python. Qui non c'è un parser JavaScript a
    disposizione, quindi si scandisce a mano tenendo conto di stringhe, apici,
    template literal e classi di caratteri di una regex: dentro una stringa un `//`
    non apre un commento, e `https://` non deve cancellare mezza riga.

    Se questo scanner sbaglia, sbaglia togliendo TROPPO — e un controllo che cerca
    l'assenza di un frammento diventa più debole, non falso.
    """
    out = []
    i, n = 0, len(text)
    quote = None          # ' " ` oppure None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != chr(10):
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            fine = text.find("*/", i + 2)
            i = n if fine < 0 else fine + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


FRONTEND_APP = ROOT / "handoff" / "Sala Server v2.dc.html"
check("il sorgente dell'applicazione frontend è dove ci si aspetta",
      FRONTEND_APP.is_file(),
      "senza, non c'è niente da confrontare con il contratto")
frontend_app = FRONTEND_APP.read_text(encoding="utf-8") if FRONTEND_APP.is_file() else ""
def html_script_code(text: str) -> str:
    """Il solo JAVASCRIPT di un file HTML, senza commenti di nessuna delle due specie.

    ⚠ Non basta passare l'HTML intero a `js_code_only`, e il perché è istruttivo: quel
    lettore tiene traccia delle stringhe, e in un HTML italiano gli apostrofi del testo
    («l'app», «un'entità») sono apici singoli FUORI da qualunque stringa. Il primo
    apostrofo apre uno stato di stringa che non si chiude più dove dovrebbe, il lettore
    si disallinea e i commenti smettono di essere riconosciuti — che è esattamente il
    modo in cui questo controllo trovava la propria spiegazione e la dichiarava un
    difetto.
    """
    import re

    senza_commenti_html = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    blocchi = re.findall(r"<script[^>]*>(.*?)</script>", senza_commenti_html,
                         flags=re.S | re.I)
    return js_code_only(chr(10).join(blocchi))


#: Il solo JavaScript, senza commenti: è su questo che si cercano i frammenti VIETATI.
frontend_code = html_script_code(frontend_app)

DOMAIN_JS = ROOT / "handoff" / "domain.js"
check("esiste il gemello JavaScript del modello di dominio", DOMAIN_JS.is_file())
domain_js = DOMAIN_JS.read_text(encoding="utf-8") if DOMAIN_JS.is_file() else ""
domain_js_code = js_code_only(domain_js)

CONTRACT = ROOT / "fixtures" / "domain"
corpora_dominio = sorted(x.name for x in CONTRACT.glob("*.json")) \
    if CONTRACT.is_dir() else []
check("i corpora del contratto di dominio sono committati",
      len(corpora_dominio) >= 9,
      f"sono il riferimento delle tre implementazioni: {corpora_dominio}")
check("il contratto copre tutte le aree del requisito",
      all(f"{nome}.json" in corpora_dominio for nome in
          ("presence", "capacity", "percent", "rows", "expiries", "notifications",
           "addresses", "search", "labels")),
      f"un'area senza corpus è un'area in cui le due implementazioni possono "
      f"divergere senza che niente diventi rosso: {corpora_dominio}")
check("esiste il generatore del contratto, e le attese sono a mano",
      (ROOT / "tools" / "make-domain-fixtures.mjs").is_file(),
      "calcolare le attese da una delle due implementazioni renderebbe il contratto "
      "vacuo: se sbagliassero entrambe allo stesso modo, nessun test diventerebbe rosso")
check("esistono le due suite che eseguono il contratto",
      (ROOT / "tools" / "domain-contract-tests.mjs").is_file()
      and (ROOT / "backend" / "tests" / "test_domain_contract.py").is_file(),
      "una sola suite dimostrerebbe che una implementazione soddisfa il contratto, non "
      "che le due sono d'accordo")

# --- il frontend NON contiene più una semantica propria ---
#
# ⚠ Questi sono i controlli che sostituiscono `VERBATIM`, e sono scritti al NEGATIVO
# di proposito: elencano le espressioni che ERANO la semantica duplicata. Se una torna,
# qualcuno ha ricominciato a decidere nel frontend.
SEMANTICA_DUPLICATA = [
    ("new Array(rk.u + 1)", "il vettore di occupazione: era la definizione di «U "
                            "occupate», ed era una di tre"),
    ("reduce((a, d) => a + (d.h || 1), 0)", "SUM(h) nel pannello del rack"),
    ("reduce((t, d) => t + (d.h || 1), 0)", "SUM(h) nell'export XLSX"),
    ("static ipToNum", "la seconda grammatica degli indirizzi"),
    ("static parseIpQuery", "la seconda grammatica delle query di rete"),
    ("rk.row || '—'", "la sentinella che collideva col dato"),
    ("=== 'dismesso') {}", "il ramo vuoto che non escludeva i dismessi"),
    ("=== 'dismesso') continue", "il filtro che li faceva sparire dalle Scadenze"),
]
tornate = [(f, perche) for f, perche in SEMANTICA_DUPLICATA if f in frontend_code]
check("il frontend non contiene più una semantica di dominio propria",
      not tornate,
      f"queste espressioni ERANO la duplicazione che la 2G ha rimosso, e sono "
      f"tornate: {[(f[:40], p) for f, p in tornate]}")
check("il frontend CHIAMA il modello di dominio",
    "import('./domain.js')" in frontend_app
      and frontend_app.count("DOM.") >= 20,
      "se non lo chiamasse, la semantica sarebbe tornata da qualche parte: il "
      "contratto sarebbe soddisfatto dal solo backend")

# --- e nemmeno domain.js decide da sé ---
check("il gemello JavaScript non usa `new Date` per interpretare una data",
      "new Date(v" not in domain_js_code and "Date.parse" not in domain_js_code,
      "`new Date` accetta sette forme che il backend rifiuta, e su `2027-02-30` non "
      "rifiuta: la fa SCORRERE al 2 marzo. Una data inesistente diventava esistente")
check("il gemello JavaScript non usa Math.round per le percentuali",
      "Math.round((u" not in domain_js_code
      and "Math.round(used" not in domain_js_code
      and "(u * 200 + t) / (t * 2)" in domain_js_code,
      "Math.round e round() di Python non sono d'accordo sulla metà esatta: "
      "l'aritmetica intera li fa combaciare per costruzione, non per fortuna")
check("il gemello JavaScript non conta i giorni con una divisione di millisecondi",
      "86400000" not in domain_js_code and "daysFromCivil" in domain_js_code,
      "`(dt - Date.now()) / 86400000` dipende dall'ora del giorno: nella notte del "
      "cambio dell'ora 23 o 25 ore si arrotondavano a un giorno per caso")
check("il vecchio generatore di parità non copia più il frontend",
      not (ROOT / "tools" / "make-query-fixtures.mjs").is_file()
      or "VERBATIM" in (ROOT / "tools" / "make-query-fixtures.mjs")
      .read_text(encoding="utf-8"),
      "resta come riferimento storico della 2E: le sue fixture misurano il "
      "comportamento del prototipo, che è cio che la 2G ha sostituito")

# --- ciò che la fase 2E NON fa ---
# ⚠ ROVESCIATO nella fase 2F: era «il worker delle notifiche non è passato a SQL»,
# cioè la delimitazione della 2E. Il commit isolato che quel commento annunciava è la
# 2F, ed è avvenuto. Ciò che resta da sorvegliare è che il worker non sia passato
# all'ENDPOINT — che è una cosa diversa e sbagliata (§8.48).
check("il worker non usa le interrogazioni interattive come sorgente",
      all(t not in worker_sources
          for t in ("app.inventory.queries", "queries.expiries", "httpx",
                    "urllib")),
      "l'endpoint riproduce la vista Scadenze, che sui dismessi e sugli scaduti non è "
      "d'accordo con lo scanner: usarlo cambierebbe la semantica degli avvisi (§8.48)")
# ⚠ Questo controllo resta, e la 2G lo rende PIÙ importante, non meno.
#
# Il frontend adesso condivide la SEMANTICA col backend, e la tentazione naturale è
# fare il passo successivo — chiamare le rotte — nello stesso commit. Sono due cose
# diverse: la 2G garantisce che i due calcoli DIANO LA STESSA RISPOSTA, e quella
# garanzia è precisamente ciò che rende la migrazione successiva noiosa e sicura. Se
# avvenissero insieme, un numero diverso sullo schermo non si saprebbe più attribuire.
# ⚠ Su `frontend_code`, non sul testo grezzo: il commento che SPIEGA che il frontend
# non chiama quelle rotte le nomina, e cercarle nel testo grezzo trovava la propria
# spiegazione. Terza volta in questa fase che ci ricasco, e la terza volta la
# soluzione è la stessa — guardare il codice, non la prosa.
check("il frontend non è stato ricablato alle rotte nuove",
      all(t not in js_code_only(handoff) + frontend_code
          for t in ("/api/inventory/search", "/api/inventory/capacity",
                    "/api/inventory/expiries")),
      "la 2G unifica la semantica; il passaggio alle rotte è la fase successiva. "
      "Farli insieme renderebbe impossibile attribuire un cambiamento a uno dei due")
# ⚠ ROVESCIATO nella 2G: il controllo pretendeva `parseIpQuery` e
# `new Array(rk.u + 1)` nel frontend, cioè la presenza della semantica duplicata,
# «altrimenti il frontend sarebbe già stato migrato e la parità non avrebbe più un
# riferimento». Il riferimento adesso è il CONTRATTO, non il codice del prototipo:
# quelle due espressioni devono essere sparite, e il frontend deve continuare a
# calcolare in locale CHIAMANDO il dominio.
check("il frontend continua a calcolare in locale, ma con la semantica condivisa",
      "DOM.rackCapacity(" in frontend_code
      and "DOM.parseAddressQuery(" in frontend_code
      and "await import('./domain.js')" not in frontend_code.replace("\n", " "),
      "calcola ancora da sé — nessuna rotta chiamata — ma le regole vengono da "
      "handoff/domain.js, che è il gemello di app/domain.py")
check("nessuna migrazione nuova per la fase 2E",
      not (ROOT / "backend" / "migrations" / "versions" / "0013_query_indexes.py")
      .is_file(),
      "le misure non giustificano nessun indice nuovo: le chiavi esterne e le date "
      "sono già indicizzate, e la ricerca è una sottostringa che nessun btree serve "
      "(§8.46.1)")

# ==================================================================
# 17. fase 2F: il worker legge la proiezione, e SOLO la sorgente e' cambiata (§8.47)
# ==================================================================
#
# I test di parità in `test_worker_sql_pg.py` provano che la sorgente nuova risponde
# come `due_items(doc)`, e i 53 test di `test_worker_pg.py` — non modificati — provano
# che la consegna non è cambiata. Qui si copre ciò che nessuno dei due vede: che il
# ripiego sul documento non sia rientrato da una porta laterale, che l'isolamento sia
# dichiarato in un posto solo, che non siano comparsi privilegi nuovi per il worker, e
# che l'endpoint interattivo e lo scanner siano rimasti due cose distinte.

CANDIDATES = ROOT / "backend" / "app" / "notifications" / "candidates.py"
WORKER = ROOT / "backend" / "app" / "notifications" / "worker.py"
EXPIRY = ROOT / "backend" / "app" / "notifications" / "expiry.py"
candidates_src = code_only(CANDIDATES)
worker_src = code_only(WORKER)
expiry_src = code_only(EXPIRY)

check("esiste un modulo dedicato alla sorgente dei candidati",
      CANDIDATES.is_file() and "def due_items_from_projection(" in candidates_src,
      "sta in `app/notifications/` e non in `app/inventory/queries.py`: quel modulo ha "
      "la semantica della vista Scadenze, e condividerne un pezzo e' l'inizio di "
      "condividerne il resto (§8.48)")
check("la sorgente legge le colonne DERIVATE e non il testo grezzo",
      "garanzia_date" in candidates_src and "supporto_date" in candidates_src,
      "le ha scritte `parse_expiry`, cioe' lo stesso parser che il worker riconosce: "
      "interpretare qui il testo significherebbe due idee di «data valida»")
check("nessun secondo interprete di date nella sorgente",
      all(t not in candidates_src
          for t in ("to_date(", "strptime", "fromisoformat", "re.compile")),
      "una seconda idea di data diverge dalla prima, e diverge sui casi limite — che "
      "sono esattamente quelli che un inventario compilato a mano produce")
check("lo scanner puro e' rimasto puro",
      all(t not in expiry_src for t in ("garanzia_date", "supporto_date",
                                        "inventory_devices", "sqlalchemy",
                                        "Connection")),
      "`due_items` e' l'ORACOLO della parita': se leggesse le stesse colonne "
      "dell'implementazione nuova, il confronto sarebbe con se' stessa")

# --- nessun filtro della vista Scadenze nella sorgente del worker ---
check("la sorgente non filtra i dispositivi dismessi",
      "dismesso" not in candidates_src,
      "`due_items` scorre `walk(doc)` e non guarda `stato`: una macchina dismessa con "
      "la garanzia in scadenza ha sempre prodotto un promemoria. Aggiungere qui il "
      "filtro della vista sarebbe una modifica di PRODOTTO travestita da migrazione")
check("la sorgente interroga una finestra, non tutta la tabella",
      "until" in candidates_src and "timedelta" in candidates_src,
      "leggere tutte le date e scartarle in Python avrebbe ricreato la scansione del "
      "documento, cioe' reso inutile la migrazione (§3 della fase 2F)")
check("i due rami sono uniti con UNION ALL e non con un OR",
      "UNION ALL" in candidates_src,
      "con un `OR` fra le due colonne data PostgreSQL non puo' usare nessuno dei due "
      "indici parziali e scandisce la tabella")

# --- il controllo di coerenza, e il fallire chiuso ---
check("la sorgente pretende una proiezione attuale E COERENTE",
      "require_valid_model" in candidates_src
      and "require_current_head" not in candidates_src,
      "attualità sola non basta: le colonne DERIVATE non entrano nel digest, quindi "
      "una `garanzia_date` azzerata a mano lascia versione e digest identici e fa "
      "smettere gli avvisi in silenzio. `validate_model` è l'unica cosa che la vede, "
      "ed è la stessa precondizione che usa il `GET` (§8.47.4)")
check("anche il ritentativo pretende la precondizione completa",
      code_only(CANDIDATES).count("require_valid_model") >= 2,
      "un ritentativo che leggesse nomi da un modello incoerente potrebbe non trovare "
      "la chiave e chiudere la consegna, marcando come inviati promemoria che nessuno "
      "ha ricevuto")
check("la validazione NON è un secondo interprete scritto qui",
      "validate_model(" not in candidates_src,
      "si usa quella che c'è, già provata dalla scrittura doppia e dal `GET`: una "
      "seconda idea di «modello coerente» divergerebbe dalla prima")
check("nessun ripiego sull'istantanea in nessun modulo del worker",
      all(t not in worker_sources for t in ("get_current", "InventoryRepository",
                                            "inventory_versions")),
      "il ripiego funzionerebbe, nessuno aprirebbe un ticket, e il difetto di "
      "coerenza resterebbe invisibile (§8.45)")
check("i due guasti della proiezione fermano il giro senza concluderlo",
      "ProjectionInconsistentError" in worker_src
      and "ProjectionNotCurrentError" in worker_src
      and "reason=exc.code" in worker_src,
      "concludere il giro con un esito farebbe rispondere `already_ran_today` fino a "
      "mezzanotte, cioe' perderebbe la giornata invece di riprovare al tick dopo. E i "
      "due codici restano DISTINTI: `not_current` si ripara con `--rebuild`, "
      "`inconsistent` si indaga — schiacciarli manderebbe chi opera nel posto "
      "sbagliato")
check("un guasto della proiezione non consuma un tentativo di consegna",
      ordered(function_source(WORKER, "_attempt_delivery"),
              "_rebuild_selection", "mark_attempt_started"),
      "i cinque tentativi esistono per un relay guasto, non per una proiezione da "
      "ricostruire: il rinvio deve avvenire PRIMA che il contatore si muova")
check("la revisione dei candidati si ricontrolla prima di prenotare",
      "unchanged" in worker_src and "inventory_moved" in worker_src,
      "fra lo snapshot e la transazione che scrive un `PUT` puo' cambiare tutto, e un "
      "avviso su una revisione che non esiste piu' annuncia una scadenza corretta")
check("il controllo della revisione non blocca la testa",
      "FOR UPDATE" not in candidates_src,
      "un lock sulla riga di testa resterebbe preso per tutta la consegna SMTP, cioe' "
      "per un timeout di rete, fermando i salvataggi di tutti")

# --- una sola dichiarazione dell'isolamento ---
_iso = [f"{q.name}:{r.strip()}"
        for q in sorted((ROOT / "backend" / "app").rglob("*.py"))
        for r in q.read_text(encoding="utf-8-sig").splitlines()
        if "isolation_level=" in r and not r.strip().startswith("#")]
check("`REPEATABLE READ` si dichiara in UN posto solo",
      len(_iso) == 1 and _iso[0].startswith("db.py:"),
      "dalla 2F i lettori della proiezione sono due processi — l'API e il worker — e "
      f"due dichiarazioni dello stesso isolamento divergono in silenzio: {_iso}")
check("il worker usa lo snapshot condiviso e non un giro suo",
      "read_snapshot" in worker_src and "read_snapshot" in code_only(
          ROOT / "backend" / "app" / "api" / "deps.py"),
      "la stessa funzione per l'API e per il worker: e' cio' che tiene una sola verita' "
      "sull'isolamento")

# --- privilegi: nessuna concessione nuova ---
_migrazioni = sorted((ROOT / "backend" / "migrations" / "versions").glob("*.py"))
# ⚠ ROVESCIATO nella 2G: la 2F non aggiungeva migrazioni, la 2G sì. E la migrazione
# NON concede privilegi nuovi: aggiunge due colonne a una tabella esistente, che
# ereditano i privilegi di tabella. Il worker resta in sola lettura.
check("la fase 2G ha la sua migrazione, e non tocca i privilegi",
      any(m.name == "0013_domain.py" for m in _migrazioni)
      and all(t not in (ROOT / "backend" / "migrations" / "versions"
                        / "0013_domain.py").read_text(encoding="utf-8")
              for t in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE")),
      "`presenza` e `ip_addr` sono colonne di una tabella che esiste: ereditano i "
      "privilegi, e non c'è niente da concedere. Il ruolo del worker resta in sola "
      "lettura su tutto lo schema dell'inventario (§8.47.7)")
check("la migrazione non fa una migrazione di DATI",
      all(t not in (ROOT / "backend" / "migrations" / "versions"
                    / "0013_domain.py").read_text(encoding="utf-8")
          for t in ("UPDATE inventory", "INSERT INTO inventory")),
      "le due colonne nascono NULL e le riempie `project.py --rebuild`: un `UPDATE` di "
      "massa in una migrazione è una riscrittura dei dati di produzione che nessuno ha "
      "chiesto")
check("la migrazione non mette un CHECK sul vocabolario della presenza",
      "presenza IN (" not in (ROOT / "backend" / "migrations" / "versions"
                              / "0013_domain.py").read_text(encoding="utf-8"),
      "l'inventario reale arriva da fogli di calcolo e contiene sempre qualche valore "
      "fuori elenco: un vincolo qui farebbe RIFIUTARE alla proiezione un documento che "
      "la fase 1 accetta. `validate_model` lo segnala come avviso, che è la cosa giusta")
check("le REVOKE che tengono il worker in sola lettura sono ancora scritte",
      all(t in (ROOT / "backend" / "migrations" / "versions" /
                "0012_dual_write.py").read_text(encoding="utf-8")
          for t in ("REVOKE INSERT, UPDATE, DELETE, TRUNCATE", "WORKER_ROLE")),
      "il worker manda avvisi: non ha nessun motivo per riscrivere la proiezione che "
      "sta leggendo, e quelle righe sono cio' che un diff mostra accanto a chi provasse")

# --- l'endpoint e lo scanner restano due cose ---
# ⚠ ROVESCIATO nella 2G. Il controllo pretendeva che le due semantiche restassero
# DIVERSE per caso — «riconciliarli è una decisione di prodotto, non di questo commit».
# La decisione è stata presa (§7): restano diverse PER SCELTA, e ognuna fa ciò che
# serve alla sua domanda.
#
#   vista Scadenze   ispettiva: mostra tutto, dismessi compresi, scaduti compresi
#   worker           azionabile: 0 <= giorni <= finestra, e non i dismessi
#
# Il controllo pretende adesso che ognuna abbia la SUA regola, e che non se le siano
# scambiate.
check("le due domande sulle scadenze sono decise, e sono ancora due",
      "expired" in queries_src                       # la vista ha i livelli
      and "notifiable" in queries_src                # e sa dire cosa e azionabile
      and "NOTIFY_INELIGIBLE_STATES" in candidates_src   # il worker ha l'idoneita
      and "expired" not in candidates_src,               # e non ha i livelli
      "la vista mostra e spiega; il worker decide e manda. Se una prendesse la regola "
      "dell'altra, gli avvisi cambierebbero senza che nessuno l'avesse deciso")
check("il registro del debito semantico esiste ed e' separato",
      "8.48" in (ROOT / "BACKEND-PLAN.md").read_text(encoding="utf-8"),
      "le incoerenze scoperte nella 2E vanno risolte di proposito prima di migrare il "
      "frontend, non scoperte una seconda volta da chi lo migra (§15 della fase 2F)")

# --- cio' che la fase 2F NON fa ---
def _voci_registro():
    """Le righe del registro del debito semantico (§8.48) numerate da 1 a 9.

    Sono le voci che il requisito della 2G dichiara da risolvere prima del rilascio.
    Si leggono dal documento invece di elencarle qui: un elenco copiato in questo file
    diventerebbe la seconda versione del registro, e la prima a essere dimenticata.
    """
    import re

    testo = (ROOT / "BACKEND-PLAN.md").read_text(encoding="utf-8")
    inizio = testo.find("### 8.48 Registro del debito semantico")
    fine = testo.find("### 8.49", inizio)
    if inizio < 0:
        return []
    sezione = testo[inizio:fine if fine > 0 else len(testo)]
    return [riga for riga in sezione.splitlines()
            if re.match("^[|]\\s*[1-9]\\s*[|]", riga)]


# ⚠ ROVESCIATO nella 2G: il rimando era «dopo aver risolto le incoerenze del
# registro», e la 2G le risolve. Ciò che resta da sorvegliare non è più il registro: è
# che la RISOLUZIONE sia arrivata in tutte le implementazioni, non solo nel backend.
check("il registro dichiara risolte le incoerenze che la 2G risolve",
      all(f"RISOLTA" in riga or "RISOLTO" in riga
          for riga in _voci_registro() if riga),
      "una voce lasciata aperta dopo la 2G è un blocco al rilascio (§14 del "
      "requisito): il posto dove si scopre è questo, non un cliente")
check("la readiness non guarda lo stato del worker",
      all(t not in code_only(ROOT / "backend" / "app" / "api" / "health.py")
          for t in ("scheduler_runs", "worker_heartbeat", "reminder_deliveries")),
      "la salute del worker e' un problema di monitoraggio suo, con il suo battito e "
      "il suo healthcheck (§14 della fase 2F)")
check("nessuna semantica delle scadenze e' stata riconciliata",
      "in dismissione" not in candidates_src and "level" not in candidates_src,
      "la sorgente del worker non conosce i livelli della vista: se li conoscesse, "
      "qualcuno avrebbe cominciato a fonderle")


if __name__ == "__main__":
    sys.exit(report())