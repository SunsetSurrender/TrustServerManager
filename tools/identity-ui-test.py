#!/usr/bin/env python3
"""Verifica che l'applicazione REALE sia cablata alla logica di identità.

`tools/identity-tests.mjs` verifica la logica; questo verifica il cablaggio.
Serve perché il bug originale — `saveDraft` che ricostruiva l'oggetto — era un
bug del frontend: un test che esercita solo il modulo non lo vedrebbe.

Controlli, tutti attraverso l'interfaccia vera (Chrome headless, rete esterna
bloccata come in tools/offline-boot-test.py):

  1. l'app carica `identity.js`
  2. NESSUN backfill a runtime: dopo il boot `crypto.randomUUID` non è mai stato
     chiamato. Se l'app generasse identità al caricamento, qui si vedrebbero ~197
     chiamate. È il criterio «caricare l'app non deve mai fabbricare identità
     sostitutive per dati esistenti».
  3. l'export JSON dall'interfaccia contiene tutte le entità, ognuna con un _uid
     valido e univoco (il seed migrato attraversa l'app intatto)
  4. creando un sito dall'interfaccia: esattamente UNA nuova identità, generata
     via crypto.randomUUID, e tutti gli _uid preesistenti invariati

Uso:
    pip install playwright
    python tools/identity-ui-test.py
"""
import json
import re
import subprocess
import sys
import threading
import http.server
import socketserver
import functools
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "handoff"
PAGE = "Sala%20Server%20v2.dc.html"
PORT = 8138
LOCAL = {"localhost", "127.0.0.1"}
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)

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


def walk_entities(doc):
    """Stesso attraversamento di identity.js, per verificare in modo indipendente."""
    out = []
    for L in doc.get("locations", []):
        out.append(("location", L.get("_uid"), L.get("id")))
        for R in L.get("sale", []):
            out.append(("room", R.get("_uid"), R.get("id")))
            for K in R.get("racks", []):
                out.append(("rack", K.get("_uid"), K.get("id")))
                for V in K.get("devices", []):
                    out.append(("device", V.get("_uid"), V.get("id")))
    for M in doc.get("manuale", []):
        out.append(("manual", M.get("_uid"), M.get("id")))
    return out


