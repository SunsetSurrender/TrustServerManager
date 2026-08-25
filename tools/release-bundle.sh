#!/bin/bash
# =====================================================================
# Fase 2K-A §6 — pacchetto di trasferimento verso l'host reale.
#
# Costruisce ciò che va su tsm-prd-01 e NIENTE altro. Due proprietà che
# un `scp -r` non ha:
#
# 1. I file si estraggono da GIT, non dalla copia di lavoro.
#    Su Windows `core.autocrlf=true` e .gitattributes copre .sh/.conf ma
#    non .json/.yaml: la baseline su disco ha CRLF e il suo digest è
#    9443191a…, non il febafb7e… qualificato. Copiata così com'è,
#    fallisce il cancello della baseline sull'host — per i soli \r.
#
# 2. L'elenco di ciò che NON deve partire è meccanico.
#    «Non trasferire l'override di sviluppo» affidato alla memoria
#    dell'operatore non è un controllo. Qui il pacchetto si costruisce
#    per inclusione esplicita e si RIFIUTA di contenere i vietati.
#
# Uso:
#   tools/release-bundle.sh --out /tmp/tsm-2k              # costruisce
#   tools/release-bundle.sh --out /tmp/tsm-2k --with-images
#   tools/release-bundle.sh --verify /opt/tsm              # sull'host
# =====================================================================
set -uo pipefail

COMMIT_QUALIFICATO="23d47dc1bd952e3b87282bb0e088d4771d409266"
# Due numeri diversi, e confonderli e stato l errore del primo giro.
#
# BASELINE_FILE: sha256 dei BYTE del file come sta nel commit (LF). E cio
# che una shell puo verificare: dice se e arrivato il file giusto.
#
# BASELINE_DOC: canonical_sha256(canonicalise(documento)), il digest del
# DOCUMENTO. Non e calcolabile da uno script di shell — serve la
# canonicalizzazione del prodotto — e lo verifica il prodotto stesso in
# --check e --init. Qui si riporta soltanto, per tracciabilita.
BASELINE_FILE="c22bcb4ed3888ca19648169a2dd06545f15e159ea33154fb9e91a50e8a9909b6"
BASELINE_DOC="febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1"
ID_API="sha256:54061db1d26a56e5235029d3852ba640106a9bfd5589d029b06ed80c8fb9a168"
ID_WEB="sha256:2d22b6e27bb3d3f3036a485ead23a26a20b5a780ac9bca44448ba208a325c541"
DIGEST_DB="sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"

# Due elenchi, e la distinzione è la sostanza di §4.
#
# IDENTITA: i file che stanno dentro l'albero del rilascio (i 117 di
# tools/release-identity.py) e che devono finire sul filesystem dell'host
# — il resto del rilascio è dentro le immagini. Questi si estraggono dal
# COMMIT QUALIFICATO, e se sono cambiati dopo la qualificazione l'identità
# del rilascio è cambiata e i cancelli si rieseguono.
IDENTITA=(
    compose.yaml
    deploy/preflight.sh
    deploy/tsm.service
    deploy/baseline/production-baseline.json
)

# DOCUMENTAZIONE: manuale operativo e manifesti. NON fanno parte
# dell'identità del rilascio — non sono tra i 117 file, e infatti sei
# commit dopo la qualificazione l'albero è rimasto identico byte per byte
# proprio perché toccavano solo questi. Si prendono da HEAD, perché
# all'operatore serve la versione più recente, non quella di un mese fa.
#
# Tenerli nello stesso elenco dei precedenti è l'errore che questo
# strumento ha commesso al primo giro: segnalava «identità del rilascio
# cambiata» per un paragrafo aggiunto a un README.
DOCUMENTAZIONE=(
    deploy/README.md
    deploy/baseline/MANIFEST.md
    deploy/RELEASE-MANIFEST.md
)

# §6 — ciò che non deve arrivare. Se uno di questi finisce nel pacchetto
# lo script si ferma: non avvisa, si ferma.
VIETATI=(
    compose.dev.yaml compose.override.yaml compose.storage-dev.yaml
    web/nginx.dev.conf
    tools/smtp-sink.py tools/make-dev-tls.ps1
    tools/notification-qualification.sh tools/failure-injection-test.py
    tools/mutation-harness.py tools/backup-restore-test.sh
)

