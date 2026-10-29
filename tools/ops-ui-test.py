#!/usr/bin/env python3
"""Prova nel browser delle novità 0.4.2 lato amministrazione:

- «Salva dati» nel Profilo salva davvero (nome, telefono, email); il team no;
- «Stato del sistema» compare per l'amministratore, con le voci principali;
- «Sicurezza accessi»: minuti di inattività salvati e validati;
- uscita automatica per inattività nel browser (orologio simulato).

Modifica i dati: solo su un'installazione di prova.

    python tools/ops-ui-test.py --password <pw admin> --allow-destructive
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

import destructive_guard
from ui_helpers import scheda

ESITI = []


def check(nome, ok, dettaglio=""):
    ESITI.append((nome, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {nome}" + ("" if ok else f"\n         → {dettaglio}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--browser", default="chromium", choices=["chromium", "firefox"])
    destructive_guard.add_arguments(ap)
    a = ap.parse_args()
    destructive_guard.enforce(a, a.base)
    base = a.base.rstrip("/")

    with sync_playwright() as p:
        browser = getattr(p, a.browser).launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1600})
        ctx.clock.install()
        page = ctx.new_page()
        errori = []
        page.on("pageerror", lambda e: errori.append(str(e)))

        page.goto(base + "/")
        page.wait_for_load_state("networkidle")
        page.get_by_placeholder("utente", exact=True).fill(a.username)
        page.get_by_placeholder("password", exact=True).fill(a.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(2500)
        page.get_by_role("button", name="AD", exact=True).first.click()
        page.wait_for_timeout(1500)

        # ---- profilo ----
        page.locator('input[name="nome"]').first.fill("Prova")
        page.locator('input[name="telefono"]').first.fill("06 555 0101")
        page.locator('input[name="email"][type="email"]').first.fill("prova.admin@example.it")
        check("il team non è modificabile dal Profilo",
              page.locator('input[name="team"]').first.is_disabled())
        page.get_by_role("button", name="Salva dati").click()
        page.wait_for_timeout(1200)
        check("«Salva dati» conferma", "Dati salvati." in page.inner_text("body"))
        me = page.request.get(base + "/api/auth/me").json()
        check("i dati sono sul server", (me.get("nome"), me.get("telefono"), me.get("email"))
              == ("Prova", "06 555 0101", "prova.admin@example.it"), me)

        # ---- stato del sistema ----
        scheda(page, "stato")
        stato = page.locator("#stato-sistema")
        page.wait_for_timeout(800)
        testo = stato.inner_text() if stato.count() else ""
        check("riquadro «Stato del sistema» presente", stato.count() == 1)
        for voce in ("Versione", "Migrazione", "Battito", "Ultimo dump", "Invio email (SMTP)",
                     "Certificato TLS", "Attive"):
            check(f"stato: voce «{voce}»", voce in testo, testo[:400])

        # ---- inattività: impostazione ----
        scheda(page, "sicurezza")
        sez = page.locator("#sicurezza-accessi")
        idle = sez.locator('input[type="number"]').nth(1)
        idle.fill("10")
        sez.get_by_role("button", name="Salva").click()
        page.wait_for_timeout(600)
        check("10 minuti rifiutati (minimo 15)", "15 e 720" in sez.inner_text(), sez.inner_text())
        idle.fill("15")
        sez.get_by_role("button", name="Salva").click()
        page.wait_for_timeout(1200)
        pol = page.request.get(base + "/api/security-policy").json()
        check("15 minuti salvati", pol.get("idleTimeoutMinutes") == 15, pol)

        # ---- uscita automatica ----
        # La soglia arriva al client con la sessione: si ricarica per leggerla.
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        ctx.clock.fast_forward("16:00")
        page.wait_for_timeout(2500)
        corpo = page.inner_text("body")
        check("dopo 16 minuti senza attività si torna al login",
              page.get_by_role("button", name="Accedi").count() == 1, corpo[:300])
        check("con il motivo", "inattività" in corpo, corpo[:300])

        # ---- ripristino ----
        page.get_by_placeholder("utente", exact=True).fill(a.username)
        page.get_by_placeholder("password", exact=True).fill(a.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(2000)
        r = page.request.put(base + "/api/security-policy", data={"idleTimeoutMinutes": 60},
                             headers={"Origin": base})
        check("soglia riportata a 60", r.ok and r.json()["idleTimeoutMinutes"] == 60, r.text()[:200])
        check("nessun errore JavaScript", not errori, "; ".join(errori)[:300])
        browser.close()

    ko = [n for n, ok in ESITI if not ok]
    print("=" * 76)
    print("RISULTATO:", "TUTTO OK" if not ko else f"{len(ko)} FALLIMENTI")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
