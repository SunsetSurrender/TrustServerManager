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

check("le colonne data derivate usano il parser dello scanner delle scadenze",
      "from app.notifications.expiry import parse_expiry" in rel,
      "un secondo parser sarebbe una seconda idea di «data valida», e divergerebbe "
      "sui casi limite — che sono i valori che l'inventario reale contiene")
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
check("nemmeno la funzione di lettura deserializza l'istantanea",
      bool(head_fn)
      and "SELECT canonical_sha256 FROM inventory_versions" in head_fn
      and all("doc FROM inventory_versions" not in f for f in (current_fn, head_fn))
      and all(".doc" not in f for f in (current_fn, head_fn)),
      "di `inventory_versions` legge il DIGEST, che è metadato e serve da giudice. "
      "Il documento no: se lo avesse in mano potrebbe restituirlo")
check("il riassemblaggio passa dalla mappa già provata",
      all(t in current_fn for t in ("read_model", "assemble", "canonical_sha256",
                                    "validate_model", "require_current")),
      "nessuna seconda implementazione della lettura: quella che c'è è provata da "
      "`test_relational_mapper.py` e dalla scrittura doppia")

# L'ORDINE dentro la funzione: la precondizione di attualità costa tre query, il
# riassemblaggio costa tutto il resto. Ma soprattutto è ciò che separa i due codici
# d'errore, e chi opera legge quello per sapere se `--rebuild` è la risposta.
check("l'attualità si pretende PRIMA di leggere le righe",
      ordered(current_fn, "require_current", "read_model"),
      "farlo dopo confonderebbe «la proiezione non è mantenuta» (rimedio: "
      "`--rebuild`) con «la proiezione mente» (rimedio: indagare, NON ricostruire)")
check("la coerenza del modello si controlla PRIMA di servire",
      ordered(current_fn, "validate_model", "return CurrentDocument"),
      "è l'unico controllo che vede le colonne DERIVATE, a cui il digest è cieco: "
      "senza, una `garanzia_date` sbagliata uscirebbe indisturbata")
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

check("esistono le tre interrogazioni, e sono tre",
      all(f"def {nome}(" in queries_src
          for nome in ("search", "capacity", "expiries")),
      "ricerca, capacità e scadenze: le tre famiglie che la fase 2 doveva sostenere")

# --- la semantica della ricerca ---
search_fn = function_source(QUERIES, "search")
rows_fn = function_source(QUERIES, "_search_rows")
check("la ricerca testuale usa `strpos`, non `LIKE`",
      "strpos(lower(" in rows_fn and "LIKE" not in rows_fn,
      "`LIKE` attribuisce un significato a `%` e `_`, che in una casella di ricerca "
      "sono caratteri normali: una query contenente `%` troverebbe tutto")
check("la ricerca NON è tokenizzata",
      all(t not in queries_src for t in ("to_tsvector", "to_tsquery", "tsquery",
                                         "plainto_", "websearch_", "@@")),
      "la ricerca a testo pieno di PostgreSQL non trova `SRV-Web-01` cercando `web`: "
      "sarebbe una ricerca «migliore» che perde risultati")
check("i campi cercati sui dispositivi sono i CINQUE del frontend",
      all(f"d.{c}" in rows_fn for c in ("name", "model", "ip", "serial", "owner"))
      and "d.note" not in rows_fn and "d.type" not in rows_fn.split("dev_type")[0],
      "`id`, `type`, `stato` e `note` NON sono cercati dalla barra globale. Sembra una "
      "dimenticanza del frontend, ma aggiungerli qui darebbe più risultati sul server "
      "che nel browser — due prodotti diversi")
check("i rack si cercano su codice, nome e seriali",
      "k.code" in rows_fn and "k.name" in rows_fn and "k.seriali" in rows_fn)
# Senza virgolette esterne: `ast.unparse` normalizza i letterali (la trappola
# documentata in `code_only`, e ci sono ricascato).
check("in modalità intervallo IP i rack non partecipano",
      "rack_where = " in rows_fn and "FALSE" in rows_fn,
      "il frontend scrive `if (!ipRange && (rk.id...))`: quando la query è una rete, i "
      "rack sono esclusi per costruzione")

# --- nessun `inet`, che è la scorciatoia più tentatrice ---
check("l'IP si confronta con l'aritmetica di `ipToNum`, non con `inet`",
      "split_part" in queries_src
      and all(t not in queries_src for t in ("::inet", "<<=", "inet_", "::cidr")),
      "`inet` accetterebbe anche IPv6 e le forme abbreviate, cioè aggiungerebbe "
      "semantica che il prodotto non ha: oggi un dispositivo con `2001:db8::1` non si "
      "trova per intervallo, e trovarlo sarebbe un comportamento nuovo")
