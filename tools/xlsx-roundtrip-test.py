#!/usr/bin/env python3
"""Regressione: export XLSX → re-import NON deve alterare i dati.

Il foglio "Dispositivi" dell'export formattato usa intestazioni leggibili
("Altezza U") ed etichette ("In manutenzione"), mentre l'import cercava i nomi
tecnici e le chiavi. Quel foglio è ri-importabile — l'import minuscolizza le
intestazioni e riconosce le sale per nome — quindi il giro export→import
azzerava le altezze a 1 e riportava ogni stato ad "attivo".

Questo test dimostra la preservazione SEMANTICA, non solo quella degli `_uid`:

  1. login e passaggio in Editing
  2. si carica via import JSON un documento con stati NON predefiniti
     (il seed non ne ha: senza questo passo il difetto sugli stati non si
     manifesterebbe)
  3. export JSON  → riferimento
  4. export XLSX  → il foglio formattato
  5. re-import di quello stesso XLSX, con "Applica import"
  6. export JSON  → confronto campo per campo con il riferimento

Il seed contiene già altezze non predefinite (2, 3, 4 e 6 U), quindi il
requisito "più altezze non predefinite" è soddisfatto dai dati reali.

Uso:
    pip install playwright
    python tools/xlsx-roundtrip-test.py
"""
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "handoff"
PAGE = "Sala%20Server%20v2.dc.html"
PORT = 8139
LOCAL = {"localhost", "127.0.0.1"}

#: Campi che devono sopravvivere al giro. `h` e `stato` sono i due difetti noti.
COMPARED = ("_uid", "id", "name", "type", "stato", "model", "ip", "serial",
            "owner", "u", "h", "garanzia", "supporto", "note")

results = []


def check(name, passed, detail=""):
    results.append((name, passed, detail))


