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

Lo scenario — quattro dispositivi con stati e altezze dichiarati (2, 3, 4 e 6 U) —
lo COSTRUISCE il test, invece di cercarlo nei dati che trova. La prima stesura
contava sulle altezze «già presenti nei dati reali»: contro la baseline di
produzione, che ha 102 rack e ZERO dispositivi, non c'era niente da misurare, e
tre controlli fallivano parlando di altezze.

⚠ PORTATO ALLO STACK VERO DALLA FASE 2J.

La prima stesura serviva `handoff/` con un `http.server` locale ed entrava con
`admin`/`admin`. Quella era la forma dell'applicazione PRIMA che arrivasse
l'autenticazione: da allora il frontend prende i dati dall'API e non esiste piu'
nessun `admin/admin`, quindi il login non passava, il pannello di accesso restava
sopra la pagina, e il primo clic finiva su un `<div>` che intercettava gli eventi.
Il test era rosso — o meglio, non arrivava a essere ne' rosso ne' verde — da molto
prima della 2J, e nessuno l'aveva eseguito. Lo si e' scoperto eseguendo OGNI suite
committata invece di un sottoinsieme scelto (§32 del requisito della 2J).

Che cosa protegge, e perche' valeva portarlo: un giro export->import che azzera le
altezze a 1 e riporta ogni stato ad "attivo" e' PERDITA DI DATI su una funzione che
viene distribuita (`handoff/xlsx.js` e' nell'allowlist dell'immagine web). Nessuna
altra suite copre quella semantica.

⚠ SCRIVE SUL SERVER, e la prima stesura del commento diceva il contrario.
«Applica import» non modifica soltanto la copia di lavoro del client: salva, e
l'inventario passa alla versione successiva. Misurato subito dopo — quattro
dispositivi in proiezione e `inventory_versions` a 2 — eseguendo §9 della
qualificazione, che ha visto lo stato non piu' iniziale e l'ha detto.
Quindi: `--allow-destructive` obbligatorio, come per le altre suite del browser,
e da eseguire su un'installazione di qualificazione.

Uso:
    pip install playwright
    python tools/xlsx-roundtrip-test.py --base https://localhost \
        --password ... --allow-destructive
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import destructive_guard                                        # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("serve playwright:  pip install playwright")

ROOT = Path(__file__).resolve().parent.parent

#: Campi che devono sopravvivere al giro. `h` e `stato` sono i due difetti noti.
COMPARED = ("_uid", "id", "name", "type", "stato", "model", "ip", "serial",
            "owner", "u", "h", "garanzia", "supporto", "note")

results = []


def check(name, passed, detail=""):
    results.append((name, passed, detail))



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
    ap = argparse.ArgumentParser(description="giro export/import XLSX")
    ap.add_argument("--base", default="https://localhost")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", required=True)
    destructive_guard.add_arguments(ap)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    destructive_guard.enforce(args, base)

    tmp = ROOT / ".xlsx-roundtrip-tmp"
    tmp.mkdir(exist_ok=True)
    dialogs, console_errors = [], []

    try:
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
                # Viewport alto: il pannello Impostazioni è un dropdown lungo e
                # l'anteprima dell'import finisce fuori da un viewport standard.
                context = browser.new_context(accept_downloads=True,
                                              ignore_https_errors=True,
                                              viewport={"width": 1600, "height": 2400})
                # ⚠ NESSUN `context.route` che aborta le richieste esterne. Serviva
                # quando la pagina veniva servita da un `http.server` locale e non
                # doveva raggiungere niente; adesso l'applicazione PARLA con l'API
                # sulla stessa origine, e abortire le richieste la lascerebbe senza
                # dati — che è il modo in cui questo test era diventato inutilizzabile.
                page = context.new_page()
                # ⚠ Si tengono gli errori di JAVASCRIPT e si scartano quelli di
                # STATO HTTP. Chrome registra come «error» di console anche un
                # «Failed to load resource: 401» — che qui è normale: prima del
                # login l'applicazione interroga `/api/auth/me` e riceve 401, e
                # quello è il comportamento voluto, non un guasto. Un filtro che
                # tiene dentro quel rumore rende il controllo rosso sempre, e un
                # controllo sempre rosso viene disattivato. Stessa correzione già
                # applicata a clean-room-test.py nella fase 2I.
                def _console(m):
                    if m.type != "error":
                        return
                    if "Failed to load resource" in m.text:
                        return
                    console_errors.append(m.text)

                page.on("console", _console)
                page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

                page.goto(f"{base}/", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                page.get_by_placeholder("utente", exact=True).fill(args.username)
                page.get_by_placeholder("password", exact=True).fill(args.password)
                page.get_by_role("button", name="Accedi").click()
                page.wait_for_timeout(4000)
                check("accesso riuscito",
                      page.get_by_placeholder("utente", exact=True).count() == 0,
                      "la schermata di accesso è ancora là: credenziali sbagliate?")
                # ⚠ `dispatch_event` e non `click()`: sullo stack vero la pianta della
                # sala copre il pulsante con un `<div>` che intercetta gli eventi di
                # puntatore, e un clic reale resta in attesa fino al timeout. Qui
                # interessa l'HANDLER, non la geometria — la geometria la coprono
                # identity-ui-test.py e clean-room-test.py con clic veri. Lo stesso
                # criterio è già applicato più avanti al pulsante «Applica import».
                page.get_by_role("button", name="Editing", exact=True).dispatch_event("click")
                page.wait_for_timeout(1200)

                # --- 2. lo scenario si COSTRUISCE, non si spera ---
                #
                # ⚠ La prima stesura cercava nell'inventario esistente quattro
                # dispositivi con `h == 1` e ne cambiava lo stato, contando sulle
                # altezze non predefinite «già presenti nei dati reali». Contro la
                # BASELINE DI PRODUZIONE quella premessa non vale: la baseline ha
                # 102 rack e ZERO dispositivi, e i tre controlli sul riferimento
                # fallivano parlando di altezze mentre il problema era che non
                # c'era niente da misurare.
                #
                # Un test che dipende da quali dati capita di trovare prova una
                # cosa diversa a ogni installazione. Qui lo scenario si scrive: si
                # aggiungono al primo rack quattro dispositivi con stati e altezze
                # DICHIARATI, e da quel momento il test vale su qualunque
                # installazione — baseline vuota compresa, che è appunto lo stato
                # con cui il prodotto viene consegnato.
                seed = export_json(page, tmp, "seed")
                SCENARIO = (
                    ("manutenzione", 2), ("dismissione", 3),
                    ("dismesso", 4), ("manutenzione", 6),
                )
                rack = None
                for L in seed.get("locations", []):
                    for R in L.get("sale", []):
                        for K in R.get("racks", []):
                            rack = rack or K
                check("c'è almeno un rack in cui costruire lo scenario",
                      rack is not None, "nessun rack nell'inventario")
                if rack is None:
                    raise RuntimeError("nessun rack: non posso costruire lo scenario")
                rack.setdefault("devices", [])
                # Slot liberi: si parte dopo l'ultimo occupato, così lo scenario non
                # si sovrappone a quello che c'era (sulla baseline: niente).
                occupato = max((int(d.get("u") or 0) + int(d.get("h") or 1) - 1)
                               for d in rack["devices"]) if rack["devices"] else 0
                touched = []
                for i, (stato, h) in enumerate(SCENARIO):
                    uid = f"c1a55e00-0000-4000-8000-{i:012d}"
                    rack["devices"].append({
                        "_uid": uid, "id": f"XLSX-{i}", "name": f"prova xlsx {i}",
                        "u": occupato + 1 + sum(x[1] for x in SCENARIO[:i]),
                        "h": h, "stato": stato, "presenza": "presente",
                        "type": "server", "model": "modello di prova",
                        "note": "scenario del giro export/import",
                    })
                    touched.append(uid)
                check("scenario costruito: 4 dispositivi con stati e altezze dichiarati",
                      len(touched) == 4, f"{len(touched)}")

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

            except Exception as exc:
                # ⚠ Un'eccezione a meta' non deve buttare via i controlli gia'
                # raccolti: sapere QUALI passi erano verdi prima del guasto e' meta'
                # della diagnosi, e senza questo il test stampava «0 PASS 0 FAIL»
                # su un giro che ne aveva passati parecchi. Stesso criterio di
                # clean-room-test.py.
                check(f"ESECUZIONE INTERROTTA: {type(exc).__name__}", False,
                      str(exc).splitlines()[0][:300] if str(exc) else "")
            finally:
                if browser is not None:
                    browser.close()
    finally:
        # Niente da chiudere: la pagina arriva dall'installazione, non da un
        # server locale di questo processo.
        pass

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
