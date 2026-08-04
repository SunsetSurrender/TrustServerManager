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
# la prima volta: creare il secret
#   Linux/macOS
openssl rand -base64 24 | tr -d '\n+/=' > secrets/postgres_password
#   Windows PowerShell — vedi la nota sul newline più sotto

docker compose up -d --build --wait
python tools/smoke-test.py
```

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

## Dipendenze

`requirements.txt` è il runtime; `requirements-dev.txt` aggiunge Playwright e pytest e
**non entra nell'immagine di produzione**.

Le versioni sono pinnate ma **senza hash**. Prima del primo deploy in rete chiusa va
generato un lock con hash (`pip-compile --generate-hashes`): il pinning per numero di
versione non protegge da una ri-pubblicazione dello stesso numero sull'indice.

Le immagini base sono pinnate per digest sia nel `Dockerfile` sia in `compose.yaml`.
