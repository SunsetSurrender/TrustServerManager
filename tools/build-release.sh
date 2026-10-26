#!/bin/bash
# =====================================================================
# Costruisce le immagini del rilascio DA GIT e scrive deploy/release.pin.
#
# Perché da `git archive` e non dalla copia di lavoro: su Windows i file
# su disco hanno CRLF (core.autocrlf) e un'immagine costruita da lì non è
# quella del commit. `git -c core.autocrlf=false archive` restituisce i
# blob così come sono nel repository.
#
# Requisiti: bash, git, docker con compose, accesso ai registry per le
# immagini base (pinnate per digest) e a PyPI. Va bene WSL, una macchina
# Linux qualsiasi, o la VM stessa se raggiunge internet.
#
# Uso:
#   tools/build-release.sh             # costruisce da HEAD (albero pulito)
#   tools/build-release.sh --save DIR  # e scrive DIR/tsm-images.tar
#
# Dopo: committare deploy/release.pin. Il pin identifica il binario
# costruito QUI: due build della stessa sorgente danno Id diversi (le date
# finiscono nei livelli), quindi le immagini da trasferire sono queste.
# =====================================================================
set -euo pipefail

SAVE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --save) SAVE="${2:?--save richiede una directory}"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) printf 'argomento non riconosciuto: %s\n' "$1" >&2; exit 2 ;;
    esac
done

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
die() { printf '\033[31mERRORE\033[0m %s\n' "$*" >&2; exit 1; }

# Il pin stesso non conta: è l'unico file che questo script modifica.
SPORCO="$(git -c core.autocrlf=true status --porcelain | grep -v ' deploy/release.pin$' || true)"
[ -z "$SPORCO" ] || die "albero di lavoro non pulito: committare prima di costruire un rilascio.
$SPORCO"

COMMIT="$(git rev-parse HEAD)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -c core.autocrlf=false archive "$COMMIT" | tar -x -C "$TMP"

# compose valida i secret anche solo per costruire: segnaposto vuoti, che
# NON entrano in nessun contesto di build (.dockerignore li esclude).
mkdir -p "$TMP/secrets/tls"
for f in postgres_password api_db_password worker_db_password smtp_password; do : > "$TMP/secrets/$f"; done

printf '\033[1m== build di %s da git archive ==\033[0m\n' "${COMMIT:0:12}"
( cd "$TMP" && docker compose -f compose.yaml build )

IMMAGINI="$(cd "$TMP" && docker compose -f compose.yaml config --images | sort -u)"
TAG_API="$(printf '%s\n' "$IMMAGINI" | grep '^tsm-api:' | head -1)"
TAG_WEB="$(printf '%s\n' "$IMMAGINI" | grep '^tsm-web:' | head -1)"
DB_REF="$(printf '%s\n' "$IMMAGINI" | grep '^postgres@' | head -1)"
[ -n "$TAG_API" ] && [ -n "$TAG_WEB" ] && [ -n "$DB_REF" ] || die "immagini non riconosciute: $IMMAGINI"
docker image inspect "$DB_REF" >/dev/null 2>&1 || docker pull "$DB_REF"

ID_API="$(docker image inspect --format '{{.Id}}' "$TAG_API")"
ID_WEB="$(docker image inspect --format '{{.Id}}' "$TAG_WEB")"
DIGEST_DB="${DB_REF#postgres@}"
BASELINE_FILE="$(git show "$COMMIT:deploy/baseline/production-baseline.json" | sha256sum | awk '{print $1}')"
BASELINE_DOC="$(git show "$COMMIT:backend/app/inventory/baseline.py" | tr -d '\n' \
    | grep -o 'BASELINE_CANONICAL_SHA256 = ([^)]*)' | grep -o '[0-9a-f]\{64\}' | head -1 || true)"
[ -n "$BASELINE_DOC" ] || BASELINE_DOC="$(git show "$COMMIT:backend/app/inventory/baseline.py" \
    | grep -o 'BASELINE_CANONICAL_SHA256 = "[0-9a-f]\{64\}"' | grep -o '[0-9a-f]\{64\}' | head -1 || true)"
[ -n "$BASELINE_DOC" ] || die "BASELINE_CANONICAL_SHA256 non trovato in backend/app/inventory/baseline.py"
ULTIMA="$(git ls-tree --name-only "$COMMIT" backend/migrations/versions/ | grep -E '/[0-9]{4}_[a-z0-9_]+\.py$' | sort | tail -1)"
MIGRAZIONE="$(git show "$COMMIT:$ULTIMA" | grep -oE '^revision[^=]*= *"[0-9]{4}_[a-z0-9_]+"' | grep -oE '[0-9]{4}_[a-z0-9_]+')"
ALBERO_RILASCIO="$(python3 tools/release-identity.py --json --no-docker 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["identity"]["release_tree"]["sha256"])')"

cat > deploy/release.pin <<PIN
# Pin del rilascio — scritto da tools/build-release.sh, NON a mano.
# Lo leggono tools/release-bundle.sh e tools/preinstall-acceptance.sh.
# Costruito il $(date -u +%Y-%m-%dT%H:%M:%SZ) su $(hostname).
COMMIT_QUALIFICATO=$COMMIT
ALBERO_RILASCIO=$ALBERO_RILASCIO
TAG_API=$TAG_API
TAG_WEB=$TAG_WEB
ID_API=$ID_API
ID_WEB=$ID_WEB
DIGEST_DB=$DIGEST_DB
BASELINE_FILE=$BASELINE_FILE
BASELINE_DOC=$BASELINE_DOC
MIGRAZIONE=$MIGRAZIONE
PIN
printf '\n'; grep -v '^#' deploy/release.pin | sed 's/^/  /'

if [ -n "$SAVE" ]; then
    mkdir -p "$SAVE"
    docker save -o "$SAVE/tsm-images.tar" "$DB_REF" "$TAG_API" "$TAG_WEB"
    printf '\n  %s/tsm-images.tar (%s)\n' "$SAVE" "$(du -h "$SAVE/tsm-images.tar" | cut -f1)"
fi
printf '\nOra: git add deploy/release.pin && git commit -m "release: pin %s"\n' "${COMMIT:0:12}"
printf 'poi: tools/release-bundle.sh --out <dir> --with-images\n'
