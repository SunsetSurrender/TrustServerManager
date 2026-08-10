#!/usr/bin/env python3
"""Schermata delle impostazioni, nel browser vero via nginx/TLS.

Copre i criteri del commit: lettura esclusiva da /api/settings, ETag conservato e
rimandato in `If-Match`, nessuno stato «salvato» prima della conferma, conflitto
che RICARICA invece di sovrascrivere, validazione lato client per comodità ma
autorità del server, nessun campo password, stato SMTP come sì/no, pulsanti
disabilitati durante le richieste, nessun invio di prova automatico dopo un
salvataggio, ed esiti di salvataggio e prova mostrati separatamente.

Il test CAMBIA le impostazioni salvate, quindi passa dalla guardia dei test
distruttivi: le impostazioni di notifica sono configurazione di produzione, e una
prova lanciata per sbaglio contro un'installazione vera potrebbe spegnere gli
avvisi o cambiare i destinatari.

Uso:
    python tools/settings-ui-test.py --base https://localhost --password <pw> \
        --allow-destructive
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import destructive_guard  # noqa: E402

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
    """Accede, oppure prosegue se la sessione è già aperta (vedi audit-ui-test)."""
    page.goto(f"{base}/", wait_until="load")
    page.wait_for_timeout(4500)
    campo = page.get_by_placeholder("utente")
    if campo.count() == 0:
        return
    campo.fill(username)
    page.get_by_placeholder("password").fill(password)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(4500)


def apri_impostazioni(page):
    page.get_by_role("button", name="Impostazioni").first.click()
    page.wait_for_timeout(2500)


def api(page, base, method, path, body=None, headers=None):
    """Chiamata diretta, con il barattolo di cookie del browser."""
    kw = {"headers": {"Content-Type": "application/json", "Origin": base,
                      **(headers or {})},
          "ignore_https_errors": True}
    if body is not None:
        kw["data"] = json.dumps(body)
    return getattr(page.request, method.lower())(f"{base}{path}", **kw)


def run(page, base, args, api_calls, console_text) -> None:
    login(page, base, args.username, args.password)
    me_resp = page.request.get(f"{base}/api/auth/me", ignore_https_errors=True)
    me = me_resp.json() if me_resp.status == 200 else {}
    if me.get("role") != "admin":
        check("PRECONDIZIONE: sessione amministrativa attiva", False,
              f"HTTP {me_resp.status} {me_resp.text()[:160]}")
        return
    check("PRECONDIZIONE: sessione amministrativa attiva", True)

    # ---------------- il pannello legge da /api/settings ----------------
    api_calls.clear()
    apri_impostazioni(page)
    check("le impostazioni arrivano da GET /api/settings",
          any(c.startswith("GET /api/settings") for c in api_calls),
          str(api_calls[:8]))
    check("la sezione notifiche è presente",
          page.locator("#settings-notifiche").count() == 1)
    check("i campi sono popolati dal server",
          page.locator("#set-timezone").input_value() != "",
          page.locator("#set-timezone").input_value())

    # ---------------- NESSUN campo password ----------------
    check("nessun campo di tipo password nella schermata",
          page.evaluate("() => document.querySelectorAll("
                        "'#settings-notifiche input[type=password]').length") == 0)
    check("nessun campo che chieda host o utenza SMTP",
          page.evaluate("""() => {
            const t = document.getElementById('settings-notifiche').innerHTML.toLowerCase();
            return !t.includes('smtp.azienda') && !t.includes('placeholder="host')
                && !t.includes('placeholder="utente') && !t.includes('starttls');
          }"""))
    smtp_txt = page.locator("#settings-smtp-status").inner_text()
    check("lo stato SMTP è un sì/no e nient'altro",
          smtp_txt.startswith("Credenziali SMTP configurate:")
          and ("sì" in smtp_txt or "no" in smtp_txt), smtp_txt)
    check("lo stato SMTP non cita host né percorsi",
          "/" not in smtp_txt.replace("l'invio", "") and "@" not in smtp_txt,
          smtp_txt)

    # ---------------- l'ETag viene conservato e rimandato ----------------
    etag = page.evaluate("""async () => {
      const r = await fetch('/api/settings', {credentials:'same-origin'});
      return r.headers.get('ETag');
    }""")
    check("la GET restituisce un ETag", bool(etag), str(etag))

    api_calls.clear()
    put_headers: list[str] = []
    page.on("request", lambda r: put_headers.append(
        r.header_value("if-match") or "(assente)")
        if r.method == "PUT" and "/api/settings" in r.url else None)

    page.locator("#set-recipients").fill("ced@example.internal\nnoc@example.internal")
    page.locator("#set-warning-days").fill("90, 7, 30")
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)

    check("il salvataggio invia PUT /api/settings",
          any(c.startswith("PUT /api/settings") for c in api_calls), str(api_calls))
    check("la PUT porta If-Match con l'ETag ricevuto",
          put_headers and put_headers[0] == etag, str(put_headers))

    # ---------------- «salvato» solo DOPO la conferma ----------------
    msg = page.locator("#settings-save-msg")
    check("l'esito del salvataggio è mostrato", msg.count() == 1)
    check("l'esito dice salvato con la nuova revisione",
          "salvat" in msg.inner_text().lower(), msg.inner_text())
    check("la revisione mostrata è aggiornata",
          "revisione" in page.locator("#settings-version").inner_text(),
          page.locator("#settings-version").inner_text())

    # canonicalizzazione del server, riletta nella schermata
    check("i giorni di preavviso tornano ordinati dal server",
          page.locator("#set-warning-days").input_value().replace(" ", "") == "7,30,90",
          page.locator("#set-warning-days").input_value())

    # ---------------- doppio clic su Salva: UNA sola richiesta ----------------
    page.locator("#set-warning-days").fill("15, 45")
    page.wait_for_timeout(400)
    api_calls.clear()
    page.evaluate("""() => {
      const b = document.getElementById('btn-settings-save');
      b.click(); b.click();
    }""")
    page.wait_for_timeout(3500)
    puts = [c for c in api_calls if c.startswith("PUT /api/settings")]
    check("il doppio clic su Salva fa UNA sola PUT", len(puts) == 1, str(api_calls))

    # ---------------- no-op: non si dice «salvato» a vanvera ----------------
    api_calls.clear()
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)
    check("un salvataggio senza modifiche lo dichiara",
          "nessuna modifica" in page.locator("#settings-save-msg").inner_text().lower(),
          page.locator("#settings-save-msg").inner_text())
    ver_prima = page.locator("#settings-version").inner_text()
    api_calls.clear()
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)
    check("la revisione non sale per un salvataggio a vuoto",
          page.locator("#settings-version").inner_text() == ver_prima,
          f"{ver_prima} -> {page.locator('#settings-version').inner_text()}")

    # ---------------- validazione lato client, per comodità ----------------
    page.locator("#set-recipients").fill("questo-non-e-un-indirizzo")
    api_calls.clear()
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(1500)
    check("un indirizzo evidentemente rotto è segnalato subito",
          "non valido" in page.locator("#settings-save-msg").inner_text().lower(),
          page.locator("#settings-save-msg").inner_text())
    check("e non viene nemmeno inviato al server",
          not [c for c in api_calls if c.startswith("PUT /api/settings")],
          str(api_calls))

    # ...ma l'autorità resta del server: un valore che il client non conosce
    # (un fuso inesistente) deve essere rifiutato DAL SERVER e mostrato.
    page.locator("#set-recipients").fill("ced@example.internal")
    page.locator("#set-timezone").fill("Europa/Roma")
    api_calls.clear()
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)
    check("un fuso inesistente arriva al server e torna rifiutato",
          [c for c in api_calls if c.startswith("PUT /api/settings")]
          and "fuso" in page.locator("#settings-save-msg").inner_text().lower(),
          page.locator("#settings-save-msg").inner_text() + " | " + str(api_calls))

    # si ripristina un fuso valido
    page.locator("#set-timezone").fill("Europe/Rome")
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)

    # ---------------- conflitto: si RICARICA, non si sovrascrive ----------------
    # Un altro amministratore salva alle nostre spalle (chiamata diretta all'API
    # con l'ETag corrente), poi si prova a salvare dalla schermata, che ha in mano
    # un ETag ormai vecchio.
    corrente = api(page, base, "GET", "/api/settings")
    etag_corrente = corrente.headers.get("etag")
    doc = corrente.json()["notifications"]
    altrui = dict(doc, recipients=["altro-amministratore@example.internal"])
    r = api(page, base, "PUT", "/api/settings", {"notifications": altrui},
            {"If-Match": etag_corrente})
    check("PRECONDIZIONE: la modifica «di un altro» è passata", r.status == 200,
          f"HTTP {r.status} {r.text()[:200]}")

    page.locator("#set-recipients").fill("mio-valore@example.internal")
    api_calls.clear()
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(4000)
    testo = page.locator("#settings-save-msg").inner_text().lower()
    check("il conflitto è comunicato all'utente",
          "modificat" in testo or "altro" in testo, testo)
    check("dopo il conflitto la schermata RICARICA dal server",
          any(c.startswith("GET /api/settings") for c in api_calls), str(api_calls))
    check("il valore dell'altro amministratore NON è stato sovrascritto",
          "altro-amministratore@example.internal"
          in page.locator("#set-recipients").input_value(),
          page.locator("#set-recipients").input_value())

    server_dopo = api(page, base, "GET", "/api/settings").json()
    check("e nemmeno sul server",
          server_dopo["notifications"]["recipients"]
          == ["altro-amministratore@example.internal"],
          str(server_dopo["notifications"]["recipients"]))

    # ---------------- invio di prova: separato, non automatico ----------------
    api_calls.clear()
    page.locator("#set-warning-days").fill("30")
    page.locator("#btn-settings-save").click()
    page.wait_for_timeout(3000)
    check("un salvataggio NON manda un invio di prova",
          not [c for c in api_calls if "notifications/test" in c], str(api_calls))

    api_calls.clear()
    page.evaluate("""() => {
      const b = document.getElementById('btn-settings-test');
      b.click(); b.click();
    }""")
    page.wait_for_timeout(4000)
    tests = [c for c in api_calls if "notifications/test" in c]
    check("il doppio clic su «Invia prova» fa UNA sola richiesta",
          len(tests) == 1, str(api_calls))
    check("l'esito della prova è in un'area SUA, distinta dal salvataggio",
          page.locator("#settings-test-msg").count() == 1,
          page.inner_text("#settings-notifiche")[:300])
    prova_txt = page.locator("#settings-test-msg").inner_text()
    check("l'esito della prova non contiene dettagli del server di posta",
          "relay" not in prova_txt.lower() and "587" not in prova_txt
          and "traceback" not in prova_txt.lower(), prova_txt)
    salva_txt = (page.locator("#settings-save-msg").inner_text()
                 if page.locator("#settings-save-msg").count() else "")
    check("l'esito della prova non ha sovrascritto quello del salvataggio",
          prova_txt != salva_txt, f"{prova_txt!r} == {salva_txt!r}")

    # ---------------- l'endpoint di prova non accetta parametri ----------------
    r = api(page, base, "POST", "/api/notifications/test",
            {"to": "estraneo@altrove.example"})
    check("l'API rifiuta un destinatario scelto dal chiamante",
          r.status == 422 and r.json()["detail"]["code"] == "unexpected_fields",
          f"HTTP {r.status} {r.text()[:200]}")

    # ---------------- nessun segreto nelle risposte ----------------
    blob = page.evaluate("""async () => {
      const r = await fetch('/api/settings', {credentials:'same-origin'});
      return JSON.stringify(await r.json());
    }""")
    for leaked in ("password", "secret", "postgresql", "/run/secrets", "tsm_api"):
        check(f"nessun «{leaked}» nella risposta delle impostazioni",
              leaked not in blob.lower(), blob[:200])

    # ---------------- il registro ha visto il cambio ----------------
    audit = page.evaluate("""async () => {
      const r = await fetch('/api/audit?event=settings', {credentials:'same-origin'});
      return await r.json();
    }""")
    check("il cambio di impostazioni è nel registro",
          any(i["event"] == "settings.updated" for i in audit["items"]),
          str(audit)[:200])
    check("il registro non contiene gli indirizzi dei destinatari",
          "example.internal" not in json.dumps(audit),
          "indirizzo trovato nel registro")

    # ---------------- non-admin: niente sezione, 403 dal server ----------------
    if args.editor_password:
        page.request.post(f"{base}/api/auth/logout", headers={"Origin": base},
                          ignore_https_errors=True)
        login(page, base, args.editor, args.editor_password)
        apri_impostazioni(page)
        check("l'operatore non vede la sezione notifiche",
              page.locator("#settings-notifiche").count() == 0)
        r = page.request.get(f"{base}/api/settings", ignore_https_errors=True)
        check("l'operatore riceve 403 da GET /api/settings", r.status == 403,
              f"HTTP {r.status}")
        r = api(page, base, "POST", "/api/notifications/test", {})
        check("l'operatore riceve 403 dall'invio di prova", r.status == 403,
              f"HTTP {r.status}")

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
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    # Il test riscrive le impostazioni di notifica: consenso esplicito (§8.37).
    destructive_guard.enforce(args, base)

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
