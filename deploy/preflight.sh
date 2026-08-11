#!/usr/bin/env bash
#
# Preflight di produzione per Trust Server Manager.
#
# Gira PRIMA di `docker compose up`, e se qualcosa non torna il servizio NON
# parte. Il motivo per cui esiste è uno scenario preciso e silenzioso (§8.40):
#
#   Il volume `pgdata` è ancorato con un bind a /srv/tsm-data/postgres. Se il
#   secondo disco non è montato, Docker CREA quella directory sul filesystem di
#   root, PostgreSQL ci fa `initdb` e comincia a lavorare. Nessun errore, nessun
#   avviso: il servizio sembra sano e i dati sono sul disco sbagliato — che è
#   anche il disco che si riempie e che il backup del secondo volume non copre.
#
# Per questo il preflight non «sistema» niente. In particolare NON crea la
# directory dei dati quando il mount manca: un secondo disco assente è un
# guasto di avvio, non una condizione da aggirare.
#
# Ogni prerequisito ha un CODICE DI USCITA STABILE, così un'automazione può
# distinguere «manca il disco» da «manca il certificato» senza leggere il testo.
#
#   2   uso errato dello script
#   10  docker non utilizzabile
#   11  `docker compose config` non valido
#   12  immagine attesa non presente in locale
#   20  /srv/tsm-data non è un punto di mount
#   21  /srv/tsm-data risolve al filesystem di root
#   22  /srv/tsm-data/postgres non esiste
#   23  proprietario/gruppo della directory dei dati errati
#   24  permessi della directory dei dati troppo larghi
#   25  directory dei dati non scrivibile dall'identità di PostgreSQL
#   26  spazio libero insufficiente
#   27  contesto SELinux inadeguato
#   28  contesto SELinux non persistente (solo `chcon`, manca `semanage fcontext`)
#   30  file di secret mancante o con permessi/proprietario inadeguati
#   31  materiale TLS mancante o non leggibile dall'identità di nginx
#   40  deroga di sviluppo attiva (cookie non `Secure` o TSM_ENV non production)
#   41  una porta che in produzione non va pubblicata risulta pubblicata
#   42  la configurazione resa non è quella di produzione (override di sviluppo)
#
# Uso:
#   deploy/preflight.sh [--dir <directory del progetto>] [--quiet]
#
set -euo pipefail

# ------------------------------------------------------------------ costanti
DATA_MOUNT="/srv/tsm-data"
PG_DIR="${DATA_MOUNT}/postgres"

#: Spazio libero minimo per accettare l'avvio. 5 GiB non è «abbastanza per
#: lavorare», è «abbastanza per non riempire il disco mentre qualcuno se ne
#: accorge»: un PostgreSQL che finisce lo spazio si ferma in scrittura, e il
#: recupero è molto più lungo del previsto.
#:
#: ⚠ Sono 5 GiB VISTI DAL GUEST. Il disco è thin provisioned: il datastore
#: VMware sottostante può essere pieno anche quando il guest vede spazio. Quella
#: è responsabilità del monitoraggio dell'infrastruttura, non di questo script,
#: che non ha modo di vederla.
MIN_FREE_BYTES=$((5 * 1024 * 1024 * 1024))

#: Permessi ammessi per la directory dei dati. PostgreSQL stesso pretende 0700 o
#: 0750 e rifiuta di partire con di più; qui si rifiuta prima e con un messaggio
#: che dice cosa fare.
ALLOWED_MODES="700 750"

#: Secret attesi. Il terzo può essere VUOTO — un relay interno senza
#: autenticazione è normale in rete chiusa — ma il file deve esistere, perché
#: Compose fallisce il bind di un secret inesistente.
SECRETS_REQUIRED="postgres_password api_db_password"
SECRETS_OPTIONAL_EMPTY="smtp_password"

TLS_FILES="fullchain.pem privkey.pem"

QUIET=0
PROJECT_DIR=""

# ------------------------------------------------------------------ output
say()  { [ "$QUIET" -eq 1 ] || printf '  %s\n' "$*"; }
ok()   { [ "$QUIET" -eq 1 ] || printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [attenzione] %s\n' "$*" >&2; }

fail() {
    local code="$1"; shift
    printf '\nPREFLIGHT FALLITO (%s): %s\n' "$code" "$1" >&2
    shift
    while [ "$#" -gt 0 ]; do printf '  → %s\n' "$1" >&2; shift; done
    printf '\nL'\''avvio è stato rifiutato. Vedi deploy/README.md.\n' >&2
    exit "$code"
}

# ------------------------------------------------------------------ argomenti
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)   PROJECT_DIR="${2:-}"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) printf 'argomento non riconosciuto: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [ -z "$PROJECT_DIR" ]; then
    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
