#!/usr/bin/env python3
"""Proiezione relazionale dell'inventario: stato, verifica, ricostruzione.

Comando ESPLICITO, che gira come PROPRIETARIO dello schema. Non è una migrazione di
dati, e la differenza è deliberata (§8.42, §8.44): una migrazione si esegue una volta
sola, all'avvio, senza che nessuno la guardi, e se aborta ferma il deployment. Questa
ricostruzione deve poter essere rieseguita, deve confrontare un digest, deve poter
dire di no, e il suo esito deve essere LETTO da una persona.

⚠ Che cosa è cambiato con la fase 2C. Adesso ogni salvataggio mantiene la proiezione
dentro la propria transazione, quindi **`--rebuild` non è più il modo normale di
tenerla aggiornata**. Gli restano due mestieri, entrambi da proprietario:

  1. il passo di ATTIVAZIONE. Finché la proiezione non rispecchia la testa, l'API
     rifiuta i salvataggi con 503 `projection_not_current`: non si cura da sola, di
     proposito (vedi `ProjectionNotCurrentError`). `--rebuild` è il passo 3 della
     sequenza di aggiornamento documentata;
  2. il RIPRISTINO dopo un guasto: una scrittura fuori dall'API, un ripristino
     parziale da backup, una versione della mappa cambiata da un aggiornamento.

E `--verify` resta lo strumento indipendente: la verifica automatica dopo ogni
scrittura dimostra che quel salvataggio era fedele, non che lo sia ancora oggi.

⚠ Che cosa è cambiato con la fase 2D (§8.45). La proiezione non è più solo ciò che si
scrive: `GET /api/inventory` la LEGGE, e verifica il giro completo prima di servire.
Cambia il PESO di questi comandi, non il loro mestiere:

  - una proiezione non attuale adesso rende indisponibile anche la LETTURA
    dell'inventario, non solo il salvataggio. Il passo 1 qui sopra non è più
    «l'applicazione non salva», è «l'applicazione non funziona»;
  - `projection_inconsistent` è un guasto nuovo, e il suo rimedio **non è
    `--rebuild`**. Una ricostruzione lo farebbe sparire cancellando le prove di una
    corruzione di cui non si conosce ancora la causa. Prima `--verify`, che dice cosa
    non torna e dove; `--rebuild` solo dopo aver capito perché.

⚠ E questi comandi restano INDIPENDENTI dalla rotta, di proposito. La tentazione
sarebbe far diventare `--verify` una chiamata a `GET /api/inventory`, che ormai fa la
stessa verifica: sarebbe meno codice e sarebbe sbagliato. Uno strumento diagnostico
che dipende dal servizio che deve diagnosticare non si può usare nel guasto che conta
— quando il servizio non risponde, o non è ancora avviato, o gira con uno schema che
non riconosce. Qui si parla al database.

L'invariante operativa utile, che vale la pena controllare a mano dopo un
aggiornamento:

    `GET` risponde 200  ∧  `--verify` esce 0  ∧  GET.sha256 == digest della testa

Uso:
    python scripts/project.py --status     che versione rispecchia (sola lettura)
    python scripts/project.py --verify     riassembla da SQL e confronta (sola lettura)
    python scripts/project.py --rebuild    ricostruisce, e aborta se non torna

In Compose gira dal servizio che è già il proprietario dello schema:

    docker compose run --rm migrate python scripts/project.py --status

Codici di uscita, e la ragione della differenza:

    --status    sempre 0. È un rapporto, non un'asserzione: serve a guardare, anche
                (e soprattutto) quando qualcosa non va.
    --verify    0 se la proiezione è FEDELE **e** ATTUALE, 1 altrimenti.
                ⚠ In fase 2B l'attualità non contava: una proiezione vecchia era
                normale, perché nessuno la sincronizzava, e farla fallire avrebbe
                insegnato a ignorare il codice di uscita. Adesso una proiezione
                vecchia è lo stato in cui l'API rifiuta le scritture, quindi è un
                guasto. Le due cause restano riportate a parte, perché sono diverse:
                la fedeltà è un difetto del codice, l'attualità un comando mancante.
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
        # La versione della MAPPA, non dello schema: dice come i dati sono
        # distribuiti fra colonne ed `extra`. Una proiezione scritta da una mappa
        # diversa riassembla lo stesso documento e sta nelle colonne sbagliate,
        # cosa che il digest non vede.
        atteso = projection.MAPPER_VERSION
        marca = "" if state.mapper_version == atteso else f"  ⚠ attesa {atteso}"
        print(f"  mappa        versione {state.mapper_version}{marca}")
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

            # Le due domande si stampano SEMPRE entrambe, anche quando la prima
            # fallisce: sono cause diverse e chi diagnostica ha bisogno di sapere se
            # ne ha una o due. Riportarne solo la prima costringerebbe a rieseguire
            # il comando dopo ogni rimedio per scoprire la successiva.
            if result.faithful:
                print("  fedeltà      OK: le tabelle riassemblano la versione che "
                      "dichiarano di rispecchiare")
            else:
                print(f"  fedeltà      FALLITA ({result.reason})")
                _print_details(result.details)

            if result.current:
                print("  attualità    OK: rispecchia la testa, con una mappa "
                      "supportata")
            else:
                print("  attualità    FALLITA "
                      f"({result.status.currency.problem()})")
                print("               dalla fase 2C l'API rifiuta i salvataggi in "
                      "questo stato: eseguire `--rebuild`")

            return 0 if result.ok else 1

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
