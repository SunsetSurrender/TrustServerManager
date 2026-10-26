#!/bin/bash
# =====================================================================
# Fase 2K-A — accettazione PRIMA dell'installazione, sull'host reale.
#
# Misura tsm-prd e lo confronta con il rilascio QUALIFICATO. Non
# installa niente, non avvia lo stack, non crea stato applicativo:
# nessun cluster PostgreSQL, nessun amministratore, nessuna versione
# di inventario, nessuna cronologia di audit.
#
# Perché uno script e non un runbook a mano: la parte §4 (SELinux)
# richiede di ROMPERE l'etichetta della directory dei dati per provare
# che il preflight se ne accorge. Fatto a mano, su una macchina
# destinata alla produzione, si lascia dietro una directory
# mal-etichettata alla prima distrazione. Qui il contesto originale si
# rileva prima, si ripristina in un trap, e il ripristino viene
# VERIFICATO — non dato per fatto.
#
# Opzioni:
#   --dir <dir>        progetto installato (default /opt/tsm)
#   --pin <file>       pin del rilascio (default <dir>/deploy/release.pin)
#   --senza-selinux    SELinux non in uso per decisione: lo si registra
#   --selinux-gate     esercita i codici 27/28 (solo con SELinux enforcing)
#   --manifest <file>  scrive il manifesto di accettazione
#
# Uscita:
#   0   pronto per la distribuzione controllata
#   1   bloccato: almeno un requisito pre-installazione non è soddisfatto
#   2   uso errato
#
# ⚠ Un valore che non si riesce a MISURARE è un FALLIMENTO, non un
#   salto. È il motivo per cui la fase 2J non ha potuto chiudere
#   SELinux: là «non enforcing» era legittimo (WSL), qui è un blocco.
# =====================================================================
set -uo pipefail

# ---------------------------------------------------------------- attesi
# Valori PINNATI dal rilascio qualificato. Sono scritti qui perché il
# compito di questo script è confrontare l'host con ciò che è stato
# qualificato: la costante È il riferimento, non un'ipotesi.
ATTESO_HOSTNAME="tsm-prd"          # nome definitivo (2026-10-26)
ATTESI_VCPU=4
ATTESA_RAM_GIB=8
ATTESO_SWAP_GIB=8          # ⚠ 8, non i 2 dell'host WSL di qualificazione
ATTESO_DISCO_GB=100        # sistema e dati, thin, ciascuno
MOUNT_DATI="/srv/tsm-data"
DIR_PG="${MOUNT_DATI}/postgres"
DIR_PROGETTO="/opt/tsm"

COMMIT_QUALIFICATO="23d47dc1bd952e3b87282bb0e088d4771d409266"
ALBERO_RILASCIO="18f712556844e6ba21b32603d2aa431f06569da60cbf5ea4f2197b292dd387cb"
# Il digest del FILE (byte, LF) e quello del DOCUMENTO sono due numeri
# diversi. Uno script di shell puo verificare solo il primo: il secondo
# richiede la canonicalizzazione del prodotto, e lo verifica il prodotto.
BASELINE_FILE="c22bcb4ed3888ca19648169a2dd06545f15e159ea33154fb9e91a50e8a9909b6"
BASELINE_DOC="febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"
MIGRAZIONE="0013_domain"
MAPPER=2
SCHEMA_VERSION=1

# Le due immagini COSTRUITE si confrontano per Id: non sono riproducibili
# dal sorgente (le date finiscono nei layer), quindi si confrontano, non
# si ricalcolano. Quella di PostgreSQL arriva dal registry e si confronta
# per RepoDigest, che è la cosa che `postgres@sha256:…` nomina.
ID_API="sha256:54061db1d26a56e5235029d3852ba640106a9bfd5589d029b06ed80c8fb9a168"
ID_WEB="sha256:2d22b6e27bb3d3f3036a485ead23a26a20b5a780ac9bca44448ba208a325c541"
DIGEST_DB="sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
TAG_API="tsm-api:0.2.0"
TAG_WEB="tsm-web:0.2.0"

# Il pin del rilascio (deploy/release.pin, scritto da tools/build-release.sh)
# prevale sulle costanti qui sopra, che restano quelle del rilascio 23d47dc.
# Lettura del pin SENZA `source`: è un file di dati, e un file di dati che
# viene eseguito è un modo di far girare codice come root sull'host. Si
# accettano solo chiavi note, ognuna con la propria forma.
carica_pin() { # file
    local f="$1" riga k v forma
    [ -f "$f" ] || return 1
    while IFS= read -r riga || [ -n "$riga" ]; do
        riga="${riga%$'\r'}"
        case "$riga" in ''|'#'*) continue ;; esac
        k="${riga%%=*}"; v="${riga#*=}"
        case "$k" in
            COMMIT_QUALIFICATO)            forma='^[0-9a-f]{40}$' ;;
            ALBERO_RILASCIO|BASELINE_FILE|BASELINE_DOC) forma='^[0-9a-f]{64}$' ;;
            ID_API|ID_WEB|DIGEST_DB)       forma='^sha256:[0-9a-f]{64}$' ;;
            TAG_API)                       forma='^tsm-api:[0-9A-Za-z._-]+$' ;;
            TAG_WEB)                       forma='^tsm-web:[0-9A-Za-z._-]+$' ;;
            MIGRAZIONE)                    forma='^[0-9]{4}_[a-z0-9_]+$' ;;
            *) printf 'pin %s: chiave sconosciuta %s\n' "$f" "$k" >&2; exit 2 ;;
        esac
        [[ "$v" =~ $forma ]] || { printf 'pin %s: valore non valido per %s\n' "$f" "$k" >&2; exit 2; }
        printf -v "$k" '%s' "$v"
    done < "$f"
    return 0
}

# §6 — ciò che NON deve arrivare sull'host. Un elenco meccanico: la
# memoria dell'operatore non è un controllo.
VIETATI=(
    "compose.dev.yaml"                 # override di sviluppo
    "compose.override.yaml"            # docker compose lo legge DA SOLO
    "compose.storage-dev.yaml"
    "tools/smtp-sink.py"               # un ascoltatore in attesa di essere avviato
    "tools/make-dev-tls.ps1"           # certificati di sviluppo
    "tools/notification-qualification.sh"
    "tools/failure-injection-test.py"
    "tools/mutation-harness.py"
    "web/nginx.dev.conf"               # cookie non Secure, nessun HSTS
    "secrets/tls/dev-cert.pem"
    "deploy/baseline/fixture.json"     # la fixture di qualificazione NON è la baseline
)