[ -d "$PROJECT_DIR" ] || { printf 'directory di progetto inesistente: %s\n' "$PROJECT_DIR" >&2; exit 2; }
cd "$PROJECT_DIR"

say "progetto: $PROJECT_DIR"

# ==================================================================
# 1. docker e configurazione resa
# ==================================================================
command -v docker >/dev/null 2>&1 \
    || fail 10 "docker non è nel PATH" \
            "installare Docker Engine e il plugin compose"

docker info >/dev/null 2>&1 \
    || fail 10 "il demone Docker non risponde" \
            "systemctl status docker"

# La configurazione RESA è l'unica fonte attendibile: è quella che Compose
# userà davvero, con gli override già applicati. Controllare i file sorgente
# uno per uno lascerebbe fuori proprio il caso che interessa, cioè un override
# di sviluppo aggiunto per sbaglio.
CONFIG="$(docker compose config 2>/dev/null)" \
    || fail 11 "\`docker compose config\` non è valido" \
            "eseguirlo a mano per vedere l'errore"
ok "configurazione di Compose valida"

# ------------------------------------------------------------------ lettura
# della configurazione resa, con `awk` e nient'altro.
#
# La prima versione usava python3 con PyYAML e ricadeva su grep quando mancava.
# Era un errore di forma pericolosa: su una macchina dove `python3` esiste ma non
# funziona (accade: interprete a metà, PYTHONHOME rotto), `command -v` diceva sì,
# l'estrazione restituiva stringa vuota, e «nessuna porta pubblicata» risultava
# vero perché non si era letto niente. Un preflight che si distrae proprio sul
# controllo che deve fare è peggio di un preflight assente.
#
# `docker compose config` produce YAML NORMALIZZATO — indentazione di due spazi,
# chiavi in ordine, forma lunga per le porte — quindi un estrattore basato
# sull'indentazione è deterministico. Niente interpreti, niente ripieghi
# silenziosi. Sintassi POSIX: nessuna estensione GNU.

#: Righe appartenenti a un servizio (tutto ciò che è indentato sotto `  <nome>:`).
cfg_service_block() {
    printf '%s\n' "$CONFIG" | awk -v want="$1" '
        $0 == "services:" { sec = 1; next }
        /^[^ ]/           { sec = 0; insvc = 0 }
        sec && /^  [^ ]/ {
            name = $0
            sub(/^  /, "", name)
            sub(/:[[:space:]]*$/, "", name)
            insvc = (name == want)
            next
        }
        insvc { print }
    '
}

