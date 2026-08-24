#!/usr/bin/env bash
#
# §29: prova di RIPRISTINO. L'integrità dell'applicazione dopo un restore.
#
# In produzione il backup della VM lo fa Veeam, e la configurazione dei job è
# responsabilità dell'infrastruttura. Quello che NON è responsabilità
# dell'infrastruttura è la domanda che questo script pone: una volta ripristinato
# lo stato, PostgreSQL riparte, la storia dell'inventario è intatta, la proiezione
# è attuale e coerente, e la readiness torna verde?
#
# Se un ambiente Veeam non è disponibile, l'equivalente è un'istantanea COERENTE
# del volume dei dati e il suo ripristino — che è ciò che questo script fa.
#
# ⚠ L'istantanea si prende a SERVIZIO FERMO. Copiare l'immagine di un disco mentre
# PostgreSQL scrive produce un'istantanea incoerente, e una prova di ripristino che
# la usasse riprodurrebbe l'errore invece di escluderlo. In produzione la coerenza
# la dà il quiesce dell'ipervisore; qui la si ottiene nel modo più semplice
# possibile — si spegne.
#
# ⚠⚠ IL RIPRISTINO SOVRASCRIVE I DATI ATTUALI. È il senso della prova.
#
#   backup-restore-test.sh --snapshot <nome>    prende l'istantanea
#   backup-restore-test.sh --restore <nome>     ripristina e VERIFICA
#
set -uo pipefail

APP="${TSM_APP_DIR:-/opt/tsm}"
IMG="${TSM_DATA_IMG:-/var/lib/tsm-data.img}"
MNT="${TSM_DATA_MNT:-/srv/tsm-data}"
#: Digest della baseline approvata, scritto a mano dal manifesto: se lo leggesse dal
#: codice, il confronto direbbe soltanto che il codice è d'accordo con se stesso.
BASELINE_SHA="febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"

MODO=""; NOME=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --snapshot) MODO=snapshot; NOME="${2:-}"; shift 2 ;;
        --restore)  MODO=restore;  NOME="${2:-}"; shift 2 ;;
        --dir)      APP="${2:-}"; shift 2 ;;
        -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "argomento non riconosciuto: $1" >&2; exit 2 ;;
    esac
done
[ -n "$MODO" ] && [ -n "$NOME" ] || { echo "serve --snapshot <nome> o --restore <nome>" >&2; exit 2; }
SNAP="/var/lib/tsm-snap-${NOME}.img"
cd "$APP" || { echo "directory inesistente: $APP" >&2; exit 2; }

PASS=0; FAIL=0
ck() {
    if [ "$2" = "1" ]; then printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1))
    else printf '  [FAIL] %s\n' "$1"; [ -n "${3:-}" ] && printf '         -> %s\n' "$3"
         FAIL=$((FAIL+1)); fi
}
q() { docker compose exec -T db psql -U tsm -d tsm -At -c "$1" 2>/dev/null | tr -d '\r'; }

attendi_attivo() {
    local i
    for i in $(seq 1 90); do
        [ "$(systemctl is-active tsm.service)" = "active" ] && return 0
        sleep 2
    done
    return 1
}
pronto() {
    local i
    for i in $(seq 1 90); do
        if docker compose exec -T api python -c "
import urllib.request, sys
try:
    sys.exit(0 if b'ready' in urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=5).read() else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then return 0; fi
        sleep 2
    done
    return 1
}

