#!/usr/bin/env python3
"""Regressione: le intestazioni `X-Forwarded-*` non sono falsificabili.

Attraverso nginx VERO, sulla porta 443. È l'unico modo di provarlo: chiamando
FastAPI direttamente il peer non è un proxy fidato, quindi gli header vengono
ignorati e il test passerebbe anche con nginx configurato male — che è
esattamente com'è stato scoperto il difetto che questo file previene.

Tre proprietà:

  1. nginx SOVRASCRIVE `X-Forwarded-Proto`, `X-Forwarded-Host` e
     `X-Forwarded-For`: non li accoda e non li lascia passare;
  2. l'API si fida di quegli header SOLO se il peer diretto è il proxy;
  3. una richiesta attraverso nginx con header falsificati non riesce a
     spacciare origine, schema, host o IP del client.

Il difetto storico: `$proxy_add_x_forwarded_for` ACCODA l'IP reale all'header
del client, quindi un `X-Forwarded-For: 1.2.3.4` arrivava all'API come
"1.2.3.4, <ip reale>" e la prima voce — quella scelta dall'attaccante — veniva
presa come IP del client, aggirando il limitatore per IP.

Uso:
    python tools/proxy-security-test.py --base https://localhost
"""
from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Su Windows lo stdout piped usa cp1252 e non regge i caratteri non-ASCII dei
# messaggi: si forza UTF-8 invece di scrivere i messaggi in ASCII stentato.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

FORGED_IP = "203.0.113.77"          # TEST-NET-3, non instradabile
FORGED_HOST = "malintenzionato.example"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


