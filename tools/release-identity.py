#!/usr/bin/env python3
"""L'identità del rilascio: che cosa, ESATTAMENTE, è stato qualificato.

Una qualificazione vale per gli artefatti su cui è stata eseguita e per nessun
altro. «Abbiamo provato la versione di venerdì» non è una frase verificabile: il
requisito della 2J lo dice in una riga — «Do not certify "latest". Certify exact
hashes» — e questo strumento esiste per rendere quella riga eseguibile.

Due categorie di identificatori, e la distinzione conta
-------------------------------------------------------
RIPRODUCIBILE dal repository: il commit di git, i digest delle immagini di BASE,
il digest canonico della baseline, la revisione delle migrazioni, la versione
della mappa relazionale, lo `schemaVersion`, e il digest di ogni file che
l'immagine web serve. Chiunque abbia il commit ottiene gli stessi numeri.

DEL BUILD: l'Id delle immagini `tsm-api` e `tsm-web`. Due build della stessa
sorgente NON danno lo stesso Id — nei livelli finiscono date — quindi questo
numero non identifica la sorgente, identifica il binario che è stato provato.
Serve per sapere se l'immagine caricata sull'host è quella qualificata, e si
confronta, non si ricalcola.

⚠ Tenerle separate non è pedanteria. Un manifesto che mescola le due categorie
suggerisce che ricostruire l'immagine dallo stesso commit ridia lo stesso digest,
e chi ci conta scopre il contrario nel momento peggiore.

Il cancello
-----------
`--check` non stampa: giudica. Rifiuta un albero sporco, una base pinnata per tag
invece che per digest, un digest della baseline che non torna, una somma vendor che
non torna, un file dell'allowlist assente. Sono le condizioni sotto le quali la
frase «abbiamo qualificato questo» non è vera.

Uso:
    python tools/release-identity.py                 # tabella leggibile
    python tools/release-identity.py --json          # per il manifesto
    python tools/release-identity.py --check         # cancello, esce != 0
    python tools/release-identity.py --no-docker     # senza demone (solo sorgente)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

#: Le immagini del rilascio, con il tag che compose.yaml usa. Scritte qui a mano:
#: leggerle da compose.yaml renderebbe il controllo circolare — direbbe soltanto
#: che compose è d'accordo con se stesso.
RELEASE_IMAGES = ("tsm-api:0.4.1", "tsm-web:0.4.1")

#: Dove si dichiarano le basi. Ognuna DEVE essere `nome@sha256:...`.
BASE_IMAGE_SOURCES = (
    ("api", Path("backend/Dockerfile"), r"^ARG PYTHON_IMAGE=(\S+)"),
    ("web", Path("web/Dockerfile"), r"^ARG NGINX_IMAGE=(\S+)"),
    ("db", Path("compose.yaml"), r"^\s+image:\s+(postgres\S+)"),
)

problemi: list[str] = []


def problema(testo: str) -> None:
    problemi.append(testo)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blocco in iter(lambda: f.read(1 << 20), b""):
            h.update(blocco)
    return h.hexdigest()


def run(*cmd: str) -> str:
    """Esegue e restituisce stdout ripulito. Su fallimento: stringa vuota."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout.strip()


# ==================================================================
# 1. git
# ==================================================================
def identita_git() -> dict:
    commit = run("git", "-C", str(ROOT), "rev-parse", "HEAD")
    sporco = run("git", "-C", str(ROOT), "status", "--porcelain")
    if not commit:
        problema("non riesco a leggere il commit di git: l'identità del rilascio "
                 "non è determinabile")
    if sporco:
        # Un albero sporco significa che il codice provato non è il codice
        # committato, e nessuno potrà ricostruirlo.
        # ⚠ Non `r[3:]`: `run()` fa `.strip()` sull'output intero, quindi la PRIMA
        # riga perde lo spazio iniziale del formato porcelain e un taglio a
        # posizione fissa le mangia un carattere del nome. Un difetto piccolo e
        # del tipo peggiore: rende il messaggio di diagnosi vagamente sbagliato,
        # e chi legge cerca un file che non esiste.
        righe = [r for r in sporco.splitlines() if r.strip()]
        nomi = [r.split(maxsplit=1)[-1] for r in righe[:6]]
        problema(f"albero di lavoro SPORCO ({len(righe)} voci): la qualificazione "
                 f"non identificherebbe niente di ricostruibile — "
                 + ", ".join(nomi)
                 + (" ..." if len(righe) > 6 else ""))
    return {
        "commit": commit or None,
        "short": commit[:12] if commit else None,
        "branch": run("git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD") or None,
        "committed": run("git", "-C", str(ROOT), "log", "-1", "--format=%cI") or None,
        "clean": not sporco,
    }


