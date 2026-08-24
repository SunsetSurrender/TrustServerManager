#!/usr/bin/env bash
#
# §7 della qualificazione: il riavvio della macchina, e i riavvii controllati.
#
# La domanda è una sola e non è teorica: dopo un riavvio, TSM torna su DA SOLO e
# con i dati di prima? Il pericolo che questo test esiste per escludere è scritto
# in `deploy/tsm.service`: Docker può essere pronto prima che il secondo disco sia
# montato, il bind di `pgdata` creerebbe la directory sul filesystem di root, e
# PostgreSQL vi farebbe `initdb`. Nessun errore, dati sul disco sbagliato.
# `RequiresMountsFor` esiste per vincere quella corsa, e una dichiarazione in un
# file di unità non è una prova che la corsa sia stata vinta.
#
# Il riavvio NON lo fa questo script: lo fa chi lo invoca, perché va fatto da fuori
# (su un host virtuale un reset, sulla distribuzione di qualificazione
# `wsl --terminate`). Lo script serve prima e dopo.
#
#   reboot-restart-test.sh --capture         prima: scrive lo stato in un file
#   reboot-restart-test.sh --verify          dopo:  confronta e giudica
#   reboot-restart-test.sh --restart-matrix  i riavvii controllati, uno per servizio
#
# Uso tipico:
#   ./tools/reboot-restart-test.sh --capture
#   <riavvio dell'host>
#   ./tools/reboot-restart-test.sh --verify
#   ./tools/reboot-restart-test.sh --restart-matrix
#
set -uo pipefail

APP="${TSM_APP_DIR:-/opt/tsm}"
STATO="${TSM_QUAL_STATE:-/var/tmp/tsm-prima-riavvio.txt}"
MODO=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --capture)        MODO=capture; shift ;;
        --verify)         MODO=verify; shift ;;
        --restart-matrix) MODO=matrix; shift ;;
        --dir)            APP="${2:-}"; shift 2 ;;
        --state)          STATO="${2:-}"; shift 2 ;;
        -h|--help)        sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "argomento non riconosciuto: $1" >&2; exit 2 ;;
    esac
done
[ -n "$MODO" ] || { echo "serve --capture, --verify o --restart-matrix" >&2; exit 2; }
cd "$APP" || { echo "directory inesistente: $APP" >&2; exit 2; }

PASS=0; FAIL=0
ck() {
    if [ "$2" = "1" ]; then printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1))
    else printf '  [FAIL] %s\n' "$1"; [ -n "${3:-}" ] && printf '         -> %s\n' "$3"
         FAIL=$((FAIL+1)); fi
}
q() { docker compose exec -T db psql -U tsm -d tsm -At -c "$1" 2>/dev/null | tr -d '\r'; }

# ------------------------------------------------------------------ attese
#: Readiness verde, con un limite. Si interroga DA DENTRO l'API: la domanda è se
#: l'applicazione è pronta, non se nginx risponde.
pronto() {
    local limite="${1:-60}" i
    for i in $(seq 1 "$limite"); do
        if docker compose exec -T api python -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=5)
    sys.exit(0 if b'ready' in r.read() else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then return 0; fi
        sleep 2
    done
    return 1
}

#: Tutti e quattro healthy, con un limite.
#:
#: ⚠ Si ASPETTA. Contarli subito dopo un riavvio di PostgreSQL ne trovava due:
#: gli healthcheck di api e worker hanno uno `start_period`, e misurare durante
#: quello misura la fretta di chi misura, non la salute del servizio.
tutti_sani() {
    local limite="${1:-60}" n i
    for i in $(seq 1 "$limite"); do
        n=$(docker compose ps --format '{{.Health}}' | grep -c healthy || true)
        [ "${n:-0}" -ge 4 ] && { echo "$n"; return 0; }
        sleep 2
    done
    echo "${n:-0}"; return 1
}