#: Valore di una variabile d'ambiente di un servizio, stringa vuota se assente.
cfg_env_value() {
    cfg_service_block "$1" | awk -v k="$2" '
        /^    environment:/ { ine = 1; next }
        /^    [^ ]/         { ine = 0 }
        ine {
            line = $0
            sub(/^[[:space:]]+/, "", line)
            pos = index(line, ":")
            if (pos > 0) {
                key = substr(line, 1, pos - 1)
                val = substr(line, pos + 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
                gsub(/^"|"$/, "", val)
                if (key == k) { print val; exit }
            }
        }
    '
}

#: Porte pubblicate sull'host da un servizio, una per riga.
cfg_published_ports() {
    cfg_service_block "$1" | awk '
        /^    ports:/ { inp = 1; next }
        /^    [^ ]/   { inp = 0 }
        inp && /published:/ {
            line = $0
            sub(/.*published:[[:space:]]*/, "", line)
            gsub(/"/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            if (line != "") print line
        }
    '
}

#: Immagine di un servizio.
cfg_image() {
    cfg_service_block "$1" | awk '
        /^    image:/ {
            line = $0
            sub(/.*image:[[:space:]]*/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            print line; exit
        }
    '
}

#: `device` del bind del volume pgdata, stringa vuota se non è ancorato.
cfg_pgdata_device() {
    printf '%s\n' "$CONFIG" | awk '
        $0 == "volumes:" { sec = 1; next }
        /^[^ ]/          { sec = 0; inpg = 0 }
        sec && /^  [^ ]/ {
            name = $0
            sub(/^  /, "", name)
            sub(/:[[:space:]]*$/, "", name)
            inpg = (name == "pgdata")
            next
        }
        inpg && /^      device:/ {
            line = $0
            sub(/.*device:[[:space:]]*/, "", line)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
            print line; exit
        }
    '
}

# Controllo di sanità dell'estrattore: se non riesce a leggere nemmeno
# l'immagine del database, la configurazione non ha la forma attesa e TUTTI i
# controlli che seguono sarebbero vacui. Si fallisce, non si prosegue al buio.
if [ -z "$(cfg_image db)" ]; then
    fail 11 "non riesco a leggere la configurazione resa di Compose" \
            "l'estrattore non trova l'immagine del servizio 'db'" \
            "verificare con: docker compose config"
fi

# ==================================================================
# 2. identità dei container, dedotte dalle IMMAGINI pinnate
# ==================================================================
# NON si assume «postgres è 999» né «nginx è 101»: sono valori dell'immagine, e
# un'immagine diversa (o un aggiornamento del digest) li cambierebbe senza che
# nessuno se ne accorga finché PostgreSQL non riesce più a scrivere.
id_in_image() {
    # $1 = immagine, $2 = -u|-g, $3 = utente atteso nell'immagine
    docker run --rm --entrypoint id "$1" "$2" "$3" 2>/dev/null | tr -d '\r\n'
}

DB_IMAGE="$(cfg_image db)"
API_IMAGE="$(cfg_image api)"
WEB_IMAGE="$(cfg_image web)"
[ -n "$DB_IMAGE" ] && [ -n "$API_IMAGE" ] && [ -n "$WEB_IMAGE" ] \
    || fail 11 "non ho potuto determinare le immagini dei servizi dalla configurazione"

for img in "$DB_IMAGE" "$API_IMAGE" "$WEB_IMAGE"; do
    docker image inspect "$img" >/dev/null 2>&1 \
        || fail 12 "immagine non presente in locale: $img" \
                "in rete chiusa si carica con: docker load -i <archivio>.tar" \
                "il preflight non scarica niente di propria iniziativa"
done
ok "immagini attese presenti in locale"

PG_UID="$(id_in_image "$DB_IMAGE" -u postgres)"
PG_GID="$(id_in_image "$DB_IMAGE" -g postgres)"
[ -n "$PG_UID" ] && [ -n "$PG_GID" ] \
    || fail 12 "non ho potuto leggere uid/gid di postgres dall'immagine $DB_IMAGE"
ok "identità di PostgreSQL dall'immagine: ${PG_UID}:${PG_GID}"

# L'utente dell'immagine dell'API e del web si legge dalla configurazione
# dell'immagine, che è dove `USER` finisce.
API_USER="$(docker image inspect --format '{{.Config.User}}' "$API_IMAGE" 2>/dev/null)"
WEB_USER="$(docker image inspect --format '{{.Config.User}}' "$WEB_IMAGE" 2>/dev/null)"
API_UID="${API_USER%%:*}"; API_UID="${API_UID:-0}"
WEB_UID="${WEB_USER%%:*}"; WEB_UID="${WEB_UID:-0}"
ok "identità dell'API: ${API_UID} · identità di nginx: ${WEB_UID}"

# ==================================================================
# 3. il secondo disco. Il controllo che giustifica lo script.
# ==================================================================
command -v findmnt >/dev/null 2>&1 \
    || fail 20 "findmnt non disponibile (pacchetto util-linux)" \
            "senza findmnt non si può distinguere un mount da una directory"

MNT_INFO="$(findmnt --noheadings --output SOURCE,FSTYPE,TARGET \
                    --mountpoint "$DATA_MOUNT" 2>/dev/null || true)"
if [ -z "$MNT_INFO" ]; then
    extra=""
    [ -d "$DATA_MOUNT" ] && extra="la directory $DATA_MOUNT ESISTE ma non è un punto di mount: è una directory sul filesystem di root"
    fail 20 "$DATA_MOUNT non è un punto di mount" \
            "${extra:-la directory $DATA_MOUNT non esiste}" \
            "il secondo disco non è montato. NON si crea la directory per far partire il servizio:" \
            "PostgreSQL scriverebbe sul filesystem di root, e il backup del volume dati non lo coprirebbe." \
            "verificare: lsblk; findmnt -M $DATA_MOUNT; mount -a; systemctl status srv-tsm\\\\x2ddata.mount"
fi
ok "punto di mount presente: $MNT_INFO"

# Sorgente e numero di device confrontati con quelli di `/`. Due segnali
# indipendenti di proposito: `findmnt` da solo non distinguerebbe un bind mount
# del filesystem di root su $DATA_MOUNT, che avrebbe la stessa SOURCE ma
# passerebbe il controllo «è un mountpoint».
SRC_DATA="$(findmnt -n -o SOURCE --mountpoint "$DATA_MOUNT")"
SRC_ROOT="$(findmnt -n -o SOURCE --mountpoint / || true)"
DEV_DATA="$(stat -c %d "$DATA_MOUNT")"
DEV_ROOT="$(stat -c %d /)"
if [ "$DEV_DATA" = "$DEV_ROOT" ]; then
    fail 21 "$DATA_MOUNT sta sullo STESSO filesystem di /" \
            "sorgente dati: ${SRC_DATA:-?} · sorgente root: ${SRC_ROOT:-?}" \
            "è un bind del filesystem di root, non il secondo disco" \
            "il volume dati deve essere un filesystem dedicato (disco 2)"
fi
ok "filesystem dedicato, distinto da / (dev $DEV_DATA ≠ $DEV_ROOT)"

# ------------------------------------------------ directory dei dati
[ -d "$PG_DIR" ] \
    || fail 22 "$PG_DIR non esiste" \
            "il mount c'è ma la directory dei dati no: crearla come da runbook" \
            "install -d -o $PG_UID -g $PG_GID -m 0700 $PG_DIR"

DIR_UID="$(stat -c %u "$PG_DIR")"
DIR_GID="$(stat -c %g "$PG_DIR")"
DIR_MODE="$(stat -c %a "$PG_DIR")"

[ "$DIR_UID" = "$PG_UID" ] && [ "$DIR_GID" = "$PG_GID" ] \
    || fail 23 "proprietario di $PG_DIR errato: ${DIR_UID}:${DIR_GID}, atteso ${PG_UID}:${PG_GID}" \
            "l'uid atteso viene dall'immagine $DB_IMAGE, non da un valore fisso" \
            "chown -R ${PG_UID}:${PG_GID} $PG_DIR"

case " $ALLOWED_MODES " in
    *" $DIR_MODE "*) : ;;
    *) fail 24 "permessi di $PG_DIR troppo larghi: 0$DIR_MODE" \
              "PostgreSQL ammette solo 0700 o 0750 e rifiuta di partire con di più" \
              "chmod 0700 $PG_DIR" ;;
esac
ok "directory dei dati: ${DIR_UID}:${DIR_GID} 0${DIR_MODE}"

# ------------------------------------------------ scrivibilità reale
# Si prova a scrivere DAVVERO, come l'utente di PostgreSQL, attraverso Docker.
# Un controllo sui soli permessi non basta: sotto SELinux enforcing l'etichetta
# sbagliata dà EACCES con permessi perfetti, ed è un guasto che si manifesta solo
# quando il container prova a scrivere.
PROBE=".tsm-preflight-$$"
if ! docker run --rm --user "${PG_UID}:${PG_GID}" \
        -v "${PG_DIR}:/probe" --entrypoint sh "$DB_IMAGE" \
        -c "touch /probe/$PROBE && rm -f /probe/$PROBE" >/dev/null 2>&1; then
    fail 25 "$PG_DIR non è scrivibile dall'identità di PostgreSQL (${PG_UID}:${PG_GID})" \
            "cause tipiche: proprietario errato, permessi errati, etichetta SELinux errata," \
            "filesystem montato in sola lettura, o quota esaurita" \
            "provare: findmnt -M $DATA_MOUNT -o OPTIONS ; ls -lZd $PG_DIR"
fi
ok "scrittura verificata come ${PG_UID}:${PG_GID} attraverso Docker"

# ------------------------------------------------ spazio libero
AVAIL="$(df -B1 --output=avail "$DATA_MOUNT" 2>/dev/null | tail -1 | tr -d ' ')"
USEPCT="$(df --output=pcent "$DATA_MOUNT" 2>/dev/null | tail -1 | tr -d ' %')"
[ -n "$AVAIL" ] || fail 26 "non ho potuto leggere lo spazio libero di $DATA_MOUNT"
if [ "$AVAIL" -lt "$MIN_FREE_BYTES" ]; then
    fail 26 "spazio libero insufficiente su $DATA_MOUNT" \
            "liberi $((AVAIL / 1024 / 1024)) MiB, minimo $((MIN_FREE_BYTES / 1024 / 1024)) MiB (uso ${USEPCT:-?}%)" \
            "un PostgreSQL che finisce lo spazio si ferma in scrittura, e il recupero è lungo"
fi
ok "spazio libero: $((AVAIL / 1024 / 1024 / 1024)) GiB (uso ${USEPCT:-?}%)"
# Soglie di monitoraggio: qui si avvisa, non si blocca. Bloccare all'85% vorrebbe
# dire rifiutare l'avvio a un servizio che sta ancora lavorando bene.
if [ -n "${USEPCT:-}" ] && [ "$USEPCT" -ge 85 ]; then
    warn "uso al ${USEPCT}%: soglia critica di monitoraggio superata (§8.40)"
elif [ -n "${USEPCT:-}" ] && [ "$USEPCT" -ge 70 ]; then
    warn "uso al ${USEPCT}%: soglia di allerta di monitoraggio superata (§8.40)"
fi

# ------------------------------------------------ SELinux
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    CTX="$(stat -c %C "$PG_DIR" 2>/dev/null || true)"
    TYPE="$(printf '%s' "$CTX" | cut -d: -f3)"
    case "$TYPE" in
        container_file_t|svirt_sandbox_file_t) ok "contesto SELinux: $TYPE" ;;
        "") warn "SELinux enforcing ma non ho potuto leggere il contesto di $PG_DIR" ;;
        *)  fail 27 "contesto SELinux inadeguato su $PG_DIR: $CTX" \
                  "atteso il tipo container_file_t" \
                  "semanage fcontext -a -t container_file_t '${PG_DIR}(/.*)?'" \
                  "restorecon -Rv $PG_DIR" ;;
    esac

    # Persistenza. Un `chcon` funziona subito e si perde alla prima
    # rietichettatura del filesystem (o a un `restorecon -R /`): il servizio
    # lavora per mesi e poi un giorno PostgreSQL non scrive più, senza che
    # nessuno abbia toccato TSM. La regola persistente si verifica, non si spera.
    if command -v semanage >/dev/null 2>&1; then
        if semanage fcontext -l 2>/dev/null | grep -q "^${PG_DIR}"; then
            ok "regola SELinux persistente presente per $PG_DIR"
        else
            fail 28 "l'etichetta SELinux di $PG_DIR non è persistente" \
                    "il tipo attuale è corretto, ma manca la regola in semanage:" \
                    "un chcon si perde alla prima rietichettatura, e PostgreSQL smette di scrivere" \
                    "semanage fcontext -a -t container_file_t '${PG_DIR}(/.*)?' && restorecon -Rv $PG_DIR"
        fi
    else
        warn "semanage non disponibile: persistenza dell'etichetta SELinux NON verificata"
        warn "installare policycoreutils-python-utils per poterla verificare"
    fi
