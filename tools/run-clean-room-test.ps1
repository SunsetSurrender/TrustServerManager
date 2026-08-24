# Camera bianca: costruisce una installazione NUOVA da un database VUOTO e la prova.
#
# È la sequenza di §23 del requisito della fase 2I, e i primi sette passi stanno qui
# perché sono passi di INFRASTRUTTURA: creare i secret, applicare le migrazioni,
# eseguire il preflight della baseline, inizializzarla, catturare la provvisoria.
# Dall'accesso in poi tocca a tools/clean-room-test.py, che parla col browser.
#
# ⚠ `down -v` CANCELLA il volume dei dati. È il punto — una camera bianca non è
# bianca se contiene il database di ieri — ma è anche la ragione per cui questo
# script si esegue solo su una macchina di sviluppo. Il volume di produzione sta su
# un altro disco e su un'altra macchina, e `compose.storage-dev.yaml` (che serve qui
# perché il secondo disco non esiste) non si distribuisce.
#
# ⚠ La provvisoria NON passa da una riga di comando: si legge dallo stdout del
# comando della baseline e si consegna al test in una variabile d'ambiente. Una riga
# di comando finisce nella cronologia della shell e nell'elenco dei processi, dove
# chiunque sulla macchina la vede con `ps`.
#
# Uso:
#   .\tools\run-clean-room-test.ps1
#   .\tools\run-clean-room-test.ps1 -KeepUp     (lascia lo stack in piedi)

param(
    [string]$Base = "https://localhost",
    [string]$Admin = "admin",
    [string]$Venv = "$env:USERPROFILE\tsmtest\venv",
    [switch]$KeepUp
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# `-f compose.storage-dev.yaml`: su questa macchina il secondo disco non esiste, e
# compose.yaml ancora pgdata a /srv/tsm-data/postgres (§8.40).
$C = @("-f", "compose.yaml", "-f", "compose.storage-dev.yaml")

# ⚠ In PowerShell 5.1 lo stderr di un eseguibile nativo diventa un ErrorRecord, e con
# $ErrorActionPreference = "Stop" la prima riga ferma lo script. `docker compose
# --build` scrive l'avanzamento su stderr: senza questa funzione lo script moriva su
# « Image tsm-api:0.2.0 Building », che è un messaggio di successo.
#
# Non si usa `Quiet` perché qui il CODICE DI USCITA conta, e Quiet lo perde.
# ⚠ Il nome NON puo essere `Docker`: PowerShell risolve i comandi senza distinguere
# maiuscole, quindi `& docker` dentro una funzione chiamata `Docker` chiama la funzione
# — ricorsione infinita, e l'errore che si legge e «call depth overflow», che non
# parla di nomi.
function Invoke-Dk([string[]]$cmdArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & docker @cmdArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Lines = $out }
    } finally { $ErrorActionPreference = $prev }
}

function Quiet([scriptblock]$cmd) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $cmd 2>$null | Out-Null } catch { }
    $ErrorActionPreference = $prev
}

Write-Host "=== 1. il database di prima non esiste piu ===" -ForegroundColor Cyan
Quiet { docker compose @C down -v }

Write-Host "=== 2. secret e TLS ===" -ForegroundColor Cyan
# I secret di sviluppo: se mancano, il preflight e Compose si fermano. In produzione
# li scrive l'operatore (deploy/README.md §3.1) e NON somigliano a questi.
foreach ($f in @("postgres_password", "api_db_password", "worker_db_password")) {
    if (-not (Test-Path "secrets/$f")) {
        Write-Host "  manca secrets/${f}: lo creo per lo sviluppo"
        [IO.File]::WriteAllText("$root/secrets/$f", "dev-$f-" + [guid]::NewGuid().ToString("N"))
    }
}
if (-not (Test-Path "secrets/smtp_password")) {
    [IO.File]::WriteAllText("$root/secrets/smtp_password", "")
}
# ⚠ ENTRAMBI i file, non solo il certificato: su questa macchina `fullchain.pem`
# era presente e `privkey.pem` no — probabilmente per una ripulitura parziale — e
# nginx non parte con mezzo certificato. Un controllo su un file solo avrebbe detto
# «c'e» e lo stack sarebbe morto tre passi dopo, parlando d'altro.
$tlsOk = (Test-Path "secrets/tls/fullchain.pem") -and (Test-Path "secrets/tls/privkey.pem")
if (-not $tlsOk) {
    Write-Host "  genero un certificato di sviluppo (mancava almeno un file)"
    & "$root/tools/make-dev-tls.ps1" | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "generazione del certificato fallita"; exit 1 }
}

