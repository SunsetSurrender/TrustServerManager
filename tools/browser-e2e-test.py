#!/usr/bin/env python3
"""Flusso completo nel browser, attraverso nginx e TLS sulla porta 443.

Non si chiama FastAPI direttamente. Il motivo è che metà di quello che c'è da
verificare vive fra il browser e il proxy: il cookie `Secure` che senza HTTPS non
viene inviato, HSTS, il reindirizzamento da HTTP, la validazione di `Origin`, e
l'allowlist dei file statici. Un test contro l'API salterebbe tutto questo e
direbbe che funziona.

Il certificato di sviluppo è autofirmato: si accetta l'errore di certificato
(`ignore_https_errors`), che è l'unica finzione di questo test. Tutto il resto è
la catena vera.

Prerequisiti:
    .\\tools\\make-dev-tls.ps1
    docker compose up -d --build --wait
    docker compose run --rm -v ".../fixtures:/seed:ro" migrate \\
        python scripts/bootstrap.py --seed /seed/seed.json --from-legacy

Uso:
    python tools/browser-e2e-test.py --base https://localhost --password <pw>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import destructive_guard

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--new-password", default="")
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    host = urlparse(base).hostname

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        # ignore_https_errors: il certificato di sviluppo è autofirmato.
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        console_errors: list[str] = []
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # I 401 su /api/auth/me PRIMA del login e dopo il logout sono attesi: è così
        # che il client scopre di dover mostrare la schermata di accesso (§8.1).
        # Filtrare sul testo del messaggio non basta, perché non contiene la URL.
        bad_responses: list[str] = []

        def _on_response(resp):
            if resp.status < 400:
                return
            path = urlparse(resp.url).path
            if resp.status == 401 and path == "/api/auth/me":
                return
            if path.endswith("favicon.ico"):
                return
            bad_responses.append(f"{resp.status} {path}")

        page.on("response", _on_response)
        dialogs: list[str] = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

        api_calls: list[str] = []
        page.on("request", lambda r: api_calls.append(f"{r.method} {urlparse(r.url).path}")
                if "/api/" in r.url else None)

        # ---------------- 1. TLS e reindirizzamento ----------------
        r = page.goto(f"http://{host}/", wait_until="domcontentloaded")
        check("HTTP reindirizza a HTTPS",
              page.url.startswith("https://"), f"finito su {page.url}")

        r = page.goto(f"{base}/", wait_until="load")
        check("la pagina è servita via HTTPS", r is not None and r.status == 200,
              f"status {r.status if r else '-'}")
        hsts = (r.headers or {}).get("strict-transport-security", "")
        check("HSTS presente sulla risposta HTTPS", "max-age=" in hsts, f"HSTS={hsts!r}")

        page.wait_for_timeout(4000)

        # ---------------- 2. avvio via /api/auth/me, login su 401 ----------------
        check("l'avvio interroga /api/auth/me",
              any(c == "GET /api/auth/me" for c in api_calls),
              f"chiamate: {api_calls[:6]}")
        check("l'inventario NON è chiesto prima di essere autenticati",
              not any(c == "GET /api/inventory" for c in api_calls),
              f"chiamate: {api_calls[:6]}")
        check("il login è mostrato dopo il 401",
              page.get_by_role("button", name="Accedi").is_visible())

        # ---------------- 3. credenziali sbagliate ----------------
        page.get_by_placeholder("utente").fill(args.username)
        page.get_by_placeholder("password").fill("del-tutto-sbagliata")
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("credenziali errate: messaggio generico e nessun accesso",
              "Credenziali non valide" in body, body[:200])
        # Il 401 appena provocato è voluto: non deve contare come errore inatteso.
        bad_responses.clear()

        # ---------------- 4. accesso corretto ----------------
        api_calls.clear()
        page.get_by_placeholder("password").fill(args.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(4000)

        check("il login passa da POST /api/auth/login",
              any(c == "POST /api/auth/login" for c in api_calls), str(api_calls[:6]))

        # ---- 4b. password provvisoria: sessione valida ma ristretta (§8.26) ----
        forced = page.get_by_role("button", name="Cambia password")
        if forced.count() and forced.is_visible():
            check("password provvisoria: si impone il cambio",
                  "provvisoria" in page.inner_text("body").lower())
            check("con password provvisoria l'inventario NON viene chiesto",
                  not any(c == "GET /api/inventory" for c in api_calls),
                  str(api_calls[:8]))

            new_password = args.new_password or (args.password + "-cambiata")
            page.get_by_placeholder("password attuale").fill(args.password)
            page.get_by_placeholder("nuova password (min. 10 caratteri)").fill(new_password)
            forced.click()
            page.wait_for_timeout(3000)

            # Il cambio revoca tutte le sessioni: si deve tornare al login.
            check("dopo il cambio password si torna al login",
                  page.get_by_role("button", name="Accedi").is_visible(),
                  page.inner_text("body")[:200])

            api_calls.clear()
            page.get_by_placeholder("utente").fill(args.username)
            page.get_by_placeholder("password").fill(new_password)
            page.get_by_role("button", name="Accedi").click()
            page.wait_for_timeout(4000)
            args.password = new_password    # per le prove successive

        check("l'inventario si carica DOPO l'autenticazione senza restrizioni",
              any(c == "GET /api/inventory" for c in api_calls), str(api_calls[:8]))

        cookies = {c["name"]: c for c in context.cookies()}
        sc = cookies.get("tsm_session")
        check("cookie di sessione presente", sc is not None, f"cookie: {list(cookies)}")
        if sc:
            check("cookie Secure", bool(sc.get("secure")), json.dumps(sc))
            check("cookie HttpOnly", bool(sc.get("httpOnly")), json.dumps(sc))
            check("cookie SameSite=Strict",
                  str(sc.get("sameSite", "")).lower() == "strict", json.dumps(sc))

        page.wait_for_timeout(1500)
        check("interfaccia caricata (pulsante Esporta presente)",
              page.get_by_role("button", name="Esporta ▾").is_visible())

        probe = page.evaluate(
            "() => ({ hasVersion: /v[0-9]+/.test(document.body.innerText),"
            "         text: document.body.innerText.slice(0, 300) })")
        check("la versione del server è mostrata", probe["hasVersion"], probe["text"][:200])

        # ---------------- 5. un salvataggio reale ----------------
        api_calls.clear()
        page.get_by_role("button", name="Editing", exact=True).click()
        page.wait_for_timeout(800)
        page.get_by_role("button", name="+ Sito").click()
        page.get_by_placeholder("Nome (es. Oriolo Romano — A0)").fill("Sito E2E TLS")
        page.get_by_role("button", name="Crea").click()
        page.wait_for_timeout(4000)

        check("il salvataggio passa da PUT /api/inventory",
              any(c == "PUT /api/inventory" for c in api_calls), str(api_calls[:8]))
        check("una sola PUT per una sola modifica",
              sum(1 for c in api_calls if c == "PUT /api/inventory") == 1,
              str(api_calls))

        # La modifica è davvero nel database: si ricarica la pagina da zero.
        page.reload(wait_until="load")
        page.wait_for_timeout(5000)
        body = page.inner_text("body")
        check("la modifica è persistita e ricompare dopo il ricaricamento",
              "Sito E2E TLS" in body, body[:300])

        # ---------------- 6. nessun errore inatteso ----------------
        check("nessun errore JavaScript non gestito", not console_errors,
              " | ".join(console_errors[:3]))
        check("nessuna risposta di errore inattesa", not bad_responses,
              " | ".join(bad_responses[:5]))
        check("nessun alert inatteso", not dialogs, " | ".join(dialogs[:3]))

        # ---------------- 7. allowlist statica attraverso nginx ----------------
        for blocked in ("/inventario.js", "/README.md",
                        "/Sale%20Server%20Pomezia%20(standalone).html"):
            resp = page.request.get(f"{base}{blocked}", ignore_https_errors=True)
            check(f"{blocked} non servito", resp.status != 200, f"HTTP {resp.status}")

        # ---------------- 8. logout ----------------
        # Il pulsante è nel pannello Profilo (avatar con le iniziali), non in
        # Impostazioni.
        api_calls.clear()
        bad_responses.clear()
        page.get_by_role("button", name="⚙ Impostazioni").click()   # chiude eventuali menu
        page.wait_for_timeout(300)
        page.get_by_role("button", name="⚙ Impostazioni").click()
        page.wait_for_timeout(300)

        avatar = page.get_by_role("button", name="AD", exact=True)
        check("avatar del profilo presente", avatar.count() == 1, f"{avatar.count()} trovati")
        avatar.first.click()
        page.wait_for_timeout(800)
        logout = page.get_by_role("button", name="Esci dall'applicazione")
        check("pulsante di logout presente nel profilo", logout.count() > 0)
        if logout.count():
            logout.first.dispatch_event("click")
            page.wait_for_timeout(2500)
            check("il logout passa da POST /api/auth/logout",
                  any(c == "POST /api/auth/logout" for c in api_calls), str(api_calls))
            check("dopo il logout si torna al login",
                  page.get_by_role("button", name="Accedi").is_visible())
            check("dopo il logout l'inventario non è più leggibile",
                  page.request.get(f"{base}/api/inventory",
                                   ignore_https_errors=True).status == 401)

        browser.close()

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


if __name__ == "__main__":
    sys.exit(main())
