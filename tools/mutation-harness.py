#!/usr/bin/env python3
"""Mutazioni: si guasta una cosa per volta e si pretende che qualcuno se ne accorga.

Un test verde dice che il codice fa quello che il test si aspetta. Non dice che il
test si accorgerebbe del contrario — e su una fase il cui contenuto sono dei **no**
(cancelli che rifiutano, guardie che si oppongono, strumenti che giudicano) quella è
l'unica domanda che conta. Un cancello che dice sempre sì passa qualunque prova
scritta per il caso felice.

Le quattro proprietà che §33 pretende, e perché
----------------------------------------------
1. **ALBERO PULITO ALL'INIZIO.** Con modifiche non committate in giro non si sa più
   che cosa si sta mutando, e il ripristino cancellerebbe lavoro.
2. **PASSATA DI CONTROLLO, senza mutazioni, PRIMA di tutto.** Una sonda che non sa
   vedere il verde non ha nessun diritto di dichiarare il rosso. Gli strumenti
   delle fasi precedenti hanno avuto quattro difetti propri, tutti nella direzione
   peggiore — «le protezioni non funzionano» quando funzionavano — e tutti
   sarebbero stati presi da questa passata.
3. **RIPRISTINO AFFIDABILE.** Si ripristina da `git`, non da una copia in memoria:
   se il processo muore a metà, `git checkout` rimette a posto comunque, mentre una
   copia in RAM se ne va col processo.
4. **RIFIUTO DI FINIRE CON L'ALBERO SPORCO.** Se al termine qualcosa è rimasto
   modificato, lo strumento lo dice e esce male. Una mutazione dimenticata nel
   codice è peggio di una mutazione non provata.

Uso:
    python tools/mutation-harness.py                    # statiche + pure
    python tools/mutation-harness.py --with-pg          # anche quelle su PostgreSQL
    python tools/mutation-harness.py --only nginx-header
    python tools/mutation-harness.py --list
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

#: Gli intercettatori: comando, e quanto ci mette. Si prova il più economico prima,
#: perché una mutazione presa dai controlli statici non ha bisogno di far girare
#: PostgreSQL.
INTERCETTATORI = {
    "statico": [sys.executable, "tools/storage-config-test.py"],
    "puro": [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:warnings",
             "--tb=no", "tests/test_production_baseline.py",
             "tests/test_qualification_fixture.py"],
    "pg": ["pwsh-o-powershell", "segnaposto"],   # vedi `_comando_pg`
}


def _comando_pg(selezione: str) -> list[str]:
    """La suite su PostgreSQL gira nel container, come sempre in questo progetto."""
    return ["powershell", "-NoProfile", "-Command",
            f".\\tools\\run-backend-tests.ps1 -KeepDb -- -q --no-header "
            f"-p no:warnings --tb=no -k '{selezione}'"]


#: Una mutazione: nome, file, il testo da sostituire, con che cosa, e chi deve
#: accorgersene. Il testo `prima` deve comparire ESATTAMENTE UNA VOLTA nel file:
#: se compare zero volte la mutazione non è stata applicata (e lo strumento lo
#: dice invece di dichiararla intercettata), se compare più volte non si sa che
#: cosa si è cambiato.
MUTAZIONI: list[dict] = [
    # ---------------------------------------------------------- identità (§4)
    {"nome": "base-web-a-tag", "tipo": "statico",
     "file": "web/Dockerfile",
     "prima": "ARG NGINX_IMAGE=nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10",
     "dopo": "ARG NGINX_IMAGE=nginx:1.27-alpine",
     "perche": "una base pinnata per tag fa costruire due immagini diverse dallo "
               "stesso commit: l'identità del rilascio non identifica più niente"},
    {"nome": "identita-dal-disco", "tipo": "statico",
     "file": "tools/release-identity.py",
     "prima": '"worktree_differs": sorted(divergenti),',
     "dopo": '"worktree_ignorato": sorted(divergenti),',
     "perche": "senza dichiarare quali file divergono dal commit, nessuno sa che "
               "l'immagine costruita qui non è quella del commit"},
    {"nome": "identita-senza-api", "tipo": "statico",
     "file": "tools/release-identity.py",
     "prima": '("api", Path("backend/Dockerfile"), r"^ARG PYTHON_IMAGE=(\\S+)"),',
     "dopo": "",
     "perche": "un servizio non elencato non verrebbe mai controllato, e la sua "
               "base potrebbe tornare a un tag senza che niente lo dica"},

    # ---------------------------------------------------------- nginx (§16)
    {"nome": "nginx-header", "tipo": "statico",
     "file": "web/nginx.conf",
     "prima": "        location ^~ /vendor/ {\n            root /srv/app;\n            expires 30d;\n        }",
     "dopo": "        location ^~ /vendor/ {\n            root /srv/app;\n            expires 30d;\n            add_header Cache-Control \"public, immutable\";\n        }",
     "perche": "è il difetto trovato dalla 2J: un `add_header` in quel blocco "
               "riporta via tutte e quattro le intestazioni di sicurezza, e "
               "`nosniff` sparisce dal JavaScript vendorizzato"},

    # ---------------------------------------------------------- qualificazione
    {"nome": "hsty-solo-radice", "tipo": "statico",
     "file": "tools/release-qualification.py",
     "prima": '"/fonts/PublicSans-400.woff2", "/domain.js", "/index.html"):',
     "dopo": '"/domain.js", "/index.html"):',
     "perche": "chiedere le intestazioni solo dove ci sono già è il modo in cui "
               "il difetto di nginx sarebbe rimasto invisibile"},
    {"nome": "sweep-senza-controprova", "tipo": "statico",
     "file": "tools/release-qualification.py",
     "prima": 'check(f"controprova: la scansione TROVA {etichetta}", bool(colpi),',
     "dopo": 'check(f"controprova: la scansione TROVA {etichetta}", True,',
     "perche": "senza controprova «nessun segreto trovato» non distingue un "
               "database pulito da una scansione che non guarda niente"},
    {"nome": "pool-senza-invariante", "tipo": "statico",
     "file": "tools/release-qualification.py",
     "prima": 'check("capienza(letture) >= capienza(richieste)", cap_read >= cap_req,',
     "dopo": 'check("capienza(letture) >= capienza(richieste)", True,',
     "perche": "ridurre il pool di lettura reintroduce lo stallo da acquisizione "
               "a due fasi, che si manifesta solo sotto carico"},

    # ---------------------------------------------------------- guasti (§31)
    {"nome": "guasto-mancante", "tipo": "statico",
     "file": "tools/failure-injection-test.py",
     "prima": '        "disco-assente": guasto_disco_assente,\n',
     "dopo": "",
     "perche": "il caso del disco inesistente è quello che impedisce a PostgreSQL "
               "di fare initdb sul filesystem di root"},
    {"nome": "iniezioni-senza-consenso", "tipo": "statico",
     "file": "tools/failure-injection-test.py",
     "prima": 'ap.add_argument("--allow-destructive", action="store_true", required=True,',
     "dopo": 'ap.add_argument("--allow-destructive", action="store_true", required=False,',
     "perche": "uno strumento che ferma servizi e corrompe righe non deve poter "
               "partire senza che qualcuno lo dichiari"},

    # ---------------------------------------------------------- ripristino (§29)
    {"nome": "istantanea-senza-losetup", "tipo": "statico",
     "file": "tools/backup-restore-test.sh",
     "prima": '    for L in $(losetup -j "$IMG" -O NAME --noheadings 2>/dev/null); do\n        echo "  stacco $L"\n        losetup -d "$L" || true\n    done\n    sync\n',
     "dopo": "",
     "perche": "è il difetto che ha prodotto un'istantanea inutilizzabile: "
               "PostgreSQL al ripristino è uscito con «PANIC: could not locate a "
               "valid checkpoint record»"},
    {"nome": "ripristino-sempre-ok", "tipo": "statico",
     "file": "tools/backup-restore-test.sh",
     "prima": 'ck "lo stato è CAMBIATO: il volume è stato davvero sostituito"',
     "dopo": 'ck "lo stato del volume"',
     "perche": "un controllo che non può fallire dichiarava riuscito un ripristino "
               "che non era avvenuto"},

    # ---------------------------------------------------------- riavvio (§7)
    {"nome": "riavvio-con-orario", "tipo": "statico",
     "file": "tools/reboot-restart-test.sh",
     "prima": "    journalctl -u tsm.service -n 0 --show-cursor --no-pager 2>/dev/null \\\n        | sed -n 's/^-- cursor: //p'",
     "dopo": "    date '+%Y-%m-%d %H:%M:%S'",
     "perche": "l'orologio dell'host non è monotono fra un avvio e l'altro: un "
               "filtro temporale seleziona un insieme che non corrisponde a niente"},

    # ---------------------------------------------------------- notifiche (§19)
    {"nome": "atteso-dedotto", "tipo": "statico",
     "file": "tools/notification-qualification.sh",
     "prima": "ATTESI_NOTIFICABILI=14",
     "dopo": "ATTESI_NOTIFICABILI=0",
     "perche": "l'atteso della fixture è dichiarato dal requisito: se lo si può "
               "cambiare senza che niente diventi rosso, non è più un atteso"},
    {"nome": "sink-senza-prova-scrittura", "tipo": "statico",
     "file": "tools/smtp-sink.py",
     "prima": '        prova = DIR / ".prova-di-scrittura"',
     "dopo": '        prova = DIR / ".prova-non-fatta" if False else None',
     "perche": "un relay che dice sì e non conserva è peggio di un relay che non "
               "parte, e fa attribuire al worker un guasto di trasporto"},

    # ---------------------------------------------------------- baseline (§36)
    {"nome": "digest-baseline", "tipo": "puro",
     "file": "backend/app/inventory/baseline.py",
     "prima": '    "febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"',
     "dopo": '    "0000000000000000000000000000000000000000000000000000000000000000"',
     "perche": "il digest appuntato è ciò che impedisce a un documento qualunque "
               "di essere installato come baseline"},
    {"nome": "conteggio-dispositivi", "tipo": "puro",
     "file": "backend/app/inventory/baseline.py",
     "prima": 'BASELINE_HAS_DEVICES = "baseline_has_devices"',
     "dopo": 'BASELINE_HAS_DEVICES = "baseline_ok_devices"',
     "perche": "il codice del rifiuto è il contratto: cambiandolo, chi cerca quel "
               "codice nei log non lo trova più"},

    # ---------------------------------------------------------- worker (§7, 2J)
    {"nome": "lock-mai-riafferrato", "tipo": "pg", "selezione": "singleton or RIPRESO or RINUNCIA",
     "file": "backend/app/notifications/worker.py",
     "prima": "    try:\n        return bool(conn.execute(_LOCK_HELD, {\"k\": WORKER_LOCK_KEY}).scalar_one())\n    except Exception:\n        return False",
     "dopo": "    return True",
     "perche": "è il difetto che la 2J ha trovato: senza riaffermare il lock, dopo "
               "un riavvio di PostgreSQL la garanzia «un solo worker» è falsa e "
               "niente lo dice"},
    {"nome": "lock-strappato", "tipo": "pg", "selezione": "singleton or RIPRESO or RINUNCIA",
     "file": "backend/app/notifications/worker.py",
     "prima": "    nuova = engine.connect()\n    if acquire_singleton(nuova):\n        log.info(\"lock del worker ripreso\")\n        return nuova\n    nuova.close()\n    return None",
     "dopo": "    nuova = engine.connect()\n    acquire_singleton(nuova)\n    return nuova",
     "perche": "strappare il lock a un altro processo lascia due worker convinti "
               "entrambi di essere l'unico"},
]

VERDE = "\033[32m"
ROSSO = "\033[31m"
FINE = "\033[0m"


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       timeout=3600)
    return p.returncode, (p.stdout + p.stderr)


def albero_sporco() -> str:
    _, out = run(["git", "status", "--porcelain"], ROOT)
    return out.strip()


def intercetta(m: dict) -> tuple[int, str]:
    tipo = m["tipo"]
    if tipo == "pg":
        return run(_comando_pg(m.get("selezione", "worker")), ROOT)
    cmd = INTERCETTATORI[tipo]
    cwd = ROOT / "backend" if tipo == "puro" else ROOT
    return run(cmd, cwd)


def main() -> int:
    ap = argparse.ArgumentParser(description="mutazioni")
    ap.add_argument("--with-pg", action="store_true",
                    help="esegue anche le mutazioni la cui rete è una suite su PostgreSQL")
    ap.add_argument("--only", default="", help="nomi separati da virgola")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for m in MUTAZIONI:
            print(f"  {m['tipo']:<8} {m['nome']:<28} {m['file']}")
        return 0

    scelte = [x.strip() for x in args.only.split(",") if x.strip()]
    lotto = [m for m in MUTAZIONI
             if (not scelte or m["nome"] in scelte)
             and (args.with_pg or m["tipo"] != "pg")]
    if not lotto:
        print("nessuna mutazione selezionata", file=sys.stderr)
        return 2

    # ---- proprietà 1: albero pulito
    sporco = albero_sporco()
    if sporco:
        print("l'albero di lavoro NON è pulito: non si mutano modifiche non "
              "committate, e il ripristino le cancellerebbe.\n")
        print(sporco[:1500])
        return 2
    print("albero pulito.\n")

    # ---- proprietà 2: passata di CONTROLLO, senza mutazioni
    print("=" * 74)
    print("CONTROLLO senza mutazioni — una sonda che non sa vedere il verde non ha")
    print("nessun diritto di dichiarare il rosso.")
    print("=" * 74)
    tipi = sorted({m["tipo"] for m in lotto})
    for tipo in tipi:
        finto = {"tipo": tipo, "selezione": "singleton or RIPRESO or RINUNCIA"}
        rc, out = intercetta(finto)
        stato = f"{VERDE}verde{FINE}" if rc == 0 else f"{ROSSO}ROSSO{FINE}"
        print(f"  intercettatore «{tipo}»: {stato} (uscita {rc})")
        if rc != 0:
            print(out[-1500:])
            print("\nil controllo non è verde: mi fermo. Qualunque «intercettata» "
                  "che seguisse sarebbe priva di significato.")
            return 2
    print()

    # ---- le mutazioni
    intercettate, sfuggite, non_applicate = [], [], []
    for i, m in enumerate(lotto, 1):
        f = ROOT / m["file"]
        originale = f.read_text(encoding="utf-8")
        n = originale.count(m["prima"])
        print("=" * 74)
        print(f"[{i}/{len(lotto)}] {m['nome']}  ({m['tipo']})  {m['file']}")
        print(f"        {m['perche']}")
        if n != 1:
            # ⚠ Non si dichiara «intercettata»: non si è mutato niente. È la
            # differenza fra una protezione che funziona e una mutazione che non è
            # mai avvenuta, e confonderle è il modo più rapido di credersi al
            # sicuro.
            print(f"  {ROSSO}NON APPLICATA{FINE}: il testo compare {n} volte "
                  f"(attesa 1)")
            non_applicate.append(m["nome"])
            continue
        try:
            f.write_text(originale.replace(m["prima"], m["dopo"], 1), encoding="utf-8")
            rc, out = intercetta(m)
            if rc != 0:
                print(f"  {VERDE}INTERCETTATA{FINE} (uscita {rc})")
                intercettate.append(m["nome"])
            else:
                print(f"  {ROSSO}SFUGGITA{FINE}: nessuno se n'è accorto")
                sfuggite.append(m["nome"])
        finally:
            # ---- proprietà 3: ripristino da git, non da una copia in memoria
            run(["git", "checkout", "--", m["file"]], ROOT)

    # ---- proprietà 4: rifiuto di finire con l'albero sporco
    print("\n" + "=" * 74)
    sporco = albero_sporco()
    if sporco:
        print(f"{ROSSO}L'ALBERO È RIMASTO SPORCO{FINE}: una mutazione non è stata "
              f"ripristinata.")
        print(sporco[:1000])
        return 2
    print("albero pulito alla fine: tutte le mutazioni sono state ripristinate.")

    print(f"\nintercettate: {len(intercettate)} / {len(lotto)}")
    if non_applicate:
        print(f"{ROSSO}non applicate:{FINE} {non_applicate}")
    if sfuggite:
        print(f"{ROSSO}SFUGGITE:{FINE} {sfuggite}")
        print("\nUna mutazione sfuggita non è un test da aggiungere e basta: prima "
              "va capito che cosa quella protezione stava proteggendo, e se lo "
              "stava proteggendo davvero.")
    return 0 if (not sfuggite and not non_applicate) else 1


if __name__ == "__main__":
    sys.exit(main())
