#!/usr/bin/env bash
#
# Test portante dell'archiviazione (§8.40), su un filesystem VERO.
#
# Non simula il mount: crea un'immagine, la formatta, la monta su /srv/tsm-data,
# fa partire lo stack di produzione e guarda dove finiscono i byte. Poi smonta e
# ripete, perché la domanda che conta è cosa accade QUANDO IL DISCO NON C'È.
#
# La controprova è la parte importante: si dimostra prima che senza preflight
# PostgreSQL inizializza davvero sul filesystem di root — nessun errore, dati sul
# disco sbagliato — e solo dopo che il preflight rifiuta esattamente quel caso.
# Un test che verifica solo il rifiuto non dice se il pericolo era reale.
#
# Gira come root DENTRO l'host del demone Docker: là dove /srv/tsm-data è un
# percorso vero e dove i bind mount dei container si risolvono. Su Windows lo
# lancia tools/run-storage-e2e-test.ps1 attraverso nsenter.
#
# Uso:  storage-e2e-test.sh --repo <percorso del repository>
#
set -uo pipefail

REPO=""
APP=/opt/tsm
DATA=/srv/tsm-data
PGDIR="$DATA/postgres"
IMGFILE=/var/tmp/tsm-data.img
IMGSIZE_GB=7          # > 5 GiB, altrimenti il preflight rifiuta per spazio
PROJ=tsmstorage

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) REPO="${2:-}"; shift 2 ;;
        *) echo "argomento non riconosciuto: $1" >&2; exit 2 ;;
    esac
done
[ -n "$REPO" ] && [ -f "$REPO/compose.yaml" ] || {
    echo "serve --repo <percorso con compose.yaml>" >&2; exit 2; }

# Il demone deve rispondere PRIMA di cominciare: senza, ogni controllo
# fallirebbe e sembrerebbe un difetto dell'archiviazione invece di un problema di
# ambiente. Vedi la nota su DOCKER_HOST in run-storage-e2e-test.ps1.
docker info >/dev/null 2>&1 || {
    echo "il demone Docker non risponde da qui (DOCKER_HOST=${DOCKER_HOST:-<vuoto>})" >&2
    exit 2
}

PASS=0; FAIL=0
check() {
    if [ "$2" = "1" ] || [ "$2" = "true" ]; then
        printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1))
    else
        printf '  [FAIL] %s\n' "$1"; [ -n "${3:-}" ] && printf '         -> %s\n' "$3"
        FAIL=$((FAIL+1))
    fi
}
step() { printf '\n== %s ==\n' "$1"; }
yn() { if [ "$1" -eq 0 ]; then echo 1; else echo 0; fi; }

compose() { docker compose -p "$PROJ" --project-directory "$APP" -f "$APP/compose.yaml" "$@"; }

cleanup() {
    step "pulizia"
    compose down -v >/dev/null 2>&1
    umount "$DATA" >/dev/null 2>&1
    # NIENTE `losetup -D`: stacca TUTTI i loop device della macchina, compresi
    # quelli di altri servizi. `mount -o loop` imposta autoclear, quindi il
    # device si libera con lo smontaggio.
    rm -f "$IMGFILE"
    rm -rf "$DATA"
    rm -rf "$APP"
    printf '  fatto\n'
}
trap cleanup EXIT

# ==================================================================
step "0. preparazione: copia del repository su un filesystem Linux"
# ==================================================================
# Si copia in /opt/tsm invece di usare il repository montato da Windows: i file
# su quel mount hanno proprietario e permessi imposti dal mount, e i controlli su
# secret e chiave privata non avrebbero alcun significato. /opt/tsm è anche il
# percorso di produzione.
rm -rf "$APP"; mkdir -p "$APP"
tar -C "$REPO" --exclude=.git --exclude='.*-tmp' -cf - . 2>/dev/null | tar -C "$APP" -xf - 2>/dev/null
[ -f "$APP/compose.yaml" ] || { echo "copia del repository fallita" >&2; exit 1; }
chmod 0750 "$APP/deploy/preflight.sh"

