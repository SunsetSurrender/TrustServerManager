# Genera un certificato TLS AUTOFIRMATO in secrets/tls/ per lo sviluppo.
#
# In produzione qui va il certificato aziendale: stessi nomi di file
# (fullchain.pem, privkey.pem), montati dal volume dichiarato in compose.yaml.
#
# Un certificato autofirmato fa protestare il browser: è corretto che protesti.
# Serve a provare che la catena TLS funziona, non a essere fidato.
#
# Uso:  .\tools\make-dev-tls.ps1 [-CommonName localhost]

param([string]$CommonName = "localhost")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "secrets\tls"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# openssl in container: non serve averlo installato sulla macchina. Si usa
# `alpine` e si aggiunge openssl, invece di un'immagine dedicata: alpine è già
# nella cache per via degli altri servizi, e un'immagine in meno da recuperare è
# un problema in meno in rete chiusa.
$cmd = "apk add --no-cache openssl >/dev/null 2>&1 && " +
       "openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes " +
       "-keyout /out/privkey.pem -out /out/fullchain.pem " +
       "-subj '/CN=$CommonName' " +
       "-addext 'subjectAltName=DNS:$CommonName,DNS:localhost,IP:127.0.0.1' && " +
       # nginx nell'immagine gira come uid 101: la chiave deve essere leggibile
       # da LUI, non da root. È lo stesso inciampo dei secret di Compose
       # (backend/README.md): il file arriva nel container coi permessi che ha
       # sull'host, e un 0640 di root lo rende illeggibile al processo.
       "chown 101:101 /out/privkey.pem /out/fullchain.pem && " +
       "chmod 0644 /out/fullchain.pem && chmod 0640 /out/privkey.pem"

# openssl scrive l'avanzamento su stderr; in PowerShell 5.1 quello basta a far
# sembrare fallito un comando riuscito, quindi si silenzia dentro il container e
# si giudica dal codice di uscita e dai file prodotti.
docker run --rm -v "${dir}:/out" alpine:3.20 sh -c "( $cmd ) 2>/dev/null"

if ($LASTEXITCODE -ne 0) { throw "generazione del certificato fallita" }
foreach ($f in @("fullchain.pem", "privkey.pem")) {
    if (-not (Test-Path (Join-Path $dir $f))) { throw "manca $f" }
}

Write-Host "certificato autofirmato in secrets/tls/ (CN=$CommonName, 365 giorni)"
Write-Host "ATTENZIONE: solo per sviluppo. In produzione sostituire con il certificato aziendale."
