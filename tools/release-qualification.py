#!/usr/bin/env python3
"""Qualificazione del rilascio: i cancelli che solo una installazione VERA può aprire.

Gira DENTRO l'host di qualificazione (`tsm-prd-01`), non sulla postazione di
sviluppo, e parla con l'installazione attraverso le stesse porte di un operatore:
443 via nginx, e `docker compose exec` per ciò che sta nel database.

Che cosa fa qui e non in una suite
----------------------------------
Le suite del backend provano la semantica; questo strumento prova l'INSTALLAZIONE.
Sono domande diverse, e la fase 2H ne ha già mostrato il costo due volte — un
modulo dimenticato nell'allowlist di nginx e una sala con `vani: []` avevano
superato ogni suite e rotto l'applicazione nel browser.

Qui si aggiungono le domande che nemmeno la camera bianca poneva:

  * la composizione RESA è quella di produzione? (§5) — e non lo si chiede a un
    file, lo si chiede alla rete;
  * i due pool di connessioni stanno dentro `max_connections`? (§14) — con i
    numeri EFFETTIVI, non con quelli scritti nel commento;
  * la concorrenza ottimistica regge attraverso nginx e TLS? (§12);
  * quanto consuma, da fermo e sotto le operazioni reali? (§27);
  * dopo tutta la qualificazione, ci sono segreti nei log o nel database? (§30) —
    con canarini piantati, perché una ricerca che non può trovare niente non
    dimostra niente.

Che cosa NON fa: non rompe niente. Le iniezioni di guasto e la corruzione
deliberata della proiezione stanno in `tools/failure-injection-test.py`, che si
esegue dopo e dichiara di essere distruttivo. La separazione è nel nome dei file
di proposito: chi legge deve poter sapere, senza aprirli, quale dei due può
lasciare l'installazione da rimettere in piedi.

⚠ Solo libreria standard: sull'host di qualificazione non c'è pip, e non deve
essercene bisogno.

Uso:
    python3 tools/release-qualification.py --dir /opt/tsm
    python3 tools/release-qualification.py --dir /opt/tsm --section 14
    python3 tools/release-qualification.py --dir /opt/tsm --json /root/ris.json

La password dell'amministratore arriva da TSM_QUAL_PASSWORD, non da un argomento:
una riga di comando finisce nella cronologia della shell e in `ps`.
"""
from __future__ import annotations

import argparse
import http.client
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
HOST = "localhost"
#: Impostate da `main`: servono alla sonda di §30, che deve aprire un client
#: proprio (un tentativo fallito non deve toccare la sessione buona).
BASE_GLOBALE = ""
CA_GLOBALE = None

#: Canarini piantati prima della qualificazione e cercati dopo (§30). Non sono
#: segreti veri: sono stringhe che NON DEVONO comparire dove i segreti non devono
#: comparire, e servono a rendere la ricerca falsificabile. Una ricerca di segreti
#: che non trova niente perché non c'era niente da trovare non prova nulla — e
#: senza un positivo atteso non si distingue «pulito» da «grep sbagliato».
CANARINI = {
    # Piantato nel campo `note` di un dispositivo: DEVE essere ritrovabile nel
    # database (è dato dell'inventario) e NON nei log.
    "inventario": "CANARINO-INVENTARIO-9f3a1c",
    # Passato come password sbagliata a un login: NON deve comparire da nessuna
    # parte, né nei log dell'API né nell'audit del tentativo fallito.
    "password": "CANARINO-PASSWORD-4b8e77",
}

#: UID dei due dispositivi di prova di §12. FISSI, non generati: il replay deve
#: rimandare lo STESSO candidato, e un UID nuovo a ogni chiamata farebbe del secondo
#: invio un documento diverso — cioè proverebbe l'opposto di quello che serve.
UID_A = "2a1f0c44-9e51-4d7b-8f30-aaaa0000000a"
UID_B = "2a1f0c44-9e51-4d7b-8f30-bbbb0000000b"

risultati: list[tuple[int, str, bool, str]] = []
_sezione_corrente = 0


def sezione(n: int, titolo: str) -> None:
    global _sezione_corrente
    _sezione_corrente = n
    print(f"\n== §{n}. {titolo} ==")


def check(nome: str, passato: bool, dettaglio: str = "") -> None:
    risultati.append((_sezione_corrente, nome, bool(passato), dettaglio))
    esito = "PASS" if passato else "FAIL"
    print(f"  [{esito}] {nome}")
    if dettaglio and not passato:
        print(f"         -> {dettaglio}")


# ==================================================================
# infrastruttura: compose, psql, HTTPS
# ==================================================================
def compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """`docker compose` nella directory del progetto, SENZA override.

    ⚠ Nessun `-f`: il punto della 2J è che la composizione di produzione sia
    quella che compose sceglie da sé — compose.yaml più, se esistesse,
    compose.override.yaml. Passare i file a mano qui renderebbe il test cieco
    proprio all'override aggiunto per sbaglio, che è il caso da prendere.
    """
    return subprocess.run(["docker", "compose", *args], cwd=str(APP),
                          capture_output=True, text=True, timeout=timeout)


def psql(sql: str, *, user: str = "tsm", db: str = "tsm") -> str:
    """Una riga di SQL come proprietario dello schema. Restituisce stdout grezzo."""
    p = compose("exec", "-T", "db", "psql", "-U", user, "-d", db,
                "-At", "-c", sql)
    if p.returncode != 0:
        return f"__ERRORE__ {p.stderr.strip()[:300]}"
    return p.stdout.strip()


def psql_int(sql: str) -> int | None:
    out = psql(sql)
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return None


