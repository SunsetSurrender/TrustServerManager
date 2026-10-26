#!/usr/bin/env python3
"""Genera l'artefatto di baseline di produzione dal sottoinsieme STRUTTURALE del seed.

⚠ Questo strumento si esegue A MANO, si guarda l'esito, e si committa il risultato.
NON gira al build, NON gira al deploy, e la baseline **non dipende dal seed a
runtime** (§20 del requisito della fase 2I). La ragione è tutta qui: il seed di
sviluppo è un file che qualcuno modifica per provare una cosa, e se la baseline lo
leggesse al momento dell'installazione, una prova fatta un martedì diventerebbe la
struttura di produzione di un giovedì. L'artefatto è esplicito, revisionabile e
appuntato a un digest; cambiarlo è un atto deliberato che passa da una revisione.

Che cosa tiene e che cosa toglie
--------------------------------
TIENE, verbatim: `_uid` di siti, sale e rack; ordine strutturale; nomi e codici
reali; `u` dei rack; fila; `vani` con la geometria e le porte; `segnaposto`;
`seriali` dell'armadio.

TOGLIE: i dispositivi (tutti), le radici legacy del prototipo (`utenti`,
`versione` — la baseline nasce già nella forma congelata del `PUT` normale), e i
riferimenti a foto sui rack.

⚠ I `seriali` del RACK non sono dati di dispositivo, e la distinzione vale la pena
scriverla perché è esattamente l'errore che §6 del requisito mette in guardia:
sono i numeri di inventario degli ARMADI — cespiti fisici con la loro targhetta.
Toglierli perché «sembrano seriali di macchine» butterebbe via metadato di
produzione vero.

La classificazione dei campi ignoti (§3)
---------------------------------------
Un documento di entità è aperto di proposito: l'applicazione conserva in `extra`
ciò che non ha una colonna. Per la baseline questo non basta — «non lo conosco»
non è una decisione. Quindi il generatore CONFRONTA ogni chiave di ogni sito, sala
e rack con `FIELD_MAP` della mappa relazionale, e se trova una chiave che non è né
tipizzata né una collezione di figli **si ferma e chiede**, invece di tenerla o
buttarla in silenzio. Oggi l'esito è: zero chiavi ignote. Il valore del controllo
è per il giorno in cui non sarà più vero.

Correzioni dei seriali degli armadi
-----------------------------------
I seriali che il seed porta sui rack sono in parte segnaposto (il codice del
rack, «R01», dove la targhetta non era stata letta). Quelli VERI arrivano da un
rilievo in sala, compilato nel foglio «Seriali rack» del modello Excel e
trasformato da `tools/apply-rack-serials.py` in `deploy/baseline/rack-serials.json`.

Quel file è una SOVRAPPOSIZIONE revisionata, non una modifica del seed: per ogni
rack nominato per `_uid` sostituisce i `seriali`, e nient'altro. Il seed resta un
file di sviluppo; ciò che va in produzione è scritto in un file che si legge in
una revisione, rack per rack, e il manifesto elenca ogni sostituzione con il
prima e il dopo.

Uso (dalla radice del repository; `app` importa sqlalchemy, quindi si esegue nella
stessa immagine della suite, come `tools/canonicalise-query-docs.py`):

    docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
        sh -c "pip install -q -r backend/requirements.txt && \
               python tools/make-production-baseline.py [--dry-run] [--aggiorna-digest]"

`--aggiorna-digest` sostituisce il digest appuntato con quello nuovo nei posti
che lo usano come riferimento (vedi `PINNED_IN`). Va usato DOPO aver letto il
manifesto: è la firma che la nuova struttura è quella giusta.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.identity.canonical import canonicalise            # noqa: E402
from app.inventory.digest import canonical_sha256           # noqa: E402
from app.inventory.relational import FIELD_MAP              # noqa: E402

SEED = ROOT / "fixtures" / "seed.json"
OUT_DIR = ROOT / "deploy" / "baseline"
OUT_DOC = OUT_DIR / "production-baseline.json"
OUT_MANIFEST = OUT_DIR / "MANIFEST.md"
CORREZIONI = OUT_DIR / "rack-serials.json"

#: I file che contengono il digest canonico come RIFERIMENTO (non come storia).
#: I documenti di qualificazione (RELEASE-MANIFEST, PREDEPLOYMENT-GATES, …)
#: restano come sono: descrivono ciò che è stato provato allora.
PINNED_IN = (
    "backend/app/inventory/baseline.py",
    "deploy/README.md",
    "tools/backup-restore-test.sh",
    "tools/clean-room-test.py",
    "tools/mutation-harness.py",
    "tools/notification-qualification.sh",
    "tools/release-qualification.py",
)
MAX_SERIALI = 2      # quanti ne mostra e ne modifica l'interfaccia («⚙ Rack»)


class CorrezioneNonValida(Exception):
    pass


def carica_correzioni(path: Path = CORREZIONI) -> dict[str, dict]:
    """`{_uid: {"seriali": [...], "rack": codice, ...}}`; vuoto se il file non c'è."""
    if not path.exists():
        return {}
    dati = json.loads(path.read_text(encoding="utf-8"))
    rack = dati.get("rack") if isinstance(dati, dict) else None
    if dati.get("schema") != 1 or not isinstance(rack, dict):
        raise CorrezioneNonValida(f"{path}: atteso {{\"schema\": 1, \"rack\": {{...}}}}")
    for uid, voce in rack.items():
        ser = voce.get("seriali") if isinstance(voce, dict) else None
        if (not isinstance(ser, list) or len(ser) > MAX_SERIALI
                or not all(isinstance(x, str) and x.strip() == x and x for x in ser)):
            raise CorrezioneNonValida(
                f"{path}: rack {uid}: `seriali` deve essere una lista di al massimo "
                f"{MAX_SERIALI} stringhe non vuote e senza spazi ai bordi")
    return rack