MANIFESTO=""
ESERCITA_SELINUX=0
# SELinux non in uso su tsm-prd: è una DECISIONE dell'amministratore
# (2026-10-26), non un requisito mancato. Si dichiara con --senza-selinux, così
# resta scritta nel manifesto invece di sparire in un controllo saltato.
SENZA_SELINUX=0
PIN=""
JSON=0

while [ $# -gt 0 ]; do
    case "$1" in
        --manifest)  MANIFESTO="${2:?--manifest richiede un percorso}"; shift 2 ;;
        --dir)       DIR_PROGETTO="${2:?--dir richiede un percorso}"; shift 2 ;;
        --selinux-gate) ESERCITA_SELINUX=1; shift ;;
        --senza-selinux) SENZA_SELINUX=1; shift ;;
        --pin)       PIN="${2:?--pin richiede un percorso}"; shift 2 ;;
        --json)      JSON=1; shift ;;
        -h|--help)   sed -n '2,37p' "$0"; exit 0 ;;
        *) printf 'argomento non riconosciuto: %s\n' "$1" >&2; exit 2 ;;
    esac
done

PIN="${PIN:-$DIR_PROGETTO/deploy/release.pin}"
if carica_pin "$PIN"; then
    printf 'rilascio dal pin %s: %s\n' "$PIN" "${COMMIT_QUALIFICATO:0:12}"
else
    printf 'pin %s assente: valgono le costanti del rilascio %s\n' "$PIN" "${COMMIT_QUALIFICATO:0:12}"
fi

# ---------------------------------------------------------------- esito
OK=0; KO=0; APERTI=0
RIGHE=()
FATTI=()

_reg() { RIGHE+=("$1|$2|$3"); }                       # stato|voce|osservato
fatto() { FATTI+=("$1"); }                            # per il manifesto §14

ok()    { OK=$((OK+1));         printf '  \033[32mOK\033[0m    %s\n' "$1"; _reg OK "$1" "${2:-}"; }
ko()    { KO=$((KO+1));         printf '  \033[31mKO\033[0m    %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; _reg KO "$1" "${2:-}"; }
aperto(){ APERTI=$((APERTI+1)); printf '  \033[33mAPERTO\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; _reg APERTO "$1" "${2:-}"; }
sez()   { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# Confronto numerico con tolleranza: la RAM riportata dal kernel è
# sempre un po' meno di quella nominale (memoria riservata dal firmware).
circa() { # valore atteso tolleranza_percento
    python3 - "$@" <<'PY' 2>/dev/null || return 1
import sys
v,a,t=float(sys.argv[1]),float(sys.argv[2]),float(sys.argv[3])
sys.exit(0 if abs(v-a) <= a*t/100 else 1)
PY
}

# =====================================================================
sez "§2 — la macchina reale"
# =====================================================================
H="$(hostnamectl --static 2>/dev/null || hostname 2>/dev/null || true)"
if [ "$H" = "$ATTESO_HOSTNAME" ]; then
    ok "hostname: $H"
else
    ko "hostname: '${H:-?}', atteso '$ATTESO_HOSTNAME'" \
       "questo script è per tsm-prd: su un'altra macchina i risultati non valgono"
fi
fatto "hostname=${H:-?}"

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    SO="$(. /etc/os-release; printf '%s %s' "${NAME:-?}" "${VERSION:-${VERSION_ID:-?}}")"
    ok "sistema operativo: $SO"
else
    ko "impossibile leggere /etc/os-release"
    SO="?"
fi
fatto "os=$SO"
KERNEL="$(uname -r)"
fatto "kernel=$KERNEL"

# ⚠ Il nome dell'host NON è una prova d'identità.
#   L'host WSL di qualificazione della fase 2J si chiama anch'esso
#   «tsm-prd-01»: eseguendo questo script là, il controllo dell'hostname
#   passava e il resto dei numeri sembrava autorevole. È l'errore
#   operativo più probabile di tutta la fase — misurare la macchina
#   sbagliata e crederci — e costa una riga impedirlo.
case "$KERNEL$(cat /proc/version 2>/dev/null)" in
    *microsoft*|*WSL*|*Microsoft*)
        ko "questo è un host WSL, non la VM di produzione" \
           "kernel: $KERNEL" \
           "l'host di qualificazione si chiama anch'esso tsm-prd-01: il nome non prova niente." \
           "eseguire l'accettazione sulla VM reale." ;;
    *) ok "kernel non WSL: $KERNEL" ;;
esac
if [ -d /run/systemd/system ]; then
    ok "systemd è il gestore dei servizi"
else
    ko "systemd non attivo: l'unità tsm.service non avrebbe chi la esegua"
fi

VCPU="$(nproc 2>/dev/null || echo 0)"
[ "$VCPU" = "$ATTESI_VCPU" ] \
    && ok "vCPU: $VCPU" \
    || ko "vCPU: $VCPU, attese $ATTESI_VCPU" \
          "il budget dei pool (15+15, un worker uvicorn) è stato calcolato su ${ATTESI_VCPU} vCPU"
fatto "vcpu=$VCPU"

RAM_KB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
RAM_GIB="$(awk -v k="$RAM_KB" 'BEGIN{printf "%.2f", k/1048576}')"
if circa "$RAM_GIB" "$ATTESA_RAM_GIB" 8; then
    ok "RAM: ${RAM_GIB} GiB"
else
    ko "RAM: ${RAM_GIB} GiB, attesi ~${ATTESA_RAM_GIB} GiB"
fi
fatto "ram_gib=$RAM_GIB"

SWAP_KB="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
SWAP_GIB="$(awk -v k="$SWAP_KB" 'BEGIN{printf "%.2f", k/1048576}')"
# ⚠ Nessuna tolleranza al ribasso qui, e nessun «basta che ci sia».
#   La qualificazione ha girato con 2 GiB perché WSL non ne dava altri, e
#   quella era una DEROGA registrata, non la specifica.
if circa "$SWAP_GIB" "$ATTESO_SWAP_GIB" 5; then
    ok "swap: ${SWAP_GIB} GiB"
    fatto "swap_gib=$SWAP_GIB (conforme)"
else
    ko "swap: ${SWAP_GIB} GiB, attesi ${ATTESO_SWAP_GIB} GiB" \
       "correggere la configurazione della VM. Non si indebolisce la specifica" \
       "perché l'host di qualificazione WSL aveva 2 GiB: quella era una deroga."
    fatto "swap_gib=$SWAP_GIB (NON conforme, atteso $ATTESO_SWAP_GIB)"
fi
if command -v swapon >/dev/null 2>&1; then
    SW="$(swapon --show=NAME,TYPE,SIZE --noheadings 2>/dev/null | tr '\n' ';')"
    printf '        swap attivo: %s\n' "${SW:-nessuno}"
    fatto "swap_dispositivi=${SW:-nessuno}"
fi

if command -v lsblk >/dev/null 2>&1; then
    printf '        dischi:\n'
    lsblk -dno NAME,SIZE,TYPE 2>/dev/null | sed 's/^/          /'
    DISCHI="$(lsblk -dno NAME,SIZE --nodeps 2>/dev/null | tr -s ' ' | tr '\n' ';')"
    fatto "dischi=$DISCHI"
    N_DISCHI="$(lsblk -dno NAME --nodeps 2>/dev/null | grep -c . || echo 0)"
    [ "$N_DISCHI" -ge 2 ] \
        && ok "dischi fisici presenti: $N_DISCHI (sistema + dati)" \
        || ko "un solo disco visibile: i dati di PostgreSQL devono stare su un disco separato"
else
    ko "lsblk non disponibile: non posso misurare i dischi"
fi

# =====================================================================
sez "§3 — disco dei dati e mount"
# =====================================================================
if ! command -v findmnt >/dev/null 2>&1; then
    ko "findmnt non disponibile (util-linux)" "senza findmnt non si distingue un mount da una directory"
else
    INFO="$(findmnt -bno SOURCE,FSTYPE,SIZE,AVAIL --mountpoint "$MOUNT_DATI" 2>/dev/null || true)"
    if [ -z "$INFO" ]; then
        extra="la directory non esiste"
        [ -d "$MOUNT_DATI" ] && extra="la directory ESISTE ma è sul filesystem di root"
        ko "$MOUNT_DATI non è un punto di mount" "$extra"
        fatto "mount_dati=ASSENTE"
    else
        SRC="$(printf '%s' "$INFO" | awk '{print $1}')"
        FST="$(printf '%s' "$INFO" | awk '{print $2}')"
        SZ="$(printf '%s'  "$INFO" | awk '{print $3}')"
        AV="$(printf '%s'  "$INFO" | awk '{print $4}')"
        SZ_GB="$(awk -v b="$SZ" 'BEGIN{printf "%.1f", b/1000000000}')"
        AV_GB="$(awk -v b="$AV" 'BEGIN{printf "%.1f", b/1000000000}')"
        ok "mount reale: $SRC ($FST) ${SZ_GB} GB, liberi ${AV_GB} GB"
        fatto "mount_dati=$SRC fs=$FST size_gb=$SZ_GB avail_gb=$AV_GB"

        # Due segnali indipendenti, come nel preflight: `findmnt` non
        # distinguerebbe un bind del filesystem di root.
        if [ "$(stat -c %d "$MOUNT_DATI")" = "$(stat -c %d /)" ]; then
            ko "$MOUNT_DATI sta sullo STESSO filesystem di /" "è un bind di root, non il secondo disco"
        else
            ok "filesystem dedicato, distinto da /"
        fi
        case "$FST" in
            ext4|xfs) ok "filesystem: $FST" ;;
            *) aperto "filesystem inatteso: $FST" "qualificato su ext4" ;;
        esac
        circa "$SZ_GB" "$ATTESO_DISCO_GB" 12 \
            && ok "dimensione del disco dati: ${SZ_GB} GB" \
            || aperto "disco dati ${SZ_GB} GB, atteso ~${ATTESO_DISCO_GB} GB"

        # fstab: un mount fatto a mano scompare al riavvio.
        if grep -qE "[[:space:]]${MOUNT_DATI}[[:space:]]" /etc/fstab 2>/dev/null; then
            ok "$MOUNT_DATI presente in /etc/fstab (sopravvive al riavvio)"
        else
            ko "$MOUNT_DATI NON è in /etc/fstab" \
               "un mount manuale non c'è più al riavvio, e RequiresMountsFor bloccherebbe l'avvio"
        fi
    fi
