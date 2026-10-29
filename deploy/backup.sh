#!/bin/sh
# =====================================================================
# Dump notturno del database di TSM. Gira nel servizio `backup` di
# compose.yaml (immagine postgres, la stessa versione del server).
#
# Ogni notte all'ora TSM_DUMP_HOUR:TSM_DUMP_MINUTE (predefinito 02:15, fuso
# TSM_DUMP_TZ) scrive /backups/tsm-AAAAMMGG-HHMMSS.dump, cioè
# /srv/tsm-data/backups sulla VM:
#
#   - formato custom di pg_dump (-Fc), compresso, ripristinabile con pg_restore;
#   - scritto prima come .part e rinominato solo se pg_restore lo rilegge per
#     intero: un file con il nome definitivo è un file completo;
#   - si tengono gli ultimi TSM_DUMP_KEEP (predefinito 14), gli altri si cancellano;
#   - esito, dimensione e spazio libero del disco dati vanno in `db_dumps`, e il
#     giro in `maintenance_runs` (job db_dump), che Nagios e la pagina «Stato
#     del sistema» leggono.
#
# Perché un dump oltre a Veeam: Veeam fotografa la VM con PostgreSQL acceso, e
# un ripristino da lì è un ripristino «dopo un'interruzione di corrente». Il
# dump è una copia sicuramente coerente, e ripristinabile anche su un'altra
# macchina.
#
# A mano:   docker compose exec backup sh /opt/tsm-backup/backup.sh --now
# =====================================================================
set -u
export HOME=/tmp
export PGHOST=db PGUSER=tsm PGDATABASE=tsm
export TZ="${TSM_DUMP_TZ:-Europe/Rome}"
PGPASSWORD="$(cat /run/secrets/postgres_password)" || { echo "secret illeggibile" >&2; exit 1; }
export PGPASSWORD
HOUR="${TSM_DUMP_HOUR:-2}"
MINUTE="${TSM_DUMP_MINUTE:-15}"
KEEP="${TSM_DUMP_KEEP:-14}"
DIR=/backups

sql() { psql -X -q -t -A -v ON_ERROR_STOP=1 -c "$1"; }
log() { echo "$(date '+%F %T') backup: $*"; }

dump() {
    oggi="$(date +%F)"
    sql "INSERT INTO maintenance_runs (job, run_date, timezone)
         VALUES ('db_dump', '$oggi', '$TZ')
         ON CONFLICT (job, run_date) DO UPDATE SET started_at = now(), finished_at = NULL" >/dev/null \
        || { log "database non raggiungibile"; return 1; }
    id="$(sql "INSERT INTO db_dumps DEFAULT VALUES RETURNING id")"
    f="tsm-$(date +%Y%m%d-%H%M%S).dump"
    if pg_dump -Fc -Z6 -f "$DIR/.$f.part" 2>/tmp/err \
            && pg_restore -l "$DIR/.$f.part" >/dev/null 2>>/tmp/err; then
        mv "$DIR/.$f.part" "$DIR/$f" && chmod 0600 "$DIR/$f"
        ok=true; msg=""; size="$(stat -c %s "$DIR/$f")"
    else
        ok=false; msg="$(head -c 400 /tmp/err | tr "'\n" '  ')"; size=NULL
        rm -f "$DIR/.$f.part"; f=""
    fi
    # Conservazione: i più recenti KEEP, mai il file appena scritto.
    ls -1t "$DIR"/tsm-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r vecchio; do
        rm -f "$vecchio"
    done
    kept="$(ls -1 "$DIR"/tsm-*.dump 2>/dev/null | wc -l)"
    set -- $(df -P -k "$DIR" | awk 'NR==2 {print $2 * 1024, $4 * 1024}')
    tot="${1:-NULL}"; free="${2:-NULL}"
    sql "UPDATE db_dumps SET finished_at = now(), ok = $ok, file = NULLIF('$f', ''),
             bytes = $size, disk_total_bytes = $tot, disk_free_bytes = $free,
             kept = $kept, message = NULLIF('$msg', '') WHERE id = $id" >/dev/null
    if [ "$ok" = true ]; then
        sql "UPDATE maintenance_runs SET finished_at = now(), examined_count = $kept,
                 deleted_count = 0, outcome = 'ok'
              WHERE job = 'db_dump' AND run_date = '$oggi'" >/dev/null
        log "ok: $f ($size byte), conservati $kept"
    else
        # Senza finished_at il giro resta «non concluso»: Nagios e la pagina di
        # stato lo vedono. Si riprova fra un'ora, non a ogni ciclo.
        sql "UPDATE maintenance_runs SET outcome = 'errore'
              WHERE job = 'db_dump' AND run_date = '$oggi'" >/dev/null
        log "ERRORE: $msg"
    fi
}

if [ "${1:-}" = "--now" ]; then
    dump
    exit $?
fi

log "avviato: dump giornaliero alle $(printf '%02d:%02d' "$HOUR" "$MINUTE") ($TZ), conservati $KEEP"
while true; do
    touch /tmp/alive
    adesso=$(( $(date +%H | sed 's/^0//') * 60 + $(date +%M | sed 's/^0//') ))
    ora=$(( HOUR * 60 + MINUTE ))
    if [ "$adesso" -ge "$ora" ]; then
        oggi="$(date +%F)"
        fatto="$(sql "SELECT 1 FROM maintenance_runs WHERE job = 'db_dump'
                        AND run_date = '$oggi' AND finished_at IS NOT NULL" 2>/dev/null)"
        recente="$(sql "SELECT 1 FROM db_dumps WHERE started_at > now() - interval '1 hour'" 2>/dev/null)"
        if [ -z "$fatto" ] && [ -z "$recente" ]; then
            dump
        fi
    fi
    sleep 300
done
