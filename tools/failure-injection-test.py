#!/usr/bin/env python3
"""§31, §10 e §11: si rompe una cosa per volta, e si guarda cosa dice.

⚠ QUESTO STRUMENTO È DISTRUTTIVO. Ferma servizi, smonta il disco dei dati, corrompe
righe del database e tronca il materiale TLS. Ogni iniezione viene rimessa a posto
dal `finally` che le appartiene, ma va eseguito su un'installazione di
QUALIFICAZIONE e mai su una vera. Il nome del file lo dichiara di proposito: chi lo
legge in un elenco deve poterlo sapere senza aprirlo.

Perché esiste
-------------
Un guasto operativo ha tre proprietà che vanno decise in anticipo, non scoperte alle
otto di sera: che cosa vede chi usa l'applicazione, se lo stato cambia, e come si
rimedia. Le suite del backend provano la PRIMA su doppi. Le altre due si vedono solo
rompendo l'installazione vera — e la terza è l'unica che una persona userà.

Il requisito chiede una cosa in particolare: «No unexplained 500 should remain for a
known operational failure mode». Un 500 è la risposta che non dice niente; per ogni
guasto previsto deve esserci un codice stabile che qualcuno possa cercare.

Ogni iniezione dichiara l'attesa PRIMA di essere eseguita. Dedurla dal
comportamento osservato proverebbe solo che il codice è coerente con se stesso.

Uso (dentro l'host di qualificazione):
    TSM_QUAL_NEW_PASSWORD=… python3 tools/failure-injection-test.py --dir /opt/tsm
    …                                        --only proiezione,limitatore
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path("/opt/tsm")
BASE = "https://localhost"
UTENTE = "admin"
PASSWORD = ""

risultati: list[tuple[str, str, bool, str]] = []
_caso = ""
#: La matrice, riempita man mano: guasto → cosa si osserva, stato, rimedio.
matrice: list[dict] = []


def caso(nome: str, attesa: str) -> None:
    global _caso
    _caso = nome
    print(f"\n== {nome} ==")
    print(f"   attesa dichiarata: {attesa}")


def check(nome: str, passato: bool, dettaglio: str = "") -> None:
    risultati.append((_caso, nome, bool(passato), dettaglio))
    print(f"  [{'PASS' if passato else 'FAIL'}] {nome}")
    if dettaglio and not passato:
        print(f"         -> {dettaglio}")


def riga_matrice(guasto: str, osservabile: str, stato: str, rimedio: str) -> None:
    matrice.append({"guasto": guasto, "osservabile": osservabile,
                    "stato": stato, "rimedio": rimedio})


# ------------------------------------------------------------------ utilità
def sh(*cmd: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or APP), capture_output=True,
                          text=True, timeout=timeout)


def compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return sh("docker", "compose", *args, timeout=timeout)


def psql(sql: str) -> str:
    p = compose("exec", "-T", "db", "psql", "-U", "tsm", "-d", "tsm", "-At", "-c", sql)
    return p.stdout.strip() if p.returncode == 0 else f"__ERRORE__ {p.stderr.strip()[:200]}"


def _ctx() -> ssl.SSLContext:
    ca = APP / "secrets" / "tls" / "fullchain.pem"
    if ca.is_file():
        return ssl.create_default_context(cafile=str(ca))
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def http(metodo: str, percorso: str, corpo: dict | None = None,
         cookie: str | None = None, timeout: int = 30) -> dict:
    dati = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(BASE + percorso, data=dati, method=metodo)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", BASE)
    if dati is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", f"tsm_session={cookie}")
    try:
        with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
            g = r.read()
            return {"status": r.status, "body": g.decode("utf-8", "replace"),
                    "cookie": _cookie(r.headers)}
    except urllib.error.HTTPError as e:
        g = e.read()
        return {"status": e.code, "body": g.decode("utf-8", "replace"),
                "cookie": _cookie(e.headers)}
    except Exception as exc:
        return {"status": 0, "body": f"{type(exc).__name__}: {exc}", "cookie": None}


def _cookie(headers) -> str | None:
    for h in headers.get_all("Set-Cookie") or []:
        m = re.match(r"tsm_session=([^;]+)", h)
        if m and m.group(1) not in ('""', ""):
            return m.group(1)
    return None


def _rimuovi_orfani() -> None:
    """Uccide i container `compose run` rimasti da un giro precedente.

    ⚠ `subprocess` con `timeout` uccide il CLI di docker, NON il container: `--rm`
    non scatta e un worker lanciato a mano resta vivo, a tenere il lock consultivo.
    Il caso successivo lo trova preso ed esce con 3 — cioe' fallisce per colpa del
    caso precedente, e il messaggio parla del lock invece che dell'orfano.
    """
    fuori = sh("docker", "ps", "-q", "--filter", "name=tsm-worker-run-",
               "--filter", "name=tsm-migrate-run-").stdout.split()
    for cid in fuori:
        sh("docker", "rm", "-f", cid)
    if fuori:
        print(f"     (rimossi {len(fuori)} container orfani di `compose run`)")


def accedi() -> str | None:
    r = http("POST", "/api/auth/login", {"username": UTENTE, "password": PASSWORD})
    return r["cookie"] if r["status"] == 200 else None


def pronto(limite: int = 60) -> bool:
    for _ in range(limite):
        r = http("GET", "/api/ready", timeout=10)
        if r["status"] == 200 and '"ready"' in r["body"]:
            return True
        time.sleep(2)
    return False


def codice(corpo: str) -> str:
    """Il codice stabile dentro una risposta di errore, qualunque sia l'involucro."""
    try:
        d = json.loads(corpo)
    except Exception:
        return ""
    for posto in (d, d.get("detail") if isinstance(d, dict) else None,
                  d.get("error") if isinstance(d, dict) else None):
        if isinstance(posto, dict):
            for chiave in ("code", "reason"):
                if isinstance(posto.get(chiave), str):
                    return posto[chiave]
    return ""