Write-Host "=== 3. lo stack, ricostruito ===" -ForegroundColor Cyan
# `--build`: senza, si proverebbe l'immagine dell'ultima volta, cioè codice vecchio.
# Un test verde su codice che non è quello che si sta scrivendo è peggio di un rosso.
$up = Invoke-Dk (@("compose") + $C + @("up", "-d", "--build", "--wait"))
if ($up.Code -ne 0) {
    # `--wait` fallisce se l'API non diventa "healthy". Su un database appena creato
    # l'healthcheck guarda /api/health (liveness) e non /api/ready, quindi questo
    # NON dovrebbe accadere per «inventario assente» — se accade, è un guasto vero.
    $up.Lines | ForEach-Object { Write-Host "    $_" }
    (Invoke-Dk (@("compose") + $C + @("ps"))).Lines | ForEach-Object { Write-Host "    $_" }
    Write-Error "avvio dello stack fallito"
    exit 1
}
Write-Host "    stack in piedi"

Write-Host "=== 4. preflight della baseline (sola lettura) ===" -ForegroundColor Cyan
$r = Invoke-Dk (@("compose") + $C + @("run", "--rm", "-v", "${root}/deploy/baseline:/baseline:ro",
    "migrate", "python", "scripts/production_baseline.py", "--check",
    "--baseline", "/baseline/production-baseline.json"))
$checkCode = $r.Code
$r.Lines | ForEach-Object { Write-Host "    $_" }
if ($checkCode -ne 0) {
    Write-Error "il preflight ha detto no su un database appena migrato: e un guasto"
    exit 1
}

Write-Host "=== 5. inizializzazione, e cattura della provvisoria ===" -ForegroundColor Cyan
$r = Invoke-Dk (@("compose") + $C + @("run", "--rm", "-v", "${root}/deploy/baseline:/baseline:ro",
    "migrate", "python", "scripts/production_baseline.py", "--init",
    "--allow-initialise", "--admin", $Admin,
    "--baseline", "/baseline/production-baseline.json"))
$initOut = $r.Lines
$initCode = $r.Code
# Si stampa tutto TRANNE la riga della provvisoria: questo output finisce nel
# terminale di chi lancia i test, e non c'e ragione di lasciarla scritta due volte.
$initOut | ForEach-Object {
    if ($_ -match '^password provvisoria di') { Write-Host "    password provvisoria: (catturata, non stampata)" }
    else { Write-Host "    $_" }
}
if ($initCode -ne 0) { Write-Error "inizializzazione fallita"; exit 1 }

$riga = $initOut | Where-Object { $_ -match '^password provvisoria di \S+:\s+(\S+)$' }
if (-not $riga) { Write-Error "provvisoria non trovata nell'output"; exit 1 }
$null = $riga -match '^password provvisoria di \S+:\s+(\S+)$'
$provvisoria = $Matches[1]
Write-Host "    (provvisoria di $($provvisoria.Length) caratteri catturata)"

Write-Host "=== 6. lo stato, riletto dal comando ===" -ForegroundColor Cyan
# Il secondo --check deve dire NO, e con il codice stabile: e la prova di §21 fatta
# sull'installazione vera invece che in una suite.
$r = Invoke-Dk (@("compose") + $C + @("run", "--rm", "-v", "${root}/deploy/baseline:/baseline:ro",
    "migrate", "python", "scripts/production_baseline.py", "--check",
    "--baseline", "/baseline/production-baseline.json"))
$secondo = $r.Lines
$secondoCode = $r.Code
if ($secondoCode -eq 0) {
    Write-Error "il preflight dice ancora SI su un database inizializzato"
    exit 1
}
if (-not ($secondo -match 'production_baseline_already_initialized')) {
    $secondo | ForEach-Object { Write-Host "    $_" }
    Write-Error "atteso production_baseline_already_initialized"
    exit 1
}
Write-Host "    il secondo preflight rifiuta con production_baseline_already_initialized"

Write-Host "=== 7. la proiezione, verificata dal proprietario ===" -ForegroundColor Cyan
$r = Invoke-Dk (@("compose") + $C + @("run", "--rm", "migrate", "python", "scripts/project.py",
    "--verify"))
$r.Lines | ForEach-Object { Write-Host "    $_" }
if ($r.Code -ne 0) { Write-Error "la proiezione non e fedele o non e attuale"; exit 1 }

Write-Host "=== 8-20. il browser ===" -ForegroundColor Cyan
$py = if (Test-Path "$Venv\Scripts\python.exe") { "$Venv\Scripts\python.exe" } else { "python" }
$env:TSM_CLEANROOM_PASSWORD = $provvisoria
try {
    & $py "$root/tools/clean-room-test.py" --base $Base --username $Admin `
        --allow-destructive
    $code = $LASTEXITCODE
} finally {
    # La variabile non resta nell'ambiente della shell di chi ha lanciato.
    Remove-Item Env:\TSM_CLEANROOM_PASSWORD -ErrorAction SilentlyContinue
}

if (-not $KeepUp) {
    Write-Host "=== 21. smontaggio ===" -ForegroundColor Cyan
    # L'artefatto committato non e stato toccato: si e solo montato in sola lettura.
    Quiet { docker compose @C down -v }
} else {
    Write-Host "stack lasciato in piedi (-KeepUp). La password dell'amministratore" `
        "e stata cambiata dal test."
}

exit $code