DB_IMAGE=$(docker compose --project-directory "$APP" -f "$APP/compose.yaml" config --images 2>/dev/null | grep -i postgres | head -1)
API_IMAGE=$(docker compose --project-directory "$APP" -f "$APP/compose.yaml" config --images 2>/dev/null | grep -i 'tsm-api' | head -1)
WEB_IMAGE=$(docker compose --project-directory "$APP" -f "$APP/compose.yaml" config --images 2>/dev/null | grep -i 'tsm-web' | head -1)
printf '  immagini: %s | %s | %s\n' "$DB_IMAGE" "$API_IMAGE" "$WEB_IMAGE"

PG_UID=$(docker run --rm --entrypoint id "$DB_IMAGE" -u postgres 2>/dev/null | tr -d '\r\n')
PG_GID=$(docker run --rm --entrypoint id "$DB_IMAGE" -g postgres 2>/dev/null | tr -d '\r\n')
API_UID=$(docker image inspect --format '{{.Config.User}}' "$API_IMAGE" 2>/dev/null | cut -d: -f1)
WEB_UID=$(docker image inspect --format '{{.Config.User}}' "$WEB_IMAGE" 2>/dev/null | cut -d: -f1)
check "uid/gid di postgres letti dall'immagine pinnata" \
      "$([ -n "$PG_UID" ] && [ -n "$PG_GID" ] && echo 1 || echo 0)" "$PG_UID:$PG_GID"
printf '  postgres=%s:%s api=%s web=%s\n' "$PG_UID" "$PG_GID" "$API_UID" "$WEB_UID"

# secret e TLS con proprietario/permessi da runbook
mkdir -p "$APP/secrets/tls"
# `worker_db_password` c'è dalla migrazione 0009: il worker ha un ruolo di database
# proprio, perché la GC delle foto ha bisogno di DELETE su `photos` e l'API non
# deve averlo (§8.5). Il preflight lo pretende, quindi senza questo file il test
# fallirebbe sul preflight — per un motivo che con l'archiviazione non c'entra.
for s in postgres_password api_db_password worker_db_password; do
    [ -s "$APP/secrets/$s" ] || head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$APP/secrets/$s"