#: Lo stato dei dati in una riga. Con ritentativi: subito dopo un riavvio il primo
#: `exec` può arrivare mentre il container c'è e il server non accetta ancora
#: connessioni, e due stringhe vuote confrontate fra loro darebbero un PASS che non
#: significa niente.
stato_dati() {
    local v r d i
    for i in $(seq 1 20); do
        v=$(q "SELECT max(version) FROM inventory_versions")
        if [ -n "$v" ]; then
            r=$(q "SELECT count(*) FROM inventory_racks")
            d=$(q "SELECT count(*) FROM inventory_devices")
            echo "v${v}/r${r}/d${d}"
            return 0
        fi
        sleep 2
    done
    echo "__NON_LEGGIBILE__"; return 1
}

#: Il cursore del journal per l'unità.
#:
#: ⚠ Si usa un CURSORE e non `--since <orario>`. Sull'host di qualificazione
#: l'orologio non è monotono — a ogni avvio il kernel lo riporta all'ora dell'RTC e
#: poi la sincronizzazione lo riporta avanti — quindi un filtro temporale seleziona
#: un insieme che non corrisponde a niente, e diceva «il preflight non è girato» su
#: un preflight girato e superato. Il cursore è una posizione: cresce sempre, e non
#: gli importa che ora sia.
cursore() {
    journalctl -u tsm.service -n 0 --show-cursor --no-pager 2>/dev/null \
        | sed -n 's/^-- cursor: //p'
}

# ==================================================================
if [ "$MODO" = "capture" ]; then
    echo "== stato PRIMA del riavvio =="
    {
        echo "quando=$(date -Is)"
        echo "dati=$(stato_dati)"
        echo "utenze=$(q "SELECT count(*) || '|' || bool_or(must_change_pw)::text FROM users")"
        echo "audit=$(q 'SELECT count(*) FROM audit')"
        echo "sessioni=$(q 'SELECT count(*) FROM sessions')"
        echo "proiezione=$(q "SELECT head_version || '|' || head_sha256 FROM inventory_projection_state")"
        echo "digest=$(q "SELECT canonical_sha256 FROM inventory_versions ORDER BY version DESC LIMIT 1")"
    } > "$STATO"
    sed 's/^/  /' "$STATO"
    echo "  scritto in $STATO"
    exit 0
fi