# ==================================================================
# 2. la baseline di produzione
# ==================================================================
def identita_baseline() -> dict:
    art = ROOT / "deploy" / "baseline" / "production-baseline.json"
    modulo = ROOT / "backend" / "app" / "inventory" / "baseline.py"
    if not art.is_file():
        problema(f"artefatto della baseline assente: {art}")
        return {}

    grezzo = art.read_bytes()
    # Il digest CANONICO, che è quello appuntato: si calcola come lo calcola
    # l'applicazione, non sui byte del file. I due coincidono perché l'artefatto
    # è in forma canonica — ed è esattamente ciò che questo controllo prova.
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from app.inventory.digest import canonical_sha256          # type: ignore
        doc = json.loads(grezzo.decode("utf-8"))
        canonico = canonical_sha256(doc)
    except Exception as exc:
        problema(f"non ho potuto calcolare il digest canonico: {type(exc).__name__}: {exc}")
        return {"file_sha256": hashlib.sha256(grezzo).hexdigest()}

    appuntato = None
    # `\s*\(?\s*` perché la costante è scritta fra parentesi su tre righe: un
    # pattern che pretendeva la stringa subito dopo l'uguale non la trovava, e il
    # tool diceva «non trovo il digest appuntato» su un digest presente.
    m = re.search(r'BASELINE_CANONICAL_SHA256\s*=\s*\(?\s*"([0-9a-f]{64})"',
                  modulo.read_text(encoding="utf-8"))
    if m:
        appuntato = m.group(1)
    else:
        problema("non trovo BASELINE_CANONICAL_SHA256 nel modulo della baseline")

    if appuntato and appuntato != canonico:
        problema(f"il digest appuntato non è quello dell'artefatto: "
                 f"appuntato {appuntato[:16]}…, artefatto {canonico[:16]}…")

    conteggi = {}
    try:
        from app.inventory import baseline as bl                   # type: ignore
        conteggi = bl.counts(doc)
    except Exception:
        pass

    # I byte si contano sul BLOB, non sul disco: su Windows l'artefatto ha CRLF e
    # il conteggio locale sarebbe 44154 invece di 42602 — un numero che nessun
    # altro ritroverebbe. Il digest canonico non ne soffre (si calcola sul
    # documento interpretato, non sui byte), ma la dimensione sì, e un manifesto
    # che dichiara una dimensione irriproducibile insegna a non fidarsi delle
    # altre cifre.
    blob = subprocess.run(
        ["git", "-C", str(ROOT), "show", "HEAD:deploy/baseline/production-baseline.json"],
        capture_output=True, timeout=60)
    byte_commit = len(blob.stdout) if blob.returncode == 0 else None

    return {
        "canonical_sha256": canonico,
        "pinned_sha256": appuntato,
        "bytes": byte_commit,
        "bytes_worktree": len(grezzo),
        "counts": conteggi,
    }


# ==================================================================
# 3. schema, mappa, versione del documento
# ==================================================================
def identita_schema() -> dict:
    versioni = sorted(p.name for p in
                      (ROOT / "backend" / "migrations" / "versions").glob("*.py"))
    testa = None
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from app.api.health import expected_head_revision          # type: ignore
        testa = expected_head_revision()
    except Exception as exc:
        problema(f"non ho potuto leggere la revisione di testa: {type(exc).__name__}: {exc}")

    mapper = schema_doc = None
    try:
        from app.inventory.relational import MAPPER_VERSION        # type: ignore
        from app.identity.schema import CURRENT_SCHEMA_VERSION     # type: ignore
        mapper, schema_doc = MAPPER_VERSION, CURRENT_SCHEMA_VERSION
    except Exception as exc:
        problema(f"non ho potuto leggere le versioni del codice: {type(exc).__name__}: {exc}")

    return {
        "migration_head": testa,
        "migration_count": len(versioni),
        "migrations": versioni,
        "mapper_version": mapper,
        "document_schema_version": schema_doc,
    }


