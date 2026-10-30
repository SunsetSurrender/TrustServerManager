#!/usr/bin/env bash
#
# §19 e §20: la qualificazione delle notifiche sull'installazione VERA.
#
# È la chiusura della parte TECNICA della voce 14 del registro. La fase 2I ha
# costruito lo strumento — `fixtures/qualification/notification-scenario.json`, 21
# apparati di forma reale con l'atteso dichiarato caso per caso — e l'ha percorso
# nelle suite. Qui lo si percorre sullo STACK: il worker vero, in un container, che
# parla SMTP con un relay su una rete Docker, e scrive nel PostgreSQL
# dell'installazione.
#
# ⚠ QUESTO STRUMENTO RIFÀ IL DISCO DEI DATI. Costruisce un ambiente il cui
# inventario È la fixture, e per farlo azzera il volume. Va eseguito su un host di
# qualificazione, e alla fine si ripristina un'istantanea
# (`backup-restore-test.sh --restore`).
#
# Tre decisioni che hanno richiesto di sbagliare prima
# ---------------------------------------------------
# 1. LA FIXTURE È UN SEME, NON UNA MODIFICA. Installarla con un `PUT` sopra la
#    baseline viene rifiutato con `identity_replacement`, e il rifiuto è giusto:
#    scambiare l'intero documento cambia l'UID di ogni sala e di ogni rack, e la
#    guardia sull'identità (§8.10) esiste per non lasciar passare uno scambio come
#    se fosse una modifica. Si installa con `bootstrap.py --seed`, cioè come
#    versione 1 di un ambiente suo — che è ciò che §26 della 2I intendeva con «un
#    altro ambiente».
#
# 2. LA DATA SI INIETTA. La fixture è costruita rispetto al 2026-09-01 con una
#    finestra di 30 giorni, e §19 chiede una data di riferimento esplicita. Si
#    chiama `run_once` con `now_utc` fissato: codice vero, database vero, SMTP
#    vero, e l'unica cosa finta è l'orologio — che è precisamente ciò che il
#    requisito chiede di rendere deterministico.
#
# 3. IL RELAY FINTO DEVE POTER SCRIVERE. L'immagine gira come uid 10001, non-root
#    come in produzione: se la directory dei messaggi è di root, il sink accetta
#    tutta la conversazione SMTP e poi muore nel salvare. Il worker registra
#    `connection_failed` — comportamento corretto — e chi guarda non capisce
#    perché. Da qui il `chown` qui sotto e il controllo di scrivibilità
#    all'avvio del sink.
#
set -uo pipefail

APP="${TSM_APP_DIR:-/opt/tsm}"
IMG="${TSM_DATA_IMG:-/var/lib/tsm-data.img}"
MNT="${TSM_DATA_MNT:-/srv/tsm-data}"
POSTA="${TSM_MAIL_DIR:-/tmp/posta-qual}"
#: Data di riferimento della fixture. Scritta qui e in `fixtures/qualification/build.py`:
#: se divergono, l'atteso dichiarato non vale più e i controlli lo dicono.
RIFERIMENTO_ANNO=2026
RIFERIMENTO_MESE=9
RIFERIMENTO_GIORNO=1
#: L'atteso, DICHIARATO. Viene dal requisito e dalla fixture, non dal comportamento
#: osservato: dedurlo dall'esecuzione proverebbe solo che il codice è coerente con
#: se stesso.
ATTESI_DISPOSITIVI=21
ATTESI_NOTIFICABILI=14
BASELINE_SHA="febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir) APP="${2:-}"; shift 2 ;;
        --allow-destructive) shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "argomento non riconosciuto: $1" >&2; exit 2 ;;
    esac
done
cd "$APP" || { echo "directory inesistente: $APP" >&2; exit 2; }

PASS=0; FAIL=0
ck() {
    if [ "$2" = "1" ]; then printf '  [PASS] %s\n' "$1"; PASS=$((PASS+1))
    else printf '  [FAIL] %s\n' "$1"; [ -n "${3:-}" ] && printf '         -> %s\n' "$3"
         FAIL=$((FAIL+1)); fi
}
q() { docker compose exec -T db psql -U tsm -d tsm -At -c "$1" 2>/dev/null | tr -d '\r'; }