fi

# La directory dei dati: si prepara, NON si inizializza.
if [ -d "$DIR_PG" ]; then
    ok "$DIR_PG esiste" "$(stat -c '%u:%g 0%a' "$DIR_PG")"
    if [ -e "$DIR_PG/PG_VERSION" ]; then
        ko "$DIR_PG contiene già un cluster PostgreSQL (PG_VERSION presente)" \
           "la fase 2K-A non inizializza il database. Questo host ha già stato applicativo:" \
           "capire da dove viene PRIMA di distribuire."
        fatto "cluster_preesistente=SI"
    else
        ok "nessun cluster inizializzato (PG_VERSION assente) — corretto per 2K-A"
        fatto "cluster_preesistente=no"
    fi
    VUOTA="$(find "$DIR_PG" -mindepth 1 -maxdepth 1 2>/dev/null | grep -c . || echo 0)"
    fatto "voci_in_pgdata=$VUOTA"
else
    ko "$DIR_PG non esiste" "install -d -m 0700 $DIR_PG e assegnarlo all'uid di postgres dell'immagine"
fi

# =====================================================================
sez "§5 — Docker e Compose, senza avviare niente"
# =====================================================================
if ! command -v docker >/dev/null 2>&1; then
    ko "docker non presente"
else
    DV="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
    CV="$(docker compose version --short 2>/dev/null || echo '?')"
    [ "$DV" != "?" ] && ok "docker $DV, compose $CV" || ko "il daemon Docker non risponde"
    fatto "docker=$DV compose=$CV"

    # Immagini: si confrontano, non si ricalcolano.
    A="$(docker image inspect --format '{{.Id}}' "$TAG_API" 2>/dev/null || echo assente)"
    W="$(docker image inspect --format '{{.Id}}' "$TAG_WEB" 2>/dev/null || echo assente)"
    [ "$A" = "$ID_API" ] && ok "$TAG_API = ${ID_API:7:16}…" \
                         || ko "$TAG_API: $A" "atteso $ID_API"
    [ "$W" = "$ID_WEB" ] && ok "$TAG_WEB = ${ID_WEB:7:16}…" \
                         || ko "$TAG_WEB: $W" "atteso $ID_WEB"
    fatto "immagine_api=$A"
    fatto "immagine_web=$W"

    D="$(docker image inspect --format '{{range .RepoDigests}}{{.}} {{end}}' "postgres@${DIGEST_DB}" 2>/dev/null || true)"
    case "$D" in
        *"$DIGEST_DB"*) ok "postgres@${DIGEST_DB:7:16}… presente per digest" ;;
        *) ko "immagine postgres attesa assente" "docker load -i tsm-images.tar (in rete chiusa non si scarica)" ;;
    esac
    fatto "immagine_db=$DIGEST_DB"

    # Nessun tag mutabile come identità di rilascio.
    if [ -f "$DIR_PROGETTO/compose.yaml" ]; then
        RESA="$(cd "$DIR_PROGETTO" && docker compose config 2>/dev/null || true)"
        if [ -z "$RESA" ]; then
            ko "docker compose config non valido in $DIR_PROGETTO"
        else
            MUT="$(printf '%s\n' "$RESA" | grep -nE '^\s+image:.*(:latest|:[0-9]+\.[0-9]+-alpine|:[0-9]+-alpine)\s*$' || true)"
            [ -z "$MUT" ] && ok "nessun tag mutabile nella configurazione resa" \
                          || ko "tag mutabile nella configurazione resa" "$MUT"
        fi
    fi
