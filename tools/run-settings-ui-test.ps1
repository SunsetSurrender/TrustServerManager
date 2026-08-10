# Esegue tools/settings-ui-test.py da uno stato pulito.
#
# Perché serve ripartire da zero:
#
#  - il test verifica che «la revisione non sale per un salvataggio a vuoto», e
#    la revisione è cumulativa: da uno stato usato in precedenza i numeri
#    sarebbero diversi ma soprattutto imprevedibili;
#  - verifica anche il conflitto fra due amministratori, che dipende dall'ETag
#    corrente;
#  - l'invio di prova è LIMITATO (3 per utenza all'ora, §8.38): due esecuzioni
#    ravvicinate esaurirebbero il limite e il test riceverebbe 429 dove si
#    aspetta un esito.
#
# Il test è distruttivo — riscrive le impostazioni di notifica — quindi la
# guardia (§8.37) pretende `--allow-destructive`, che passiamo qui: questo script
# ricrea lo stato da sé, e l'obiettivo è per costruzione locale.

param(
    [string]$Base = "https://localhost",
    [string]$Admin = "admin",
    [string]$Editor = "operatore",
    [string]$Venv = "$env:USERPROFILE\tsmtest\venv"
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$temp = "admin-iniziale-1"
$final = "admin-definitiva-1"
$editorPw = "operatore-definitiva-1"

Write-Host "ricreo lo stato ..."
docker compose down -v 2>$null | Out-Null
# `--build`: senza, si proverebbe l'immagine costruita l'ultima volta, cioè
# codice vecchio. Un test verde su codice che non è quello che si sta scrivendo
# è peggio di un test rosso.
docker compose up -d --build --wait 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "avvio dello stack fallito"; exit 1 }

docker compose run --rm -v "${root}/fixtures:/seed:ro" `
    -e TSM_BOOTSTRAP_PASSWORD=$temp `
    migrate python scripts/bootstrap.py --seed /seed/seed.json --admin $Admin --from-legacy 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "bootstrap fallito"; exit 1 }

$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv non trovato in $Venv"; exit 1 }

# Amministratore con password definitiva, più un operatore per i controlli di
# autorizzazione (la sezione non deve comparire, e il server deve dire 403).
& $py -c @"
import ssl, json, urllib.request
t = ssl.create_default_context(); t.check_hostname = False; t.verify_mode = ssl.CERT_NONE
op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=t))

def call(method, path, body=None, cookie=None):
    h = {'Content-Type': 'application/json', 'Origin': '$Base'}
    if cookie: h['Cookie'] = cookie
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request('$Base' + path, data=data, headers=h, method=method)
    resp = op.open(r, timeout=20)
    return resp.status, dict(resp.headers), (resp.read() or b'null')

st, hd, _ = call('POST', '/api/auth/login', {'username': '$Admin', 'password': '$temp'})
ck = [v for k, v in hd.items() if k.lower() == 'set-cookie'][0].split(';')[0]
st, _, _ = call('POST', '/api/auth/password',
                {'currentPassword': '$temp', 'newPassword': '$final'}, ck)
assert st == 204, st

# La password è cambiata: le sessioni sono state revocate, serve un nuovo accesso.
st, hd, _ = call('POST', '/api/auth/login', {'username': '$Admin', 'password': '$final'})
ck = [v for k, v in hd.items() if k.lower() == 'set-cookie'][0].split(';')[0]

st, _, body = call('POST', '/api/users',
                   {'username': '$Editor', 'role': 'edit'}, ck)
temp_editor = json.loads(body)['temporaryPassword']
st, hd, _ = call('POST', '/api/auth/login',
                 {'username': '$Editor', 'password': temp_editor})
ck2 = [v for k, v in hd.items() if k.lower() == 'set-cookie'][0].split(';')[0]
st, _, _ = call('POST', '/api/auth/password',
                {'currentPassword': temp_editor, 'newPassword': '$editorPw'}, ck2)
assert st == 204, st
"@
if ($LASTEXITCODE -ne 0) { Write-Error "preparazione delle utenze fallita"; exit 1 }

Write-Host "esecuzione del test ..."
& $py (Join-Path $root "tools\settings-ui-test.py") --base $Base `
    --username $Admin --password $final `
    --editor $Editor --editor-password $editorPw `
    --allow-destructive
exit $LASTEXITCODE