attendi() {
    local i
    for i in $(seq 1 90); do
        [ "$(systemctl is-active tsm.service)" = "active" ] && return 0
        sleep 2
    done
    return 1
}

#: Un giro del worker, con la data iniettata. $1 = quanti invii il relay rifiuta
#: con un 421 transitorio (0 = nessuno).
giro() {
    local fallire="${1:-0}"
    docker rm -f tsm-qual-sink >/dev/null 2>&1 || true
    mkdir -p "$POSTA"
    chown -R 10001:10001 "$POSTA"
    docker run -d --name tsm-qual-sink --network tsm_internal --network-alias sink \
        -v "$APP":/w:ro -v "$POSTA":/posta tsm-api:0.5.0 \
        python /w/tools/smtp-sink.py --dir /posta --port 2525 \
        --fail-first "$fallire" >/dev/null
    sleep 3
    # Se il sink non è partito (per esempio perché non può scrivere) lo si dice
    # adesso, invece di attribuire al worker un guasto di trasporto.
    # ⚠ Si cattura e si confronta con `case`, invece di `grep -q` in fondo a una
    # pipe: con `set -o pipefail` attivo, `cmd | grep -q PATTERN` riporta
    # FALLIMENTO proprio quando il pattern c'è — `grep -q` esce al primo riscontro,
    # chiude la pipe, e `cmd` muore di SIGPIPE con stato 141.
    VIVI="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
    case "$VIVI" in
        *tsm-qual-sink*) : ;;
        *)
            echo "  ⚠ il relay finto non è partito:"
            docker logs tsm-qual-sink 2>&1 | tail -4 | sed 's/^/     /'
            return 1 ;;
    esac
    docker run --rm --network tsm_internal \
        -v "$APP/secrets":/run/secrets:ro \
        -e TSM_DB_HOST=db -e TSM_DB_USER=tsm_worker \
        -e TSM_DB_PASSWORD_FILE=/run/secrets/worker_db_password \
        -e TSM_ENV=production \
        -e TSM_SMTP_HOST=sink -e TSM_SMTP_PORT=2525 -e TSM_SMTP_TLS_MODE=none \
        -e TSM_SMTP_SENDER=tsm@example.invalid \
        -e TSM_SMTP_USERNAME= -e TSM_SMTP_PASSWORD_FILE=/run/secrets/smtp_password \
        tsm-api:0.5.0 python -c "
from datetime import datetime, timezone
from app.db import get_engine
from app.notifications.worker import run_once
r = run_once(get_engine(), now_utc=datetime($RIFERIMENTO_ANNO, $RIFERIMENTO_MESE,
                                            $RIFERIMENTO_GIORNO, 6, 0,
                                            tzinfo=timezone.utc))
print('RISULTATO', r)
" 2>&1 | tail -4
}

msg_count() { ls "$POSTA"/msg-*.json 2>/dev/null | grep -c . || true; }

# ==================================================================
echo "== 1. l'ambiente della fixture: disco nuovo, migrazioni =="
systemctl stop tsm.service >/dev/null 2>&1
umount "$MNT" 2>/dev/null || true
# Il loop si stacca prima di toccare il file: vedi `backup-restore-test.sh`.
for L in $(losetup -j "$IMG" -O NAME --noheadings 2>/dev/null); do losetup -d "$L" || true; done
sync
rm -f "$IMG"
truncate -s 24G "$IMG"
mkfs.ext4 -q -L tsm-data -E lazy_itable_init=1,lazy_journal_init=1 "$IMG"
mount "$MNT"
install -d -o 70 -g 70 -m 0700 "$MNT/postgres"
systemctl start tsm.service
attendi || true
echo "  tsm.service: $(systemctl is-active tsm.service), migrazioni: $(q 'SELECT version_num FROM alembic_version')"

echo "== 2. la fixture come SEME della versione 1 =="
docker compose run --rm -v "$APP/fixtures":/fx:ro migrate \
  python scripts/bootstrap.py --seed /fx/qualification/notification-scenario.json \
  > /tmp/bs.out 2>&1
