#!/usr/bin/env python3
"""Prova nel browser delle novità 0.5.0 sulla concorrenza, con DUE persone:

- «● N online» in testata e l'elenco di chi è collegato;
- il rack aperto in modifica da A appare bloccato a B (🔒, avviso, niente
  «+ Dispositivo»), e A vede «Stai modificando questo rack»;
- un salvataggio di B su un ALTRO rack, partito da una versione superata, viene
  UNITO alle modifiche di A invece di andare in conflitto, e B vede le modifiche
  di A senza ricaricare;
- l'amministratore sblocca un rack dalla scheda Diagnostica;
- Avvisi di sistema: salvataggio della configurazione da Impostazioni → Notifiche.

Crea un'utenza di prova e modifica l'inventario: solo su un'installazione di prova.

    python tools/concurrency-ui-test.py --password <pw admin> --allow-destructive
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

import destructive_guard
from ui_helpers import scheda

ESITI = []


def check(nome, ok, dettaglio=""):
    ESITI.append((nome, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {nome}" + ("" if ok else f"\n         → {dettaglio}"))


def login(page, base, u, p):
    page.goto(base + "/")
    page.wait_for_load_state("networkidle")
    page.get_by_placeholder("utente", exact=True).fill(u)
    page.get_by_placeholder("password", exact=True).fill(p)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(2500)


def apri_rack(page, label):
    import re
    loc = page.get_by_text(re.compile(r"^(🔒 )?" + re.escape(label) + "$"))
    if loc.count() == 0:
        page.get_by_role("button", name=re.compile("^Pianta")).first.click()
        page.wait_for_timeout(900)
    loc.first.dispatch_event("click", timeout=10000)
    page.wait_for_timeout(500)


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
    H = {"Origin": base}

    with sync_playwright() as p:
        browser = getattr(p, a.browser).launch(headless=True)
        ctxA = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        ctxB = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        A, B = ctxA.new_page(), ctxB.new_page()
        errori = []
        for pg in (A, B):
            pg.on("pageerror", lambda e: errori.append(str(e)))
            pg.on("dialog", lambda d: d.accept())

        # ---- preparazione: utenza di prova con password scelta ----
        login(A, base, a.username, a.password)
        nome_b = "collega-" + uuid.uuid4().hex[:6]
        pw_b = "una frase lunga della collega di prova"
        r = A.request.post(base + "/api/users", headers=H, data={
            "username": nome_b, "role": "edit", "nome": "Carla", "cognome": "Verdi"})
        uid_b = r.json()["user"]["id"]
        A.request.post(f"{base}/api/users/{uid_b}/reset-password", headers=H,
                       data={"password": pw_b, "mustChange": False})
        login(B, base, nome_b, pw_b)
        me = A.request.get(base + "/api/auth/me").json()
        chi_a = " ".join(x for x in (me.get("nome"), me.get("cognome")) if x) or a.username

        inv = A.request.get(base + "/api/inventory").json()
        racks = inv["doc"]["locations"][0]["sale"][0]["racks"]
        R1, R2 = racks[0], racks[1]
        etichetta = lambda rk: (rk.get("seriali") or [None])[0] or rk["id"]   # noqa: E731

        # ---- modalità modifica, A apre R1 ----
        for pg in (A, B):
            pg.get_by_role("button", name="Editing", exact=True).click()
            pg.wait_for_timeout(400)
        apri_rack(A, etichetta(R1))
        A.wait_for_timeout(3500)
        check("A vede «Stai modificando questo rack»", A.locator("#rack-mio").count() == 1)

        apri_rack(B, etichetta(R1))
        B.wait_for_timeout(3500)
        banner = B.locator("#rack-bloccato")
        check("B vede il rack bloccato", banner.count() == 1)
        check("…con il nome di chi lo modifica", banner.count() and chi_a in banner.inner_text(),
              banner.inner_text() if banner.count() else "")
        check("B non può aggiungere dispositivi a quel rack",
              B.get_by_role("button", name="+ Dispositivo").count() == 0)
        check("la pianta di B mostra il lucchetto", "🔒" in B.inner_text("body"))

        # ---- chi è online ----
        B.wait_for_timeout(500)
        chip = B.locator("#btn-online")
        check("«N online» in testata, almeno 2", chip.count() and int(chip.inner_text().split()[1]) >= 2,
              chip.inner_text() if chip.count() else "")
        chip.click()
        B.wait_for_timeout(1200)
        pannello = B.locator("#pannello-online")
        testo = pannello.inner_text() if pannello.count() else ""
        check("l'elenco mostra entrambi", "Carla Verdi (tu)" in testo and chi_a in testo, testo)
        chip.click()

        # ---- unione: A salva su R1 via API mentre B lavora su R2 ----
        apri_rack(B, etichetta(R2))
        B.wait_for_timeout(3000)
        cur = A.request.get(base + "/api/inventory").json()
        doc = cur["doc"]
        rk1 = next(k for k in doc["locations"][0]["sale"][0]["racks"] if k["_uid"] == R1["_uid"])
        nota_a = "modificato da A " + uuid.uuid4().hex[:4]
        rk1["note"] = nota_a
        r = A.request.put(base + "/api/inventory", headers=H,
                          data={"baseVersion": cur["version"], "doc": doc})
        check("A salva il suo rack", r.ok, r.text()[:200])

        occupate = {u for d in R2.get("devices", []) for u in range(d.get("u", 1), d.get("u", 1) + (d.get("h") or 1))}
        libera = next(u for u in range(R2.get("u", 42), 0, -1) if u not in occupate)
        B.get_by_role("button", name="+ Dispositivo").click()
        B.wait_for_timeout(500)
        nuovo = "prova-unione-" + uuid.uuid4().hex[:4]
        B.locator('input[name="name"]').first.fill(nuovo)
        B.locator('input[name="u"]').first.fill(str(libera))
        B.get_by_role("button", name="Salva", exact=True).first.click()
        B.wait_for_timeout(3500)
        nota = B.locator("#nota-unione")
        check("B vede «salvato insieme alle modifiche…»", nota.count() == 1,
              B.inner_text("body")[:300])
        check("nessun conflitto mostrato a B", "Un'altra sessione ha salvato" not in B.inner_text("body"))
        fin = A.request.get(base + "/api/inventory").json()["doc"]
        rr = {k["_uid"]: k for k in fin["locations"][0]["sale"][0]["racks"]}
        check("sul server ci sono ENTRAMBE le modifiche",
              rr[R1["_uid"]].get("note") == nota_a
              and any(d["name"] == nuovo for d in rr[R2["_uid"]]["devices"]))
        stato_b = B.evaluate("() => document.body.innerText")
        check("B ha ricevuto il documento unito (vede il proprio dispositivo)", nuovo in stato_b)

        # ---- sblocco da Diagnostica ----
        # A apre le Impostazioni: non sta più modificando R1, e il blocco si libera.
        scheda(A, "diagnostica", 2500)
        apri_rack(B, etichetta(R1))           # ora R1 è libero: lo prende B
        B.wait_for_timeout(3000)
        check("aperte le Impostazioni, il rack di A si libera e lo prende B",
              B.locator("#rack-mio").count() == 1 and B.locator("#rack-bloccato").count() == 0)
        A.get_by_role("button", name="Aggiorna").first.click()
        A.wait_for_timeout(1500)
        diag = A.locator("#diagnostica")
        testo = diag.inner_text() if diag.count() else ""
        check("Diagnostica: il rack in modifica è elencato con chi lo tiene",
              "🔒" in testo and "Carla Verdi" in testo, testo[:600])
        check("Diagnostica: sezioni presenti",
              all(t in testo for t in ("AVVISI DI SISTEMA", "DUMP DEL DATABASE", "ACCESSI")))
        diag.get_by_role("button", name="Sblocca").first.click()
        A.wait_for_timeout(1200)
        locks = A.request.get(base + "/api/system/diagnostics").json().get("locks", [])
        check("dopo lo sblocco il rack è libero", not locks, json.dumps(locks))

        # ---- avvisi di sistema: configurazione ----
        scheda(A, "notifiche", 1500)
        av = A.locator("#avvisi-sistema")
        check("Notifiche: riquadro Avvisi di sistema", av.count() == 1)
        A.locator("#al-extra").fill("noc@example.it")
        A.locator("#al-extra").dispatch_event("change")
        A.locator("#btn-al-save").click()
        A.wait_for_timeout(1500)
        cfg = A.request.get(base + "/api/system/alerts").json()
        check("destinatario aggiuntivo salvato", cfg.get("extraRecipients") == ["noc@example.it"],
              json.dumps(cfg)[:300])
        check("…e mostrato fra i destinatari effettivi", "noc@example.it" in av.inner_text())
        A.request.put(base + "/api/system/alerts", headers=H,
                      data={**{k: cfg[k] for k in ("enabled", "includeAdmins", "minLevel",
                                                   "repeatHours")}, "extraRecipients": []})

        # ---- stato: legenda ----
        scheda(A, "stato", 1500)
        leg = A.locator("#stato-legenda")
        check("Stato: legenda dei colori", leg.count() and "informativo" in leg.inner_text())

        # ---- pulizia ----
        A.request.post(f"{base}/api/users/{uid_b}/disable", headers=H)
        check("nessun errore JavaScript", not errori, "; ".join(errori)[:300])
        browser.close()

    ko = [n for n, ok in ESITI if not ok]
    print("=" * 76)
    print("RISULTATO:", "TUTTO OK" if not ko else f"{len(ko)} FALLIMENTI")
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
