#!/usr/bin/env python3
"""Le viste interrogate dal server, in un browser vero, attraverso nginx (fase 2H).

Perché un file a parte da `browser-e2e-test.py`: quello prova la CATENA — TLS, cookie,
redirect, allowlist statica, un salvataggio reale — su un seed di produzione. Questo
prova la SEMANTICA delle tre viste migrate, e per farlo ha bisogno di uno scenario
costruito: una sovrapposizione, un rack la cui fila è letteralmente «—», un
`dismesso + presente` accanto a un `dismesso + rimosso`, `10.0.0.1` accanto a
`10.0.0.100`, una data illeggibile. Mescolare le due cose avrebbe reso il test della
catena dipendente da dati che il seed non ha.

⚠ Lo scenario si installa con una `fetch` eseguita DENTRO la pagina, non con un client
HTTP del test. Due ragioni, entrambe pratiche: il browser manda il cookie di sessione e
l'`Origin` da sé — e l'`Origin` è validato (§8.27), quindi un client esterno riceverebbe
403 e il test finirebbe a discutere di intestazioni invece di viste.

⚠ Quello che questo file NON fa: non verifica che i numeri siano quelli giusti *in
astratto*. Quello lo fanno le fixture language-neutral, da entrambi i lati. Qui si
verifica che l'INTERFACCIA mostri ciò che il server ha calcolato — che è la domanda
propria della fase 2H, e l'unica che un browser può rispondere.

Prerequisiti: come browser-e2e-test.py (stack in piedi, seed inizializzato).

Uso:
    python tools/queries-ui-test.py --base https://localhost --password <pw>
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import destructive_guard

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


# ==================================================================
# lo scenario
# ==================================================================
#
# Ogni rack risponde a UNA domanda, e il commento dice quale. Un documento di prova in
# cui non si sa più che cosa dimostri ogni riga è un documento che si aggiusta finché
# passa.

def uid(prefix: str, n: int) -> str:
    return f"{(prefix * 8)[:8]}-0000-4000-8000-{n:012d}"


def dev(n: int, **campi) -> dict:
    d = {"_uid": uid("d", n), "id": campi.pop("id", f"d{n}"), "u": 1, "h": 1}
    d.update(campi)
    return d


def scenario(oggi: date) -> dict:
    fra10 = (oggi + timedelta(days=10)).isoformat()
    fra200 = (oggi + timedelta(days=200)).isoformat()

    racks = [
        # 1. SOVRAPPOSIZIONE: due apparati che condividono degli slot. Slot 2..6 = 5 U.
        #    `SUM(h)` direbbe 7, ed è la differenza che il test misura.
        {"_uid": uid("c", 1), "id": "R-OVER", "name": "Sovrapposti", "row": "A", "u": 10,
         "devices": [dev(101, id="ov-1", name="ov-1", u=2, h=3),
                     dev(102, id="ov-2", name="ov-2", u=3, h=4)]},

        # 2. FILA «—» LETTERALE: il rack CS-Q01 del seed di produzione ha questa forma.
        #    Deve formare un gruppo PROPRIO, distinto da chi non ha fila.
        {"_uid": uid("c", 2), "id": "R-TRATTINO", "name": "Fila trattino", "row": "—",
         "u": 8, "devices": [dev(201, id="tr-1", name="tr-1", u=1, h=1)]},

        # 3. NESSUNA FILA: l'altro lato della collisione. 1/8 = 12,5% → 13 HALF-UP.
        {"_uid": uid("c", 3), "id": "R-SENZAFILA", "name": "Senza fila", "u": 8,
         "devices": [dev(301, id="sf-1", name="sf-1", u=1, h=1)]},

        # 4. DISMESSI: `presente` occupa 2 U, `rimosso` non occupa niente. Stesso stato
        #    operativo, presenza fisica diversa: è la separazione della 2G, e qui si
        #    vede sul numero.
        {"_uid": uid("c", 4), "id": "R-DISM", "name": "Dismessi", "row": "D", "u": 10,
         "devices": [
             dev(401, id="dism-presente", name="dism-presente", u=1, h=2,
                 stato="dismesso", presenza="presente", serial="SN-PRESENTE-1",
                 model="ProLiant DL360", owner="Rossi"),
             dev(402, id="dism-rimosso", name="dism-rimosso", u=5, h=2,
                 stato="dismesso", presenza="rimosso", serial="SN-RIMOSSO-2",
                 model="PowerEdge R640", owner="Bianchi",
                 note="portato via il 2026-05-01"),
         ]},

        # 5. INDIRIZZI: `10.0.0.1` non deve trovare `10.0.0.100`. E un IPv6 esatto.
        {"_uid": uid("c", 5), "id": "R-RETE", "name": "Rete", "row": "E", "u": 10,
         "devices": [
             dev(501, id="ip-uno", name="ip-uno", ip="10.0.0.1", u=1),
             dev(502, id="ip-cento", name="ip-cento", ip="10.0.0.100", u=2),
             dev(503, id="ip-sei", name="ip-sei", ip="2001:db8::1", u=3),
             # `%` è un carattere normale, non un jolly di LIKE.
             dev(504, id="pct", name="srv%web", u=4),
             # Unicode: non deve inciampare né in ricerca né nel confronto.
             dev(505, id="uni", name="serveur-éàü", u=5),
         ]},

        # 6. SCADENZE: le quattro risposte possibili più quella che NON è una data.
        {"_uid": uid("c", 6), "id": "R-SCAD", "name": "Scadenze", "row": "F", "u": 20,
         "devices": [
             dev(601, id="scad-passata", name="scad-passata", u=1,
                 garanzia="2020-01-01"),
             dev(602, id="scad-oggi", name="scad-oggi", u=2,
                 garanzia=oggi.isoformat()),
             dev(603, id="scad-vicina", name="scad-vicina", u=3, garanzia=fra10),
             dev(604, id="scad-lontana", name="scad-lontana", u=4, garanzia=fra200),
             # Dismesso: COMPARE nell'elenco (è ispezionabile) e non è avvisabile.
             dev(605, id="scad-dismesso", name="scad-dismesso", u=5, garanzia=fra10,
                 stato="dismesso"),
             # ⚠ Il valore illeggibile: `new Date` lo accettava, il parser no. Resta
             # scritto nel documento e NON compare come data.
             dev(606, id="scad-illeggibile", name="scad-illeggibile", u=6,
                 supporto="March 15, 2027"),
             # E la data che non esiste: V8 la faceva scorrere al 2 marzo.
             dev(607, id="scad-inesistente", name="scad-inesistente", u=7,
                 garanzia="2027-02-30"),
         ]},

        # 7. PAGINAZIONE: 60 apparati con un prefisso comune. Il limite della ricerca è
        #    50, quindi «carica altri» deve esistere e funzionare.
        {"_uid": uid("c", 7), "id": "R-PAGINE", "name": "Pagine", "row": "G", "u": 45,
         "devices": [dev(1000 + i, id=f"pag-{i:03d}", name=f"pag-{i:03d}",
                         u=(i % 45) + 1, h=1) for i in range(60)]},
    ]
    # ⚠ La GEOMETRIA si aggiunge qui, in un posto solo: `x`, `y`, `w`, `h` non hanno un
    # default canonico (a differenza di `u`, `name`, `row`), quindi un documento che le
    # omette è accettato dal server e poi disegnato male. Una griglia: i rack non si
    # sovrappongono sulla pianta, che rende leggibile uno screenshot quando un test
    # fallisce.
    for i, k in enumerate(racks):
        k.setdefault("w", 0.6)
        k.setdefault("h", 0.65)
        k.setdefault("x", 0.5 + (i % 4) * 2.0)
        k.setdefault("y", 0.5 + (i // 4) * 2.0)
    return {
        "schemaVersion": 1,
        "locations": [{
            "_uid": uid("a", 1), "id": "sito-2h", "nome": "Sito 2H",
            # ⚠ Un vano esplicito. `vani: []` è il default canonico della sala, ed è la
            # forma che ha fatto emergere il difetto del render (corretto): il test
            # però deve somigliare a un documento che l'applicazione produce, e
            # l'applicazione crea sempre almeno un vano.
            "sale": [{"_uid": uid("b", 1), "id": "sala-2h", "nome": "Sala 2H",
                      "w": 20, "h": 12,
                      "vani": [{"x": 0, "y": 0, "w": 20, "h": 12}],
                      "racks": racks}],
        }],
    }


# ==================================================================
# aiuti nel browser
# ==================================================================

JS_PUT = """
async (doc) => {
  const cur = await fetch('/api/inventory', { credentials: 'same-origin' });
  const j = await cur.json();
  const res = await fetch('/api/inventory', {
    method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ baseVersion: j.version, doc }),
  });
  const body = await res.text();
  return { status: res.status, body: body.slice(0, 400) };
}
"""

JS_GET_JSON = """
async (url) => {
  const r = await fetch(url, { credentials: 'same-origin' });
  return { status: r.status, json: r.ok ? await r.json() : null };
}
"""


def accedi(page, base, args):
    """Login, con il cambio password se la sessione è ristretta."""
    page.goto(base + "/", wait_until="load")
    page.wait_for_timeout(2500)
    page.get_by_placeholder("utente", exact=True).fill(args.username)
    page.get_by_placeholder("password", exact=True).fill(args.password)
    page.get_by_role("button", name="Accedi").click()
    page.wait_for_timeout(3000)
    # ⚠ La sessione con password provvisoria è RISTRETTA: può solo cambiarla (§8.26).
    # Fino al cambio anche la `PUT` dello scenario riceve 403
    # `password_change_required` — che è la difesa che funziona, e il test deve
    # passarci attraverso invece di aggirarla.
    if page.get_by_text("Imposta una password personale").count():
        nuova = args.new_password or "TrustServerManager-2H-prova"
        page.get_by_placeholder("password attuale").fill(args.password)
        page.get_by_placeholder("nuova password", exact=False).fill(nuova)
        page.get_by_role("button", name="Cambia password").click()
        page.wait_for_timeout(3000)
        # Dopo il cambio si torna al login: si rientra con la password nuova.
        if page.get_by_placeholder("utente", exact=True).count():
            page.get_by_placeholder("utente", exact=True).fill(args.username)
            page.get_by_placeholder("password", exact=True).fill(nuova)
            page.get_by_role("button", name="Accedi").click()
            page.wait_for_timeout(3500)
        args.password = nuova
        print(f"    (password cambiata in: {nuova})")
    page.wait_for_timeout(1500)


def prova(errori_condivisi: list) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    ap.add_argument("--new-password", default="")
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        errori_js = errori_condivisi
        page.on("console", lambda m: errori_js.append(m.text) if m.type == "error" else None)
        richieste: list[str] = []
        page.on("request", lambda r: richieste.append(r.url) if "/api/inventory/" in r.url else None)

        accedi(page, base, args)

        # ------------------------------------------------ 0. lo scenario
        # `today` viene dal SERVER: il fuso è configurato (§8.38) e la data del test
        # non è necessariamente la stessa. Prendendola dall'endpoint, «oggi» significa
        # la stessa cosa qui e là — che è precisamente il punto di quella scelta.
        out = page.evaluate(JS_GET_JSON, "/api/inventory/expiries?limit=1")
        oggi = date.fromisoformat(out["json"]["today"]) if out["json"] else date.today()
        esito = page.evaluate(JS_PUT, scenario(oggi))
        check("lo scenario si installa con una PUT", esito["status"] == 200,
              f"HTTP {esito['status']}: {esito['body']}")
        if esito["status"] != 200:
            return
        page.reload(wait_until="load")
        page.wait_for_timeout(3500)

        # ==============================================================
        # 1. RICERCA
        # ==============================================================
        cerca = page.locator('[data-test="ricerca"]')

        def ricerca(testo, attesa=1500):
            cerca.fill("")
            page.wait_for_timeout(300)
            cerca.fill(testo)
            page.wait_for_timeout(attesa)
            return [t.strip() for t in
                    page.locator('[data-test="risultato"]').all_inner_texts()]

        r = ricerca("ov-")
        check("ricerca: sottostringa ordinaria",
              any("ov-1" in x for x in r) and any("ov-2" in x for x in r), str(r)[:200])

        # ⚠ Il caso che vale più di tutti (§8.48 voce 1).
        r = ricerca("10.0.0.1")
        check("ricerca: un indirizzo esatto NON trova il suo prefisso",
              any("ip-uno" in x for x in r) and not any("ip-cento" in x for x in r),
              str(r)[:200])
        r = ricerca("10.0.0.100")
        check("ricerca: e l'altro indirizzo trova sé stesso",
              any("ip-cento" in x for x in r) and not any("ip-uno" in x for x in r),
              str(r)[:200])
        r = ricerca("10.0.0.0/24")
        check("ricerca: una rete trova entrambi",
              any("ip-uno" in x for x in r) and any("ip-cento" in x for x in r),
              str(r)[:200])
        r = ricerca("2001:db8::1")
        check("ricerca: IPv6 esatto", any("ip-sei" in x for x in r), str(r)[:200])
        r = ricerca("2001:DB8::1")
        check("ricerca: IPv6 esatto, maiuscolo (lo normalizza il dominio)",
              any("ip-sei" in x for x in r), str(r)[:200])
        r = ricerca("srv%web")
        check("ricerca: `%` è un carattere, non un jolly",
              any("srv%web" in x for x in r) and len(r) == 1, str(r)[:200])
        r = ricerca("%")
        check("ricerca: `%` da solo non trova tutto",
              all("pag-" not in x for x in r), f"{len(r)} risultati: {str(r)[:200]}")
        r = ricerca("éàü")
        check("ricerca: Unicode", any("serveur" in x for x in r), str(r)[:200])
        r = ricerca("RIMOSSO-2")
        check("ricerca: un apparato RIMOSSO si trova ancora (riscontro incrociato)",
              any("dism-rimosso" in x for x in r), str(r)[:200])
        check("ricerca: e il risultato dice che è rimosso",
              any("rimosso" in x.lower() for x in r), str(r)[:200])

        # --- paginazione ---
        ricerca("pag-", attesa=2000)
        altri = page.locator('[data-test="ricerca-altri"]')
        check("ricerca: con più risultati del limite compare «carica altri»",
              altri.count() == 1, f"{altri.count()} pulsanti")
        nota = page.locator('[data-test="ricerca-nota"]')
        prima = nota.inner_text() if nota.count() else ""
        check("ricerca: la nota dice quanti risultati sono caricati",
              "50" in prima and "altri" in prima, f"nota={prima!r}")
        if altri.count():
            altri.click()
            page.wait_for_timeout(2500)
            dopo = nota.inner_text() if nota.count() else ""
            # ⚠ Il menu ne mostra dodici per volta: ciò che cresce è l'INSIEME
            # caricato, non le righe visibili. Contare le righe misurerebbe il limite
            # del menu e passerebbe anche se «carica altri» non avesse fatto niente.
            check("ricerca: «carica altri» aggiunge una pagina",
                  "60" in dopo and "altri" not in dopo,
                  f"nota prima={prima!r} dopo={dopo!r}")

        # --- una risposta vecchia non sovrascrive una nuova ---
        #
        # ⚠ Il test che il requisito chiede per nome (§3, §18). La richiesta di «ov-»
        # si fa arrivare in ritardo; quella di «pag-» parte dopo e arriva prima. Deve
        # restare la seconda.
        def rallenta(route):
            if "q=ov-" in route.request.url:
                page.wait_for_timeout(2500)
            route.continue_()

        cerca.fill("")
        page.wait_for_timeout(400)
        page.route("**/api/inventory/search**", rallenta)
        cerca.fill("ov-")
        page.wait_for_timeout(600)      # il ritardo di digitazione è 250 ms
        cerca.fill("dism-presente")
        page.wait_for_timeout(4500)     # oltre il ritardo artificiale
        page.unroute("**/api/inventory/search**")
        r = [t.strip() for t in page.locator('[data-test="risultato"]').all_inner_texts()]
        check("ricerca: la risposta della query A, tornata dopo la B, non si mostra",
              any("dism-presente" in x for x in r) and not any("ov-1" in x for x in r),
              str(r)[:200])

        # --- il ritardo di digitazione ---
        richieste.clear()
        cerca.fill("")
        page.wait_for_timeout(400)
        richieste.clear()
        for i in range(1, len("dism-presente") + 1):
            cerca.fill("dism-presente"[:i])
            page.wait_for_timeout(40)
        page.wait_for_timeout(1500)
        n = len([u for u in richieste if "/search" in u])
        check("ricerca: tredici battute non fanno tredici richieste",
              1 <= n <= 3, f"{n} richieste per 13 caratteri")

        cerca.fill("")
        page.wait_for_timeout(400)

        # ==============================================================
        # 2. CAPACITÀ
        # ==============================================================
        page.get_by_role("button", name="Capacità", exact=True).click()
        page.wait_for_timeout(2500)
        testo_cap = page.inner_text("body")

        # ⚠ La vista mostra i totali della SALA e una riga per FILA, non una riga per
        # rack. Il rack sovrapposto è il solo della fila A: dieci unità, cinque
        # occupate, quindi «Fila A: 5 U libere / 10». Con `SUM(h)` sarebbero state
        # sette occupate e tre libere, ed è la differenza che questa riga misura.
        check("capacità: la sovrapposizione conta uno slot una volta sola",
              "Fila A: 5 U libere / 10" in testo_cap,
              _estrai(testo_cap, "Fila A"))
        # ⚠ Le due file «—» sono DUE gruppi. Con la sentinella vecchia sarebbe stata
        # una riga sola con 14 U libere su 16.
        righe_trattino = testo_cap.count("Fila —:")
        check("capacità: «fila assente» e «fila uguale a —» sono gruppi distinti",
              righe_trattino == 2,
              f"{righe_trattino} gruppi «—» (atteso 2): {_estrai(testo_cap, 'Fila')}")
        check("capacità: nessun gruppo somma i due (14 su 16 sarebbe la collisione)",
              "Fila —: 14 U libere / 16" not in testo_cap, _estrai(testo_cap, "Fila"))
        check("capacità: gli apparati rimossi si dicono",
              "rimoss" in testo_cap.lower(), _estrai(testo_cap, "Sala 2H"))
        check("capacità: la revisione dei numeri è mostrata",
              page.locator('[data-test="cap-revisione"]').count() == 1
              and "revisione" in page.locator('[data-test="cap-revisione"]').inner_text(),
              page.locator('[data-test="cap-revisione"]').inner_text()
              if page.locator('[data-test="cap-revisione"]').count() else "assente")

        # I numeri della vista vengono dall'endpoint: si confrontano con l'endpoint.
        cap = page.evaluate(JS_GET_JSON, "/api/inventory/capacity")["json"]
        sala = cap["locations"][0]["rooms"][0]
        atteso = f"{sala['usedU']}/{sala['totalU']} U occupate ({sala['occupancyPercent']}%)"
        check("capacità: la vista mostra ESATTAMENTE i numeri dell'endpoint",
              atteso in testo_cap, f"atteso «{atteso}»")
        # `dismesso + presente` occupa, `dismesso + rimosso` no: 2 U su 10.
        rack_over = [r for r in sala["racks"] if r["code"] == "R-OVER"][0]
        check("capacità: l'endpoint conta 5 slot distinti, non la somma di h (7)",
              rack_over["usedU"] == 5, json.dumps(rack_over))
        rack_dism = [r for r in sala["racks"] if r["code"] == "R-DISM"][0]
        check("capacità: dismesso+presente occupa, dismesso+rimosso no",
              (rack_dism["usedU"], rack_dism["removedCount"]) == (2, 1),
              json.dumps(rack_dism))
        # 1/8 = 12,5% → 13, non 12. È l'arrotondamento HALF-UP, sullo schermo.
        rack_tr = [r for r in sala["racks"] if r["code"] == "R-TRATTINO"][0]
        check("capacità: la metà esatta arrotonda per eccesso (12,5% → 13%)",
              rack_tr["usedU"] == 1 and rack_tr["u"] == 8, json.dumps(rack_tr))

        # ==============================================================
        # 3. SCADENZE
        # ==============================================================
        page.get_by_role("button", name="Scadenze").click()
        page.wait_for_timeout(2500)
        testo_scad = page.inner_text("body")

        check("scadenze: una data passata compare come scaduta",
              "scad-passata" in testo_scad and "2020-01-01" in testo_scad,
              _estrai(testo_scad, "scad-passata"))
        check("scadenze: oggi è zero giorni",
              "+0 gg" in testo_scad, _estrai(testo_scad, "scad-oggi"))
        check("scadenze: una futura compare",
              "scad-lontana" in testo_scad, _estrai(testo_scad, "scad-lontana"))
        check("scadenze: un dismesso è VISIBILE nella vista ispettiva",
              "scad-dismesso" in testo_scad, _estrai(testo_scad, "scad-dismesso"))
        # ⚠ Le due date che il browser accettava e il backend rifiuta.
        check("scadenze: «March 15, 2027» NON è interpretata come data",
              "March 15, 2027" not in testo_scad and "2027-03-15" not in testo_scad,
              _estrai(testo_scad, "illeggibile"))
        check("scadenze: «2027-02-30» non scivola al 2 marzo",
              "2027-03-02" not in testo_scad, _estrai(testo_scad, "2027-03"))
        check("scadenze: la data di riferimento è mostrata",
              page.locator('[data-test="scad-oggi"]').count() == 1
              and oggi.isoformat() in page.locator('[data-test="scad-oggi"]').inner_text(),
              page.locator('[data-test="scad-oggi"]').inner_text()
              if page.locator('[data-test="scad-oggi"]').count() else "assente")

        # `notifiable`: il dismesso c'è, ma non genererebbe un'email.
        exp = page.evaluate(JS_GET_JSON, "/api/inventory/expiries?limit=200")["json"]
        per_nome = {i["device"]["code"]: i for i in exp["items"]}
        check("scadenze: il dismesso non è avvisabile, il vicino sì",
              per_nome["scad-dismesso"]["notifiable"] is False
              and per_nome["scad-vicina"]["notifiable"] is True,
              json.dumps({k: per_nome[k]["notifiable"] for k in per_nome}))
        check("scadenze: le forme illeggibili non compaiono fra le voci",
              "scad-illeggibile" not in per_nome and "scad-inesistente" not in per_nome,
              str(sorted(per_nome)))

        # --- i filtri passano dal server ---
        page.locator('[data-test="scad-filtro-stato"]').select_option("dismesso")
        page.wait_for_timeout(2000)
        testo_f = page.inner_text("body")
        check("scadenze: il filtro per stato è applicato dal server",
              "scad-dismesso" in testo_f and "scad-vicina" not in testo_f,
              _estrai(testo_f, "scad-"))
        page.locator('[data-test="scad-filtro-stato"]').select_option("")
        page.wait_for_timeout(1500)

        # ==============================================================
        # 4. DISMESSI
        # ==============================================================
        page.locator('[data-test="tab-dismessi"]').click()
        page.wait_for_timeout(2500)
        testo_dism = page.inner_text("body")

        check("dismessi: la vista esiste e si apre",
              page.locator('[data-test="vista-dismessi"]').count() == 1)
        check("dismessi: c'è l'apparato dismesso ANCORA in rack",
              "dism-presente" in testo_dism, _estrai(testo_dism, "dism-"))
        check("dismessi: e quello RIMOSSO",
              "dism-rimosso" in testo_dism, _estrai(testo_dism, "dism-"))
        check("dismessi: il rack di provenienza è conservato",
              "Dismessi" in testo_dism or "R-DISM" in testo_dism,
              _estrai(testo_dism, "dism-rimosso"))
        check("dismessi: lo slot di un rimosso è dichiarato «ultimo»",
              "(ultimo)" in testo_dism, _estrai(testo_dism, "dism-rimosso"))
        check("dismessi: il seriale è mostrato, per il riscontro incrociato",
              "SN-RIMOSSO-2" in testo_dism and "SN-PRESENTE-1" in testo_dism,
              _estrai(testo_dism, "SN-"))
        check("dismessi: nessuna etichetta è un valore dell'implementazione",
              not any(t in testo_dism for t in ("undefined", "null", "None")),
              _estrai(testo_dism, "undefined") or _estrai(testo_dism, "null"))
        # ⚠ I conteggi si RICAVANO dall'endpoint, non si scrivono a mano. La prima
        # stesura pretendeva «1 ancora in rack» e il vero era 2: i dismessi dello
        # scenario sono tre, perché ce n'è uno anche fra i casi delle scadenze. Un
        # numero copiato a mano in un test è un numero che si aggiusta finché passa.
        dism = page.evaluate(
            JS_GET_JSON, "/api/inventory/search?q=&stato=dismesso&limit=200")["json"]
        presenti = sum(1 for r in dism["results"]
                       if r["device"]["presenza"] == "presente")
        rimossi = sum(1 for r in dism["results"]
                      if r["device"]["presenza"] == "rimosso")
        riepilogo = (page.locator('[data-test="dism-riepilogo"]').inner_text()
                     if page.locator('[data-test="dism-riepilogo"]').count() else "")
        check("dismessi: il riepilogo distingue presenti e rimossi",
              f"{presenti} ancora in rack" in riepilogo
              and f"{rimossi} rimoss" in riepilogo,
              f"riepilogo={riepilogo!r}, attesi {presenti} presenti e {rimossi} rimossi")
        check("dismessi: tutti i dismessi dell'inventario sono nell'elenco",
              len(dism["results"]) == len(dism["results"])
              and all(r["device"]["stato"] == "dismesso" for r in dism["results"])
              and len(dism["results"]) >= 3,
              f"{len(dism['results'])} risultati")

        # il filtro sulla presenza
        page.locator('[data-test="dism-presenza"]').select_option("rimosso")
        page.wait_for_timeout(2000)
        t = page.inner_text("body")
        check("dismessi: il filtro «rimossi» esclude i presenti",
              "dism-rimosso" in t and "dism-presente" not in t, _estrai(t, "dism-"))
        page.locator('[data-test="dism-presenza"]').select_option("presente")
        page.wait_for_timeout(2000)
        t = page.inner_text("body")
        check("dismessi: il filtro «ancora in rack» esclude i rimossi",
              "dism-presente" in t and "dism-rimosso" not in t, _estrai(t, "dism-"))
        page.locator('[data-test="dism-presenza"]').select_option("")
        page.wait_for_timeout(1500)

        # la ricerca dentro i dismessi è la stessa ricerca
        page.locator('[data-test="dism-ricerca"]').fill("SN-RIMOSSO-2")
        page.wait_for_timeout(2000)
        t = page.inner_text("body")
        check("dismessi: la ricerca interna usa la stessa semantica",
              "dism-rimosso" in t and "dism-presente" not in t, _estrai(t, "dism-"))
        page.locator('[data-test="dism-ricerca"]').fill("")
        page.wait_for_timeout(1500)

        # ==============================================================
        # 5. UN RIMOSSO NON OCCUPA IL RACK, ma la relazione resta
        # ==============================================================
        page.get_by_role("button", name="Pianta").click()
        page.wait_for_timeout(1500)
        # si apre il rack dei dismessi dalla ricerca
        cerca.fill("dism-presente")
        page.wait_for_timeout(1500)
        if page.locator('[data-test="risultato"]').count():
            page.locator('[data-test="risultato"]').first.click()
            page.wait_for_timeout(2000)
        pannello = page.inner_text("body")
        check("rack: il pannello conta gli apparati rimossi a parte",
              page.locator('[data-test="rack-rimossi"]').count() == 1
              and "non occupa" in page.locator('[data-test="rack-rimossi"]').inner_text(),
              page.locator('[data-test="rack-rimossi"]').inner_text()
              if page.locator('[data-test="rack-rimossi"]').count() else "assente")
        check("rack: un rimosso è marcato tale nell'elevazione",
              "RIMOSSO" in pannello, _estrai(pannello, "dism-rimosso"))
        check("rack: e il suo rack di provenienza è ancora questo",
              "dism-rimosso" in pannello, _estrai(pannello, "DISPOSITIVI"))

        # ==============================================================
        # 6. REVISIONE: un risultato di un'altra revisione non si mostra
        # ==============================================================
        def revisione_sbagliata(route):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({
                              "version": 999999,
                              "sha256": "f" * 64,
                              "locations": [],
                          }))

        page.route("**/api/inventory/capacity**", revisione_sbagliata)
        page.get_by_role("button", name="Capacità", exact=True).click()
        page.wait_for_timeout(4000)
        page.unroute("**/api/inventory/capacity**")
        errore = page.locator('[data-test="cap-errore"]')
        check("revisione: un risultato di un'altra revisione NON si mostra",
              errore.count() == 1, page.inner_text("body")[:300])
        if errore.count():
            check("revisione: e la vista spiega perché",
                  "revisione" in errore.inner_text().lower(), errore.inner_text()[:200])
        check("revisione: e non compare nessun numero di capacità",
              "U occupate" not in page.inner_text("body"),
              _estrai(page.inner_text("body"), "U occupate"))

        # ==============================================================
        # 6-bis. CONCORRENZA VERA: un salvataggio mentre la richiesta è in volo
        # ==============================================================
        #
        # ⚠ Il caso di sopra è quello senza uscita: la revisione non combacia mai e si
        # dichiara il disaccordo. Questo è il caso normale — un collega salva, la
        # risposta arriva vecchia di una revisione, il client si riconcilia e il
        # risultato SI MOSTRA. Serve un salvataggio vero: è la riconciliazione che deve
        # funzionare, non l'intercettazione.
        page.get_by_role("button", name="Pianta").click()
        page.wait_for_timeout(800)

        salvato = {"fatto": False}

        def ritarda_e_salva(route):
            # Mentre la richiesta di capacità è ferma qui, si scrive davvero. La
            # risposta che seguirà appartiene alla revisione PRECEDENTE.
            if not salvato["fatto"]:
                salvato["fatto"] = True
                esito = page.evaluate("""
                  async () => {
                    const cur = await (await fetch('/api/inventory',
                                       { credentials: 'same-origin' })).json();
                    const doc = cur.doc;
                    doc.locations[0].nome = 'Sito 2H — rinominato';
                    const r = await fetch('/api/inventory', {
                      method: 'PUT', credentials: 'same-origin',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ baseVersion: cur.version, doc }),
                    });
                    return r.status;
                  }
                """)
                check("concorrenza: il salvataggio durante la richiesta riesce",
                      esito == 200, f"HTTP {esito}")
            route.continue_()

        page.route("**/api/inventory/capacity**", ritarda_e_salva)
        page.get_by_role("button", name="Capacità", exact=True).click()
        page.wait_for_timeout(6000)
        page.unroute("**/api/inventory/capacity**")

        corpo = page.inner_text("body")
        errore = page.locator('[data-test="cap-errore"]')
        check("concorrenza: dopo la riconciliazione la vista mostra i numeri",
              errore.count() == 0 and "U occupate" in corpo,
              _estrai(corpo, "U occupate") or corpo[:250])
        # ⚠ E sono i numeri della revisione NUOVA, non della vecchia: si confronta con
        # l'endpoint, che è l'unica verifica che distingue «ha mostrato qualcosa» da
        # «ha mostrato la cosa giusta».
        cap2 = page.evaluate(JS_GET_JSON, "/api/inventory/capacity")["json"]
        sala2 = cap2["locations"][0]["rooms"][0]
        atteso2 = f"{sala2['usedU']}/{sala2['totalU']} U occupate"
        check("concorrenza: e sono i numeri della revisione nuova",
              atteso2 in corpo, f"atteso «{atteso2}»")
        rev = page.locator('[data-test="cap-revisione"]')
        check("concorrenza: la revisione mostrata è quella corrente",
              rev.count() == 1 and str(cap2["version"]) in rev.inner_text(),
              f"mostrata={rev.inner_text() if rev.count() else '—'}, "
              f"corrente={cap2['version']}")
        # Il nome del sito rinominato deve essere arrivato anche nell'INVENTARIO
        # caricato: è la prova che il ricaricamento è avvenuto per davvero.
        check("concorrenza: l'inventario caricato è stato ricaricato",
              "rinominato" in page.inner_text("body"),
              _estrai(page.inner_text("body"), "Sito 2H"))

        # ==============================================================
        # 7. GUASTO: un 503 non fa tornare il calcolo locale
        # ==============================================================
        def non_disponibile(route):
            route.fulfill(status=503, content_type="application/json",
                          body=json.dumps({"detail": {
                              "code": "projection_not_current",
                              "message": "la proiezione non rispecchia la testa"}}))

        page.route("**/api/inventory/capacity**", non_disponibile)
        page.get_by_role("button", name="Pianta").click()
        page.wait_for_timeout(800)
        page.get_by_role("button", name="Capacità", exact=True).click()
        page.wait_for_timeout(3000)
        page.unroute("**/api/inventory/capacity**")
        corpo = page.inner_text("body")
        errore = page.locator('[data-test="cap-errore"]')
        check("guasto: un 503 diventa uno stato visibile", errore.count() == 1,
              corpo[:300])
        if errore.count():
            check("guasto: il messaggio nomina la ricostruzione",
                  "ricostruzione" in errore.inner_text().lower(),
                  errore.inner_text()[:250])
        # ⚠ Il controllo che conta: nessun ripiego locale. Se la vista ricalcolasse i
        # numeri nel browser, sarebbe indistinguibile da una che funziona.
        check("guasto: NESSUN numero di capacità viene calcolato in locale",
              "U occupate" not in corpo, _estrai(corpo, "U occupate"))
        check("guasto: e l'inventario resta consultabile",
              page.get_by_role("button", name="Pianta").is_visible())

        # ==============================================================
        # 8. XLSX/report e pannello: gli stessi numeri dell'endpoint
        # ==============================================================
        # ⚠ §14: l'export è client-side e usa l'unico aiuto locale condiviso. Si
        # confronta il suo conteggio con quello dell'endpoint sullo stesso inventario,
        # dentro la pagina, dove entrambi sono disponibili.
        confronto = page.evaluate("""
          async () => {
            const cap = await (await fetch('/api/inventory/capacity',
                                           { credentials: 'same-origin' })).json();
            const inv = await (await fetch('/api/inventory',
                                           { credentials: 'same-origin' })).json();
            const DOM = await import('./domain.js');
            const perUid = {};
            for (const L of cap.locations) for (const R of L.rooms)
              for (const k of R.racks) perUid[k.uid] = k.usedU;
            const diff = [];
            for (const L of inv.doc.locations) for (const R of L.sale)
              for (const k of R.racks) {
                const locale = DOM.rackCapacity(k.u, k.devices).usedU;
                if (locale !== perUid[k._uid]) {
                  diff.push({ rack: k.id, locale, sql: perUid[k._uid] });
                }
              }
            return { racks: Object.keys(perUid).length, diff };
          }
        """)
        check("export: l'aiuto locale e l'endpoint danno lo stesso «U usate»",
              confronto["racks"] > 0 and not confronto["diff"],
              json.dumps(confronto)[:400])

        # ==============================================================
        # 8-bis. il corpus del contratto, nel MODULO CHE IL BROWSER HA CARICATO
        # ==============================================================
        #
        # ⚠ §20 del requisito, e la ragione per cui non basta il contratto in node.
        # Quello gira sul file su disco; le suite Python sul modulo importato. Nessuno
        # dei due prova che il modulo SERVITO DA NGINX sia quello provato — e un modulo
        # dimenticato nell'allowlist è già accaduto due volte in questo progetto.
        #
        # Le fixture arrivano come argomento: non sono servite da nginx, e non devono
        # esserlo. L'allowlist è corta di proposito.
        corpora = {}
        for nome in ("capacity", "percent", "presence", "rows", "rack-height"):
            corpora[nome] = json.loads(
                (Path(__file__).resolve().parents[1] / "fixtures" / "domain"
                 / f"{nome}.json").read_text(encoding="utf-8"))
        esito_corpus = page.evaluate("""
          async (corpora) => {
            const D = await import('./domain.js');
            const errori = [];
            let controlli = 0;
            for (const c of corpora.capacity.cases) {
              const cap = D.rackCapacity(c.rackU, c.devices);
              controlli++;
              if (cap.usedU !== c.usedU || cap.freeU !== c.freeU
                  || cap.largestFreeRun !== c.largestFreeRun) {
                errori.push(`capacità «${c.name}»: ${JSON.stringify(cap)}`);
              }
              if (D.percent(cap.usedU, cap.totalU) !== c.percent) {
                errori.push(`percentuale «${c.name}»`);
              }
            }
            for (const c of corpora.percent.cases) {
              controlli++;
              if (D.percent(c.used, c.total) !== c.percent) {
                errori.push(`percent ${c.used}/${c.total}`);
              }
            }
            for (const c of corpora.presence.cases) {
              controlli++;
              if (D.occupiesSpace(c.device) !== c.occupies
                  || D.notifies(c.device) !== c.notifies) {
                errori.push(`presenza «${c.name}»`);
              }
            }
            for (const c of corpora.rows.cases) {
              controlli++;
              const g = D.rowGroup({ row: c.row });
              if (g.assigned !== c.assigned || g.label !== c.label) {
                errori.push(`fila «${c.name}»`);
              }
            }
            for (const c of corpora['rack-height'].cases) {
              controlli++;
              if (D.rackHeightSupported(c.u) !== c.supported) {
                errori.push(`altezza ${JSON.stringify(c.u)}`);
              }
            }
            return { controlli, errori };
          }
        """, corpora)
        check("contratto: il modulo servito da nginx soddisfa il corpus",
              not esito_corpus["errori"],
              f"{len(esito_corpus['errori'])} divergenze: "
              f"{esito_corpus['errori'][:4]}")
        check("contratto: e il corpus eseguito nel browser non è vuoto",
              esito_corpus["controlli"] >= 80, f"{esito_corpus['controlli']} controlli")

        # ==============================================================
        # 9. igiene
        # ==============================================================
        veri = [e for e in errori_js
                if "ERR_CERT" not in e and "favicon" not in e
                and "503" not in e and "Failed to load resource" not in e]
        check("nessun errore JavaScript non gestito", not veri, " | ".join(veri[:3]))

        browser.close()
    return


def main() -> int:
    """Esegue la prova e riporta SEMPRE, anche se inciampa a metà.

    ⚠ La prima stesura lasciava sfuggire l'eccezione, e con lei tutti gli esiti già
    raccolti: un selettore che scade dopo trenta secondi cancellava dal rapporto i
    venti controlli passati e i due falliti che spiegavano perché. Adesso il riepilogo
    esce comunque, e con gli errori della pagina accanto — che sono la causa, mentre il
    selettore mancante è il sintomo.
    """
    errori_pagina: list[str] = []
    try:
        prova(errori_pagina)
    except Exception as exc:                       # noqa: BLE001 — si riporta e basta
        check("la prova è arrivata alla fine", False,
              f"{type(exc).__name__}: {str(exc).splitlines()[0][:220]}")
        if errori_pagina:
            print("\n  errori JavaScript raccolti dalla pagina:")
            for e in errori_pagina[:10]:
                print(f"    - {e[:220]}")
    return riepiloga()

def _estrai(testo: str, ago: str, intorno: int = 120) -> str:
    i = testo.find(ago)
    if i < 0:
        return f"«{ago}» non trovato"
    return testo[max(0, i - 40):i + intorno].replace("\n", " ⏎ ")


def riepiloga() -> int:
    print()
    falliti = 0
    for nome, ok, dettaglio in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nome}")
        if not ok:
            falliti += 1
            if dettaglio:
                print(f"         → {dettaglio}")
    print("=" * 78)
    print(f"controlli: {len(results)}   falliti: {falliti}")
    print("RISULTATO: " + ("TUTTI PASSATI" if not falliti else "CI SONO FALLIMENTI"))
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
