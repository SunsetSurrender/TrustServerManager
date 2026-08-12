# Messa in produzione — `tsm-prd-01`

Archiviazione, avvio del servizio e cosa fare quando il disco dei dati non c'è.
Riferimento di progetto: `BACKEND-PLAN.md` §8.40.

Questo documento descrive operazioni **manuali e deliberate**. Niente qui viene
eseguito automaticamente dall'applicazione: partizionare e formattare un disco è
una decisione di chi amministra, non un effetto collaterale dell'avvio di un
servizio.

---

## 1. La macchina

| | |
|---|---|
| hostname | `tsm-prd-01` |
| vCPU | 4 |
| RAM | 8 GB |
| swap | 8 GB, **file di swap sul disco 1** |
| Disco 1 | 100 GB thin — sistema operativo, `/var/lib/docker`, immagini, livelli dei container, log, swap |
| Disco 2 | 100 GB thin — **solo** dati durevoli di PostgreSQL |
| mount del disco 2 | `/srv/tsm-data` |
| dati PostgreSQL | `/srv/tsm-data/postgres` |
| installazione | `/opt/tsm` |

```text
Disco 1
/
└── 8 GB file di swap

Disco 2
/srv/tsm-data
└── postgres        ← dati di PostgreSQL
```

### Perché due dischi

Separare i dati dal sistema è ciò che rende indipendenti due guasti che altrimenti
si trascinerebbero: le immagini Docker che crescono non possono riempire il disco
del database, e il database che cresce non può impedire al sistema di scrivere i
log o di fare swap. È anche ciò che rende sensato parlare di «volume dei dati» nel
backup.

`/var/lib/docker` **resta sul disco 1**. Le immagini e i livelli dei container si
ricostruiscono o si ricaricano; i dati no. Metterli sullo stesso disco
riporterebbe il problema al punto di partenza, e il preflight rifiuta l'avvio se
la radice di Docker finisce sotto `/srv/tsm-data`.

### Backup

Il backup è a livello di VM, con Veeam. **Entrambi i dischi virtuali devono
appartenere alla VM protetta e rientrare nel job di backup.** Un job che copia
solo il disco di sistema produce un ripristino che parte, sembra sano e non ha
dentro nessun dato: è il tipo di backup che si scopre incompleto il giorno del
ripristino.

Da verificare nella configurazione del job:

- la VM `tsm-prd-01` è inclusa;
- **nessuna esclusione di dischi** (in Veeam: *VM object → Disks → All disks*);
- il ripristino viene provato almeno una volta, e dopo il ripristino
  `findmnt -M /srv/tsm-data` mostra ancora un filesystem dedicato.

---

## 2. Preparazione del disco 2

⚠ Tutti i comandi vanno eseguiti come `root`. Il disco 2 viene **cancellato**:
verificare di stare lavorando sul disco giusto prima di continuare.

### 2.1 Identificare il disco

```bash
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE
```

Il disco 2 è quello da 100 GB senza filesystem e senza punto di mount. Nel
seguito è `/dev/sdb`: **sostituire con il nome reale**.

### 2.2 Partizione e filesystem

XFS è il filesystem predefinito di Oracle Linux e la scelta appropriata qui: si
espande a caldo con `xfs_growfs`, cosa che su un disco thin provisioned prima o
poi serve.

```bash
# tabella GPT e una sola partizione che occupa tutto il disco
parted -s /dev/sdb mklabel gpt
parted -s /dev/sdb mkpart primary xfs 1MiB 100%
partprobe /dev/sdb

mkfs.xfs -L tsm-data /dev/sdb1
```

### 2.3 `/etc/fstab` con l'UUID, non con `/dev/sdb1`

```bash
blkid /dev/sdb1
# /dev/sdb1: LABEL="tsm-data" UUID="....-....-...." TYPE="xfs" PARTUUID="..."
```

`/dev/sdb1` **non è un'identità stabile**: dipende dall'ordine con cui il kernel
enumera i controller. Aggiungere un disco, spostare la VM su un altro host o
cambiare il tipo di controller virtuale può rinominare `sdb` in `sdc`. A quel
punto `/etc/fstab` monta il disco sbagliato oppure non monta niente — e nel
secondo caso, senza `RequiresMountsFor`, il servizio partirebbe scrivendo sul
filesystem di root. L'UUID appartiene al filesystem e lo segue.