fi

# =====================================================================
sez "§4 — SELinux"
# =====================================================================
SEL="assente"
if command -v getenforce >/dev/null 2>&1; then SEL="$(getenforce 2>/dev/null || echo '?')"; fi
fatto "selinux=$SEL"
if [ "$SEL" = "Enforcing" ]; then
    ok "SELinux: Enforcing"
    CTX="$(ls -Zd "$DIR_PG" 2>/dev/null | awk '{print $1}')"
    TIPO="$(printf '%s' "$CTX" | awk -F: '{print $3}')"
    fatto "contesto_pgdata=$CTX"
    case "$TIPO" in
        container_file_t|svirt_sandbox_file_t) ok "contesto di $DIR_PG: $TIPO" ;;
        *) ko "contesto SELinux inadeguato su $DIR_PG: $CTX" "il preflight uscirebbe con 27" ;;
    esac
    if command -v semanage >/dev/null 2>&1; then
        REGOLA="$(semanage fcontext -l 2>/dev/null | grep "^${DIR_PG}" || true)"
        [ -n "$REGOLA" ] \
            && ok "regola semanage persistente presente" \
            || ko "etichetta non persistente: manca la regola semanage" \
                  "un chcon si perde alla prima rietichettatura; il preflight uscirebbe con 28"
    else
        ko "semanage non disponibile: la persistenza dell'etichetta non è verificabile"
    fi
elif [ "$SENZA_SELINUX" = 1 ]; then
    ok "SELinux: $SEL — non in uso per decisione dichiarata (--senza-selinux)" \
       "i codici 27/28 del preflight non si applicano: il preflight salta i controlli di contesto"
    fatto "selinux_decisione=non in uso, dichiarato con --senza-selinux"
else
    # ⚠ Qui sta la differenza con la fase 2J. Là questo era un salto
    #   legittimo (WSL non fa girare SELinux enforcing) e i codici 27 e 28
    #   sono rimasti NON esercitati. Sull'host reale «non enforcing» è un
    #   requisito mancato, non un salto.
    ko "SELinux: $SEL, richiesto Enforcing" \
       "§4 richiede enforcing sull'host reale. NON si disabilita SELinux per far funzionare TSM." \
       "i codici 27 e 28 del preflight restano non esercitati finché non lo è."
fi

# ---- esercizio dei percorsi di guasto 27 e 28 -----------------------
if [ "$ESERCITA_SELINUX" = 1 ]; then
    if [ "$SEL" != "Enforcing" ]; then
        ko "--selinux-gate richiesto ma SELinux non è Enforcing: niente da esercitare"
    elif [ ! -x "$DIR_PROGETTO/deploy/preflight.sh" ]; then
        ko "--selinux-gate: preflight non trovato in $DIR_PROGETTO/deploy"
    else
        printf '        esercizio dei codici 27 e 28 (reversibile)\n'
        CTX0="$(ls -Zd "$DIR_PG" | awk '{print $1}')"
        REG0="$(semanage fcontext -l 2>/dev/null | grep -c "^${DIR_PG}" || echo 0)"
        RIPRISTINA() {
            restorecon -R "$DIR_PG" 2>/dev/null || chcon "$CTX0" "$DIR_PG" 2>/dev/null || true
        }
        trap RIPRISTINA EXIT

        # 27: etichetta sbagliata → il preflight deve RIFIUTARE con 27.
        chcon -t user_home_t "$DIR_PG" 2>/dev/null
        "$DIR_PROGETTO/deploy/preflight.sh" --dir "$DIR_PROGETTO" >/dev/null 2>&1
        C=$?
        [ "$C" = 27 ] && ok "etichetta errata → preflight esce 27" \
                      || ko "etichetta errata → preflight esce $C, atteso 27"

        # ripristino, e la VERIFICA che sia avvenuto. Un ripristino dato
        # per fatto è il difetto trovato nel backup della fase 2J.
        RIPRISTINA
        CTX1="$(ls -Zd "$DIR_PG" | awk '{print $1}')"
        if [ "$CTX1" = "$CTX0" ]; then
            ok "contesto ripristinato e CONFRONTATO con l'originale"
        else
            ko "contesto NON ripristinato: era $CTX0, ora $CTX1" \
               "rimediare a mano prima di distribuire: restorecon -Rv $DIR_PG"
        fi
        "$DIR_PROGETTO/deploy/preflight.sh" --dir "$DIR_PROGETTO" >/dev/null 2>&1
        C=$?
        [ "$C" != 27 ] && ok "dopo il ripristino il preflight non lamenta più il contesto (uscita $C)" \
                       || ko "il preflight esce ancora 27 dopo il ripristino"

        # 28: regola persistente rimossa → 28. Solo se ce n'era una da
        # rimettere: senza, non si tocca la policy di una macchina reale.
        if [ "$REG0" -gt 0 ]; then
            semanage fcontext -d "${DIR_PG}(/.*)?" >/dev/null 2>&1
            chcon -t container_file_t "$DIR_PG" 2>/dev/null
            "$DIR_PROGETTO/deploy/preflight.sh" --dir "$DIR_PROGETTO" >/dev/null 2>&1
            C=$?
            [ "$C" = 28 ] && ok "etichetta corretta ma non persistente → preflight esce 28" \
                          || ko "regola rimossa → preflight esce $C, atteso 28"
            semanage fcontext -a -t container_file_t "${DIR_PG}(/.*)?" >/dev/null 2>&1
            restorecon -R "$DIR_PG" >/dev/null 2>&1
            REG1="$(semanage fcontext -l 2>/dev/null | grep -c "^${DIR_PG}" || echo 0)"
            [ "$REG1" -gt 0 ] && ok "regola semanage rimessa e verificata" \
                              || ko "regola semanage NON rimessa: rimediare a mano"
        else
            aperto "codice 28 non esercitato: non c'era una regola semanage da rimuovere" \
                   "crearla prima (semanage fcontext -a …), poi rieseguire --selinux-gate"
        fi
        trap - EXIT
    fi