else
    say "SELinux non enforcing: controlli di contesto saltati"
fi

# ==================================================================
# 4. secret e materiale TLS
# ==================================================================
check_secret() {
    # Dichiarazioni SEPARATE: `local a="$1" b="secrets/$a"` non è affidabile —
    # in alcune versioni di bash `local` crea tutti i nomi prima di valutare le
    # assegnazioni, e con `set -u` il riferimento ad `$a` nella stessa
    # istruzione aborta con «unbound variable». Trovato eseguendo il preflight
    # nella VM, dove bash è più recente di quello usato per scriverlo.
    local name="$1"
    local allow_empty="$2"
    local path="secrets/$name"
    [ -f "$path" ] || fail 30 "file di secret mancante: $path" \
            "Compose non può montare un secret inesistente" \
            "vedi backend/README.md per la generazione"
    local mode owner
    mode="$(stat -c %a "$path")"
    owner="$(stat -c %u "$path")"
    # Nessun bit per gruppo o altri: un secret leggibile da chiunque sulla
    # macchina non è un secret.
    case "$mode" in
        400|600) : ;;
        *) fail 30 "permessi inadeguati su $path: 0$mode" \
                 "atteso 0400 o 0600" \
                 "chmod 0400 $path" ;;
    esac
    # Con 0400/0600 il file è leggibile SOLO dal proprietario: deve quindi essere
    # dell'utente del container che lo legge, altrimenti il servizio parte e
    # fallisce alla prima lettura.
    [ "$owner" = "$API_UID" ] \
        || fail 30 "proprietario di $path errato: uid $owner, atteso $API_UID" \
                "con 0$mode solo il proprietario può leggere, e a leggerlo è il container" \
                "chown ${API_UID}:${API_UID} $path"
    if [ "$allow_empty" != "yes" ] && [ ! -s "$path" ]; then
        fail 30 "il secret $path è vuoto" "generarne uno come da backend/README.md"
    fi
    ok "secret $name: 0$mode uid $owner"
}