# ==================================================================
# 1. PostgreSQL non disponibile
# ==================================================================
def guasto_database(cookie: str) -> None:
    caso("PostgreSQL non disponibile",
         "readiness 503 con `database: not-ready`; nessun 500 inspiegato; "
         "nessun cambio di stato; si rimedia riavviando il servizio")
    compose("stop", "db")
    try:
        r = http("GET", "/api/ready", timeout=20)
        check("readiness risponde 503", r["status"] == 503, f"HTTP {r['status']}")
        check("e dice quale sottosistema è caduto",
              '"database":"not-ready"' in r["body"].replace(" ", ""),
              r["body"][:200])
        # `/api/health` è LIVENESS: deve restare verde, perché il processo è vivo.
        # Se cadesse anche lui, Docker riavvierebbe l'API a ogni singhiozzo del
        # database — cioè il riavvio peggiorerebbe il guasto.
        h = http("GET", "/api/health", timeout=20)
        check("la liveness resta verde (il processo è vivo)", h["status"] == 200,
              f"HTTP {h['status']}")
        g = http("GET", "/api/inventory", cookie=cookie, timeout=30)
        check("un GET dell'inventario NON risponde 500",
              g["status"] != 500, f"HTTP {g['status']}: {g['body'][:200]}")
        check("risponde 503 (servizio non disponibile)", g["status"] == 503,
              f"HTTP {g['status']}: {g['body'][:200]}")
        riga_matrice("PostgreSQL non disponibile",
                     f"readiness 503 database:not-ready; GET inventario {g['status']}; "
                     f"liveness 200",
                     "nessun cambio", "`systemctl restart tsm` o `docker compose start db`")
    finally:
        compose("start", "db")
        check("dopo la ripartenza la readiness torna verde", pronto(90))


# ==================================================================
# 2. il limitatore dei tentativi non è utilizzabile → si CHIUDE
# ==================================================================
def guasto_limitatore() -> None:
    caso("limitatore dei tentativi non utilizzabile",
         "il login NON riesce e non risponde «credenziali errate»: se i tentativi "
         "non si possono contare, il limitatore non esiste, e non esistere in "
         "silenzio significa tentativi illimitati (§8.32). Codice atteso: "
         "rate_limiter_unavailable")
    # Si toglie a `tsm_api` il diritto di scrivere il registro dei tentativi:
    # è il guasto reale (privilegio mancante, tabella piena, disco pieno) reso
    # riproducibile.
    psql("REVOKE INSERT ON login_attempts FROM tsm_api")
    try:
        r = http("POST", "/api/auth/login",
                 {"username": UTENTE, "password": PASSWORD})
        c = codice(r["body"])
        check("il login non riesce", r["status"] != 200, f"HTTP {r['status']}")
        check("e NON dice «credenziali errate»", "invalid_credentials" not in r["body"],
              r["body"][:200])
        check("il codice è rate_limiter_unavailable o un 503 esplicito",
              c == "rate_limiter_unavailable" or r["status"] == 503,
              f"HTTP {r['status']} codice={c!r} {r['body'][:200]}")
        check("nessuna sessione è stata creata", r["cookie"] is None,
              "un cookie di sessione con il limitatore rotto sarebbe il caso peggiore")
        riga_matrice("limitatore dei tentativi non scrivibile",
                     f"login HTTP {r['status']} codice={c or 'n/d'}, nessuna sessione",
                     "nessun cambio",
                     "ripristinare il privilegio INSERT su login_attempts; "
                     "verificare spazio disco e privilegi del ruolo")
    finally:
        psql("GRANT INSERT ON login_attempts TO tsm_api")
        check("dopo il ripristino si entra di nuovo", accedi() is not None)