elif [ "$SENZA_SELINUX" = 1 ]; then
    fatto "selinux_27_28=non applicabili (SELinux non in uso)"
else
    aperto "percorsi di guasto SELinux 27/28 non esercitati" "richiede --selinux-gate sull'host reale"
fi

# =====================================================================
sez "§6 — artefatti trasferiti"
# =====================================================================
if [ ! -d "$DIR_PROGETTO" ]; then
    ko "$DIR_PROGETTO non esiste: gli artefatti non sono stati trasferiti"
else
    B="$DIR_PROGETTO/deploy/baseline/production-baseline.json"
    if [ -f "$B" ]; then
        # Digest dei BYTE con fine-riga LF, come nel commit. Un digest sul
        # file così com'è su disco identifica il checkout, non il rilascio
        # — è la lezione CRLF della fase 2J.
        SHA="$(tr -d '\r' < "$B" | sha256sum | awk '{print $1}')"
        [ "$SHA" = "$BASELINE_FILE" ] \
            && ok "baseline: il file qualificato, ${SHA:0:16}…" \
            || ko "baseline: $SHA" "atteso $BASELINE_FILE — NON è l'artefatto qualificato"
        fatto "baseline_file_sha=$SHA"
        fatto "baseline_documento_sha=$BASELINE_DOC"
        printf '        digest del DOCUMENTO %s… : lo verifica il prodotto in --check/--init,\n' "${BASELINE_DOC:0:12}"
        printf '        non calcolabile da una shell (serve la canonicalizzazione del prodotto).\n'
        # I \r non si vedono nel digest LF qui sopra, ma dentro il container
        # il documento verrebbe letto CON i \r e il digest del DOCUMENTO non
        # tornerebbe: il gate della baseline fallirebbe all'installazione.
        # ⚠ NON con `grep -q $'\r'`: alcune grep (MSYS, e le implementazioni
        #   che aprono il file in modalità testo) considerano \r\n il
        #   terminatore di riga, quindi nessuna riga «contiene» un CR e il
        #   controllo passa sempre. Verificato: su un file CRLF quel grep
        #   non trovava niente. Si contano i BYTE, che nessuna
        #   interpretazione dei fine-riga può alterare.
        N_CON="$(wc -c < "$B" | tr -d ' ')"
        N_SENZA="$(tr -d '\r' < "$B" | wc -c | tr -d ' ')"
        if [ "$N_CON" != "$N_SENZA" ]; then
            ko "la baseline sull'host contiene CR (fine-riga Windows)" \
               "trasferita da una copia di lavoro Windows invece che da git:" \
               "usare tools/release-bundle.sh, che estrae i blob dal commit"
        else
            ok "baseline senza CR: fine-riga come nel commit"
        fi
        SV="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8')).get('schemaVersion'))" "$B" 2>/dev/null || echo '?')"
        [ "$SV" = "$SCHEMA_VERSION" ] && ok "schemaVersion: $SV" || ko "schemaVersion: $SV, atteso $SCHEMA_VERSION"
    else
        ko "baseline assente in $B"
        fatto "baseline_file_sha=ASSENTE"
    fi

    for f in deploy/preflight.sh deploy/tsm.service compose.yaml; do
        [ -f "$DIR_PROGETTO/$f" ] && ok "presente: $f" || ko "assente: $f"
    done

    TROVATI=0
    for v in "${VIETATI[@]}"; do
        if [ -e "$DIR_PROGETTO/$v" ]; then
            ko "artefatto VIETATO presente: $v" "§6 non lo ammette in produzione"
            TROVATI=$((TROVATI+1))
        fi
    done
    [ "$TROVATI" = 0 ] && ok "nessuno dei ${#VIETATI[@]} artefatti vietati è presente"
    fatto "artefatti_vietati=$TROVATI"

    # §15 — nessuna deriva. Un file modificato sull'host non è il rilascio.
    if [ -d "$DIR_PROGETTO/.git" ]; then
        ko "$DIR_PROGETTO contiene un repository git" \
           "l'host reale non è un ambiente di sviluppo: si trasferiscono artefatti, non un checkout"
    else
        ok "nessun repository git in $DIR_PROGETTO"
    fi
fi
fatto "commit_qualificato=$COMMIT_QUALIFICATO"
fatto "albero_rilascio=$ALBERO_RILASCIO"
fatto "migrazione=$MIGRAZIONE"
fatto "mapper=$MAPPER"

# =====================================================================
sez "§7 — certificato TLS aziendale"
# =====================================================================
CERT="$DIR_PROGETTO/secrets/tls/fullchain.pem"
KEY="$DIR_PROGETTO/secrets/tls/privkey.pem"
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    ko "materiale TLS assente (${CERT##*/} / ${KEY##*/})" "il preflight uscirebbe con 31"
    fatto "tls=ASSENTE"
elif ! command -v openssl >/dev/null 2>&1; then
    ko "openssl non disponibile: non posso verificare il certificato"
else
    # La coppia si verifica confrontando la chiave PUBBLICA derivata dalle
    # due parti. Il materiale privato non viene né stampato né copiato.
    P1="$(openssl x509 -in "$CERT" -noout -pubkey 2>/dev/null | sha256sum | awk '{print $1}')"
    P2="$(openssl pkey -in "$KEY" -pubout 2>/dev/null | sha256sum | awk '{print $1}')"
    if [ -n "$P1" ] && [ "$P1" = "$P2" ]; then
        ok "certificato e chiave sono la stessa coppia"
    else
        ko "certificato e chiave NON corrispondono" "nginx partirebbe e servirebbe un handshake rotto"
    fi
    SUB="$(openssl x509 -in "$CERT" -noout -subject 2>/dev/null | sed 's/^subject= *//')"
    ISS="$(openssl x509 -in "$CERT" -noout -issuer  2>/dev/null | sed 's/^issuer= *//')"
    FIN="$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null | cut -d= -f2)"
    SAN="$(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' ')"
    printf '        soggetto: %s\n        emittente: %s\n        SAN: %s\n        scade: %s\n' \
           "$SUB" "$ISS" "${SAN:-nessuno}" "$FIN"
    fatto "tls_soggetto=$SUB"
    fatto "tls_emittente=$ISS"
    fatto "tls_san=${SAN:-nessuno}"
    fatto "tls_scadenza=$FIN"

    openssl x509 -in "$CERT" -noout -checkend 2592000 >/dev/null 2>&1 \
        && ok "validità: oltre 30 giorni" \
        || ko "il certificato scade entro 30 giorni (o è già scaduto): $FIN"

    # Autofirmato: ammesso solo se è la politica aziendale a chiederlo.
    if [ "$SUB" = "$ISS" ]; then
        aperto "certificato AUTOFIRMATO (soggetto = emittente)" \
               "§7 non ammette un autofirmato per far diventare verde il cancello:" \
               "serve conferma esplicita che sia il certificato previsto dalla politica aziendale"
    else
        ok "emesso da una CA distinta dal soggetto"
    fi
    case "$SAN" in
        *"$ATTESO_HOSTNAME"*) ok "il SAN contiene $ATTESO_HOSTNAME" ;;
        *) aperto "il SAN non contiene $ATTESO_HOSTNAME" "verificare il nome con cui gli operatori raggiungeranno TSM" ;;
    esac
    M="$(stat -c %a "$KEY" 2>/dev/null)"
    case "$M" in
        400|440|600|640) ok "permessi della chiave privata: 0$M" ;;
        *) ko "permessi della chiave privata troppo larghi: 0$M" "chmod 0640 $KEY" ;;
    esac