for s in $SECRETS_REQUIRED; do check_secret "$s" no; done
for s in $SECRETS_OPTIONAL_EMPTY; do check_secret "$s" yes; done

for f in $TLS_FILES; do
    path="secrets/tls/$f"
    [ -f "$path" ] || fail 31 "materiale TLS mancante: $path" \
            "in produzione qui va il certificato aziendale" \
            "nginx non parte senza certificato e chiave"
    mode="$(stat -c %a "$path")"
    owner="$(stat -c %u "$path")"
    # La chiave privata non deve essere leggibile da altri; il certificato può.
    if [ "$f" = "privkey.pem" ]; then
        case "$mode" in
            400|600|440|640) : ;;
            *) fail 31 "permessi inadeguati sulla chiave privata $path: 0$mode" \
                     "atteso 0400 o 0600" "chmod 0400 $path" ;;
        esac
        [ "$owner" = "$WEB_UID" ] \
            || fail 31 "proprietario di $path errato: uid $owner, atteso $WEB_UID (nginx)" \
                    "chown ${WEB_UID}:${WEB_UID} $path"
    fi
    ok "TLS $f: 0$mode uid $owner"
done

# ==================================================================
# 5. la configurazione resa è quella di PRODUZIONE
# ==================================================================
# Questo blocco è ciò che impedisce all'unità systemd di avviare per sbaglio una
# configurazione di sviluppo: non si controlla quali file sono stati passati, si
# controlla il RISULTATO. Un override aggiunto da chiunque, in qualunque modo,
# cambia il risultato e viene visto qui.

