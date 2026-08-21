#!/usr/bin/env python3
"""Misura il comportamento REALE del browser dopo la migrazione (fase 2H, §16).

Non è un test: non ha soglie e non fallisce. È una misura, e serve a rispondere a
quattro domande che si possono solo misurare:

  - quante richieste parte una battitura? (il ritardo funziona, o no?)
  - quanto pesa una risposta? (una vista che scarica un megabyte per disegnare una
    barra è un problema che si vede solo qui)
  - quanto si aspetta all'apertura di una vista?
  - c'è un N+1? — la domanda che il requisito pone per nome: **una richiesta per
    rack** per disegnare la Capacità sarebbe il difetto classico, e si vede contando.

⚠ I numeri di una sola esecuzione su un portatile non sono una prestazione di
produzione. Servono a distinguere «millisecondi» da «secondi» e «una richiesta» da
«cento», che è la differenza che conta a questa scala.

Uso:
    python tools/queries-perf.py --base https://localhost --password <pw>
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    # ⚠ Il termine da cercare è un PARAMETRO: dipende dai dati. Sul seed di produzione
    # `srv` trova quindici dispositivi; sullo scenario del test dell'interfaccia trova
    # zero, e una ricerca senza risultati non misura la ricerca.
    ap.add_argument("--termine", default="srv",
                    help="testo da cercare: deve trovare qualcosa nei dati presenti")
    ap.add_argument("--termine-largo", default="s",
                    help="testo che trova molti risultati, per misurare una pagina piena")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    righe: list[tuple[str, str]] = []

    def nota(quale: str, valore: str) -> None:
        righe.append((quale, valore))
        print(f"  {quale:52s} {valore}")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        # (percorso, durata_ms, byte)
        chiamate: list[tuple[str, float, int]] = []

        def _risposta(resp):
            if "/api/inventory/" not in resp.url:
                return
            try:
                corpo = resp.body()
            except Exception:
                corpo = b""
            # ⚠ `responseEnd` è già un OFFSET in millisecondi da `startTime`, che
            # è un'epoca assoluta. Sottrarli dà l'epoca col segno cambiato — un numero
            # abbastanza assurdo da non poter essere creduto, che è la sola ragione per
            # cui la prima misura non è finita in un rapporto.
            t = resp.request.timing
            durata = float(t["responseEnd"]) if t and t["responseEnd"] >= 0 else -1.0
            percorso = resp.url.split("/api/")[1].split("?")[0]
            chiamate.append((percorso, durata, len(corpo)))

        page.on("response", _risposta)

        def attendi(selettore, limite_ms=15000):
            """Millisecondi fino alla comparsa del contenuto, o `None` se non compare.

            ⚠ Sostituisce un `wait_for_timeout` fisso, che misurava l'attesa che avevo
            scritto io: tremila millisecondi per tutte e tre le viste. Un numero che non
            misura niente è peggio di un numero assente, perché sembra un dato.
            """
            t0 = time.monotonic()
            while (time.monotonic() - t0) * 1000 < limite_ms:
                if page.locator(selettore).count():
                    return (time.monotonic() - t0) * 1000
                page.wait_for_timeout(25)
            return None

        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(2500)
        page.get_by_placeholder("utente", exact=True).fill(args.username)
        page.get_by_placeholder("password", exact=True).fill(args.password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(5000)

        print("\n=== all'avvio ===")
        avvio = list(chiamate)
        nota("richieste di interrogazione all'avvio", str(len(avvio)))
        for percorso, d, n in avvio:
            nota(f"  {percorso}", f"{d:.0f} ms, {n} byte")

        # ---------------- la battitura ----------------
        print("\n=== ricerca: una battitura di 13 caratteri ===")
        cerca = page.locator('[data-test="ricerca"]')
        chiamate.clear()
        testo = args.termine
        t0 = time.monotonic()
        for i in range(1, len(testo) + 1):
            cerca.fill(testo[:i])
            page.wait_for_timeout(40)
        page.wait_for_timeout(2000)
        ricerche = [c for c in chiamate if c[0] == "inventory/search"]
        nota(f"richieste per {len(testo)} caratteri", str(len(ricerche)))
        if ricerche:
            nota("latenza della ricerca (mediana)",
                 f"{statistics.median(d for _, d, _ in ricerche):.0f} ms")
            nota("dimensione della risposta", f"{ricerche[-1][2]} byte")
        nota("tempo totale della battitura", f"{(time.monotonic() - t0) * 1000:.0f} ms")
        if ricerche:
            nota("risultati trovati (dev'essere > 0, altrimenti non misura nulla)",
                 str(page.locator('[data-test="risultato"]').count()))

        # una ricerca larga: quanto pesa una pagina piena
        chiamate.clear()
        cerca.fill("")
        page.wait_for_timeout(400)
        cerca.fill(args.termine_largo)
        page.wait_for_timeout(2500)
        larghe = [c for c in chiamate if c[0] == "inventory/search"]
        if larghe:
            nota("ricerca larga (50 risultati): latenza", f"{larghe[-1][1]:.0f} ms")
            nota("ricerca larga: dimensione", f"{larghe[-1][2]} byte")
            nota("ricerca larga: risultati mostrati",
                 str(page.locator('[data-test="risultato"]').count()))
        # ⚠ La casella si svuota E si aspetta che il menu dei risultati sparisca: un
        # menu ancora aperto intercetta il clic sulle linguette, e la misura fallisce
        # per un motivo che non ha niente a che vedere con le prestazioni.
        cerca.fill("")
        page.keyboard.press("Escape")
        page.wait_for_timeout(1500)

        # ---------------- le tre viste ----------------
        for nome, selettore, atteso, contenuto in (
            ("Capacità", ("button", "Capacità"), "inventory/capacity",
             # ⚠ Un selettore sul TESTO, non sull'esistenza dell'elemento: il div
             # esiste già durante il caricamento (vuoto), e `:not(:empty)` combaciava
             # subito perché un nodo di testo vuoto conta. Misurava sei millisecondi
             # di React, non l'arrivo dei numeri.
             '[data-test="cap-revisione"]:has-text("revisione")'),
            ("Scadenze", ("button", "Scadenze"), "inventory/expiries",
             '[data-test="scad-oggi"]:has-text("oggi")'),
        ):
            print(f"\n=== apertura della vista {nome} ===")
            chiamate.clear()
            t0 = time.monotonic()
            page.get_by_role(selettore[0], name=selettore[1], exact=True).click()
            disegno = attendi(contenuto)
            page.wait_for_timeout(500)
            visto = [c for c in chiamate if c[0] == atteso]
            nota(f"{nome}: richieste", str(len(visto)))
            # ⚠ La domanda del requisito: NON una richiesta per rack.
            nota(f"{nome}: richieste totali di interrogazione", str(len(chiamate)))
            if visto:
                nota(f"{nome}: latenza", f"{visto[-1][1]:.0f} ms")
                nota(f"{nome}: dimensione", f"{visto[-1][2]} byte")
            nota(f"{nome}: dal clic al contenuto sullo schermo",
                 "non comparso" if disegno is None else f"{disegno:.0f} ms")
            page.get_by_role("button", name="Pianta").click()
            page.wait_for_timeout(600)

        print("\n=== apertura della vista Dismessi ===")
        chiamate.clear()
        t0 = time.monotonic()
        page.locator('[data-test="tab-dismessi"]').click()
        disegno = attendi('[data-test="dism-riga"], [data-test="dism-vuota"]')
        page.wait_for_timeout(500)
        nota("Dismessi: richieste", str(len(chiamate)))
        if chiamate:
            nota("Dismessi: latenza", f"{chiamate[-1][1]:.0f} ms")
            nota("Dismessi: dimensione", f"{chiamate[-1][2]} byte")
        nota("Dismessi: dal clic al contenuto sullo schermo",
             "non comparso" if disegno is None else f"{disegno:.0f} ms")

        # ---------------- quante volte si ridisegna senza chiedere ----------------
        print("\n=== interazione sulla pianta (non deve chiedere niente) ===")
        page.get_by_role("button", name="Pianta").click()
        page.wait_for_timeout(1200)
        chiamate.clear()
        for _ in range(20):
            page.mouse.move(700, 500)
            page.mouse.move(760, 540)
        page.wait_for_timeout(1500)
        nota("richieste durante 40 movimenti del mouse", str(len(chiamate)))

        print("\n=== confronto con l'inventario intero ===")
        peso = page.evaluate("""
          async () => {
            const t0 = performance.now();
            const r = await fetch('/api/inventory', { credentials: 'same-origin' });
            const t = await r.text();
            return { ms: Math.round(performance.now() - t0), bytes: t.length };
          }
        """)
        nota("GET /api/inventory (documento intero)",
             f"{peso['ms']} ms, {peso['bytes']} byte")

        browser.close()

    print("\n" + "=" * 78)
    print(json.dumps(dict(righe), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
