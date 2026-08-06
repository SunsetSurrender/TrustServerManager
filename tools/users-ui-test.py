#!/usr/bin/env python3
"""Interfaccia di amministrazione delle utenze, nel browser vero via nginx/TLS.

Copre i criteri del commit 1: creazione, modifica, disattivazione, riattivazione,
reimpostazione; nessun DELETE; password provvisoria mostrata una volta e poi
cancellata; politiche del server rispettate e non duplicate in JavaScript;
autorità riletta dopo un'autoretrocessione; nessuna traccia della password in
storage, console, export o risposte successive.

Prerequisiti: stack in piedi con TLS, inventario inizializzato, e un'utenza
amministrativa che NON richieda il cambio password.

Uso:
    python tools/users-ui-test.py --base https://localhost --password <pw>
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

NUOVO = "e2e-utente"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, bool(passed), detail))


def report() -> int:
    """Stampa gli esiti. Si chiama SEMPRE, anche se il test si interrompe: senza,
    un'eccezione a metà nasconde tutto quello che era già stato verificato."""
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


def users_of(page, base) -> list[dict]:
    r = page.request.get(f"{base}/api/users?includeDisabled=true",
                         ignore_https_errors=True)
    return r.json() if r.status == 200 else []


def user_id(page, base, username: str) -> str:
    return next((u["id"] for u in users_of(page, base)
                 if u["username"] == username), "")


def open_profile(page) -> None:
    """Apre il pannello Profilo, dove vive la gestione utenze."""
    page.locator('button[title*="—"]').first.click()
    page.wait_for_timeout(1500)


def temp_password_shown(page) -> str:
    """Il valore mostrato nel riquadro, preso dal suo elemento.

    Prima si cercava un div che «somigliasse» a una password: un'euristica che
    pesca anche gli UUID, che hanno la stessa forma. Con un id dedicato il test
    legge quello che intende leggere.
    """
    return page.evaluate("""() => {
      const el = document.getElementById('temp-password-value');
      return el ? el.textContent.trim() : '';
    }""")