def serve(root: Path, port: int):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server(("127.0.0.1", port), functools.partial(Handler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


#: Valori predefiniti documentati: nel prototipo un dispositivo senza `stato` è
#: mostrato come "attivo" (`d.stato || 'attivo'`) e senza `h` occupa 1 U. Il giro
#: export→import MATERIALIZZA questi default, che è una normalizzazione e non una
#: perdita: il confronto va quindi fatto sul valore efficace, non sulla presenza
#: della chiave. Tutto il resto deve tornare identico.
DEFAULTS = {"stato": "attivo", "h": 1}


def devices_of(doc):
    """{uid: {campi confrontati}} per ogni dispositivo, più il rack che lo contiene."""
    out = {}
    for L in doc.get("locations", []):
        for R in L.get("sale", []):
            for K in R.get("racks", []):
                for V in K.get("devices", []):
                    rec = {}
                    for k in COMPARED:
                        v = V.get(k)
                        if not v and k in DEFAULTS:
                            v = DEFAULTS[k]
                        rec[k] = v
                    rec["_rack"] = K.get("_uid")
                    out[V.get("_uid")] = rec
    return out


def export_json(page, tmp: Path, label: str) -> dict:
    with page.expect_download(timeout=30000) as dl:
        page.get_by_role("button", name="Esporta ▾").click()
        page.get_by_text("JSON completo", exact=False).first.click()
    p = tmp / f"{label}.json"
    dl.value.save_as(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def export_xlsx(page, tmp: Path) -> Path:
    with page.expect_download(timeout=30000) as dl:
        page.get_by_role("button", name="Esporta ▾").click()
        page.get_by_text("Excel (.xlsx)", exact=False).first.click()
    p = tmp / "inventario.xlsx"
    dl.value.save_as(str(p))
    return p


def main() -> int:
    tmp = ROOT / ".xlsx-roundtrip-tmp"
    tmp.mkdir(exist_ok=True)
    httpd = serve(HANDOFF, PORT)
    dialogs, console_errors = [], []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            # Viewport alto: il pannello Impostazioni è un dropdown lungo e
            # l'anteprima dell'import finisce fuori da un viewport standard.
            context = browser.new_context(accept_downloads=True,
                                          viewport={"width": 1600, "height": 2400})

            def route(r, req):
                if urlparse(req.url).hostname not in LOCAL:
                    r.abort()
                else:
                    r.continue_()

            context.route("**/*", route)
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

            page.goto(f"http://localhost:{PORT}/{PAGE}", wait_until="load")
            page.wait_for_timeout(5000)

            page.get_by_placeholder("utente").fill("admin")
            page.get_by_placeholder("password").fill("admin")
            page.get_by_role("button", name="Accedi").click()
            page.wait_for_timeout(1200)
            page.get_by_role("button", name="Editing", exact=True).click()
            page.wait_for_timeout(600)

            # --- 2. si introducono stati non predefiniti via import JSON ---
            seed = export_json(page, tmp, "seed")
            touched = []
            for L in seed.get("locations", []):
                for R in L.get("sale", []):
                    for K in R.get("racks", []):
                        for V in K.get("devices", []):
                            if len(touched) < 4 and V.get("h", 1) == 1:
                                V["stato"] = ["manutenzione", "dismissione", "dismesso",
                                              "manutenzione"][len(touched)]
                                touched.append(V["_uid"])
            check("preparati stati non predefiniti", len(touched) == 4, f"{len(touched)}")

            prepared = tmp / "prepared.json"
            prepared.write_text(json.dumps(seed), encoding="utf-8")

            # Il pannello Impostazioni contiene sia l'import JSON sia l'import
            # tabellare, e va aperto perché gli input esistano. Selettore su accept
            # ESATTO: `accept*=".json"` peschererebbe anche l'input del "confronto
            # rilievi" (.json,.csv,.xlsx), che fa una cosa diversa.
            page.get_by_role("button", name="⚙ Impostazioni").click()
            page.wait_for_timeout(600)
            json_input = page.locator('input[type="file"][accept=".json"]')
            check("input import JSON trovato", json_input.count() == 1, f"{json_input.count()}")
            json_input.first.set_input_files(str(prepared))
            page.wait_for_timeout(2000)

            # --- 3. riferimento ---
            ref = export_json(page, tmp, "riferimento")
            ref_devs = devices_of(ref)
            heights = sorted({d["h"] for d in ref_devs.values() if d["h"] not in (None, 1)})
            check("il riferimento contiene più altezze non predefinite",
                  len(heights) >= 3, f"altezze non-1 presenti: {heights}")
            states = {d["stato"] for d in ref_devs.values()}
            check("il riferimento contiene più stati",
                  len({s for s in states if s and s != "attivo"}) >= 3, f"stati: {sorted(x for x in states if x)}")

            # --- 4/5. export XLSX e re-import ---
            xlsx = export_xlsx(page, tmp)
            check("XLSX esportato", xlsx.exists() and xlsx.stat().st_size > 0,
                  f"{xlsx.stat().st_size if xlsx.exists() else 0} byte")

            # Il pannello "IMPORTA DATI" vive dentro <sc-if accountOpen>: senza
            # aprirlo l'input non esiste affatto, e un selettore per accept*=".xlsx"
            # finirebbe sull'input del "confronto rilievi", che accetta anch'esso
            # .xlsx ma fa una cosa completamente diversa.
            page.get_by_role("button", name="⚙ Impostazioni").click()
            page.wait_for_timeout(600)
            import_input = page.locator('input[type="file"][accept=".csv,.xlsx"]')
            check("pannello import aperto", import_input.count() == 1,
                  f"{import_input.count()} input trovati")

            import_input.first.set_input_files(str(xlsx))
            page.wait_for_timeout(3000)

            applica = page.get_by_role("button", name="Applica import")
            check("anteprima import mostrata", applica.count() == 1 and applica.is_visible(),
                  f"dialoghi: {dialogs[-2:]}")
            # dispatch_event invece di click(): il pulsante è in fondo a un dropdown
            # che può restare fuori dal viewport, e qui interessa l'handler, non la
            # geometria (quella la copre identity-ui-test.py con click reali).
            applica.dispatch_event("click")
            page.wait_for_timeout(3000)

            # --- 6. confronto ---
            after = export_json(page, tmp, "dopo")
            after_devs = devices_of(after)

            check("nessun dispositivo perso o creato",
                  set(after_devs) == set(ref_devs),
                  f"prima {len(ref_devs)}, dopo {len(after_devs)}; "
                  f"mancanti {list(set(ref_devs) - set(after_devs))[:3]}, "
                  f"in più {list(set(after_devs) - set(ref_devs))[:3]}")

            diffs = []
            for uid, before in ref_devs.items():
                now = after_devs.get(uid)
                if now is None:
                    continue
                for k in COMPARED + ("_rack",):
                    if (before.get(k) or "") != (now.get(k) or ""):
                        diffs.append(f"{before['name']}.{k}: {before.get(k)!r} → {now.get(k)!r}")

            h_diffs = [d for d in diffs if ".h:" in d]
            s_diffs = [d for d in diffs if ".stato:" in d]
            check("ALTEZZE preservate (difetto Altezza U)", not h_diffs,
                  f"{len(h_diffs)} alterate: " + "; ".join(h_diffs[:5]))
            check("STATI preservati (difetto etichette)", not s_diffs,
                  f"{len(s_diffs)} alterati: " + "; ".join(s_diffs[:5]))
            check("nessun dispositivo spostato di rack",
                  not [d for d in diffs if "._rack:" in d],
                  "; ".join(d for d in diffs if "._rack:" in d)[:200])
            check("preservazione semantica completa", not diffs,
                  f"{len(diffs)} differenze: " + "; ".join(diffs[:8]))
            check("nessun errore di console", console_errors == [],
                  " | ".join(console_errors[:3]))

            browser.close()
    finally:
        httpd.shutdown()

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
