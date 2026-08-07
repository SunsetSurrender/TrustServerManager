"""Guardia per i test di browser che MODIFICANO lo stato.

Alcuni test end-to-end cambiano davvero i dati: creano utenze, cambiano la
password dell'amministratore, retrocedono ruoli, scrivono nell'inventario. Sono
utili solo su un ambiente usa-e-getta, e disastrosi altrove.

Finora l'unica protezione era la buona memoria di chi lancia il comando. Questa
guardia la sostituisce con due condizioni esplicite:

  1. il consenso va dichiarato ogni volta (`--allow-destructive`);
  2. l'obiettivo deve essere locale, salvo dichiarare anche `--force-remote`.

Non è un ostacolo per chi sa cosa sta facendo — sono due parole sulla riga di
comando — ma rende impossibile lanciarlo *per sbaglio* contro qualcosa che
somiglia alla produzione.
"""
from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--allow-destructive", action="store_true",
                    help="obbligatorio: conferma che questo ambiente è sacrificabile")
    ap.add_argument("--force-remote", action="store_true",
                    help="consente un host non locale (da usare con cognizione)")


def enforce(args, base: str) -> None:
    """Esce con un messaggio chiaro se le condizioni non sono soddisfatte."""
    host = (urlparse(base).hostname or "").lower()

    if not getattr(args, "allow_destructive", False):
        sys.exit(
            "\nRIFIUTATO: questo test MODIFICA lo stato (utenze, password, "
            "inventario).\n"
            "Aggiungere --allow-destructive per confermare che l'ambiente è "
            "sacrificabile.\n"
            f"Obiettivo: {base}\n"
        )

    if host not in LOCAL_HOSTS and not getattr(args, "force_remote", False):
        sys.exit(
            f"\nRIFIUTATO: l'obiettivo {host!r} non è locale.\n"
            "Un test distruttivo contro un host remoto va dichiarato "
            "esplicitamente con --force-remote.\n"
        )
