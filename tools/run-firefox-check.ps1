# Prove nel browser con FIREFOX, su uno stack locale ricreato da zero.
#
# Esegue, con Firefox (motore di Playwright):
#   - tools/browser-e2e-test.py     accesso, cookie, salvataggio, logout
#   - tools/xlsx-roundtrip-test.py  export XLSX -> import, nessun dato alterato
#   - tools/lockout-e2e-test.py     blocco/sblocco utenza, soglia, indicatore password
#
# ATTENZIONE: ricrea lo stack LOCALE (docker compose down -v): il database di
# sviluppo di questa macchina viene cancellato. Mai contro tsm-prd.
#
# Uso:  .\tools\run-firefox-check.ps1 [-Base https://localhost]

param(
    [string]$Base = "https://localhost",
    [string]$Admin = "admin",
    [string]$Venv = "$env:USERPROFILE\tsmtest\venv"
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv non trovato in $Venv (pip install playwright)"; exit 1 }

Write-Host "Firefox per Playwright (se manca lo scarica, una volta sola) ..."
& $py -m playwright install firefox
if ($LASTEXITCODE -ne 0) { Write-Error "installazione di Firefox per Playwright fallita"; exit 1 }

$temp = "admin-iniziale-1"
$final = "una frase lunga per la prova firefox"
$C = @("-f", "compose.yaml", "-f", "compose.storage-dev.yaml")

Write-Host "ricreo lo stack locale (down -v + build + bootstrap) ..."
docker compose @C down -v 2>$null | Out-Null
docker compose @C up -d --build --wait 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "avvio dello stack fallito: docker compose @C logs"; exit 1 }

docker compose @C run --rm -v "${root}/fixtures:/seed:ro" `
    -e TSM_BOOTSTRAP_PASSWORD=$temp `
    migrate python scripts/bootstrap.py --seed /seed/seed.json --admin $Admin --from-legacy 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "bootstrap fallito"; exit 1 }

# Password provvisoria -> definitiva, cosi' i test partono da una sessione piena.
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
st, _ = post('/api/auth/password', {'currentPassword': '$temp', 'newPassword': '$final'}, ck)
assert st == 204, st
"@
if ($LASTEXITCODE -ne 0) { Write-Error "preparazione dell'utenza amministrativa fallita"; exit 1 }

$esiti = @{}
foreach ($t in @("browser-e2e-test.py", "xlsx-roundtrip-test.py", "lockout-e2e-test.py")) {
    Write-Host ""
    Write-Host "==== $t (firefox) ===="
    if ($t -eq "lockout-e2e-test.py") {
        & $py (Join-Path $root "tools\$t") --base $Base --admin $Admin --password $final --browser firefox --allow-destructive
    } else {
        & $py (Join-Path $root "tools\$t") --base $Base --username $Admin --password $final --browser firefox --allow-destructive
    }
    $esiti[$t] = $LASTEXITCODE
}

Write-Host ""
Write-Host "==== riepilogo Firefox ===="
$ko = 0
foreach ($k in $esiti.Keys) {
    if ($esiti[$k] -eq 0) { Write-Host "  OK  $k" } else { Write-Host "  KO  $k"; $ko++ }
}
exit $ko