#: Chiavi ammesse per entità: quelle TIPIZZATE dalla mappa relazionale, più
#: l'identità e la collezione dei figli. Si legge da `FIELD_MAP` e non da un
#: elenco scritto a mano, così una colonna nuova non richiede di ricordarsi di
#: aggiornare anche questo file.
def _known(kind: str, *children: str) -> set[str]:
    return {"_uid", *children, *(doc_key for _col, doc_key, _fits in FIELD_MAP[kind])}


KNOWN = {
    "location": _known("location", "sale"),
    "room": _known("room", "racks"),
    "rack": _known("rack", "devices"),
}

#: Chiavi che si TOLGONO di proposito, con la ragione. Non sono «ignote»: sono
#: conosciute e non devono sopravvivere in una baseline.
DELIBERATELY_REMOVED = {
    "rack": {
        # §9: la tabella `photos` di una installazione nuova è vuota. Un `foto`
        # superstite non sarebbe soltanto inutile: `bootstrap` pretende che i
        # riferimenti ESISTANO (§8.5), quindi l'inizializzazione fallirebbe.
        "foto": "riferimento a una foto: la tabella photos nasce vuota (§9)",
    },
}


#: Sottostringhe che, in un nome o codice STRUTTURALE, meritano una conferma umana.
#: Non fanno rimuovere niente — un rack che si chiama «Test» in un CED vero è spesso
#: un rack vero, che ospita i sistemi di collaudo. Fanno finire quella riga in una
#: sezione del manifesto intitolata «da confermare», che è il posto giusto per una
#: domanda a cui solo chi conosce le sale può rispondere. §19 del requisito chiede
#: esattamente questo: prendere «un rack di sviluppo rimasto dentro» prima che diventi
#: struttura di produzione.
SOSPETTE = ("test", "prova", "demo", "fake", "finto", "esempio", "sample",
            "tmp", "temp", "xxx", "asd", "dummy", "placeholder")


def _sospetto(*valori: Any) -> str:
    """La prima sottostringa sospetta trovata, o stringa vuota."""
    for v in valori:
        if isinstance(v, str):
            basso = v.lower()
            for s in SOSPETTE:
                if s in basso:
                    return s
    return ""


