# Esegue la suite del backend, compresi i test di integrazione su PostgreSQL reale.
#
# I test di integrazione non usano doppi: il comportamento che verificano
# (SELECT ... FOR UPDATE, identity bigint, atomicità del rollback) è
# comportamento del database, e un finto non lo dimostrerebbe.
#
# Senza TSM_DB_URL i test PG si saltano e resta la suite pura.
#
# Uso:  .\tools\run-backend-tests.ps1  [-KeepDb]

param([switch]$KeepDb)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$net = "tsm-test-net"
$db = "tsm-test-db"
# postgres:17-alpine, pinnata per digest come in compose.yaml
$pgImage = "postgres@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"

docker network create $net 2>$null | Out-Null

if (-not (docker ps -q -f "name=^$db$")) {
    docker rm -f $db 2>$null | Out-Null
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

Write-Host "esecuzione della suite ..."
docker run --rm --network $net -v "${root}:/w" -w /w/backend `
    -e TSM_DB_URL="postgresql+psycopg://tsm:testpw@${db}:5432/tsm_test" `
    python:3.13-slim `
    sh -c "pip install --quiet -r requirements.txt pytest==9.1.1 2>/dev/null && python -m pytest @args"
$code = $LASTEXITCODE

if (-not $KeepDb) {
    docker rm -f $db 2>$null | Out-Null
    docker network rm $net 2>$null | Out-Null
}

exit $code
