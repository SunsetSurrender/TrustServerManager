#!/bin/bash
# =====================================================================
# Controlli Nagios (NRPE) per Trust Server Manager su tsm-prd.
#
#   check_tsm.sh <controllo>
#
#   service     docker.service e tsm.service attivi
#   containers  db, api, web, worker: in esecuzione e «healthy», riavvii
#   worker      battito del worker delle notifiche e proiezione utilizzabile
#   runs        nessun giro delle notifiche aperto da oltre un'ora
#   ready       https://127.0.0.1/api/ready risponde «ready»
#   storage     /srv/tsm-data montato, scrivibile, occupazione
#   cert        scadenza del certificato TLS servito da nginx
#   images      i container girano sulle immagini del pin del rilascio
#
# Uscita Nagios: 0 OK · 1 WARNING · 2 CRITICAL · 3 UNKNOWN. Una riga, con
# perfdata dove ha senso.
#
# Gira come root tramite sudo (vedi sudoers-tsm): parlare con Docker
# equivale a essere root, quindi l'utente nrpe NON va nel gruppo docker.
# Il sudo è ristretto a questo script e a questi argomenti, e lo script
# non accetta altro.
#
# Installazione: ops/README.md §1.
# =====================================================================
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C

DIR=/opt/tsm
PROGETTO=tsm
DATI=/srv/tsm-data
SERVIZI="db api web worker"
DISCO_WARN=75          # deploy/README.md §6 (soglie di spazio)
DISCO_CRIT=90
CERT_WARN_GIORNI=30
CERT_CRIT_GIORNI=7

ok()   { printf 'OK - %s\n' "$*"; exit 0; }
warn() { printf 'WARNING - %s\n' "$*"; exit 1; }
crit() { printf 'CRITICAL - %s\n' "$*"; exit 2; }
unk()  { printf 'UNKNOWN - %s\n' "$*"; exit 3; }

[ "$(id -u)" = 0 ] || unk "deve girare come root (sudo): Docker non è accessibile altrimenti"
ctr() { printf '%s-%s-1' "$PROGETTO" "$1"; }

case "${1:-}" in
service)
    for u in docker.service tsm.service; do
        s="$(systemctl is-active "$u" 2>/dev/null)"
        [ "$s" = active ] || crit "$u: $s"
    done
    ok "docker.service e tsm.service attivi"
    ;;

