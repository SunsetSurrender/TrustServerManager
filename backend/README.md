# Backend — scheletro

Stato: **scheletro**. Sta in piedi, parla col database, non fa ancora niente di
applicativo. Riferimento di progetto: [`../BACKEND-PLAN.md`](../BACKEND-PLAN.md).

## Cosa c'è

- `compose.yaml` (radice del repo) — reti, volumi, secret, healthcheck
- Postgres 17 su volume `pgdata`, **non pubblicato sull'host**
- Alembic cablato, con una revisione baseline che non crea nulla
- FastAPI con `/api/health` (liveness) e `/api/ready` (readiness)
- container non-root, filesystem read-only, capability droppate
- `../tools/smoke-test.py` — verifica automatica di tutto quanto sopra

## Cosa NON c'è, di proposito

Autenticazione e sessioni (§8.1, §8.6), motore di diff identity-aware (§8.10),
persistenza dell'inventario e commit atomico (§8.11), servizio nginx con l'allowlist
dei file statici (§6). Arrivano in commit separati, nell'ordine di §9 del piano.

La revisione Alembic `0001_baseline` non crea tabelle applicative: le tabelle di §2
arriveranno insieme al codice che le usa, per non avere uno schema che nessun test
esercita.

`/api/docs` è aperto perché non c'è ancora niente da proteggere. Va chiuso o messo
dietro autenticazione nello stesso commit in cui arrivano gli endpoint reali.

## Avvio

```bash
# la prima volta: creare i secret
#   Linux/macOS
openssl rand -base64 24 | tr -d '\n+/=' > secrets/postgres_password
openssl rand -base64 24 | tr -d '\n+/=' > secrets/api_db_password
# Ruolo del worker, DISTINTO da quello dell'API: la garbage collection delle foto
# ha bisogno di DELETE su `photos` e l'API non deve averlo (§8.5). Serve una
# password propria, altrimenti i due ruoli sarebbero separati nel database e
# indistinguibili nelle credenziali.
openssl rand -base64 24 | tr -d '\n+/=' > secrets/worker_db_password
#   Windows PowerShell — vedi la nota sul newline più sotto

# Password del relay SMTP. Il file deve ESISTERE, ma può essere VUOTO: un relay
# interno senza autenticazione è normale in rete chiusa, e con
# TSM_SMTP_USERNAME vuoto non viene tentato alcun login.
touch secrets/smtp_password

docker compose up -d --build --wait
python tools/smoke-test.py
```

### Configurazione dell'invio email

Il trasporto è dell'**operations**, non dell'interfaccia: host, porta, modalità
TLS, mittente e utenza sono variabili d'ambiente, e la password è un secret
montato. `/api/settings` non restituisce né accetta nulla di tutto questo —
espone solo `smtp.configured`, un booleano (§8.38).

La ragione è pratica: un oggetto `smtp` modificabile via API è un oggetto in cui,
un giorno, qualcuno aggiunge `password`. Se non esiste un posto dove metterla,
non ci finisce.

```bash
TSM_SMTP_HOST=relay.interno.azienda.it
TSM_SMTP_PORT=587
TSM_SMTP_TLS_MODE=starttls        # starttls | tls | none
TSM_SMTP_SENDER=ced@azienda.it
TSM_SMTP_USERNAME=               # vuoto = nessun login
TSM_SMTP_TLS_VERIFY=true         # false solo per una CA interna non riconosciuta
```

Con `TSM_SMTP_HOST` vuoto l'invio è «non configurato»: la schermata delle
impostazioni lo dichiara e l'invio di prova risponde `503 smtp_not_configured`,
invece di far restare qualcuno ad aspettare una posta che non partirà.

`--wait` fa uscire il comando solo quando gli healthcheck sono verdi: se qualcosa non
parte, se ne accorge Compose e non il primo utente.

## Liveness e readiness sono due cose diverse

| Endpoint | Tocca il DB | A cosa serve |
|---|---|---|
| `/api/health` | no | il processo è vivo. Se rispondesse 503 a DB giù, l'orchestratore riavvierebbe l'API per un guasto che non è dell'API |
| `/api/ready` | sì | le dipendenze sono usabili. È questo che il reverse proxy deve guardare |

Comportamento verificato fermando il database:

```
DB fermo:      /api/health → 200 {"status":"ok"}
               /api/ready  → 503 {"status":"unavailable","database":"unreachable"}
DB ripartito:  /api/ready  → 200 {"status":"ready","database":"ok"}
```

Il recupero è automatico grazie a `pool_pre_ping`: non serve riavviare l'API dopo un
riavvio del database.

## ⚠ Permessi del file di secret in produzione

Questo è il punto dove il deploy fallisce più facilmente, e in modo poco chiaro.

I secret di Compose (fuori da Swarm) sono **bind mount del file dell'host**: le opzioni
`uid`/`gid`/`mode` valgono solo in Swarm e qui vengono ignorate. Il file arriva nel
container con proprietario e permessi che ha sull'host.

L'API gira come **uid 10001**. Se sull'host il file è `0400 root:root`, il container non
riesce a leggerlo e l'avvio termina con
`RuntimeError: secret non leggibile: /run/secrets/postgres_password`.

Su un host Linux di produzione:

```bash
sudo chown 10001:10001 secrets/postgres_password
sudo chmod 0400        secrets/postgres_password
```

