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

#### Il worker legge la proiezione relazionale (§8.47)

Dalla fase 2F le scadenze non vengono più dal documento JSON: vengono dalle colonne
`garanzia_date` / `supporto_date` delle tabelle normalizzate. **La conseguenza
operativa è una sola, e va conosciuta prima di vederla:**

> se la proiezione non rispecchia la testa, **il worker non manda niente**, e lo dice.

Non è un guasto del worker ed è la stessa condizione che fa rispondere 503 all'API. Il
rimedio è lo stesso, ed è in §3.8: `project.py --rebuild`.

Come si riconosce:

```bash
docker compose exec -T worker python scripts/worker_health.py --json
# {"healthy": true, "state": "running", "detail": "projection_not_current (due=0, sent=0)", ...}

docker compose logs worker | grep proiezione
# ERROR ... proiezione non attuale (...): nessun avviso inviato,
#           il giro di 2026-08-20 verrà ripreso al prossimo tick
```

⚠ **Il worker resta `healthy`.** Il battito dice «il processo gira e vede il database»,
che è vero; il problema è nell'inventario, non nel worker. Il segnale da guardare è il
campo `detail`, e per questo va nel monitoraggio (vedi §5, «Controlli di monitoraggio»).

⚠ **La giornata NON è persa.** Il giro di oggi resta *aperto* nel registro
`scheduler_runs`, quindi appena la proiezione è riparata il tick successivo — entro
cinque minuti — rifà il giro di oggi e manda gli avvisi. Non serve nessun comando, e
non serve aspettare domani.

```sql
-- il giro di oggi è aperto o concluso?
SELECT run_date, started_at, finished_at, outcome FROM scheduler_runs
 ORDER BY run_date DESC LIMIT 5;
-- finished_at NULL = verrà ripreso
```

Altri due esiti nuovi che si possono leggere nel `detail` del battito, entrambi normali
e transitori:

| `detail` | significa | cosa fare |
|---|---|---|
| `projection_not_current` | la proiezione non rispecchia la testa: dichiara una versione vecchia, o nessuna, o una mappa che non gira più | `project.py --rebuild` (§3.8) |
| `projection_inconsistent` | la proiezione **dichiara il vero ma le righe non corrispondono**: le colonne non sono coerenti con la versione che lo stato afferma | ⚠ **indagare prima di ricostruire** — vedi sotto |
| `inventory_moved` | qualcuno ha salvato l'inventario mentre il worker calcolava | niente: ricalcola al tick dopo |

⚠ **I due codici della proiezione non si trattano allo stesso modo.**

`projection_not_current` è quasi sempre operativo: un aggiornamento in cui il passo
`--rebuild` non è stato eseguito. Si ricostruisce e si va avanti.

`projection_inconsistent` è diverso, e la differenza vale un minuto di attenzione: nessun
percorso dell'applicazione può produrlo — la fase 2C dimostra il giro completo dentro la
transazione di ogni scrittura — quindi la causa è **fuori** dall'applicazione. Un `UPDATE`
fatto a mano, un ripristino parziale, un guasto del supporto. Ricostruire funziona e
**cancella la traccia**: la domanda «perché era incoerente» non si potrà più fare. Quindi,
nell'ordine:

```bash
# 1. cosa dice la verifica, in dettaglio (sola lettura, non ripara niente)
docker compose run --rm migrate python scripts/project.py --verify

# 2. una copia dello stato attuale, prima di toccarlo
docker compose exec -T db pg_dump -U tsm -t 'inventory_*' tsm > /srv/tsm-data/incoerenza-$(date +%F).sql

# 3. solo allora
docker compose run --rm migrate python scripts/project.py --rebuild
```

Il worker non manda niente in nessuno dei due casi, e in nessuno dei due perde la giornata.

⚠ **Perché il worker valida il modello** (§8.47.4). Le colonne `garanzia_date` e
`supporto_date` sono *derivate*: non tornano nel documento, quindi non entrano nel digest.
Azzerarne una a mano lascia versione e digest identici — la proiezione si dichiara attuale e
lo è, per quanto quel controllo può misurare — e il worker non troverebbe quella scadenza:
nessun avviso, giro concluso «niente da fare», battito verde. Un sistema di allerta che non
allerta e si dichiara sano. La validazione del modello è l'unica cosa che lo vede, e per
questo gira a ogni giro anche se costa una lettura completa della proiezione (≈14 ms alla
scala di produzione, ≈420 ms a trenta volte quella scala — una volta al giorno).

