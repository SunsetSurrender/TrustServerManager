# Esegue tools/photos-ui-test.py da uno stato pulito.
#
# Perché serve ripartire da zero:
#
#  - il test crea un sito, una sala e un rack con identificativi FISSI: da uno
#    stato già usato la creazione fallirebbe con «esiste già», e il test si
#    fermerebbe per un motivo che non c'entra con le foto;
#  - verifica che sostituire una foto NON cancelli quella precedente, e per
#    dimostrarlo servono esattamente le versioni che crea lui;
#  - verifica un conflitto di versione, che dipende dalla versione corrente.
#
# Il test è distruttivo — scrive nell'inventario e carica immagini — quindi la
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
# `-f compose.storage-dev.yaml`: su questa macchina il secondo disco non esiste, e
# compose.yaml ancora pgdata a /srv/tsm-data/postgres (§8.40).
$C = @("-f", "compose.yaml", "-f", "compose.storage-dev.yaml")

docker compose @C down -v 2>$null | Out-Null
# `--build`: senza, si proverebbe l'immagine costruita l'ultima volta, cioè codice
# vecchio. Un test verde su codice che non è quello che si sta scrivendo è peggio
# di un test rosso.
docker compose @C up -d --build --wait 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "avvio dello stack fallito"; exit 1 }

docker compose @C run --rm -v "${root}/fixtures:/seed:ro" `
    -e TSM_BOOTSTRAP_PASSWORD=$temp `
    migrate python scripts/bootstrap.py --seed /seed/seed.json --admin $Admin --from-legacy 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "bootstrap fallito"; exit 1 }

$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv non trovato in $Venv"; exit 1 }

# Amministratore con password definitiva, più un operatore: il caricamento delle
# foto è riservato agli amministratori (§8.5), e va provato che l'operatore non
# veda il comando e riceva 403 dal server.
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
& $py (Join-Path $root "tools\photos-ui-test.py") --base $Base `
    --username $Admin --password $final `
    --editor $Editor --editor-password $editorPw `
    --allow-destructive
exit $LASTEXITCODE
