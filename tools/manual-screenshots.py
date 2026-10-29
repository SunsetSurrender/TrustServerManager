#!/usr/bin/env python3
"""Schermate per il manuale (docs/manuale/img/*.png), da uno stack di PROVA.

Prepara alcuni dispositivi d'esempio — nomi `esempio-…`, chiaramente finti — sopra
la struttura della baseline (sale e rack veri), poi fotografa le viste principali
con il tema Chiaro, che è quello che si legge meglio stampato.

    python tools/manual-screenshots.py --password <pw admin> --allow-destructive

Modifica l'inventario: mai su tsm-prd.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

import destructive_guard

OUT = Path(__file__).resolve().parents[1] / "docs" / "manuale" / "img"


def esempi() -> list[tuple[str, list[dict]]]:
    oggi = date.today()
    d = lambda giorni: (oggi + timedelta(days=giorni)).isoformat()
    def dev(nome, tipo, modello, ip, u, h, garanzia=None, owner="Team Sistemi"):
        x = {"_uid": str(uuid.uuid4()), "id": nome, "name": nome, "type": tipo,
             "model": modello, "ip": ip, "serial": "ES-" + nome[-2:].upper() + "0042",
             "owner": owner, "u": u, "h": h}
        if garanzia:
            x["garanzia"] = garanzia
        return x
    return [
        ("A", [dev("esempio-fw-01", "firewall", "Modello firewall", "10.20.0.1", 40, 1, d(25), "Team Rete"),
               dev("esempio-sw-01", "rete", "Switch 48 porte", "10.20.0.2", 38, 1, d(200), "Team Rete"),
               dev("esempio-srv-01", "server", "Server 2U", "10.20.1.11", 30, 2, d(12)),
               dev("esempio-srv-02", "server", "Server 2U", "10.20.1.12", 27, 2, d(400)),
               dev("esempio-sto-01", "storage", "Storage 4U", "10.20.2.10", 18, 4, d(-10)),
               dev("esempio-ups-01", "alimentazione", "UPS 3U", "", 2, 3, d(700))]),
        ("B", [dev("esempio-srv-03", "server", "Server 1U", "10.20.1.13", 35, 1, d(60)),
               dev("esempio-srv-04", "server", "Server 1U", "10.20.1.14", 34, 1),
               dev("esempio-sw-02", "rete", "Switch 24 porte", "10.20.0.3", 42, 1)]),
    ]


def _rifila(f: Path) -> None:
    """Toglie il fondo vuoto sotto il contenuto (con Pillow, se c'è)."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    im = Image.open(f).convert("RGB")
    fondo = Image.new("RGB", im.size, im.getpixel((im.width - 2, im.height - 2)))
    box = ImageChops.difference(im, fondo).getbbox()
    if box and box[3] < im.height - 40:
        im.crop((0, 0, im.width, min(im.height, box[3] + 40))).save(f, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    destructive_guard.add_arguments(ap)
    a = ap.parse_args()
    destructive_guard.enforce(a, a.base)
    base = a.base.rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900},
                            device_scale_factor=2)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())

        def shot(nome, el=None, **kw):
            page.wait_for_timeout(700)
            f = OUT / f"{nome}.png"
            (el or page).screenshot(path=str(f), **kw)
            _rifila(f)
            print("  ", nome)

        # ---- accesso ----
        page.goto(base + "/")
        page.wait_for_load_state("networkidle")
        shot("01-accesso", el=page.locator("img[alt='Trust Technologies']").first.locator(".."))
        if page.get_by_role("button", name="Password dimenticata?").count():
            page.get_by_role("button", name="Password dimenticata?").click()
            shot("02-recupero", el=page.locator("img[alt='Trust Technologies']").first.locator(".."))
            page.get_by_role("button", name="Torna all'accesso").click()
        page.get_by_placeholder("utente", exact=True).fill(a.username)
        page.get_by_placeholder("password", exact=True).fill(a.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(2500)

        # ---- dati d'esempio sopra la baseline ----
        inv = page.request.get(base + "/api/inventory").json()
        doc = inv["doc"]
        racks = [R for L in doc["locations"][:1] for S in L["sale"][:1] for R in S["racks"]]
        if racks and not any(dv.get("name", "").startswith("esempio-")
                             for R in racks for dv in R.get("devices", [])):
            for (quale, devs), R in zip(esempi(), racks[:2]):
                R.setdefault("devices", []).extend(devs)
            r = page.request.put(base + "/api/inventory",
                                 data={"baseVersion": inv["version"], "doc": doc,
                                       "action": "dispositivi d'esempio per il manuale"},
                                 headers={"Origin": base})
            print("   dati d'esempio:", r.status)
            page.reload()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2500)


        # ---- viste ----
        import re
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        # tema chiaro: si legge meglio stampato (dopo il reload, che lo azzera)
        page.get_by_role("button", name="⚙ Impostazioni").click()
        page.wait_for_timeout(400)
        page.get_by_role("button", name="Chiaro", exact=True).click()
        page.wait_for_timeout(300)
        page.get_by_role("button", name="⚙ Impostazioni").click()
        page.wait_for_timeout(400)
        shot("03-dashboard")                       # all'accesso si apre la Dashboard

        def vista(etichetta):
            page.get_by_role("button", name=re.compile("^" + etichetta)).first.click()
            page.wait_for_timeout(900)

        vista("Pianta")
        shot("04-pianta")
        if racks:
            page.get_by_text(racks[0]["id"], exact=True).first.dispatch_event("click")
            page.wait_for_timeout(1500)
            shot("05-rack")
        vista("Inventario")
        shot("06-inventario")
        vista("Scadenze")
        shot("07-scadenze")
        vista("Capacità")
        shot("08-capacita")
        vista("Pianta")

        # ---- profilo e utenze ----
        page.get_by_role("button", name="AD", exact=True).first.click()
        page.wait_for_timeout(1200)
        shot("09-profilo", full_page=True)
        righe = page.locator("[data-tsm-reset]")
        if righe.count():
            righe.first.click()
            page.get_by_role("button", name="Scegli tu la password").click()
            shot("10-reimposta", el=page.locator("#form-reimposta-password"))
            page.locator("#form-reimposta-password").get_by_role("button", name="Annulla").click()
        if page.locator("#stato-sistema").count():
            page.wait_for_timeout(1200)
            shot("11-stato", el=page.locator("#stato-sistema"))
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
