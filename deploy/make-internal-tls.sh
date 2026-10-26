#!/bin/bash
# =====================================================================
# Certificato TLS INTERNO per TSM: una CA locale e un certificato server
# firmato da lei, con i nomi (SAN) con cui gli operatori raggiungono TSM.
#
# Perché una CA e non un certificato autofirmato secco: si installa UNA
# volta la CA sui client (GPO «Autorità di certificazione radice
# attendibili», o importazione manuale) e da lì il certificato server si
# può rinnovare senza toccare di nuovo i client. Un autofirmato secco va
# ridistribuito a ogni rinnovo.
#
# ⚠ HSTS: nginx invia Strict-Transport-Security. Un browser lo registra
#   solo se il certificato è valido, quindi finché la CA non è installata
#   sul client si vede l'avviso del browser ad ogni visita; installata la
#   CA, tutto torna normale. Distribuire la CA PRIMA di aprire TSM agli
#   operatori.
#
# La chiave della CA resta in --ca-dir (default /root/tsm-ca, 0700). Chi
# ha quella chiave può emettere certificati di cui i client si fidano:
# finito il lavoro, conviene spostarla fuori dalla macchina.
#
# Uso (come root, sull'host):
#   deploy/make-internal-tls.sh --name tsm-prd-01.par-tec.it --name tsm-prd-01 [--ip 10.1.2.3]
#   deploy/make-internal-tls.sh ... --force     # rinnovo: sostituisce il certificato server
#
# Opzioni: --dir /opt/tsm  --ca-dir /root/tsm-ca  --days 825
# =====================================================================
set -euo pipefail

DIR=/opt/tsm
CA_DIR=/root/tsm-ca
DAYS=825            # massimo accettato da macOS/iOS anche per CA private
FORCE=0
NOMI=(); IPS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --name)   NOMI+=("${2:?}"); shift 2 ;;
        --ip)     IPS+=("${2:?}"); shift 2 ;;
        --dir)    DIR="${2:?}"; shift 2 ;;
        --ca-dir) CA_DIR="${2:?}"; shift 2 ;;
        --days)   DAYS="${2:?}"; shift 2 ;;
        --force)  FORCE=1; shift ;;
        -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
        *) printf 'argomento non riconosciuto: %s\n' "$1" >&2; exit 2 ;;
    esac
done
die() { printf '\033[31mERRORE\033[0m %s\n' "$*" >&2; exit 1; }
[ "${#NOMI[@]}" -gt 0 ] || die "serve almeno un --name (il nome DNS con cui si apre TSM)"
[[ "$DAYS" =~ ^[0-9]+$ ]] || die "--days deve essere un numero"
for n in "${NOMI[@]}"; do [[ "$n" =~ ^[A-Za-z0-9.-]+$ ]] || die "nome non valido: $n"; done
for i in "${IPS[@]}"; do [[ "$i" =~ ^[0-9.]+$|^[0-9A-Fa-f:]+$ ]] || die "IP non valido: $i"; done
command -v openssl >/dev/null || die "openssl non trovato"

TLS="$DIR/secrets/tls"
[ -d "$DIR/secrets" ] || die "$DIR/secrets non esiste: è la directory del progetto giusta?"
if [ -e "$TLS/privkey.pem" ] && [ "$FORCE" != 1 ]; then
    die "$TLS/privkey.pem esiste già. Per rinnovare: --force"
fi

umask 077
install -d -m 0700 "$CA_DIR"
if [ ! -f "$CA_DIR/ca.key" ]; then
    printf '== nuova CA interna in %s\n' "$CA_DIR"
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$CA_DIR/ca.key"
    openssl req -x509 -new -key "$CA_DIR/ca.key" -sha256 -days 3650 \
        -subj "/O=Trust Technologies/CN=TSM CA interna" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$CA_DIR/ca.crt"
else
    printf '== CA esistente in %s: la riuso (i client che l'\''hanno già non cambiano)\n' "$CA_DIR"
fi

SAN=""
for n in "${NOMI[@]}"; do SAN="${SAN:+$SAN,}DNS:$n"; done
for i in "${IPS[@]}";  do SAN="${SAN:+$SAN,}IP:$i"; done

LAV="$(mktemp -d)"; trap 'rm -rf "$LAV"' EXIT
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$LAV/privkey.pem"
openssl req -new -key "$LAV/privkey.pem" -subj "/O=Trust Technologies/CN=${NOMI[0]}" -out "$LAV/req.csr"
cat > "$LAV/ext.cnf" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=$SAN
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
EXT
openssl x509 -req -in "$LAV/req.csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
    -CAserial "$CA_DIR/ca.srl" -CAcreateserial -days "$DAYS" -sha256 \
    -extfile "$LAV/ext.cnf" -out "$LAV/cert.pem"
openssl verify -CAfile "$CA_DIR/ca.crt" "$LAV/cert.pem" >/dev/null || die "il certificato non si verifica con la CA"

install -d -m 0755 "$TLS"
cat "$LAV/cert.pem" "$CA_DIR/ca.crt" > "$TLS/fullchain.pem"
cp "$LAV/privkey.pem" "$TLS/privkey.pem"
cp "$CA_DIR/ca.crt" "$TLS/ca-interna.crt"

# La chiave deve essere leggibile da nginx (uid dell'immagine web, 101).
WEB_UID=101
if command -v docker >/dev/null 2>&1; then
    TAG_WEB="$(cd "$DIR" && docker compose config --images 2>/dev/null | grep '^tsm-web:' | head -1 || true)"
    if [ -n "$TAG_WEB" ]; then
        U="$(docker image inspect --format '{{.Config.User}}' "$TAG_WEB" 2>/dev/null | cut -d: -f1 || true)"
        [[ "$U" =~ ^[0-9]+$ ]] && WEB_UID="$U"
    fi
fi
chown "$WEB_UID:$WEB_UID" "$TLS/privkey.pem"
chmod 0400 "$TLS/privkey.pem"
chmod 0444 "$TLS/fullchain.pem" "$TLS/ca-interna.crt"

printf '\n== fatto\n'
printf '  certificato  %s/fullchain.pem  (SAN: %s, %s giorni)\n' "$TLS" "$SAN" "$DAYS"
printf '  chiave       %s/privkey.pem    (uid %s, 0400)\n' "$TLS" "$WEB_UID"
printf '  CA da distribuire ai client: %s/ca-interna.crt\n' "$TLS"
printf '  impronta SHA-256 della CA (da confrontare sui client):\n    '
openssl x509 -in "$CA_DIR/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2
printf '\n  Rinnovo: rieseguire con gli stessi --name e --force, poi\n'
printf '    docker compose -f %s/compose.yaml restart web\n' "$DIR"
