#!/usr/bin/env python3
"""Prova nel browser dell'import dei seriali dei rack (0.4.2).

1. In Editing, Impostazioni → «Scarica foglio Seriali rack»: si scarica il foglio.
2. Lo si compila con openpyxl: un rack con due seriali, uno con uno, uno con
   «NESSUNO», una riga con un _uid inesistente, una con il seriale 2 senza l'1.
3. Si reimporta: l'anteprima conta 2 da aggiornare e 2 righe da saltare (il rack
   con NESSUNO non ha seriali, quindi resta «invariato»).
4. «Applica import» salva; l'inventario del server ha i seriali nuovi.

Modifica i dati: solo su un'installazione di prova.

    python tools/rack-serials-ui-test.py --password <pw admin> --allow-destructive
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
from pathlib import Path

from openpyxl import load_workbook
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
    tmp = Path(tempfile.mkdtemp(prefix="tsm-seriali-"))

    with sync_playwright() as p:
        browser = getattr(p, a.browser).launch(headless=True)
        page = browser.new_context(ignore_https_errors=True, accept_downloads=True,
                                   viewport={"width": 1600, "height": 2400}).new_page()
        errori, dialoghi = [], []
        page.on("pageerror", lambda e: errori.append(str(e)))
        page.on("dialog", lambda d: (dialoghi.append(d.message), d.accept()))

        page.goto(base + "/")
        page.wait_for_load_state("networkidle")
        page.get_by_placeholder("utente", exact=True).fill(a.username)
        page.get_by_placeholder("password", exact=True).fill(a.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(3000)
        page.get_by_role("button", name="Editing", exact=True).dispatch_event("click")
        page.wait_for_timeout(800)
        scheda(page, "importa")

        # ---- 1. scarico ----
        with page.expect_download(timeout=30000) as dl:
            page.get_by_role("button", name="↓ Scarica foglio «Seriali rack» da compilare").dispatch_event("click")
        foglio = tmp / "seriali-rack.xlsx"
        dl.value.save_as(foglio)
        wb = load_workbook(foglio)
        check("il file ha il foglio «Seriali rack»", "Seriali rack" in wb.sheetnames, wb.sheetnames)
        ws = wb["Seriali rack"]
        hdr = [str(c.value).strip().lower() for c in ws[1]]
        col = {h: i + 1 for i, h in enumerate(hdr)}
        righe = ws.max_row - 1
        check("una riga per rack", righe > 3, f"{righe} righe")

        # ---- 2. compilo ----
        # Seriali nuovi a ogni giro: la prova si può ripetere sugli stessi dati.
        n = random.randint(1000, 9999)
        A1, A2, B1 = f"SN-E2E-{n}-A1", f"SN-E2E-{n}-A2", f"SN-E2E-{n}-B1"
        uid = [ws.cell(r, col["_uid"]).value for r in range(2, 6)]
        terzo_ha_seriali = bool(str(ws.cell(4, col["seriali attuali"]).value or "").strip())
        attesi = 2 + (1 if terzo_ha_seriali else 0)
        ws.cell(2, col["seriale effettivo 1"], A1)
        ws.cell(2, col["seriale effettivo 2"], A2)
        ws.cell(3, col["seriale effettivo 1"], B1)
        ws.cell(4, col["seriale effettivo 1"], "NESSUNO")
        ws.cell(5, col["seriale effettivo 2"], "SOLO-IL-2")        # da saltare
        nuova = ws.max_row + 1
        ws.cell(nuova, col["_uid"], "00000000-0000-0000-0000-000000000000")
        ws.cell(nuova, col["seriale effettivo 1"], "FANTASMA")      # da saltare
        compilato = tmp / "seriali-compilati.xlsx"
        wb.save(compilato)

        # ---- 3. anteprima ----
        inp = page.locator("label", has_text="Importa seriali rack").locator('input[type="file"]')
        check("pulsante di import dei seriali presente", inp.count() == 1, inp.count())
        inp.set_input_files(str(compilato))
        page.wait_for_timeout(2000)
        corpo = page.inner_text("body")
        check(f"anteprima: {attesi} rack da aggiornare", f"{attesi} rack da aggiornare" in corpo, corpo[-800:])
        check("anteprima: 2 righe da saltare", "2 righe da saltare" in corpo, corpo[-800:])
        check("anteprima: motivo del seriale 2 senza l'1", "seriale 2 ma non l'1" in corpo)
        check("anteprima: rack inesistente segnalato", "rack non trovato" in corpo)

        # ---- 4. applico ----
        page.get_by_role("button", name="Applica import").dispatch_event("click")
        page.wait_for_timeout(3000)
        inv = page.request.get(base + "/api/inventory").json()
        doc = inv.get("doc", inv)
        per_uid = {}
        for L in doc.get("locations", []):
            for S in L.get("sale", []):
                for R in S.get("racks", []):
                    per_uid[R.get("_uid")] = R.get("seriali") or []
        check("rack 1: due seriali salvati", per_uid.get(uid[0]) == [A1, A2],
              per_uid.get(uid[0]))
        check("rack 2: un seriale salvato", per_uid.get(uid[1]) == [B1], per_uid.get(uid[1]))
        check("rack 3: nessun seriale", per_uid.get(uid[2]) == [], per_uid.get(uid[2]))
        check("conferma all'utente", any(f"Seriali applicati a {attesi} rack" in d for d in dialoghi), dialoghi[-2:])
        check("nessun errore JavaScript", not errori, "; ".join(errori)[:300])
        browser.close()

    ko = [n for n, ok in ESITI if not ok]
    print("=" * 76)
    print("RISULTATO:", "TUTTO OK" if not ko else f"{len(ko)} FALLIMENTI")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