def export_json(page, tmpdir: Path, label: str) -> dict:
    """Esporta il documento dall'interfaccia e restituisce il JSON scaricato."""
    with page.expect_download(timeout=20000) as dl:
        page.get_by_role("button", name="Esporta ▾").click()
        page.get_by_text("JSON completo", exact=False).first.click()
    path = tmpdir / f"{label}.json"
    dl.value.save_as(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    tmpdir = ROOT / ".identity-ui-tmp"
    tmpdir.mkdir(exist_ok=True)
    httpd = serve(HANDOFF, PORT)
    blocked, console_errors, dialogs = [], [], []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(accept_downloads=True)

            # Spia su crypto.randomUUID, installata PRIMA di ogni script di pagina.
            context.add_init_script("""
              (() => {
                window.__uuidCalls = 0;
                const c = window.crypto;
                if (c && typeof c.randomUUID === 'function') {
                  const orig = c.randomUUID.bind(c);
                  Object.defineProperty(c, 'randomUUID', {
                    configurable: true, writable: true,
                    value: function () { window.__uuidCalls++; return orig(); },
                  });
                }
              })();
            """)

            def route(r, req):
                if urlparse(req.url).hostname not in LOCAL:
                    blocked.append(req.url)
                    r.abort()
                else:
                    r.continue_()

            context.route("**/*", route)
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            # Un alert qui significherebbe seed senza identità: va catturato, non ignorato.
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

            requested = []
            page.on("requestfinished", lambda r: requested.append(r.url))

            page.goto(f"http://localhost:{PORT}/{PAGE}", wait_until="load")
            page.wait_for_timeout(5000)

            check("l'app carica identity.js",
                  any("identity.js" in u for u in requested),
                  f"{len(requested)} richieste, nessuna a identity.js")
            check("boot senza errori di console", console_errors == [],
                  " | ".join(console_errors[:3]))
            check("boot senza alert di identità non valida", dialogs == [],
                  " | ".join(dialogs[:2]))

            # --- criterio: nessun backfill a runtime ---
            calls_after_boot = page.evaluate("() => window.__uuidCalls")
            check("NESSUN backfill al caricamento (0 chiamate a randomUUID)",
                  calls_after_boot == 0,
                  f"randomUUID chiamata {calls_after_boot} volte durante il boot")

            # --- login ---
            page.get_by_placeholder("utente").fill("admin")
            page.get_by_placeholder("password").fill("admin")
            page.get_by_role("button", name="Accedi").click()
            page.wait_for_timeout(1500)
            check("login riuscito", page.get_by_role("button", name="Esporta ▾").is_visible())

            # --- export iniziale ---
            doc0 = export_json(page, tmpdir, "prima")
            ents0 = walk_entities(doc0)
            uids0 = [u for (_, u, _) in ents0]
            check("export: entità presenti", len(ents0) > 150, f"{len(ents0)} entità")
            check("export: ogni entità ha un _uid conforme",
                  all(u and UUID_RE.match(u) for u in uids0),
                  f"{sum(1 for u in uids0 if not (u and UUID_RE.match(u)))} non conformi")
            check("export: tutti gli _uid distinti", len(set(uids0)) == len(uids0),
                  f"{len(set(uids0))} distinti su {len(uids0)}")
            check("export: i vani non hanno _uid",
                  all("_uid" not in v for L in doc0.get("locations", [])
                      for R in L.get("sale", []) for v in R.get("vani", [])))

            # --- creazione di un sito dall'interfaccia ---
            # Il login parte in sola visualizzazione: i comandi di struttura
            # esistono solo in Editing.
            page.get_by_role("button", name="Editing", exact=True).click()
            page.wait_for_timeout(800)
            check("passaggio in Editing",
                  page.get_by_role("button", name="+ Sito").is_visible())

            before = page.evaluate("() => window.__uuidCalls")
            page.get_by_role("button", name="+ Sito").click()
            page.get_by_placeholder("Nome (es. Oriolo Romano — A0)").fill("Sito Di Prova UID")
            page.get_by_role("button", name="Crea").click()
            page.wait_for_timeout(1200)
            after = page.evaluate("() => window.__uuidCalls")
            check("creazione sito: esattamente 1 identità generata", after - before == 1,
                  f"{after - before} chiamate a randomUUID")

            doc1 = export_json(page, tmpdir, "dopo")
            ents1 = walk_entities(doc1)
            uids1 = [u for (_, u, _) in ents1]
            nuovo = [e for e in ents1 if e[0] == "location" and e[2] == "sito-di-prova-uid"]
            check("creazione sito: presente nell'export", len(nuovo) == 1, str(nuovo))
            check("creazione sito: _uid conforme",
                  bool(nuovo and nuovo[0][1] and UUID_RE.match(nuovo[0][1])),
                  str(nuovo[0][1]) if nuovo else "assente")
            check("creazione sito: gli _uid preesistenti sono invariati",
                  set(uids0).issubset(set(uids1)),
                  f"mancanti: {list(set(uids0) - set(uids1))[:3]}")
            check("creazione sito: una sola entità in più",
                  len(ents1) == len(ents0) + 1, f"{len(ents0)} → {len(ents1)}")

            check("nessuna richiesta esterna durante il test", blocked == [],
                  " | ".join(blocked[:3]))
            browser.close()
    finally:
        httpd.shutdown()

    print("=" * 74)
    ok = True
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        if detail and not passed:
            print(f"         → {detail}")
        ok &= passed
    print("=" * 74)
    print("RISULTATO:", "TUTTI I CONTROLLI PASSATI" if ok else "CI SONO FALLIMENTI")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