# ==================================================================
if [ "$MODO" = "verify" ]; then
    [ -f "$STATO" ] || { echo "manca $STATO: eseguire prima --capture" >&2; exit 2; }

    echo "== 0. si aspetta che l'avvio finisca =="
    # Prima si aspetta, POI si guarda: interrogare i container mentre systemd sta
    # ancora eseguendo ExecStart dà FAIL su un avvio che sta riuscendo.
    for i in $(seq 1 90); do
        [ "$(systemctl is-active tsm.service)" = "active" ] && break
        sleep 2
    done
    echo "  tsm.service: $(systemctl is-active tsm.service) dopo $((i*2))s"

    echo "== 1. è un avvio nuovo =="
    # NON si usa /proc/sys/kernel/random/boot_id: su una distribuzione WSL il
    # kernel è condiviso con l'host e terminare la distribuzione non lo cambia,
    # quindi il boot_id direbbe «stesso avvio» anche dopo che systemd è stato
    # rieseguito da zero e tutti i mount e i container sono stati ricreati.
    # Il segnale affidabile è che i container siano stati avviati DOPO la cattura.
    RIF=$(stat -c %Y "$STATO")
    ID_DB=$(docker compose ps -q db 2>/dev/null | head -1)
    AVVIO=0
    [ -n "$ID_DB" ] && AVVIO=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$ID_DB")" +%s 2>/dev/null || echo 0)
    ck "i container sono stati avviati dopo la cattura precedente" \
       "$([ "$AVVIO" -gt "$RIF" ] && echo 1 || echo 0)" \
       "cattura=$(date -d "@$RIF" -Is) avvio=$(date -d "@$AVVIO" -Is)"
    echo "  systemd (spazio utente) partito: $(systemctl show -p UserspaceTimestamp --value)"

    echo "== 2. il disco dei dati si monta da solo =="
    for i in $(seq 1 30); do mountpoint -q /srv/tsm-data && break; sleep 2; done
    ck "/srv/tsm-data è montato" "$(mountpoint -q /srv/tsm-data && echo 1 || echo 0)"
    ck "ed è un filesystem distinto da /" \
       "$([ "$(stat -c %d /srv/tsm-data)" != "$(stat -c %d /)" ] && echo 1 || echo 0)" \
       "$(findmnt -n -o SOURCE,FSTYPE --mountpoint /srv/tsm-data)"
    ck "l'unità .mount generata da fstab è attiva" \
       "$([ "$(systemctl is-active 'srv-tsm\x2ddata.mount')" = "active" ] && echo 1 || echo 0)" \
       "$(systemctl is-active 'srv-tsm\x2ddata.mount')"

    echo "== 3. TSM ha aspettato il disco, e il preflight è girato =="
    ck "tsm.service è attivo senza intervento" \
       "$([ "$(systemctl is-active tsm.service)" = "active" ] && echo 1 || echo 0)" \
       "$(systemctl is-active tsm.service)"
    ck "l'unità non è in stato failed" \
       "$([ "$(systemctl is-failed tsm.service)" != "failed" ] && echo 1 || echo 0)"
    DEPS=$(systemctl show tsm.service -p After --value)
    # Il valore reso porta gli apici e DUE backslash (`"srv-tsm\\x2ddata.mount"`):
    # un pattern con un solo carattere jolly fra le due parti non combacia, e
    # diceva FAIL su una dipendenza presente e corretta.
    ck "systemd ha dedotto la dipendenza dal mount (RequiresMountsFor)" \
       "$(echo "$DEPS" | grep -qF 'x2ddata.mount' && echo 1 || echo 0)" "After=$DEPS"
    # ⚠ Si CATTURA in una variabile e poi si cerca, invece di mettere `grep -q` in
    # fondo a una pipe. Con `set -o pipefail` attivo, `cmd | grep -q PATTERN`
    # riporta FALLIMENTO proprio quando il pattern c'è: `grep -q` esce al primo
    # riscontro e chiude la pipe, `cmd` muore di SIGPIPE con stato 141, e
    # `pipefail` prende quello. Questo controllo diceva «il preflight non è
    # girato» su un journal che conteneva la riga cercata sei volte.
    PRE_UNIT="$(systemctl show tsm.service -p ExecStartPre --value)"
    case "$PRE_UNIT" in
        *preflight.sh*) E=1 ;;
        *) E=0 ;;
    esac
    ck "il preflight è ancora un ExecStartPre dell'unità" "$E" "$PRE_UNIT"

    JOURNAL="$(journalctl -u tsm.service -b --no-pager 2>/dev/null || true)"
    case "$JOURNAL" in
        *"PREFLIGHT SUPERATO"*) E=1 ;;
        *) E=0 ;;
    esac
    ck "il journal contiene un preflight SUPERATO in questo avvio" "$E" \
       "$(printf '%s' "$JOURNAL" | grep -i preflight | tail -2)"

    echo "== 4. i servizi =="
    SANI=$(tutti_sani 60)
    docker compose ps --format '  {{.Service}}  {{.Status}}'
    ck "tutti e quattro i servizi sono healthy" \
       "$([ "${SANI:-0}" -ge 4 ] && echo 1 || echo 0)" "$SANI healthy"

    echo "== 5. readiness =="
    pronto 90 && E=1 || E=0
    ck "readiness verde senza interventi" "$E" "timeout"

    echo "== 6. i dati sono esattamente quelli di prima =="
    for chiave in dati utenze proiezione digest; do
        A=$(sed -n "s/^$chiave=//p" "$STATO")
        case "$chiave" in
            dati)       B=$(stato_dati) ;;
            utenze)     B=$(q "SELECT count(*) || '|' || bool_or(must_change_pw)::text FROM users") ;;
            proiezione) B=$(q "SELECT head_version || '|' || head_sha256 FROM inventory_projection_state") ;;
            digest)     B=$(q "SELECT canonical_sha256 FROM inventory_versions ORDER BY version DESC LIMIT 1") ;;
        esac
        ck "$chiave identico dopo il riavvio" "$([ "$A" = "$B" ] && echo 1 || echo 0)" \
           "prima=$A dopo=$B"
    done
    # L'audit può solo CRESCERE — il worker registra — mai perdere righe.
    A=$(sed -n 's/^audit=//p' "$STATO"); B=$(q 'SELECT count(*) FROM audit')
    ck "l'audit non ha perso righe" "$([ "${B:-0}" -ge "${A:-0}" ] && echo 1 || echo 0)" \
       "prima=$A dopo=$B"

    echo "== 7. lo stato del cambio password =="
    # Un riavvio che rimettesse must_change_pw a true rimanderebbe l'amministratore
    # allo schermo del cambio, con una provvisoria che non esiste più.
    A=$(sed -n 's/^utenze=//p' "$STATO" | cut -d'|' -f2)
    B=$(q 'SELECT bool_or(must_change_pw) FROM users')
    ATTESO=$([ "$A" = "true" ] && echo t || echo f)
    ck "must_change_pw è invariato dopo il riavvio" \
       "$([ "$B" = "$ATTESO" ] && echo 1 || echo 0)" "prima=$A dopo=$B"

    # Le sessioni vivono in PostgreSQL e sopravvivono di proposito: un riavvio del
    # servizio non deve buttare fuori tutti gli operatori.
    echo "  sessioni nel database: $(q 'SELECT count(*) FROM sessions') (sopravvivono di proposito)"

    echo
    echo "=== $PASS PASS, $FAIL FAIL ==="
    exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