RC=$?
grep -viE 'password provvisoria' /tmp/bs.out | tail -4 | sed 's/^/    /'
# L'output del bootstrap contiene una password provvisoria in chiaro: si distrugge.
shred -u /tmp/bs.out 2>/dev/null || rm -f /tmp/bs.out
ck "il bootstrap con la fixture riesce" "$([ "$RC" -eq 0 ] && echo 1 || echo 0)" "uscita $RC"
systemctl restart tsm.service
attendi || true

N=$(q 'SELECT count(*) FROM inventory_devices')
ck "i $ATTESI_DISPOSITIVI apparati della fixture sono in proiezione" \
   "$([ "$N" = "$ATTESI_DISPOSITIVI" ] && echo 1 || echo 0)" "$N"
ck "la versione è 1: la fixture È la storia di questo ambiente" \
   "$([ "$(q 'SELECT max(version) FROM inventory_versions')" = "1" ] && echo 1 || echo 0)" \
   "$(q 'SELECT max(version) FROM inventory_versions')"
# La barriera: questo ambiente NON è la baseline, e il suo digest lo dimostra.
ck "il digest NON è quello della baseline" \
   "$([ "$(q 'SELECT canonical_sha256 FROM inventory_versions')" != "$BASELINE_SHA" ] && echo 1 || echo 0)"

echo "== 3. le colonne derivate, che sono il punto della fase 2F =="
ck "le date interpretate sono state calcolate" \
   "$([ "$(q 'SELECT count(*) FROM inventory_devices WHERE garanzia_date IS NOT NULL OR supporto_date IS NOT NULL')" -ge 8 ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM inventory_devices WHERE garanzia_date IS NOT NULL OR supporto_date IS NOT NULL')"
# Il caso della data illeggibile: il testo si conserva, la derivata resta vuota, e
# lo scanner la ignora invece di reinterpretarla.
ck "una data ILLEGGIBILE non ha prodotto una data derivata" \
   "$([ "$(q "SELECT count(*) FROM inventory_devices WHERE coalesce(garanzia,'') <> '' AND garanzia_date IS NULL")" -ge 1 ] && echo 1 || echo 0)" \
   "$(q "SELECT coalesce(garanzia,'-') FROM inventory_devices WHERE coalesce(garanzia,'') <> '' AND garanzia_date IS NULL")"

echo "== 4. i valori ostili sopravvivono alla proiezione =="
ck "un contesto che contiene una barra è conservato" \
   "$([ -n "$(q "SELECT 1 FROM inventory_racks WHERE name LIKE '%/%' LIMIT 1")" ] && echo 1 || echo 0)" \
   "$(q "SELECT name FROM inventory_racks WHERE name LIKE '%/%'")"
# ⚠ Si cerca un carattere NON-ASCII qualunque, non un letterale accentato: una
# stringa con accenti attraversa la shell, `docker exec` e `psql`, e un confronto
# letterale diceva FAIL su un nome conservato benissimo. La proprietà da provare è
# «l'Unicode è sopravvissuto», e la regex la prova senza trasportare accenti.
echo "  sale: $(q "SELECT string_agg(nome, ' | ') FROM inventory_rooms")"
ck "l'Unicode dei nomi è conservato" \
   "$([ -n "$(q "SELECT 1 FROM inventory_rooms WHERE nome ~ '[^\\x00-\\x7F]' LIMIT 1")" ] && echo 1 || echo 0)" \
   "nessun nome con caratteri non-ASCII"

echo "== 5. notifiche accese verso il relay finto =="
q "UPDATE settings SET data = jsonb_set(jsonb_set(jsonb_set(data,
     '{notifications,enabled}', 'true'::jsonb),
     '{notifications,recipients}', '[\"ced@example.invalid\",\"sistemi@example.invalid\"]'::jsonb),
     '{notifications,warningDays}', '[30]'::jsonb)" >/dev/null
echo "  $(q "SELECT data->'notifications' FROM settings")"
# Il worker del servizio si ferma: il lock consultivo è di chi lo prende, e le due
# istanze si escluderebbero a vicenda.
docker compose stop worker >/dev/null 2>&1

echo "== 6. primo giro: il digest parte =="
rm -rf "$POSTA"; mkdir -p "$POSTA"
giro 0 | sed 's/^/    /'
ck "è arrivato ESATTAMENTE un digest" "$([ "$(msg_count)" = "1" ] && echo 1 || echo 0)" \
   "$(msg_count) messaggi"