Un valore non interpretabile come data — `garanzia: "da verificare"` — è un **avviso**, non un
errore: non blocca niente. Se bloccasse, un campo compilato male su un dispositivo spegnerebbe
gli avvisi di tutti gli altri.

Il secondo esiste perché un avviso calcolato su una revisione superata annuncerebbe una
scadenza che qualcuno ha appena corretto. È raro — richiede un salvataggio nei
millisecondi giusti — e si risolve da sé.

⚠ **Il worker non passa dall'API.** Legge il database direttamente, con il proprio
ruolo `tsm_worker` e in sola lettura sull'inventario. Non chiama
`GET /api/inventory/expiries`: quell'endpoint serve l'interfaccia e ha una semantica
sua — elenca gli scaduti e salta i dismessi, il worker fa l'opposto (§8.48). Se un
giorno i numeri della vista Scadenze e quelli di un avviso non tornano, **non è un
difetto**: è quella differenza, ed è registrata.

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

### 3.8 La proiezione relazionale: è lo stato corrente, e ora è ciò che si legge

Le migrazioni `0010_normalised`, `0011_projection`, `0012_dual_write` e
`0013_domain` creano le tabelle dello stato operativo (`inventory_locations`,
`inventory_rooms`, `inventory_racks`, `inventory_devices`,
`inventory_manual_entries`, `inventory_projection_state`) e, dalla fase 2C, danno
all'API i privilegi per mantenerle.

⚠ **La `0013_domain` richiede una RICOSTRUZIONE, e va letta prima di aggiornare.**
Vedi §3.8.1 qui sotto: dopo quella migrazione le rotte di lettura rispondono 503
finché non gira `project.py --rebuild`. È previsto, ha un rimedio di un comando, e
NON è una migrazione di dati — ma se l'aggiornamento avviene in orario di lavoro senza
saperlo, per qualche minuto l'applicazione non serve l'inventario.

**Fase 2C (§8.44) — la scrittura.** Ogni `PUT` che cambia qualcosa mantiene la
proiezione **nella stessa transazione** dell'istantanea JSON: dopo un salvataggio
riuscito le due rappresentazioni sono allineate, sempre. Non esiste uno stato in cui
una è avanzata e l'altra no.

**Fase 2D (§8.45) — la lettura.** `GET /api/inventory` restituisce ora il documento
**riassemblato dalle tabelle**, non `inventory_versions.doc`. Il contratto HTTP è
identico e il frontend non è cambiato di una riga; cambia la fonte.

| | ruolo |
|---|---|
| tabelle normalizzate | stato operativo **corrente**, autorevole |
| `inventory_versions.doc` | storia immutabile, e **giudice** della coerenza |
| `inventory_projection_state` | dichiarazione di *quale* testa le tabelle rappresentano |

Ogni lettura verifica il giro completo prima di servire: riassembla, ricalcola il
digest, e pretende che coincida con quello registrato in testa. **Non c'è nessun
ripiego sull'istantanea JSON**, ed è deliberato — un ripiego funzionerebbe, l'utente
vedrebbe l'inventario giusto, e il difetto resterebbe invisibile fino al giorno in cui
qualcuno interroga le tabelle.

**Conseguenza operativa da conoscere.** Una proiezione non attuale o incoerente non
rende indisponibili soltanto i salvataggi: rende indisponibile **l'inventario**. Fino
alla 2C il `GET` funzionava comunque e il guasto era difficile da vedere; adesso
l'applicazione si ferma e lo dice. La readiness lo dice per prima.

> ⚠ **Passo obbligatorio all'aggiornamento.** Le proiezioni costruite prima della 2C
> non dichiarano la versione della mappa (`mapper_version` nasce NULL), quindi l'API
> **rifiuta tutto** con `projection_not_current` (503) finché non si esegue
> `--rebuild`. È deliberato: una proiezione disallineata ha una causa, e ripararla di
> nascosto al primo salvataggio di un utente cancellerebbe l'unica occasione di
> scoprirla. La sequenza completa è in §8.44.1:
>
> 1. fermare le scritture; 2. migrazione; 3. `--rebuild` come proprietario;
> 4. `--verify` deve riuscire; 5. avviare l'API; 6. readiness verde; 7. riaprire.
>
> La **readiness** comprende la proiezione: un'istanza con la proiezione vecchia
> risponde 503 invece di dire «pronto» e poi rifiutare ogni richiesta.

#### I due 503 della proiezione, e perché il rimedio è diverso

Su `/api/inventory` (sia `GET` sia `PUT`) possono arrivare due codici. Distinguerli
prima di agire è importante:

