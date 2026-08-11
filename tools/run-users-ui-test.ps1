# Esegue tools/users-ui-test.py partendo da uno stato pulito.
#
# Il test NON è ripetibile su uno stato già usato, e non per sbadataggine: uno dei
# casi da verificare è l'autoretrocessione dell'amministratore, che alla fine
# lascia l'utenza di prova senza privilegi. Quindi lo stato si ricrea qui.
#
# Uso:  .\tools\run-users-ui-test.ps1 [-Base https://localhost]

param(
    [string]$Base = "https://localhost",
    [string]$Admin = "admin",
    [string]$Venv = "$env:USERPROFILE\tsmtest\venv"
)

# NIENTE $ErrorActionPreference = "Stop": docker compose scrive l'avanzamento su
# stderr, e in PowerShell 5.1 questo basta a far sembrare fallito un comando
# riuscito. Si controlla $LASTEXITCODE, che è l'unico segnale attendibile.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$temp = "admin-iniziale-1"
$final = "admin-definitiva-1"

Write-Host "ricreo lo stato (down -v + bootstrap) ..."
# `-f compose.storage-dev.yaml`: su questa macchina il secondo disco non esiste,
# e compose.yaml ancora pgdata a /srv/tsm-data/postgres (§8.40). Senza
# l'override, Docker rifiuterebbe di montare il volume — che è il comportamento
# giusto in produzione e un impedimento qui.
$C = @("-f", "compose.yaml", "-f", "compose.storage-dev.yaml")

docker compose @C down -v 2>$null | Out-Null
# `--build`: senza, si proverebbe l'immagine costruita l'ultima volta, cioè
# codice vecchio. Un test verde su codice che non è quello che si sta scrivendo
# è peggio di un test rosso.
docker compose @C up -d --build --wait 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "avvio dello stack fallito"; exit 1 }

docker compose @C run --rm -v "${root}/fixtures:/seed:ro" `
    -e TSM_BOOTSTRAP_PASSWORD=$temp `
    migrate python scripts/bootstrap.py --seed /seed/seed.json --admin $Admin --from-legacy 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "bootstrap fallito"; exit 1 }

# L'utenza di bootstrap nasce con password provvisoria: la si cambia una volta,
# così il test parte da una sessione piena e non da una ristretta.
$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv non trovato in $Venv (pip install playwright)"; exit 1 }

& $py -c @"
import ssl, json, urllib.request
t = ssl.create_default_context(); t.check_hostname = False; t.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=t))
def post(path, body, cookie=None):
    h = {'Content-Type': 'application/json', 'Origin': '$Base'}
    if cookie: h['Cookie'] = cookie
    r = urllib.request.Request('$Base' + path, data=json.dumps(body).encode(),
                               headers=h, method='POST')
    resp = op.open(r, timeout=20); return resp.status, dict(resp.headers)
st, hd = post('/api/auth/login', {'username': '$Admin', 'password': '$temp'})
ck = [v for k, v in hd.items() if k.lower() == 'set-cookie'][0].split(';')[0]
st, _ = post('/api/auth/password',
             {'currentPassword': '$temp', 'newPassword': '$final'}, ck)
assert st == 204, st
"@
if ($LASTEXITCODE -ne 0) { Write-Error "preparazione dell'utenza amministrativa fallita"; exit 1 }

Write-Host "esecuzione del test ..."
& $py (Join-Path $root "tools\users-ui-test.py") --base $Base --username $Admin --password $final --allow-destructive
exit $LASTEXITCODE