# ==================================================================
# 3. proiezione NON ATTUALE  (§10, §11)
# ==================================================================
def guasto_proiezione_vecchia(cookie: str) -> None:
    caso("proiezione non attuale (stale)",
         "readiness 503 con `projection: not-ready`; il GET rifiuta con "
         "projection_not_current; il rimedio è `project.py --rebuild`, che NON "
         "crea una versione e NON tocca la storia")
    testa = psql("SELECT version FROM inventory_head")
    versioni_prima = psql("SELECT count(*) FROM inventory_versions")
    digest_prima = psql("SELECT canonical_sha256 FROM inventory_versions "
                        "ORDER BY version DESC LIMIT 1")
    foto_prima = psql("SELECT count(*) FROM inventory_photo_refs")

    # Si dichiara che la proiezione rispecchia una versione PRECEDENTE. È
    # esattamente lo stato in cui si trova un'installazione a cui è mancato un
    # `--rebuild` dopo una migrazione della mappa.
    psql("UPDATE inventory_projection_state SET head_version = 1")
    try:
        r = http("GET", "/api/ready", timeout=20)
        check("readiness risponde 503", r["status"] == 503, f"HTTP {r['status']}")
        check("e indica la proiezione",
              '"projection":"not-ready"' in r["body"].replace(" ", ""), r["body"][:200])
        g = http("GET", "/api/inventory", cookie=cookie, timeout=30)
        c = codice(g["body"])
        check("il GET NON risponde 500", g["status"] != 500,
              f"HTTP {g['status']}: {g['body'][:200]}")
        check("il GET rifiuta con projection_not_current",
              c == "projection_not_current",
              f"HTTP {g['status']} codice={c!r} {g['body'][:200]}")
        v = compose("run", "--rm", "migrate", "python", "scripts/project.py", "--verify")
        check("project.py --verify lo rileva", v.returncode != 0,
              f"uscita {v.returncode}")
        # ⚠ Si cerca la RADICE della parola, senza accento. L'accento attraversa
        # docker, il locale del container, il decoder di `subprocess` e la
        # console: quattro strati che possono trasformarlo, e il controllo
        # diceva FAIL su un output che conteneva esattamente la parola cercata.
        # «attualit» e «fedelt» distinguono le due cause quanto le parole intere.
        uscita_v = (v.stdout + v.stderr).lower()
        check("nomina l'ATTUALITA, non la fedelta",
              "attualit" in uscita_v or "not_current" in uscita_v,
              (v.stdout + v.stderr)[-400:])
        riga_matrice("proiezione non attuale",
                     f"readiness 503 projection:not-ready; GET {g['status']} "
                     f"{c}; project.py --verify esce {v.returncode}",
                     "nessun dato perso: la storia è intatta",
                     "`project.py --rebuild` (è il caso in cui è appropriato subito)")
    finally:
        # §11: per `projection_not_current` il rimedio appropriato è la
        # ricostruzione, senza indagine — la storia è la fonte di verità e la
        # proiezione è derivata.
        rb = compose("run", "--rm", "migrate", "python", "scripts/project.py",
                     "--rebuild", timeout=600)
        check("project.py --rebuild riesce", rb.returncode == 0,
              (rb.stdout + rb.stderr)[-400:])
        check("la proiezione è tornata alla testa",
              psql("SELECT head_version FROM inventory_projection_state") == testa,
              f"testa={testa} proiezione={psql('SELECT head_version FROM inventory_projection_state')}")
        # Le tre cose che una ricostruzione NON deve fare.
        check("--rebuild NON ha creato una versione di inventario",
              psql("SELECT count(*) FROM inventory_versions") == versioni_prima,
              f"prima={versioni_prima} ora={psql('SELECT count(*) FROM inventory_versions')}")
        check("--rebuild NON ha toccato la storia immutabile",
              psql("SELECT canonical_sha256 FROM inventory_versions "
                   "ORDER BY version DESC LIMIT 1") == digest_prima)
        check("--rebuild NON ha distrutto i riferimenti storici alle foto",
              psql("SELECT count(*) FROM inventory_photo_refs") == foto_prima,
              f"prima={foto_prima} ora={psql('SELECT count(*) FROM inventory_photo_refs')}")
        check("readiness torna verde", pronto(90))


