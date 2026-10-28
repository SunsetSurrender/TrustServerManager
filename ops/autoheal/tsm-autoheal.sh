#!/bin/bash
# =====================================================================
# tsm-autoheal — ripristino automatico di Trust Server Manager su tsm-prd.
#
# Lato SISTEMISTICO, non applicativo: non viaggia nel pacchetto di rilascio
# e un aggiornamento dell'applicazione non lo tocca. Installazione: ops/README.md §3.
#
# Che cosa copre (e che cosa Docker da solo NON copre):
#
#   docker.service giù          systemd lo riavvia (Restart=always), ma dopo 3
#                               fallimenti in 1 minuto si arrende: qui si
#                               azzera il contatore e si riprova.
#   tsm.service non attivo      succede quando docker.service si ferma
#                               (Requires=): i container ripartono da soli per
#                               la loro restart policy, l'unità no. Qui si
#                               riavvia, preflight compreso.
#   container fermo/assente     `docker compose up -d <servizio>`
#   container «unhealthy»       Docker lo segnala ma NON lo riavvia:
#                               `restart: unless-stopped` scatta solo se il
#                               processo esce. Qui `docker compose restart`.
#
# Freno: al massimo MAX_PER_ORA interventi per oggetto all'ora. Oltre, si
# smette e si scrive un errore nel journal: un guasto che si ripresenta non
# si cura riavviando in loop, si guarda (Nagios lo vede comunque).
#
# Manutenzione: prima di fermare TSM di proposito
#     systemctl stop tsm-autoheal.timer      (e poi start a lavoro finito)
# oppure   touch /var/lib/tsm-autoheal/pausa
#
# Log:  journalctl -t tsm-autoheal
# =====================================================================
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C

DIR=/opt/tsm
SERVIZI="db api web worker"
STATO=/var/lib/tsm-autoheal
MAX_PER_ORA=3

log() { logger -t tsm-autoheal -p "daemon.$1" -- "$2"; printf '%s: %s\n' "$1" "$2"; }

install -d -m 0700 "$STATO"
[ -e "$STATO/pausa" ] && exit 0
# Un servizio disabilitato è una scelta, non un guasto.
systemctl is-enabled --quiet tsm.service || exit 0

# Restituisce 0 se c'è ancora margine per intervenire su "$1", e lo registra.
budget() {
    local f="$STATO/$1" ora; ora=$(date +%s)
    touch "$f"
    awk -v lim=$((ora - 3600)) '$1 > lim' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    if [ "$(wc -l < "$f")" -ge "$MAX_PER_ORA" ]; then
        log err "$1: già $MAX_PER_ORA interventi nell'ultima ora, mi fermo — serve un controllo manuale"
        return 1
    fi
    echo "$ora" >> "$f"
}

# ---- 1. Docker -------------------------------------------------------
if ! systemctl is-active --quiet docker.service; then
    budget docker || exit 1
    log warning "docker.service non attivo: azzero il limite di systemd e lo avvio"
    systemctl reset-failed docker.service docker.socket 2>/dev/null
    if ! systemctl start docker.service; then
        log err "docker.service non parte: journalctl -u docker"
        exit 1
    fi
    sleep 20      # lascia ripartire i container con la loro restart policy
fi

# ---- 2. tsm.service --------------------------------------------------
if ! systemctl is-active --quiet tsm.service; then
    budget tsm.service || exit 1
    log warning "tsm.service non attivo: lo avvio (preflight compreso)"
    systemctl reset-failed tsm.service 2>/dev/null
    if ! systemctl start tsm.service; then
        log err "tsm.service non parte, probabilmente rifiutato dal preflight: systemctl status tsm"
        exit 1
    fi
    exit 0        # appena avviato: i container si guardano al prossimo giro
fi

# ---- 3. container ----------------------------------------------------
cd "$DIR" || { log err "$DIR mancante"; exit 1; }
for s in $SERVIZI; do
    cid=$(docker compose ps -a -q "$s" 2>/dev/null | head -1)
    if [ -z "$cid" ]; then
        stato="assente"
    else
        stato=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || echo "assente")
    fi
    case "$stato" in
        "running healthy"|"running starting"|"running") ;;
        "running unhealthy")
            budget "$s" || continue
            log warning "$s unhealthy: docker compose restart $s"
            docker compose restart "$s" >/dev/null 2>&1 || log err "$s: restart non riuscito"
            ;;
        *)
            budget "$s" || continue
            log warning "$s in stato «$stato»: docker compose up -d $s"
            docker compose up -d "$s" >/dev/null 2>&1 || log err "$s: avvio non riuscito"
            ;;
    esac
done
exit 0
