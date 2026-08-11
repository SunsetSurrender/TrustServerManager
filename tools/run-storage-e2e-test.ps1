# Esegue tools/storage-e2e-test.sh dentro l'host del demone Docker.
#
# Perché serve un giro così: il test deve montare un filesystem su
# /srv/tsm-data e poi far scrivere PostgreSQL là attraverso un bind mount. Il
# bind di un container si risolve sull'host del DEMONE, non su Windows e non
# dentro un container qualsiasi: /srv/tsm-data deve esistere ed essere montato
# proprio là. Con Docker Desktop quell'host è la VM Linux, e ci si entra con
# nsenter nei namespace del PID 1.
#
# Su tsm-prd-01 tutto questo non serve: il demone gira sulla macchina, e il test
# si lancia direttamente.
#
#     sudo bash tools/storage-e2e-test.sh --repo /opt/tsm
#
# Il test è distruttivo per lo stack di sviluppo: usa un progetto Compose
# separato (`tsmstorage`) ma ferma quello locale per non contendersi le porte.

param(
    [switch]$KeepLocalStack
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$cliImage = "docker:28-cli"

Write-Host "verifico l'immagine del client Docker ..."
docker image inspect $cliImage *> $null
if ($LASTEXITCODE -ne 0) {
    docker pull -q $cliImage
    if ($LASTEXITCODE -ne 0) { Write-Error "non ho potuto ottenere $cliImage"; exit 1 }
}

if (-not $KeepLocalStack) {
    Write-Host "fermo lo stack di sviluppo (porte 80/443) ..."
    docker compose -f compose.yaml -f compose.storage-dev.yaml down *> $null
}

# 1. Il client Docker (e il plugin compose) dentro la VM: là non c'è.
Write-Host "installo il client Docker nella VM ..."
docker run --rm --privileged -v /:/vmroot $cliImage sh -c @'
mkdir -p /vmroot/usr/local/bin /vmroot/usr/local/libexec/docker/cli-plugins
cp /usr/local/bin/docker /vmroot/usr/local/bin/docker
cp /usr/local/libexec/docker/cli-plugins/docker-compose /vmroot/usr/local/libexec/docker/cli-plugins/docker-compose
chmod +x /vmroot/usr/local/bin/docker /vmroot/usr/local/libexec/docker/cli-plugins/docker-compose
'@
if ($LASTEXITCODE -ne 0) { Write-Error "installazione del client nella VM fallita"; exit 1 }

# 2. Percorso del repository come lo vede la VM.
$drive = $root.Substring(0,1).ToLower()
$rest  = $root.Substring(2).Replace('\','/')
$repoInVm = "/run/desktop/mnt/host/$drive$rest"
Write-Host "repository nella VM: $repoInVm"

# 3. Esecuzione nei namespace del PID 1.
#
# DOCKER_HOST va imposto: l'immagine docker:*-cli lo preimposta a
# `tcp://docker:2375` per il modello dind, e nsenter erediterebbe quel valore.
# Il client copiato nella VM cercherebbe un host chiamato «docker» che non
# esiste, e ogni comando fallirebbe con «lookup docker: no such host» — un
# errore che nel mezzo di un test di archiviazione sembra un difetto del test.
Write-Host "esecuzione del test ...`n"
docker run --rm --privileged --pid=host -i `
    -e DOCKER_HOST=unix:///var/run/docker.sock $cliImage `
    nsenter -t 1 -m -u -i -n -- `
    env DOCKER_HOST=unix:///var/run/docker.sock `
    bash "$repoInVm/tools/storage-e2e-test.sh" --repo "$repoInVm"
$code = $LASTEXITCODE

Write-Host "`nrimuovo il client Docker dalla VM ..."
docker run --rm --privileged -v /:/vmroot $cliImage sh -c @'
rm -f /vmroot/usr/local/bin/docker /vmroot/usr/local/libexec/docker/cli-plugins/docker-compose
'@ *> $null

exit $code