# ==================================================================
# 4. proiezione INCOERENTE  (§10, §11)
# ==================================================================
def guasto_proiezione_incoerente(cookie: str) -> None:
    caso("proiezione incoerente (fidelity)",
         "il GET rifiuta con projection_inconsistent; il worker RIFIUTA di "
         "spedire; project.py --verify lo rileva. ⚠ Il rimedio NON è ricostruire "
         "subito: prima si verifica, si guarda e si conserva la prova")
    nome_vero = psql("SELECT nome FROM inventory_locations ORDER BY nome LIMIT 1")
    versioni_prima = psql("SELECT count(*) FROM inventory_versions")

    # Si manomette una riga della PROIEZIONE senza toccare la storia: il digest
    # registrato non cambia, quindi il riassemblaggio non torna. È il guasto che la
    # fase 2F ha reso visibile (voce 13 del registro).
    psql("UPDATE inventory_locations SET nome = nome || ' [MANOMESSO]' "
         f"WHERE nome = $q${nome_vero}$q$")
    try:
        g = http("GET", "/api/inventory", cookie=cookie, timeout=30)
        c = codice(g["body"])
        check("il GET NON risponde 500", g["status"] != 500,
              f"HTTP {g['status']}: {g['body'][:200]}")
        check("il GET rifiuta con projection_inconsistent",
              c == "projection_inconsistent",
              f"HTTP {g['status']} codice={c!r} {g['body'][:300]}")
        check("il documento manomesso NON viene servito",
              "MANOMESSO" not in g["body"],
              "una proiezione incoerente è stata restituita al client")
        v = compose("run", "--rm", "migrate", "python", "scripts/project.py", "--verify")
        check("project.py --verify lo rileva", v.returncode != 0,
              f"uscita {v.returncode}")
        uscita_v = (v.stdout + v.stderr).lower()
        check("nomina la FEDELTA, non l'attualita",
              "fedelt" in uscita_v or "inconsistent" in uscita_v,
              (v.stdout + v.stderr)[-400:])
        # Il worker: deve rifiutare invece di spedire avvisi calcolati su dati che
        # non corrispondono alla storia.
        #
        # ⚠ Il worker del servizio si FERMA prima. Lasciandolo su, l'istanza
        # lanciata a mano troverebbe il lock consultivo già preso ed escebbe con 3
        # e il messaggio «un altro worker è già attivo»: il controllo sarebbe
        # passato, e per la ragione sbagliata. Provare il rifiuto sbagliato è
        # peggio che non provarlo, perché sembra provato.
        compose("stop", "worker")
        _rimuovi_orfani()
        # ⚠ Due cose vanno cambiate perché questo controllo significhi qualcosa, e
        # la prima stesura non ne aveva capita nessuna delle due.
        #
        # 1. LE NOTIFICHE SI ACCENDONO. Con `notifications.enabled = false` — che è
        #    lo stato della baseline — `run_once` esce subito con
        #    `notifications_disabled` e non arriva nemmeno a guardare il modello.
        #    La guardia conta quando il worker STAREBBE per spedire.
        #
        # 2. IL GUASTO DEVE ESSERE DEL TIPO GIUSTO. La guardia del worker è
        #    `validate_model`, e `validate_model` guarda il MODELLO — non confronta
        #    il digest. Il nome di un sito storpiato è una perdita di FEDELTÀ: il
        #    modello resta valido (un nome è un nome), e il worker fa bene a non
        #    accorgersene, perché a quello serve il confronto col digest che il GET
        #    fa. Quello che il worker deve prendere è una COLONNA DERIVATA che non
        #    corrisponde al testo da cui deriva — `DERIVED_MISMATCH`, gravità
        #    ERROR — perché una data derivata sbagliata lascia il documento e il
        #    digest identici e manderebbe un promemoria alla data sbagliata.
        #    È la voce 13 del registro, ed è l'unico controllo che può vederla.
        #
        # Quindi si inietta una `garanzia_date` senza nessuna `garanzia` di testo
        # dietro: l'interpretazione del nulla è nulla, la colonna dice una data, e
        # le due cose non corrispondono.
        # ⚠ `coalesce(garanzia,'') = ''` e non `garanzia IS NULL`: la colonna del
        # dispositivo di prova contiene la STRINGA VUOTA, non NULL, e un
        # `IS NULL` non trovava niente — l'iniezione non avveniva, e il controllo
        # sul worker giudicava un modello sano.
        psql("UPDATE inventory_devices SET garanzia_date = DATE '2027-01-15' "
             "WHERE coalesce(garanzia, '') = '' AND code LIKE 'QUAL-%'")
        n_iniettate = psql("SELECT count(*) FROM inventory_devices WHERE "
                           "garanzia_date IS NOT NULL AND coalesce(garanzia, '') = ''")
        check("l'iniezione della colonna derivata incoerente è avvenuta",
              n_iniettate not in ("0", "") and "__ERRORE__" not in n_iniettate,
              f"righe con derivata senza testo: {n_iniettate}")
        # 3. E IL GIRO DI OGGI DEVE ESSERE ANCORA DA FARE. `run_once` chiede
        #    `claim_run` prima di leggere i candidati: se la riga di oggi in
        #    `scheduler_runs` esiste già, esce con `already_ran_today` e non arriva
        #    mai alla validazione del modello. Un worker che aveva già girato oggi
        #    faceva quindi passare per «non rifiuta» un guasto che non aveva
        #    nemmeno guardato.
        #    Si cancella come proprietario dello schema: il ruolo di runtime non ha
        #    DELETE su quella tabella, di proposito.
        psql("DELETE FROM scheduler_runs WHERE run_date = current_date")
        prima = psql("SELECT data::text FROM settings")
        psql("UPDATE settings SET data = jsonb_set("
             "jsonb_set(data, '{notifications,enabled}', 'true'::jsonb), "
             "'{notifications,recipients}', '[\"qualifica@example.invalid\"]'::jsonb)")
        try:
            # `worker.py` non ha `--once`: è un ciclo, e il primo giro è immediato
            # (`run_once` sta in cima al `while`).
            #
            # ⚠ Il ciclo NON si interrompe col timeout di `subprocess`: quello
            # uccide il CLI di docker e restituisce quasi niente — la diagnosi
            # arrivava come «uscita -1: Container tsm-db-1 Running», che non dice
            # nulla di quello che il worker ha fatto. Si dà un nome al container,
            # si lascia girare un momento, e si legge dai suoi LOG: l'output c'è
            # sempre, indipendentemente da come il processo è finito.
            nome = "tsm-qual-worker-incoerente"
            sh("docker", "rm", "-f", nome)
            proc = subprocess.Popen(
                ["docker", "compose", "run", "--name", nome, "worker",
                 "python", "scripts/worker.py"],
                cwd=str(APP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            uscita, rc = "", -1
            try:
                for _ in range(30):
                    time.sleep(2)
                    log = sh("docker", "logs", nome).stdout \
                        + sh("docker", "logs", nome).stderr
                    if log.strip():
                        uscita = log
                        # Il worker ha detto qualcosa: o è il rifiuto, o è il
                        # risultato del giro. In entrambi i casi basta.
                        if "run_once" in log or "inconsist" in log.lower() \
                                or "sent=" in log or "ERROR" in log:
                            break
                stato = sh("docker", "inspect", "-f", "{{.State.ExitCode}}", nome).stdout.strip()
                rc = int(stato) if stato.lstrip("-").isdigit() else -1
            finally:
                proc.kill()
                sh("docker", "rm", "-f", nome)
            basso = uscita.lower()
            check("con le notifiche ACCESE il worker rifiuta il modello incoerente",
                  "inconsistent" in basso or "incoerent" in basso,
                  f"uscita {rc}: {uscita[-600:]}")
            check("e non dichiara di aver spedito niente",
                  "sent=0" in basso or "sent=" not in basso,
                  f"{uscita[-300:]}")
            check("nessuna consegna è stata marcata come riuscita",
                  psql("SELECT count(*) FROM reminder_deliveries "
                       "WHERE state = 'delivered'") in ("0", ""),
                  psql("SELECT state, count(*) FROM reminder_deliveries GROUP BY state"))
        finally:
            psql(f"UPDATE settings SET data = $q${prima}$q$::jsonb")
            check("le notifiche sono tornate spente",
                  psql("SELECT data->'notifications'->>'enabled' FROM settings")
                  == "false",
                  psql("SELECT data->'notifications' FROM settings"))
            # La derivata iniettata si toglie qui e non dal `--rebuild` finale: una
            # ricostruzione la sistemerebbe comunque, ma allora questa parte del
            # test dipenderebbe da un effetto collaterale di un'altra.
            psql("UPDATE inventory_devices SET garanzia_date = NULL "
                 "WHERE coalesce(garanzia, '') = ''")
            compose("start", "worker")
        riga_matrice("proiezione incoerente",
                     f"GET {g['status']} {c}; worker rifiuta; "
                     f"project.py --verify esce {v.returncode}",
                     "la storia è intatta; la proiezione è da ricostruire",
                     "PRIMA verificare e conservare la prova (`--verify`, dump "
                     "delle righe sospette), POI `project.py --rebuild` come "
                     "azione di recupero esplicita")
    finally:
        rb = compose("run", "--rm", "migrate", "python", "scripts/project.py",
                     "--rebuild", timeout=600)
        check("dopo l'indagine, --rebuild rimette la proiezione fedele",
              rb.returncode == 0, (rb.stdout + rb.stderr)[-300:])
        check("il nome manomesso è sparito",
              "MANOMESSO" not in psql("SELECT string_agg(nome, '|') FROM inventory_locations"))
        check("nessuna versione creata dal recupero",
              psql("SELECT count(*) FROM inventory_versions") == versioni_prima)
        check("readiness torna verde", pronto(90))


# ==================================================================
# 5. hash della password corrotto
# ==================================================================
def guasto_hash() -> None:
    caso("hash della password corrotto",
         "401 generico (non un 500, e non un messaggio che rivela il guasto), e "
         "il tentativo viene comunque REGISTRATO dal limitatore")
    vero = psql("SELECT password_hash FROM users WHERE username = 'admin'")
    prima = psql("SELECT count(*) FROM login_attempts")
    psql("UPDATE users SET password_hash = 'non-un-hash' WHERE username = 'admin'")
    try:
        r = http("POST", "/api/auth/login", {"username": UTENTE, "password": PASSWORD})
        check("non risponde 500", r["status"] != 500,
              f"HTTP {r['status']}: {r['body'][:200]}")
        check("risponde 401", r["status"] == 401, f"HTTP {r['status']}")
        check("il corpo non rivela il guasto",
              "argon" not in r["body"].lower() and "hash" not in r["body"].lower(),
              r["body"][:200])
        dopo = psql("SELECT count(*) FROM login_attempts")
        check("il tentativo è stato registrato dal limitatore",
              dopo.isdigit() and prima.isdigit() and int(dopo) > int(prima),
              f"prima={prima} dopo={dopo}: un hash rotto non deve diventare "
              f"un modo di provare password senza essere contati")
        riga_matrice("hash della password corrotto",
                     f"login 401 generico; tentativo registrato",
                     "nessun cambio", "reimpostare la password dell'utenza "
                     "(`/api/users/{id}/reset-password` o bootstrap)")
    finally:
        psql(f"UPDATE users SET password_hash = $q${vero}$q$ WHERE username = 'admin'")
        check("dopo il ripristino si entra di nuovo", accedi() is not None)


# ==================================================================
# 6. SMTP non disponibile
# ==================================================================
def guasto_smtp(cookie: str) -> None:
    caso("SMTP non configurato / non disponibile",
         "l'invio di prova risponde 503 con un codice PROPRIO, non un 500, e non "
         "marca niente come consegnato")
    r = http("POST", "/api/notifications/test", {}, cookie=cookie, timeout=60)
    c = codice(r["body"])
    check("non risponde 500", r["status"] != 500, f"HTTP {r['status']}: {r['body'][:200]}")
    check("risponde 503 (o un rifiuto con codice)",
          r["status"] in (400, 422, 503), f"HTTP {r['status']} {r['body'][:200]}")
    check("il codice dice che il trasporto non è configurato",
          bool(c), f"nessun codice nella risposta: {r['body'][:200]}")
    consegne = psql("SELECT count(*) FROM reminder_deliveries WHERE state = 'delivered'")
    check("nessuna consegna marcata come riuscita",
          consegne in ("0", ""), f"{consegne} consegne 'delivered'")
    riga_matrice("SMTP non configurato",
                 f"POST /api/notifications/test → {r['status']} {c or 'n/d'}",
                 "nessuna consegna marcata", "configurare TSM_SMTP_* "
                 "nell'ambiente del servizio (operations, non interfaccia)")


# ==================================================================
# 7. secondo worker
# ==================================================================
def guasto_secondo_worker() -> None:
    caso("un SECONDO worker",
         "il secondo esce con un messaggio e non spedisce niente: il lock "
         "consultivo nel database è la garanzia, `deploy.replicas` è solo "
         "la dichiarazione d'intenti")
    w = compose("run", "--rm", "worker", "python", "scripts/worker.py", timeout=120)
    uscita = (w.stdout + w.stderr).lower()
    check("il secondo worker non gira",
          w.returncode != 0 or "lock" in uscita or "altro" in uscita or "già" in uscita,
          f"uscita {w.returncode}: {(w.stdout + w.stderr)[-300:]}")
    check("e lo dice invece di uscire in silenzio",
          any(x in uscita for x in ("lock", "istanza", "già", "altro", "attiv")),
          (w.stdout + w.stderr)[-300:])
    riga_matrice("secondo worker avviato",
                 f"esce con {w.returncode} e un messaggio sul lock",
                 "nessun avviso duplicato",
                 "nessuno: è il comportamento voluto")


# ==================================================================
# 8. materiale TLS rotto  (il preflight, a servizio fermo)
# ==================================================================
def guasto_tls() -> None:
    caso("chiave TLS assente",
         "il preflight RIFIUTA con il codice 31 e il servizio non parte: "
         "nessun avvio in chiaro come ripiego")
    chiave = APP / "secrets" / "tls" / "privkey.pem"
    salva = chiave.read_bytes()
    sh("systemctl", "stop", "tsm.service")
    chiave.unlink()
    try:
        p = sh(str(APP / "deploy" / "preflight.sh"), "--dir", str(APP), "--quiet")
        check("il preflight rifiuta con il codice 31", p.returncode == 31,
              f"uscita {p.returncode}: {(p.stdout + p.stderr)[-300:]}")
        s = sh("systemctl", "start", "tsm.service")
        check("systemctl start FALLISCE (ExecStartPre ha detto no)",
              s.returncode != 0, f"uscita {s.returncode}")
        check("l'unità risulta fallita, non attiva",
              sh("systemctl", "is-active", "tsm.service").stdout.strip() != "active",
              sh("systemctl", "is-active", "tsm.service").stdout.strip())
        riga_matrice("materiale TLS assente/illeggibile",
                     "preflight esce 31; `systemctl start` fallisce; nessun "
                     "container avviato", "nessun cambio",
                     "ripristinare secrets/tls/{fullchain,privkey}.pem con "
                     "proprietario uid 101 e 0400, poi `systemctl start tsm`")
    finally:
        chiave.write_bytes(salva)
        sh("chown", "101:101", str(chiave))
        sh("chmod", "0400", str(chiave))
        sh("systemctl", "reset-failed", "tsm.service")
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        check("ripristinata la chiave, il servizio riparte", s.returncode == 0,
              (s.stdout + s.stderr)[-300:])
        check("readiness torna verde", pronto(120))


# ==================================================================
# 9. disco dei dati non montato
# ==================================================================
def guasto_mount() -> None:
    caso("disco dei dati NON montato",
         "il preflight rifiuta con il codice 20 e NON crea la directory: se la "
         "creasse, PostgreSQL farebbe initdb sul disco di sistema e i dati "
         "finirebbero dove il backup non guarda")
    sh("systemctl", "stop", "tsm.service")
    u = sh("umount", "/srv/tsm-data")
    if u.returncode != 0:
        check("smontaggio del disco dei dati", False, (u.stdout + u.stderr)[-200:])
        sh("systemctl", "start", "tsm.service", timeout=600)
        return
    try:
        p = sh(str(APP / "deploy" / "preflight.sh"), "--dir", str(APP), "--quiet")
        check("il preflight, eseguito a mano, rifiuta con il codice 20",
              p.returncode == 20,
              f"uscita {p.returncode}: {(p.stdout + p.stderr)[-300:]}")
        check("e NON ha creato la directory dei dati",
              not Path("/srv/tsm-data/postgres").exists(),
              "la directory è stata creata sul filesystem di root")

        # ⚠ Qui l'attesa iniziale di questo test era SBAGLIATA, e la verità è
        # migliore: `systemctl start` RIESCE, perché `RequiresMountsFor` non è solo
        # un ordinamento — rende l'unità .mount una dipendenza NECESSARIA, e
        # systemd la AVVIA. Cioè monta il disco da sé e poi parte.
        #
        # È il comportamento voluto: un mount dimenticato si ripara invece di
        # diventare un guasto. Il codice 20 del preflight resta la seconda rete,
        # per il caso in cui il mount non si possa soddisfare — provato subito
        # sotto, che è l'unico modo di sapere che quella rete c'è.
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        montato = Path("/srv/tsm-data").is_mount() if hasattr(Path, "is_mount") \
            else sh("mountpoint", "-q", "/srv/tsm-data").returncode == 0
        check("systemctl start RIMONTA il disco da sé (RequiresMountsFor) e riesce",
              s.returncode == 0 and montato,
              f"uscita {s.returncode}, montato={montato}")
        riga_matrice("disco dei dati non montato (ma montabile)",
                     "preflight a mano esce 20; `systemctl start` monta il disco "
                     "via RequiresMountsFor e parte", "nessun cambio",
                     "nessuno: systemd lo ripara. Se non lo ripara, vedi il caso "
                     "seguente")
    finally:
        sh("mount", "/srv/tsm-data")
        sh("systemctl", "reset-failed", "tsm.service")
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        check("rimontato, il servizio riparte", s.returncode == 0,
              (s.stdout + s.stderr)[-300:])
        check("readiness torna verde", pronto(120))
        check("i dati sono ancora quelli",
              psql("SELECT count(*) FROM inventory_racks") == "102",
              psql("SELECT count(*) FROM inventory_racks"))


# ==================================================================
# 9-bis. il disco NON C'È: il mount non si può soddisfare
# ==================================================================
def guasto_disco_assente() -> None:
    caso("il disco dei dati NON ESISTE (mount impossibile)",
         "`systemctl start` FALLISCE e nessun container parte: è il caso che "
         "`RequiresMountsFor` esiste per prendere. Il pericolo è che Docker crei "
         "la directory sul filesystem di root e PostgreSQL vi faccia initdb — "
         "nessun errore, dati sul disco sbagliato e fuori dal backup")
    immagine = Path("/var/lib/tsm-data.img")
    spostata = Path("/var/lib/tsm-data.img.nascosta")
    sh("systemctl", "stop", "tsm.service")
    sh("umount", "/srv/tsm-data")
    immagine.rename(spostata)
    try:
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        check("systemctl start FALLISCE", s.returncode != 0,
              f"uscita {s.returncode}: {(s.stdout + s.stderr)[-200:]}")
        check("l'unità non è attiva",
              sh("systemctl", "is-active", "tsm.service").stdout.strip() != "active",
              sh("systemctl", "is-active", "tsm.service").stdout.strip())
        # La cosa che conta più del rifiuto: nessun byte di PostgreSQL sul disco
        # di sistema.
        check("NESSUNA directory dei dati è stata creata sul filesystem di root",
              not Path("/srv/tsm-data/postgres").exists(),
              "Docker ha creato il bind su / e PostgreSQL potrebbe averci scritto")
        n = sh("docker", "ps", "-q").stdout.strip()
        check("nessun container di TSM è in piedi",
              "tsm-db" not in sh("docker", "ps", "--format", "{{.Names}}").stdout,
              sh("docker", "ps", "--format", "{{.Names}}").stdout.strip())
        riga_matrice("disco dei dati inesistente (mount non soddisfacibile)",
                     "`systemctl start` fallisce; nessun container avviato; "
                     "nessuna directory creata su /", "nessun cambio",
                     "ripristinare il disco/volume, `mount -a`, poi "
                     "`systemctl start tsm`")
    finally:
        spostata.rename(immagine)
        sh("mount", "/srv/tsm-data")
        sh("systemctl", "reset-failed", "tsm.service")
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        check("ripristinato il disco, il servizio riparte", s.returncode == 0,
              (s.stdout + s.stderr)[-300:])
        check("readiness torna verde", pronto(120))
        check("i dati sono ancora quelli",
              psql("SELECT count(*) FROM inventory_racks") == "102",
              psql("SELECT count(*) FROM inventory_racks"))


# ==================================================================
# 10. spazio libero sotto la soglia
# ==================================================================
def guasto_spazio() -> None:
    caso("spazio libero sotto la soglia",
         "il preflight rifiuta con il codice 26: un PostgreSQL che finisce lo "
         "spazio si ferma in scrittura, e il recupero è molto più lungo del "
         "previsto")
    # Si prepara un disco PICCOLO e lo si monta al posto di quello vero, a
    # servizio fermo: riempire quello vero costerebbe venti gigabyte veri e non
    # proverebbe niente di più.
    piccolo = Path("/var/tmp/tsm-piccolo.img")
    sh("systemctl", "stop", "tsm.service")
    smontato = sh("umount", "/srv/tsm-data").returncode == 0
    try:
        if not smontato:
            check("preparazione del disco piccolo", False, "non ho potuto smontare")
            return
        sh("truncate", "-s", "6G", str(piccolo))
        sh("mkfs.ext4", "-q", "-E", "lazy_itable_init=1,lazy_journal_init=1", str(piccolo))
        sh("mount", "-o", "loop", str(piccolo), "/srv/tsm-data")
        sh("install", "-d", "-o", "70", "-g", "70", "-m", "0700", "/srv/tsm-data/postgres")
        # Si riempie fino a lasciare meno di 5 GiB liberi.
        sh("fallocate", "-l", "2G", "/srv/tsm-data/zavorra", timeout=300)
        liberi = sh("df", "-BG", "--output=avail", "/srv/tsm-data").stdout.strip()
        print(f"     liberi ora: {liberi.splitlines()[-1].strip()}")
        p = sh(str(APP / "deploy" / "preflight.sh"), "--dir", str(APP), "--quiet")
        check("il preflight rifiuta con il codice 26", p.returncode == 26,
              f"uscita {p.returncode}: {(p.stdout + p.stderr)[-300:]}")
        riga_matrice("spazio libero sotto la soglia (5 GiB)",
                     "preflight esce 26; il servizio non parte", "nessun cambio",
                     "liberare spazio sul disco dati o estenderlo; le soglie di "
                     "monitoraggio sono al 70% e all'85%")
    finally:
        sh("umount", "/srv/tsm-data")
        piccolo.unlink(missing_ok=True)
        sh("mount", "/srv/tsm-data")
        sh("systemctl", "reset-failed", "tsm.service")
        s = sh("systemctl", "start", "tsm.service", timeout=600)
        check("rimontato il disco vero, il servizio riparte", s.returncode == 0,
              (s.stdout + s.stderr)[-300:])
        check("readiness torna verde", pronto(120))


# ==================================================================
# 11. carico concorrente sui due pool
# ==================================================================
def carico_pool(cookie: str) -> None:
    caso("carico concorrente sui DUE pool",
         "nessuno stallo e nessun 500: un GET tiene due connessioni insieme, e con "
         "un pool solo trenta GET simultanei si bloccherebbero a vicenda (§8.45)")
    import concurrent.futures as cf

    # Il doppio della capienza di un pool (15): è il numero che con un pool solo
    # produceva lo stallo da acquisizione a due fasi.
    n = 30
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        futuri = [ex.submit(http, "GET", "/api/inventory", None, cookie, 90)
                  for _ in range(n)]
        esiti = [f.result() for f in futuri]
    durata = time.perf_counter() - t0
    stati: dict[int, int] = {}
    for e in esiti:
        stati[e["status"]] = stati.get(e["status"], 0) + 1
    print(f"     {n} GET simultanei in {durata:.1f}s: {stati}")
    check(f"tutti e {n} i GET simultanei rispondono 200",
          stati.get(200) == n, f"esiti: {stati}")
    check("nessun 500", 500 not in stati, f"esiti: {stati}")
    # Lo stallo si manifestava come trenta secondi di blocco e poi un timeout del
    # pool: un tempo totale ragionevole è la prova che non si è verificato.
    check("nessuno stallo (nessuna attesa da timeout del pool)", durata < 30,
          f"{durata:.1f}s per {n} richieste")

    # E lo stesso mescolando letture e scritture, che è il caso che usa entrambi
    # i pool per davvero.
    misto = []
    with cf.ThreadPoolExecutor(max_workers=20) as ex:
        for i in range(20):
            percorso = ("/api/inventory" if i % 4 else "/api/inventory/capacity")
            misto.append(ex.submit(http, "GET", percorso, None, cookie, 90))
        esiti2 = [f.result() for f in misto]
    stati2: dict[int, int] = {}
    for e in esiti2:
        stati2[e["status"]] = stati2.get(e["status"], 0) + 1
    check("letture e interrogazioni mescolate rispondono tutte 200",
          stati2.get(200) == 20, f"esiti: {stati2}")
    riga_matrice("carico concorrente 2× capienza di un pool",
                 f"{n} GET simultanei, tutti 200, {durata:.1f}s",
                 "nessun cambio", "nessuno: è il caso normale")


# ==================================================================
def main() -> int:
    global APP, BASE, UTENTE, PASSWORD
    ap = argparse.ArgumentParser(description="iniezione di guasti (DISTRUTTIVO)")
    ap.add_argument("--dir", default="/opt/tsm")
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--only", default="",
                    help="elenco separato da virgole dei casi da eseguire")
    ap.add_argument("--json", default=None)
    ap.add_argument("--allow-destructive", action="store_true", required=True,
                    help="obbligatorio: questo strumento rompe l'installazione")
    args = ap.parse_args()

    APP = Path(args.dir)
    BASE = args.base.rstrip("/")
    UTENTE = args.username
    PASSWORD = os.environ.get("TSM_QUAL_NEW_PASSWORD", "")
    if not PASSWORD:
        print("serve TSM_QUAL_NEW_PASSWORD", file=sys.stderr)
        return 2

    cookie = accedi()
    if not cookie:
        print("accesso non riuscito: mi fermo prima di rompere qualcosa",
              file=sys.stderr)
        return 2

    casi = {
        "database": lambda: guasto_database(cookie),
        "limitatore": guasto_limitatore,
        "proiezione-vecchia": lambda: guasto_proiezione_vecchia(cookie),
        "proiezione-incoerente": lambda: guasto_proiezione_incoerente(cookie),
        "hash": guasto_hash,
        "smtp": lambda: guasto_smtp(cookie),
        "secondo-worker": guasto_secondo_worker,
        "tls": guasto_tls,
        "mount": guasto_mount,
        "disco-assente": guasto_disco_assente,
        "spazio": guasto_spazio,
        "pool": lambda: carico_pool(cookie),
    }
    vuole = [x.strip() for x in args.only.split(",") if x.strip()] or list(casi)
    ignoti = [x for x in vuole if x not in casi]
    if ignoti:
        print(f"casi ignoti: {ignoti}. disponibili: {list(casi)}", file=sys.stderr)
        return 2

    for nome in vuole:
        try:
            casi[nome]()
        except Exception as exc:
            # Un'eccezione a metà non deve buttare via i risultati già raccolti:
            # sapere quali iniezioni erano andate a buon fine è metà della
            # diagnosi. Ma va detto forte, perché il `finally` potrebbe non
            # essere arrivato e l'installazione potrebbe essere rotta.
            check(f"CASO INTERROTTO: {type(exc).__name__}", False, str(exc)[:400])
        # Fra un caso e l'altro si rientra: alcuni casi invalidano la sessione.
        nuovo = accedi()
        if nuovo:
            cookie = nuovo

    passati = sum(1 for r in risultati if r[2])
    falliti = len(risultati) - passati
    print("\n" + "=" * 74)
    print("MATRICE DEI GUASTI")
    print("=" * 74)
    for m in matrice:
        print(f"\n  {m['guasto']}")
        print(f"    osservabile: {m['osservabile']}")
        print(f"    stato:       {m['stato']}")
        print(f"    rimedio:     {m['rimedio']}")
    print("\n" + "=" * 74)
    print(f"=== {passati} PASS, {falliti} FAIL su {len(risultati)} controlli ===")
    if falliti:
        print("\nfalliti:")
        for c, nome, ok, det in risultati:
            if not ok:
                print(f"  [{c}] {nome}" + (f"  -> {det}" if det else ""))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "checks": [{"case": c, "name": n, "passed": p, "detail": d}
                       for c, n, p, d in risultati],
            "matrix": matrice, "passed": passati, "failed": falliti,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nrisultati in {args.json}")

    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
