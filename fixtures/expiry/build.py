#!/usr/bin/env python3
"""Inventario di prova con scadenze RELATIVE a una data di riferimento.

Le date fisse in un file smettono di provare ciò che dicono il giorno dopo:
`2026-09-15` è «fra 30 giorni» soltanto per una settimana. Qui si genera
l'inventario a partire da una data, così la stessa fixture resta valida per
sempre e i test possono spostare «oggi» a piacere — compresi i giorni del cambio
dell'ora legale.

Riferimento: BACKEND-PLAN.md §8.41.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

CURRENT_SCHEMA_VERSION = 1

LOC = "aaaaaaaa-0000-4000-8000-0000000000e1"
ROOM = "bbbbbbbb-0000-4000-8000-0000000000e1"
RACK_A = "cccccccc-0000-4000-8000-0000000000ea"
RACK_B = "cccccccc-0000-4000-8000-0000000000eb"

#: Nome con markup e con un tentativo di iniezione di intestazione. Non è
#: folklore: i nomi dei dispositivi li scrive una persona, spesso incollandoli, e
#: un `\r\n` in un nome non deve poter aggiungere un destinatario al digest.
NOME_OSTILE = "<b>srv-x</b>\r\nBcc: qualcuno@altrove.example"


def _dev(uid: str, ident: str, name: str, *, garanzia: str = "",
         supporto: str = "", u: int = 1) -> dict:
    return {"_uid": uid, "id": ident, "name": name, "u": u,
            "garanzia": garanzia, "supporto": supporto}


def build_inventory(reference: date) -> dict:
    """Inventario con scadenze calcolate rispetto a `reference`."""
    def d(offset: int) -> str:
        return (reference + timedelta(days=offset)).isoformat()

    devices_a = [
        _dev("dddddddd-0000-4000-8000-000000000001", "srv-oggi", "srv-oggi",
             garanzia=d(0)),
        _dev("dddddddd-0000-4000-8000-000000000002", "srv-7", "srv-7",
             garanzia=d(7)),
        # Dentro la finestra da 7 senza essere il giorno esatto: è il caso del
        # recupero, quello che una regola `giorni == N` perderebbe.
        _dev("dddddddd-0000-4000-8000-000000000003", "srv-6", "srv-6",
             garanzia=d(6)),
        _dev("dddddddd-0000-4000-8000-000000000004", "srv-30", "srv-30",
             garanzia=d(30), supporto=d(30)),
        _dev("dddddddd-0000-4000-8000-000000000005", "srv-29", "srv-29",
             garanzia=d(29)),
        _dev("dddddddd-0000-4000-8000-000000000006", "srv-90", "srv-90",
             garanzia=d(90)),
        # Fuori da ogni finestra: non deve comparire in nessun digest.
        _dev("dddddddd-0000-4000-8000-000000000007", "srv-91", "srv-91",
             garanzia=d(91)),
    ]

    devices_b = [
        # Già scaduti: esclusi dallo scheduler per scelta esplicita, visibili
        # nella vista Scadenze.
        _dev("dddddddd-0000-4000-8000-000000000010", "srv-scaduto",
             "srv-scaduto", garanzia=d(-10)),
        _dev("dddddddd-0000-4000-8000-000000000011", "srv-scaduto-ieri",
             "srv-scaduto-ieri", garanzia=d(-1)),
        # Campi vuoti, il caso più comune nell'inventario reale.
        _dev("dddddddd-0000-4000-8000-000000000012", "srv-senza-date",
             "srv-senza-date"),
        # Testo non interpretabile come data: si ignora in silenzio invece di far
        # cadere il giro. Il posto dove si nota è la vista Scadenze.
        _dev("dddddddd-0000-4000-8000-000000000013", "srv-data-rotta",
             "srv-data-rotta", garanzia="in attesa", supporto="2026-13-45"),
        # STESSO id di business, `_uid` diversi: due promemoria distinti. Con gli
        # inventari importati da fogli di calcolo gli id ripetuti sono la norma.
        _dev("dddddddd-0000-4000-8000-000000000020", "SRV-DUP", "dup-a",
             garanzia=d(7)),
        _dev("dddddddd-0000-4000-8000-000000000021", "SRV-DUP", "dup-b",
             garanzia=d(30)),
        # Nome ostile.
        _dev("dddddddd-0000-4000-8000-000000000030", "srv-iniezione",
             NOME_OSTILE, garanzia=d(7)),
    ]

    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "locations": [{
            "_uid": LOC, "id": "pomezia", "nome": "Pomezia G0",
            "sale": [{
                "_uid": ROOM, "id": "sala-1", "nome": "Sala 1",
                "w": 10, "h": 8, "vani": [],
                "racks": [
                    {"_uid": RACK_A, "id": "R01", "name": "R01", "u": 45,
                     "x": 0.2, "y": 0.2, "w": 0.6, "h": 0.8,
                     "devices": devices_a},
                    {"_uid": RACK_B, "id": "R02", "name": "R02", "u": 45,
                     "x": 0.6, "y": 0.2, "w": 0.6, "h": 0.8,
                     "devices": devices_b},
                ],
            }],
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-from", default=date.today().isoformat(),
                    help="data di riferimento, YYYY-MM-DD")
    args = ap.parse_args()
    ref = date.fromisoformat(args.days_from)
    json.dump(build_inventory(ref), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