fi

# ==================================================================
if [ "$MODO" = "matrix" ]; then
    PRIMA=$(stato_dati)
    echo "stato di partenza: $PRIMA"
    [ "$PRIMA" = "__NON_LEGGIBILE__" ] && { echo "il database non risponde" >&2; exit 2; }
    echo

    echo "== riavvio di nginx =="
    t0=$(date +%s); docker compose restart web >/dev/null 2>&1
    pronto 60 && E=1 || E=0
    ck "readiness verde dopo il riavvio di nginx" "$E" "timeout"
    # Si chiede il CORPO e si guarda che ci sia: `wget -S` stampa le intestazioni
    # in un formato che dipende dall'implementazione (busybox), e cercarci un codice
    # di stato legherebbe il controllo a come wget stampa invece che a come nginx
    # risponde.
    BYTE=$(docker compose exec -T web sh -c \
        "wget -q --no-check-certificate -O- https://127.0.0.1:8443/ 2>/dev/null | wc -c" \
        2>/dev/null | tr -d '\r')
    ck "nginx serve di nuovo la pagina" \
       "$([ "${BYTE:-0}" -gt 1000 ] && echo 1 || echo 0)" "$BYTE byte"
    echo "  ($(($(date +%s)-t0))s)"

    echo "== riavvio dell'API =="
    t0=$(date +%s); docker compose restart api >/dev/null 2>&1
    pronto 90 && E=1 || E=0
    ck "readiness verde dopo il riavvio dell'API" "$E" "timeout"
    ORA=$(stato_dati)
    ck "i dati sono intatti" "$([ "$ORA" = "$PRIMA" ] && echo 1 || echo 0)" "prima=$PRIMA ora=$ORA"
    echo "  ($(($(date +%s)-t0))s)"

    echo "== riavvio del worker =="
    t0=$(date +%s); docker compose restart worker >/dev/null 2>&1
    H=""
    for i in $(seq 1 60); do
        H=$(docker compose ps worker --format '{{.Health}}' | tr -d '\r')
        [ "$H" = "healthy" ] && break
        sleep 2
    done
    # La salute del worker è il BATTITO nel database, non un PID vivo: un processo
    # vivo che non gira più è il guasto che conta, e è invisibile a un controllo
    # sul PID.
    ck "il worker torna healthy (battito nel database)" \
       "$([ "$H" = "healthy" ] && echo 1 || echo 0)" "health=$H"
    N=$(q 'SELECT count(*) FROM worker_heartbeat')
    ck "il battito resta una riga sola" "$([ "$N" = "1" ] && echo 1 || echo 0)" "$N righe"
    echo "  ($(($(date +%s)-t0))s)"

    echo "== riavvio di PostgreSQL =="
    t0=$(date +%s); docker compose restart db >/dev/null 2>&1
    pronto 120 && E=1 || E=0
    # `pool_pre_ping` esiste per questo: se servisse riavviare l'API a mano, un
    # riavvio del database lascerebbe l'applicazione morta con i container sani.
    ck "l'API riprende le connessioni da sola (pool_pre_ping)" "$E" \
       "readiness non torna verde: le connessioni morte non vengono scartate"
    ORA=$(stato_dati)
    ck "i dati sono intatti" "$([ "$ORA" = "$PRIMA" ] && echo 1 || echo 0)" "prima=$PRIMA ora=$ORA"
    SANI=$(tutti_sani 60)
    ck "tutti e quattro tornano healthy" \
       "$([ "${SANI:-0}" -ge 4 ] && echo 1 || echo 0)" "$SANI healthy"
    echo "  ($(($(date +%s)-t0))s)"

    echo "== riavvio completo attraverso systemd =="
    t0=$(date +%s); CUR=$(cursore)
    systemctl restart tsm.service
    for i in $(seq 1 60); do
        [ "$(systemctl is-active tsm.service)" = "active" ] && break
        sleep 2
    done
    ck "systemctl restart riesce" \
       "$([ "$(systemctl is-active tsm.service)" = "active" ] && echo 1 || echo 0)" \
       "$(systemctl is-active tsm.service)"
    NUOVE=$(journalctl -u tsm.service --after-cursor "$CUR" --no-pager 2>/dev/null)
    # Confronto con `case`, non `grep -q` in fondo a una pipe: vedi la nota in
    # --verify sulla trappola di `pipefail`.
    case "$NUOVE" in
        *"PREFLIGHT SUPERATO"*) E=1 ;;
        *) E=0 ;;
    esac
    ck "il preflight è girato in questo riavvio, e ha detto SUPERATO" "$E" \
       "$(printf '%s' "$NUOVE" | grep -i preflight | tail -2)"
    pronto 120 && E=1 || E=0
    ck "readiness verde dopo il riavvio completo" "$E" "timeout"
    ORA=$(stato_dati)
    ck "i dati sono intatti" "$([ "$ORA" = "$PRIMA" ] && echo 1 || echo 0)" "prima=$PRIMA ora=$ORA"
    echo "  ($(($(date +%s)-t0))s)"

    echo "== fermata e ripartenza =="
    t0=$(date +%s)
    systemctl stop tsm.service; sleep 3
    GIU=$(docker compose ps --format '{{.Service}}' | grep -c . || true)
    ck "a servizio fermo non resta nessun container in piedi" \
       "$([ "${GIU:-9}" -eq 0 ] && echo 1 || echo 0)" "$GIU ancora su"
    systemctl start tsm.service
    for i in $(seq 1 60); do
        [ "$(systemctl is-active tsm.service)" = "active" ] && break
        sleep 2
    done
    pronto 120 && E=1 || E=0
    ck "readiness verde dopo stop/start" "$E" "timeout"
    ORA=$(stato_dati)
    ck "i dati sono intatti" "$([ "$ORA" = "$PRIMA" ] && echo 1 || echo 0)" "prima=$PRIMA ora=$ORA"
    SANI=$(tutti_sani 60)
    ck "tutti e quattro healthy alla fine" \
       "$([ "${SANI:-0}" -ge 4 ] && echo 1 || echo 0)" "$SANI healthy"
    echo "  ($(($(date +%s)-t0))s)"

    echo
    echo "=== $PASS PASS, $FAIL FAIL ==="
    exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
fi