# ==================================================================
# 4. le immagini di BASE, che devono essere pinnate per digest
# ==================================================================
def identita_basi() -> dict:
    fuori: dict[str, str] = {}
    for servizio, rel, pattern in BASE_IMAGE_SOURCES:
        path = ROOT / rel
        if not path.is_file():
            problema(f"file assente: {rel}")
            continue
        trovato = None
        for riga in path.read_text(encoding="utf-8").splitlines():
            m = re.match(pattern, riga)
            if m:
                trovato = m.group(1)
                break
        if not trovato:
            problema(f"non trovo la dichiarazione dell'immagine di base in {rel}")
            continue
        fuori[servizio] = trovato
        if "@sha256:" not in trovato:
            # Il difetto che la 2J ha trovato su `nginx:1.27-alpine`: un tag
            # mobile, e lo stesso commit che costruisce due immagini diverse.
            problema(f"l'immagine di base di '{servizio}' è pinnata per TAG e non "
                     f"per digest ({trovato} in {rel}): due build dello stesso "
                     f"commit possono dare due immagini diverse")
    return fuori


# ==================================================================
# 5. le immagini del rilascio (identità del BUILD)
# ==================================================================
def identita_immagini(usa_docker: bool) -> dict:
    if not usa_docker:
        return {}
    fuori: dict[str, dict] = {}
    for tag in RELEASE_IMAGES:
        grezzo = run("docker", "image", "inspect", tag, "--format",
                     "{{.Id}}|{{.Created}}|{{len .RootFS.Layers}}")
        if not grezzo:
            problema(f"immagine del rilascio assente in locale: {tag} "
                     f"(costruirla con `docker compose build`)")
            continue
        pezzi = grezzo.split("|")
        fuori[tag] = {
            "id": pezzi[0],
            "created": pezzi[1] if len(pezzi) > 1 else None,
            "layers": int(pezzi[2]) if len(pezzi) > 2 and pezzi[2].isdigit() else None,
        }
    return fuori


# ==================================================================
# 6. il frontend SERVITO, letto dall'allowlist del Dockerfile
# ==================================================================
def identita_frontend() -> dict:
    """Digest di ogni file che l'immagine web contiene.

    L'elenco si legge dalle righe COPY di web/Dockerfile e non da una lista qui:
    un file aggiunto all'immagine senza passare da qui non esisterebbe per questo
    controllo, che è il modo in cui una identità del rilascio diventa finta.
    """
    dockerfile = ROOT / "web" / "Dockerfile"
    if not dockerfile.is_file():
        problema("web/Dockerfile assente")
        return {}

    sorgenti: list[str] = []
    for riga in dockerfile.read_text(encoding="utf-8").splitlines():
        riga = riga.strip()
        if not riga.startswith("COPY"):
            continue
        m = re.match(r"COPY\s+(\[.*\])\s*$", riga)
        if m:
            try:
                voci = json.loads(m.group(1))
            except json.JSONDecodeError:
                problema(f"riga COPY non interpretabile: {riga}")
                continue
            if len(voci) >= 2:
                sorgenti.append(voci[0])
        else:
            pezzi = riga.split()
            if len(pezzi) >= 3:
                sorgenti.append(pezzi[1])

    fuori: dict[str, str] = {}
    for src in sorgenti:
        path = ROOT / src
        if src.endswith("/"):
            if not path.is_dir():
                problema(f"directory dell'allowlist assente: {src}")
                continue
            for f in sorted(path.rglob("*")):
                if f.is_file():
                    fuori[str(f.relative_to(ROOT)).replace("\\", "/")] = sha256_file(f)
        elif path.is_file():
            fuori[src] = sha256_file(path)
        else:
            problema(f"file dell'allowlist assente: {src}")
    return fuori


