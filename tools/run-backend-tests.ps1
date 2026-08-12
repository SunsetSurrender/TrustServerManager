# Esegue la suite del backend, compresi i test di integrazione su PostgreSQL reale.
#
# I test di integrazione non usano doppi: il comportamento che verificano
# (SELECT ... FOR UPDATE, identity bigint, atomicità del rollback) è
# comportamento del database, e un finto non lo dimostrerebbe.
#
# Senza TSM_DB_URL i test PG si saltano e resta la suite pura.
#
# `httpx` è pinnato qui e non in requirements.txt: serve SOLO al TestClient di
# Starlette, cioè ai test, e non deve finire nell'immagine di produzione. È
# pinnato lo stesso perché non pinnarlo l'ha già rotta una volta — `starlette`
# è passata alla 1.x, che pretende `httpx2`, e la suite non si è più nemmeno
# raccolta.
#
# Uso:  .\tools\run-backend-tests.ps1  [-KeepDb]

param([switch]$KeepDb)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$net = "tsm-test-net"
$db = "tsm-test-db"
# postgres:17-alpine, pinnata per digest come in compose.yaml
$pgImage = "postgres@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"

# `docker network create`/`docker rm` scrivono su stderr quando la rete esiste già
# o il container non esiste, e con $ErrorActionPreference = "Stop" PowerShell 5.1
# trasforma lo stderr di un eseguibile nativo in un errore che ferma lo script. Non
# è un guasto: sono le due condizioni normali di un avvio pulito. `Quiet` le
# esegue con la gestione errori allentata, senza allentarla per tutto il resto.
function Quiet([scriptblock]$cmd) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $cmd 2>$null | Out-Null } catch { }
    $ErrorActionPreference = $prev
}

Quiet { docker network create $net }

if (-not (docker ps -q -f "name=^$db$")) {
    Quiet { docker rm -f $db }
    Write-Host "avvio $db ..."
    docker run -d --name $db --network $net `
        -e POSTGRES_USER=tsm -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=tsm_test `
        $pgImage | Out-Null

    $deadline = (Get-Date).AddSeconds(90)
    do {
        docker exec $db pg_isready -U tsm -d tsm_test -q 2>$null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) { throw "il database di test non è diventato pronto" }
}

# Gli argomenti in più vanno a pytest (per esempio un singolo file, o `-k`). Si
# compongono nella stringa passata a `sh -c`: `@args` dentro una stringa
# PowerShell NON si espande — resta il testo letterale `@args`, che pytest riceve
# come nome di file e rifiuta con un errore d'uso. Difetto reale del runner,
# invisibile finché lo si eseguiva senza argomenti.
$extra = ""
if ($args.Count -gt 0) {
    $extra = " " + (($args | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join " ")
}

Write-Host "esecuzione della suite ...$extra"
docker run --rm --network $net -v "${root}:/w" -w /w/backend `
    -e TSM_DB_URL="postgresql+psycopg://tsm:testpw@${db}:5432/tsm_test" `
    python:3.13-slim `
    sh -c "pip install --quiet -r requirements.txt pytest==9.1.1 httpx==0.28.1 2>/dev/null && python -m pytest$extra"
$code = $LASTEXITCODE

if (-not $KeepDb) {
    Quiet { docker rm -f $db }
    Quiet { docker network rm $net }
}

exit $code