done
: > "$APP/secrets/smtp_password"
SECRET_FILES="$APP/secrets/postgres_password $APP/secrets/api_db_password $APP/secrets/worker_db_password $APP/secrets/smtp_password"
chown "$API_UID:$API_UID" $SECRET_FILES
chmod 0400 $SECRET_FILES
if [ ! -f "$APP/secrets/tls/privkey.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=tsm-prd-01 \
        -keyout "$APP/secrets/tls/privkey.pem" -out "$APP/secrets/tls/fullchain.pem" >/dev/null 2>&1
fi
chown "$WEB_UID:$WEB_UID" "$APP/secrets/tls/privkey.pem"
chmod 0400 "$APP/secrets/tls/privkey.pem"
chmod 0444 "$APP/secrets/tls/fullchain.pem"

# ==================================================================
step "1. disco 2 finto ma vero: immagine, filesystem, mount"
# ==================================================================
umount "$DATA" >/dev/null 2>&1
rm -rf "$DATA"; mkdir -p "$DATA"
rm -f "$IMGFILE"
# File sparso: occupa spazio reale solo per i blocchi scritti, ma il filesystem
# dichiara 7 GiB — serve perché il preflight pretende almeno 5 GiB liberi.
truncate -s "${IMGSIZE_GB}G" "$IMGFILE"
mkfs.ext4 -q -F "$IMGFILE" >/dev/null 2>&1
mount -o loop "$IMGFILE" "$DATA"

MNT=$(findmnt -n -o SOURCE,FSTYPE --mountpoint "$DATA" 2>/dev/null)
check "/srv/tsm-data è un punto di mount" "$([ -n "$MNT" ] && echo 1 || echo 0)" "$MNT"
DEV_DATA=$(stat -c %d "$DATA"); DEV_ROOT=$(stat -c %d /)
check "il filesystem dei dati è distinto da quello di root" \
      "$([ "$DEV_DATA" != "$DEV_ROOT" ] && echo 1 || echo 0)" "dev $DEV_DATA vs $DEV_ROOT"

# `postgres` come SOTTODIRECTORY: la radice del mount contiene lost+found e
# initdb rifiuterebbe una directory non vuota.
install -d -o "$PG_UID" -g "$PG_GID" -m 0700 "$PGDIR"
check "directory dei dati creata 0700 con l'uid dell'immagine" \
      "$([ "$(stat -c %u:%g:%a "$PGDIR")" = "${PG_UID}:${PG_GID}:700" ] && echo 1 || echo 0)" \
      "$(stat -c %u:%g:%a "$PGDIR")"

# ==================================================================
step "2. il preflight approva una configurazione corretta"
# ==================================================================
"$APP/deploy/preflight.sh" --dir "$APP" --quiet
rc=$?
check "preflight superato (uscita 0)" "$(yn $rc)" "uscita $rc"

# Niente python3: nella VM di Docker Desktop l'interprete c'è ma è rotto, e
# affidarsi a lui darebbe stringa vuota facendo passare il controllo per errore.
BIND=$(docker compose --project-directory "$APP" -f "$APP/compose.yaml" config 2>/dev/null  \
       | awk '/^  pgdata:/{p=1} p && /^      device:/{sub(/.*device:[[:space:]]*/,"");print;exit}')
check "pgdata ancorato a $PGDIR" "$([ "$BIND" = "$PGDIR" ] && echo 1 || echo 0)" "$BIND"

# ==================================================================
step "3. avvio reale: i dati finiscono sul disco 2"
# ==================================================================
compose up -d --wait db migrate api >/dev/null 2>&1
rc=$?
check "lo stack parte" "$(yn $rc)" "uscita $rc — $(compose ps 2>&1 | tail -3)"

check "PG_VERSION esiste sotto $PGDIR" \
      "$([ -f "$PGDIR/PG_VERSION" ] && echo 1 || echo 0)" \
      "$(ls -A "$PGDIR" 2>/dev/null | head -5 | tr '\n' ' ')"

# Su quale MOUNT si trovano davvero i file dei dati.
#
# Si usa `findmnt -T`, non un confronto di `st_dev`: su un filesystem overlay —
# come la radice della VM di Docker Desktop — un file appena creato riporta un
# st_dev diverso da quello della sua stessa directory. Confrontando i numeri di
# device il test dichiarava «non è sul filesystem di root» mentre i dati erano
# esattamente là. `findmnt -T` risponde alla domanda vera: quale mount contiene
# questo percorso.
if [ -f "$PGDIR/PG_VERSION" ]; then
    HOLDER=$(findmnt -n -o TARGET -T "$PGDIR/PG_VERSION" 2>/dev/null | tail -1)
    check "i file dei dati sono sul filesystem dedicato, non su /"           "$([ "$HOLDER" = "$DATA" ] && echo 1 || echo 0)"           "il mount che li contiene è '$HOLDER', atteso '$DATA'"
fi

# Una scrittura nota deve produrre modifiche SOTTO /srv/tsm-data/postgres.
MARK="marcatore-$(date +%s)"
sleep 1
touch /var/tmp/tsm-before
compose exec -T db psql -q -U tsm -d tsm -c \
    "CREATE TABLE IF NOT EXISTS tsm_storage_probe(v text); INSERT INTO tsm_storage_probe VALUES ('$MARK'); CHECKPOINT;" \
    >/dev/null 2>&1
rc=$?
check "scrittura di prova eseguita nel database" "$(yn $rc)"
CHANGED=$(find "$PGDIR" -newer /var/tmp/tsm-before -type f 2>/dev/null | wc -l)
check "la scrittura ha modificato file sotto $PGDIR" \
      "$([ "$CHANGED" -gt 0 ] && echo 1 || echo 0)" "$CHANGED file modificati"

READBACK=$(compose exec -T db psql -tAq -U tsm -d tsm -c \
           "SELECT v FROM tsm_storage_probe" 2>/dev/null | tr -d '\r\n')
check "il valore scritto si rilegge" "$([ "$READBACK" = "$MARK" ] && echo 1 || echo 0)" "$READBACK"

# Le immagini di Docker restano sul disco di sistema.
DROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
case "$DROOT" in
    "$DATA"|"$DATA"/*) check "la radice di Docker NON è sul disco dei dati" 0 "$DROOT" ;;
    *) check "la radice di Docker NON è sul disco dei dati" 1 "$DROOT" ;;
esac

compose down >/dev/null 2>&1

# ==================================================================
step "4. CONTROPROVA: senza mount e senza preflight, i dati vanno su /"
# ==================================================================
# È il pericolo che il preflight esiste per fermare. Se questo blocco non
# dimostrasse il danno, il rifiuto del preflight non proverebbe nulla.
umount "$DATA"
check "smontato: /srv/tsm-data non è più un mount" \
      "$([ -z "$(findmnt -n --mountpoint "$DATA" 2>/dev/null)" ] && echo 1 || echo 0)"
rm -rf "${DATA:?}"/*
check "la directory vuota resta su / (il caso peggiore: 'sembra a posto')" \
      "$([ -d "$DATA" ] && [ -z "$(ls -A "$DATA")" ] && echo 1 || echo 0)"

# Il volume va rimosso, altrimenti Docker riusa la definizione precedente.
docker volume rm "${PROJ}_pgdata" >/dev/null 2>&1

# 4a. Con la SOTTODIRECTORY ASSENTE, Docker si rifiuta di montare.
#
# Scoperto misurando, non a ragionamento: con `type: none, o: bind` il driver
# `local` NON crea il percorso del device, e il container non parte. È una
# seconda linea di difesa gradita, ma copre solo il caso in cui la directory non
# esista — che non è quello frequente.
compose up -d db >/dev/null 2>&1
for _ in $(seq 1 10); do [ -f "$PGDIR/PG_VERSION" ] && break; sleep 1; done
check "senza la directory, il bind di Docker fallisce e nulla viene creato"       "$([ ! -e "$PGDIR" ] && echo 1 || echo 0)"       "atteso nessun $PGDIR, trovato: $(ls -A "$DATA" 2>/dev/null | tr '
' ' ')"
compose down -v >/dev/null 2>&1

# 4b. Con la SOTTODIRECTORY PRESENTE, il danno si manifesta.
#
# È il caso realistico: la directory l'ha creata l'amministratore seguendo il
# runbook, e poi il disco non è stato montato — al riavvio, dopo una modifica a
# fstab, o perché il device ha cambiato nome. Il bind riesce, PostgreSQL fa
# initdb, e i dati finiscono sul filesystem di root senza un solo errore.
install -d -o "$PG_UID" -g "$PG_GID" -m 0700 "$PGDIR"
compose up -d db >/dev/null 2>&1
for _ in $(seq 1 40); do [ -f "$PGDIR/PG_VERSION" ] && break; sleep 1; done

DANGER=0
if [ -f "$PGDIR/PG_VERSION" ]; then
    HOLDER=$(findmnt -n -o TARGET -T "$PGDIR/PG_VERSION" 2>/dev/null | tail -1)
    [ "$HOLDER" = "/" ] && DANGER=1
fi
check "con la directory presente, SENZA preflight PostgreSQL inizializza su ROOT"       "$DANGER" "il pericolo non si è manifestato: PG_VERSION assente, o il mount che la contiene non è /"
printf '  (nessun errore, servizio apparentemente sano, dati sul disco sbagliato:
'
printf '   è esattamente il guasto silenzioso che il preflight deve impedire)
'


compose down -v >/dev/null 2>&1
rm -rf "${DATA:?}"/*

# ==================================================================
step "5. il preflight rifiuta quel caso, e non 'aggiusta' niente"
# ==================================================================
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1
rc=$?
check "preflight rifiuta con il codice 20 (non è un punto di mount)" \
      "$([ "$rc" -eq 20 ] && echo 1 || echo 0)" "uscita $rc"
check "il preflight NON ha creato la directory dei dati" \
      "$([ ! -d "$PGDIR" ] && echo 1 || echo 0)" "$(ls -A "$DATA" | head -3 | tr '\n' ' ')"
check "nessun file di pgdata è comparso su /" \
      "$([ -z "$(ls -A "$DATA")" ] && echo 1 || echo 0)"

# ==================================================================
step "6. rimontato il disco, tutto riparte e i dati sono quelli di prima"
# ==================================================================
mount -o loop "$IMGFILE" "$DATA"
check "rimontato" "$([ -n "$(findmnt -n --mountpoint "$DATA" 2>/dev/null)" ] && echo 1 || echo 0)"
check "i dati di prima sono ancora là" \
      "$([ -f "$PGDIR/PG_VERSION" ] && echo 1 || echo 0)"

"$APP/deploy/preflight.sh" --dir "$APP" --quiet
rc=$?
check "preflight di nuovo superato" "$(yn $rc)" "uscita $rc"

compose up -d --wait db migrate api >/dev/null 2>&1
rc=$?
check "lo stack riparte" "$(yn $rc)" "uscita $rc"
READBACK=$(compose exec -T db psql -tAq -U tsm -d tsm -c \
           "SELECT v FROM tsm_storage_probe" 2>/dev/null | tr -d '\r\n')
check "il valore scritto prima dello smontaggio è ancora nel database" \
      "$([ "$READBACK" = "$MARK" ] && echo 1 || echo 0)" "letto: '$READBACK' atteso: '$MARK'"

# ==================================================================
step "7. altri rifiuti del preflight, sul mount vero"
# ==================================================================
compose down >/dev/null 2>&1

chown root:root "$PGDIR"
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
check "proprietario errato → codice 23" "$([ "$rc" -eq 23 ] && echo 1 || echo 0)" "uscita $rc"
chown "$PG_UID:$PG_GID" "$PGDIR"

chmod 0777 "$PGDIR"
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
check "permessi troppo larghi → codice 24" "$([ "$rc" -eq 24 ] && echo 1 || echo 0)" "uscita $rc"
chmod 0700 "$PGDIR"

mv "$APP/secrets/tls/privkey.pem" "$APP/secrets/tls/privkey.pem.via"
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
check "chiave TLS mancante → codice 31" "$([ "$rc" -eq 31 ] && echo 1 || echo 0)" "uscita $rc"
mv "$APP/secrets/tls/privkey.pem.via" "$APP/secrets/tls/privkey.pem"

chmod 0644 "$APP/secrets/api_db_password"
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
check "secret leggibile da altri → codice 30" "$([ "$rc" -eq 30 ] && echo 1 || echo 0)" "uscita $rc"
chmod 0400 "$APP/secrets/api_db_password"

# Configurazione di sviluppo: il preflight guarda il RISULTATO, non i file.
docker compose -p "$PROJ" --project-directory "$APP" \
    -f "$APP/compose.yaml" -f "$APP/compose.storage-dev.yaml" config >/dev/null 2>&1
# compose.override.yaml è il modo più realistico in cui una configurazione di
# sviluppo entra in produzione: Compose lo applica DA SOLO, senza che nessuno lo
# nomini sulla riga di comando.
cat > "$APP/compose.override.yaml" <<'YML'
volumes:
  pgdata:
    driver_opts: !reset null
YML
"$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
check "override di sviluppo (compose.override.yaml) → codice 42" \
      "$([ "$rc" -eq 42 ] && echo 1 || echo 0)" "uscita $rc"
rm -f "$APP/compose.override.yaml"

# Spazio insufficiente: si riempie il filesystem fino a scendere sotto i 5 GiB.
FREE_MB=$(df -BM --output=avail "$DATA" | tail -1 | tr -d ' M')
TO_FILL=$((FREE_MB - 4000))
if [ "$TO_FILL" -gt 0 ]; then
    fallocate -l "${TO_FILL}M" "$DATA/zavorra" 2>/dev/null || \
        dd if=/dev/zero of="$DATA/zavorra" bs=1M count="$TO_FILL" >/dev/null 2>&1
    "$APP/deploy/preflight.sh" --dir "$APP" --quiet >/dev/null 2>&1; rc=$?
    check "spazio libero sotto la soglia → codice 26" \
          "$([ "$rc" -eq 26 ] && echo 1 || echo 0)" "uscita $rc, liberi $(df -BM --output=avail "$DATA" | tail -1)"
    rm -f "$DATA/zavorra"
else
    check "spazio libero sotto la soglia → codice 26" 0 "non ho potuto ridurre lo spazio libero"
fi

# ==================================================================
printf '\n======================================================================\n'
printf '  %d PASS, %d FAIL\n' "$PASS" "$FAIL"
printf '======================================================================\n'
[ "$FAIL" -eq 0 ] && printf 'RISULTATO: TUTTI I CONTROLLI PASSATI\n' || printf 'RISULTATO: CI SONO FALLIMENTI\n'
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
