#!/usr/bin/env python3
"""Prova nel browser delle novità 0.4.0: indicatore di robustezza della password,
blocco dell'utenza dopo accessi falliti, sblocco da amministratore, soglia
impostabile, versione mostrata.

    python tools/lockout-e2e-test.py --base https://localhost --password <pw admin>

Crea un'utenza di prova (`e2e.blocco.<n>`), la blocca, la sblocca e la lascia
DISATTIVATA alla fine. Riporta la soglia al valore che aveva. Da usare su
un'installazione di prova, non su tsm-prd.
"""
from __future__ import annotations

import argparse
import random
import sys

from playwright.sync_api import sync_playwright

import destructive_guard

ESITI: list[tuple[str, bool, str]] = []


def check(nome: str, ok: bool, dettaglio: str = "") -> None:
    ESITI.append((nome, ok, dettaglio))
    print(f"  [{'PASS' if ok else 'FAIL'}] {nome}" + ("" if ok else f"\n         → {dettaglio}"))


def login(page, base, user, pw):
    page.goto(base + "/")
    page.get_by_placeholder("utente").fill(user)
    page.get_by_placeholder("password", exact=True).fill(pw)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(1200)


def testo(page) -> str:
    return page.evaluate("() => document.body.innerText")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--browser", default="chromium", choices=["chromium", "firefox"])
    destructive_guard.add_arguments(ap)
    a = ap.parse_args()
    destructive_guard.enforce(a, a.base)
    base = a.base.rstrip("/")
    utenza = f"e2e.blocco.{random.randint(1000, 9999)}"

    with sync_playwright() as p:
        browser = getattr(p, a.browser).launch(headless=True)
        adm = browser.new_context(ignore_https_errors=True).new_page()
        errori_js: list[str] = []
        adm.on("pageerror", lambda e: errori_js.append(str(e)))

        # ---- versione nella schermata di accesso ----
        adm.goto(base + "/")
        adm.wait_for_timeout(1500)
        # La versione attesa è quella che dichiara il server, non un numero scritto qui.
        ver = adm.request.get(base + "/api/health").json()["version"]
        check("versione mostrata nella schermata di accesso",
              f"versione {ver}" in testo(adm), testo(adm)[:200])

        login(adm, base, a.admin, a.password)
        check("versione mostrata nella barra", f"TSM {ver}" in testo(adm), testo(adm)[-300:])

        # ---- profilo: indicatore di robustezza ----
        adm.get_by_role("button", name="AD", exact=True).first.click()
        adm.wait_for_timeout(1000)
        campo = adm.get_by_placeholder("Nuova password (min 12)")
        check("segnaposto aggiornato a 12 caratteri", campo.count() == 1)
        for valore, atteso in [("corta", "Non sicura"), ("password1234", "Debole"),
                               ("1234567890123", "Debole"),
                               ("Gatto-Rosso-42", "Sicura"),
                               ("il gatto rosso dorme sul divano", "Sicura")]:
            campo.fill(valore)
            adm.wait_for_timeout(150)
            t = testo(adm)
            check(f"indicatore per «{valore}» → {atteso}", f"{atteso} —" in t,
                  [r for r in t.split("\n") if "—" in r and ("sicura" in r.lower() or "debole" in r.lower() or "accettabile" in r.lower())][:2].__str__())
        campo.fill("")

        # ---- soglia ----
        sez = adm.locator("#sicurezza-accessi")
        check("riquadro «Sicurezza accessi» presente", sez.count() == 1)
        soglia = sez.locator("input[type=number]").first
        iniziale = soglia.input_value()
        check("soglia caricata dal server", iniziale.isdigit() and 3 <= int(iniziale) <= 20, iniziale)
        soglia.fill("25")
        sez.get_by_role("button", name="Salva").click()
        adm.wait_for_timeout(500)
        check("soglia fuori intervallo rifiutata nel client", "fra 3 e 20" in sez.inner_text(), sez.inner_text())
        soglia.fill("3")
        sez.get_by_role("button", name="Salva").click()
        adm.wait_for_timeout(800)
        check("soglia 3 salvata", "Salvato." in sez.inner_text(), sez.inner_text())

        # ---- nuova utenza ----
        adm.get_by_role("button", name="+ Utenza").click()
        adm.get_by_placeholder("utenza (es. m.rossi)").fill(utenza)
        adm.locator("#btn-crea-utenza").click()
        adm.wait_for_timeout(1200)
        provv = adm.locator("#temp-password-value").inner_text().strip()
        check("utenza di prova creata", len(provv) >= 12, provv[:4] + "…")
        adm.get_by_role("button", name="Ho annotato la password — chiudi").click()
        adm.wait_for_timeout(300)

        # ---- blocco ----
        usr = browser.new_context(ignore_https_errors=True).new_page()
        for i in range(1, 4):
            login(usr, base, utenza, f"sbagliata-{i}-xxxxxxxx")
        t = testo(usr)
        check("al 3° errore: messaggio di utenza bloccata", "Utenza bloccata" in t, t[:300])
        login(usr, base, utenza, provv)
        t = testo(usr)
        check("bloccata anche con la password giusta", "Utenza bloccata" in t, t[:300])

        # ---- sblocco ----
        adm.locator("#btn-aggiorna-utenze").click()
        adm.wait_for_timeout(1200)
        riga = adm.locator("div", has_text=utenza).filter(has=adm.get_by_role("button", name="Sblocca"))
        check("stato «bloccata» e pulsante Sblocca visibili",
              riga.count() > 0 and "bloccata" in riga.last.inner_text(), riga.count().__str__())
        if riga.count():
            riga.last.get_by_role("button", name="Sblocca").click()
            adm.wait_for_timeout(1200)
        check("dopo lo sblocco il pulsante sparisce",
              adm.locator("div", has_text=utenza).filter(has=adm.get_by_role("button", name="Sblocca")).count() == 0)

        # ---- accesso dopo lo sblocco, cambio forzato con indicatore ----
        login(usr, base, utenza, provv)
        nuovo = usr.get_by_placeholder("nuova password (min. 12 caratteri)")
        check("dopo lo sblocco si entra (cambio password obbligatorio)", nuovo.count() == 1, testo(usr)[:200])
        if nuovo.count():
            # Dalla 0.4.1 la provvisoria non si ridigita dopo il login: il campo
            # compare solo se la pagina è stata ricaricata.
            if usr.get_by_placeholder("password attuale").count():
                usr.get_by_placeholder("password attuale").fill(provv)
            nuovo.fill("abc")
            usr.wait_for_timeout(150)
            check("indicatore anche nel cambio obbligatorio", "Non sicura —" in testo(usr))
            nuovo.fill("una frase di prova abbastanza lunga")
            usr.get_by_placeholder("ripeti la nuova password").fill("una frase di prova abbastanza lunga")
            usr.get_by_role("button", name="Cambia password").click()
            usr.wait_for_timeout(1200)
            login(usr, base, utenza, "una frase di prova abbastanza lunga")
            check("cambio password riuscito: si entra con la nuova",
                  usr.get_by_role("button", name="Esporta ▾").count() == 1, testo(usr)[:300])

        # ---- pulizia: soglia originale, utenza disattivata ----
        soglia.fill(iniziale)
        sez.get_by_role("button", name="Salva").click()
        adm.wait_for_timeout(800)
        r = adm.request.get(base + "/api/security-policy")
        check("soglia ripristinata", r.ok and r.json()["maxFailedLogins"] == int(iniziale), r.text()[:200])
        riga = adm.locator("div", has_text=utenza).filter(has=adm.get_by_role("button", name="Disattiva"))
        if riga.count():
            riga.last.get_by_role("button", name="Disattiva").click()
            adm.wait_for_timeout(1000)

        # ---- registro ----
        r = adm.request.get(base + "/api/audit?limit=50")
        corpo = r.text() if r.ok else ""
        for ev in ("users.locked", "users.unlocked", "security.policy.updated"):
            check(f"audit contiene {ev}", ev in corpo, str(r.status))
        check("nessun errore JavaScript", not errori_js, "; ".join(errori_js)[:300])
        browser.close()

    ko = [n for n, ok, _ in ESITI if not ok]
    print("=" * 76)
    print("RISULTATO:", "TUTTO OK" if not ko else f"{len(ko)} FALLIMENTI")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