# ==================================================================
if [ "$MODO" = "snapshot" ]; then
    echo "== istantanea coerente: si ferma, si smonta, si STACCA, si copia =="
    systemctl stop tsm.service
    sleep 2
    umount "$MNT" || { echo "smontaggio non riuscito" >&2; systemctl start tsm.service; exit 1; }

    # ⚠ IL PASSO CHE MANCAVA, e che ha prodotto un'istantanea INUTILIZZABILE.
    #
    # Fermare il servizio e smontare il filesystem NON basta quando il disco è un
    # file con un device di loop. `umount` scarica il filesystem sul DEVICE; la
    # scrittura del device verso il file sottostante è un'altra cosa, e `cp` legge
    # il file. Copiando in quel momento si ottiene un'immagine a metà.
    #
    # Come si è manifestato: l'istantanea sembrava perfetta — 54 MiB, digest
    # stampato, servizio ripartito senza un errore — e al ripristino PostgreSQL è
    # uscito con «PANIC: could not locate a valid checkpoint record». Un backup che
    # nessuno ha mai ripristinato non è un backup, ed è precisamente la ragione per
    # cui §29 esiste: la prova non è che il backup si prende, è che si ripristina.
    #
    # `losetup -d` più `sync` chiudono il buco. Su una VM vera il disco non è un
    # file e questo passo non serve; la coerenza la dà il quiesce dell'ipervisore.
    for L in $(losetup -j "$IMG" -O NAME --noheadings 2>/dev/null); do
        echo "  stacco $L"
        losetup -d "$L" || true
    done
    sync

    # `--sparse=always`: l'immagine dichiara decine di gigabyte e ne occupa poche
    # decine di mega. Senza, la copia materializzerebbe tutto il disco.
    cp --sparse=always "$IMG" "$SNAP"
    sync
    ls -lsh "$SNAP" | awk '{print "  " $1 " occupati (" $6 " dichiarati)"}'
    A=$(sha256sum "$IMG" | cut -d' ' -f1); B=$(sha256sum "$SNAP" | cut -d' ' -f1)
    echo "  digest: ${B:0:24}"
    [ "$A" = "$B" ] && echo "  copia identica al volume" \
                    || { echo "  ⚠ la copia NON è identica al volume" >&2; exit 1; }

    # E si controlla che il filesystem dell'istantanea sia SANO, adesso, invece di
    # scoprirlo il giorno del ripristino.
    if command -v e2fsck >/dev/null 2>&1; then
        e2fsck -fn "$SNAP" >/tmp/fsck.out 2>&1
        RC=$?
        # e2fsck: 0 = pulito, 1 = corretto (non con -n), >1 = problemi.
        [ "$RC" -le 1 ] && echo "  e2fsck: filesystem coerente" \
                        || { echo "  ⚠ e2fsck ha trovato problemi (uscita $RC):" >&2
                             tail -5 /tmp/fsck.out >&2; exit 1; }
    fi

    mount "$MNT"
    systemctl start tsm.service
    attendi_attivo && echo "  servizio ripartito" || echo "  ⚠ servizio NON ripartito"
    # E si aspetta che sia PRONTO, non solo attivo: chi chiamava subito dopo trovava
    # l'API che non accettava ancora connessioni, e la modifica che doveva rendere
    # il ripristino distinguibile non avveniva.
    pronto && echo "  readiness verde" || echo "  ⚠ readiness NON verde"
    exit 0
fi

# ==================================================================
[ -f "$SNAP" ] || { echo "istantanea inesistente: $SNAP" >&2; exit 2; }

echo "== stato PRIMA del ripristino (per contrasto) =="
PRIMA_V=$(q "SELECT max(version) FROM inventory_versions")
PRIMA_U=$(q "SELECT count(*) FROM users")
PRIMA_A=$(q "SELECT count(*) FROM audit")
echo "  versione $PRIMA_V, $PRIMA_U utenze, $PRIMA_A voci di audit"