check("non è stata aggiunta nessuna colonna derivata per l'IP",
      "ip_inet" not in code_only(ROOT / "backend" / "app" / "inventory"
                                / "relational.py"),
      "una colonna derivata nuova cambia la distribuzione dei dati fra colonne, quindi "
      "obbligherebbe ad alzare `MAPPER_VERSION` e a un `--rebuild` in manutenzione")
check("la versione della mappa NON è stata alzata",
      "MAPPER_VERSION = 1" in code_only(ROOT / "backend" / "app" / "inventory"
                                        / "relational.py"),
      "la fase 2E non cambia la proiezione: solo la interroga. Alzarla costringerebbe "
      "a una ricostruzione per una funzione di sola lettura")

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
check("la sentinella del raggruppamento per fila è quella del frontend",
      "ROW_SENTINEL" in queries_src,
      "il frontend raggruppa per `rk.row || '—'`, e nel seed reale esiste un rack la "
      "cui fila È «—»: la sentinella collide col dato, e i due finiscono nello stesso "
      "gruppo. Rimapparla darebbe due gruppi dove l'utente ne vede uno")

# --- la semantica delle scadenze ---
expiries_fn = function_source(QUERIES, "expiries")
check("le scadenze leggono le colonne DERIVATE",
      "garanzia_date" in queries_src and "supporto_date" in queries_src,
      "sono la sorgente interrogabile, e le ha scritte `parse_expiry`")
# ⚠ `to_date(` con la parentesi: senza, il frammento combacia con `suppor·to_date`,
# cioè con il nome di una colonna che DEVE esserci. Un controllo che fallisce perché ha
# trovato la cosa giusta è un controllo scritto male.
check("le scadenze non reinterpretano il testo",
      all(t not in queries_src.lower()
          for t in ("to_date(", "to_timestamp(", "date_parse", "::date)")),
      "un secondo interprete di date divergerebbe dal primo, e divergerebbe sui casi "
      "limite — che sono l'unico posto dove la differenza si vede")
check("i dispositivi dismessi si escludono come nel frontend",
      "nullif(d.stato, '')" in queries_src and "dismesso" in queries_src,
      "il frontend scrive `(d.stato || 'attivo')`, e in JavaScript la stringa vuota è "
      "falsa: `stato: \"\"` significa «attivo»")
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

#: Le righe che il generatore dichiara copiate ALLA LETTERA dal frontend. Se una non si
#: trova più identica nell'HTML, il riferimento semantico si è spostato e le fixture
#: vanno rigenerate — che è l'unico modo di accorgersene.
VERBATIM = [
    "const m = String(ip || '').trim().match(/^(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})$/);",
    "if (p.some(x => x > 255)) return null;",
    "return ((p[0] * 256 + p[1]) * 256 + p[2]) * 256 + p[3];",
    "const size = Math.pow(2, 32 - bits);",
    "const start = Math.floor(base / size) * size;",
    "return [Math.min(a, b), Math.max(a, b)];",
    "while (lo.length < 4) { lo.push(0); hi.push(255); }",
    "return [d.name, d.model, d.ip, d.serial, d.owner].some(v => (v || '').toLowerCase().includes(q));",
    "const occ = new Array(rk.u + 1).fill(false);",
    "for (let k = 1; k <= rk.u; k++) { if (occ[k]) { rkUsed++; run = 0; } else { run++; if (run > maxRun) maxRun = run; } }",
    "if (maxRun > bestFree) { bestFree = maxRun; bestRack = rk.id; }",
    "const rw = rk.row || '—';",
    "const pct = tot ? used / tot : 0;",
    "if ((d.stato || 'attivo') === 'dismesso') continue;",
    "const lv = giorni < 0 ? 2 : (giorni <= 90 ? 1 : 0);",
    "entries.sort((a, b) => a.dt - b.dt);",
]

#: Il sorgente dell'APPLICAZIONE, che sta nell'HTML a file unico e non nei `.js`.
#:
#: ⚠ `handoff` concatena soltanto `handoff/**/*.js` — dati del seed, modulo identità,
#: client dell'API — e la logica di ricerca, capacità e scadenze non è là: è dentro
#: `Sala Server v2.dc.html`. Cercare i frammenti in `handoff` li dichiarava tutti
#: assenti, che è il modo in cui questo controllo sarebbe potuto passare per sbaglio se
#: l'avessi scritto al negativo.
FRONTEND_APP = ROOT / "handoff" / "Sala Server v2.dc.html"
check("il sorgente dell'applicazione frontend è dove ci si aspetta",
      FRONTEND_APP.is_file(),
      "è il riferimento semantico della fase 2E: senza, la parità non ha un termine "
      "di confronto")
