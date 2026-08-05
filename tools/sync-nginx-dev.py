#!/usr/bin/env python3
"""Rigenera web/nginx.dev.conf da web/nginx.conf.

Le due configurazioni devono differire in UNA cosa sola: la produzione termina
TLS su 8443 e reindirizza 8080, lo sviluppo serve in chiaro su 8080. Tutto il
resto — allowlist dei file statici, proxy dell'API, limiti di dimensione,
intestazioni di sicurezza — deve restare identico, altrimenti si prova qualcosa
di diverso da quello che si distribuisce.

Tenerle allineate a mano non funziona: la modifica si fa su una e si dimentica
l'altra, e il divario si scopre in produzione.

Uso:  python tools/sync-nginx-dev.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "web" / "nginx.conf"
DEV = ROOT / "web" / "nginx.dev.conf"

PROD_BLOCK = """    # ------------------------------------------------------------------
    # HTTP: solo reindirizzamento. Il cookie di sessione è `Secure`, quindi su
    # HTTP non funzionerebbe comunque nulla; meglio dirlo con un 301 che lasciare
    # credere che il servizio risponda in chiaro.
    # ------------------------------------------------------------------
    server {
        listen 8080;
        server_name _;
        return 301 https://$host$request_uri;
    }

    # ------------------------------------------------------------------
    # HTTPS
    # ------------------------------------------------------------------
    server {
        listen 8443 ssl;
        http2 on;
        server_name _;

        # Certificato e chiave montati dall'infrastruttura. Non sono `secrets:` di
        # Compose perché nginx li deve leggere all'avvio come file normali.
        ssl_certificate     /etc/nginx/tls/fullchain.pem;
        ssl_certificate_key /etc/nginx/tls/privkey.pem;

        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers off;
        ssl_session_cache   shared:SSL:10m;
        ssl_session_timeout 1d;
        ssl_session_tickets off;

        # HSTS: dopo la prima visita il browser non tenta più HTTP. Va acceso solo
        # quando il certificato è quello vero, altrimenti si blocca l'accesso a chi
        # ha ancora un certificato di prova.
        add_header Strict-Transport-Security "max-age=31536000" always;

        add_header X-Content-Type-Options nosniff always;"""

DEV_BLOCK = """    # ------------------------------------------------------------------
    # SOLO SVILUPPO: HTTP in chiaro, nessun TLS, nessun HSTS.
    # Usato da compose.dev.yaml. Non finisce nell'immagine di produzione.
    # ------------------------------------------------------------------
    server {
        listen 8080;
        server_name _;

        add_header X-Content-Type-Options nosniff always;"""

HEADER = """# GENERATO da tools/sync-nginx-dev.py a partire da web/nginx.conf.
# Differenza unica: nessun TLS e nessun HSTS, un solo server in chiaro su 8080.
# Rigenerare dopo ogni modifica a nginx.conf, così le due configurazioni non
# divergono su tutto il resto (allowlist, proxy, limiti).
"""


def render() -> str:
    prod = PROD.read_text(encoding="utf-8")
    if PROD_BLOCK not in prod:
        sys.exit("il blocco TLS atteso non è in web/nginx.conf: aggiornare "
                 "tools/sync-nginx-dev.py insieme alla configurazione.")
    return HEADER + prod.replace(PROD_BLOCK, DEV_BLOCK)


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