class Client:
    """Client HTTPS minimo con cookie, che VERIFICA il certificato.

    Il certificato si verifica contro `secrets/tls/fullchain.pem` invece di
    disattivare il controllo: un test che parla in HTTPS con la verifica spenta
    non prova che nginx serva il certificato giusto, prova solo che risponda.
    """

    def __init__(self, base: str, ca: Path | None):
        self.base = base.rstrip("/")
        self.ca_path = ca
        self.cookies: dict[str, str] = {}
        if ca and ca.is_file():
            self.ctx = ssl.create_default_context(cafile=str(ca))
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, metodo: str, percorso: str, corpo: dict | None = None,
                intestazioni: dict[str, str] | None = None) -> dict:
        url = f"{self.base}{percorso}"
        dati = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(url, data=dati, method=metodo)
        req.add_header("Accept", "application/json")
        if dati is not None:
            req.add_header("Content-Type", "application/json")
        # L'origine si dichiara sempre: senza, la validazione di origine salta il
        # controllo e il test non eserciterebbe il percorso di produzione.
        req.add_header("Origin", self.base)
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        for k, v in (intestazioni or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
                grezzo = r.read()
                self._raccogli_cookie(r.headers.get_all("Set-Cookie") or [])
                return {"status": r.status, "headers": dict(r.headers),
                        "raw": grezzo, "json": self._json(grezzo)}
        except urllib.error.HTTPError as e:
            grezzo = e.read()
            self._raccogli_cookie(e.headers.get_all("Set-Cookie") or [])
            return {"status": e.code, "headers": dict(e.headers),
                    "raw": grezzo, "json": self._json(grezzo)}
        except Exception as exc:
            return {"status": 0, "headers": {}, "raw": b"", "json": None,
                    "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _json(grezzo: bytes):
        try:
            return json.loads(grezzo.decode("utf-8"))
        except Exception:
            return None

    def _raccogli_cookie(self, intestazioni: list[str]) -> None:
        for h in intestazioni:
            pezzo = h.split(";")[0]
            if "=" in pezzo:
                k, v = pezzo.split("=", 1)
                if v in ('""', ""):
                    self.cookies.pop(k.strip(), None)
                else:
                    self.cookies[k.strip()] = v

    def login(self, utente: str, password: str) -> dict:
        return self.request("POST", "/api/auth/login",
                            {"username": utente, "password": password})


# ==================================================================
# §5. produzione, non sviluppo — e lo si chiede alla RETE
# ==================================================================
def sez_5_separazione(cli: Client) -> None:
    sezione(5, "la composizione RESA è di produzione, provata dall'esterno")

    reso = compose("config").stdout
    check("`docker compose config` è leggibile", bool(reso.strip()),
          "senza questo tutti i controlli su di essa sarebbero vacui")

    # Gli indizi di sviluppo, cercati nella configurazione RESA e non nei file
    # sorgente: un override aggiunto in qualunque modo cambia il risultato.
    #
    # ⚠ Si ESTRAGGONO i valori e si confrontano, invece di cercare un pattern
    # negativo. La prima stesura usava `TSM_ENV:\s*["']?(?!production)` e diceva
    # FAIL su `TSM_ENV: production`: con `\s*` che può tornare indietro, il motore
    # prova anche zero spazi, e allora il lookahead guarda « production» — che
    # davvero non è «production», e la negazione riesce. Un controllo di sicurezza
    # scritto in negativo su un'espressione che può backtrackare dice sì e no per
    # ragioni che non hanno a che vedere con quello che sta guardando.
    valori = dict(re.findall(r"^\s+(TSM_ENV|TSM_COOKIE_SECURE):\s*[\"']?([^\"'\s]*)",
                             reso, re.M))
    check("TSM_ENV nella configurazione resa è 'production'",
          valori.get("TSM_ENV") == "production",
          f"TSM_ENV={valori.get('TSM_ENV')!r}")
    check("il cookie di sessione è Secure",
          str(valori.get("TSM_COOKIE_SECURE", "true")).lower() not in ("false", "0", "no"),
          f"TSM_COOKIE_SECURE={valori.get('TSM_COOKIE_SECURE')!r}")

    # Nessuna porta pubblicata per db e api. Si legge dalla forma lunga che
    # `compose config` produce, servizio per servizio.
    for servizio in ("db", "api", "worker"):
        blocco = _blocco_servizio(reso, servizio)
        check(f"il servizio '{servizio}' non pubblica porte sull'host",
              "published:" not in blocco,
              "una richiesta dal bridge di Docker cade dentro TSM_TRUSTED_PROXIES")

    # E adesso la parte che l'ispezione statica non può dare: si guarda quali porte
    # il PROGETTO pubblica davvero, e si prova a raggiungere l'API.
    #
    # ⚠ Le porte si chiedono a `compose ps` e non a un `ss` sull'host. Sulla
    # macchina di qualificazione la 5432 risulta in ascolto — è un container di un
    # ALTRO progetto, che con TSM non ha niente a che fare — e un controllo che
    # guarda solo «qualcuno ascolta sulla 5432» avrebbe dichiarato esposto il
    # database di TSM sulla base di un servizio altrui. Un falso positivo su un
    # controllo di esposizione è costoso: insegna a ignorarlo.
    pubblicate = compose("ps", "--format", "{{.Service}} {{.Publishers}}").stdout
    print(f"     porte del progetto: {pubblicate.strip().replace(chr(10), ' | ')}")
    for servizio in ("db", "api", "worker"):
        riga = next((r for r in pubblicate.splitlines()
                     if r.split(" ", 1)[0] == servizio), "")
        # Un publisher senza porta sull'host compare con PublishedPort 0.
        esposto = bool(re.search(r"PublishedPort:(?!0\b)\d+", riga))
        check(f"il servizio '{servizio}' non ha porte pubblicate a runtime",
              not esposto, riga[:200])

    raggiungibile = _porta_aperta("127.0.0.1", 8000)
    check("la porta 8000 (API) NON risponde su 127.0.0.1", not raggiungibile,
          "l'API è raggiungibile dall'host: le sue X-Forwarded-* sarebbero credute")

    # CORS permissivo: si chiede al server, con una preflight vera.
    r = cli.request("OPTIONS", "/api/inventory", intestazioni={
        "Origin": "https://attaccante.example",
        "Access-Control-Request-Method": "PUT"})
    acao = {k.lower(): v for k, v in r["headers"].items()}.get("access-control-allow-origin")
    check("nessuna intestazione CORS permissiva",
          acao in (None, "") or acao == cli.base,
          f"Access-Control-Allow-Origin: {acao!r}")

    # La build di sviluppo di React non deve essere servita.
    r = cli.request("GET", "/vendor/react.production.min.js")
    check("è servita la build di PRODUZIONE di React", r["status"] == 200,
          f"HTTP {r['status']}")
    r = cli.request("GET", "/vendor/react.development.js")
    check("la build di sviluppo di React non esiste", r["status"] == 404,
          f"HTTP {r['status']}")
    # I file esclusi dall'allowlist (§6): non devono nemmeno essere nell'immagine.
    # ⚠ Il percorso si CODIFICA: quello con gli spazi dava «HTTP 0», cioè un errore
    # del client, e un test che non riesce a fare la richiesta stava dichiarando
    # protetto un file che non aveva provato a chiedere.
    from urllib.parse import quote
    for escluso in ("/inventario.js", "/Sale Server Pomezia (standalone).html",
                    "/vendor/SHA256SUMS", "/README.md"):
        r = cli.request("GET", quote(escluso))
        check(f"{escluso} non è servito", r["status"] in (403, 404),
              f"HTTP {r['status']} {r.get('error', '')}")


def _blocco_servizio(reso: str, nome: str) -> str:
    """Le righe indentate sotto `  <nome>:` dentro `services:`."""
    fuori: list[str] = []
    in_services = in_svc = False
    for riga in reso.splitlines():
        if riga.rstrip() == "services:":
            in_services = True
            continue
        if riga and not riga[0].isspace():
            in_services = in_svc = False
            continue
        if in_services and re.match(r"^  \S", riga):
            in_svc = riga.strip().rstrip(":") == nome
            continue
        if in_svc:
            fuori.append(riga)
    return "\n".join(fuori)


def _porta_aperta(host: str, porta: int, timeout: float = 2.0) -> bool:
    import socket
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True
    except OSError:
        return False


# ==================================================================
# §16. TLS, intestazioni, cookie — chiesti al server, non al file
# ==================================================================
def sez_16_tls(cli: Client, utente: str, password: str) -> None:
    sezione(16, "TLS, intestazioni di sicurezza, cookie di sessione")

    # ---- il reindirizzamento da HTTP. Si usa una connessione in chiaro vera:
    # nginx ha un listener separato sulla 8080, e provarlo in HTTPS non lo toccherebbe.
    import http.client as hc
    try:
        c = hc.HTTPConnection("localhost", 80, timeout=15)
        c.request("GET", "/", headers={"Host": "tsm-prd-01"})
        r = c.getresponse()
        loc = r.getheader("Location") or ""
        stato = r.status
        c.close()
    except Exception as exc:
        stato, loc = 0, f"{type(exc).__name__}: {exc}"
    check("HTTP reindirizza a HTTPS con 301", stato == 301, f"HTTP {stato}")
    check("e il reindirizzamento conserva l'host e va su https",
          loc.startswith("https://") and "tsm-prd-01" in loc, f"Location: {loc}")

    # ---- HSTS e le altre intestazioni, LETTE DALLA RISPOSTA.
    #
    # ⚠ Si guardano sulla risposta e non in nginx.conf. `add_header` in nginx ha una
    # regola che sorprende: una direttiva in un blocco più interno SOSTITUISCE tutte
    # quelle del blocco esterno, e `always` cambia solo il comportamento sugli
    # errori. Un `add_header` aggiunto dentro una `location` può quindi far
    # scomparire HSTS da quel percorso, mentre il file continua a contenerlo.
    r = cli.request("GET", "/")
    h = {k.lower(): v for k, v in r["headers"].items()}
    attese = {
        "strict-transport-security": "max-age=",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    for nome, atteso in attese.items():
        val = h.get(nome, "")
        check(f"la risposta HTTPS porta {nome}: {atteso}", atteso in val,
              f"{nome}: {val!r}")
    # `max-age` di un anno: un HSTS di dieci minuti non protegge la prima visita
    # del giorno dopo.
    hsts = h.get("strict-transport-security", "")
    m = re.search(r"max-age=(\d+)", hsts)
    check("HSTS dura almeno un anno",
          bool(m) and int(m.group(1)) >= 31536000, f"{hsts!r}")

    # E su OGNI TIPO di risposta, non solo sulla radice.
    #
    # ⚠ Qui la 2J ha trovato un difetto vero: `/vendor/*` e `/fonts/*` non
    # portavano nessuna delle quattro intestazioni, perché quei due `location`
    # avevano un `add_header Cache-Control` e in nginx un `add_header` in un blocco
    # interno SCARTA tutti quelli ereditati. Mancava `nosniff` esattamente sul
    # JavaScript vendorizzato. Il file dichiarava le intestazioni: solo chiederle
    # al server poteva dirlo.
    for percorso in ("/api/health", "/vendor/react.production.min.js",
                     "/fonts/PublicSans-400.woff2", "/domain.js", "/index.html"):
        r2 = cli.request("GET", percorso)
        h2 = {k.lower(): v for k, v in r2["headers"].items()}
        mancanti = [n for n, atteso in attese.items() if atteso not in h2.get(n, "")]
        check(f"tutte e quattro le intestazioni di sicurezza su {percorso}",
              not mancanti, f"HTTP {r2['status']}, mancano: {mancanti}")

    # ---- il cookie di sessione: i tre attributi, dalla risposta del login.
    #
    # ⚠ La chiave si cerca in minuscolo. Uvicorn emette i nomi delle intestazioni
    # in minuscolo (è la forma ASGI), quindi `get("Set-Cookie")` tornava vuoto e i
    # tre controlli sul cookie fallivano dicendo «nessun cookie» su un login
    # riuscito che il cookie lo impostava.
    sonda = Client(cli.base, cli.ca_path)
    rl = sonda.request("POST", "/api/auth/login",
                       {"username": utente, "password": password})
    hl = {k.lower(): v for k, v in rl["headers"].items()}
    grezzi = hl.get("set-cookie", "")
    check("il login riesce", rl["status"] == 200, f"HTTP {rl['status']}")
    check("il login imposta un cookie di sessione",
          "tsm_session=" in grezzi, f"Set-Cookie: {grezzi[:120]!r}")
    for attributo in ("Secure", "HttpOnly"):
        check(f"il cookie di sessione è {attributo}",
              attributo.lower() in grezzi.lower(), f"Set-Cookie: {grezzi[:200]!r}")
    check("il cookie di sessione è SameSite=Strict",
          "samesite=strict" in grezzi.lower(), f"Set-Cookie: {grezzi[:200]!r}")

    # ---- TLS: versioni. Una 1.0/1.1 accettata sarebbe un difetto silenzioso.
    for versione, etichetta in ((ssl.TLSVersion.TLSv1_2, "TLS 1.2"),
                                (ssl.TLSVersion.TLSv1_3, "TLS 1.3")):
        ok = _tls_parla(cli.ca_path, minimo=versione, massimo=versione)
        check(f"{etichetta} è accettato", ok, "handshake fallito")
    # 1.1 e sotto: OpenSSL 3 spesso non li offre nemmeno, e allora il controllo
    # direbbe «rifiutato» per merito del client. Si dichiara cosa si è potuto
    # provare invece di far finta.
    try:
        rifiutato = not _tls_parla(cli.ca_path, minimo=ssl.TLSVersion.TLSv1,
                                  massimo=ssl.TLSVersion.TLSv1_1)
        check("TLS 1.0/1.1 sono rifiutati", rifiutato,
              "il server ha accettato una versione obsoleta")
    except (ValueError, ssl.SSLError, OSError) as exc:
        print(f"     TLS 1.0/1.1: non provabile da questo client ({exc}); "
              f"nginx.conf dichiara `ssl_protocols TLSv1.2 TLSv1.3`")


def _tls_parla(ca: Path | None, *, minimo, massimo) -> bool:
    import socket
    ctx = ssl.create_default_context(cafile=str(ca) if ca and ca.is_file() else None)
    if not (ca and ca.is_file()):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = minimo
        ctx.maximum_version = massimo
    except ValueError:
        raise
    try:
        with socket.create_connection(("localhost", 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname="localhost"):
                return True
    except Exception:
        return False


# ==================================================================
# §9. lo stato INIZIALE del database, guardato sull'installazione vera
# ==================================================================
def sez_9_stato_iniziale(sha_baseline: str) -> None:
    sezione(9, "lo stato iniziale del database")

    # ⚠ PRECONDIZIONE, e dichiarata come tale.
    #
    # Questa sezione descrive lo stato INIZIALE: una riga in `inventory_versions`,
    # nessun dispositivo, l'amministratore che deve ancora cambiare la password.
    # Eseguirla su un'installazione già usata produceva otto fallimenti che
    # parlavano di versioni e di sessioni mentre la causa era una sola — «questa
    # non è più un'installazione appena inizializzata» — e otto righe rosse che
    # dicono la stessa cosa nascondono quale sia.
    #
    # Quindi si guarda prima, e se la precondizione non tiene si fallisce UNA
    # volta, dicendo cosa fare.
    versioni = psql_int("SELECT count(*) FROM inventory_versions")
    dispositivi = psql_int("SELECT count(*) FROM inventory_devices")
    da_cambiare = psql("SELECT bool_or(must_change_pw)::text FROM users")
    if versioni != 1 or dispositivi != 0 or da_cambiare != "true":
        check("PRECONDIZIONE: l'installazione è appena inizializzata", False,
              f"versioni={versioni} (attesa 1), dispositivi={dispositivi} "
              f"(attesi 0), must_change_pw={da_cambiare} (atteso true). "
              f"§9 descrive lo stato INIZIALE e va eseguita subito dopo "
              f"`production_baseline.py --init`, prima di qualunque accesso: "
              f"su un'installazione già usata non c'è niente da verificare.")
        return
    check("PRECONDIZIONE: l'installazione è appena inizializzata", True)

    check("`inventory_versions` ha esattamente una riga",
          psql_int("SELECT count(*) FROM inventory_versions") == 1,
          psql("SELECT count(*) FROM inventory_versions"))
    check("ed è la versione 1",
          psql_int("SELECT max(version) FROM inventory_versions") == 1,
          psql("SELECT version FROM inventory_versions"))
    check("il digest registrato è quello della baseline APPROVATA",
          psql("SELECT canonical_sha256 FROM inventory_versions WHERE version = 1")
          == sha_baseline,
          psql("SELECT canonical_sha256 FROM inventory_versions WHERE version = 1"))
    check("`inventory_head` punta alla versione 1",
          psql_int("SELECT version FROM inventory_head") == 1,
          psql("SELECT version FROM inventory_head"))

    strutture = {
        "inventory_locations": 3,
        "inventory_rooms": 6,
        "inventory_racks": 102,
        "inventory_devices": 0,
        "photos": 0,
        "inventory_photo_refs": 0,
        "sessions": 0,
    }
    for tabella, atteso in strutture.items():
        trovato = psql_int(f"SELECT count(*) FROM {tabella}")
        check(f"{tabella}: {atteso}", trovato == atteso, f"trovate {trovato}")

    check("esiste esattamente una utenza",
          psql_int("SELECT count(*) FROM users") == 1,
          psql("SELECT username, role FROM users"))
    check("ed è un amministratore con l'obbligo di cambio password",
          psql("SELECT role || '/' || must_change_pw::text FROM users") == "admin/true",
          psql("SELECT role, must_change_pw FROM users"))
    check("la password è un hash Argon2id, non altro",
          psql("SELECT left(password_hash, 10) FROM users") == "$argon2id$",
          psql("SELECT left(password_hash, 12) FROM users"))

    # L'audit: solo la storia di QUESTA installazione.
    # La colonna e' `action` (piu' `result`); `events` e' il JSONB del dettaglio.
    eventi = psql("SELECT action || '/' || result || ':' || count(*) FROM audit "
                  "GROUP BY action, result ORDER BY 1")
    check("l'audit contiene SOLO la riga di bootstrap di questa installazione",
          len(eventi.splitlines()) == 1 and eventi.startswith("bootstrap/"), eventi)

    # Le impostazioni: notifiche spente, nessun destinatario.
    check("le notifiche sono DISATTIVATE",
          psql("SELECT (data->'notifications'->>'enabled') FROM settings") in ("false", ""),
          psql("SELECT data->'notifications' FROM settings"))
    dest = psql("SELECT coalesce(jsonb_array_length(data->'notifications'->'recipients'), 0) FROM settings")
    check("nessun destinatario configurato", dest in ("0", ""), dest)

    # Lo stato del worker: nessun giro finto già fatto.
    check("il worker non ha un giro registrato come già fatto oggi",
          psql("SELECT coalesce(last_run_date::text, 'NULL') FROM worker_heartbeat") in ("NULL", ""),
          psql("SELECT last_run_date FROM worker_heartbeat"))
    for tabella in ("reminders", "reminder_deliveries", "scheduler_runs"):
        n = psql_int(f"SELECT count(*) FROM {tabella}")
        check(f"{tabella} è vuota", n == 0, f"{n} righe")

    # La GC delle foto: si giudica nel MERITO, non contando le righe. Il worker è
    # un servizio di Compose e parte insieme a tutto il resto: un giro di GC su un
    # database vuoto è normale e lascia una riga. Zero esaminate e zero cancellate
    # è quel giro; qualcosa di più è la prova che delle foto sono esistite.
    gc = psql("SELECT coalesce(sum(examined_count), 0) || '/' || "
              "coalesce(sum(deleted_count), 0) FROM maintenance_runs")
    check("la GC delle foto non ha esaminato né cancellato niente",
          gc in ("0/0", ""), f"esaminate/cancellate: {gc}")

    # La proiezione.
    # ⚠ La colonna si chiama `head_version`: è la versione che le tabelle
    # RISPECCHIANO. Il campo `projected_version` del dataclass in projection.py ha
    # un altro nome, e cercarlo nel database dà «column does not exist».
    check("la proiezione è alla versione 1 con lo stesso digest",
          psql("SELECT head_version || '/' || head_sha256 "
               "FROM inventory_projection_state") == f"1/{sha_baseline}",
          psql("SELECT head_version, head_sha256, schema_version, has_manual, "
               "root_extra FROM inventory_projection_state"))
    check("la versione della mappa registrata è quella corrente (2)",
          psql_int("SELECT mapper_version FROM inventory_projection_state") == 2,
          psql("SELECT mapper_version FROM inventory_projection_state"))


# ==================================================================
# §14. il bilancio delle connessioni, con i numeri EFFETTIVI
# ==================================================================
def sez_14_pool() -> None:
    sezione(14, "il bilancio delle connessioni")

    # I due engine NON passano pool_size/max_overflow: usano i default di
    # SQLAlchemy. Il commento in db.py fa il conto su (5 + 10); questo controllo
    # chiede alla libreria INSTALLATA quanto valgono davvero, perché un
    # aggiornamento di dipendenza potrebbe spostarli senza che nessuno tocchi il
    # nostro codice — e allora il conto scritto sarebbe sbagliato e sembrerebbe
    # giusto.
    codice = (
        "import json;"
        "from app.db import get_engine, get_read_engine;"
        "e=get_engine(); r=get_read_engine();"
        "f=lambda p:{'size':p.size(),'overflow':p._max_overflow};"
        "print(json.dumps({'request':f(e.pool),'read':f(r.pool)}))"
    )
    p = compose("exec", "-T", "api", "python", "-c", codice)
    try:
        pool = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        check("i due pool sono interrogabili", False,
              f"{p.stdout.strip()[:200]} {p.stderr.strip()[:200]}")
        return

    cap_req = pool["request"]["size"] + pool["request"]["overflow"]
    cap_read = pool["read"]["size"] + pool["read"]["overflow"]
    print(f"     pool richieste: {pool['request']['size']} + {pool['request']['overflow']} = {cap_req}")
    print(f"     pool letture:   {pool['read']['size']} + {pool['read']['overflow']} = {cap_read}")

    check("la capienza è quella su cui db.py fa il conto (15 + 15)",
          cap_req == 15 and cap_read == 15,
          f"richieste {cap_req}, letture {cap_read}: il conto scritto in db.py "
          f"non vale più")
    # L'invariante che impedisce lo stallo da acquisizione a due fasi.
    check("capienza(letture) >= capienza(richieste)", cap_read >= cap_req,
          "ridurre il pool di lettura reintroduce lo stallo della fase 2D")

    # Quanti lavoratori uvicorn: si legge dal processo, non dal Dockerfile.
    ps = compose("exec", "-T", "api", "sh", "-c",
                 "cat /proc/1/cmdline | tr '\\0' ' '").stdout
    workers = 1
    m = re.search(r"--workers\s+(\d+)", ps)
    if m:
        workers = int(m.group(1))
    print(f"     lavoratori uvicorn: {workers}  ({ps.strip()[:90]})")

    maxconn = psql_int("SHOW max_connections")
    riservate = psql_int("SHOW superuser_reserved_connections") or 0
    # Il bilancio: i due pool per processo, per il numero di lavoratori, più il
    # worker delle notifiche, più un margine per le operazioni (migrate, psql).
    totale_app = (cap_req + cap_read) * workers
    worker_notifiche = 1
    margine_operazioni = 5
    richiesto = totale_app + worker_notifiche + margine_operazioni
    print(f"     bilancio: {totale_app} (app) + {worker_notifiche} (worker) + "
          f"{margine_operazioni} (operazioni) = {richiesto} su max_connections={maxconn} "
          f"(-{riservate} riservate al superuser)")

    check("max_connections copre il bilancio con margine",
          maxconn is not None and (maxconn - riservate) >= richiesto,
          f"richiesto {richiesto}, disponibile {(maxconn or 0) - riservate}")
    # Il numero che qualcuno cambierà: si dichiara qui che moltiplica.
    check("con i lavoratori attuali il conto è quello documentato (30 in totale)",
          totale_app == 30,
          f"{totale_app}: se i lavoratori sono cambiati va rifatto il conto di "
          f"db.py, di max_connections e della memoria")

    # Quante connessioni ci sono DAVVERO adesso, per ruolo: dice se la stima
    # somiglia alla realtà.
    per_ruolo = psql("SELECT usename || '=' || count(*) FROM pg_stat_activity "
                     "WHERE datname = 'tsm' GROUP BY usename ORDER BY usename")
    print(f"     connessioni attive: {per_ruolo.replace(chr(10), ' ')}")
    attive = psql_int("SELECT count(*) FROM pg_stat_activity WHERE datname = 'tsm'")
    check("le connessioni attive sono molte meno del massimo",
          attive is not None and maxconn is not None and attive < maxconn // 2,
          f"{attive} attive su {maxconn}")


# ==================================================================
# §12. concorrenza ottimistica ATTRAVERSO nginx e TLS
# ==================================================================
def sez_12_concorrenza(cli: Client, password: str, utente: str) -> None:
    sezione(12, "concorrenza ottimistica attraverso lo stack vero")

    a = Client(cli.base, cli.ca_path)
    b = Client(cli.base, cli.ca_path)
    for nome, c in (("A", a), ("B", b)):
        r = c.login(utente, password)
        if r["status"] != 200:
            check(f"il client {nome} entra", False, f"HTTP {r['status']}")
            return

    ra = a.request("GET", "/api/inventory")
    rb = b.request("GET", "/api/inventory")
    if ra["status"] != 200 or rb["status"] != 200:
        check("i due client caricano l'inventario", False,
              f"A={ra['status']} B={rb['status']}")
        return
    n = ra["json"]["version"]
    check("i due client vedono la stessa versione",
          n == rb["json"]["version"], f"A={n} B={rb['json']['version']}")

    def con_nota(doc: dict, nota: str, uid: str) -> dict:
        """Aggiunge un dispositivo con una nota riconoscibile al primo rack.

        ⚠ `_uid` è obbligatorio: senza, il documento è rifiutato con 422 e
        `missing_uid`. È l'identità stabile su cui si regge il diff (§8.10), e non
        la genera il server — la genera chi crea l'entità. Gli UID sono FISSI e
        scritti qui, non casuali: il replay di §12 deve rimandare lo STESSO
        candidato, e un UID nuovo a ogni chiamata renderebbe il secondo invio un
        documento diverso, cioè proverebbe l'esatto contrario di quello che serve.
        """
        d = json.loads(json.dumps(doc))
        rack = d["locations"][0]["sale"][0]["racks"][0]
        rack.setdefault("devices", []).append(
            {"_uid": uid, "id": f"QUAL-{nota[-6:]}", "nome": f"prova {nota}",
             "u": 40, "note": nota, "stato": "attivo", "presenza": "presente"})
        return d

    # ⚠ Il campo si chiama `doc`, non `document`, e il corpo del PUT è
    # {baseVersion, doc, action} — senza `schemaVersion`, che il server deduce dal
    # documento. Il contratto sta in app/api/inventory.py, non nell'intuizione.
    doc_a = con_nota(ra["json"]["doc"], CANARINI["inventario"], UID_A)
    doc_b = con_nota(rb["json"]["doc"], "SECONDO-CANDIDATO", UID_B)

    # --- A salva: N+1
    sa = a.request("PUT", "/api/inventory",
                   {"baseVersion": n, "doc": doc_a,
                    "action": "qualificazione 2J: primo salvataggio"})
    check("il salvataggio di A riesce", sa["status"] == 200, f"HTTP {sa['status']} "
          f"{(sa['json'] or {})}")
    if sa["status"] != 200:
        return
    check(f"e produce la versione {n + 1}", sa["json"].get("version") == n + 1,
          f"versione {sa['json'].get('version')}")
    check("A dichiara changed=true", sa["json"].get("changed") is True,
          str(sa["json"].get("changed")))
    check("la proiezione ha seguito", psql_int(
        "SELECT head_version FROM inventory_projection_state") == n + 1,
        psql("SELECT head_version, head_sha256 FROM inventory_projection_state"))
    check("l'audit ha registrato la modifica", psql_int(
        "SELECT count(*) FROM audit WHERE action = 'inventory.save'") >= 1,
        psql("SELECT action, count(*) FROM audit GROUP BY action"))

    # --- B salva un candidato DIVERSO dalla stessa base: 409 pulito
    versioni_prima = psql_int("SELECT count(*) FROM inventory_versions")
    sb = b.request("PUT", "/api/inventory",
                   {"baseVersion": n, "doc": doc_b,
                    "action": "qualificazione 2J: candidato in conflitto"})
    check("il salvataggio di B è rifiutato con 409", sb["status"] == 409,
          f"HTTP {sb['status']} {(sb['json'] or {})}")
    check("e non ha scritto niente",
          psql_int("SELECT count(*) FROM inventory_versions") == versioni_prima,
          "una versione in più dopo un 409 sarebbe una scrittura parziale")
    check("il 409 dichiara la versione corrente",
          (sb["json"] or {}).get("currentVersion") == n + 1
          or str(n + 1) in json.dumps(sb["json"] or {}),
          json.dumps(sb["json"] or {})[:200])

    # --- replay della risposta perduta: A rimanda lo STESSO candidato
    versioni_prima = psql_int("SELECT count(*) FROM inventory_versions")
    audit_prima = psql_int("SELECT count(*) FROM audit WHERE action = 'inventory.save'")
    ra2 = a.request("PUT", "/api/inventory",
                    {"baseVersion": n, "doc": doc_a,
                     "action": "qualificazione 2J: primo salvataggio"})
    check("il replay dello stesso candidato risponde 200", ra2["status"] == 200,
          f"HTTP {ra2['status']} {(ra2['json'] or {})}")
    check("con changed=false", (ra2["json"] or {}).get("changed") is False,
          str((ra2["json"] or {}).get("changed")))
    check("e restituisce la versione corrente",
          (ra2["json"] or {}).get("version") == n + 1,
          str((ra2["json"] or {}).get("version")))
    check("nessuna versione nuova",
          psql_int("SELECT count(*) FROM inventory_versions") == versioni_prima,
          "il replay ha creato una versione")
    check("nessuna voce di audit doppia",
          psql_int("SELECT count(*) FROM audit WHERE action = 'inventory.save'")
          == audit_prima,
          "il replay ha registrato una seconda modifica")


# ==================================================================
# §27. il consumo, da fermo e sotto le operazioni reali
# ==================================================================
def sez_27_risorse(cli: Client) -> dict:
    sezione(27, "consumo e tempi di risposta")

    misure: dict[str, object] = {}

    # A riposo: memoria dei container, disco dei due volumi, swap.
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
        capture_output=True, text=True, timeout=120).stdout.strip()
    print("     container (a riposo):")
    for riga in stats.splitlines():
        print(f"       {riga}")
    misure["containers"] = stats

    for etichetta, percorso in (("sistema", "/"), ("dati", "/srv/tsm-data")):
        df = subprocess.run(["df", "-h", "--output=size,used,avail,pcent", percorso],
                            capture_output=True, text=True).stdout.strip().splitlines()
        if len(df) > 1:
            print(f"     disco {etichetta}: {' '.join(df[-1].split())}")
            misure[f"disk_{etichetta}"] = " ".join(df[-1].split())

    libera = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout.strip()
    print("     memoria:")
    for riga in libera.splitlines()[:3]:
        print(f"       {riga}")
    misure["memory"] = libera

    check("il disco dei dati è un filesystem distinto da /",
          _dev_di("/srv/tsm-data") != _dev_di("/"),
          "il volume dei dati sta sullo stesso filesystem della radice")
    check("lo spazio libero sul disco dei dati è sopra la soglia del preflight (5 GiB)",
          _liberi_bytes("/srv/tsm-data") > 5 * 1024**3,
          f"{_liberi_bytes('/srv/tsm-data') / 1024**3:.1f} GiB")

    # I tempi: mediana e p95 su un numero modesto di ripetizioni. Non è un
    # benchmark, è un confronto con le misure già registrate nel piano.
    def tempi(nome: str, percorso: str, giri: int = 15) -> None:
        campioni = []
        for _ in range(giri):
            t0 = time.perf_counter()
            r = cli.request("GET", percorso)
            campioni.append((time.perf_counter() - t0) * 1000)
            if r["status"] != 200:
                check(f"{nome} risponde 200", False, f"HTTP {r['status']}")
                return
        campioni.sort()
        med = campioni[len(campioni) // 2]
        p95 = campioni[min(len(campioni) - 1, int(len(campioni) * 0.95))]
        misure[nome] = {"median_ms": round(med, 1), "p95_ms": round(p95, 1),
                        "n": giri}
        print(f"     {nome:<28} mediana {med:6.1f} ms   p95 {p95:6.1f} ms")

    tempi("GET /api/inventory", "/api/inventory")
    tempi("GET search", "/api/inventory/search?q=rack")
    tempi("GET capacity", "/api/inventory/capacity")
    tempi("GET expiries", "/api/inventory/expiries")
    tempi("GET /api/ready", "/api/ready")

    # Nessuna soglia inventata: si controlla soltanto che nulla sia fuori scala di
    # un ordine di grandezza rispetto alle misure della 2D/2H (39 ms mediani per
    # un GET). Un numero preciso qui diventerebbe un test che fallisce quando la
    # macchina è occupata, e allora lo si disattiverebbe.
    for nome in ("GET /api/inventory", "GET search", "GET capacity"):
        m = misure.get(nome)
        if isinstance(m, dict):
            check(f"{nome}: la mediana resta nell'ordine di grandezza atteso (< 2 s)",
                  m["median_ms"] < 2000, f"{m['median_ms']} ms")
    return misure


def _dev_di(percorso: str) -> int:
    return os.stat(percorso).st_dev


def _liberi_bytes(percorso: str) -> int:
    s = os.statvfs(percorso)
    return s.f_bavail * s.f_frsize


# ==================================================================
# §30. la battuta sui segreti, con canarini
# ==================================================================
def sez_30_segreti(password_provvisoria: str, password_nuova: str) -> None:
    sezione(30, "nessun segreto nei log, nel database, negli artefatti")

    # Il canarino delle password si USA, adesso: si tenta un accesso con quel
    # valore come password sbagliata.
    #
    # ⚠ Senza questo, la ricerca «il canarino non compare da nessuna parte»
    # passerebbe perché il canarino non è mai entrato nel sistema — cioè
    # proverebbe che una stringa mai inviata non si trova, che non è una notizia.
    # Un tentativo fallito è precisamente il momento in cui una password potrebbe
    # finire in un log di eccezione o in una riga di audit, quindi è il caso da
    # far accadere prima di cercare.
    sonda = Client(BASE_GLOBALE or "https://localhost", CA_GLOBALE)
    r = sonda.request("POST", "/api/auth/login",
                      {"username": "canarino-inesistente",
                       "password": CANARINI["password"]})
    check("il tentativo con il canarino come password è stato rifiutato",
          r["status"] != 200, f"HTTP {r['status']}")
    # E deve essere stato REGISTRATO: il limitatore conta i tentativi, quindi la
    # riga esiste — ed è la riga in cui la password non deve esserci.
    n = psql("SELECT count(*) FROM login_attempts")
    check("e registrato dal limitatore (la riga dove cercare esiste)",
          n.isdigit() and int(n) > 0, f"login_attempts: {n}")

    segreti = {
        "password provvisoria del bootstrap": password_provvisoria,
        "password nuova dell'amministratore": password_nuova,
        "canarino piantato come password sbagliata": CANARINI["password"],
    }
    # I secret montati: il loro CONTENUTO non deve comparire nei log.
    for nome in ("postgres_password", "api_db_password", "worker_db_password"):
        f = APP / "secrets" / nome
        if f.is_file():
            try:
                segreti[f"secret {nome}"] = f.read_text(encoding="utf-8").strip()
            except PermissionError:
                pass
    segreti = {k: v for k, v in segreti.items() if v and len(v) >= 8}

    # ---- i log dei container
    log = ""
    for servizio in ("api", "worker", "web", "db", "migrate"):
        p = compose("logs", "--no-color", "--tail", "20000", servizio, timeout=180)
        log += p.stdout + p.stderr
    print(f"     log raccolti: {len(log)} caratteri")
    check("i log dei container non sono vuoti (la ricerca ha materiale)",
          len(log) > 500, f"{len(log)} caratteri: una ricerca su niente non prova niente")

    for nome, valore in segreti.items():
        check(f"log: nessuna occorrenza di «{nome}»", valore not in log,
              "il valore compare nei log dei container")

    # ---- le colonne di testo del database
    colonne = psql(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type IN "
        "('text','character varying','jsonb','json') ORDER BY 1")
    elenco = [c for c in colonne.splitlines() if c and "__ERRORE__" not in c]
    check("le colonne di testo sono enumerabili", len(elenco) > 10,
          f"{len(elenco)} colonne")

    def scansiona(valore: str) -> list[str]:
        """Cerca `valore` in ogni colonna di testo. Restituisce i colpi."""
        colpi: list[str] = []
        for col in elenco:
            tabella, colonna = col.split(".", 1)
            # `::text` perché jsonb non si confronta con LIKE senza cast.
            n = psql(f"SELECT count(*) FROM {tabella} WHERE "
                     f"{colonna}::text LIKE '%' || $q${valore}$q$ || '%'")
            if n.isdigit() and int(n) > 0:
                colpi.append(f"{col}={n}")
        return colpi

    # ---- LA CONTROPROVA. Senza, «nessun segreto trovato» non è una notizia: non
    # si distingue un database pulito da una scansione che non guarda niente.
    #
    # ⚠ Non si usa un canarino piantato in un dispositivo da una sezione
    # precedente: la prima stesura lo faceva, e una suite eseguita dopo aveva
    # sostituito il documento dell'inventario portandoselo via. La controprova
    # falliva parlando della ricerca mentre il problema era l'ordine dei test.
    # Si usano invece due valori che l'installazione contiene PER COSTRUZIONE:
    # il nome dell'amministratore e il digest della baseline. Se la scansione non
    # trova quelli, non trova niente.
    for etichetta, valore in (("il nome dell'amministratore", "admin"),
                              ("il digest della baseline",
                               "febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1")):
        colpi = scansiona(valore)
        check(f"controprova: la scansione TROVA {etichetta}", bool(colpi),
              f"nessun colpo: la scansione non guarda niente, quindi «nessun "
              f"segreto trovato» non significherebbe nulla")

    for nome, valore in segreti.items():
        colpi = scansiona(valore)
        check(f"database: nessuna occorrenza in chiaro di «{nome}»", not colpi,
              ", ".join(colpi))

    # ---- l'audit, che è il posto dove un tentativo fallito potrebbe finire
    audit = psql("SELECT coalesce(string_agg(row_to_json(a)::text, ' '), '') FROM audit a")
    for nome, valore in segreti.items():
        check(f"audit: nessuna occorrenza di «{nome}»", valore not in audit,
              "il valore compare in una riga di audit")
    # E l'hash non va nell'audit nemmeno lui.
    check("audit: nessun hash di password", "$argon2id$" not in audit,
          "un hash nell'audit è un segreto nell'audit")

    # ---- gli artefatti generati durante la qualificazione
    for artefatto in sorted(Path("/root").glob("*.json")) + \
                     sorted(Path("/root").glob("*.txt")):
        try:
            testo = artefatto.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for nome, valore in segreti.items():
            if valore in testo:
                check(f"artefatto {artefatto.name}: nessuna occorrenza di «{nome}»",
                      False, "un segreto è finito in un file di risultati")
    check("gli artefatti di qualificazione non contengono segreti",
          all(r[2] for r in risultati if r[1].startswith("artefatto ")), "")


# ==================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="qualificazione del rilascio")
    ap.add_argument("--dir", default="/opt/tsm")
    ap.add_argument("--base", default=None,
                    help="predefinito: https://localhost (nginx sull'host)")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--baseline-sha",
                    default="febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1",
                    help="scritto a mano dal manifesto, non letto dal codice")
    ap.add_argument("--section", type=int, action="append", default=None)
    ap.add_argument("--json", default=None, help="dove scrivere i risultati")
    ap.add_argument("--new-password", default=None,
                    help="password gia' cambiata dell'amministratore")
    args = ap.parse_args()

    global APP
    APP = Path(args.dir)
    base = args.base or f"https://{HOST}"
    ca = APP / "secrets" / "tls" / "fullchain.pem"
    globals()["BASE_GLOBALE"] = base
    globals()["CA_GLOBALE"] = ca

    provvisoria = os.environ.get("TSM_QUAL_PASSWORD", "")
    nuova = args.new_password or os.environ.get("TSM_QUAL_NEW_PASSWORD", "")

    vuole = set(args.section) if args.section else None

    def scelta(n: int) -> bool:
        return vuole is None or n in vuole

    # ⚠ L'accesso serve solo a §12, §27 e §30, e va richiesto solo se una di
    # quelle è stata scelta. Pretenderlo sempre renderebbe impossibile eseguire
    # §9 — lo stato INIZIALE del database — che per definizione va guardato prima
    # che qualcuno entri e cambi la password provvisoria.
    serve_accesso = any(scelta(n) for n in (12, 16, 27, 30))
    cli = Client(base, ca)
    if serve_accesso:
        if not nuova:
            print("le sezioni 12/27/30 richiedono TSM_QUAL_NEW_PASSWORD "
                  "(la password corrente dell'amministratore)", file=sys.stderr)
            return 2
        r = cli.login(args.username, nuova)
        if r["status"] != 200:
            print(f"accesso non riuscito: HTTP {r['status']} "
                  f"{(r.get('json') or r.get('error'))}", file=sys.stderr)
            return 2

    misure = None
    if scelta(5):
        sez_5_separazione(cli)
    if scelta(9):
        sez_9_stato_iniziale(args.baseline_sha)
    if scelta(16):
        sez_16_tls(cli, args.username, nuova)
    if scelta(14):
        sez_14_pool()
    if scelta(12):
        sez_12_concorrenza(cli, nuova, args.username)
    if scelta(27):
        misure = sez_27_risorse(cli)
    if scelta(30):
        sez_30_segreti(provvisoria, nuova)

    passati = sum(1 for r in risultati if r[2])
    falliti = len(risultati) - passati
    print(f"\n=== {passati} PASS, {falliti} FAIL su {len(risultati)} controlli ===")
    if falliti:
        print("\nfalliti:")
        for s, nome, ok, dett in risultati:
            if not ok:
                print(f"  §{s} {nome}" + (f"  -> {dett}" if dett else ""))

    if args.json:
        # ⚠ Le misure sì, i segreti no: questo file si allega al manifesto.
        Path(args.json).write_text(json.dumps({
            "checks": [{"section": s, "name": n, "passed": p, "detail": d}
                       for s, n, p, d in risultati],
            "passed": passati, "failed": falliti,
            "measurements": misure if isinstance(misure, dict) else None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"risultati in {args.json}")

    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
