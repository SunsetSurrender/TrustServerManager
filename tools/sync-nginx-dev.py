#!/usr/bin/env python3
"""Rigenera web/nginx.dev.conf da web/nginx.conf.

Le due configurazioni devono differire in UNA cosa sola: la produzione termina
TLS su 8443 (mappata su host 443) e reindirizza 8080 (host 80), lo sviluppo serve
in chiaro su 8080 senza HSTS. Tutto il resto — allowlist dei file statici, proxy
dell'API, limiti di dimensione, intestazioni di sicurezza — deve restare identico,
altrimenti si prova qualcosa di diverso da quello che si distribuisce.

Tenerle allineate a mano non funziona: la modifica si fa su una e si dimentica
l'altra, e il divario si scopre in produzione.

Il pezzo da sostituire è delimitato da marcatori dentro nginx.conf, non da un
blocco di testo duplicato qui: una copia letterale di venti righe divergeva al
primo commento modificato, ed è già successo.

Uso:  python tools/sync-nginx-dev.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "web" / "nginx.conf"
DEV = ROOT / "web" / "nginx.dev.conf"

BEGIN = "    # >>> SYNC:LISTENERS-BEGIN"
END = "    # >>> SYNC:LISTENERS-END"

DEV_LISTENER = """    # ------------------------------------------------------------------
    # SOLO SVILUPPO: HTTP in chiaro, nessun TLS, NESSUN HSTS.
    #
    # L'assenza di HSTS qui non è una dimenticanza: lo sviluppo usa porte non
    # standard (8080), e un HSTS visto una volta su un host resterebbe memorizzato
    # dal browser, che poi trasformerebbe http://host:8080 in https://host:8080 —
    # dove non c'è TLS. Vedi §8.31.
    #
    # Usato da compose.dev.yaml. Non finisce nell'immagine di produzione.
    # ------------------------------------------------------------------
    server {
        listen 8080;
        server_name _;

        add_header X-Content-Type-Options nosniff always;"""

HEADER = """# GENERATO da tools/sync-nginx-dev.py a partire da web/nginx.conf.
# Differenza unica: nessun TLS e nessun HSTS, un solo server in chiaro su 8080.
# Rigenerare dopo ogni modifica a nginx.conf, così le due configurazioni non
# divergono su tutto il resto (allowlist, proxy, limiti, intestazioni).
"""


def render() -> str:
    prod = PROD.read_text(encoding="utf-8")
    try:
        i = prod.index(BEGIN)
        j = prod.index(END) + len(END)
    except ValueError:
        sys.exit(f"marcatori {BEGIN!r} / {END!r} non trovati in web/nginx.conf: "
                 "ripristinarli, servono a tenere allineata la configurazione di "
                 "sviluppo.")
    return HEADER + prod[:i] + DEV_LISTENER + prod[j:]


def main() -> int:
    want = render()
    if "--check" in sys.argv:
        have = DEV.read_text(encoding="utf-8") if DEV.exists() else ""
        if have != want:
            print("web/nginx.dev.conf è disallineato: eseguire "
                  "`python tools/sync-nginx-dev.py`")
            return 1
        print("web/nginx.dev.conf allineato")
        return 0
    DEV.write_text(want, encoding="utf-8")
    print(f"scritto {DEV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