| codice nella risposta | significato | che fare |
|---|---|---|
| `projection_not_current` | la proiezione **dichiara** una versione vecchia, o nessuna, o una mappa che non gira più | `project.py --rebuild` |
| `projection_inconsistent` | la dichiarazione è **falsa**: le tabelle contengono qualcos'altro | `project.py --verify` **prima**, e capire perché |

Il secondo caso **non si risolve con `--rebuild`**. Nessun percorso dell'applicazione
può produrlo — ogni scrittura dimostra il giro completo dentro la propria transazione
— quindi la causa è fuori: una scrittura fatta a mano sul database, un ripristino
parziale da backup, un guasto del supporto. Un `--rebuild` lo farebbe sparire
cancellando le prove. Prima `--verify`, che dice **cosa** non torna e **dove**; il
messaggio completo sta nei log dell'API (la risposta HTTP non nomina tabelle né
contenuti, di proposito).

Nei log dell'API si riconoscono così:

```text
proiezione non attuale: inventario non servibile e salvataggi rifiutati.
  Eseguire `project.py --rebuild`. Dettagli: [...]

proiezione INCOERENTE: l'inventario non viene servito. Non è un caso da `--rebuild`
  alla leggera — una ricostruzione cancella le prove. Verificare con
  `project.py --verify`. Dettagli: [...]
```

#### Una readiness verde non garantisce la fedeltà, e non è una lacuna

Tre domande diverse con tre costi diversi, separate di proposito:

| | che cosa verifica | quando gira |
|---|---|---|
| `/api/ready` | versione, digest, versione della mappa: valori già registrati | ogni pochi secondi, per sempre |
| `GET /api/inventory` | il giro completo, riassemblando | una volta per richiesta |
| `project.py --verify` | il giro completo, su richiesta | quando una persona lo chiede |

Quindi: **una colonna corrotta a mano lascia `/api/ready` verde e fa cadere il `GET`**.
La readiness guarda ciò che è dichiarato; riassemblare l'inventario a ogni sonda
costerebbe un `--verify` completo ogni pochi secondi per sempre. Se si vuole la
fedeltà, si chiede a `--verify`.

#### Le tre interrogazioni (fase 2E, §8.46)

Dalla fase 2E esistono tre endpoint di sola lettura sopra le stesse tabelle:

```text
GET /api/inventory/search?q=…     dispositivi e rack: testo o rete (10.0.0.0/24, 10.0.*)
GET /api/inventory/capacity       unità occupate, libere, blocco contiguo per rack
GET /api/inventory/expiries       garanzia e supporto, con giorni e livello
```

Le vedono tutti i ruoli autenticati (`view`, `edit`, `admin`), come le viste
corrispondenti nell'interfaccia. Rifiutano con 503 `projection_not_current` se la
proiezione non rispecchia la testa, esattamente come `GET /api/inventory` — quindi il
rimedio è lo stesso, `project.py --rebuild`.

⚠ **Non riassemblano il documento**, di proposito: pretendono che la proiezione sia
*attuale* (tre confronti fra valori registrati) e si fidano delle tabelle. La
conseguenza è la stessa asimmetria della readiness: una colonna corrotta a mano fa
cadere `GET /api/inventory` e **non** le interrogazioni. Il percorso di fedeltà
completa resta il `GET`; quello operativo `--verify`.

⚠ **Il frontend non le usa ancora.** Continua a scaricare l'inventario intero e a
calcolare in locale. È deliberato: la fase 2E prova le implementazioni sul server, e la
sostituzione dei calcoli lato client è un commit successivo. Lo stesso per lo scanner
delle notifiche, che continua a leggere il documento.

#### «A volte è lento subito dopo un salvataggio grande»

Se qualcuno lo segnala, la causa più probabile non è una query: è che
`synchronise` **cancella e reinserisce tutte le righe della proiezione a ogni
salvataggio** (fase 2C), quindi dopo una scrittura grande le statistiche del
pianificatore sono vecchie per qualche secondo, finché autoanalyze non le rifà.

Misurato: a 5 910 righe la vista Capacità passa da 65 ms a 459 ms subito dopo la
scrittura, e torna a 65 ms appena autoanalyze ha finito. Alla scala di produzione
(197 righe) è invisibile. Non c'è niente da fare in configurazione; se un giorno
diventasse fastidioso, il posto dove aggiungere un `ANALYZE` è `project.py --rebuild`,
che gira già come proprietario dello schema.

Per verificare che sia questo:

```bash
docker compose exec db psql -U tsm -d tsm -c \
  "SELECT relname, last_autoanalyze, n_mod_since_analyze FROM pg_stat_user_tables \
    WHERE relname LIKE 'inventory_%' ORDER BY relname"
```

