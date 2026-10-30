#!/bin/bash
# =====================================================================
# Installa (o aggiorna) il ripristino automatico di TSM su tsm-prd.
# Da root, dalla cartella ops/ copiata sulla VM:
#
#     bash /opt/tsm-ops/install-autoheal.sh
#
# Rilanciarlo è sicuro: reinstalla i file e riattiva il timer. Per toglierlo:
#     systemctl disable --now tsm-autoheal.timer
# Dettagli: ops/README.md §3.
# =====================================================================
set -euo pipefail
QUI="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "va lanciato da root" >&2; exit 1; }
[ -d /opt/tsm ] || { echo "/opt/tsm non esiste: installare prima TSM" >&2; exit 1; }

install -d -m 0755 /usr/local/lib/tsm
install -m 0755 -o root -g root "$QUI/autoheal/tsm-autoheal.sh" /usr/local/lib/tsm/
install -m 0644 -o root -g root "$QUI/autoheal/tsm-autoheal.service" /etc/systemd/system/
install -m 0644 -o root -g root "$QUI/autoheal/tsm-autoheal.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tsm-autoheal.timer

echo "--- prova a mano (nessuna riga = tutto in ordine) ---"
/usr/local/lib/tsm/tsm-autoheal.sh
echo "--- prossima esecuzione ---"
systemctl list-timers tsm-autoheal.timer --no-pager
echo "OK: ripristino automatico attivo (ogni 2 minuti). Log: journalctl -t tsm-autoheal"