def _file_interrotte(racks: list) -> tuple[str, list[str]]:
    """(sequenza delle etichette, rotture) per i rack di una sala, in ordine di documento.

    Una «rottura» è un'etichetta di fila che RICOMPARE dopo che un'altra si è interposta.
    In un inventario cresciuto per accodamento è normale — due rack aggiunti dopo e
    messi in fondo — e in un inventario battuto a mano è un errore di digitazione. I
    due casi non si distinguono dai dati, quindi si segnalano e non si toccano.
    """
    etichette = [(k.get("name") or k.get("id"), (k.get("row") or "").strip())
                 for k in racks]
    visti: set[str] = set()
    prec: str | None = None
    rotture: list[str] = []
    for nome, fila in etichette:
        if fila != prec and fila in visti:
            mostra = fila or "(vuota)"
            mostra_prec = prec if prec else "(vuota)"
            rotture.append(f"{nome!r}: fila «{mostra}» dopo «{mostra_prec}»")
        visti.add(fila)
        prec = fila
    sequenza = "".join((f[1][:1] or "—") for f in etichette)
    return sequenza, rotture


class Ignota(Exception):
    """Una chiave strutturale che nessuno ha classificato."""


def _classifica(kind: str, entity: dict, dove: str) -> tuple[dict, list[str]]:
    """Copia dell'entità con le chiavi conosciute, e l'elenco di quelle togliete.

    Solleva su una chiave che non è né tipizzata né deliberatamente rimossa: la
    decisione la prende una persona, non un default.
    """
    togliete: list[str] = []
    out: dict = {}
    for key, value in entity.items():
        # ⚠ La rimozione deliberata si guarda PRIMA di `KNOWN`, e l'ordine non è
        # indifferente: le due categorie non sono disgiunte. `foto` è una colonna
        # TIPIZZATA del rack, quindi sta in `KNOWN` — e con `KNOWN` per primo il
        # ramo della rimozione non veniva mai raggiunto. Un rack con una fotografia
        # (funzione del prodotto: la scheda rack stampabile la mostra) l'avrebbe
        # portata nella baseline, e il bootstrap sarebbe morto a metà su
        # `require_existing`, perché la tabella `photos` nasce vuota.
        #
        # Trovato da `test_un_foto_su_un_rack_viene_TOLTO_e_dichiarato`, che esisteva
        # proprio perché nel seed di oggi nessun rack ha una foto: l'unico modo di
        # provare il ramo era dargliene una.
        if key in DELIBERATELY_REMOVED.get(kind, {}):
            togliete.append(f"{dove}.{key} — {DELIBERATELY_REMOVED[kind][key]}")
        elif key in KNOWN[kind]:
            out[key] = value
        else:
            raise Ignota(
                f"{dove}: chiave {key!r} = {json.dumps(value, ensure_ascii=False)[:80]}\n"
                f"  Non è una colonna tipizzata della mappa relazionale, non è una\n"
                f"  collezione di figli, e nessuno l'ha dichiarata da rimuovere.\n"
                f"  → classificarla in KNOWN (metadato strutturale di produzione)\n"
                f"    oppure in DELIBERATELY_REMOVED (contenuto di sviluppo/prova),\n"
                f"    e scrivere la ragione. §3 del requisito: non si scarta in silenzio."
            )
    return out, togliete