fi

# =====================================================================
sez "§8 — segreti di produzione"
# =====================================================================
# I nomi, i permessi e il proprietario attesi NON si riscrivono qui: li
# impone già il preflight (SECRETS_REQUIRED, SECRETS_OPTIONAL_EMPTY,
# codice 30) e due elenchi separati divergerebbero. Si leggono da lui.
SDIR="$DIR_PROGETTO/secrets"
PF="$DIR_PROGETTO/deploy/preflight.sh"
ATTESI_SEGRETI=()
if [ -f "$PF" ]; then
    for v in SECRETS_REQUIRED SECRETS_OPTIONAL_EMPTY; do
        L="$(sed -n "s/^${v}=\"\\(.*\\)\"$/\\1/p" "$PF" | head -1)"
        for s in $L; do ATTESI_SEGRETI+=("$s"); done
    done
fi
if [ "${#ATTESI_SEGRETI[@]}" -eq 0 ]; then
    ko "non ho potuto leggere l'elenco dei segreti dal preflight" \
       "senza l'elenco autorevole non invento il mio: sarebbe un cancello su nomi sbagliati"
    ATTESI_SEGRETI=()
else
    ok "elenco dei segreti letto dal preflight: ${ATTESI_SEGRETI[*]}"
fi
# smtp_password può essere vuoto per progetto: un relay interno senza
# autenticazione è un caso previsto, non un errore.
VUOTO_AMMESSO="$(sed -n 's/^SECRETS_OPTIONAL_EMPTY="\(.*\)"$/\1/p' "$PF" 2>/dev/null | head -1)"
if [ ! -d "$SDIR" ]; then
    ko "$SDIR non esiste: i segreti non sono stati preparati"
