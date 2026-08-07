# Esegue tools/audit-ui-test.py da uno stato pulito.
#
# Il test genera eventi facendo accessi veri, e alcuni falliti servono al filtro
# per esito. Il limitatore per IP conta i fallimenti su una finestra di 15 minuti:
# se restano tentativi di un'esecuzione precedente, l'accesso dell'amministratore
# viene bloccato e il test non parte nemmeno. Qui si riparte da zero.

param(
    [string]$Base = "https://localhost",
    [string]$Admin = "admin",
    [string]$Venv = "$env:USERPROFILE\tsmtest\venv"
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$temp = "admin-iniziale-1"
$final = "admin-definitiva-1"

Write-Host "ricreo lo stato ..."
docker compose down -v 2>$null | Out-Null
docker compose up -d --wait 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "avvio dello stack fallito"; exit 1 }

docker compose run --rm -v "${root}/fixtures:/seed:ro" `
    -e TSM_BOOTSTRAP_PASSWORD=$temp `
    migrate python scripts/bootstrap.py --seed /seed/seed.json --admin $Admin --from-legacy 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "bootstrap fallito"; exit 1 }

$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv non trovato in $Venv"; exit 1 }

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
if ($LASTEXITCODE -ne 0) { Write-Error "preparazione dell'amministratore fallita"; exit 1 }

Write-Host "esecuzione del test ..."
& $py (Join-Path $root "tools\audit-ui-test.py") --base $Base --username $Admin --password $final
exit $LASTEXITCODE
