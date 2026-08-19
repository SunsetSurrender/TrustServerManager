#!/usr/bin/env python3
"""Canonicalizza i documenti dei corpora di parità. Passo 2 di 3.

⚠ Perché questo passo esiste, che è la scoperta che l'ha reso necessario.

Il frontend NON vede mai un documento come lo scrive una fixture: vede quello che
`GET /api/inventory` restituisce, cioè il documento **canonico**. E la
canonicalizzazione (§8.3) non riordina soltanto: RIEMPIE. Un dispositivo scritto come

    {"_uid": …, "id": "d", "name": "d", "u": 1, "h": 1}

diventa

    {"_uid": …, "id": "d", "name": "d", "u": 1, "h": 1, "stato": "attivo",
     "type": "altro", "model": "", "ip": "", "serial": "", "owner": "",
     "garanzia": "", "supporto": "", "note": ""}

Quindi misurare la parità sul documento grezzo confronterebbe lo SQL — che legge la
proiezione, costruita dal canonico — con un JavaScript che ha girato su un documento
che in produzione non esiste. La prima stesura faceva esattamente questo, e il test è
diventato rosso su `device.type`: `null` da una parte, `"altro"` dall'altra. Non era un
difetto dello SQL, era un difetto del banco di prova.

La catena, che va eseguita in ordine:

    1. node   tools/make-query-fixtures.mjs --emit-docs   → _raw.json
    2. python tools/canonicalise-query-docs.py            → _canonical.json
    3. node   tools/make-query-fixtures.mjs               → i corpora

Uso (dalla radice del repository):
    docker run --rm -v "$PWD":/w -w /w python:3.13-slim \
        sh -c "pip install -q -r backend/requirements.txt && \
               python tools/canonicalise-query-docs.py"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

RAW = ROOT / "fixtures" / "query" / "_raw.json"
OUT = ROOT / "fixtures" / "query" / "_canonical.json"


def main() -> int:
    from app.identity import canonicalise
    from app.inventory.document import strip_legacy_fields

    if not RAW.is_file():
        print(f"{RAW} assente: eseguire prima "
              "`node tools/make-query-fixtures.mjs --emit-docs`", file=sys.stderr)
        return 1

    raw = json.loads(RAW.read_text(encoding="utf-8"))
    out = {}
    for name, doc in raw.items():
        # Le radici legacy (utenti, registro, notifiche) si togliono come fa il
        # bootstrap: il seed le porta, e non sono inventario.
        stripped, removed = strip_legacy_fields(doc)
        out[name] = canonicalise(stripped)
        nota = f"  (togliendo {', '.join(removed)})" if removed else ""
        print(f"{name}: canonicalizzato{nota}")

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\nscritto {OUT.relative_to(ROOT)} ({len(out)} documenti)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