MODO=""; DEST=""; IMMAGINI=0
while [ $# -gt 0 ]; do
    case "$1" in
        --out)         MODO=build;  DEST="${2:?}"; shift 2 ;;
        --verify)      MODO=verify; DEST="${2:?}"; shift 2 ;;
        --with-images) IMMAGINI=1; shift ;;
        -h|--help)     sed -n '2,24p' "$0"; exit 0 ;;
        *) printf 'argomento non riconosciuto: %s\n' "$1" >&2; exit 2 ;;
    esac
done
[ -n "$MODO" ] || { sed -n '2,24p' "$0"; exit 2; }

KO=0
ok() { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
ko() { KO=$((KO+1)); printf '  \033[31mKO\033[0m    %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }

# ---------------------------------------------------------------- verifica
if [ "$MODO" = verify ]; then
    printf '\033[1m== verifica del pacchetto arrivato in %s ==\033[0m\n' "$DEST"
    S="$DEST/SHA256SUMS"
    [ -f "$S" ] || { printf 'SHA256SUMS assente in %s: non c'\''è niente da verificare\n' "$DEST" >&2; exit 1; }
    while read -r atteso percorso; do
        [ -n "$percorso" ] || continue
        f="$DEST/$percorso"
        if [ ! -f "$f" ]; then ko "assente: $percorso"; continue; fi
        # Digest CANONICO, con i \r rimossi: è il numero che il rilascio
        # nomina. Un digest sul file grezzo identificherebbe il trasporto.
        vero="$(tr -d '\r' < "$f" | sha256sum | awk '{print $1}')"
        [ "$vero" = "$atteso" ] && ok "$percorso" \
                                || ko "$percorso: $vero" "atteso $atteso"
    done < "$S"
    for v in "${VIETATI[@]}"; do
        [ -e "$DEST/$v" ] && ko "artefatto vietato presente sull'host: $v"
    done
    B="$DEST/deploy/baseline/production-baseline.json"
    if [ -f "$B" ]; then
        SHA="$(tr -d '\r' < "$B" | sha256sum | awk '{print $1}')"
        [ "$SHA" = "$BASELINE_FILE" ] && ok "baseline: il file qualificato, ${SHA:0:16}…" \
                                     || ko "baseline NON è il file qualificato: $SHA"
    fi
    [ "$KO" = 0 ] && { printf '\n  pacchetto integro.\n'; exit 0; }
    printf '\n  %d problemi: NON procedere.\n' "$KO"; exit 1
fi

# ---------------------------------------------------------------- costruzione
printf '\033[1m== pacchetto del rilascio %s ==\033[0m\n' "${COMMIT_QUALIFICATO:0:12}"

# Lo stato dell'albero di lavoro si REGISTRA, non blocca: ogni file esce
# da `git show <commit>:<path>`, quindi una modifica non committata non
# può finire nel pacchetto nemmeno volendo. Rifiutare qui sarebbe un
# controllo che non protegge da niente — e che insegna a ignorarlo.
SPORCO="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
if [ "$SPORCO" != 0 ]; then
    printf '  nota: albero di lavoro con %s voci non committate.\n' "$SPORCO"
    printf '        irrilevante per il pacchetto: si estrae da %s, non dal disco.\n' "${COMMIT_QUALIFICATO:0:12}"
fi

# Il commit da cui si estrae. Se HEAD non è quello qualificato lo si dice:
# i commit successivi alla qualificazione hanno lasciato l'albero del
# rilascio identico, ma è una cosa da VERIFICARE, non da assumere.
HEAD_SHA="$(git rev-parse HEAD)"
if [ "$HEAD_SHA" = "$COMMIT_QUALIFICATO" ]; then
    ok "HEAD è il commit qualificato"
else
    # Il confronto si fa SOLO sui file dell'identità: è la domanda giusta.
    DIFF="$(git diff --name-only "$COMMIT_QUALIFICATO..HEAD" -- "${IDENTITA[@]}" 2>/dev/null || true)"
    if [ -z "$DIFF" ]; then
        ok "HEAD è ${HEAD_SHA:0:12}; nessun file dell'identità del rilascio è cambiato"
    else
        ko "file dell'identità del rilascio modificati dopo la qualificazione:" \
           "$(printf '%s' "$DIFF" | tr '\n' ' ')"
        ko "l'identità del rilascio è cambiata: rieseguire i cancelli interessati"
        exit 1
    fi
fi

mkdir -p "$DEST" || exit 1
: > "$DEST/SHA256SUMS"
estrai() { # commit percorso etichetta
    local rev="$1" f="$2" et="$3" sha
    if ! git cat-file -e "${rev}:${f}" 2>/dev/null; then
        ko "non presente in ${rev:0:12}: $f"; return
    fi
    mkdir -p "$DEST/$(dirname "$f")"
    # `git show` restituisce il BLOB, con i fine-riga del commit: è la
    # ragione d'essere di questo script.
    git show "${rev}:${f}" > "$DEST/$f" || { ko "estrazione fallita: $f"; return; }
    sha="$(sha256sum < "$DEST/$f" | awk '{print $1}')"
    printf '%s  %s\n' "$sha" "$f" >> "$DEST/SHA256SUMS"
    ok "$(printf '%-9s' "$et") $f  ${sha:0:12}…"
}
printf '  file dell'\''identità, dal commit qualificato:\n'
for f in "${IDENTITA[@]}";      do estrai "$COMMIT_QUALIFICATO" "$f" identità; done
printf '  documentazione, da HEAD (fuori dall'\''identità del rilascio):\n'
for f in "${DOCUMENTAZIONE[@]}"; do estrai "$HEAD_SHA" "$f" doc; done
chmod +x "$DEST/deploy/preflight.sh" 2>/dev/null || true

# La baseline estratta DEVE avere il digest pinnato. È il controllo che
# rende falsificabile tutto il resto.
B="$DEST/deploy/baseline/production-baseline.json"
if [ -f "$B" ]; then
    SHA="$(sha256sum < "$B" | awk '{print $1}')"
    [ "$SHA" = "$BASELINE_FILE" ] \
        && ok "baseline estratta = file qualificato ${SHA:0:16}…" \
        || ko "la baseline estratta ha digest $SHA" "atteso $BASELINE_FILE"
fi

# Rifiuto meccanico dei vietati.
for v in "${VIETATI[@]}"; do
    [ -e "$DEST/$v" ] && ko "il pacchetto contiene un artefatto vietato: $v"
done
# E nessuna directory di segreti: quelli si generano SULL'host.
for d in secrets .git tools; do
    [ -e "$DEST/$d" ] && ko "il pacchetto contiene $d: non deve viaggiare"
done
ok "nessun segreto, nessun certificato, nessun checkout git nel pacchetto"
printf '        i segreti si generano sull'\''host; il certificato aziendale si installa là.\n'

# ---------------------------------------------------------------- immagini
if [ "$IMMAGINI" = 1 ]; then
    printf '\n  immagini:\n'
    A="$(docker image inspect --format '{{.Id}}' tsm-api:0.2.0 2>/dev/null || echo assente)"
    W="$(docker image inspect --format '{{.Id}}' tsm-web:0.2.0 2>/dev/null || echo assente)"
    if [ "$A" != "$ID_API" ] || [ "$W" != "$ID_WEB" ]; then
        ko "le immagini locali non sono quelle qualificate" \
           "api $A / web $W — le immagini del rilascio si costruiscono su Linux da git archive"
        printf '        non le impacchetto: un archivio con immagini sbagliate è peggio di nessun archivio.\n'
    else
        docker save -o "$DEST/tsm-images.tar" \
            "postgres@${DIGEST_DB}" tsm-api:0.2.0 tsm-web:0.2.0 \
            && ok "tsm-images.tar ($(du -h "$DEST/tsm-images.tar" | cut -f1))" \
            || ko "docker save fallito"
        if [ -f "$DEST/tsm-images.tar" ]; then
            sha256sum "$DEST/tsm-images.tar" | awk '{print $1"  tsm-images.tar"}' >> "$DEST/SHA256SUMS"
        fi
    fi
fi

printf '\n'
if [ "$KO" = 0 ]; then
    printf '  pacchetto pronto in %s — %d file, SHA256SUMS incluso.\n' "$DEST" "$(( ${#IDENTITA[@]} + ${#DOCUMENTAZIONE[@]} ))"
    printf '  sull'\''host: tools/release-bundle.sh --verify /opt/tsm\n'
    exit 0
fi
printf '  %d problemi: il pacchetto NON è utilizzabile.\n' "$KO"
exit 1
