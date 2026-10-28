#!/usr/bin/env python3
"""Camera bianca: una installazione NUOVA, dal database vuoto al browser.

La sequenza di §23 del requisito della fase 2I, percorsa sullo stack VERO — Compose,
nginx, TLS, l'immagine costruita, un browser Chrome — e non su un doppio. I passi da
1 a 7 (ruoli, migrazioni, `--check`, `--init`, cattura della provvisoria) li fa
`tools/run-clean-room-test.ps1`, che è anche l'unico posto dove la provvisoria passa
di mano; da qui in avanti li fa questo file.

Che cosa prova che le suite non provano
--------------------------------------
`test_baseline_pg.py` verifica lo stato del database dopo l'inizializzazione, e lo
verifica meglio di quanto un browser potrà mai fare. Quello che non può verificare è
se l'APPLICAZIONE è usabile su quello stato — e la fase 2H ha già mostrato due volte
che la differenza è reale: un modulo dimenticato nell'allowlist di nginx e una sala
con `vani: []` avevano superato tutte le suite e rotto l'applicazione nel browser.

Una baseline è precisamente il caso in cui questo rischio è massimo, perché è lo stato
che NESSUNO ha mai guardato: zero dispositivi, zero foto, zero scadenze, zero
dismessi. Ogni vista dell'applicazione viene esercitata qui per la prima volta sul suo
insieme vuoto.

⚠ La provvisoria arriva in `TSM_CLEANROOM_PASSWORD`, non su una riga di comando: una
riga di comando finisce nella cronologia della shell e nell'elenco dei processi.

Uso (di norma attraverso il runner):
    TSM_CLEANROOM_PASSWORD=… python tools/clean-room-test.py \\
        --base https://localhost --allow-destructive
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import destructive_guard                                        # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

#: Digest della baseline approvata. Duplicato qui di proposito: se il test lo leggesse
#: da `app/inventory/baseline.py` proverebbe che il codice è d'accordo con se stesso.
#: Questo numero l'ha letto una persona nel manifesto.
BASELINE_SHA = "febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"

#: Struttura attesa, scritta a mano dal manifesto. Se cambia la struttura di
#: produzione, questo test diventa rosso — ed è il comportamento voluto: una modifica
#: della produzione va guardata anche da qui.
ATTESI = {"locations": 3, "rooms": 6, "racks": 102, "devices": 0}

#: Nomi di dispositivo del seed di SVILUPPO. Non devono comparire in nessun posto: è
#: il controllo che §25 chiede per nome, e l'unico modo di farlo è cercarli.
NOMI_DI_SVILUPPO = ("fw-01", "sw-core-01", "srv-web-01", "srv-db-01", "nas-01",
                    "srv-vm-01", "ups-01", "san-01", "srv-bck-01")
#: E un seriale, che è il tipo di dato che sopravvive a una ripulitura incompleta.
SERIALI_DI_SVILUPPO = ("FG2-8841", "FCW2231", "SN-7HQ2K", "SYN-2210")

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


JS_GET = """
async (url) => {
  const r = await fetch(url, { credentials: 'same-origin' });
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return { status: r.status, json: body };
}
"""

JS_PUT = """
async (doc) => {
  const cur = await fetch('/api/inventory', { credentials: 'same-origin' });
  if (!cur.ok) return { status: cur.status, body: 'GET fallita' };
  const j = await cur.json();
  const res = await fetch('/api/inventory', {
    method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseVersion: j.version, doc }),
  });
  return { status: res.status, body: (await res.text()).slice(0, 300) };
}
"""


def conta_entita(doc: dict) -> dict:
    n = {"locations": 0, "rooms": 0, "racks": 0, "devices": 0}
    for L in doc.get("locations") or []:
        n["locations"] += 1
        for R in L.get("sale") or []:
            n["rooms"] += 1
            for K in R.get("racks") or []:
                n["racks"] += 1
                n["devices"] += len(K.get("devices") or [])
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--new-password", default="TrustServerManager-2I-camera")
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    provvisoria = os.environ.get("TSM_CLEANROOM_PASSWORD", "")
    if not provvisoria:
        sys.exit("TSM_CLEANROOM_PASSWORD non impostata: la provvisoria la passa il "
                 "runner, e non su una riga di comando.")

    destructive_guard.enforce(args, base)
    errori_js: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.on("console",
                lambda m: errori_js.append(m.text) if m.type == "error" else None)
        try:
            esegui(page, base, args, provvisoria)
        except Exception as exc:                                # noqa: BLE001
            # ⚠ Come nella fase 2H: un'eccezione a metà non deve buttare via i
            # risultati già raccolti. Sapere QUALI passi erano verdi prima del guasto
            # è metà della diagnosi.
            check(f"ESECUZIONE INTERROTTA: {type(exc).__name__}", False, str(exc)[:400])
        finally:
            context.close()
            browser.close()

    # --------------------------------------------------------------- rapporto
    print("\n" + "=" * 74)
    passati = sum(1 for _n, ok, _d in results if ok)
    for nome, ok, det in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {nome}")
        if not ok and det:
            print(f"       → {det}")
    print("-" * 74)
    print(f"controlli passati: {passati}/{len(results)}")

    # ⚠ Si tengono gli errori di JAVASCRIPT e si scartano i codici di stato HTTP.
    #
    # Non è indulgenza: questo test PROVOCA di proposito un 403 (la sonda con la
    # sessione ristretta) e un 401 (l'accesso rifiutato con la provvisoria dopo il
    # cambio), e quelle due righe sono le asserzioni, non i difetti. Il browser le
    # riporta come «Failed to load resource», che dice molto meno di quanto dicano
    # le asserzioni corrispondenti.
    #
    # Quello che va preso è ciò che la fase 2H ha visto rompere l'applicazione: un
    # `TypeError` durante il render, che nessuna suite aveva colto.
    RUMORE = ("favicon", "failed to load resource", "the server responded with")
    gravi = [e for e in errori_js
             if not any(r in e.lower() for r in RUMORE)]
    if gravi:
        print(f"\n⚠ {len(gravi)} errori di console nella pagina:")
        for e in gravi[:10]:
            print(f"  - {e[:200]}")

    if passati != len(results) or gravi:
        print("\nRISULTATO: la camera bianca NON è pulita")
        return 1
    print("\nRISULTATO: una installazione nuova è utilizzabile e non contiene "
          "dati di sviluppo")
    return 0


def esegui(page, base, args, provvisoria: str) -> None:
    # ==============================================================
    # 8. accesso con la provvisoria
    # ==============================================================
    page.goto(base + "/", wait_until="load")
    page.wait_for_timeout(2500)
    check("la pagina di accesso si carica",
          page.get_by_placeholder("utente", exact=True).count() == 1)

    page.get_by_placeholder("utente", exact=True).fill(args.username)
    page.get_by_placeholder("password", exact=True).fill(provvisoria)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(3000)

    # ==============================================================
    # 9. la sessione ristretta NON può usare l'inventario
    # ==============================================================
    #
    # È la difesa di §8.26, e su una installazione nuova è il primo momento in cui
    # conta: l'amministratore appena creato ha una password che qualcuno ha letto su
    # un terminale, e finché non la cambia non deve poter fare altro.
    check("la provvisoria porta allo schermo del cambio password",
          page.get_by_text("Imposta una password personale").count() == 1,
          "atteso lo schermo del cambio password")

    esito = page.evaluate(JS_GET, "/api/inventory")
    check("con la sessione ristretta l'inventario risponde 403",
          esito["status"] == 403, f"HTTP {esito['status']}")
    codice = ((esito["json"] or {}).get("error") or {}).get("code") \
        if isinstance(esito["json"], dict) else None
    check("e il codice è password_change_required",
          codice == "password_change_required" or
          "password_change_required" in json.dumps(esito["json"] or {}),
          f"corpo: {json.dumps(esito['json'])[:200]}")

    # Il token della sessione RISTRETTA, preso prima del cambio: serve fra poco per
    # provare che il cambio l'ha revocata. §8 della 2J lo chiede per nome («prove
    # all old sessions invalidated»), e non è la stessa cosa che provare la
    # provvisoria: chi ha rubato il cookie non ha bisogno della password.
    cookie_prima = next((c["value"] for c in page.context.cookies()
                         if c["name"] == "tsm_session"), None)

    # ==============================================================
    # 10-11. cambio password e accesso nuovo
    # ==============================================================
    # Dalla 0.4.1 la provvisoria non si ridigita dopo il login: il campo
    # compare solo se la pagina è stata ricaricata.
    if page.get_by_placeholder("password attuale").count():
        page.get_by_placeholder("password attuale").fill(provvisoria)
    page.get_by_placeholder("nuova password (min. 12 caratteri)").fill(args.new_password)
    page.get_by_placeholder("ripeti la nuova password").fill(args.new_password)
    page.get_by_role("button", name="Cambia password").click()
    page.wait_for_timeout(3000)

    if page.get_by_placeholder("utente", exact=True).count():
        page.get_by_placeholder("utente", exact=True).fill(args.username)
        page.get_by_placeholder("password", exact=True).fill(args.new_password)
        page.get_by_role("button", name="Accedi").click()
        page.wait_for_timeout(3500)
    check("dopo il cambio si rientra con la password nuova",
          page.get_by_placeholder("utente", exact=True).count() == 0,
          "la schermata di accesso è ancora là")

    # La provvisoria non deve più funzionare: se funzionasse, il valore letto su un
    # terminale resterebbe una credenziale valida per sempre.
    esito = page.evaluate("""
      async (pw) => {
        const r = await fetch('/api/auth/login', {
          method: 'POST', credentials: 'omit',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: 'admin', password: pw }),
        });
        return r.status;
      }
    """, provvisoria)
    check("la provvisoria NON funziona più dopo il cambio", esito != 200,
          f"HTTP {esito}")

    # E il COOKIE di prima non deve più valere. Si prova in una scheda pulita, con
    # quel solo cookie impostato a mano: usare la scheda corrente proverebbe che il
    # cookie NUOVO funziona, che è un'altra affermazione.
    if cookie_prima:
        pagina2 = page.context.browser.new_context(ignore_https_errors=True)
        pagina2.add_cookies([{"name": "tsm_session", "value": cookie_prima,
                              "url": base}])
        p2 = pagina2.new_page()
        p2.goto(f"{base}/", wait_until="domcontentloaded")
        stato = p2.evaluate("""
          async () => (await fetch('/api/auth/me', {credentials: 'include'})).status
        """)
        check("il cookie di sessione di PRIMA del cambio è stato revocato",
              stato in (401, 403), f"HTTP {stato}: la sessione vecchia vive ancora")
        pagina2.close()
    else:
        check("il cookie di sessione di PRIMA del cambio è stato revocato", False,
              "non ho trovato il cookie tsm_session prima del cambio")

    # ==============================================================
    # 12. l'inventario si carica, ed è la baseline
    # ==============================================================
    esito = page.evaluate(JS_GET, "/api/inventory")
    check("l'inventario si carica", esito["status"] == 200, f"HTTP {esito['status']}")
    if esito["status"] != 200:
        return
    inv = esito["json"]
    check("è la versione 1", inv.get("version") == 1, f"versione {inv.get('version')}")
    check("il digest è quello della baseline APPROVATA",
          inv.get("sha256") == BASELINE_SHA,
          f"{inv.get('sha256')} invece di {BASELINE_SHA}")

    doc = inv.get("doc") or {}
    n = conta_entita(doc)
    check(f"la struttura è quella attesa ({ATTESI})", n == ATTESI, f"trovata {n}")

    # ==============================================================
    # 13. tutti i rack sono vuoti
    # ==============================================================
    non_vuoti = [K.get("name") for L in doc.get("locations") or []
                 for R in L.get("sale") or [] for K in R.get("racks") or []
                 if K.get("devices")]
    check("ogni rack è vuoto", not non_vuoti, f"non vuoti: {non_vuoti[:5]}")
    check("nessuna voce di manuale", not doc.get("manuale"),
          f"manuale: {doc.get('manuale')}")

    # Nessuna traccia del seed di sviluppo, nel DOCUMENTO.
    testo_doc = json.dumps(doc, ensure_ascii=False)
    intrusi = [x for x in NOMI_DI_SVILUPPO + SERIALI_DI_SVILUPPO if x in testo_doc]
    check("nessun dato del seed di sviluppo nel documento", not intrusi,
          f"trovati: {intrusi}")

    # Nessuna FOTO: né riferimenti nel documento né byte serviti. §8 della 2J lo
    # chiede accanto a «nessun dispositivo di prova», e per una ragione: una foto
    # di sviluppo sopravvissuta sarebbe l'unico dato del vecchio ambiente ancora
    # visibile a un operatore, e nessuna vista dell'inventario la mostrerebbe come
    # anomala.
    check("nessun riferimento a foto nel documento", '"foto"' not in testo_doc,
          "il documento nomina una foto: la tabella photos nasce vuota")
    esito_foto = page.evaluate(JS_GET, "/api/photos/00000000-0000-0000-0000-000000000000")
    check("una foto qualunque non esiste (l'archivio è vuoto)",
          esito_foto["status"] in (404, 400, 422),
          f"HTTP {esito_foto['status']}")

    # I seriali degli ARMADI invece devono esserci: sono metadato di produzione, e
    # confonderli coi seriali dei dispositivi è l'errore contro cui §6 mette in
    # guardia. Il controllo positivo sta accanto a quello negativo di proposito.
    check("i seriali degli armadi sono conservati", "2020047937" in testo_doc,
          "il numero di inventario di un armadio è sparito")

    # ==============================================================
    # 14-16. le viste, sul loro insieme VUOTO (§25)
    # ==============================================================
    page.reload(wait_until="load")
    page.wait_for_timeout(3500)

    # --- la pianta ---
    check("la pianta si disegna", page.get_by_role(
        "button", name="Pianta").count() >= 1)
    corpo = page.inner_text("body")
    check("i tre siti compaiono nell'interfaccia",
          all(s in corpo for s in ("Pomezia", "Oriolo")),
          "i nomi dei siti non compaiono")

    # ⚠ OGNI sala della baseline viene disegnata, una per una, e questa è la prima
    # volta che quelle geometrie vere finiscono su uno schermo con un database nuovo.
    # Nella fase 2H una sala con `vani: []` faceva morire il render di TUTTA
    # l'applicazione, e nessuna suite l'aveva visto.
    #
    # Si naviga cercando un rack e cliccando il risultato, che è il cammino di un
    # utente vero. Cliccare il nome della sala non funziona: «H0» combacia anche con
    # l'etichetta del SITO («Pomezia — H0»), che è uno span nascosto, e il click va
    # in timeout su un elemento invisibile.
    cerca_nav = page.locator('[data-test="ricerca"]')
    SALE = (("Backend", "Rack R01"), ("Centro Stella", "Rack CS-R01"),
            ("Frontend", "Rack FE-R01"), ("Saletta Nuova", "Rack SN-R01"),
            ("H0", "Rack H0-R01"), ("Sala Oriolo", "Rack RO-A01"))
    disegnate = []
    for nome_sala, rack in SALE:
        cerca_nav.fill("")
        page.wait_for_timeout(250)
        cerca_nav.fill(rack)
        page.wait_for_timeout(1400)
        if page.locator('[data-test="risultato"]').count():
            page.locator('[data-test="risultato"]').first.click()
            page.wait_for_timeout(900)
            if nome_sala in page.inner_text("body"):
                disegnate.append(nome_sala)
    cerca_nav.fill("")
    page.wait_for_timeout(500)
    check("tutte e sei le sale della baseline si aprono e si disegnano",
          len(disegnate) == 6, f"aperte {len(disegnate)}/6: {disegnate}")

    # E i rack VUOTI si disegnano: la vista del rack esiste e non mostra apparati.
    check("un rack vuoto si disegna senza apparati",
          page.locator('[data-test="rack-rimossi"]').count() == 0,
          "compare la nota dei rimossi in un rack che non ha nessun apparato")

    # --- ricerca: stato vuoto, e una ricerca che trova ---
    cerca = page.locator('[data-test="ricerca"]')
    check("la casella di ricerca esiste", cerca.count() == 1)
    if cerca.count():
        cerca.fill("zzz-non-esiste-zzz")
        page.wait_for_timeout(1600)
        check("una ricerca senza risultati mostra lo stato vuoto",
              page.locator('[data-test="ricerca-vuota"]').count() == 1,
              "nessuno stato vuoto")
        # E una che trova: su una baseline i rack esistono, i dispositivi no.
        cerca.fill("Rack R01")
        page.wait_for_timeout(1600)
        trovati = page.locator('[data-test="risultato"]').count()
        check("la ricerca trova un RACK della baseline", trovati >= 1,
              f"{trovati} risultati")
        cerca.fill("srv-")
        page.wait_for_timeout(1600)
        check("e non trova nessun DISPOSITIVO (non ce ne sono)",
              page.locator('[data-test="ricerca-vuota"]').count() == 1,
              f"{page.locator('[data-test=\"risultato\"]').count()} risultati per «srv-»")
        cerca.fill("")
        page.wait_for_timeout(800)

    # --- capacità: zero U usate ---
    page.get_by_role("button", name="Capacità", exact=True).click()
    page.wait_for_timeout(2200)
    testo_cap = page.inner_text("body")
    check("la vista Capacità si carica senza errore",
          page.locator('[data-test="cap-errore"]').count() == 0,
          page.locator('[data-test="cap-errore"]').inner_text()
          if page.locator('[data-test="cap-errore"]').count() else "")
    esito = page.evaluate(JS_GET, "/api/inventory/capacity")
    if esito["status"] == 200:
        loc = esito["json"].get("locations") or []
        usate = sum(r["usedU"] for L in loc for room in L["rooms"]
                    for r in room["racks"])
        totali = sum((r["u"] or 0) for L in loc for room in L["rooms"]
                     for r in room["racks"])
        check("le U usate sono ZERO su tutta l'installazione", usate == 0,
              f"{usate} U usate")
        check("e le U totali no: i rack esistono", totali > 4000,
              f"{totali} U totali")
        check("la vista mostra 0%", "0%" in testo_cap, "nessun 0% nella pagina")
    else:
        check("la capacità risponde", False, f"HTTP {esito['status']}")

    # --- scadenze: stato vuoto ---
    page.get_by_role("button", name="Scadenze").click()
    page.wait_for_timeout(2200)
    check("la vista Scadenze mostra lo stato vuoto",
          page.locator('[data-test="scad-vuota"]').count() == 1,
          "nessuno stato vuoto in Scadenze")
    check("e non mostra un errore",
          page.locator('[data-test="scad-errore"]').count() == 0)

    # --- dismessi: stato vuoto ---
    page.locator('[data-test="tab-dismessi"]').click()
    page.wait_for_timeout(2200)
    check("la vista Dismessi si apre",
          page.locator('[data-test="vista-dismessi"]').count() == 1)
    check("e mostra lo stato vuoto",
          page.locator('[data-test="dism-vuota"]').count() == 1,
          "nessuno stato vuoto in Dismessi")

    # --- nessun dato di sviluppo in NESSUNA vista ---
    corpo = page.inner_text("body")
    intrusi = [x for x in NOMI_DI_SVILUPPO if x in corpo]
    check("nessun nome del seed di sviluppo compare nell'interfaccia",
          not intrusi, f"trovati: {intrusi}")

    # ==============================================================
    # 17-19. la prima modifica vera: la versione 2
    # ==============================================================
    page.get_by_role("button", name="Pianta").click()
    page.wait_for_timeout(1200)

    doc2 = json.loads(json.dumps(doc))
    primo_rack = doc2["locations"][0]["sale"][0]["racks"][0]
    primo_rack["devices"] = [{
        "_uid": "0f0f0f0f-0000-4000-8000-000000000001",
        "id": "SRV-CAMERA-01", "name": "srv-camera-01", "type": "server",
        "u": 10, "h": 2, "stato": "attivo", "presenza": "presente",
        "garanzia": "2027-06-30",
    }]
    # ⚠ E una sala con `vani: []`, che è il default canonico e la forma che nella
    # fase 2H faceva morire il render. Una installazione nuova è esattamente il posto
    # dove un operatore crea la prima sala senza planimetria.
    doc2["locations"][0]["sale"].append({
        "_uid": "0f0f0f0f-0000-4000-8000-0000000000b1",
        "id": "sala-senza-planimetria", "nome": "Sala nuova senza planimetria",
        "w": 6, "h": 4, "area": "", "dim": "", "segnaposto": False,
        "vani": [], "racks": [],
    })

    esito = page.evaluate(JS_PUT, doc2)
    check("la prima modifica si salva", esito["status"] == 200,
          f"HTTP {esito['status']}: {esito['body']}")
    if esito["status"] != 200:
        return

    esito = page.evaluate(JS_GET, "/api/inventory")
    check("la prima modifica è la VERSIONE 2", esito["json"].get("version") == 2,
          f"versione {esito['json'].get('version')}")
    check("e il digest non è più quello della baseline",
          esito["json"].get("sha256") != BASELINE_SHA)

    page.reload(wait_until="load")
    page.wait_for_timeout(3500)
    corpo = page.inner_text("body")
    check("una sala con `vani: []` si disegna senza far cadere l'applicazione",
          "Sala nuova senza planimetria" in corpo or
          page.get_by_role("button", name="Pianta").count() >= 1,
          "l'applicazione non ha renderizzato dopo l'aggiunta")

    esito = page.evaluate(JS_GET, "/api/inventory/capacity")
    if esito["status"] == 200:
        loc = esito["json"].get("locations") or []
        usate = sum(r["usedU"] for L in loc for room in L["rooms"]
                    for r in room["racks"])
        check("la capacità adesso conta 2 U usate", usate == 2, f"{usate} U")

    # La proiezione regge: la readiness è la stessa domanda vista da fuori.
    esito = page.evaluate(JS_GET, "/api/ready")
    check("la readiness resta verde dopo la modifica",
          esito["status"] == 200 and esito["json"].get("projection") == "ok",
          json.dumps(esito["json"])[:200])

    # ==============================================================
    # 20. utenze e registro
    # ==============================================================
    esito = page.evaluate(JS_GET, "/api/users")
    if esito["status"] == 200:
        utenti = esito["json"] if isinstance(esito["json"], list) \
            else esito["json"].get("users") or []
        check("esiste UNA sola utenza", len(utenti) == 1, f"{len(utenti)} utenze")
        if utenti:
            check("ed è l'amministratore del bootstrap",
                  utenti[0].get("username") == "admin"
                  and utenti[0].get("role") == "admin",
                  json.dumps(utenti[0])[:200])
    else:
        check("l'elenco utenze risponde", False, f"HTTP {esito['status']}")

    esito = page.evaluate(JS_GET, "/api/audit?limit=50")
    if esito["status"] == 200:
        voci = esito["json"].get("items") or []
        # ⚠ Il campo si chiama `event`, non `action`: `action` è il nome della COLONNA,
        # e la rotta lo rinomina di proposito (l'evento autorevole lo calcola il
        # server, §8.36).
        eventi = [v.get("event") for v in voci]
        # L'insieme ESATTO, non un conteggio parziale: dice sia che non c'è niente di
        # prima, sia che c'è tutto quello che questo test ha fatto.
        atteso = sorted([
            "bootstrap",                  # l'inizializzazione della baseline
            "auth.login.success",         # accesso con la provvisoria
            "auth.password.changed",      # il cambio obbligatorio
            "auth.login.success",         # il rientro
            "auth.login.failure",         # la provvisoria che non funziona più
            "inventory.save",             # la prima modifica
        ])
        check("il registro contiene ESATTAMENTE la storia di questa installazione",
              sorted(eventi) == atteso, f"trovati: {sorted(eventi)}")
        check("l'inizializzazione della baseline è la voce di versione 1",
              any(v.get("event") == "bootstrap" and v.get("inventoryVersion") == 1
                  for v in voci),
              f"{[(v.get('event'), v.get('inventoryVersion')) for v in voci]}")
        check("la prima modifica è la voce di versione 2",
              any(v.get("event") == "inventory.save"
                  and v.get("inventoryVersion") == 2 for v in voci))
        # Il registro di sicurezza funziona dal primo minuto: il tentativo con la
        # provvisoria scaduta è registrato, ed è la riga che qualcuno cercherebbe.
        check("il tentativo con la provvisoria scaduta è REGISTRATO",
              "auth.login.failure" in eventi)
        # E nessuna voce nomina un attore che non esiste su questa installazione.
        attori = {(v.get("actor") or {}).get("username") for v in voci}
        check("ogni voce è attribuita all'unico amministratore",
              attori == {args.username}, f"attori: {attori}")
    else:
        check("il registro risponde", False, f"HTTP {esito['status']}")


if __name__ == "__main__":
    sys.exit(main())