#### Bilancio delle connessioni al database

Da tenere presente prima di toccare il numero di lavoratori `uvicorn`:

```text
per processo   (pool_size + max_overflow) × 2 pool  =  (5 + 10) × 2  =  30
in totale      30 × lavoratori uvicorn              =  30 × 1        =  30
```

I due pool sono quello delle richieste e quello di **lettura** in
`REPEATABLE READ, READ ONLY` (fase 2D): un `GET` ne tiene una di ciascuno insieme, e
per questo sono due — con un pool solo, quindici richieste simultanee si bloccherebbero
a vicenda. Il ragionamento completo, con i numeri, è in testa a `backend/app/db.py`.

⚠ `--workers N` moltiplica per N **entrambi** i pool. Con N=4 si arriva a 120
connessioni possibili, oltre il `max_connections` predefinito di PostgreSQL (100), e il
guasto è «FATAL: sorry, too many clients already» su richieste qualunque, sotto carico.
Chi cambia quel numero deve ricalcolare insieme i due pool, `max_connections` e la
memoria della macchina. Le misure della fase 2D e 2E non danno nessuna ragione per
cambiarlo.

I comandi girano come **proprietario dello schema** — cioè dal servizio `migrate`,
l'unico che ne ha la password:

```bash
# che versione rispecchia (sola lettura, non cambia niente)
docker compose run --rm migrate python scripts/project.py --status

# costruirla, o ricostruirla
docker compose run --rm migrate python scripts/project.py --rebuild

# riassemblare da SQL e confrontare i digest (sola lettura)
docker compose run --rm migrate python scripts/project.py --verify
```

`--rebuild` è **atomico e ripetibile**: prende il lock della testa dell'inventario
(quindi un salvataggio concorrente aspetta), ricostruisce tutto, rilegge da SQL,
riassembla e confronta il digest con quello registrato nell'istantanea. Se qualcosa
non torna **aborta e non cambia niente** — nessuna proiezione a metà, e la precedente
resta buona.

Uscite: `--status` esce sempre 0 (è un rapporto). `--verify` esce 1 se le tabelle
**non** riassemblano la versione che dichiarano di rispecchiare (fedeltà) **oppure** se
quella versione non è la testa (attualità). Le due cause sono riportate separatamente,
perché sono diverse: la prima è un difetto del codice, la seconda un comando mancante.

⚠ **Dalla fase 2C una proiezione vecchia È un guasto.** Fino alla 2B era normale —
nessuno la sincronizzava — e `--status` lo diceva per non far cercare un problema che
non c'era. Adesso significa che l'API sta rifiutando le scritture:

```text
  testa        versione 42, digest 7fdbf3d8e42c…
  proiezione   versione 39, digest 1b90a4c5e0aa…, costruita il 2026-08-12 03:31:07 UTC
  mappa        versione 1
  esito        NON aggiornata: rispecchia la 39, la testa è la 42 (3 versioni di
               scarto). Dalla fase 2C ogni salvataggio la mantiene, quindi questo
               scarto NON è previsto: l'API rifiuta i salvataggi finché non si
               esegue `project.py --rebuild`
```

Se `mappa` mostra un numero diverso da quello atteso (o nessuno), la proiezione è
stata scritta da una versione precedente del codice: le righe riassemblerebbero lo
stesso documento e starebbero nelle colonne sbagliate, cosa che il confronto dei
digest non può vedere. Serve un `--rebuild`.

⚠ **Non popolarle a mano.** La verifica del digest è la sola prova che la proiezione è
fedele, e una `INSERT` scritta a mano la salta. L'API ha `INSERT/UPDATE/DELETE` perché
il suo codice le mantiene, ma **non** `TRUNCATE`; il worker resta in sola lettura; e
`inventory_versions` resta senza `UPDATE` e senza `DELETE` per chiunque, perché è il
riferimento contro cui la proiezione si verifica.

---

### 3.8.1 Aggiornare alla fase 2G: una ricostruzione obbligatoria

La migrazione `0013_domain` aggiunge due colonne a `inventory_devices` e **alza la
versione della mappa da 1 a 2**:

| colonna | che cos'è |
|---|---|
| `presenza` | presenza FISICA dell'apparato nel rack: `presente` / `rimosso`. Valore dell'utente, torna nel documento |
| `ip_addr` | l'indirizzo IP **interpretato**, tipo `inet`. Derivata, non torna nel documento |