echo "== ripristino: si sovrascrive il volume dei dati =="
systemctl stop tsm.service
sleep 2
# Si smonta ATTRAVERSO systemd, non con `umount`: l'unità .mount generata da fstab
# è ciò che il servizio dichiara come dipendenza, e smontare alle sue spalle la
# lascia convinta di essere attiva.
systemctl stop "srv-$(systemd-escape -p "${MNT#/srv/}").mount" 2>/dev/null \
    || umount "$MNT" || { echo "smontaggio non riuscito" >&2; exit 1; }

# ⚠ IL DEVICE DI LOOP VA STACCATO PRIMA DI SOSTITUIRE IL FILE, e questo è il passo
# che la prima stesura non aveva.
#
# Un `umount` non stacca necessariamente il loop associato a un mount di fstab: il
# device resta attaccato all'inode, con la SUA cache. Sostituendo il file mentre il
# loop è ancora attaccato, il kernel al rimontaggio serve la vista vecchia; il
# ripristino sembra riuscito, PostgreSQL parte sui dati di PRIMA e li riscrive
# sopra i byte appena ripristinati. Misurato: dopo il «ripristino» dello stato
# vergine l'inventario era ancora alla versione 17, e il file finiva con un digest
# diverso sia da quello di prima sia da quello dell'istantanea — perché era la
# vecchia base ri-scritta.
#
# Sull'ambiente di qualificazione il disco dati è un file con un loop; su una VM
# vera è un disco virtuale e questo passo non esiste. È una precauzione DELLO
# STRUMENTO, non una condizione del ripristino di produzione.
for L in $(losetup -j "$IMG" -O NAME --noheadings 2>/dev/null); do
    echo "  stacco $L (era attaccato a $IMG)"
    losetup -d "$L" || true
done
sync

cp --sparse=always "$SNAP" "$IMG"
sync
ATTESO=$(sha256sum "$SNAP" | cut -d' ' -f1)
EFFETTIVO=$(sha256sum "$IMG" | cut -d' ' -f1)
ck "il volume ripristinato è identico all'istantanea, byte per byte" \
   "$([ "$ATTESO" = "$EFFETTIVO" ] && echo 1 || echo 0)" \
   "atteso ${ATTESO:0:16}… effettivo ${EFFETTIVO:0:16}…"

mount "$MNT"
findmnt -n -o SOURCE,TARGET,FSTYPE --mountpoint "$MNT" | sed 's/^/  /'

echo "== ripartenza =="
systemctl start tsm.service
attendi_attivo && E=1 || E=0
ck "il servizio riparte sui dati ripristinati" "$E" "$(systemctl is-active tsm.service)"

echo "== PostgreSQL =="
# Un ripristino incoerente si manifesta qui: il server rifiuta di partire, oppure
# parte e dichiara un recupero dal WAL che non trova la fine.
ck "PostgreSQL accetta connessioni" "$([ -n "$(q 'SELECT 1')" ] && echo 1 || echo 0)"
LOGDB=$(docker compose logs --no-color --tail 200 db 2>&1)
# Il pattern si applica a una variabile già catturata: `grep -q` in fondo a una
# pipe, con `pipefail` attivo, riporta fallimento proprio quando trova.
SOSPETTO="$(printf '%s' "$LOGDB" | grep -iE 'could not|corrupt|invalid page|PANIC' || true)"
ck "nessun errore di recupero o di corruzione nei log di PostgreSQL" \
   "$([ -z "$SOSPETTO" ] && echo 1 || echo 0)" \
   "$(printf '%s' "$SOSPETTO" | tail -3)"

echo "== la storia dell'inventario =="
V=$(q "SELECT max(version) FROM inventory_versions")
U=$(q "SELECT count(*) FROM users")
A=$(q "SELECT count(*) FROM audit")
echo "  ripristinato: versione $V, $U utenze, $A voci di audit"
echo "  prima:        versione $PRIMA_V, $PRIMA_U utenze, $PRIMA_A voci di audit"
# ⚠ La prima stesura scriveva `[ a != b ] || [ a = b ]`, che è vero SEMPRE: un
# controllo che non può fallire non è un controllo, e dichiarava riuscito un
# ripristino che non era avvenuto. Adesso si pretende che ALMENO UNA delle tre
# grandezze sia cambiata: un'istantanea presa in un momento diverso non può
# coincidere in tutto con lo stato attuale, e se coincide il volume non è stato
# sostituito.
ck "lo stato è CAMBIATO: il volume è stato davvero sostituito" \
   "$([ "$V" != "$PRIMA_V" ] || [ "$U" != "$PRIMA_U" ] || [ "$A" != "$PRIMA_A" ] && echo 1 || echo 0)" \
   "versione, utenze e audit sono identici a prima: il ripristino non ha avuto effetto"
ck "il digest della versione 1 è quello della baseline approvata" \
   "$([ "$(q "SELECT canonical_sha256 FROM inventory_versions WHERE version = 1")" = "$BASELINE_SHA" ] && echo 1 || echo 0)" \
   "$(q "SELECT canonical_sha256 FROM inventory_versions WHERE version = 1")"
# ⚠ `h.version` qualificato: `version` da sola è ambigua fra le due tabelle,
# PostgreSQL rifiuta la query, `q` restituisce vuoto, e il controllo diceva FAIL su
# una testa perfettamente valida.
ck "la testa punta a una versione che esiste" \
   "$([ -n "$(q 'SELECT h.version FROM inventory_head h JOIN inventory_versions v ON v.version = h.version')" ] && echo 1 || echo 0)" \
   "testa=$(q 'SELECT version FROM inventory_head')"

echo "== la proiezione =="
ck "la proiezione dichiara la stessa versione della testa" \
   "$([ "$(q 'SELECT head_version FROM inventory_projection_state')" = "$(q 'SELECT version FROM inventory_head')" ] && echo 1 || echo 0)" \
   "proiezione=$(q 'SELECT head_version FROM inventory_projection_state') testa=$(q 'SELECT version FROM inventory_head')"
ck "la struttura è quella attesa (3/6/102)" \
   "$([ "$(q 'SELECT count(*) FROM inventory_locations')" = "3" ] \
      && [ "$(q 'SELECT count(*) FROM inventory_rooms')" = "6" ] \
      && [ "$(q 'SELECT count(*) FROM inventory_racks')" = "102" ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM inventory_locations') / $(q 'SELECT count(*) FROM inventory_rooms') / $(q 'SELECT count(*) FROM inventory_racks')"

echo "== le foto e i loro riferimenti storici =="
ck "le foto e i riferimenti sono coerenti fra loro" \
   "$([ "$(q 'SELECT count(*) FROM inventory_photo_refs r LEFT JOIN photos p ON p.id = r.photo_id WHERE p.id IS NULL')" = "0" ] && echo 1 || echo 0)" \
   "riferimenti orfani: $(q 'SELECT count(*) FROM inventory_photo_refs r LEFT JOIN photos p ON p.id = r.photo_id WHERE p.id IS NULL')"
echo "  foto: $(q 'SELECT count(*) FROM photos'), riferimenti: $(q 'SELECT count(*) FROM inventory_photo_refs')"

echo "== lo stato del worker =="
ck "il battito è una riga sola" \
   "$([ "$(q 'SELECT count(*) FROM worker_heartbeat')" = "1" ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM worker_heartbeat')"
ck "nessuna consegna resta in uno stato intermedio impossibile" \
   "$([ "$(q "SELECT count(*) FROM reminder_deliveries WHERE state NOT IN ('pending','delivered','failed','retry_exhausted')")" = "0" ] && echo 1 || echo 0)" \
   "$(q 'SELECT state, count(*) FROM reminder_deliveries GROUP BY state')"

echo "== readiness =="
pronto && E=1 || E=0
ck "readiness verde dopo il ripristino" "$E"

echo "== project.py --verify =="
docker compose run --rm migrate python scripts/project.py --verify > /tmp/verify.out 2>&1
RC=$?
sed 's/^/    /' /tmp/verify.out | tail -8
ck "project.py --verify esce 0 (fedeltà e attualità)" \
   "$([ "$RC" -eq 0 ] && echo 1 || echo 0)" "uscita $RC"

echo "== l'applicazione risponde dall'esterno =="
CA="$APP/secrets/tls/fullchain.pem"
C=$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" https://localhost/ 2>/dev/null)
ck "nginx serve la pagina" "$([ "$C" = "200" ] && echo 1 || echo 0)" "HTTP $C"

echo
echo "=== $PASS PASS, $FAIL FAIL ==="
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
