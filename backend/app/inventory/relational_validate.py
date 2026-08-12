"""Coerenza di un modello relazionale. Pura, e con due livelli di gravità.

`normalise` non solleva mai: costruisce il modello che il documento descrive,
anche se il documento è incoerente. Questo modulo lo esamina e dice cosa non
torna — tutto insieme, non «la prima cosa che è andata storta», perché la
migrazione della fase 2B deve poter riportare l'elenco completo prima di
rinunciare.

Due gravità, e la differenza conta
----------------------------------
`ERROR` — il modello non può rappresentare fedelmente lo stato: identità
duplicate, genitori inesistenti, righe contraddittorie. Un `ERROR` deve fermare
una migrazione o una scrittura.

`WARNING` — lo stato è rappresentato correttamente ma la tabella non lo può
interrogare, oppure il valore è fuori da un vocabolario noto. Un `WARNING` non
deve fermare niente: l'inventario reale è pieno di caselle scritte a mano, e
rifiutarle vorrebbe dire perdere il dato invece di correggerlo. Serve a sapere
che quel campo, per quella riga, non risponderà a una query.

⚠ Perché i codici di dispositivo duplicati NON sono un errore
-------------------------------------------------------------
Nella struttura (siti, sale, rack) un codice ripetuto è un errore: l'interfaccia
stessa lo rifiuta («ID già esistente in questa sala»), e il database lo impedisce
con un vincolo di unicità con ambito.

Per i DISPOSITIVI no. Il codice di un dispositivo arriva dall'import tabellare,
dove due righe con lo stesso identificativo di asset nello stesso rack sono un
caso reale, e il validatore di identità (§8.4) le tollera da sempre. Metterci un
vincolo significherebbe che la fase 2C rifiuta documenti che la fase 1 accetta —
un cambio di comportamento introdotto di straforo da un commit che dichiara di non
cambiarne nessuno. Resta un `WARNING`, e diventerà una decisione di prodotto
quando qualcuno vorrà prenderla.

Riferimento: BACKEND-PLAN.md §8.42.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.identity.model import is_uid
from app.inventory.relational import (
    DEVICE_STATES,
    DEVICE_TYPES,
    FIELD_MAP,
    RelationalModel,
    document_key,
)

ERROR = "error"
WARNING = "warning"

# --- errori: il modello non rappresenta fedelmente lo stato ---
MISSING_UID = "missing_uid"
MALFORMED_UID = "malformed_uid"
DUPLICATE_UID = "duplicate_uid"
UNKNOWN_PARENT = "unknown_parent"
DUPLICATE_SCOPED_CODE = "duplicate_scoped_code"
DUPLICATE_ORDINAL = "duplicate_ordinal"
INVALID_ORDINAL = "invalid_ordinal"
MALFORMED_ROW = "malformed_row"
EXTRA_SHADOWS_COLUMN = "extra_shadows_column"
PHOTO_NOT_FOUND = "photo_not_found"
MISSING_SCHEMA_VERSION = "missing_schema_version"

# --- avvisi: rappresentato, ma non interrogabile o fuori vocabolario ---
CARRIED_VERBATIM = "carried_verbatim"
NON_CONTIGUOUS_ORDINAL = "non_contiguous_ordinal"
DUPLICATE_DEVICE_CODE = "duplicate_device_code"
INVALID_ENUM = "invalid_enum"
INVALID_DATE = "invalid_date"

#: Tipi le cui collezioni non ammettono due codici uguali nello stesso ambito.
#: I dispositivi non ci sono, di proposito: vedi la nota in testa al modulo.
UNIQUE_CODE_KINDS = ("location", "room", "rack", "manual")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    kind: str
    uid: Any
    message: str
    field: str | None = None

    def as_dict(self) -> dict:
        d = {"code": self.code, "severity": self.severity, "kind": self.kind,
             "message": self.message}
        if self.uid is not None:
            d["uid"] = str(self.uid)
        if self.field:
            d["field"] = self.field
        return d


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == ERROR]


def warnings(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == WARNING]


def codes(findings: Iterable[Finding]) -> list[str]:
    return sorted({f.code for f in findings})


# ==================================================================

def _rows(model: RelationalModel) -> list[tuple[str, Any, str | None, Any]]:
    """(tipo, riga, nome della colonna del genitore, uid del genitore)."""
    out: list[tuple[str, Any, str | None, Any]] = []
    out += [("location", r, None, None) for r in model.locations]
    out += [("room", r, "location_uid", r.location_uid) for r in model.rooms]
    out += [("rack", r, "room_uid", r.room_uid) for r in model.racks]
    out += [("device", r, "rack_uid", r.rack_uid) for r in model.devices]
    out += [("manual", r, None, None) for r in model.manual]
    return out


def validate_model(model: RelationalModel, *,
                   known_photo_ids: Iterable[str] | None = None) -> list[Finding]:
    """Tutte le incoerenze del modello, errori e avvisi insieme.

    `known_photo_ids` è l'insieme delle foto che esistono davvero. Se non viene
    fornito, il riferimento alla foto NON viene controllato: questo modulo è puro e
    non sa interrogare la tabella `photos`. Passare un insieme vuoto significa
    «nessuna foto esiste», che è diverso da «non controllare» — e la differenza
    conta, perché un controllo saltato per distrazione somiglia molto a un
    controllo passato.
    """
    found: list[Finding] = []
    add = found.append

    if model.schema_version is None:
        add(Finding(MISSING_SCHEMA_VERSION, ERROR, "document", None,
                    "il modello non dichiara schemaVersion: un documento senza "
                    "versione di schema va rifiutato, non aggiornato in silenzio "
                    "(§8.13)"))

    all_rows = _rows(model)

    # ---------------------------------------------------------- identità
    seen: dict[Any, str] = {}
    for kind, row, _pcol, _puid in all_rows:
        if row.uid is None or row.uid == "":
            add(Finding(MISSING_UID, ERROR, kind, None,
                        f"{kind} senza _uid: l'identità è la chiave primaria"))
            continue
        if not is_uid(row.uid):
            add(Finding(MALFORMED_UID, ERROR, kind, row.uid,
                        f"{kind}: _uid non conforme a UUID v4"))
            continue
        if row.uid in seen:
            add(Finding(DUPLICATE_UID, ERROR, kind, row.uid,
                        f"_uid duplicato fra {seen[row.uid]} e {kind}: "
                        "la chiave primaria è l'identità, e due entità non "
                        "possono condividerla"))
            continue
        seen[row.uid] = kind

    # ------------------------------------------------------- genitori
    parents = {"room": {r.uid for r in model.locations},
               "rack": {r.uid for r in model.rooms},
               "device": {r.uid for r in model.racks}}
    for kind, row, pcol, puid in all_rows:
        if pcol is None:
            continue
        if puid is None or puid not in parents[kind]:
            add(Finding(UNKNOWN_PARENT, ERROR, kind, row.uid,
                        f"{kind}: {pcol}={puid!r} non corrisponde a nessuna riga "
                        "esistente. La riga non appartiene a nessun documento "
                        "assemblabile, e non viene attaccata altrove per finta.",
                        field=pcol))

    # -------------------------------------------- righe contraddittorie
    for kind, row, _pcol, _puid in all_rows:
        if not isinstance(row.extra, dict):
            add(Finding(MALFORMED_ROW, ERROR, kind, row.uid,
                        f"{kind}: `extra` non è un oggetto ({type(row.extra).__name__})",
                        field="extra"))
            continue
        for column, key, _fits in FIELD_MAP[kind]:
            if key in row.extra:
                if getattr(row, column) is not None:
                    # La regola del modulo: la colonna vale NULL ⇔ la chiave è in
                    # `extra`. Entrambe valorizzate significa due verità sullo
                    # stesso campo, e il riassemblaggio ne sceglierebbe una in
                    # silenzio.
                    add(Finding(EXTRA_SHADOWS_COLUMN, ERROR, kind, row.uid,
                                f"{kind}.{key}: valorizzato sia nella colonna "
                                f"`{column}` sia in `extra`",
                                field=key))
                else:
                    add(Finding(CARRIED_VERBATIM, WARNING, kind, row.uid,
                                f"{kind}.{key}: valore non rappresentabile nella "
                                f"colonna `{column}`, conservato in `extra`. Il "
                                "documento è integro; quel campo, per questa riga, "
                                "non risponde a una query.",
                                field=key))
        if not isinstance(row.ordinal, int) or isinstance(row.ordinal, bool) \
                or row.ordinal < 0:
            add(Finding(INVALID_ORDINAL, ERROR, kind, row.uid,
                        f"{kind}: ordinal={row.ordinal!r} non è un intero non negativo",
                        field="ordinal"))

    # ------------------------------------------------ ordine e codici
    _check_collection(add, "location", model.locations, None)
    _check_collection(add, "manual", model.manual, None)
    for L in model.locations:
        _check_collection(add, "room",
                          [r for r in model.rooms if r.location_uid == L.uid], L.uid)
    for R in model.rooms:
        _check_collection(add, "rack",
                          [r for r in model.racks if r.room_uid == R.uid], R.uid)
    for K in model.racks:
        _check_collection(add, "device",
                          [d for d in model.devices if d.rack_uid == K.uid], K.uid)

    # ----------------------------------------------------------- foto
    if known_photo_ids is not None:
        known = {str(p) for p in known_photo_ids}
        for rack in model.racks:
            if rack.photo_id is not None and str(rack.photo_id) not in known:
                add(Finding(PHOTO_NOT_FOUND, ERROR, "rack", rack.uid,
                            f"rack: la foto {rack.photo_id} non esiste. Caricare "
                            "l'immagine prima di salvare il rack (§8.5).",
                            field="photo_id"))

    # ------------------------------------------ vocabolari e date
    for device in model.devices:
        if device.type is not None and device.type not in DEVICE_TYPES:
            add(Finding(INVALID_ENUM, WARNING, "device", device.uid,
                        f"device.type={device.type!r} fuori dal vocabolario noto "
                        f"({', '.join(DEVICE_TYPES)}): l'interfaccia lo mostrerà "
                        "come «Altro»",
                        field="type"))
        if device.stato is not None and device.stato not in DEVICE_STATES:
            add(Finding(INVALID_ENUM, WARNING, "device", device.uid,
                        f"device.stato={device.stato!r} fuori dal vocabolario noto "
                        f"({', '.join(DEVICE_STATES)})",
                        field="stato"))
        for column in ("garanzia", "supporto"):
            value = getattr(device, column)
            if value in (None, ""):
                continue
            # ⚠ Si usa il parser dello SCANNER delle scadenze, non un secondo
            # controllo scritto qui: così l'avviso significa esattamente «il worker
            # ignorerà questa data» (§8.41). Due idee di «data valida» in due
            # moduli divergono, e divergerebbero proprio sui casi limite.
            from app.notifications.expiry import parse_expiry
            if parse_expiry(value) is None:
                add(Finding(INVALID_DATE, WARNING, "device", device.uid,
                            f"device.{column}={value!r} non è una data "
                            "`YYYY-MM-DD`: lo scanner delle scadenze la ignorerà",
                            field=column))

    return found


def _check_collection(add, kind: str, rows: list, parent_uid: Any) -> None:
    """Ordinali e codici di una singola collezione di fratelli."""
    ordinals: dict[Any, Any] = {}
    for row in rows:
        if row.ordinal in ordinals:
            add(Finding(DUPLICATE_ORDINAL, ERROR, kind, row.uid,
                        f"{kind}: ordinal={row.ordinal} usato anche da "
                        f"{ordinals[row.ordinal]}. L'ordine è un dato, e due righe "
                        "nella stessa posizione lo rendono indeterminato.",
                        field="ordinal"))
        else:
            ordinals[row.ordinal] = row.uid

    numeric = sorted(o for o in ordinals if isinstance(o, int)
                     and not isinstance(o, bool))
    if numeric and numeric != list(range(len(numeric))):
        add(Finding(NON_CONTIGUOUS_ORDINAL, WARNING, kind, parent_uid,
                    f"{kind}: ordinali non contigui {numeric}. Il riassemblaggio "
                    "resta corretto (si ordina, non si indicizza), ma un buco "
                    "segnala una scrittura incompleta.",
                    field="ordinal"))

    if kind in UNIQUE_CODE_KINDS:
        seen: dict[Any, Any] = {}
        for row in rows:
            if row.code is None:
                continue
            if row.code in seen:
                add(Finding(DUPLICATE_SCOPED_CODE, ERROR, kind, row.uid,
                            f"{kind}: codice {row.code!r} già usato da "
                            f"{seen[row.code]} nello stesso ambito",
                            field=document_key(kind, "code")))
            else:
                seen[row.code] = row.uid
    elif kind == "device":
        seen = {}
        for row in rows:
            if row.code is None:
                continue
            if row.code in seen:
                add(Finding(DUPLICATE_DEVICE_CODE, WARNING, kind, row.uid,
                            f"device: identificativo {row.code!r} già usato da "
                            f"{seen[row.code]} nello stesso rack. Ammesso — arriva "
                            "dall'import tabellare — ma l'identità resta l'_uid.",
                            field="id"))
            else:
                seen[row.code] = row.uid
