# TSM — lato sistemistico (`ops/`)

Tutto ciò che riguarda la **macchina** `tsm-prd` e non l'applicazione: monitoraggio
Nagios, elenco FIM, ripristino automatico. Non fa parte del pacchetto di rilascio
(`tools/release-bundle.sh` non lo include) e si installa una volta, a mano, da
root. Un aggiornamento dell'applicazione non lo tocca.

| cartella | contenuto | si installa in |
|---|---|---|
| `nagios/` | `check_tsm.sh`, `nrpe-tsm.cfg`, `sudoers-tsm` | `/usr/local/lib/tsm/`, `/etc/nrpe.d/`, `/etc/sudoers.d/` |
| `autoheal/` | `tsm-autoheal.sh`, `.service`, `.timer` | `/usr/local/lib/tsm/`, `/etc/systemd/system/` |

Copia sulla VM (dal PC, come per il pacchetto):

```bash
# sulla VM, da root, con la cartella ops/ copiata in /tmp/ops
install -d -m 0755 /opt/tsm-ops
cp -r /tmp/ops/. /opt/tsm-ops/
```

Gli script eseguiti come root stanno in `/usr/local/lib/tsm` e non in
`/opt/tsm-ops`: chi modifica la copia di lavoro non cambia il codice che gira come
root senza reinstallarlo.

## 1. Nagios (NRPE)

I controlli di `deploy/README.md` §5 (servizi, container, disco, battito del worker),
pronti per NRPE, stanno in `nagios/check_tsm.sh`.
Lo script parla con Docker, e parlare con Docker equivale a essere root: per questo
l'utente `nrpe` **non** entra nel gruppo `docker`, ma esegue tramite `sudo` solo
quello script e solo con gli argomenti previsti.

```bash
install -d -m 0755 /usr/local/lib/tsm
install -m 0755 -o root -g root /opt/tsm-ops/nagios/check_tsm.sh /usr/local/lib/tsm/
install -m 0440 -o root -g root /opt/tsm-ops/nagios/sudoers-tsm /etc/sudoers.d/tsm-nrpe
visudo -cf /etc/sudoers.d/tsm-nrpe
install -m 0644 /opt/tsm-ops/nagios/nrpe-tsm.cfg /etc/nrpe.d/tsm.cfg
grep -q '^include_dir=/etc/nrpe.d' /etc/nagios/nrpe.cfg || echo 'include_dir=/etc/nrpe.d/' >> /etc/nagios/nrpe.cfg
systemctl restart nrpe
sudo -u nrpe /usr/bin/sudo -n /usr/local/lib/tsm/check_tsm.sh containers    # prova
```

Lo script sta in `/usr/local/lib/tsm` e non in `/opt/tsm`: un aggiornamento del
pacchetto non cambia il codice che `sudo` esegue senza che qualcuno lo reinstalli.

| comando NRPE | controlla | intervallo suggerito |
|---|---|---|
| `check_tsm_service` | `docker.service` e `tsm.service` attivi | 5 min |
| `check_tsm_containers` | db/api/web/worker `running` e `healthy`, riavvii in perfdata | 5 min |
| `check_tsm_storage` | `/srv/tsm-data` montato, `rw`, occupazione 75/90 % | 5 min |
| `check_tsm_ready` | `/api/ready` attraverso nginx | 5 min |
| `check_tsm_worker` | battito del worker e proiezione utilizzabile (controlli 0 e 5) | 15 min |
| `check_tsm_runs` | giri delle notifiche aperti da oltre un'ora (controllo 6) | 60 min |
| `check_tsm_cert` | scadenza del certificato, 30/7 giorni | 1 giorno |
| `check_tsm_images` | i container girano sulle immagini del pin | 1 giorno |

Dal **server Nagios**, oltre a NRPE, un controllo diretto: è l'unico che prova
anche la rete fra gli operatori e la VM.

```text
check_http -H 10.11.43.230 -S --sni -u /api/ready -s '"ready"'
check_http -H 10.11.43.230 -S --sni -C 30,7
```

## 2. FIM (integrità dei file)

