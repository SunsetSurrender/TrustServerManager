#!/usr/bin/env python3
"""
Verifica che l'applicazione parta con OGNI accesso di rete esterno bloccato.

Requisito di rete chiusa: `support.js` scarica React da unpkg.com se
`window.React`/`window.ReactDOM` non esistono già. `handoff/vendor/` li fornisce
localmente e il runtime salta la CDN da solo. Questo test lo dimostra.

Metodo
------
Chrome reale (installato, headless) carica l'app da un server statico locale.
Ogni richiesta verso un host diverso da localhost viene ABORTITA a livello di
browser e registrata. Girano due casi:

  fixed   = handoff/ come sta adesso
  control = copia identica senza i tag <script> di vendor/ e senza vendor/

Il control serve a dimostrare che il blocco è reale e che il segnale di successo
discrimina davvero: DEVE fallire, e la sua lista di richieste bloccate DEVE
contenere unpkg.com. Senza il control, un "ha renderizzato" non proverebbe nulla
(potrebbe essere cache, o un blocco che non funziona).

Segnale di boot riuscito: `support.js:168` fa `dc.replaceWith(hostEl)`, quindi a
boot riuscito nel DOM c'è <div id="dc-root"> e NON c'è più <x-dc>. A boot
fallito resta <x-dc> e #dc-root non esiste.

Uso
---
    pip install playwright
    python tools/offline-boot-test.py [--handoff PERCORSO] [--port 8137]

Chrome deve essere installato (usa il canale "chrome", non scarica browser).
Su Windows tenere il percorso di lavoro corto: i path lunghi rompono pip.
Exit code 0 = tutti i controlli passati.
"""
import argparse
import functools
import http.server
import json
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

PAGE = "Sala%20Server%20v2.dc.html"
PAGE_FILE = "Sala Server v2.dc.html"
LOCAL_HOSTS = {"localhost", "127.0.0.1"}

VENDOR_TAGS = (
    '<script src="./vendor/react.production.min.js"></script>',
    '<script src="./vendor/react-dom.production.min.js"></script>',
)


def build_tree(handoff: Path, root: Path) -> None:
    """fixed = copia fedele; control = copia senza vendor (stato pre-fix)."""
    shutil.copytree(handoff, root / "fixed")
    shutil.copytree(handoff, root / "control")
    shutil.rmtree(root / "control" / "vendor")

    page = root / "control" / PAGE_FILE
    html = page.read_text(encoding="utf-8")
    for tag in VENDOR_TAGS:
        if tag not in html:
            sys.exit(f"il control non ha trovato il tag da rimuovere: {tag}")
        html = html.replace(tag, "")
    page.write_text(html, encoding="utf-8")


def serve(root: Path, port: int) -> socketserver.TCPServer:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):        # silenzio: il report è il JSON
            pass

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server(("127.0.0.1", port),
                   functools.partial(Handler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run_case(browser, base: str, case: str) -> dict:
    blocked, console, page_errors = [], [], []
    context = browser.new_context()
    page = context.new_page()

    def handler(route, request):
        if urlparse(request.url).hostname not in LOCAL_HOSTS:
            blocked.append(request.url)
            route.abort()
        else:
            route.continue_()

    context.route("**/*", handler)
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{base}/{case}/{PAGE}", wait_until="load")
    page.wait_for_timeout(5000)   # boot async + import('./inventario.js')

    probe = page.evaluate("""() => ({
      reactVersion:    window.React ? window.React.version : null,
      reactDomVersion: window.ReactDOM ? window.ReactDOM.version : null,
      xdcCount:        document.querySelectorAll('x-dc').length,
      dcRootPresent:   !!document.getElementById('dc-root'),
      renderedButtons: [...document.querySelectorAll('button')]
                         .filter(b => !b.closest('x-dc'))
                         .map(b => b.textContent.trim()).slice(0, 5),
    })""")
    context.close()
    return {"case": case, "blocked_external_requests": blocked,
            "console_errors": [c for c in console if c.startswith("error")],
            "page_errors": page_errors, **probe}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handoff", default=str(Path(__file__).resolve().parent.parent / "handoff"))
    ap.add_argument("--port", type=int, default=8137)
    args = ap.parse_args()

    handoff = Path(args.handoff).resolve()
    if not (handoff / PAGE_FILE).is_file():
        return sys.exit(f"non trovo {PAGE_FILE} in {handoff}")

    with tempfile.TemporaryDirectory(prefix="tsm-offline-") as tmp:
        root = Path(tmp)
        build_tree(handoff, root)
        httpd = serve(root, args.port)
        base = f"http://localhost:{args.port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                results = {c: run_case(browser, base, c) for c in ("fixed", "control")}
                browser.close()
        finally:
            httpd.shutdown()

    print(json.dumps(results, indent=2))
    f, c = results["fixed"], results["control"]

    # react-dom@18.3.1 dichiara "18.3.1-next-f1338f8080-20240426": è il valore
    # dell'artefatto ufficiale. La provenienza esatta la pinnano gli SHA in
    # handoff/vendor/README.md, non questa stringa.
    checks = [
        ("fixed: zero richieste esterne",      f["blocked_external_requests"] == []),
        ("fixed: React 18.3.1 presente",       f["reactVersion"] == "18.3.1"),
        ("fixed: ReactDOM build 18.3.1",       (f["reactDomVersion"] or "").startswith("18.3.1")),
        ("fixed: boot ha sostituito x-dc",     f["dcRootPresent"] and f["xdcCount"] == 0),
        ("fixed: UI renderizzata",             len(f["renderedButtons"]) > 0),
        ("fixed: bottone Accedi presente",     "Accedi" in f["renderedButtons"]),
        ("fixed: nessun errore console",       f["console_errors"] == []),
        ("fixed: nessun errore non gestito",   f["page_errors"] == []),
        ("control: ha tentato unpkg",          any("unpkg.com" in u for u in c["blocked_external_requests"])),
        ("control: React assente",             c["reactVersion"] is None),
        ("control: boot NON avvenuto",         (not c["dcRootPresent"]) and c["xdcCount"] == 1),
        ("control: niente renderizzato",       c["renderedButtons"] == []),
    ]
    print("\n" + "=" * 72)
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok &= passed
    print("=" * 72)
    print("RISULTATO:", "TUTTI I CONTROLLI PASSATI" if ok else "CI SONO FALLIMENTI")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