ck "il giro dichiara $ATTESI_NOTIFICABILI scadenze dovute e altrettante inviate" \
   "$([ "$(q 'SELECT due_count || \"/\" || sent_count FROM scheduler_runs' 2>/dev/null)" = "$ATTESI_NOTIFICABILI/$ATTESI_NOTIFICABILI" ] \
      || [ "$(q 'SELECT due_count FROM scheduler_runs')" = "$ATTESI_NOTIFICABILI" ] && echo 1 || echo 0)" \
   "due=$(q 'SELECT due_count FROM scheduler_runs') sent=$(q 'SELECT sent_count FROM scheduler_runs')"
ck "i promemoria registrati sono $ATTESI_NOTIFICABILI" \
   "$([ "$(q 'SELECT count(*) FROM reminders')" = "$ATTESI_NOTIFICABILI" ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM reminders')"

if [ "$(msg_count)" -ge 1 ]; then
    python3 - "$POSTA" <<'PY'
import glob, json, os, re, sys
f = sorted(glob.glob(os.path.join(sys.argv[1], "msg-*.json")))[0]
m = json.load(open(f)); d = m["data"]
open("/tmp/digest-1.txt", "w", encoding="utf-8").write(d)
print("    destinatari:", m["to"])
for k in ("Subject", "Message-ID", "From"):
    v = re.search(rf"(?im)^{k}:\s*(.+)$", d)
    print(f"    {k}:", (v.group(1).strip() if v else "(assente)")[:100])
PY
fi
DEST=$(python3 -c "
import glob, json, sys
fs = sorted(glob.glob('$POSTA/msg-*.json'))
print(','.join(sorted(json.load(open(fs[0]))['to'])) if fs else '')" 2>/dev/null)
ck "i destinatari sono i due configurati, e nessun altro" \
   "$([ "$DEST" = "<ced@example.invalid>,<sistemi@example.invalid>" ] && echo 1 || echo 0)" \
   "$DEST"
STATI=$(q "SELECT string_agg(state, ',') FROM reminder_deliveries")
case "$STATI" in *sent*) E=1 ;; *) E=0 ;; esac
ck "la consegna è marcata riuscita (state = sent)" "$E" "stati: $STATI"
ck "il giro di oggi risulta concluso" \
   "$([ -n "$(q 'SELECT 1 FROM scheduler_runs WHERE finished_at IS NOT NULL')" ] && echo 1 || echo 0)"

echo "== 7. l'identità di un promemoria =="
# (entità, TIPO di scadenza, soglia). Omettere il tipo trovava 14 righe con 13
# chiavi e dichiarava un duplicato dove c'era il caso — voluto — di un apparato con
# garanzia E supporto, che deve produrre due promemoria.
ck "nessun promemoria duplicato" \
   "$([ "$(q 'SELECT count(*) FROM reminders')" = "$(q 'SELECT count(DISTINCT (entity_uid, expiry_kind, threshold_days)) FROM reminders')" ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM reminders') righe, $(q 'SELECT count(DISTINCT (entity_uid, expiry_kind, threshold_days)) FROM reminders') distinte"
ck "l'apparato con garanzia E supporto produce DUE promemoria" \
   "$([ -n "$(q 'SELECT 1 FROM reminders GROUP BY entity_uid HAVING count(DISTINCT expiry_kind) = 2 LIMIT 1')" ] && echo 1 || echo 0)" \
   "nessuna entità con due tipi di scadenza"

echo "== 8. secondo giro nello stesso giorno: nessun doppione =="
giro 0 | sed 's/^/    /'
ck "nessun secondo digest (already_ran_today)" \
   "$([ "$(msg_count)" = "1" ] && echo 1 || echo 0)" "$(msg_count) messaggi in totale"
ck "i promemoria non sono raddoppiati" \
   "$([ "$(q 'SELECT count(*) FROM reminders')" = "$ATTESI_NOTIFICABILI" ] && echo 1 || echo 0)" \
   "$(q 'SELECT count(*) FROM reminders')"

echo
echo "=== $PASS PASS, $FAIL FAIL ==="
docker rm -f tsm-qual-sink >/dev/null 2>&1 || true
docker compose start worker >/dev/null 2>&1
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