Così il file resta illeggibile a tutti tranne all'utente del container — che è
l'obiettivo, non un compromesso. Su Docker Desktop (Windows/macOS) i permessi dei bind
mount sono sintetici e la lettura funziona comunque: **il problema si manifesta solo in
produzione**, che è il momento peggiore per scoprirlo.

Il container Postgres non ha questo problema: il suo entrypoint parte da root, legge il
secret, fa `initdb` e poi lascia i privilegi al servizio.

Il valore del secret non viene messo in cache dall'applicazione: si rilegge a ogni
apertura di connessione, quindi la rotazione non richiede un rebuild dell'immagine.

## Perché `user:` non è impostato sul servizio db

L'immagine ufficiale di Postgres deve partire da root per inizializzare il volume, che
Docker crea di proprietà di root, e poi passa da sé all'utente `postgres`. Forzare
`user:` in Compose romperebbe `initdb` al primo avvio su un volume vuoto.

Lo smoke test non si fida di questa spiegazione e verifica il fatto:
`stat -c %U /proc/1` dentro il container deve restituire un utente diverso da `root`.

## Il controllo «Postgres non è pubblicato»

Non è verificato sondando `127.0.0.1:5432`. Su una macchina di sviluppo quella porta può
essere occupata da un altro Postgres, e il test fallirebbe pur essendo corretta la nostra
configurazione — è precisamente quello che è successo alla prima esecuzione qui, per un
container `portale-postgres` di un altro progetto.

La domanda giusta è «il **nostro** Postgres è pubblicato?», e si risponde interrogando la
configurazione del progetto:

1. i `Publishers` del servizio `db` non hanno porta host
2. `docker compose port db 5432` non risolve nulla
3. `NetworkSettings.Ports` del container è senza binding
4. la rete `tsm_internal` ha `Internal=true`, cioè nessun gateway verso l'esterno

Un listener estraneo su 5432 viene segnalato come nota informativa, con il nome di chi lo
pubblica, così non può essere confuso con il nostro.

## Migrazioni

Girano come servizio one-shot `migrate`, non nell'entrypoint dell'API: l'API non ha
bisogno di privilegi DDL a runtime e l'ordine di avvio resta esplicito
(`depends_on: service_completed_successfully`).

```bash
docker compose run --rm migrate alembic history
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic revision -m "descrizione"   # nuova revisione
```

La stringa di connessione non è in `alembic.ini`: `migrations/env.py` la prende da
`app.config`, che legge la password dal secret. In un file di configurazione finirebbe
nell'immagine e nel repository.

## Comandi del proprietario dello schema

Girano dal servizio `migrate`, l'unico che ha la password del proprietario.

```bash
# inizializzazione una-volta-sola (§8.1)
docker compose run --rm migrate python scripts/bootstrap.py --seed ... --admin ...

# proiezione relazionale (§8.42): stato, verifica, ricostruzione
docker compose run --rm migrate python scripts/project.py --status
docker compose run --rm migrate python scripts/project.py --verify
docker compose run --rm migrate python scripts/project.py --rebuild
```

`project.py` **non è una migrazione di dati e non è un servizio**, di proposito. Una
migrazione si esegue una volta sola, all'avvio, senza che nessuno la guardi, e se
aborta ferma il deployment; un servizio manterrebbe aggiornata una rappresentazione
che oggi nessuno legge, e i guasti si scoprirebbero il giorno in cui qualcuno comincia
a leggerla. Questo comando si lancia quando si vuole, è ripetibile, ed è fatto perché
il suo esito venga letto.

`--rebuild` è atomico: prende il lock della testa, ricostruisce tutto, rilegge **da
SQL**, riassembla e confronta il digest con quello registrato nell'istantanea. Se non
torna, aborta e nel database non cambia niente. Nessun ruolo di runtime ha i privilegi
per eseguirlo.

## Test

```powershell
.\tools\run-backend-tests.ps1          # suite completa, avvia un Postgres dedicato
.\tools\run-backend-tests.ps1 -KeepDb  # lascia il database in piedi fra le esecuzioni
```

Senza `TSM_DB_URL` i test di integrazione si saltano e resta la suite pura (identità, diff,
canonicalizzazione, schema, politica, schema congelato del documento). Per puntarli a un
database qualsiasi:

```
TSM_DB_URL=postgresql+psycopg://utente:password@host:5432/dbname
```

`TSM_DB_URL` scavalca host/porta/nome/utente e la lettura del secret: è pensata per i test e
per un Postgres gestito con credenziali fornite dall'infrastruttura. In produzione resta
vuota e la password arriva dal secret montato.

I test dell'inventario girano su **PostgreSQL reale**, senza doppi: quello che verificano —
`SELECT … FOR UPDATE`, identity bigint, atomicità del rollback — è comportamento del
database, e un finto non lo dimostrerebbe. Sono anche il motivo per cui è stato trovato un
difetto di concorrenza reale: vedi §8.17 del piano, «Il lock non deve contenere una JOIN».

## Dipendenze

`requirements.txt` è il runtime; `requirements-dev.txt` aggiunge Playwright e pytest e
**non entra nell'immagine di produzione**.

Le versioni sono pinnate ma **senza hash**. Prima del primo deploy in rete chiusa va
generato un lock con hash (`pip-compile --generate-hashes`): il pinning per numero di
versione non protegge da una ri-pubblicazione dello stesso numero sull'indice.

Le immagini base sono pinnate per digest sia nel `Dockerfile` sia in `compose.yaml`.