TSM_ENV_VAL="$(cfg_env_value api TSM_ENV)"
COOKIE_VAL="$(cfg_env_value api TSM_COOKIE_SECURE)"

case "$TSM_ENV_VAL" in
    production|"") : ;;
    *) fail 40 "TSM_ENV nella configurazione resa è '$TSM_ENV_VAL', non 'production'" \
             "è la deroga che autorizza i cookie non Secure: in produzione non si usa" \
             "non passare -f compose.dev.yaml all'unità systemd" ;;
esac
case "$(printf '%s' "$COOKIE_VAL" | tr 'A-Z' 'a-z')" in
    false|0|no) fail 40 "TSM_COOKIE_SECURE è '$COOKIE_VAL' nella configurazione resa" \
             "un cookie di sessione senza Secure viaggia in chiaro" \
             "l'API rifiuterebbe comunque di partire (§8.29): qui si rifiuta prima" ;;
esac
ok "ambiente: production, cookie Secure"

# ------------------------------------------------ porte non pubblicate
for svc in db api; do
    p="$(cfg_published_ports "$svc")"
    [ -z "$p" ] || fail 41 "il servizio '$svc' pubblica porte sull'host: $(printf '%s' "$p" | tr '\n' ' ')" \
            "in produzione né il database né l'API si raggiungono dall'host (§8.34)" \
            "una richiesta che arriva dal bridge Docker cade dentro TSM_TRUSTED_PROXIES," \
            "e le sue intestazioni X-Forwarded-* verrebbero credute"
done
ok "db e api non pubblicano porte"

# ------------------------------------------------ il volume è ancorato al disco
BIND_DEVICE="$(cfg_pgdata_device)"
[ "$BIND_DEVICE" = "$PG_DIR" ] \
    || fail 42 "il volume pgdata non è ancorato a $PG_DIR (device: '${BIND_DEVICE:-nessuno}')" \
            "senza il bind, PostgreSQL scrive sotto /var/lib/docker sul disco di sistema" \
            "probabile causa: è stato incluso compose.storage-dev.yaml, che è solo per lo sviluppo"
ok "pgdata ancorato a $BIND_DEVICE"

# ------------------------------------------------ le immagini stanno sul disco 1
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [ -n "$DOCKER_ROOT" ]; then
    case "$DOCKER_ROOT" in
        "$DATA_MOUNT"|"$DATA_MOUNT"/*)
            fail 42 "la radice di Docker è sul disco dei dati: $DOCKER_ROOT" \
                  "immagini e livelli dei container vanno sul disco di sistema (§8.40)" \
                  "il disco 2 è solo per i dati durevoli del database" ;;
        *) ok "radice di Docker fuori dal disco dati: $DOCKER_ROOT" ;;
    esac
fi

[ "$QUIET" -eq 1 ] || printf '\nPREFLIGHT SUPERATO: si può avviare.\n'
exit 0