def req(base: str, path: str, *, method: str = "GET", headers: dict | None = None,
        body: dict | None = None, cookie: str | None = None) -> tuple[int, dict, str]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data else {}
    h.update(headers or {})
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(f"{base}{path}", data=data, headers=h, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_TLS))
    try:
        with opener.open(r, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()
    except Exception as e:                                    # pragma: no cover
        return 0, {}, str(e)


def psql(sql: str) -> str:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "tsm", "-d", "tsm",
         "-tAc", sql],
        cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # ---------------------------------------------------------------
    # 0. la configurazione di nginx SOVRASCRIVE, non accoda
    # ---------------------------------------------------------------
    #
    # Perché un controllo sul file e non solo sul comportamento: le due difese —
    # nginx che sovrascrive e l'API che legge l'ultima voce della catena — sono
    # indipendenti, e ognuna da sola nasconde la regressione dell'altra. Con
    # `$proxy_add_x_forwarded_for` l'header diventa "falso, reale" e l'API,
    # leggendo l'ultima voce, prende comunque quella giusta: il comportamento
    # resta corretto mentre la configurazione è tornata vulnerabile. Se poi
    # cambiasse anche il lato API, si aprirebbero entrambe insieme.
    for conf_name in ("nginx.conf", "nginx.dev.conf"):
        conf = (ROOT / "web" / conf_name).read_text(encoding="utf-8")
        # Solo le DIRETTIVE: i commenti nominano `$proxy_add_x_forwarded_for` per
        # spiegare perché non si usa, e un controllo sul testo grezzo scambierebbe
        # la spiegazione per il difetto.
        directives = [ln.strip() for ln in conf.splitlines()
                      if ln.strip().startswith("proxy_set_header")]
        joined = " | ".join(directives)

        xff = [d for d in directives if "X-Forwarded-For" in d]
        check(f"{conf_name}: X-Forwarded-For sovrascritto con $remote_addr",
              len(xff) == 1 and "$remote_addr" in xff[0], f"trovato: {xff}")
        check(f"{conf_name}: X-Forwarded-For non accoda",
              not any("$proxy_add_x_forwarded_for" in d for d in directives),
              f"direttive: {joined[:200]}")
        check(f"{conf_name}: X-Forwarded-Host impostato",
              any("X-Forwarded-Host" in d for d in directives),
              "non impostato: quello del client passerebbe intatto")
        check(f"{conf_name}: X-Forwarded-Proto sovrascritto con $scheme",
              any("X-Forwarded-Proto" in d and "$scheme" in d for d in directives),
              f"direttive: {joined[:200]}")

    # ---------------------------------------------------------------
    # 1. effetto osservabile: l'IP che finisce nel database
    # ---------------------------------------------------------------
    before = psql("SELECT count(*) FROM login_attempts")

    # Tentativo fallito con header falsificati. Il login non porta cookie, quindi
    # la validazione di origine non entra in gioco: qui interessa solo l'IP.
    status, _, _ = req(base, "/api/auth/login", method="POST",
                       headers={"X-Forwarded-For": FORGED_IP,
                                "X-Forwarded-Host": FORGED_HOST,
                                "X-Forwarded-Proto": "http",
                                "Origin": base},
                       body={"username": "utente-inesistente-per-test",
                             "password": "sbagliata"})
    check("il tentativo falsificato riceve 401", status == 401, f"HTTP {status}")

    after = psql("SELECT count(*) FROM login_attempts")
    check("il tentativo è stato registrato", after != before, f"{before} -> {after}")

    forged_rows = psql(
        f"SELECT count(*) FROM login_attempts WHERE ip = '{FORGED_IP}'")
    check("l'IP falsificato NON è finito nel registro dei tentativi",
          forged_rows == "0",
          f"{forged_rows} righe con ip={FORGED_IP}")

    last_ip = psql("SELECT ip FROM login_attempts ORDER BY id DESC LIMIT 1")
    check("l'IP registrato è quello del proxy, non quello dichiarato",
          last_ip != FORGED_IP and last_ip != "",
          f"ip registrato = {last_ip!r}")

    audit_forged = psql(
        f"SELECT count(*) FROM audit WHERE ip = '{FORGED_IP}'")
    check("l'IP falsificato NON è finito nell'audit", audit_forged == "0",
          f"{audit_forged} righe")

    # ---------------------------------------------------------------
    # 2. X-Forwarded-Host falsificato non sposta l'origine attesa
    # ---------------------------------------------------------------
    # Serve una sessione: la validazione di origine si applica alle richieste che
    # modificano stato E portano il cookie.
    cookie = None
    if args.password:
        status, headers, _ = req(base, "/api/auth/login", method="POST",
                                 headers={"Origin": base},
                                 body={"username": args.username,
                                       "password": args.password})
        # urllib conserva la maiuscolatura originale: si cerca senza distinguerla.
        raw = next((v for k, v in headers.items() if k.lower() == "set-cookie"), "")
        if status == 200 and "tsm_session=" in raw:
            cookie = raw.split(";")[0]
        check("sessione ottenuta per la prova sull'origine", cookie is not None,
              f"HTTP {status}")

    if cookie:
        # L'attacco: dichiaro di essere arrivato da un altro host, e mando un
        # Origin che combacia con quello. Se nginx lasciasse passare
        # X-Forwarded-Host, l'API costruirebbe l'origine attesa con il valore
        # dell'attaccante e la richiesta verrebbe accettata.
        status, _, body = req(base, "/api/auth/password", method="POST",
                              headers={"Origin": f"https://{FORGED_HOST}",
                                       "X-Forwarded-Host": FORGED_HOST,
                                       "X-Forwarded-Proto": "https"},
                              body={"currentPassword": "x" * 12,
                                    "newPassword": "y" * 12},
                              cookie=cookie)
        check("Origin estraneo + X-Forwarded-Host falsificato → rifiutato",
              status == 403 and "origin_not_allowed" in body,
              f"HTTP {status}: {body[:160]}")

        # Controprova: senza falsificazione, con l'origine giusta, la richiesta
        # arriva all'handler (fallisce sulla password, non sull'origine).
        status, _, body = req(base, "/api/auth/password", method="POST",
                              headers={"Origin": base},
                              body={"currentPassword": "password-sbagliata-ma-lunga",
                                    "newPassword": "y" * 12},
                              cookie=cookie)
        check("con l'origine giusta la richiesta arriva all'handler",
              status != 403 or "origin_not_allowed" not in body,
              f"HTTP {status}: {body[:160]}")

        # X-Forwarded-Proto falsificato non deve far credere all'API di essere
        # su http e accettare un Origin http.
        status, _, body = req(base, "/api/auth/password", method="POST",
                              headers={"Origin": f"http://{urlparse_host(base)}",
                                       "X-Forwarded-Proto": "http"},
                              body={"currentPassword": "x" * 12,
                                    "newPassword": "y" * 12},
                              cookie=cookie)
        check("X-Forwarded-Proto falsificato non abilita un Origin http",
              status == 403 and "origin_not_allowed" in body,
              f"HTTP {status}: {body[:160]}")

    # ---------------------------------------------------------------
    # 3. non esiste una scorciatoia che scavalchi il proxy
    # ---------------------------------------------------------------
    # L'API non è pubblicata sull'host. Non è pignoleria: le richieste dall'host
    # al container arrivano attraverso il bridge di Docker con sorgente 172.x,
    # cioè DENTRO le reti fidate, quindi da lì gli header `X-Forwarded-*`
    # verrebbero creduti. "Solo loopback" non bastava (§8.34).
    import socket
    reachable = True
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=3):
            pass
    except OSError:
        reachable = False
    check("l'API non è raggiungibile direttamente dall'host", not reachable,
          "127.0.0.1:8000 risponde: la porta è pubblicata e scavalca il proxy")

    # Formato JSON: `{{.Publishers}}` stampa "{ 8000 0 tcp}", dove il secondo
    # numero è la porta HOST. Zero = non pubblicata. Leggere quella stringa a
    # occhio porta a confondere la porta del container con quella dell'host.
    api_pub = []
    for line in subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=ROOT, capture_output=True, text=True).stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Service") == "api":
            api_pub = [p for p in (row.get("Publishers") or [])
                       if p.get("PublishedPort")]
    check("il servizio api non pubblica porte sull'host", not api_pub,
          json.dumps(api_pub)[:200])

    print("=" * 76)
    ok = True
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        if detail and not passed:
            print(f"         → {detail}")
        ok &= passed
    print("=" * 76)
    print("RISULTATO:", "TUTTI I CONTROLLI PASSATI" if ok else "CI SONO FALLIMENTI")
    return 0 if ok else 1


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc


if __name__ == "__main__":
    sys.exit(main())