Riga da aggiungere a `/etc/fstab` (una sola riga):

```text
UUID=<quello di blkid>  /srv/tsm-data  xfs  defaults,nofail,x-systemd.device-timeout=30s  0  0
```

- `nofail` — la macchina si avvia comunque se il disco manca, invece di fermarsi
  in emergency shell. Non è un permesso ad avviare TSM: ci pensano
  `RequiresMountsFor` e il preflight, che rifiutano. Il risultato è una macchina
  raggiungibile su cui si può diagnosticare, invece di una console da aprire.
- `x-systemd.device-timeout=30s` — evita un'attesa di 90 secondi se il device non
  compare.

```bash
mkdir -p /srv/tsm-data
mount -a
```

### 2.4 Verificare che sia montato davvero

```bash
findmnt -M /srv/tsm-data
# TARGET        SOURCE    FSTYPE OPTIONS
# /srv/tsm-data /dev/sdb1 xfs    rw,relatime,...
```

Se `findmnt` non stampa niente, `/srv/tsm-data` è una directory sul filesystem di
root e **non** un punto di mount. Non proseguire.

Controprova utile: i due numeri devono essere diversi.

```bash
stat -c %d /srv/tsm-data /
```

### 2.5 Directory dei dati, con l'uid dell'immagine

L'uid di PostgreSQL **si legge dall'immagine pinnata**, non si assume.

Quanto conti, si vede subito: nell'immagine `postgres:17-alpine` usata qui l'uid
è **70**, non il 999 delle immagini basate su Debian che quasi tutte le guide
danno per scontato. Scrivere 999 nel runbook avrebbe prodotto una directory di
proprietà di un utente inesistente, e PostgreSQL non sarebbe riuscito a scrivere
— con un messaggio che parla di permessi e non di uid. È una proprietà
dell'immagine e cambia con la distribuzione di base: si interroga, ogni volta.

```bash
cd /opt/tsm
IMG=$(docker compose config --images | grep -i postgres)
PG_UID=$(docker run --rm --entrypoint id "$IMG" -u postgres)
PG_GID=$(docker run --rm --entrypoint id "$IMG" -g postgres)
echo "postgres nell'immagine: ${PG_UID}:${PG_GID}"

install -d -o "$PG_UID" -g "$PG_GID" -m 0700 /srv/tsm-data/postgres
ls -ld /srv/tsm-data/postgres
```

`0700` non è prudenza generica: PostgreSQL **rifiuta di partire** se la directory
dei dati ha permessi più larghi di `0750`.

`postgres` è una **sottodirectory** del punto di mount, non il punto di mount
stesso. Un filesystem appena creato non è vuoto — XFS e ext4 hanno metadati
propri, ext4 ha `lost+found` — e `initdb` rifiuta una directory non vuota.

### 2.6 SELinux: etichetta **persistente**

Su Oracle Linux SELinux è enforcing per default. Un container non può scrivere in
una directory dell'host che non ha un tipo adatto, anche con permessi perfetti.

```bash
getenforce            # Enforcing

# regola PERSISTENTE (sopravvive a una rietichettatura)
semanage fcontext -a -t container_file_t '/srv/tsm-data/postgres(/.*)?'

# applicazione della regola ai file esistenti
restorecon -Rv /srv/tsm-data/postgres

ls -lZd /srv/tsm-data/postgres
# ... system_u:object_r:container_file_t:s0 ...
```

**Non usare solo `chcon`.** `chcon` cambia l'etichetta adesso e non lascia
traccia nella policy: alla prima rietichettatura del filesystem — un
`restorecon -R /`, un `touch /.autorelabel`, un aggiornamento della policy —
l'etichetta torna quella di default e PostgreSQL smette di scrivere. Il servizio
avrebbe funzionato per mesi, e il guasto arriverebbe in un giorno in cui nessuno
ha toccato TSM. Il preflight verifica anche la presenza della regola in
`semanage`, non solo l'etichetta corrente.

Se `semanage` non c'è: `dnf install -y policycoreutils-python-utils`.

### 2.7 File di swap da 8 GB sul disco 1