containers)
    problemi=(); avvisi=(); perf=""
    for s in $SERVIZI; do
        info="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' "$(ctr "$s")" 2>/dev/null)" \
            || { problemi+=("$s assente"); continue; }
        read -r stato salute riavvii <<<"$info"
        perf="$perf ${s}_restarts=${riavvii}"
        if [ "$stato" != running ]; then problemi+=("$s $stato")
        elif [ "$salute" = unhealthy ]; then problemi+=("$s unhealthy")
        elif [ "$salute" = starting ]; then avvisi+=("$s starting")
        fi
    done
    [ ${#problemi[@]} -eq 0 ] || crit "${problemi[*]} |$perf"
    [ ${#avvisi[@]} -eq 0 ]   || warn "${avvisi[*]} |$perf"
    ok "db, api, web, worker running e healthy |$perf"
    ;;

worker)
    out="$(timeout 30 docker exec "$(ctr worker)" python scripts/worker_health.py --json 2>&1)"; rc=$?
    stato="$(printf '%s' "$out" | sed -n 's/.*"state": *"\([^"]*\)".*/\1/p')"
    eta="$(printf '%s' "$out" | sed -n 's/.*"ageSeconds": *\([0-9.]*\).*/\1/p')"
    det="$(printf '%s' "$out" | sed -n 's/.*"detail": *"\([^"]*\)".*/\1/p')"
    [ "$rc" = 124 ] && crit "worker_health non ha risposto in 30 s"
    # Il battito può essere sano mentre NESSUN avviso parte (README §3.10):
    # la proiezione non rispecchia la testa. È un guasto, e va detto.
    case "$det" in *projection_not_current*|*projection_inconsistent*)
        crit "worker gira ma la proiezione non è utilizzabile, nessun avviso parte: $det |age=${eta:-0}s" ;;
    esac
    [ "$rc" = 0 ] || crit "worker non sano: stato=${stato:-?} età=${eta:-?}s ${det} |age=${eta:-0}s"
    ok "worker stato=$stato età=${eta}s |age=${eta}s"
    ;;

runs)
    n="$(timeout 30 docker exec "$(ctr db)" psql -U tsm -d tsm -tAc \
        "SELECT count(*) FROM scheduler_runs WHERE finished_at IS NULL AND started_at < now() - interval '1 hour'" 2>&1)" \
        || unk "psql non eseguibile: $n"
    [[ "$n" =~ ^[0-9]+$ ]] || unk "risposta inattesa: $n"
    [ "$n" = 0 ] || warn "$n giri delle notifiche aperti da oltre un'ora |open_runs=$n"
    ok "nessun giro aperto |open_runs=0"
    ;;

ready)
    # Da dentro la VM, attraverso nginx come un client vero. Il controllo
    # dal server Nagios (check_http) resta il riferimento: prova anche la rete.
    body="$(curl -sk --max-time 10 https://127.0.0.1/api/ready 2>&1)"; rc=$?
    [ "$rc" = 0 ] || crit "https://127.0.0.1/api/ready non risponde (curl $rc)"
    case "$body" in *'"status":"ready"'*|*'"status": "ready"'*) ok "api ready" ;; esac
    crit "api non pronta: ${body:0:200}"
    ;;

storage)
    findmnt -M "$DATI" >/dev/null 2>&1 || crit "$DATI NON è montato: PostgreSQL scriverebbe sul disco di sistema"
    findmnt -M "$DATI" -no OPTIONS | tr ',' '\n' | grep -qx rw || crit "$DATI montato in sola lettura"
    pc="$(df --output=pcent "$DATI" | tail -1 | tr -dc 0-9)"
    lib="$(df -BG --output=avail "$DATI" | tail -1 | tr -dc 0-9)"
    perf="|data_used=${pc}%;${DISCO_WARN};${DISCO_CRIT};0;100 data_free=${lib}GB"
    [ "$pc" -lt "$DISCO_CRIT" ] || crit "$DATI al ${pc}% (liberi ${lib} GB) $perf"
    [ "$pc" -lt "$DISCO_WARN" ] || warn "$DATI al ${pc}% (liberi ${lib} GB) $perf"
    ok "$DATI montato rw, ${pc}% usato, ${lib} GB liberi $perf"
    ;;

cert)
    f="$DIR/secrets/tls/fullchain.pem"
    [ -r "$f" ] || crit "$f non leggibile"
    fine="$(openssl x509 -in "$f" -noout -enddate | cut -d= -f2)"
    giorni=$(( ( $(date -d "$fine" +%s) - $(date +%s) ) / 86400 ))
    perf="|days_left=${giorni};${CERT_WARN_GIORNI};${CERT_CRIT_GIORNI}"
    [ "$giorni" -gt "$CERT_CRIT_GIORNI" ] || crit "certificato TLS scade tra $giorni giorni ($fine) — deploy/make-internal-tls.sh --force $perf"
    [ "$giorni" -gt "$CERT_WARN_GIORNI" ] || warn "certificato TLS scade tra $giorni giorni ($fine) $perf"
    ok "certificato TLS valido ancora $giorni giorni $perf"
    ;;

images)
    pin="$DIR/deploy/release.pin"
    [ -r "$pin" ] || unk "$pin assente"
    atteso() { sed -n "s/^$1=\(sha256:[0-9a-f]\{64\}\)$/\1/p" "$pin" | tr -d '\r'; }
    api="$(atteso ID_API)"; web="$(atteso ID_WEB)"
    [ -n "$api" ] && [ -n "$web" ] || unk "ID_API/ID_WEB non leggibili da $pin"
    diversi=()
    for s in api worker; do
        id="$(docker inspect -f '{{.Image}}' "$(ctr "$s")" 2>/dev/null)"
        [ "$id" = "$api" ] || diversi+=("$s")
    done
    id="$(docker inspect -f '{{.Image}}' "$(ctr web)" 2>/dev/null)"
    [ "$id" = "$web" ] || diversi+=("web")
    [ ${#diversi[@]} -eq 0 ] || warn "non girano sulle immagini del pin: ${diversi[*]} (aggiornamento a metà?)"
    ok "api, worker, web sulle immagini del pin $(sed -n 's/^COMMIT_QUALIFICATO=\(.\{12\}\).*/\1/p' "$pin")"
    ;;

*)
    unk "uso: $0 service|containers|worker|runs|ready|storage|cert|images"
    ;;
esac