# ==================================================================
# 7. le dipendenze vendorizzate del frontend
# ==================================================================
def identita_vendor() -> dict:
    somme = ROOT / "frontend" / "vendor" / "SHA256SUMS"
    if not somme.is_file():
        problema("frontend/vendor/SHA256SUMS assente")
        return {}
    fuori: dict[str, dict] = {}
    # `utf-8-sig`: il file ha un BOM, e senza questo il primo digest arriverebbe
    # con tre byte davanti e non tornerebbe mai.
    for riga in somme.read_text(encoding="utf-8-sig").splitlines():
        riga = riga.strip()
        if not riga:
            continue
        pezzi = riga.split()
        if len(pezzi) != 2:
            problema(f"riga di SHA256SUMS non interpretabile: {riga!r}")
            continue
        atteso, nome = pezzi
        path = ROOT / "frontend" / "vendor" / nome
        if not path.is_file():
            problema(f"dipendenza vendorizzata assente: {nome}")
            continue
        effettivo = sha256_file(path)
        fuori[nome] = {"expected": atteso, "actual": effettivo}
        if atteso != effettivo:
            problema(f"la somma di {nome} non torna: attesa {atteso[:16]}…, "
                     f"trovata {effettivo[:16]}…")
    return fuori


# ==================================================================
# 8. l'ALBERO DEL RILASCIO: i soli file che finiscono su un host di produzione
# ==================================================================
#: File che sono release-relevant ma che nessun COPY nomina, perché non entrano in
#: un'immagine: la composizione, il preflight, l'unità systemd, e l'artefatto della
#: baseline — che in produzione si MONTA in sola lettura invece di essere copiato
#: dentro un container.
FUORI_DALLE_IMMAGINI = (
    "compose.yaml",
    "backend/Dockerfile",
    "web/Dockerfile",
    "deploy/preflight.sh",
    "deploy/tsm.service",
    "deploy/baseline/production-baseline.json",
)

#: Rumore di build: non è contenuto, e includerlo renderebbe il digest diverso a
#: ogni esecuzione di pytest sulla macchina di chi lo calcola.
def _rumore(p: Path) -> bool:
    return "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo")