else
    for s in "${ATTESI_SEGRETI[@]}"; do
        f="$SDIR/$s"
        if [ ! -f "$f" ]; then
            ko "segreto assente: $s" "il preflight uscirebbe con 30"
            continue
        fi
        M="$(stat -c %a "$f")"; O="$(stat -c '%u (%U)' "$f")"
        N="$(wc -c < "$f" | tr -d ' ')"
        # 0400 o 0600, come impone il preflight: con un bit per il gruppo
        # il file resta leggibile a chiunque sia in quel gruppo.
        case "$M" in
            400|600) ok "$s: 0$M, proprietario $O, $N byte" ;;
            *) ko "$s: permessi 0$M non ammessi (attesi 0400 o 0600)" "chmod 0400 $f" ;;
        esac
        # La lunghezza si misura, il contenuto non si stampa mai.
        case " $VUOTO_AMMESSO " in
            *" $s "*) [ "$N" -eq 0 ] && printf '        %s vuoto: ammesso (relay senza autenticazione)\n' "$s" ;;
            *) [ "$N" -ge 20 ] || ko "$s è lungo $N byte: troppo corto per un segreto generato" ;;
        esac
        # Un segreto con newline finale diventa un carattere in più nella
        # password: il container lo legge alla lettera.
        [ "$(tail -c1 "$f" | wc -l | tr -d ' ')" = 0 ] \
            || ko "$s termina con un newline" "printf '%s' invece di echo"
    done

    # §8 — la password del primo amministratore NON si sceglie.
    if [ -n "${TSM_BOOTSTRAP_PASSWORD:-}" ]; then
        ko "TSM_BOOTSTRAP_PASSWORD è impostata nell'ambiente" \
           "l'inizializzatore della baseline la RIFIUTA di proposito: genera la password da sé"
    else
        ok "TSM_BOOTSTRAP_PASSWORD non impostata"
    fi
    if grep -rqs "TSM_BOOTSTRAP_PASSWORD" "$DIR_PROGETTO/compose.yaml" "$SDIR" 2>/dev/null; then
        ko "TSM_BOOTSTRAP_PASSWORD compare negli artefatti sull'host"
    else
        ok "TSM_BOOTSTRAP_PASSWORD non compare negli artefatti"
    fi
    fatto "segreti_preparati=${#ATTESI_SEGRETI[@]}"

    # Fuga di segreti: si cerca la PRESENZA dei valori, senza stamparli.
    # Con una contro-prova, perché una ricerca che non trova niente e una
    # ricerca rotta hanno lo stesso aspetto.
    FUGHE=0; CONTRO=0; CANDIDATI=0
    for s in "${ATTESI_SEGRETI[@]}"; do
        [ -f "$SDIR/$s" ] || continue
        V="$(cat "$SDIR/$s")"
        [ ${#V} -lt 12 ] && continue
        CANDIDATI=$((CANDIDATI+1))
        for dove in "$DIR_PROGETTO/compose.yaml" "$DIR_PROGETTO/deploy" /etc/systemd/system/tsm.service; do
            [ -e "$dove" ] || continue
            grep -rqsF -- "$V" "$dove" 2>/dev/null && FUGHE=$((FUGHE+1))
        done
        # contro-prova: lo stesso strumento DEVE trovare il valore là dove
        # sta per costruzione. Se non lo trova, la ricerca è rotta.
        grep -qsF -- "$V" "$SDIR/$s" && CONTRO=$((CONTRO+1))
    done
    # Tre esiti, non due. «Non c'era niente da cercare» e «la ricerca è
    # rotta» producono entrambi zero occorrenze: confonderli significa non
    # poter sapere, sull'host reale, quale dei due è accaduto — e la
    # differenza è fra un cancello non ancora pronto e uno strumento che
    # mente.
    if [ "$CANDIDATI" -eq 0 ]; then
        aperto "spazzata dei segreti VACUA: nessun segreto abbastanza lungo da cercare" \
               "non dice che non ci sono fughe: dice che non c'era niente da cercare"
    elif [ "$CONTRO" -eq 0 ]; then
        ko "contro-prova FALLITA: la ricerca non ritrova nemmeno i $CANDIDATI valori presenti" \
           "il risultato «nessuna fuga» non vale niente: lo strumento è rotto"
    elif [ "$FUGHE" = 0 ]; then
        ok "nessuna fuga fra $CANDIDATI segreti, contro-prova superata ($CONTRO ritrovati)"
    else
        ko "$FUGHE occorrenze di segreti fuori dai file dei segreti"
    fi
fi

# =====================================================================
sez "§3b — il preflight qualificato, eseguito per davvero"
# =====================================================================
# §3 chiede di esercitare il preflight «where possible without creating
# application state». Il preflight scrive un file di prova attraverso
# Docker con l'identità di PostgreSQL e lo rimuove: è una sonda, non un
# cluster. Non esegue migrazioni e non crea né inventario né utenti.
#
# Si esegue il preflight VERO e si legge il suo codice di uscita, invece
# di riscrivere i suoi controlli qui: due copie delle stesse regole
# divergono, e la copia che sbaglia è sempre quella che non gira in
# produzione.
if [ ! -x "$PF" ]; then
    ko "preflight non eseguibile: $PF"
    fatto "preflight=assente"
else
    OUT="$("$PF" --dir "$DIR_PROGETTO" 2>&1)"; C=$?
    fatto "preflight_uscita=$C"
    case "$C" in
        0)  ok "preflight: uscita 0, l'host soddisfa tutti i prerequisiti" ;;
        20) ko "preflight 20: $MOUNT_DATI non è un punto di mount" ;;
        21) ko "preflight 21: $MOUNT_DATI è sullo stesso filesystem di /" ;;
        22|23|24|25) ko "preflight $C: directory dei dati inadeguata (esistenza/proprietario/permessi/scrittura)" ;;
        26) ko "preflight 26: spazio libero insufficiente" ;;
        27) ko "preflight 27: contesto SELinux inadeguato" ;;
        28) ko "preflight 28: etichetta SELinux non persistente" ;;
        30) ko "preflight 30: file di secret mancante o con permessi/proprietario inadeguati" ;;
        31) ko "preflight 31: materiale TLS mancante o non leggibile da nginx" ;;
        40) ko "preflight 40: deroga di sviluppo attiva (cookie non Secure o TSM_ENV ≠ production)" ;;
        41) ko "preflight 41: una porta che in produzione non va pubblicata risulta pubblicata" ;;
        42) ko "preflight 42: la configurazione resa non è quella di produzione" ;;
        10|11|12) ko "preflight $C: Docker, configurazione Compose o immagini" ;;
        *)  ko "preflight: uscita $C non prevista" ;;
    esac
    if [ "$C" != 0 ]; then
        printf '        ultime righe del preflight:\n'
        printf '%s\n' "$OUT" | tail -6 | sed 's/^/          /'
    fi
    # Il preflight non è un semaforo unico: si registra quanti dei suoi
    # controlli sono passati, perché «uscita 30» al terzo controllo dice
    # molto meno di «uscita 30 dopo trenta controlli passati».
    # `[ok]`, come lo stampa davvero il preflight (QUIET=0 di default).
    # Cercare «OK» dava sempre 0: una misura che legge zero per
    # costruzione ha l'aspetto di un'informazione e non lo è.
    N_OK="$(printf '%s\n' "$OUT" | grep -c '\[ok\]' || true)"
    printf '        controlli superati dal preflight: %s\n' "$N_OK"
    fatto "preflight_controlli_ok=$N_OK"
fi

# =====================================================================
sez "§9 — esposizione di rete"
# =====================================================================
if [ -f "$DIR_PROGETTO/compose.yaml" ] && command -v docker >/dev/null 2>&1; then
    PORTE="$(cd "$DIR_PROGETTO" && docker compose config --format json 2>/dev/null \
             | python3 -c '
import json,sys
try: c=json.load(sys.stdin)
except Exception: sys.exit(0)
for n,s in (c.get("services") or {}).items():
    for p in s.get("ports") or []:
        if isinstance(p,dict): print(n, p.get("published"), p.get("target"))
        else: print(n, p)
' 2>/dev/null || true)"
    if [ -z "$PORTE" ]; then
        ko "non ho potuto leggere le porte dalla configurazione resa"
    else
        printf '        porte pubblicate dalla configurazione:\n'
        printf '%s\n' "$PORTE" | sed 's/^/          /'
        PUB="$(printf '%s\n' "$PORTE" | awk '{print $2}' | sort -u | tr '\n' ' ')"
        ATTESE=" 80 443 "
        SPURIE=""
        for p in $PUB; do
            case "$ATTESE" in *" $p "*) : ;; *) SPURIE="$SPURIE $p" ;; esac
        done
        [ -z "$SPURIE" ] && ok "solo 80 e 443 pubblicate" \
                         || ko "porte pubblicate non previste:$SPURIE" "il preflight uscirebbe con 41"
        fatto "porte_pubblicate=$PUB"
        case " $PUB " in
            *" 5432 "*) ko "PostgreSQL è esposto sull'host" ;;
            *) ok "PostgreSQL non esposto" ;;
        esac
    fi
fi
if command -v ss >/dev/null 2>&1; then
    ASC="$(ss -lntH 2>/dev/null | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' ')"
    printf '        porte in ascolto ORA sull'\''host: %s\n' "${ASC:-nessuna}"
    fatto "in_ascolto_prima_del_deploy=${ASC:-nessuna}"
    case " $ASC " in
        *" 1025 "*|*" 2525 "*) ko "un ascoltatore SMTP di prova è attivo sull'host" ;;
        *) ok "nessun ascoltatore SMTP di prova" ;;
    esac
    # 80/443 non devono essere occupate da altro: lo stack non è avviato.
    case " $ASC " in
        *" 80 "*|*" 443 "*) aperto "80 o 443 già in ascolto mentre lo stack è fermo" "verificare chi le tiene" ;;
        *) ok "80 e 443 libere (lo stack non è avviato, come richiesto)" ;;
    esac
fi
if command -v firewall-cmd >/dev/null 2>&1; then
    FW="$(firewall-cmd --list-services 2>/dev/null | tr '\n' ' ')$(firewall-cmd --list-ports 2>/dev/null | tr '\n' ' ')"
    printf '        firewalld: %s\n' "${FW:-?}"; fatto "firewall=firewalld: ${FW:-?}"