def run(page, ctx, base, args, api_calls, api_bodies, console_text) -> None:
    # ---------------- accesso ----------------
    page.goto(f"{base}/", wait_until="load")
    page.wait_for_timeout(4500)
    page.get_by_placeholder("utente").fill(args.username)
    page.get_by_placeholder("password").fill(args.password)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(4500)
    check("accesso come amministratore",
          page.get_by_role("button", name="Esporta ▾").is_visible())

    # PRECONDIZIONE: il test finisce retrocedendo l'amministratore (è uno dei casi
    # da verificare), quindi NON è ripetibile su uno stato già usato. Si controlla
    # subito e si dice cosa fare, invece di far fallire controlli successivi con
    # messaggi che non spiegano la causa.
    me = page.request.get(f"{base}/api/auth/me", ignore_https_errors=True).json()
    if me.get("role") != "admin":
        check("PRECONDIZIONE: l'utenza di prova ha ruolo admin", False,
              f"ruolo attuale: {me.get('role')!r}. Il test retrocede l'amministratore "
              f"alla fine: rieseguire `tools/run-users-ui-test.ps1`, che ripristina "
              f"lo stato prima di partire.")
        return
    check("PRECONDIZIONE: l'utenza di prova ha ruolo admin", True)

    # ---------------- l'elenco viene da /api/users ----------------
    api_calls.clear()
    open_profile(page)
    check("l'elenco arriva da GET /api/users",
          any(c.startswith("GET /api/users") for c in api_calls), str(api_calls[:8]))
    check("il pannello utenze è visibile",
          page.locator("text=UTENTI E PRIVILEGI").count() > 0)

    # ---------------- nessuna cancellazione ----------------
    body = page.inner_text("body")
    for forbidden in ("Elimina", "Rimuovi", "Cancella"):
        check(f"nessun controllo «{forbidden}»", forbidden not in body)
    check("nessuna chiamata DELETE",
          not [c for c in api_calls if c.startswith("DELETE ")], str(api_calls))

    # ---------------- creazione (con doppio clic) ----------------
    page.get_by_role("button", name="+ Utenza").click()
    page.wait_for_selector("#form-nuova-utenza", timeout=15000)
    # Selettori SCOPED al form: il pannello Profilo ha campi con gli stessi `name`,
    # quindi `input[name="nome"]` da solo pescherebbe quelli.
    f = page.locator("#form-nuova-utenza")
    f.locator('input[name="username"]').fill(NUOVO)
    f.locator('input[name="nome"]').fill("Prova")
    f.locator('input[name="cognome"]').fill("Utente")
    f.locator('select[name="role"]').select_option("edit")

    api_calls.clear()
    f.get_by_role("button", name="Crea utenza").wait_for(state="visible", timeout=15000)
    # I due clic devono avvenire nello STESSO task del browser: la creazione
    # riuscita chiude il form, quindi due `dispatch_event` separati vedrebbero il
    # secondo pulsante già scomparso e il test non proverebbe nulla. Un doppio clic
    # vero è questo: due eventi prima che parta qualsiasi risposta.
    page.evaluate("""() => {
      const b = document.getElementById('btn-crea-utenza');
      b.click(); b.click();
    }""")
    page.wait_for_timeout(4500)

    posts = [c for c in api_calls if c == "POST /api/users"]
    check("il doppio clic produce UNA sola creazione", len(posts) == 1, str(api_calls))
    check("l'elenco è ricaricato dopo la creazione",
          any(c.startswith("GET /api/users") for c in api_calls), str(api_calls))

    created = next((u for u in users_of(page, base) if u["username"] == NUOVO), None)
    check("l'utenza creata esiste con UUID", bool(created and created.get("id")))
    check("i campi di profilo sono stati inviati",
          bool(created and created.get("nome") == "Prova"
               and created.get("cognome") == "Utente"),
          json.dumps(created or {})[:200])
    check("l'utenza creata deve cambiare password al primo accesso",
          bool(created and created.get("mustChangePassword")))

    # ---------------- password provvisoria ----------------
    copia = page.get_by_role("button", name="Copia")
    check("il riquadro della password provvisoria è aperto", copia.count() == 1)
    temp_pw = temp_password_shown(page)
    check("la password provvisoria è mostrata", len(temp_pw) >= 12, repr(temp_pw))

    if copia.count():
        copia.click()
        page.wait_for_timeout(700)
        check("la copia è confermata",
              page.locator("text=Copiata negli appunti").count() > 0)

    storage = page.evaluate("""() => ({
      localStorage: JSON.stringify(Object.entries(localStorage)),
      sessionStorage: JSON.stringify(Object.entries(sessionStorage)),
      url: location.href,
    })""")
    for where, blob in storage.items():
        check(f"la password non è in {where}", temp_pw and temp_pw not in blob, where)
    check("la password non compare nell'output di console",
          temp_pw and not any(temp_pw in t for t in console_text))

    # chiusura -> il valore scompare
    page.get_by_role("button", name="Ho annotato la password — chiudi").click()
    page.wait_for_timeout(900)
    check("chiudendo il riquadro la password scompare dal testo",
          temp_pw and temp_pw not in page.inner_text("body"))
    check("la password non è più nel DOM",
          temp_pw and temp_pw not in page.evaluate("() => document.body.innerHTML"))

    # ---------------- utenza duplicata: conflitto del server ----------------
    page.get_by_role("button", name="+ Utenza").click()
    page.wait_for_selector("#form-nuova-utenza", timeout=10000)
    f = page.locator("#form-nuova-utenza")
    f.locator('input[name="username"]').fill(NUOVO)
    f.get_by_role("button", name="Crea utenza").dispatch_event("click")
    page.wait_for_timeout(2800)
    body = page.inner_text("body")
    check("l'utenza duplicata mostra il conflitto riportato dal server",
          "esiste già" in body.lower(), body[:300])
    page.get_by_role("button", name="+ Utenza").click()      # chiude il form
    page.wait_for_timeout(500)

    # ---------------- disattivazione / riattivazione ----------------
    api_calls.clear()
    dis = page.get_by_role("button", name="Disattiva")
    check("esiste un'azione di disattivazione", dis.count() > 0)
    if dis.count():
        dis.last.dispatch_event("click")
        page.wait_for_timeout(2800)
        check("la disattivazione passa da POST .../disable",
              any("/disable" in c for c in api_calls), str(api_calls))
        check("l'elenco è ricaricato dopo la disattivazione",
              any(c.startswith("GET /api/users") for c in api_calls), str(api_calls))
        body = page.inner_text("body")
        check("l'utenza disattivata resta VISIBILE con il suo stato",
              NUOVO in body and "disattivata" in body, body[:300])
        check("l'utenza disattivata offre la riattivazione",
              page.get_by_role("button", name="Riattiva").count() > 0)

        api_calls.clear()
        page.get_by_role("button", name="Riattiva").last.dispatch_event("click")
        page.wait_for_timeout(2800)
        check("la riattivazione passa da POST .../enable",
              any("/enable" in c for c in api_calls), str(api_calls))
        check("dopo la riattivazione l'utenza è attiva",
              any(u["username"] == NUOVO and not u["disabled"]
                  for u in users_of(page, base)))

    # ---------------- modifica di ruolo e profilo ----------------
    api_calls.clear()
    page.get_by_role("button", name="Modifica").last.click()
    page.wait_for_selector("#form-modifica-utenza", timeout=10000)
    m = page.locator("#form-modifica-utenza")
    m.locator('input[name="telefono"]').fill("06-1234567")
    m.locator('select[name="role"]').select_option("view")
    m.get_by_role("button", name="Salva").dispatch_event("click")
    page.wait_for_timeout(2800)
    check("la modifica passa da PATCH /api/users/{id}",
          any(c.startswith("PATCH /api/users/") for c in api_calls), str(api_calls))
    edited = next((u for u in users_of(page, base) if u["username"] == NUOVO), {})
    check("il ruolo è stato cambiato", edited.get("role") == "view",
          json.dumps(edited)[:200])
    check("il profilo è stato aggiornato", edited.get("telefono") == "06-1234567")
    check("i campi non inviati non sono stati azzerati",
          edited.get("nome") == "Prova", json.dumps(edited)[:200])

    # ---------------- reimpostazione password (doppio clic) ----------------
    api_calls.clear()
    # Anche qui i due clic nello stesso task, sull'ultima riga.
    page.evaluate("""() => {
      const bs = [...document.querySelectorAll('[data-tsm-reset]')];
      const b = bs[bs.length - 1];
      b.click(); b.click();
    }""")
    page.wait_for_timeout(3200)
    resets = [c for c in api_calls if "/reset-password" in c]
    check("il doppio clic produce UNA sola reimpostazione", len(resets) == 1,
          str(api_calls))
    temp2 = temp_password_shown(page)
    check("il riquadro mostra la nuova password provvisoria", len(temp2) >= 12,
          repr(temp2))
    check("la password reimpostata è diversa dalla prima", temp2 != temp_pw)
    page.get_by_role("button", name="Ho annotato la password — chiudi").click()
    page.wait_for_timeout(700)

    # ---------------- ultimo amministratore: politica del server ----------------
    admin_id = user_id(page, base, args.username)
    r = page.request.patch(f"{base}/api/users/{admin_id}",
                          data=json.dumps({"role": "edit"}),
                          headers={"Content-Type": "application/json",
                                   "Origin": base},
                          ignore_https_errors=True)
    check("il server rifiuta di retrocedere l'ultimo amministratore",
          r.status == 409 and "last_admin_protected" in r.text(),
          f"HTTP {r.status}: {r.text()[:200]}")

    r = page.request.post(f"{base}/api/users/{admin_id}/disable",
                          headers={"Origin": base}, ignore_https_errors=True)
    check("il server rifiuta di disattivare l'ultimo amministratore / sé stessi",
          r.status in (409, 422), f"HTTP {r.status}: {r.text()[:200]}")

    # ---------------- autoretrocessione: autorità riletta dal server ----------------
    new_id = user_id(page, base, NUOVO)
    r = page.request.patch(f"{base}/api/users/{new_id}",
                          data=json.dumps({"role": "admin"}),
                          headers={"Content-Type": "application/json",
                                   "Origin": base},
                          ignore_https_errors=True)
    check("promosso un secondo amministratore", r.status == 200, f"HTTP {r.status}")

    page.reload(wait_until="load")
    page.wait_for_timeout(4500)
    open_profile(page)
    api_calls.clear()
    # `admin` è la prima riga (ordinamento per username)
    page.get_by_role("button", name="Modifica").first.click()
    page.wait_for_selector("#form-modifica-utenza", timeout=10000)
    m = page.locator("#form-modifica-utenza")
    check("si sta modificando la propria utenza",
          args.username in m.inner_text(), m.inner_text()[:120])
    m.locator('select[name="role"]').select_option("edit")
    m.get_by_role("button", name="Salva").dispatch_event("click")
    page.wait_for_timeout(3500)

    check("dopo l'autoretrocessione si richiede /api/auth/me",
          any(c == "GET /api/auth/me" for c in api_calls), str(api_calls[:10]))
    body = page.inner_text("body")
    check("il pannello utenze è chiuso dopo la perdita dei privilegi",
          "UTENTI E PRIVILEGI" not in body, body[:200])
    check("l'utente è informato del cambio di ruolo",
          "ruolo è cambiato" in body, body[:300])

    # ---------------- non-admin: rotte rifiutate ----------------
    r = page.request.get(f"{base}/api/users", ignore_https_errors=True)
    check("un non-amministratore non può elencare le utenze", r.status == 403,
          f"HTTP {r.status}")
    r = page.request.post(f"{base}/api/users",
                          data=json.dumps({"username": "furtivo", "role": "admin"}),
                          headers={"Content-Type": "application/json",
                                   "Origin": base},
                          ignore_https_errors=True)
    check("un non-amministratore non può creare utenze", r.status == 403,
          f"HTTP {r.status}")

    # Header e parametri falsificati: il ruolo lo rilegge il server dalla sessione.
    r = page.request.get(f"{base}/api/users",
                         headers={"X-Role": "admin", "X-User-Role": "admin",
                                  "Authorization": "Bearer admin"},
                         ignore_https_errors=True)
    check("header di ruolo falsificati restano inefficaci", r.status == 403,
          f"HTTP {r.status}")
    r = page.request.get(f"{base}/api/users?role=admin&admin=1&includeDisabled=true",
                         ignore_https_errors=True)
    check("parametri di ruolo falsificati restano inefficaci", r.status == 403,
          f"HTTP {r.status}")

    # ---------------- nessuna password nelle risposte / nell'export ----------------
    joined = " ".join(api_bodies)
    for pw in (temp_pw, temp2):
        if pw:
            check("la password provvisoria non ricompare nelle risposte API",
                  joined.count(pw) <= 1, f"{joined.count(pw)} occorrenze")
    check("nessun hash di password nelle risposte API",
          "$argon2" not in joined and "password_hash" not in joined)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1600, "height": 2400},
                                  permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()

        console_text: list[str] = []
        page.on("console", lambda m: console_text.append(m.text))
        page.on("pageerror", lambda e: console_text.append(f"pageerror: {e}"))

        api_calls: list[str] = []
        page.on("request", lambda r: api_calls.append(
            f"{r.method} {urlparse(r.url).path}") if "/api/" in r.url else None)

        api_bodies: list[str] = []

        def collect(resp):
            if "/api/" in resp.url and resp.status < 400:
                try:
                    api_bodies.append(resp.text())
                except Exception:
                    pass

        page.on("response", collect)

        try:
            run(page, ctx, base, args, api_calls, api_bodies, console_text)
        finally:
            browser.close()

    return report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nINTERROTTO: {type(exc).__name__}: {str(exc)[:400]}\n")
        sys.exit(report())