def identita_albero() -> dict:
    """Digest dell'insieme dei file che vengono DISTRIBUITI, e di nessun altro.

    Esiste per rispondere a una domanda che §4 pone e che il solo SHA di git non
    sa risolvere: dopo la qualificazione si scrive documentazione, e ogni riga di
    documentazione muove il commit. Se l'identità del rilascio fosse soltanto il
    commit, ogni aggiunta al manifesto invaliderebbe formalmente la qualificazione
    che il manifesto descrive — e nessuno rieseguirebbe due giorni di prove per
    una frase in un README. La conseguenza pratica sarebbe peggiore
    dell'imprecisione: si smetterebbe di guardare quel numero.

    Quindi si distinguono le due cose. Il commit dice DA DOVE viene il rilascio;
    questo digest dice CHE COSA verrebbe installato. Un commit che tocca solo
    `tools/`, `backend/tests/` o un `.md` lascia questo numero identico — e la
    frase «la documentazione aggiunta dopo non cambia ciò che è stato qualificato»
    diventa una cosa che si verifica invece di una cosa che si promette.

    L'insieme si deduce dalle righe COPY dei due Dockerfile, non da un elenco
    scritto qui: un file aggiunto a un'immagine senza passare da un COPY non
    esiste, e un elenco a mano si sarebbe scordato proprio quello.
    """
    def sorgenti_copy(dockerfile: Path, base: Path) -> list[Path]:
        fuori: list[Path] = []
        if not dockerfile.is_file():
            problema(f"Dockerfile assente: {dockerfile}")
            return fuori
        for riga in dockerfile.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if not riga.startswith("COPY"):
                continue
            m = re.match(r"COPY\s+(\[.*\])\s*$", riga)
            if m:
                try:
                    voci = json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
                if len(voci) >= 2:
                    fuori.append(base / voci[0])
                continue
            # Forma shell: `COPY [--flag ...] <src>... <dest>`. Le opzioni si
            # scartano; l'ultimo campo è la destinazione dentro l'immagine.
            pezzi = [x for x in riga.split()[1:] if not x.startswith("--")]
            # `COPY --from=builder /install /usr/local` non è contenuto del
            # repository: è il risultato dello stadio precedente.
            if any(x.startswith("--from=") for x in riga.split()):
                continue
            for src in pezzi[:-1]:
                fuori.append(base / src)
        return fuori

    candidati: list[Path] = []
    candidati += sorgenti_copy(ROOT / "backend" / "Dockerfile", ROOT / "backend")
    candidati += sorgenti_copy(ROOT / "web" / "Dockerfile", ROOT)
    candidati += [ROOT / x for x in FUORI_DALLE_IMMAGINI]
    # requirements.txt: lo COPY dello stadio builder lo nomina, quindi arriva da
    # sorgenti_copy. Se un giorno non ci fosse più, il controllo qui sotto
    # sull'assenza lo direbbe.

    percorsi: list[str] = []
    for cand in candidati:
        if cand.is_dir():
            for f in sorted(cand.rglob("*")):
                if f.is_file() and not _rumore(f):
                    percorsi.append(str(f.relative_to(ROOT)).replace("\\", "/"))
        elif cand.is_file():
            percorsi.append(str(cand.relative_to(ROOT)).replace("\\", "/"))
        else:
            problema(f"file dell'albero del rilascio assente: "
                     f"{cand.relative_to(ROOT) if cand.is_relative_to(ROOT) else cand}")
    percorsi = sorted(set(percorsi))

    # ------------------------------------------------------------------
    # Il contenuto si prende da GIT, non dal disco.
    #
    # ⚠ Non è un dettaglio di implementazione, è la differenza fra un numero
    # riproducibile e un numero locale. Il repository si modifica su Windows e si
    # esegue su Linux, `core.autocrlf` è attivo, e `.gitattributes` impone LF a
    # `.sh`, `.service`, `.conf` e `Dockerfile` — non a `.py`, `.json`, `.js`,
    # `.css`, `.html`. Su una copia di lavoro Windows quei file hanno CRLF; nel
    # commit hanno LF. Misurato durante la 2J: 39 dei 117 file dell'albero del
    # rilascio differiscono fra copia di lavoro e commit, per il solo `\r`.
    #
    # Un digest calcolato sul disco direbbe quindi una cosa diversa su Windows e
    # su Linux A PARI COMMIT, cioè non identificherebbe il rilascio: sarebbe un
    # numero che vale solo sulla macchina che l'ha stampato, presentato come se
    # valesse per tutti. L'oggetto blob di git è la stessa cosa su ogni
    # piattaforma, ed è ciò che un host di produzione riceve.
    #
    # Conseguenza operativa, dichiarata nel manifesto: le immagini del rilascio si
    # costruiscono sull'host di qualificazione a partire da `git archive`, non
    # dalla copia di lavoro Windows. Un'immagine costruita da questa copia
    # contiene i CRLF e NON è l'immagine che quel commit produce in produzione.
    blob = run("git", "-C", str(ROOT), "ls-tree", "-r", "-z", "HEAD", "--", *percorsi)
    ids: dict[str, str] = {}
    for voce in blob.split("\0"):
        if not voce.strip():
            continue
        # `<mode> <type> <object>\t<path>`
        try:
            meta, nome = voce.split("\t", 1)
            ids[nome] = meta.split()[2]
        except (ValueError, IndexError):
            problema(f"riga di ls-tree non interpretabile: {voce!r}")

    mancanti = [p for p in percorsi if p not in ids]
    if mancanti:
        problema(f"{len(mancanti)} file dell'albero del rilascio non sono nel "
                 f"commit (non committati?): {', '.join(mancanti[:5])}"
                 + (" ..." if len(mancanti) > 5 else ""))

    # Quali file, sul disco, NON corrispondono al commit. Non è un problema di
    # per sé — su Windows è la norma — ma dice se un'immagine costruita QUI
    # corrisponde al commito che si sta qualificando.
    # L'id di un blob si calcola qui invece di chiamare `git hash-object` 117
    # volte: su Windows sono dieci secondi di creazione di processi per un numero
    # che è sha1("blob <lunghezza>\0" + contenuto), cioè due righe.
    def blob_id(dati: bytes) -> str:
        h = hashlib.sha1()
        h.update(b"blob %d\0" % len(dati))
        h.update(dati)
        return h.hexdigest()

    divergenti: list[str] = []
    for nome in percorsi:
        f = ROOT / nome
        if not f.is_file() or nome not in ids:
            continue
        if blob_id(f.read_bytes()) != ids[nome]:
            divergenti.append(nome)

    h = hashlib.sha256()
    for nome in sorted(ids):
        h.update(nome.encode("utf-8"))
        h.update(b"\0")
        h.update(ids[nome].encode("ascii"))
        h.update(b"\n")
    return {
        "sha256": h.hexdigest(),
        "files": len(ids),
        "paths": sorted(ids),
        "source": "git blob ids at HEAD",
        "worktree_differs": sorted(divergenti),
    }