def build(seed: dict, correzioni: dict[str, dict] | None = None
          ) -> tuple[dict, list[str], dict, list[str]]:
    """La baseline, le rimozioni, i conteggi e le righe da confermare.

    `correzioni`: i seriali VERI degli armadi, per `_uid` (vedi il modulo). Le
    sostituzioni fatte finiscono in `conteggi["correzioni"]`, per il manifesto.
    """
    correzioni = dict(correzioni or {})
    rimozioni: list[str] = []
    sospetti: list[str] = []
    conteggi = {"locations": 0, "rooms": 0, "racks": 0, "devices_removed": 0,
                "seriali": 0, "vani": 0, "correzioni": []}

    locations = []
    for L in seed.get("locations") or []:
        dove = f"sito {L.get('id') or L.get('_uid')}"
        loc, tolte = _classifica("location", L, dove)
        rimozioni += tolte
        conteggi["locations"] += 1
        if (hit := _sospetto(L.get("nome"), L.get("id"))):
            sospetti.append(f"SITO {L.get('nome')!r} (`{L.get('id')}`) — contiene «{hit}»")

        sale = []
        for R in L.get("sale") or []:
            dove_r = f"{dove}/{R.get('id') or R.get('_uid')}"
            room, tolte = _classifica("room", R, dove_r)
            rimozioni += tolte
            conteggi["rooms"] += 1
            if (hit := _sospetto(R.get("nome"), R.get("id"))):
                sospetti.append(
                    f"SALA {R.get('nome')!r} (`{R.get('id')}`) in {L.get('nome')} "
                    f"— contiene «{hit}»")
            conteggi["vani"] += len(room.get("vani") or [])

            racks = []
            for K in R.get("racks") or []:
                dove_k = f"{dove_r}/{K.get('name') or K.get('id') or K.get('_uid')}"
                rack, tolte = _classifica("rack", K, dove_k)
                rimozioni += tolte
                conteggi["racks"] += 1
                if (hit := _sospetto(K.get("name"), K.get("id"))):
                    sospetti.append(
                        f"RACK {K.get('name')!r} (`{K.get('id')}`) in "
                        f"{L.get('nome')}/{R.get('nome')} — contiene «{hit}»")
                if rack.get("_uid") in correzioni:
                    voce = correzioni.pop(rack["_uid"])
                    # Il codice registrato nella correzione deve essere quello del
                    # rack: se il seed lo ha rinominato nel frattempo, la riga del
                    # rilievo potrebbe riferirsi a un altro armadio. Ci si ferma.
                    if voce.get("rack") not in (None, rack.get("id")):
                        raise CorrezioneNonValida(
                            f"{dove_k}: la correzione è per il rack {voce.get('rack')!r}, "
                            f"ma questo `_uid` oggi è {rack.get('id')!r}")
                    prima = list(rack.get("seriali") or [])
                    dopo = list(voce["seriali"])
                    if prima != dopo:
                        conteggi["correzioni"].append(
                            (f"{L.get('nome')} / {R.get('nome')} / {rack.get('name') or rack.get('id')}",
                             prima, dopo))
                    rack["seriali"] = dopo
                conteggi["seriali"] += len(rack.get("seriali") or [])

                # ⚠ Il cuore della fase: ogni rack esce VUOTO. Non si filtrano i
                # dispositivi «di prova» dai «veri», perché la distinzione non
                # esiste in questi dati e inventarla significherebbe indovinare.
                # Gli operatori popolano.
                quanti = len(rack.get("devices") or [])
                if quanti:
                    conteggi["devices_removed"] += quanti
                    rimozioni.append(f"{dove_k}: {quanti} dispositivi")
                rack["devices"] = []
                racks.append(rack)

            sequenza, rotture = _file_interrotte(racks)
            for rottura in rotture:
                sospetti.append(
                    f"FILA in {L.get('nome')}/{R.get('nome')} — {rottura} "
                    f"(sequenza: {sequenza})")

            room["racks"] = racks
            sale.append(room)

        loc["sale"] = sale
        locations.append(loc)

    # ⚠ La radice: SOLO le chiavi che lo schema congelato ammette (§8.16), e senza
    # `manuale`.
    #
    # `manuale` assente NON è una dimenticanza: è la forma canonica del manuale
    # vuoto. `canonicalise` non lo inventa se manca, l'applicazione lo legge come
    # `(S.data.manuale || [])`, e le voci integrate del manuale sono nel codice del
    # frontend — non nel documento. Scrivere `manuale: []` aggiungerebbe una chiave
    # che la canonicalizzazione non produce, cioè un artefatto che differisce dal
    # documento che verrebbe salvato. Vedi §4 del requisito e la nota nel MANIFEST.
    if correzioni:
        raise CorrezioneNonValida(
            "correzioni per rack che non esistono nel seed: " + ", ".join(sorted(correzioni)))

    baseline = {
        "schemaVersion": seed["schemaVersion"],
        "locations": locations,
    }
    for key in sorted(set(seed) - {"schemaVersion", "locations"}):
        rimozioni.append(f"radice: {key!r} (legacy del prototipo, §8.16)")

    # Forma CANONICA: l'artefatto è esattamente ciò che finirebbe nel database.
    # Senza questo passo il file sarebbe una forma «equivalente ma diversa», e il
    # digest appuntato non sarebbe quello dell'istantanea salvata.
    return canonicalise(baseline), rimozioni, conteggi, sospetti


