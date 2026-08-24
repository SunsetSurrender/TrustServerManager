"""La baseline di produzione: l'artefatto, i suoi invarianti, il suo digest appuntato.

Modulo PURO — non sa di connessioni né di transazioni. Le domande sul DATABASE
(«questa installazione è vergine?», «chi può inizializzarla?») stanno in
`scripts/production_baseline.py`, che è il comando; qui c'è solo ciò che si può
stabilire guardando il documento.

Che cos'è la baseline, e che cosa non è
--------------------------------------
È lo stato da cui una installazione NUOVA di Trust Server Manager deve cominciare:
i siti, le sale, la geometria, i rack veri — **vuoti** — e nient'altro. Gli
operatori ci mettono i dispositivi.

Non è un backup, non è il seed di sviluppo, e non è un secondo archivio. In
particolare **non dipende dal seed a runtime**: `fixtures/seed.json` è un file che
qualcuno modifica per provare una cosa, e se la baseline lo leggesse al momento
dell'installazione una prova di martedì diventerebbe la struttura di produzione di
giovedì. L'artefatto è generato una volta, guardato, e committato; da lì è appuntato
a un digest (§20 del requisito della fase 2I).

Il digest appuntato
-------------------
`BASELINE_CANONICAL_SHA256` è la firma della struttura APPROVATA. Fa tre lavori, e
il terzo è quello che conta:

  1. dice se il file sul disco è quello che i test hanno verificato;
  2. rende una modifica della struttura di produzione un ATTO DELIBERATO — si
     rigenera, si rilegge il manifesto, si aggiorna la costante, e la revisione
     vede tutte e tre le cose insieme;
  3. **impedisce che un altro documento venga inizializzato come baseline.** La
     fixture di qualificazione delle notifiche (§15) è un documento valido, di
     forma reale, con dispositivi e scadenze dentro: è esattamente ciò che non deve
     finire in produzione, e il digest è la ragione per cui non può.

Riferimento: BACKEND-PLAN.md §8.52.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.identity import canonicalise
from app.identity.model import walk
from app.inventory.digest import canonical_sha256
from app.inventory.document import validate_normal_document
from app.inventory.relational import assemble, normalise
from app.inventory.relational_validate import errors as relational_errors
from app.inventory.relational_validate import validate_model
from app.photos.refs import photo_ids

#: Digest canonico della struttura APPROVATA. Si aggiorna a mano, dopo aver letto
#: `deploy/baseline/MANIFEST.md`. Vedi il modulo per il perché.
BASELINE_CANONICAL_SHA256 = (
    "febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"
)

#: Percorso dell'artefatto in un checkout del repository. Nell'immagine di
#: produzione `deploy/` non esiste — il build ha per contesto `./backend` — quindi
#: qui non si risolve e il comando PRETENDE `--baseline`. È voluto: meglio chiedere
#: il percorso che indovinarlo.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASELINE_PATH = _REPO_ROOT / "deploy" / "baseline" / "production-baseline.json"


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    path: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


# codici degli invarianti propri della baseline
BASELINE_DIGEST_MISMATCH = "baseline_digest_mismatch"
BASELINE_NOT_CANONICAL = "baseline_not_canonical"
BASELINE_ROUNDTRIP_FAILED = "baseline_roundtrip_failed"
BASELINE_HAS_DEVICES = "baseline_has_devices"
BASELINE_HAS_PHOTO_REFS = "baseline_has_photo_refs"
BASELINE_HAS_MANUAL = "baseline_has_manual"
BASELINE_NOT_A_DOCUMENT = "baseline_not_a_document"


def load(path: Path | str | None = None) -> dict:
    """Legge l'artefatto. Nessuna canonicalizzazione: si vuole sapere com'è SCRITTO."""
    p = Path(path) if path is not None else DEFAULT_BASELINE_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def counts(doc: Any) -> dict[str, int]:
    """Conteggi strutturali, contati camminando l'albero.

    ⚠ I dispositivi si contano DAVVERO, e non si deduce «zero» da `devices: []`
    (§6 del requisito): un dispositivo può stare in un ramo che nessuno guarda —
    un rack fuori posto, una chiave sbagliata — e «gli array sono vuoti» è
    un'affermazione su ciò che si è pensato di controllare. `walk` visita per
    tipo di entità, quindi trova un dispositivo dovunque sia.
    """
    out = {"locations": 0, "rooms": 0, "racks": 0, "devices": 0, "manual": 0,
           "vani": 0, "rack_serials": 0}
    for e in walk(doc):
        if e.kind == "location":
            out["locations"] += 1
        elif e.kind == "room":
            out["rooms"] += 1
            out["vani"] += len(e.obj.get("vani") or [])
        elif e.kind == "rack":
            out["racks"] += 1
            out["rack_serials"] += len(e.obj.get("seriali") or [])
        elif e.kind == "device":
            out["devices"] += 1
        elif e.kind == "manual":
            out["manual"] += 1
    return out


def _device_leftovers(doc: Any) -> list[Problem]:
    """Tracce di dati di dispositivo, cercate una per una.

    Non è un doppione di `counts`: là si conta ciò che l'albero dichiara essere un
    dispositivo, qui si cerca il RESIDUO — una `garanzia` sopravvissuta, un
    `presenza`, un `serial` — dovunque compaia, anche fuori da un `devices`. Le due
    domande hanno risposte diverse quando qualcosa è andato storto, ed è per quei
    casi che esistono i controlli.
    """
    problemi: list[Problem] = []
    sospette = ("garanzia", "supporto", "presenza")

    def visita(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                qui = f"{path}.{k}" if path else k
                if k in sospette and v not in (None, ""):
                    problemi.append(Problem(
                        BASELINE_HAS_DEVICES,
                        f"campo di dispositivo '{k}' = {v!r} in una baseline che "
                        f"non deve avere dispositivi", path=qui))
                visita(v, qui)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                visita(v, f"{path}[{i}]")

    visita(doc, "")
    return problemi


def invariants(doc: Any, *, expect_sha: str | None = BASELINE_CANONICAL_SHA256,
               ) -> list[Problem]:
    """Tutto ciò che deve valere perché questo documento sia UNA baseline.

    L'ordine è quello dell'utilità diagnostica: prima le regole che valgono per
    qualunque scrittura di produzione (lo schema congelato), poi quelle proprie
    della baseline. Si riportano TUTTI i problemi, non il primo: chi guarda vuole
    sapere quanti sono.
    """
    if not isinstance(doc, dict):
        return [Problem(BASELINE_NOT_A_DOCUMENT,
                        f"la baseline non è un oggetto: {type(doc).__name__}")]

    problemi: list[Problem] = []

    # ---- 1. le stesse regole di una scrittura normale di produzione ----
    #
    # Non un sottoinsieme e non una variante: è ESATTAMENTE la funzione che il `PUT`
    # esegue. Una baseline che passasse controlli più permissivi di quelli di un
    # salvataggio sarebbe uno stato iniziale che l'applicazione non saprebbe
    # riprodurre — e il primo salvataggio di un operatore lo rifiuterebbe.
    for e in validate_normal_document(doc):
        problemi.append(Problem(e.code, e.message, e.path))

    # ---- 2. forma canonica: l'artefatto è ciò che verrebbe salvato ----
    #
    # Se `canonicalise(doc) != doc`, il file sul disco NON è il documento che
    # finisce nel database: è una forma equivalente. Sarebbe tollerabile e sarebbe
    # una bugia — il digest appuntato non varrebbe per l'istantanea salvata, e
    # rivedere il file non sarebbe rivedere ciò che si installa.
    canonico = canonicalise(doc)
    if canonico != doc:
        problemi.append(Problem(
            BASELINE_NOT_CANONICAL,
            "l'artefatto non è in forma canonica: il documento salvato "
            "differirebbe dal file revisionato. Rigenerare con "
            "tools/make-production-baseline.py."))

    # ---- 3. il modello relazionale, e il giro completo ----
    modello = normalise(canonico)
    for p in relational_errors(validate_model(modello, known_photo_ids=set())):
        problemi.append(Problem(p.code, p.message, getattr(p, "path", "")))

    # L'invariante che il requisito scrive per intero (§5):
    #     canonicalise(assemble(normalise(baseline))) == canonicalise(baseline)
    riassemblato = canonicalise(assemble(modello))
    if riassemblato != canonico:
        problemi.append(Problem(
            BASELINE_ROUNDTRIP_FAILED,
            "il giro documento → modello → documento non torna: la baseline non "
            "sarebbe rappresentabile dalle tabelle che la servono"))

    # ---- 4. zero dispositivi, provato e non dedotto ----
    c = counts(canonico)
    if c["devices"]:
        problemi.append(Problem(
            BASELINE_HAS_DEVICES,
            f"{c['devices']} dispositivi nella baseline: una installazione nuova "
            f"parte da rack VUOTI e gli operatori li popolano"))
    problemi += _device_leftovers(canonico)

    # ---- 5. nessun riferimento a foto ----
    #
    # §9: la tabella `photos` nasce vuota. Un riferimento superstite non sarebbe
    # inerte — `bootstrap` pretende che i riferimenti esistano (§8.5) — quindi
    # l'inizializzazione fallirebbe a metà, che è il momento peggiore per scoprirlo.
    refs = photo_ids(canonico)
    if refs:
        problemi.append(Problem(
            BASELINE_HAS_PHOTO_REFS,
            f"{len(refs)} riferimenti a foto: la tabella photos di una "
            f"installazione nuova è vuota, e il bootstrap li pretende esistenti"))

    # ---- 6. il digest appuntato ----
    if expect_sha is not None:
        effettivo = canonical_sha256(canonico)
        if effettivo != expect_sha:
            problemi.append(Problem(
                BASELINE_DIGEST_MISMATCH,
                f"digest {effettivo} invece di {expect_sha}. Se la struttura è "
                f"cambiata di proposito: rigenerare, LEGGERE "
                f"deploy/baseline/MANIFEST.md, e aggiornare "
                f"BASELINE_CANONICAL_SHA256. Se non è cambiata di proposito, "
                f"questo è il controllo che serviva."))

    return problemi


def document_manifest(doc: Any) -> dict:
    """Rapporto sul DOCUMENTO, senza database e senza segreti (§18).

    Deterministico: le stesse chiavi e gli stessi valori per lo stesso artefatto.
    I metadati di esecuzione (data, host) NON stanno qui — li aggiunge il comando,
    etichettati, così questo resta confrontabile fra due esecuzioni.
    """
    canonico = canonicalise(doc) if isinstance(doc, dict) else doc
    c = counts(canonico) if isinstance(canonico, dict) else {}
    problemi = invariants(doc)
    return {
        "schemaVersion": doc.get("schemaVersion") if isinstance(doc, dict) else None,
        "canonical_sha256": canonical_sha256(canonico) if isinstance(canonico, dict) else None,
        "expected_sha256": BASELINE_CANONICAL_SHA256,
        "counts": c,
        "photo_refs": len(photo_ids(canonico)) if isinstance(canonico, dict) else None,
        "problems": [p.as_dict() for p in problemi],
        "ok": not problemi,
    }