# ==================================================================
def raccogli(usa_docker: bool) -> dict:
    return {
        "git": identita_git(),
        "release_tree": identita_albero(),
        "baseline": identita_baseline(),
        "schema": identita_schema(),
        "base_images": identita_basi(),
        "release_images": identita_immagini(usa_docker),
        "frontend": identita_frontend(),
        "vendor": identita_vendor(),
    }


def stampa(ident: dict) -> None:
    def riga(k: str, v: object) -> None:
        print(f"  {k:<26} {v}")

    print("\n=== IDENTITÀ DEL RILASCIO ===\n")
    print("riproducibile dal repository")
    g = ident["git"]
    riga("commit", f"{g.get('commit')}  ({'pulito' if g.get('clean') else 'SPORCO'})")
    riga("ramo", g.get("branch"))
    riga("data del commit", g.get("committed"))
    a = ident["release_tree"]
    riga("albero del rilascio", f"{a.get('sha256')}")
    riga("", f"{a.get('files')} file distribuiti, dai blob del commit")
    if a.get("worktree_differs"):
        riga("", f"⚠ {len(a['worktree_differs'])} differiscono sul disco "
                 f"(CRLF): un'immagine costruita QUI non è quella del commit")
    b = ident["baseline"]
    riga("baseline (canonico)", b.get("canonical_sha256"))
    riga("baseline (appuntato)", b.get("pinned_sha256"))
    riga("baseline (byte)", b.get("bytes"))
    if b.get("counts"):
        riga("baseline (struttura)", json.dumps(b["counts"], sort_keys=True))
    s = ident["schema"]
    riga("revisione migrazioni",
         f"{s.get('migration_head')}  ({s.get('migration_count')} migrazioni)")
    riga("versione della mappa", s.get("mapper_version"))
    riga("schemaVersion", s.get("document_schema_version"))
    for servizio, img in ident["base_images"].items():
        riga(f"base {servizio}", img)
    riga("file serviti dal web", f"{len(ident['frontend'])} file, digest raccolti")
    for nome, v in ident["vendor"].items():
        stato = "ok" if v["expected"] == v["actual"] else "NON TORNA"
        riga(f"vendor {nome}", f"{v['actual'][:16]}…  {stato}")

    if ident["release_images"]:
        print("\nidentità del build (NON ricostruibile dalla sorgente)")
        for tag, v in ident["release_images"].items():
            riga(tag, v["id"])
            riga("", f"costruita {v['created']}, {v['layers']} livelli")


def main() -> int:
    ap = argparse.ArgumentParser(description="identità del rilascio")
    ap.add_argument("--json", action="store_true", help="output per il manifesto")
    ap.add_argument("--check", action="store_true", help="cancello: esce != 0 sui problemi")
    ap.add_argument("--no-docker", action="store_true", help="salta le immagini locali")
    args = ap.parse_args()

    ident = raccogli(usa_docker=not args.no_docker)

    if args.json:
        print(json.dumps({"identity": ident, "problems": problemi},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        stampa(ident)
        if problemi:
            print(f"\n⚠ {len(problemi)} problemi:")
            for p in problemi:
                print(f"  - {p}")
        else:
            print("\nnessun problema: gli identificatori sono coerenti.")

    if args.check and problemi:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
