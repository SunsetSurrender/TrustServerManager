#!/usr/bin/env python3
"""Proiezione relazionale dell'inventario: stato, verifica, ricostruzione.

Comando ESPLICITO, che gira come PROPRIETARIO dello schema. Non è una migrazione di
dati e non è un servizio, e la differenza è deliberata (§8.42):

  - una migrazione di dati si esegue una volta sola, all'avvio, senza che nessuno la
    guardi, e se aborta ferma il deployment. Questo popolamento deve poter essere
    rieseguito, deve confrontare un digest, deve poter dire di no, e il suo esito
    deve essere LETTO da una persona;
  - un servizio lo eseguirebbe da solo. Nessuno consuma ancora la proiezione: farla
    aggiornare in automatico vorrebbe dire mantenere una rappresentazione che nessuno
    legge, e scoprire i guasti quando qualcuno comincerà a leggerla.

Uso:
    python scripts/project.py --status     che versione rispecchia (sola lettura)
    python scripts/project.py --verify     riassembla da SQL e confronta (sola lettura)
    python scripts/project.py --rebuild    ricostruisce, e aborta se non torna

In Compose gira dal servizio che è già il proprietario dello schema:

    docker compose run --rm migrate python scripts/project.py --status

Codici di uscita, e la ragione della differenza:

    --status    sempre 0. È un rapporto. Una proiezione non aggiornata in fase 2B
                è NORMALE — la sincronizzazione a ogni salvataggio è la 2C — e un
                codice di errore qui insegnerebbe a ignorarlo.
    --verify    0 se le tabelle riassemblano esattamente la versione che dichiarano
                di rispecchiare, 1 altrimenti. È un'asserzione: la fedeltà è l'unica
                cosa che può essere un guasto adesso. L'attualità viene riportata a
                parte, senza influire sul codice.
    --rebuild   0 se costruita e verificata, 1 se abortita (e in quel caso nel
                database non è cambiato niente).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine                        # noqa: E402

from app.config import get_settings                         # noqa: E402
from app.inventory import projection                        # noqa: E402
from app.inventory.errors import NotBootstrappedError       # noqa: E402
from app.inventory.projection import ProjectionAborted      # noqa: E402


def _print_counts(counts: dict) -> None:
    etichette = {"locations": "siti", "rooms": "sale", "racks": "rack",
                 "devices": "dispositivi", "manual": "voci di manuale"}
    print("  righe        " + ", ".join(
        f"{n} {etichette.get(k, k)}" for k, n in counts.items()))


def _print_status(state: projection.ProjectionStatus) -> None:
    if state.head_version is None:
        print("  testa        nessuna (inventario non inizializzato)")
    else:
        print(f"  testa        versione {state.head_version}, "
              f"digest {(state.head_sha256 or '')[:12]}…")
    if state.present:
        print(f"  proiezione   versione {state.projected_version}, "
              f"digest {(state.projected_sha256 or '')[:12]}…, "
              f"costruita il {state.projected_at:%Y-%m-%d %H:%M:%S %Z}")
    else:
        print("  proiezione   nessuno stato registrato")
    _print_counts(state.counts)
    print(f"  esito        {state.describe()}")


def _print_details(details: list) -> None:
    for item in details[:25]:
        riga = ", ".join(f"{k}={v}" for k, v in item.items())
        # Si tronca: un messaggio di validazione può essere lungo tre righe di
        # terminale, e venticinque messaggi così nascondono il rapporto invece di
        # spiegarlo. Il dettaglio completo sta nel codice del controllo.
        print("    - " + (riga if len(riga) <= 160 else riga[:159] + "…"))
    if len(details) > 25:
        print(f"    … e altre {len(details) - 25}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true",
                       help="che versione la proiezione rispecchia (sola lettura)")
    group.add_argument("--verify", action="store_true",
                       help="riassembla da SQL e confronta i digest (sola lettura)")
    group.add_argument("--rebuild", action="store_true",
                       help="ricostruisce la proiezione dalla testa, e aborta se il "
                            "giro completo non torna")
    args = ap.parse_args()

    engine = create_engine(get_settings().sqlalchemy_url(), future=True)
    try:
        if args.status:
            with engine.begin() as conn:
                print("proiezione dell'inventario — stato")
                _print_status(projection.status(conn))
            return 0

        if args.verify:
            with engine.begin() as conn:
                result = projection.verify(conn)
            print("proiezione dell'inventario — verifica")
            _print_status(result.status)
            if result.faithful:
                print("  fedeltà      OK: le tabelle riassemblano la versione che "
                      "dichiarano di rispecchiare")
                return 0
            print(f"  fedeltà      FALLITA ({result.reason})")
            _print_details(result.details)
            return 1

        # --- ricostruzione ---
        #
        # `engine.begin()` fa il commit all'uscita e il rollback su eccezione:
        # `rebuild` solleva a ogni passo che non torna, quindi un abort non lascia
        # nel database nessuna proiezione a metà.
        print("proiezione dell'inventario — ricostruzione")
        try:
            with engine.begin() as conn:
                report = projection.rebuild(conn)
        except ProjectionAborted as aborted:
            print(f"  ABORTITA     {aborted.reason}")
            print(f"               {aborted}")
            _print_details(aborted.details)
            print("  nel database non è cambiato niente: la transazione è stata "
                  "annullata per intero")
            return 1
        except NotBootstrappedError as exc:
            print(f"  ABORTITA     {exc}")
            return 1

        print(f"  versione     {report.version}")
        print(f"  digest       {report.sha256[:12]}… (verificato riassemblando da SQL)")
        _print_counts(report.counts)
        print(f"  scritte      {report.rows_written} righe")
        if report.warnings:
            # Gli avvisi NON fermano niente (§8.42): l'inventario reale è pieno di
            # caselle scritte a mano. Si stampano perché «quel campo, per quella
            # riga, non risponde a una query» è un'informazione che serve.
            print(f"  avvisi       {len(report.warnings)} — la proiezione è fedele, "
                  "questi campi non sono interrogabili:")
            _print_details(report.warnings)
        else:
            print("  avvisi       nessuno")
        print("ricostruzione completata e verificata")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
