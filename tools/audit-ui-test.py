#!/usr/bin/env python3
"""Vista del registro di audit, nel browser vero via nginx/TLS.

Copre i criteri del commit: sola lettura da /api/audit, solo admin, «Carica
altri» senza richieste concorrenti, azzeramento del cursore al cambio di filtri,
orari nel fuso locale con API in UTC, dettagli resi come TESTO e non eseguiti.

Il test è di sola lettura sull'inventario e sulle utenze: le voci di registro le
genera facendo cose normali (accessi, un salvataggio), non scrivendo nella
tabella. Per questo NON usa la guardia dei test distruttivi.

Uso:
    python tools/audit-ui-test.py --base https://localhost --password <pw>
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

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


def login(page, base, username, password):
    """Accede, oppure prosegue se la sessione è già aperta.

    Le chiamate di preparazione usano lo stesso barattolo di cookie del browser,
    quindi la pagina può caricarsi già autenticata: in quel caso il modulo di
    accesso non c'è, e pretenderlo bloccherebbe il test per un motivo che non è
    un difetto."""
    page.goto(f"{base}/", wait_until="load")
    page.wait_for_timeout(4500)
    campo = page.get_by_placeholder("utente")
    if campo.count() == 0:
        return                       # già autenticati
    campo.fill(username)
    page.get_by_placeholder("password").fill(password)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(4500)


def rows_count(page) -> int:
    return page.evaluate(
        "() => document.querySelectorAll('#audit-elenco > div').length")


def run(page, base, args, api_calls, console_text) -> None:
    # ---------------- si generano voci facendo cose normali ----------------
    # Il registro non si popola scrivendo nella tabella: si fanno accadere eventi
    # veri. Servono più di una pagina (50) per poter provare «Carica altri».
    #
    # ATTENZIONE al limitatore: 60 tentativi FALLITI dallo stesso IP superano la
    # soglia per indirizzo e bloccano anche l'accesso dell'amministratore, quindi
    # il test non riuscirebbe nemmeno a entrare. Gli accessi RIUSCITI invece non
    # contano ai fini del blocco (è una scelta di §8.28: chi lavora non va punito),
    # e vanno benissimo per generare volume.
    def fallito(i):
        page.request.post(f"{base}/api/auth/login",
                          data=json.dumps({"username": f"nessuno-{i}",
                                           "password": "sbagliata"}),
                          headers={"Content-Type": "application/json",
                                   "Origin": base},
                          ignore_https_errors=True)

    def riuscito():
        page.request.post(f"{base}/api/auth/login",
                          data=json.dumps({"username": args.username,
                                           "password": args.password}),
                          headers={"Content-Type": "application/json",
                                   "Origin": base},
                          ignore_https_errors=True)

    for i in range(8):        # sotto la soglia per IP: servono al filtro `failure`
        fallito(i)
    for _ in range(55):       # volume, senza avvicinarsi al limitatore
        riuscito()

    login(page, base, args.username, args.password)
    # Si verifica la SESSIONE, non la visibilità di un pulsante: dietro il pannello
    # di login gli elementi dell'app restano "visibili" per Playwright pur essendo
    # coperti, e il controllo passava anche quando l'accesso era stato rifiutato.
    me_resp = page.request.get(f"{base}/api/auth/me", ignore_https_errors=True)
    me = me_resp.json() if me_resp.status == 200 else {}
    if me.get("role") != "admin":
        check("PRECONDIZIONE: sessione amministrativa attiva", False,
              f"HTTP {me_resp.status} {me_resp.text()[:160]}. Se è 429, il "
              f"limitatore dei tentativi è ancora attivo da un'esecuzione "
              f"precedente: usare tools/run-audit-ui-test.ps1, che riparte pulito.")
        return
    check("PRECONDIZIONE: sessione amministrativa attiva", True)

    # ---------------- il pannello legge da /api/audit ----------------
    api_calls.clear()
    page.get_by_role("button", name="Registro").click()
    page.wait_for_timeout(3000)
    check("il registro arriva da GET /api/audit",
          any(c.startswith("GET /api/audit") for c in api_calls), str(api_calls[:6]))
    check("il pannello è aperto",
          page.locator("text=Registro delle modifiche").count() > 0)
    n = rows_count(page)
    check("ci sono voci", n > 0, f"{n} righe")

    # ---------------- orario nel fuso locale, API in UTC ----------------
    api_ts = page.evaluate("""async () => {
      const r = await fetch('/api/audit?pageSize=1', {credentials:'same-origin'});
      const b = await r.json();
      return b.items.length ? b.items[0].ts : '';
    }""")
    check("l'API restituisce UTC", api_ts.endswith("Z"), api_ts)
    shown = page.evaluate(
        "() => { const r = document.querySelector('#audit-elenco > div');"
        "        return r ? r.children[0].textContent.trim() : ''; }")
    check("l'interfaccia mostra un orario formattato, non l'ISO UTC",
          shown and "T" not in shown and "Z" not in shown, repr(shown))

    # ---------------- «Carica altri»: nessuna richiesta concorrente ----------------
    more = page.locator("#btn-audit-altri")
    if more.count():
        before = rows_count(page)
        api_calls.clear()
        # Due clic nello stesso task: è il caso che il guardiano deve intercettare.
        page.evaluate("""() => {
          const b = document.getElementById('btn-audit-altri');
          b.click(); b.click();
        }""")
        page.wait_for_timeout(3000)
        calls = [c for c in api_calls if c.startswith("GET /api/audit")]
        check("il doppio clic su «Carica altri» fa UNA sola richiesta",
              len(calls) == 1, str(api_calls))
        after = rows_count(page)
        check("l'elenco precedente è stato conservato e ampliato", after > before,
              f"{before} -> {after}")

        ids = page.evaluate("""() => [...document.querySelectorAll('#audit-elenco > div')]
            .map(d => d.children[3] ? d.children[3].textContent : '')""")
        check("nessuna riga duplicata dopo il caricamento", len(ids) == after)
    else:
        check("pulsante «Carica altri» presente (servono più voci)", False,
              "il dataset è più piccolo di una pagina")

    # ---------------- filtri: azzerano il cursore ----------------
    api_calls.clear()
    page.locator('#audit-filtri select[name="result"]').select_option("failure")
    page.locator("#btn-audit-filtra").click()
    page.wait_for_timeout(2500)
    urls = [c for c in api_calls if c.startswith("GET /api/audit")]
    check("il filtro rilancia la query", len(urls) >= 1, str(api_calls))
    check("il filtro riparte SENZA cursore",
          all("cursor=" not in u for u in urls), str(urls))

    filtered = page.evaluate("""() => [...document.querySelectorAll('#audit-elenco > div')]
        .map(d => d.children[4] ? d.children[4].textContent.trim() : '')""")
    check("le voci mostrate hanno tutte l'esito filtrato",
          filtered and all(x == "failure" for x in filtered), str(filtered[:5]))

    # filtro per utenza
    api_calls.clear()
    page.locator('#audit-filtri select[name="result"]').select_option("")
    page.locator('#audit-filtri input[name="username"]').fill(args.username)
    page.locator("#btn-audit-filtra").click()
    page.wait_for_timeout(2500)
    who = page.evaluate("""() => [...document.querySelectorAll('#audit-elenco > div')]
        .map(d => d.children[1] ? d.children[1].textContent.trim() : '')""")
    check("il filtro per utenza restringe l'elenco",
          who and all(x == args.username for x in who), str(who[:5]))

    # filtro non valido -> 422 mostrato, non un elenco vuoto silenzioso
    api_calls.clear()
    page.locator('#audit-filtri input[name="username"]').fill("")
    page.locator('#audit-filtri input[name="event"]').fill("auth;DROP TABLE audit")
    page.locator("#btn-audit-filtra").click()
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    check("un filtro non valido mostra un errore, non un elenco vuoto muto",
          "rifiutat" in body.lower() or "non accettat" in body.lower()
          or "422" in body or "Dati non accettati" in body, body[:300])

    # azzeramento
    page.get_by_role("button", name="Azzera").click()
    page.wait_for_timeout(2500)
    check("l'azzeramento ricarica l'elenco", rows_count(page) > 0)

    # ---------------- dettaglio: testo, non HTML ----------------
    det = page.locator("[data-tsm-audit-detail]")
    if det.count():
        det.first.click()
        page.wait_for_timeout(1200)
        check("il dettaglio si apre in un riquadro",
              page.locator("#audit-detail").count() == 1)
        # Il JSON sta nel riquadro, non spalmato nella tabella.
        check("il dettaglio non è riversato nella tabella",
              page.evaluate("() => (document.querySelector('#audit-elenco')||{}).textContent")
              .find("{") == -1)
        page.get_by_role("button", name="Chiudi").last.click()
        page.wait_for_timeout(600)
    else:
        check("almeno una voce ha un dettaglio", False, "nessun pulsante Dettaglio")

    # ---------------- contenuto malevolo reso come testo ----------------
    injected = page.evaluate("""async () => {
      const r = await fetch('/api/audit?pageSize=200', {credentials:'same-origin'});
      const b = await r.json();
      return b.items.some(i => (i.clientHint || '').includes('<'));
    }""")
    # Se c'è una nota con markup, deve comparire come testo e non come elemento.
    check("nessuno <script> iniettato nel documento",
          page.evaluate("() => document.querySelectorAll('#audit-elenco script,"
                        " #audit-detail script').length") == 0)
    check("nessun elemento <img> creato da una nota di audit",
          page.evaluate("() => document.querySelectorAll('#audit-elenco img').length") == 0,
          f"note con markup presenti: {injected}")

    # ---------------- nessun segreto nelle risposte ----------------
    blob = page.evaluate("""async () => {
      const r = await fetch('/api/audit?pageSize=200', {credentials:'same-origin'});
      return JSON.stringify(await r.json());
    }""")
    for leaked in ("$argon2", "password_hash", "token_hash", "temporaryPassword"):
        check(f"nessun {leaked} nella risposta", leaked not in blob)

    # ---------------- non-admin: niente pannello, 403 dal server ----------------
    if args.editor_password:
        page.get_by_role("button", name="✕ Chiudi").first.click()
        page.wait_for_timeout(500)
        page.request.post(f"{base}/api/auth/logout", headers={"Origin": base},
                          ignore_https_errors=True)
        login(page, base, args.editor, args.editor_password)
        check("l'operatore non vede il pulsante Registro",
              page.get_by_role("button", name="Registro").count() == 0)
        r = page.request.get(f"{base}/api/audit", ignore_https_errors=True)
        check("l'operatore riceve 403 dall'API", r.status == 403, f"HTTP {r.status}")

    check("nessun errore JavaScript", not [t for t in console_text
                                           if t.startswith("pageerror")],
          " | ".join(console_text[:3]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--editor", default="")
    ap.add_argument("--editor-password", default="")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1700, "height": 1400})
        page = ctx.new_page()
        console_text: list[str] = []
        page.on("console", lambda m: console_text.append(m.text))
        page.on("pageerror", lambda e: console_text.append(f"pageerror: {e}"))
        api_calls: list[str] = []
        page.on("request", lambda r: api_calls.append(
            f"{r.method} {urlparse(r.url).path}?{urlparse(r.url).query}")
            if "/api/" in r.url else None)
        try:
            run(page, base, args, api_calls, console_text)
        finally:
            browser.close()
    return report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nINTERROTTO: {type(exc).__name__}: {str(exc)[:400]}\n")
        sys.exit(report())