| percorso | perché | |
|---|---|---|
| `/opt/tsm` (tutto, compresi `secrets/` e `.env`) | configurazione, segreti, certificato, preflight: cambiano solo con un rilascio o una rotazione | **monitorare** |
| `/etc/systemd/system/tsm.service` e `tsm.service.d/` | cosa parte all'avvio, e con quale preflight | **monitorare** |
| `/etc/docker/` | configurazione del demone | **monitorare** |
| `/etc/fstab` | il mount del disco dati: se sparisce, i dati vanno sul disco sbagliato | **monitorare** |
| `/etc/sudoers.d/tsm-nrpe`, `/etc/nrpe.d/tsm.cfg`, `/usr/local/lib/tsm/` | ciò che `nrpe` e il timer di ripristino eseguono come root | **monitorare** |
| `/etc/systemd/system/tsm-autoheal.service`, `tsm-autoheal.timer` | il ripristino automatico (§3) | **monitorare** |
| `/var/lib/tsm-autoheal/` | contatori del ripristino: cambiano a ogni intervento | **escludere** |
| `/root/tsm-ca/` (finché c'è) | la chiave della CA interna: chi la cambia può emettere certificati fidati | **monitorare** — meglio ancora, spostarla fuori dalla VM |
| `/usr/bin/docker*`, `/usr/bin/containerd*`, `/usr/libexec/docker/` | binari | già coperti dal profilo OS standard |
| `/srv/tsm-data/postgres` | file del database: cambiano di continuo | **escludere** |
| `/var/lib/docker` | livelli delle immagini, stato e log dei container | **escludere** |

L'integrità delle **immagini** non è compito del FIM (i livelli sotto
`/var/lib/docker` cambiano a ogni avvio dei container): la dà `check_tsm_images`, che
confronta gli Id in esecuzione con quelli del pin.


## 3. Ripristino automatico (`tsm-autoheal`)

Docker Compose non è Kubernetes: nessuno ricrea un container che non risponde.
Cosa succede già da solo e cosa no:

| guasto | chi lo ripara senza questo timer |
|---|---|
| un container esce (crash, OOM) | Docker, per `restart: unless-stopped` |
| riavvio della VM | systemd: `docker.service` e `tsm.service` sono abilitati |
| `docker.service` cade | systemd (`Restart=always`), ma **si arrende dopo 3 fallimenti in 1 minuto** |
| container vivo ma `unhealthy` | **nessuno**: Docker lo segnala e basta |
| `tsm.service` fermato perché è caduto Docker (`Requires=`) | **nessuno**: i container ripartono, l'unità resta inattiva |

`tsm-autoheal` copre le ultime tre righe. Ogni 2 minuti: riavvia Docker se è
fermo, riavvia `tsm.service` se non è attivo, e per db/api/web/worker fa
`docker compose up -d` se il container non gira o `docker compose restart` se è
`unhealthy`. Massimo **3 interventi all'ora per oggetto**: oltre, smette e scrive un
errore nel journal — un guasto che ritorna va guardato, non riavviato in loop.
Nagios (`check_tsm_containers`, `check_tsm_service`) lo vede comunque.

```bash
install -m 0755 -o root -g root /opt/tsm-ops/autoheal/tsm-autoheal.sh /usr/local/lib/tsm/
install -m 0644 /opt/tsm-ops/autoheal/tsm-autoheal.service /etc/systemd/system/
install -m 0644 /opt/tsm-ops/autoheal/tsm-autoheal.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tsm-autoheal.timer
systemctl list-timers tsm-autoheal.timer          # prossima esecuzione
/usr/local/lib/tsm/tsm-autoheal.sh                # prova a mano: nessun output = tutto a posto
journalctl -t tsm-autoheal --since today          # cosa ha fatto
```

**Manutenzione.** Prima di fermare TSM di proposito (aggiornamento, prove):

```bash
systemctl stop tsm-autoheal.timer     # altrimenti entro 2 minuti lo riavvia
# ... lavoro ...
systemctl start tsm-autoheal.timer
```

Un `tsm.service` **disabilitato** (`systemctl disable tsm`) è considerato una scelta e
non viene toccato. Se il preflight rifiuta l'avvio, il timer non insiste oltre il
limite orario: il motivo è strutturale (disco, certificato) e va letto in
`systemctl status tsm`.