⚠ **Perché la ricostruzione non è opzionale.** Le righe scritte dalla mappa versione 1
riassemblerebbero lo **stesso** documento, quindi lo stesso digest: il confronto dei
digest non può accorgersi di niente. Ma `presenza` starebbe in `extra` e `ip_addr`
sarebbe vuota, quindi la vista Capacità non troverebbe la presenza e la ricerca non
troverebbe gli indirizzi. È esattamente il caso per cui la versione della mappa esiste.

Il codice se ne accorge e **rifiuta di servire**, invece di rispondere con dati che non
corrispondono:

```text
$ curl -sk https://localhost/api/inventory | head -c 200
{"error":{"code":"projection_not_current", ...
```

Sequenza dell'aggiornamento, **fuori orario di lavoro**:

```bash
# 1. dove siamo, prima di toccare niente
docker compose run --rm --no-TTY migrate python scripts/project.py --status

# 2. le migrazioni (la 0013 non tocca nessun dato: aggiunge due colonne NULL)
docker compose run --rm --no-TTY migrate python scripts/migrate.py

# 3. da qui l'applicazione risponde 503: la proiezione si dichiara non attuale
docker compose run --rm --no-TTY migrate python scripts/project.py --status
#    → mappa   versione 1   (attesa: 2)

# 4. la ricostruzione. Legge la testa da `inventory_versions` e riscrive le tabelle
docker compose run --rm --no-TTY migrate python scripts/project.py --rebuild

# 5. la verifica: riassembla e confronta il digest
docker compose run --rm --no-TTY migrate python scripts/project.py --verify
```

⚠ Il passo 5 non è decorativo, ed è l'unico che guarda le colonne DERIVATE. Il digest
è cieco a `garanzia_date`, `supporto_date` e ora `ip_addr` — non tornano nel documento
per costruzione — quindi `--verify` è la sola rete di sicurezza per loro, e va eseguito
**dopo** ogni ricostruzione invece che quando qualcosa sembra strano.

**Che cosa cambia per chi usa l'applicazione**, e va detto a chi risponde al telefono:

| | prima | dopo |
|---|---|---|
| ricerca `10.0.0.1` | trovava anche `10.0.0.100` | trova **solo** quell'indirizzo |
| ricerca IPv6 | solo per testo | per indirizzo e per rete |
| campi cercati | 5 | 9 (aggiunti id, tipo, stato, presenza) |
| U occupate | tre numeri diversi in tre viste | uno |
| vista Scadenze | saltava i dismessi | **li mostra**, con un filtro per isolarli |
| avvisi via posta | anche per i dismessi | **non più** per i dismessi |
| percentuali | il frontend e il backend potevano differire di 1 | uguali |

I primi due cambiano il numero di risultati di una ricerca. È voluto (§8.50.6), ma è
il genere di cosa per cui qualcuno apre un ticket dicendo «la ricerca non funziona
più»: la risposta è che `10.0.0.1` adesso significa quell'indirizzo, e chi vuole il
prefisso scrive `10.0.0` oppure `10.0.0.0/24`.

⚠ **La presenza fisica non si deduce dallo stato.** Tutti i dispositivi esistenti
diventano `presente`, compresi i dismessi: dell'inventario di prima si sa solo che
nessuno ha detto che quegli apparati sono stati portati via. Chi conosce la sala deve
marcare `rimosso` a mano ciò che non c'è più — e finché non lo fa, la capacità continua
a contarli come occupati, che è il verso giusto in cui sbagliare.

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

# 5. il worker sta MANDANDO, non solo girando?  (§8.47)
#    ⚠ Il controllo 0 resta verde se la proiezione non rispecchia la testa: il
#    processo gira e vede il database, e il battito dice il vero. Ma nessun avviso
#    parte. Il segnale è nel campo `detail`, e serve un controllo suo — altrimenti
#    le scadenze smettono di essere comunicate senza che niente lo dica.
docker compose exec -T worker python scripts/worker_health.py --json   | grep -qE 'projection_(not_current|inconsistent)'   && alert "TSM: proiezione non utilizzabile, nessun avviso di scadenza parte (vedi §3.6)"

# 6. c'è un giro rimasto aperto da più di un'ora?  (§8.47)
#    Un giro aperto è NORMALE per qualche minuto (viene ripreso al tick dopo). Se
#    resta aperto, la causa non si sta risolvendo da sé.
docker compose exec -T db psql -U tsm -tAc   "SELECT count(*) FROM scheduler_runs
    WHERE finished_at IS NULL AND started_at < now() - interval '1 hour'"   | grep -qx 0 || alert "TSM: giro delle notifiche aperto da oltre un'ora"
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
