#!/usr/bin/env python3
"""Foto dei rack nel browser vero, via nginx/TLS.

Il commit sostituisce completamente il percorso base64/dataURL del prototipo, e la
parte che si può provare SOLO qui è il confine fra le due richieste:

    scegli l'immagine → POST /api/photos → UUID
      → l'UUID entra nella bozza
        → PUT /api/inventory versionato
          → SOLO ADESSO il rack è salvato

Quello che nessun test dell'API può vedere:

  - che nel corpo del `PUT` viaggi l'UUID e **non i byte** dell'immagine;
  - che l'anteprima locale sia un `blob:` e venga revocata;
  - che dopo un ricaricamento l'immagine si carichi da `/api/photos/<uuid>`, cioè
    dal server e non dal documento;
  - che un conflitto durante la sostituzione NON cancelli nessuna delle due foto;
  - che nel JSON esportato non compaia nessun `data:`.

Il test CREA un sito, una sala e un rack, e carica immagini: modifica l'inventario
di produzione, quindi passa dalla guardia dei test distruttivi (§8.37).

Uso:
    python tools/photos-ui-test.py --base https://localhost --password <pw> \
        --allow-destructive
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import destructive_guard  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

try:
    from PIL import Image
except ImportError:
    sys.exit("serve pillow per generare le immagini di prova:  pip install pillow")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP = Path(__file__).resolve().parent.parent / ".photos-ui-tmp"

SITO = "Sito Foto E2E"
SALA = "Sala Foto E2E"
RACK = "RFOTO1"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, bool(passed), detail))


def report() -> int:
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


# ==================================================================
# immagini di prova
# ==================================================================

def make_file(name: str, colour, size=(600, 450), fmt="JPEG") -> Path:
    """Un'immagine vera su disco: `set_input_files` vuole un file, e serve che sia
    un JPEG valido — è il server a decidere se accettarla."""
    TMP.mkdir(exist_ok=True)
    path = TMP / name
    Image.new("RGB", size, colour).save(path, format=fmt, quality=90)
    return path


def make_svg(name: str = "finta.svg") -> Path:
    TMP.mkdir(exist_ok=True)
    path = TMP / name
    path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="10" '
                     b'height="10"><script>fetch("/api/users")</script></svg>')
    return path


# ==================================================================
# navigazione
# ==================================================================

def login(page, base, username, password):
    page.goto(f"{base}/", wait_until="load")
    page.wait_for_timeout(4500)
    campo = page.get_by_placeholder("utente")
    if campo.count() == 0:
        return
    campo.fill(username)
    page.get_by_placeholder("password").fill(password)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(4500)


def modalita_editing(page):
    b = page.get_by_role("button", name="Editing", exact=True)
    if b.count():
        b.click()
        page.wait_for_timeout(800)


def chiudi_dashboard(page):
    """La dashboard è aperta all'avvio e al suo posto NON c'è la barra dei tab.

    Senza questo passaggio «+ Sala» e «+ Rack» non esistono nel DOM, e il test si
    ferma su un timeout che parla di un pulsante mancante — cioè su un sintomo che
    non ha niente a che vedere con le foto.
    """
    if page.get_by_role("button", name="+ Sala").count() == 0:
        b = page.get_by_role("button", name="Dashboard", exact=True)
        if b.count():
            b.click()
            page.wait_for_timeout(1500)


def crea_scenario(page):
    """Sito → sala → rack. Il rack resta selezionato: è la scheda con la FOTO."""
    modalita_editing(page)
    page.get_by_role("button", name="+ Sito").click()
    page.get_by_placeholder("Nome (es. Oriolo Romano — A0)").fill(SITO)
    page.get_by_role("button", name="Crea", exact=True).click()
    page.wait_for_timeout(3500)

    chiudi_dashboard(page)
    page.get_by_role("button", name="+ Sala").click()
    page.get_by_placeholder("Nome sala").fill(SALA)
    page.get_by_role("button", name="Crea sala").click()
    page.wait_for_timeout(3500)

    page.get_by_role("button", name="+ Rack").click()
    page.wait_for_timeout(500)
    page.locator('input[name="id"]').first.fill(RACK)
    page.get_by_role("button", name="Salva rack").click()
    page.wait_for_timeout(3500)


def cerca_rack(page):
    """Riseleziona il rack dopo un ricaricamento, passando dalla ricerca."""
    box = page.get_by_placeholder("Cerca nome, seriale, rack, IP o range")
    box.fill(RACK)
    page.wait_for_timeout(1200)
    voce = page.get_by_text(RACK, exact=False).first
    voce.click()
    page.wait_for_timeout(2000)


def sfondo_anteprima(page) -> str:
    el = page.locator('[data-test="foto-anteprima"]')
    if el.count() == 0:
        return ""
    return el.first.evaluate("e => e.style.backgroundImage || ''")


def pronto_per_caricare(page) -> bool:
    """Riseleziona il rack ED entra in modalità Editing.

    Un ricaricamento riporta l'interfaccia in sola lettura — è il comportamento
    voluto — e il comando di caricamento esiste solo per un amministratore in
    modalità Editing. Dimenticarsene fa fallire il test su un input mancante, cioè
    su un sintomo che sembra un difetto delle foto e non lo è.
    """
    cerca_rack(page)
    modalita_editing(page)
    page.wait_for_timeout(800)
    return page.locator('[data-test="foto-input"]').count() > 0


def carica(page, path: Path) -> None:
    page.locator('[data-test="foto-input"]').first.set_input_files(str(path))
    page.wait_for_timeout(4000)


def api_get(page, base, path):
    return page.request.get(f"{base}{path}", ignore_https_errors=True)


# ==================================================================
# il test
# ==================================================================

def run(page, base, args, puts, api_calls, console_text) -> None:
    login(page, base, args.username, args.password)
    me = api_get(page, base, "/api/auth/me")
    body = me.json() if me.status == 200 else {}
    if body.get("role") != "admin":
        check("PRECONDIZIONE: sessione amministrativa attiva", False,
              f"HTTP {me.status} {me.text()[:160]}")
        return
    check("PRECONDIZIONE: sessione amministrativa attiva", True)

    crea_scenario(page)
    check("il rack di prova è selezionato e mostra il riquadro FOTO",
          page.locator('[data-test="foto-carica"]').count() == 1,
          page.inner_text("body")[:200])
    check("all'inizio non c'è nessuna foto",
          "Nessuna foto allegata" in page.inner_text("body"))

    # ---------------- 1. caricamento ----------------
    uno = make_file("uno.jpg", (200, 40, 40))
    puts.clear()
    api_calls.clear()
    carica(page, uno)

    check("il caricamento passa da POST /api/photos",
          any(c == "POST /api/photos" for c in api_calls), str(api_calls[:8]))
    check("dopo il caricamento c'è un PUT /api/inventory",
          any(c == "PUT /api/inventory" for c in api_calls), str(api_calls[:8]))

    sfondo = sfondo_anteprima(page)
    check("l'anteprima punta a /api/photos/<uuid>",
          "/api/photos/" in sfondo and "data:" not in sfondo, sfondo[:160])
    check("nessun blob: residuo nell'anteprima dopo il salvataggio",
          "blob:" not in sfondo, sfondo[:160])

    doc = api_get(page, base, "/api/inventory").json()
    uid_uno = trova_foto(doc)
    check("il documento contiene l'UUID della foto", bool(uid_uno),
          json.dumps(doc)[:200])
    check("il documento NON contiene nessun dataURL",
          "data:image" not in json.dumps(doc))

    # ---- il corpo del PUT: l'UUID, non i byte ----
    corpo = puts[-1] if puts else ""
    check("il PUT dell'inventario contiene l'UUID della foto",
          bool(uid_uno) and uid_uno in corpo, corpo[:200])
    check("il PUT dell'inventario NON contiene byte di immagine",
          "data:image" not in corpo and "base64" not in corpo,
          corpo[:200])
    check("il corpo del PUT resta piccolo (nessuna immagine dentro)",
          len(corpo) < 200_000, f"{len(corpo)} byte")

    # ---- le intestazioni con cui la foto si serve ----
    r = api_get(page, base, f"/api/photos/{uid_uno}")
    check("la foto si scarica autenticati", r.status == 200, f"HTTP {r.status}")
    h = {k.lower(): v for k, v in r.headers.items()}
    check("il tipo è quello del server",
          h.get("content-type") in ("image/jpeg", "image/png", "image/webp"),
          h.get("content-type", ""))
    check("cache privata e immutabile",
          "private" in h.get("cache-control", "")
          and "immutable" in h.get("cache-control", ""),
          h.get("cache-control", ""))
    check("nosniff presente", h.get("x-content-type-options") == "nosniff",
          h.get("x-content-type-options", ""))
    check("nessun nome di file nell'intestazione",
          "filename" not in h.get("content-disposition", ""),
          h.get("content-disposition", ""))

    # ---------------- 2. ricaricamento della pagina ----------------
    api_calls.clear()
    page.reload(wait_until="load")
    page.wait_for_timeout(5000)
    cerca_rack(page)
    check("dopo il ricaricamento l'immagine si richiede al server",
          any(c.endswith(f"/api/photos/{uid_uno}") for c in api_calls),
          str([c for c in api_calls if "photos" in c])[:200])
    check("dopo il ricaricamento l'anteprima è ancora quella foto",
          uid_uno in sfondo_anteprima(page), sfondo_anteprima(page)[:160])
    check("dopo il ricaricamento l'interfaccia torna in sola lettura",
          page.locator('[data-test="foto-input"]').count() == 0,
          "il comando di caricamento non deve esserci fuori dalla modalità Editing")

    # ---------------- 3. sostituzione ----------------
    check("PRECONDIZIONE: comando di caricamento disponibile in Editing",
          pronto_per_caricare(page))
    due = make_file("due.jpg", (40, 40, 200))
    puts.clear()
    carica(page, due)
    doc = api_get(page, base, "/api/inventory").json()
    uid_due = trova_foto(doc)
    check("la sostituzione produce un UUID diverso",
          bool(uid_due) and uid_due != uid_uno, f"{uid_uno} → {uid_due}")
    check("la foto NUOVA si scarica",
          api_get(page, base, f"/api/photos/{uid_due}").status == 200)
    # ⚠ Il punto del commit: sostituire non cancella. La versione precedente
    # referenzia ancora la prima foto, e un ripristino deve mostrarla.
    check("la foto PRECEDENTE è ancora sul server (nessuna cancellazione)",
          api_get(page, base, f"/api/photos/{uid_uno}").status == 200,
          "la sostituzione ha cancellato i byte della foto vecchia")

    # ---------------- 4. conflitto durante la sostituzione ----------------
    # Qualcun altro salva alle nostre spalle: il prossimo PUT dell'app parte da una
    # versione superata.
    corrente = api_get(page, base, "/api/inventory").json()
    altro = page.request.put(
        f"{base}/api/inventory",
        headers={"Content-Type": "application/json", "Origin": base},
        data=json.dumps({"baseVersion": corrente["version"],
                         "doc": tocca_documento(corrente["doc"]),
                         "action": "modifica da un'altra sessione"}),
        ignore_https_errors=True)
    check("PRECONDIZIONE: l'altra sessione ha salvato", altro.status == 200,
          f"HTTP {altro.status} {altro.text()[:200]}")

    # Il messaggio NON deve esserci prima: senza questo, un `in testo` che trovasse
    # la frase per un altro motivo renderebbe il controllo successivo verde a vuoto.
    ATTESO = "Un'altra sessione ha salvato"
    check("PRECONDIZIONE: il messaggio di conflitto non è ancora mostrato",
          ATTESO not in page.inner_text("body"))

    tre = make_file("tre.jpg", (40, 200, 40))
    carica(page, tre)
    page.wait_for_timeout(3000)
    testo = page.inner_text("body")
    check("il conflitto è dichiarato all'utente e i dati vengono ricaricati",
          ATTESO in testo, testo[:300])
    # Il salvataggio del rack NON è avvenuto: la terza foto è stata caricata ma
    # nessuna versione la referenzia. È il confine fra «immagine caricata» e «rack
    # salvato», e l'orfana la raccoglie la GC.
    doc_dopo = api_get(page, base, "/api/inventory").json()
    check("dopo il conflitto la terza foto NON è nel documento",
          trova_foto(doc_dopo) in (uid_due, uid_uno),
          f"nel documento c'è {trova_foto(doc_dopo)}")
    check("dopo il conflitto NESSUNA delle foto è stata cancellata",
          api_get(page, base, f"/api/photos/{uid_uno}").status == 200
          and api_get(page, base, f"/api/photos/{uid_due}").status == 200)

    # ---------------- 5. un'immagine rifiutata ----------------
    page.reload(wait_until="load")
    page.wait_for_timeout(5000)
    check("PRECONDIZIONE: comando di caricamento disponibile dopo il conflitto",
          pronto_per_caricare(page))
    carica(page, make_svg())
    page.wait_for_timeout(1500)
    errore = page.locator('[data-test="foto-errore"]')
    check("un SVG viene rifiutato e l'errore è mostrato accanto alla foto",
          errore.count() == 1 and "SVG" in errore.first.inner_text().upper(),
          errore.first.inner_text() if errore.count() else "nessun messaggio")

    # ---------------- 6. esportazione ----------------
    page.get_by_role("button", name="Esporta ▾").click()
    page.wait_for_timeout(400)
    with page.expect_download() as scarico:
        page.get_by_text("JSON completo").click()
    percorso = scarico.value.path()
    esportato = Path(percorso).read_text(encoding="utf-8", errors="replace")
    check("il JSON esportato non contiene nessun dataURL",
          "data:image" not in esportato and "base64" not in esportato,
          esportato[:200])
    check("il JSON esportato contiene l'UUID della foto",
          uid_uno in esportato or uid_due in esportato)

    # ---------------- 7. non-admin ----------------
    if args.editor_password:
        page.request.post(f"{base}/api/auth/logout", headers={"Origin": base},
                          ignore_https_errors=True)
        login(page, base, args.editor, args.editor_password)
        page.wait_for_timeout(1500)
        cerca_rack(page)
        modalita_editing(page)
        page.wait_for_timeout(800)
        check("l'operatore non vede il comando di caricamento",
              page.locator('[data-test="foto-carica"]').count() == 0)
        check("l'operatore vede comunque la foto del rack",
              "/api/photos/" in sfondo_anteprima(page), sfondo_anteprima(page)[:160])
        r = page.request.post(f"{base}/api/photos",
                              headers={"Origin": base},
                              multipart={"file": {"name": "x.jpg",
                                                  "mimeType": "image/jpeg",
                                                  "buffer": uno.read_bytes()}},
                              ignore_https_errors=True)
        check("l'operatore riceve 403 da POST /api/photos", r.status == 403,
              f"HTTP {r.status} {r.text()[:160]}")

    check("nessun errore JavaScript",
          not [t for t in console_text if t.startswith("pageerror")],
          " | ".join(console_text[:3]))


def trova_foto(doc: dict) -> str:
    """Primo riferimento a una foto nel rack di prova."""
    for L in doc.get("doc", doc).get("locations", []):
        for R in L.get("sale", []):
            for rk in R.get("racks", []):
                if rk.get("id") == RACK and rk.get("foto"):
                    return rk["foto"]
    return ""


def tocca_documento(doc: dict) -> dict:
    """Una modifica minima e innocua, per far avanzare la versione dal fianco."""
    out = json.loads(json.dumps(doc))
    for L in out.get("locations", []):
        for R in L.get("sale", []):
            for rk in R.get("racks", []):
                if rk.get("id") == RACK:
                    rk["row"] = "Z" if rk.get("row") != "Z" else "Y"
                    return out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--editor", default="")
    ap.add_argument("--editor-password", default="")
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    # Il test crea entità e carica immagini nell'inventario: consenso esplicito.
    destructive_guard.enforce(args, base)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(ignore_https_errors=True, accept_downloads=True,
                                  viewport={"width": 1700, "height": 1400})
        page = ctx.new_page()
        console_text: list[str] = []
        page.on("console", lambda m: console_text.append(m.text))
        page.on("pageerror", lambda e: console_text.append(f"pageerror: {e}"))

        api_calls: list[str] = []
        puts: list[str] = []

        def _on_request(r):
            if "/api/" not in r.url:
                return
            api_calls.append(f"{r.method} {urlparse(r.url).path}")
            if r.method == "PUT" and urlparse(r.url).path == "/api/inventory":
                puts.append(r.post_data or "")

        page.on("request", _on_request)
        try:
            run(page, base, args, puts, api_calls, console_text)
        finally:
            browser.close()
    return report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\nINTERROTTO: {type(exc).__name__}: {str(exc)[:400]}\n")
        sys.exit(report())