def manifest_text(doc: dict, sha: str, conteggi: dict, rimozioni: list[str],
                  sospetti: list[str]) -> str:
    """Il manifesto strutturale: revisionabile DA UNA PERSONA (§19).

    Non è un doppione dei test. Serve a un'altra domanda: «questa è la struttura
    giusta?», che nessuna asserzione può rispondere — un sito di prova rimasto
    dentro passa qualunque validazione di schema.
    """
    righe = [
        "# Manifesto strutturale della baseline di produzione",
        "",
        "⚠ **Generato** da `tools/make-production-baseline.py`. Non si modifica a mano:",
        "si modifica la struttura, si rigenera, e si RILEGGE questo file.",
        "",
        "Serve a una domanda che nessun test può porre: *questa è la struttura giusta?*",
        "Un sito di prova rimasto dentro passa qualunque validazione di schema; una sala",
        "vera dimenticata fuori pure. Quindi va letta da una persona, sito per sito.",
        "",
        f"- digest canonico: `{sha}`",
        f"- siti: **{conteggi['locations']}**",
        f"- sale: **{conteggi['rooms']}**",
        f"- rack: **{conteggi['racks']}**",
        f"- dispositivi: **0** (rimossi: {conteggi['devices_removed']})",
        f"- voci di manuale: **0** (chiave `manuale` assente: è il manuale vuoto canonico)",
        f"- seriali di armadio conservati: **{conteggi['seriali']}**",
        f"- vani (geometria) conservati: **{conteggi['vani']}**",
        f"- seriali corretti dal rilievo (`rack-serials.json`): **{len(conteggi.get('correzioni') or [])}** rack",
        "",
    ]
    if conteggi.get("correzioni"):
        righe += [
            "## Seriali degli armadi corretti dal rilievo",
            "",
            "Da `deploy/baseline/rack-serials.json`. Ogni riga sostituisce i seriali del",
            "seed con quelli letti sulla targhetta: verificare che il rack sia quello giusto.",
            "",
            "| rack | prima | dopo |",
            "|---|---|---|",
        ]
        for dove, prima, dopo in conteggi["correzioni"]:
            righe.append(f"| {dove} | {', '.join(prima) or '—'} | {', '.join(dopo) or '—'} |")
        righe.append("")
    if sospetti:
        righe += [
            "## ⚠ Da confermare",
            "",
            "Due tipi di segnalazione, e nessuna delle due rimuove niente.",
            "",
            "**NOMI** che contengono una parola tipica dei dati di prova: in un CED vero un",
            "rack che si chiama «Test» è spesso un rack vero, che ospita i sistemi di",
            "collaudo. Ma la differenza fra quello e un rack di sviluppo rimasto dentro non",
            "la può stabilire un programma, e uno solo dei due casi deve finire in",
            "produzione.",
            "",
            "**FILE** le cui etichette non sono raggruppate nell'ordine del documento: un",
            "rack di fila «B» che compare dopo il blocco «C». In un inventario cresciuto per",
            "accodamento è normale — due rack aggiunti dopo e messi in fondo — e in un",
            "inventario battuto a mano è un errore di digitazione. Dai dati i due casi non",
            "si distinguono.",
            "",
            "Chi conosce le sale conferma riga per riga:",
            "",
        ]
        righe += [f"- [ ] {s}" for s in sospetti]
        righe.append("")

    righe += [
        "## Struttura",
        "",
    ]
    for L in doc["locations"]:
        righe.append(f"### {L.get('nome') or '(senza nome)'} — `{L.get('id')}`")
        righe.append("")
        righe.append(f"`_uid` {L['_uid']}")
        righe.append("")
        for R in L["sale"]:
            vani = len(R.get("vani") or [])
            segn = " · **segnaposto**" if R.get("segnaposto") else ""
            righe.append(f"#### Sala {R.get('nome')} — `{R.get('id')}`{segn}")
            righe.append("")
            righe.append(f"`_uid` {R['_uid']} · {R.get('w')}×{R.get('h')} m · "
                         f"{vani} van{'o' if vani == 1 else 'i'} · "
                         f"{len(R['racks'])} rack")
            righe.append("")
            righe.append("| # | nome | codice | fila | U | seriali armadio |")
            righe.append("|---|---|---|---|---|---|")
            for n, K in enumerate(R["racks"], 1):
                ser = ", ".join(K.get("seriali") or []) or "—"
                fila = K.get("row") or "(nessuna)"
                righe.append(f"| {n} | {K.get('name')} | `{K.get('id')}` | "
                             f"{fila} | {K.get('u')} | {ser} |")
            righe.append("")

    righe += [
        "## Che cosa è stato tolto",
        "",
        "Elenco completo, generato: se qui compare qualcosa che doveva restare, la",
        "baseline è sbagliata e va rigenerata dopo aver corretto la struttura.",
        "",
    ]
    for r in rimozioni:
        righe.append(f"- {r}")
    righe.append("")
    return "\n".join(righe)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="riporta senza scrivere niente")
    ap.add_argument("--aggiorna-digest", action="store_true",
                    help="scrive il digest nuovo al posto di quello appuntato (dopo aver letto il manifesto)")
    args = ap.parse_args()

    seed = json.loads(SEED.read_text(encoding="utf-8"))
    try:
        correzioni = carica_correzioni()
        doc, rimozioni, conteggi, sospetti = build(seed, correzioni)
    except Ignota as exc:
        print("FERMO: campo strutturale non classificato\n", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 2
    except CorrezioneNonValida as exc:
        print(f"FERMO: correzione dei seriali non valida\n{exc}", file=sys.stderr)
        return 2

    sha = canonical_sha256(doc)
    testo = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    print("baseline di produzione — generazione")
    print(f"  sorgente     {SEED.relative_to(ROOT)}")
    print(f"  siti         {conteggi['locations']}")
    print(f"  sale         {conteggi['rooms']}")
    print(f"  rack         {conteggi['racks']}  (tutti vuoti)")
    print(f"  dispositivi  0  (rimossi {conteggi['devices_removed']})")
    print(f"  seriali      {conteggi['seriali']} numeri di armadio conservati")
    print(f"  vani         {conteggi['vani']} conservati con la geometria")
    print(f"  correzioni   {len(conteggi['correzioni'])} rack con i seriali del rilievo")
    print(f"  digest       {sha}")
    print(f"  byte         {len(testo.encode('utf-8'))}")
    if sospetti:
        print("")
        print(f"  ⚠ {len(sospetti)} nomi da CONFERMARE a mano "
              "(sezione «Da confermare» del manifesto):")
        for s_ in sospetti:
            print(f"      {s_}")

    if args.dry_run:
        print("\n--dry-run: niente scritto")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DOC.write_text(testo, encoding="utf-8")
    OUT_MANIFEST.write_text(
        manifest_text(doc, sha, conteggi, rimozioni, sospetti), encoding="utf-8")
    print(f"\nscritti:\n  {OUT_DOC.relative_to(ROOT)}\n  {OUT_MANIFEST.relative_to(ROOT)}")
    sys.path.insert(0, str(ROOT / "backend"))
    from app.inventory.baseline import BASELINE_CANONICAL_SHA256 as appuntato
    if sha == appuntato:
        print("\n  digest invariato: niente da aggiornare.")
    elif not args.aggiorna_digest:
        print(f"\n⚠ Il digest è cambiato: appuntato {appuntato[:16]}…, nuovo {sha[:16]}….")
        print("  Leggere deploy/baseline/MANIFEST.md, poi rieseguire con --aggiorna-digest:")
        print("  è il passo che rende una modifica di produzione un atto deliberato.")
    else:
        print(f"\n  digest appuntato: {appuntato[:16]}… → {sha[:16]}…")
        for rel in PINNED_IN:
            f = ROOT / rel
            raw = f.read_bytes()
            n = raw.count(appuntato.encode())
            if n:
                f.write_bytes(raw.replace(appuntato.encode(), sha.encode()))
            print(f"    {rel}: {n} sostituzion{'e' if n == 1 else 'i'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