elif command -v nft >/dev/null 2>&1; then
    N="$(nft list ruleset 2>/dev/null | grep -c . || echo 0)"; fatto "firewall=nftables, $N righe di ruleset"
else
    fatto "firewall=non rilevato"
fi

# =====================================================================
sez "§12 — systemd, installato ma fermo e disabilitato"
# =====================================================================
U=/etc/systemd/system/tsm.service
if [ ! -f "$U" ]; then
    ko "$U non installato"
    fatto "systemd=non installato"
else
    ok "unità installata"
    S1="$(tr -d '\r' < "$U" | sha256sum | awk '{print $1}')"
    S2="?"
    [ -f "$DIR_PROGETTO/deploy/tsm.service" ] && S2="$(tr -d '\r' < "$DIR_PROGETTO/deploy/tsm.service" | sha256sum | awk '{print $1}')"
    [ "$S1" = "$S2" ] && ok "l'unità installata è identica a quella del rilascio" \
                      || ko "l'unità installata DIFFERISCE da quella del rilascio" "installata $S1 / rilascio $S2"
    grep -q '^RequiresMountsFor=/srv/tsm-data$' "$U" \
        && ok "RequiresMountsFor=/srv/tsm-data presente nell'unità installata" \
        || ko "RequiresMountsFor=/srv/tsm-data ASSENTE" \
              "senza, all'avvio si vince una corsa: initdb sul disco di root, nessun errore"
    grep -q 'ExecStartPre=.*preflight.sh' "$U" \
        && ok "ExecStartPre esegue il preflight" \
        || ko "ExecStartPre non esegue il preflight"
    ATT="$(systemctl is-active tsm.service 2>/dev/null || true)"
    ENA="$(systemctl is-enabled tsm.service 2>/dev/null || true)"
    [ "$ATT" != "active" ] && ok "tsm.service non attivo ($ATT)" \
                           || ko "tsm.service è ATTIVO: la fase 2K-A non avvia l'applicazione"
    [ "$ENA" = "disabled" ] && ok "tsm.service disabilitato" \
                            || ko "tsm.service è '$ENA': deve restare disabled fino all'approvazione"
    fatto "systemd=installato, $ATT/$ENA"
    if command -v systemd-analyze >/dev/null 2>&1; then
        V="$(systemd-analyze verify "$U" 2>&1 || true)"
        # systemd-analyze si lamenta anche di cose innocue: si registra
        # tutto e si segnala, non si dichiara verde per silenzio.
        if [ -z "$V" ]; then
            ok "systemd-analyze verify: nessun rilievo"
        else
            aperto "systemd-analyze verify ha rilievi" "$(printf '%s' "$V" | head -3 | tr '\n' ' ')"
        fi
        fatto "systemd_analyze=$([ -z "$V" ] && echo pulito || echo 'con rilievi')"
    fi
    # Il mount come unità: è quello che RequiresMountsFor renderà necessario.
    MU="$(systemctl show -p Id --value 'srv-tsm\x2ddata.mount' 2>/dev/null || true)"
    [ -n "$MU" ] && ok "unità di mount vista da systemd: $MU" \
                 || aperto "systemd non vede srv-tsm\\x2ddata.mount" "verificare fstab e systemctl daemon-reload"
fi

# =====================================================================
sez "§13 — baseline: solo ciò che non crea stato"
# =====================================================================
# `--check` esiste ed è non distruttivo, ma interroga il database
# (gate_schema, conteggi delle tabelle). Per eseguirlo servirebbe un
# cluster avviato, e avviarlo significa initdb sul disco dei dati: cioè
# esattamente lo stato che §13 vieta. Quindi non lo si esegue, e non si
# finge di averlo eseguito. Le sue verifiche SENZA database sono già
# state fatte qui sopra: digest canonico della baseline (§6),
# TSM_BOOTSTRAP_PASSWORD assente (§8), TSM_ENV di produzione (preflight).
aperto "baseline --check non eseguito: richiede un cluster avviato" \
       "initdb sul disco dei dati è lo stato che §13 vieta. Rinviato alla distribuzione controllata."
aperto "--allow-initialise NON eseguito, come richiesto" \
       "nessuna versione di inventario, nessun amministratore, nessuna cronologia di audit creati"

# =====================================================================
sez "esito"
# =====================================================================
printf '  OK %d · KO %d · APERTI %d\n' "$OK" "$KO" "$APERTI"

if [ -n "$MANIFESTO" ]; then
    {
        printf '# Manifesto di accettazione pre-installazione — TSM\n\n'
        printf 'Prodotto da tools/preinstall-acceptance.sh. NESSUN SEGRETO: nessuna\n'
        printf 'password, nessun hash, nessun materiale privato di chiave.\n\n'
        printf '| voce | valore |\n|---|---|\n'
        for f in "${FATTI[@]}"; do
            printf '| %s | `%s` |\n' "${f%%=*}" "${f#*=}"
        done
        printf '\n## Esiti\n\n| stato | voce | osservato |\n|---|---|---|\n'
        for r in "${RIGHE[@]}"; do
            IFS='|' read -r st vo os <<< "$r"
            printf '| %s | %s | %s |\n' "$st" "$vo" "${os:-—}"
        done
        printf '\n## Cancelli non tecnici\n\n'
        printf -- '- conferma umana «Rack R09 — Test»: da registrare in deploy/HUMAN-STRUCTURAL-REVIEW.md\n'
        printf -- '- conferma umana ordine file Saletta Nuova: idem\n'
        printf -- '- VEEAM-A: politica di backup preparata per la VM\n'
        printf -- '- VEEAM-B: primo backup e ripristino reali, DOPO l installazione\n'
        printf -- '- 14B: accettazione SMTP reale, DOPO l installazione\n'
        printf '\n## Esito\n\n%s\n' "$([ "$KO" -eq 0 ] && echo 'B — PRONTO PER LA DISTRIBUZIONE CONTROLLATA' || echo 'A — BLOCCATO')"
    } > "$MANIFESTO"
    printf '  manifesto: %s\n' "$MANIFESTO"
fi

if [ "$KO" -eq 0 ]; then
    printf '\n  \033[32mB — PRONTO PER LA DISTRIBUZIONE CONTROLLATA\033[0m\n'
    printf '  restano aperti %d cancelli operativi post-installazione (14B, VEEAM-B).\n' "$APERTI"
    exit 0
fi
printf '\n  \033[31mA — BLOCCATO\033[0m: %d requisiti pre-installazione non soddisfatti.\n' "$KO"
exit 1
