#!/usr/bin/env python3
"""Prova nel browser delle novità 0.4.1:

- cambio obbligatorio senza ridigitare la provvisoria, con conferma della nuova;
- provvisorie generate facili da ridigitare (6 gruppi di 5);
- reimpostazione dall'admin con password scelta;
- email sulle utenze;
- recupero password dalla schermata di accesso (codice per email e link);
- interruttore del recupero in «Sicurezza accessi».

Serve un relay SMTP finto che scriva i messaggi in una cartella (`--posta`), da
cui la prova legge il codice. Da usare su un'installazione di prova, mai su
tsm-prd.

    python tools/recovery-e2e-test.py --password <pw admin> --posta <dir> --allow-destructive
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

import destructive_guard

ESITI: list[tuple[str, bool, str]] = []


def check(nome, ok, dettaglio=""):
    ESITI.append((nome, bool(ok), dettaglio))
    print(f"  [{'PASS' if ok else 'FAIL'}] {nome}" + ("" if ok else f"\n         → {dettaglio}"))


def testo(page):
    return page.evaluate("() => document.body.innerText")


def login(page, base, user, pw):
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    page.get_by_placeholder("utente").first.fill(user)
    page.get_by_placeholder("password", exact=True).fill(pw)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(1500)


def ultimo_codice(posta, dopo):
    for _ in range(30):
        files = sorted(f for f in glob.glob(os.path.join(posta, "*.eml"))
                       if os.path.getmtime(f) > dopo)
        if files:
            corpo = open(files[-1], encoding="utf-8", errors="replace").read()
            m = re.search(r"Codice:\s+([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})", corpo)
            if m:
                return m.group(1), corpo
        time.sleep(0.5)
    return None, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--posta", required=True, help="cartella dove il relay finto scrive le mail")
    ap.add_argument("--browser", default="chromium", choices=["chromium", "firefox"])
    destructive_guard.add_arguments(ap)
    a = ap.parse_args()
    destructive_guard.enforce(a, a.base)
    base = a.base.rstrip("/")
    utenza = f"e2e.recupero.{random.randint(1000, 9999)}"

    with sync_playwright() as p:
        browser = getattr(p, a.browser).launch(headless=True)
        errori: list[str] = []
        adm = browser.new_context(ignore_https_errors=True).new_page()
        adm.on("pageerror", lambda e: errori.append(f"admin: {e}"))

        # ---- admin: utenza con email, provvisoria ridigitabile ----
        login(adm, base, a.admin, a.password)
        adm.get_by_role("button", name="AD", exact=True).first.click()
        adm.wait_for_timeout(1200)
        adm.get_by_role("button", name="+ Utenza").click()
        adm.get_by_placeholder("utenza (es. m.rossi)").fill(utenza)
        adm.get_by_placeholder("email (per il recupero password)").first.fill(f"{utenza}@example.it")
        adm.locator("#btn-crea-utenza").click()
        adm.wait_for_timeout(1500)
        provv = adm.locator("#temp-password-value").inner_text().strip()
        check("provvisoria in 6 gruppi ridigitabili",
              re.fullmatch(r"[a-z2-9]{5}(-[a-z2-9]{5}){5}", provv), provv)
        adm.get_by_role("button", name="Ho annotato la password — chiudi").click()
        adm.wait_for_timeout(300)
        check("email mostrata nella riga dell'utenza", f"{utenza}@example.it" in testo(adm))

        # ---- primo accesso: niente «password attuale» da ridigitare ----
        usr = browser.new_context(ignore_https_errors=True).new_page()
        usr.on("pageerror", lambda e: errori.append(f"utente: {e}"))
        login(usr, base, utenza, provv)
        check("cambio obbligatorio senza chiedere di nuovo la provvisoria",
              usr.get_by_placeholder("password attuale (quella provvisoria)").count() == 0,
              testo(usr)[:300])
        usr.get_by_placeholder("nuova password (min. 12 caratteri)").fill("una frase per la prova")
        usr.get_by_placeholder("ripeti la nuova password").fill("una frase diversa davvero")
        usr.get_by_role("button", name="Cambia password").click()
        usr.wait_for_timeout(500)
        check("conferma diversa: rifiutata nel browser", "non coincidono" in testo(usr))
        usr.get_by_placeholder("ripeti la nuova password").fill("una frase per la prova")
        usr.get_by_role("button", name="Cambia password").click()
        usr.wait_for_timeout(1500)
        login(usr, base, utenza, "una frase per la prova")
        check("con la nuova password si entra", usr.get_by_role("button", name="Esporta ▾").count() == 1,
              testo(usr)[:300])

        # ---- admin: reimposta con password scelta ----
        adm.locator("#btn-aggiorna-utenze").click()
        adm.wait_for_timeout(1200)
        riga = adm.locator("div", has_text=utenza).filter(
            has=adm.get_by_role("button", name="Reimposta password"))
        riga.last.get_by_role("button", name="Reimposta password").click()
        adm.wait_for_timeout(400)
        f = adm.locator("#form-reimposta-password")
        check("finestra di reimpostazione aperta", f.count() == 1)
        f.get_by_role("button", name="Scegli tu la password").click()
        f.get_by_placeholder("nuova password (min. 12 caratteri)").fill("scelta dall amministratore")
        adm.wait_for_timeout(150)
        check("indicatore anche per la password scelta", "—" in f.inner_text())
        f.get_by_placeholder("ripeti la password").fill("scelta dall amministratore")
        f.get_by_role("button", name="Reimposta", exact=True).click()
        adm.wait_for_timeout(1500)
        check("conferma della password impostata", "impostata" in testo(adm), testo(adm)[-400:])
        login(usr, base, utenza, "scelta dall amministratore")
        check("la password scelta funziona e chiede il cambio",
              usr.get_by_role("button", name="Cambia password").count() == 1, testo(usr)[:300])
        usr.get_by_placeholder("nuova password (min. 12 caratteri)").fill("terza frase per la prova")
        usr.get_by_placeholder("ripeti la nuova password").fill("terza frase per la prova")
        usr.get_by_role("button", name="Cambia password").click()
        usr.wait_for_timeout(1500)

        # ---- recupero dalla schermata di accesso ----
        usr.goto(base + "/")
        usr.wait_for_load_state("networkidle")
        link = usr.get_by_role("button", name="Password dimenticata?")
        check("link «Password dimenticata?» sulla schermata di accesso", link.count() == 1)
        link.click()
        usr.get_by_placeholder("utente").first.fill(utenza)
        istante = time.time()
        usr.get_by_role("button", name="Invia il codice").click()
        usr.wait_for_timeout(1500)
        check("messaggio generico dopo la richiesta", "Se l'utenza esiste" in testo(usr), testo(usr)[:300])
        code, mail = ultimo_codice(a.posta, istante)
        check("mail con il codice arrivata al relay", code is not None, mail[:300])
        check("la mail contiene il link con utenza e codice", "#reset?u=" in mail)
        check("la mail non contiene password", "terza frase" not in mail)
        if code:
            usr.get_by_placeholder("codice (es. ABCD-EFGH-JKMN)").fill(code.lower())
            usr.get_by_placeholder("nuova password (min. 12 caratteri)").fill("recuperata con il codice")
            usr.get_by_placeholder("ripeti la nuova password").fill("recuperata con il codice")
            usr.get_by_role("button", name="Imposta la password").click()
            usr.wait_for_timeout(1500)
            check("password impostata con il codice", "Password impostata" in testo(usr), testo(usr)[:300])
            login(usr, base, utenza, "recuperata con il codice")
            check("si entra con la password recuperata",
                  usr.get_by_role("button", name="Esporta ▾").count() == 1, testo(usr)[:300])

        # ---- il link della mail compila il modulo e sparisce dalla barra ----
        ctx2 = browser.new_context(ignore_https_errors=True).new_page()
        ctx2.goto(base + f"/#reset?u={utenza}&c=ABCD-EFGH-JKMN")
        ctx2.wait_for_load_state("networkidle")
        ctx2.wait_for_timeout(800)
        check("il link apre il modulo del codice già compilato",
              ctx2.get_by_placeholder("codice (es. ABCD-EFGH-JKMN)").input_value() == "ABCD-EFGH-JKMN")
        check("il codice non resta nella barra degli indirizzi", "#reset" not in ctx2.url, ctx2.url)
        ctx2.get_by_placeholder("nuova password (min. 12 caratteri)").fill("una password qualsiasi")
        ctx2.get_by_placeholder("ripeti la nuova password").fill("una password qualsiasi")
        ctx2.get_by_role("button", name="Imposta la password").click()
        ctx2.wait_for_timeout(1200)
        check("codice sbagliato: messaggio chiaro", "Codice" in testo(ctx2) or "codice" in testo(ctx2),
              testo(ctx2)[:300])

        # ---- interruttore ----
        adm.locator("#btn-aggiorna-utenze").click()
        adm.wait_for_timeout(1200)
        sez = adm.locator("#sicurezza-accessi")
        sez.get_by_role("button", name="attivo", exact=True).click()
        adm.wait_for_timeout(1000)
        check("recupero disattivato dall'interfaccia",
              sez.get_by_role("button", name="disattivato").count() == 1, sez.inner_text())
        ctx2.goto(base + "/")
        ctx2.wait_for_load_state("networkidle")
        check("a recupero spento il link non compare",
              ctx2.get_by_role("button", name="Password dimenticata?").count() == 0)
        sez.get_by_role("button", name="disattivato").click()
        adm.wait_for_timeout(1000)

        # ---- pulizia ----
        riga = adm.locator("div", has_text=utenza).filter(has=adm.get_by_role("button", name="Disattiva"))
        if riga.count():
            riga.last.get_by_role("button", name="Disattiva").click()
            adm.wait_for_timeout(800)
        check("nessun errore JavaScript", not errori, "; ".join(errori)[:400])
        browser.close()

    ko = [n for n, ok, _ in ESITI if not ok]
    print("=" * 76)
    print("RISULTATO:", "TUTTO OK" if not ko else f"{len(ko)} FALLIMENTI")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