frontend_app = FRONTEND_APP.read_text(encoding="utf-8") if FRONTEND_APP.is_file() else ""

mancanti_html = [frammento for frammento in VERBATIM
                 if frammento not in frontend_app]
check("gli algoritmi copiati nel generatore esistono ancora nel frontend",
      not mancanti_html,
      f"il riferimento semantico si è spostato: rigenerare le fixture. Non trovati: "
      f"{[f[:60] for f in mancanti_html]}")
mancanti_gen = [frammento for frammento in VERBATIM if frammento not in generator]
check("il generatore contiene davvero quegli algoritmi",
      not mancanti_gen,
      f"un frammento nell'elenco ma non nel generatore rende il controllo vacuo: "
      f"{[f[:60] for f in mancanti_gen]}")

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
check("il frontend non è stato ricablato alle rotte nuove",
      all(t not in handoff + frontend_app
          for t in ("/api/inventory/search", "/api/inventory/capacity",
                    "/api/inventory/expiries")),
      "la 2E prova prima le implementazioni sul server (§18); la sostituzione dei "
      "calcoli lato client è un commit successivo e delimitato")
check("il frontend continua a calcolare da sé",
      "parseIpQuery" in frontend_app and "new Array(rk.u + 1)" in frontend_app,
      "se questi fossero spariti il frontend sarebbe già stato migrato, e la parità "
      "non avrebbe più un riferimento")
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
check("la sorgente pretende una proiezione attuale",
      "require_current_head" in candidates_src,
      "le quattro condizioni della §4 in un posto solo, condiviso con il `GET` e con "
      "le tre interrogazioni")
check("nessun ripiego sull'istantanea in nessun modulo del worker",
      all(t not in worker_sources for t in ("get_current", "InventoryRepository",
                                            "inventory_versions")),
      "il ripiego funzionerebbe, nessuno aprirebbe un ticket, e il difetto di "
      "coerenza resterebbe invisibile (§8.45)")
check("«proiezione non attuale» non conclude il giro",
      "projection_not_current" in worker_src,
      "concludere il giro con un esito farebbe rispondere `already_ran_today` fino a "
      "mezzanotte, cioe' perderebbe la giornata invece di riprovare al tick dopo")
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
check("nessuna migrazione nuova per la fase 2F",
      not any(m.name.startswith("0013") for m in _migrazioni),
      "il ruolo del worker aveva gia' tutte le `SELECT` che gli servono (0009 per la "
      "testa e le versioni, 0010 per le tabelle, 0011 per lo stato, 0012 ribadite): "
      "la 2F verifica quel fatto con una matrice di privilegi, non lo cambia")
check("le REVOKE che tengono il worker in sola lettura sono ancora scritte",
      all(t in (ROOT / "backend" / "migrations" / "versions" /
                "0012_dual_write.py").read_text(encoding="utf-8")
          for t in ("REVOKE INSERT, UPDATE, DELETE, TRUNCATE", "WORKER_ROLE")),
      "il worker manda avvisi: non ha nessun motivo per riscrivere la proiezione che "
      "sta leggendo, e quelle righe sono cio' che un diff mostra accanto a chi provasse")

# --- l'endpoint e lo scanner restano due cose ---
check("l'endpoint delle scadenze non e' stato piegato alla semantica del worker",
      "dismesso" in queries_src and "expired" in queries_src,
      "la vista Scadenze salta i dismessi ed elenca gli scaduti; il worker fa il "
      "contrario. Riconciliarli e' una decisione di prodotto, non di questo commit")
check("il registro del debito semantico esiste ed e' separato",
      "8.48" in (ROOT / "BACKEND-PLAN.md").read_text(encoding="utf-8"),
      "le incoerenze scoperte nella 2E vanno risolte di proposito prima di migrare il "
      "frontend, non scoperte una seconda volta da chi lo migra (§15 della fase 2F)")

# --- cio' che la fase 2F NON fa ---
check("il frontend non e' stato ricablato dalla 2F",
      all(t not in handoff + frontend_app
          for t in ("/api/inventory/search", "/api/inventory/capacity",
                    "/api/inventory/expiries")),
      "la migrazione del frontend e' un commit suo, e va fatta dopo aver risolto le "
      "incoerenze del registro")
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