```bash
fallocate -l 8G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=8192
chmod 0600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show
```

Lo swap sta sul **disco 1**. Sul disco 2 sottrarrebbe spazio ai dati e
mescolerebbe traffico di paginazione con l'I/O del database.

---

## 3. Installazione del servizio

```bash
# il contenuto del repository va in /opt/tsm
install -d -m 0755 /opt/tsm
# (copia del repository, oppure git clone in rete raggiungibile)

cd /opt/tsm
chmod 0750 deploy/preflight.sh
```

### 3.1 Secret e TLS

I secret sono file montati, mai variabili d'ambiente (`backend/README.md`). Devono
essere leggibili **solo** dal proprietario, e il proprietario è l'utente del
container che li legge (uid 10001 per l'API).

```bash
API_UID=$(docker image inspect --format '{{.Config.User}}' tsm-api:0.2.0 | cut -d: -f1)

openssl rand -base64 24 | tr -d '\n+/=' > secrets/postgres_password
openssl rand -base64 24 | tr -d '\n+/=' > secrets/api_db_password
# Ruolo del worker, distinto da quello dell'API: la GC delle foto ha bisogno di
# DELETE su `photos`, che è l'unico privilegio di cancellazione dello schema, e
# l'API non deve averlo (§8.5). Il preflight si ferma se questo file manca.
openssl rand -base64 24 | tr -d '\n+/=' > secrets/worker_db_password
: > secrets/smtp_password          # può restare VUOTO se il relay non autentica

SECRETS="secrets/postgres_password secrets/api_db_password \
secrets/worker_db_password secrets/smtp_password"
chown "${API_UID}:${API_UID}" $SECRETS
chmod 0400 $SECRETS
```

> L'uid è lo stesso per API e worker: girano dalla stessa immagine. Sono i
> **privilegi nel database** a distinguerli, non l'utente del sistema operativo.

Certificato aziendale, con la chiave leggibile dall'utente di nginx (uid 101):

```bash
install -d -m 0755 secrets/tls
cp /percorso/fullchain.pem secrets/tls/fullchain.pem
cp /percorso/privkey.pem   secrets/tls/privkey.pem

WEB_UID=$(docker image inspect --format '{{.Config.User}}' tsm-web:0.2.0 | cut -d: -f1)
chown "${WEB_UID}:${WEB_UID}" secrets/tls/privkey.pem
chmod 0444 secrets/tls/fullchain.pem
chmod 0400 secrets/tls/privkey.pem
```

### 3.2 Immagini in rete chiusa

La macchina non raggiunge internet: le immagini si trasferiscono, non si scaricano.

```bash
# su una macchina con rete
docker compose pull && docker compose build
docker save -o tsm-images.tar postgres@sha256:742f... tsm-api:0.2.0 tsm-web:0.2.0

# su tsm-prd-01
docker load -i tsm-images.tar
```

Il preflight rifiuta l'avvio se un'immagine attesa non è presente in locale, e non
tenta di scaricarla per conto proprio.

### 3.3 Preflight a mano, prima di systemd

```bash
/opt/tsm/deploy/preflight.sh --dir /opt/tsm
echo "esito: $?"
```

Va eseguito e superato **prima** di installare l'unità. Un preflight che passa a
mano rende l'eventuale fallimento dell'unità un problema di systemd e non di
archiviazione, che è già metà della diagnosi.

Codici di uscita: vedi l'intestazione di `deploy/preflight.sh`. In breve:
`20`–`28` archiviazione, `30`–`31` secret e TLS, `40`–`42` configurazione non di
produzione, `10`–`12` Docker.

### 3.4 Unità systemd

```bash
cp /opt/tsm/deploy/tsm.service /etc/systemd/system/tsm.service
systemctl daemon-reload
systemctl enable --now tsm.service

systemctl status tsm.service
journalctl -u tsm.service -n 50 --no-pager
```

`RequiresMountsFor=/srv/tsm-data` fa dedurre a systemd l'unità di mount e la rende
una dipendenza necessaria: senza il disco, l'unità non parte.

```bash
# come systemd chiama quel mount
systemctl list-units --type=mount | grep tsm
# srv-tsm\x2ddata.mount
```

### 3.5 Bootstrap dell'inventario (una volta sola)

Non passa da HTTP e non passa dall'unità: è un'operazione esplicita
(`BACKEND-PLAN.md` §8.17).

```bash
cd /opt/tsm
TSM_BOOTSTRAP_PASSWORD='<password iniziale>' \
docker compose run --rm -v /opt/tsm/fixtures:/seed:ro migrate \
  python scripts/bootstrap.py --seed /seed/seed.json --admin admin --from-legacy
```

### 3.6 Worker delle notifiche

Parte insieme allo stack: è un servizio Compose (`worker`) con la stessa immagine
dell'API e un comando diverso. Non pubblica porte e vive solo sulla rete interna.

```bash
docker compose ps worker
docker compose logs -f worker

# stato leggibile da un sistema di monitoraggio
docker compose exec -T worker python scripts/worker_health.py --json
# {"healthy": true, "state": "running", "ageSeconds": 12.4, "lastRunDate": "2026-08-11", ...}
```

**Deve esistere un solo worker.** `replicas: 1` è una dichiarazione d'intenti; la
garanzia è un lock consultivo nel database, quindi un secondo processo — anche
lanciato a mano con `docker compose run` — esce dicendo perché invece di mandare
avvisi doppi.

Le notifiche si configurano dall'interfaccia (Impostazioni → Notifiche scadenze):
destinatari, finestre di preavviso, fuso orario e ora di invio. Con
`notifications.enabled = false` il worker gira e non manda niente.

Verifica dell'invio senza aspettare l'ora pianificata: il pulsante «Invia
messaggio di prova» nell'interfaccia (limitato, §8.38). Per un giro reale forzato
in diagnostica:

```bash
docker compose exec -T worker python -c "
from datetime import datetime, timezone
from app.db import get_engine
from app.notifications.worker import run_once
print(run_once(get_engine(), now_utc=datetime.now(timezone.utc)))"
```

Un giro già eseguito oggi risponde `already_ran_today` e non manda niente: la
protezione è nel database, non nella memoria del processo.

### 3.7 Manutenzione: garbage collection delle foto

Lo stesso processo `worker` esegue anche la raccolta delle foto orfane (§8.5), ma
è un lavoro **indipendente**: tabella di esecuzioni propria (`maintenance_runs`),
orario proprio (03:30 locali), e nessuna dipendenza dallo stato delle notifiche —
spegnere gli avvisi non deve riempire il disco.

Cancella solo una foto che soddisfa **entrambe** le condizioni:

```text
    nessuna versione dell'inventario la referenzia (inventory_photo_refs)
  E è più vecchia di 24 ore
```

La finestra di grazia copre le foto legittimamente orfane: caricate e non ancora
salvate nel rack — cosa che succede a ogni conflitto sul salvataggio e a ogni
modulo chiuso a metà. **Una foto referenziata da una versione vecchia è viva**,
anche se l'inventario corrente non la usa più: serve a un eventuale ripristino.

```bash
# ultimo giro di GC
docker compose exec -T worker python scripts/worker_health.py --json
# ... "photoGc": {"lastRunDate": "2026-08-11", "examined": 12, "deleted": 2, ...}

# spazio occupato dalle foto, e quante sono orfane
docker compose exec -T db psql -U tsm -d tsm -c "
  SELECT count(*) AS foto,
         pg_size_pretty(sum(size_bytes)) AS spazio,
         count(*) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM inventory_photo_refs r WHERE r.photo_id = p.id)) AS orfane
    FROM photos p"
```

⚠ **Non cancellare righe da `photos` a mano.** Il ruolo dell'API non ne ha il
privilegio, e la chiave esterna dei riferimenti rifiuta comunque la cancellazione
di una foto ancora usata — anche al proprietario dello schema. Se serve liberare
spazio, il modo è la potatura delle versioni storiche (non ancora implementata):
eliminando una versione se ne eliminano i riferimenti, e solo allora le sue foto
diventano raccoglibili.

Se un salvataggio dell'inventario risponde `photo_not_found`, il documento
referenzia una foto che non esiste: non è un guasto del server, è un client che
sta salvando un UUID che non ha caricato (o che la GC ha già raccolto perché il
salvataggio era rimasto indietro di più di ventiquattro ore).

### 3.8 Tabelle normalizzate: presenti e VUOTE (fase 2A)

La migrazione `0010_normalised` crea le tabelle dello stato operativo
(`inventory_locations`, `inventory_rooms`, `inventory_racks`,
`inventory_devices`, `inventory_manual_entries`, `inventory_state`). **Nessuno le
popola e nessuno le legge**: `GET` e `PUT` continuano a lavorare sull'istantanea
JSON, come prima (§8.42).

Non c'è niente da fare in fase di deployment, e vedere le tabelle vuote è lo stato
corretto:

```bash
docker compose exec -T db psql -U tsm -d tsm -c "
  SELECT count(*) AS siti FROM inventory_locations"
# 0

docker compose exec -T db psql -U tsm -d tsm -c "
  SELECT count(*) AS stato FROM inventory_state"
# 0   -- nessuna riga = la proiezione non rispecchia nessuna versione
```

I ruoli di runtime hanno **solo `SELECT`** su queste tabelle: i privilegi di
scrittura arrivano con la fase 2C, insieme al codice che sincronizza. Il
popolamento della fase 2B girerà come proprietario dello schema e si fermerà da
sé se il documento riassemblato da SQL non darà lo stesso digest dell'istantanea
in testa.

⚠ Non popolarle a mano. La verifica del digest è la sola prova che la proiezione è
fedele, e una `INSERT` fatta a mano la salta.

---

## 4. Il caso che conta: disco dei dati assente

Da provare **prima** di mettere il servizio in esercizio, e da ripetere dopo ogni
modifica all'archiviazione. È il controllo portante.

```bash
systemctl stop tsm.service
umount /srv/tsm-data

# la directory rimane, VUOTA, sul filesystem di root: è il caso peggiore,
# perché "esiste" e sembra a posto
ls -ld /srv/tsm-data
findmnt -M /srv/tsm-data || echo "non è un mount — corretto"

systemctl start tsm.service; echo "esito: $?"
```

Atteso:

```text
il preflight fallisce con codice 20
PostgreSQL non parte
nessun file di pgdata compare sul filesystem di root
```

Verifica che nulla sia stato scritto su `/`:

```bash
ls -A /srv/tsm-data            # deve essere vuoto
find /srv/tsm-data -mindepth 1 | head
docker volume inspect tsm_pgdata --format '{{.Options.device}}'
```

Ripristino, che deve tornare a funzionare:

```bash
mount -a
findmnt -M /srv/tsm-data
systemctl start tsm.service
systemctl status tsm.service
curl -sk https://localhost/api/ready
```

Lo stesso scenario è automatizzato in `tools/storage-e2e-test.ps1`, che lo esegue
sia nel verso giusto sia in quello sbagliato: mostra che **senza** il preflight
PostgreSQL inizializza davvero sul filesystem di root, e che **con** il preflight
l'avvio viene rifiutato.

---

## 5. Se il disco sparisce a servizio avviato

`RequiresMountsFor` protegge l'**avvio**. Non protegge da un disco che scompare
dopo, e nessuna configurazione di systemd rende sicura la rimozione a caldo di un
filesystem sotto un database in esecuzione: PostgreSQL ha descrittori aperti,
pagine sporche in memoria e un WAL da scrivere. Da quel momento la sola cosa
sensata è **fermarsi**, non arrangiarsi.

L'applicazione non tenta alcun recupero automatico, e in particolare non
reinizializza né ricrea niente: un `initdb` automatico su una directory vuota
trasformerebbe un guasto di archiviazione in una perdita di dati.

**Comportamento atteso**

| | |
|---|---|
| Monitoraggio | allarme **immediato** se `/srv/tsm-data` non è più un mount o è passato in sola lettura |
| Readiness | `/api/ready` deve fallire: dipende dal database, e il database non scrive |
| Amministratore | **fermare** lo stack, non provare a scrivere su un mount degradato |

```bash
# fermare, poi diagnosticare
systemctl stop tsm.service

dmesg -T | tail -40
findmnt -M /srv/tsm-data -o TARGET,SOURCE,FSTYPE,OPTIONS
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE
```

Solo dopo che il filesystem è di nuovo montato e leggibile/scrivibile:

```bash
mount -a
/opt/tsm/deploy/preflight.sh --dir /opt/tsm
systemctl start tsm.service
```

Un ripristino da Veeam è preferibile a qualunque tentativo di riparazione a mano
se `dmesg` mostra errori di I/O o se `xfs_repair` è stato necessario.

### Controlli di monitoraggio da configurare

```bash
# 0. il worker delle notifiche fa ancora giri?  (§8.41)
#    Esce 1 se il battito è vecchio o lo stato non è sano. NON va legato alla
#    prontezza dell'API: un worker fermo non è un'interruzione di servizio.
docker compose exec -T worker python scripts/worker_health.py \
  || alert "TSM: worker delle notifiche fermo"

# 1. è ancora un mount dedicato?  (critico, immediato)
findmnt -M /srv/tsm-data >/dev/null || alert "TSM: /srv/tsm-data non è montato"

# 2. è ancora scrivibile?  (critico, immediato)
findmnt -M /srv/tsm-data -o OPTIONS -n | grep -qw rw \
  || alert "TSM: /srv/tsm-data in sola lettura"

# 3. readiness dell'applicazione
curl -fsk https://tsm-prd-01/api/ready >/dev/null || alert "TSM: not ready"

# 4. occupazione (vedi soglie sotto)
df --output=pcent /srv/tsm-data | tail -1
```

---

## 6. Spazio: soglie e il tranello del thin provisioning

| Dove | Soglia | Effetto |
|---|---|---|
| preflight | **meno di 5 GiB liberi** | l'avvio viene **rifiutato** (codice 26) |
| monitoraggio | uso **70–75%** | allerta |
| monitoraggio | uso **85–90%** | critico |

Il preflight avvisa anche quando l'uso supera 70% e 85%, ma **non** blocca: un
servizio che sta lavorando bene non si rifiuta di riavviare perché il disco è
pieno all'86%. Blocca solo sotto i 5 GiB liberi, che è la soglia oltre la quale il
recupero diventa lungo — un PostgreSQL che esaurisce lo spazio si ferma in
scrittura.

⚠ **100 GB nel guest non sono 100 GB sul datastore.** I dischi sono thin
provisioned: il guest vede la dimensione dichiarata, mentre il datastore VMware
alloca i blocchi man mano. Un datastore pieno si manifesta nel guest come errori
di I/O o come un filesystem improvvisamente in sola lettura, **con `df` che
mostra spazio libero in abbondanza**. Nessun controllo dentro la VM può vederlo:
la capacità del datastore resta responsabilità del monitoraggio
dell'infrastruttura, e va sorvegliata separatamente.

Crescita a caldo del disco 2, dopo un'espansione lato VMware:

```bash
echo 1 > /sys/class/block/sdb/device/rescan
parted -s /dev/sdb resizepart 1 100%
xfs_growfs /srv/tsm-data
df -h /srv/tsm-data
```

---

## 7. Riepilogo dei controlli del preflight

| Codice | Prerequisito |
|---|---|
| 10 | `docker` nel PATH e demone attivo |
| 11 | `docker compose config` valido |
| 12 | immagini attese presenti in locale (nessun download automatico) |
| 20 | `/srv/tsm-data` **è** un punto di mount |
| 21 | non è il filesystem di root (confronto di sorgente **e** di `st_dev`) |
| 22 | `/srv/tsm-data/postgres` esiste |
| 23 | proprietario = uid/gid di `postgres` **letti dall'immagine** |
| 24 | permessi `0700` o `0750` |
| 25 | scrittura **provata** come utente di PostgreSQL attraverso Docker |
| 26 | almeno 5 GiB liberi |
| 27 | tipo SELinux `container_file_t` quando enforcing |
| 28 | etichetta SELinux **persistente** (`semanage`, non solo `chcon`) |
| 30 | secret presenti, `0400`/`0600`, di proprietà dell'utente del container |
| 31 | certificato e chiave presenti, chiave leggibile da nginx |
| 40 | `TSM_ENV=production` e cookie `Secure` |
| 41 | `db` e `api` non pubblicano porte sull'host |
| 42 | `pgdata` ancorato a `/srv/tsm-data/postgres`; radice di Docker fuori dal disco dati |
