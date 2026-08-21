# Trust Server Manager — Piano backend, DB e deploy

Decisioni prese: **host Docker singolo + Compose**, **rete chiusa senza internet**,
**Postgres in container**, **utenze locali nel DB**.

Riferimenti al prototipo: `handoff/` (vedi `handoff/README.md`).

---

## 1. Strategia: due fasi, frontend toccato una volta sola

Il prototipo consegna l'intero documento a ogni modifica (`persist(next, azione)`,
[Sala Server v2.dc.html:1391](handoff/Sala%20Server%20v2.dc.html#L1391)). Sfruttiamo questo fatto
invece di combatterlo: fissiamo subito il **contratto HTTP** (`GET`/`PUT /api/inventory` con
versione) e cambiamo lo storage sottostante in un secondo momento, senza che il frontend se ne accorga.

### Fase 1 — persistenza durevole (stima 3-4 giorni)

L'albero spaziale (`locations → sale → vani/racks → devices`) resta un documento **JSONB versionato**.
Quattro cose escono subito dal documento, perché non possono aspettare:

| Cosa | Perché non può restare nel documento |
|---|---|
| `utenti` | le password vanno hashate, e il documento viene servito a *tutti* i client |
| `registro` | audit lato client non è audit (priorità 1 del README) |
| `foto` rack | sono dataURL base64: con il versionamento ogni versione le duplicherebbe |
| password SMTP | va in un secret, non nel DB (già segnalato dal README) |

Guadagno immediato: undo/redo/rollback lato server gratis, un `pg_dump` copre tutto,
e nessun lavoro da buttare in fase 2.

### Fase 2 — normalizzazione (incrementale, frontend invariato)

Le tabelle vere sostituiscono il documento. `GET` assembla l'albero, `PUT` fa un sync
transazionale (upsert dei presenti, delete degli assenti) usando gli id già stabili nel modello.
Si aggiungono endpoint di sola lettura (ricerca, capacità, scadenze) che interrogano SQL invece
di filtrare JSON in memoria, e il job scadenze legge da indice invece di scandire il documento.

**Il frontend non cambia in fase 2.** È esattamente il motivo per cui l'envelope resta stabile.

---

## 2. Schema database

### Fase 1

```sql
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username       citext UNIQUE NOT NULL,           -- unico anche fra i disabilitati
  role           text NOT NULL CHECK (role IN ('view','edit','admin')),
  password_hash  text NOT NULL,                    -- argon2id
  must_change_pw boolean NOT NULL DEFAULT false,    -- ex pwTemp
  nome           text, cognome text, telefono text, team text,
  disabled_at    timestamptz,                      -- disattivazione logica, mai DELETE (§8.6)
  last_login_at  timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  ip         inet, user_agent text
);
CREATE INDEX ON sessions (user_id) WHERE revoked_at IS NULL;

-- append-only: mai UPDATE, mai DELETE
CREATE TABLE inventory (
  version    integer PRIMARY KEY,
  doc        jsonb NOT NULL,
  author_id  uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- NB: nessuna colonna `action`. La stringa del client sta in audit.client_hint (§8.9),
-- e la versione corrente NON è "max(version)" ma inventory_head.version (§8.11).

-- singleton: il punto di serializzazione delle scritture (§8.11).
-- `id boolean PRIMARY KEY CHECK (id)` ammette una sola riga possibile, id = true.
CREATE TABLE inventory_head (
  id         boolean PRIMARY KEY DEFAULT true CHECK (id),
  version    integer NOT NULL REFERENCES inventory(version),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- immutabili e content-addressed: mai UPDATE, mai DELETE su richiesta utente (§8.5)
CREATE TABLE photos (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mime        text NOT NULL,
  bytes       bytea NOT NULL,
  sha256      text NOT NULL UNIQUE,                -- dedup: stessa foto = una riga
  size_bytes  integer NOT NULL,
  uploaded_by uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit (
  id                bigserial PRIMARY KEY,
  ts                timestamptz NOT NULL DEFAULT now(),  -- orologio del server, non del client
  user_id           uuid REFERENCES users(id),
  username          citext,                        -- denormalizzato: sopravvive al disable
  role              text,                          -- letto dal DB, non dal body
  ip                inet,
  inventory_version integer REFERENCES inventory(version),  -- NULL per azioni non-inventario
  scopes            text[],                        -- ambiti toccati, dal diff (§8.3)
  events            jsonb,                         -- eventi di dominio dal diff (§8.10)
  client_hint       text                           -- stringa di persist(): NON attendibile (§8.9)
);
CREATE INDEX ON audit (ts DESC);
CREATE INDEX ON audit (inventory_version);

CREATE TABLE settings (            -- notifiche + campi SMTP NON segreti (§8.7)
  key   text PRIMARY KEY,
  value jsonb NOT NULL
);

-- idempotenza dell'invio scadenze (§8.8): un avviso per device/tipo/scadenza/giorno
CREATE TABLE notifications_sent (
  device_uid uuid NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('garanzia','supporto')),
  due_date   date NOT NULL,
  sent_on    date NOT NULL,
  PRIMARY KEY (device_uid, kind, due_date, sent_on)
);

CREATE TABLE job_runs (            -- visibilità sui giorni saltati
  job         text PRIMARY KEY,
  last_ok_at  timestamptz,
  last_error  text,
  last_run_at timestamptz
);
```

Nessun limite FIFO a 500 righe sull'audit: è una tabella, non un array in memoria. Retention
per data, non per conteggio.

### Fase 2 (aggiunte)

```sql
locations (id uuid pk, code text unique, nome text, ordinamento int)
rooms     (id uuid pk, location_id fk, code text, nome text,
           w numeric, h numeric, area text, dim text, segnaposto bool,
           UNIQUE (location_id, code))
-- `id` qui è identità di RIGA, non di dominio: non esce mai verso il client e le
-- righe si riscrivono in blocco quando cambia la geometria della sala. I vani non
-- hanno `_uid` nel documento. Motivazione in §8.12.
vani      (id uuid pk, room_id fk, x numeric, y numeric, w numeric, h numeric,
           porta jsonb, porta2 jsonb)
racks     (id uuid pk, room_id fk, code text, name text, riga text, u int,
           x numeric, y numeric, w numeric, h numeric,
           seriali text[], photo_id uuid REFERENCES photos(id),
           UNIQUE (room_id, code))
devices   (id uuid pk, rack_id fk, code text, name text,
           tipo  text CHECK (tipo IN ('server','rete','storage','firewall','alimentazione','altro')),
           stato text CHECK (stato IN ('attivo','manutenzione','dismissione','dismesso')),
           model text, ip text, ip_addr inet, serial text, owner text,
           garanzia date, supporto date, note text, u int, h int NOT NULL DEFAULT 1,
           UNIQUE (rack_id, code))
manual_entries (id uuid pk, titolo text, blocchi jsonb, ordinamento int)
```

Indici che servono a funzionalità già presenti nella UI:

```sql
CREATE INDEX ON devices (garanzia) WHERE garanzia IS NOT NULL;   -- job scadenze
CREATE INDEX ON devices (supporto) WHERE supporto IS NOT NULL;   -- job scadenze
CREATE INDEX ON devices USING gist (ip_addr inet_ops);           -- ricerca CIDR/range
CREATE INDEX ON devices USING gin  (to_tsvector('simple',
         coalesce(name,'')||' '||coalesce(model,'')||' '||coalesce(serial,'')));
```

Nota su `ip`: nei dati del prototipo è testo libero e può essere vuoto. Si tiene la colonna
`ip` come testo (fedeltà al dato inserito) più una `ip_addr inet` popolata quando il valore è
parsabile — così la ricerca per CIDR/intervallo/wildcard, oggi fatta in JS lato client,
diventa una query.

Chiavi surrogate uuid con unique su `(parent, code)`: gli id di business (`R01`, `srv-db-01`)
sono rinominabili dagli utenti, quindi non possono essere chiavi primarie se si vuole
conservare la storia di un dispositivo attraverso un rinomino.

La colonna `id` di queste tabelle **è** lo `_uid` che il documento porta già dalla fase 1
(§8.4): la fase 2 non genera identità nuove, le promuove a chiave primaria. È il motivo per
cui gli `_uid` devono esistere dal primo giorno di persistenza durevole — introdurli dopo
significa inventare identità per righe già scritte, senza sapere quali rinomini sono
avvenuti nel frattempo.

---

## 3. API

```
POST   /api/auth/login              → cookie di sessione HttpOnly+Secure+SameSite=Strict
POST   /api/auth/logout
GET    /api/auth/me                 → { username, role, must_change_pw }
POST   /api/auth/password           → cambio password propria, azzera must_change_pw

GET    /api/users?includeDisabled        (admin)
POST   /api/users                        (admin) → crea con password provvisoria
PATCH  /api/users/{id}                   (admin)
POST   /api/users/{id}/disable           (admin) → disattivazione logica + revoca sessioni
POST   /api/users/{id}/enable            (admin)
                                         -- NIENTE DELETE: romperebbe l'audit (§8.6)

GET    /api/inventory               → { version, doc }
PUT    /api/inventory               → { baseVersion, doc, action }
                                      200 { version }          commit atomico (§8.11)
                                      409 { currentVersion }   baseVersion superato
                                      403 { events }           ambito non consentito (§8.3)
                                      422 { errors }           _uid mancanti/duplicati/mutati (§8.4)
GET    /api/inventory/versions      → [{ version, ts, author, scopes, clientHint }]
                                      -- da JOIN con audit su inventory_version: la
                                         descrizione non è duplicata in inventory (§8.9)
GET    /api/inventory/versions/{v}  → { version, doc }
POST   /api/inventory/rollback/{v}       (admin) → nuova versione = copia della v

POST   /api/photos                  → multipart, → { id }  (immutabile, dedup per sha256)
GET    /api/photos/{id}             → bytes, Cache-Control immutable
                                      -- NIENTE DELETE: le versioni storiche le referenziano.
                                         Rimozione = si toglie il riferimento; i byte li
                                         raccoglie la GC quando nessuna versione li usa (§8.5)

GET    /api/audit?limit&offset&from&to
GET    /api/settings   PUT /api/settings  (admin)
                                      -- la password SMTP NON è nello schema, né in lettura
                                         né in scrittura: la gestisce l'operations (§8.7)
POST   /api/notifications/test           (admin)
GET    /api/health     GET /api/ready
```

Concorrenza: **last-write-wins con lock ottimistico**. Il `PUT` che arriva con un
`baseVersion` non più corrente riceve 409 e la UI propone il ricaricamento. Per una squadra
di pochi operatori è la scelta giusta; endpoint granulari si aggiungono in fase 2 solo se
l'editing simultaneo diventa un problema reale.

Il `PUT` **non è un salvataggio fidato**: arriva l'intero documento da un client che non è
più l'autorità. Va autorizzato per ambito e verbalizzato per differenza — vedi §8.3 e §8.9.

---

## 4. Modifiche al frontend (fase 1)

Otto punti, tutti localizzati:

1. **`componentDidMount`** ([:1120](handoff/Sala%20Server%20v2.dc.html#L1120)) — non carica più
   i dati direttamente: prima `GET /api/auth/me`, e **solo se autenticato** `GET /api/inventory`
   (§8.1). `inventario.js` sopravvive solo come seed per lo script di import iniziale.
2. **`persist(data, azione)`** ([:1391](handoff/Sala%20Server%20v2.dc.html#L1391)) — resta
   ottimistica (`setState` immediato), ma le scritture passano da una **coda serializzata**
   con una sola `PUT` in volo (§8.2). Su 409: avviso "modificato da un altro utente" +
   ricarica. `dirty` passa da "non esportato" a "salvataggio in corso / fallito".
3. **`_doLogin()`** ([:1351](handoff/Sala%20Server%20v2.dc.html#L1351)) — `POST /api/auth/login`.
   Va **rimosso** il confronto password lato client e il fallback che concede `admin`
   quando l'elenco utenze è vuoto ([:1358](handoff/Sala%20Server%20v2.dc.html#L1358)).
4. **`saveDraft` dei dispositivi** ([:2925](handoff/Sala%20Server%20v2.dc.html#L2925)) — oggi
   ricostruisce l'oggetto da zero (`const dev = { id, name, ... }`) e sostituisce il
   precedente: **distruggerebbe lo `uid` assegnato dal server a ogni modifica**. Va cambiato
   in uno spread dell'originale (§8.4). Il salvataggio dei rack invece usa
   `Object.assign(t, …)` ([:2809](handoff/Sala%20Server%20v2.dc.html#L2809)) e i campi
   sconosciuti li conserva già: va bene così.
5. **Foto rack** — `POST /api/photos`, `foto` diventa un id/URL invece di un dataURL.
6. **Registro** — legge da `/api/audit` invece che da `data.registro`.
7. **Undo/redo** — resta client-side; le versioni server sono la rete di sicurezza.
8. **React vendorizzato** — fatto, vedi §5.

---

## 5. Rete chiusa: dipendenze eliminate ✔ FATTO

`support.js` caricava React da unpkg.com a runtime
([support.js:1143-1146](handoff/support.js#L1143)): in rete chiusa l'app **non partiva**.

Risolto vendorizzando i due file in `handoff/vendor/`, caricati **prima** di `support.js`.
Il runtime controlla `if (w.React && w.ReactDOM) return Promise.resolve()`
([support.js:1840](handoff/support.js#L1840)) e salta la CDN da sé: **`support.js` non è
stato modificato**. `@babel/standalone` non serve — è richiesto solo da `x-import`, che
l'applicazione non usa.

Provenienza, SHA-256 e corrispondenza SRI: [handoff/vendor/README.md](handoff/vendor/README.md).
Verifica automatica dell'avvio offline: `python tools/offline-boot-test.py`.

⚠ **`Sale Server Pomezia (standalone).html` è solo-sviluppo.** Non è stato toccato e continua
a dipendere da unpkg: offline **non parte**. Finché non viene rigenerato con React
inlinizzato *e* riverificato con la stessa procedura di §5, vale che:

- non va distribuito né descritto come "offline" o "doppio click": la dicitura nel
  `handoff/README.md` è oggi inesatta;
- non entra in nessuna immagine e non è raggiungibile via nginx (allowlist, §6) — a maggior
  ragione perché contiene l'inventario completo inline e quindi scavalca auth e ruoli;
- non è sulla strada del deploy, quindi non blocca niente: è un artefatto di consultazione
  per chi lavora al progetto.

Quando verrà rigenerato, il file va sottoposto a `tools/offline-boot-test.py` esattamente
come l'applicazione normale, con il suo caso di controllo. «Sembra che funzioni» non è la
verifica: era già la situazione da cui siamo partiti.

Da verificare in fase di hardening: il runtime valuta la logica dell'app con `new Function`
([support.js:844](handoff/support.js#L844)), quindi una CSP stretta richiede `'unsafe-eval'`
su `script-src` e `'unsafe-inline'` su `style-src` (la UI usa style object inline in modo
massiccio). Eliminarli richiede un passo di precompilazione del blocco `text/x-dc`: fattibile,
ma non in fase 1.

---

## 6. Deploy

### Immagini

| Immagine | Base | Contenuto |
|---|---|---|
| `tsm-web` | `nginx:1.27-alpine` | **solo i file in allowlist** (sotto), proxy `/api` → `api:8000`, TLS |
| `tsm-api` | `python:3.13-slim` | FastAPI + uvicorn, multi-stage, utente non-root |
| `postgres` | `postgres:17-alpine` | volume `pgdata`, **nessuna porta pubblicata sull'host** |

### Allowlist dei file statici

L'immagine web **non** contiene `handoff/`. Contiene un elenco chiuso di file, copiati uno
per uno. Servire la cartella così com'è pubblicherebbe cose che non devono stare su un
server raggiungibile.

Cosa va servito:

| Percorso | File |
|---|---|
| `/` (index) | `Sala Server v2.dc.html` |
| `/support.js` | runtime dc |
| `/xlsx.js` | import/export Excel |
| `/vendor/react.production.min.js` | React (§5) |
| `/vendor/react-dom.production.min.js` | ReactDOM (§5) |
| `/vendor/THIRD-PARTY-NOTICES.md` | note di licenza — **obbligatorio**, vedi sotto |
| `/fonts/fonts.css`, `/fonts/*.woff2` | font locali |
| `/trust-logo.png`, `/trust-logo-dark.png` | loghi |
| `/api/…` | proxy verso `api:8000` |

Cosa **non** deve uscire, e perché:

| Escluso | Motivo |
|---|---|
| `Sale Server Pomezia (standalone).html` | 761 KB con l'inventario completo dentro: un dump dei dati scaricabile, che scavalca del tutto auth e ruoli (§8.1). Ed è solo-sviluppo (§5). |
| `inventario.js` | l'inventario di seed in chiaro, senza autenticazione. Dopo §4.1 l'app non lo usa più: il runtime lo prende da `GET /api/inventory`. |
| `README.md`, `vendor/README.md`, `vendor/SHA256SUMS` | documentazione interna: struttura del modello dati, punti di aggancio, credenziali di collaudo citate. Ricognizione gratis. |
| asset di migrazione, seed, dump, `tools/` | non hanno niente a che fare con il runtime |

Due livelli, perché uno solo non basta:

1. **Al build**: il `Dockerfile` fa `COPY` dei singoli file consentiti. Quello che non è
   nell'immagine non può essere servito per sbaglio da una regola nginx scritta male in
   futuro. È la difesa che regge nel tempo.
2. **A runtime**: nginx serve i percorsi previsti e chiude tutto il resto
   (`location / { return 404; }` come default, non `autoindex`, e nessuna directory servita
   in blocco). Difesa in profondità, e protegge dal caso in cui qualcuno monti un volume.

⚠ **Sequenza**: `inventario.js` va escluso **solo dopo** il punto 6 di §9 (aggancio del
frontend all'API). Fino a quel momento l'app lo importa in `componentDidMount` e senza di
esso non parte. Nell'immagine di sviluppo intermedia c'è, in quella di produzione no.

### Note di licenza

`handoff/vendor/THIRD-PARTY-NOTICES.md` copre React, ReactDOM e `scheduler` (quest'ultimo
incorporato nel bundle UMD di ReactDOM, quindi redistribuito anche se non è un file a sé).
Tutti MIT, Meta Platforms.

La licenza MIT richiede che avviso di copyright e testo accompagnino le copie del software:
l'immagine Docker **è** la copia distribuita, quindi il file va nell'immagine e in allowlist.
Non è una formalità che si può rimandare a un wiki interno.

Da chiudere separatamente: le licenze dei font in `handoff/fonts/` (Public Sans, Roboto Mono)
non sono ancora documentate.

Stack: FastAPI + SQLAlchemy + Alembic. Motivo: i tre lavori non-CRUD di questa
applicazione sono tutti terreno Python — round-trip XLSX (`openpyxl`, o si riusa
`handoff/xlsx.js` lato client), SMTP con il cron scadenze, e in prospettiva la
vettorizzazione delle planimetrie da PDF citata in fondo al README.

Job scadenze: APScheduler **dentro** il container api, replica singola. Compose non ha
CronJob e una sola replica rende superfluo il leader election. Alternativa se si preferisce
tenerlo fuori: cron dell'host che chiama un endpoint autenticato.

### Trasferimento in rete chiusa

```bash
# macchina di build (con internet)
docker compose build --pull
docker save tsm-web:1.0.0 tsm-api:1.0.0 postgres:17-alpine -o tsm-1.0.0.tar
sha256sum tsm-1.0.0.tar > tsm-1.0.0.tar.sha256

# trasferire: tar + sha256 + compose.yaml + .env.example + template dei secret

# host di destinazione
sha256sum -c tsm-1.0.0.tar.sha256 && docker load -i tsm-1.0.0.tar
docker compose up -d
```

`requirements.txt` con versioni e hash pinnati; immagini base pinnate per digest
(`postgres:17-alpine@sha256:...`), altrimenti il build non è riproducibile e in rete chiusa
non si ha modo di recuperare la versione che funzionava.

### Dipendenze di sviluppo

Separate da quelle di runtime, in `requirements-dev.txt`, e **fuori dall'immagine di
produzione**:

```
playwright==1.62.0
```

Playwright serve alla verifica dell'avvio offline (§5, `tools/offline-boot-test.py`) e ai
test di round-trip degli `_uid` (§8.4), che devono girare sul frontend reale. Pinnato come
tutto il resto: un aggiornamento di Playwright che cambia comportamento del browser
trasformerebbe un test di regressione in un falso allarme.

Usa il Chrome **già installato** (`channel="chrome"`) e non scarica browser propri: in rete
chiusa `playwright install` non funzionerebbe comunque, e su una macchina di build il
download del browser sarebbe l'unico pezzo non riproducibile della catena.

### Secret (file su host, mode 0400, root)

```
postgres_password
smtp_password        → letta dall'api a runtime, mai nel DB, mai in GET /api/settings
session_secret
```

### Backup

`pg_dump -Fc` notturno in una directory bind-mounted che l'agente di backup esistente già
raccoglie; retention 30 giorni; **prova di restore documentata e provata almeno una volta**.
Essendo in rete chiusa, conservare anche il tar delle immagini della versione in produzione:
è l'unico modo per ricostruire l'ambiente senza rifare il build.

### Hardening

- container non-root, `read_only: true` + tmpfs dove possibile, `no-new-privileges`, capability droppate
- Postgres solo sulla rete interna Compose
- rate limit su `/api/auth/login` (l'unico endpoint non autenticato che tocca il DB)
- argon2id per le password; sessioni server-side revocabili
- TLS obbligatorio: il cookie di sessione è `Secure`, senza HTTPS non si entra
- rimuovere l'utenza di bootstrap `admin/admin` (`handoff/inventario.js:39`) — l'import
  iniziale crea il primo admin con password provvisoria e `must_change_pw = true`

---

## 7. Dati di seed: buco da colmare presto

`handoff/inventario.js` non popola `stato`, `garanzia`, `supporto`, `note` (l'helper `D()`
non li imposta, [inventario.js:10](handoff/inventario.js#L10)), e non contiene le chiavi
`notifiche`, `smtp`, `manuale`, `registro`.

Conseguenza: la vista *Scadenze*, i KPI urgenze e **l'intero job email di priorità 3** non
sono mai stati esercitati su dati reali. Prima di progettare quel job serve un seed che
popoli le date di scadenza, altrimenti lo si scrive alla cieca.

---

## 8. Vincoli di progetto vincolanti per l'implementazione

Nove decisioni da rispettare, tutte conseguenza di un fatto: **con il backend, il client
smette di essere l'autorità**. Il prototipo si fida di sé stesso; il server no.

Tre di questi punti (autorizzazione, audit, sync di fase 2) condividono un componente:
un **motore di diff fra due documenti** che classifica ogni cambiamento per ambito. Va
scritto una volta, bene, e testato da solo. È il pezzo più importante del backend.

### 8.1 Sequenza di avvio autenticata: `/auth/me` prima dell'inventario

Oggi il login è un cancello disegnato *sopra* dati già caricati: `componentDidMount` importa
tutto l'inventario e `_doLogin` decide solo cosa mostrare. Trasposto così su HTTP,
**un utente non autenticato riceverebbe l'intero inventario** — sito, rack, IP, seriali.

Sequenza obbligatoria:

```
GET /api/auth/me
  401  → schermata di login. NESSUNA chiamata a /api/inventory.
  200  → se must_change_pw: cambio password forzato, e ancora nessun inventario
       → altrimenti GET /api/inventory
```

`GET /api/inventory` risponde 401 se la sessione manca: non è una cortesia del client, è il
server che rifiuta. Un 401 su qualunque chiamata durante la sessione svuota lo stato locale
(`data: null`) e riporta al login, perché lo stato React resta in memoria dopo la scadenza
del cookie.

### 8.2 `PUT` dell'inventario serializzate e coalescate

`persist()` è chiamata anche durante drag e resize dei rack
([:1707](handoff/Sala%20Server%20v2.dc.html#L1707), [:1735](handoff/Sala%20Server%20v2.dc.html#L1735)):
decine di eventi al secondo. E la versione è una sequenza stretta — due `PUT` in parallelo
dallo stesso client fanno **409 contro sé stessi**.

Regola: **una sola `PUT` in volo, e una sola modifica in attesa** (l'ultima vince, il
documento è completo quindi coalescere significa semplicemente scartare l'intermedio).

```
inFlight   : la PUT in corso
pending    : ultimo documento da inviare (sovrascritto, non accodato)
alla risposta OK → baseVersion = version restituita; se pending, invia
alla risposta 409 → stop, avvisa, offri ricarica; scarta pending
```

Le posizioni intermedie di un drag non hanno valore storico, quindi vanno scartate anche
lato audit: si invia **al rilascio**, non durante. Aggiungere un `beforeunload` se `pending`
o `inFlight` sono valorizzati, altrimenti l'ultima modifica si perde chiudendo la scheda.

### 8.3 Autorizzazione lato server delle modifiche a documento intero

Il punto più delicato. Il `PUT` porta tutto il documento, quindi «questo utente può
scrivere?» non basta: il README assegna ad **admin** la struttura (siti, sale, rack), l'SMTP
e le utenze, e a **edit** solo l'operatività sui dispositivi. Senza controllo, un `edit`
può cambiare qualsiasi cosa semplicemente inviandola.

Il server calcola il diff fra documento corrente e documento ricevuto con il motore
**identity-aware** di §8.10, che restituisce eventi di dominio (`add`, `delete`, `update`,
`move`, `rename`, `reorder`) e non differenze testuali. Ogni evento porta il proprio ambito,
che viene confrontato con il ruolo:

| Ambito | Cosa comprende | Ruolo minimo |
|---|---|---|
| `devices` | dispositivi: creazione, modifica, spostamento, stato | `edit` |
| `structure` | siti, sale, vani, rack, dimensioni, posizioni | `admin` |
| `manuale` | voci del manuale | `admin` |
| `settings` | notifiche, SMTP non segreto | `admin` |

La politica è implementata come modulo puro: vedi **§8.15** per le regole esatte per
evento, il comportamento «tutto o niente» e il formato delle violazioni.

Un `PUT` che tocca ambiti non consentiti → **403 con l'elenco degli eventi incriminati**
(evento, entità, `_uid`, percorso leggibile), e niente viene scritto: il controllo avviene
dentro la stessa transazione della scrittura (§8.11), quindi o passa tutto o non passa niente.

Nota di sostanza sul perché serve il diff identity-aware: **spostare un dispositivo da un
rack a un altro è ambito `devices`** (il README lo assegna agli operatori), ma tocca i
sottoalberi di due rack diversi. Un diff per percorso vedrebbe modifiche sotto
`locations[0].sale[1].racks[3]` e `…racks[7]` e le classificherebbe come `structure`,
negando a un operatore un'azione che deve poter fare. L'ambito si può decidere solo
conoscendo l'*intento*, e l'intento lo dà l'identità.

Le chiavi che non vivono più nel documento — `utenti`, `registro`, e la password SMTP —
vanno **rifiutate se presenti**, non ignorate in silenzio: il silenzio nasconde un client
vecchio o un tentativo. Il server le espone solo tramite i loro endpoint.

Da controllare anche gli invarianti che oggi la UI garantisce da sola e che un `PUT`
artigianale può violare: `u` del device dentro `u` del rack, nessuna sovrapposizione di
slot, rack dentro i limiti della sala, `tipo`/`stato` fra i valori ammessi, id univoci.

### 8.4 Ciclo di vita degli `_uid`

Gli id di business sono rinominabili: `dr.id` del form rack finisce diretto nell'oggetto
([:2809](handoff/Sala%20Server%20v2.dc.html#L2809)), con `_orig` che tiene il vecchio.
Se l'identità è il codice, **rinominare `R01` in `R02` è una cancellazione più un
inserimento**: storico perso, audit bugiardo, foto orfana.

Ogni **location, sala, rack, dispositivo e voce di manuale** porta quindi un campo `_uid`:
un UUID v4, immutabile per tutta la vita dell'oggetto. Il codice diventa un attributo come
gli altri. Rinominare = `update` sullo stesso `_uid`.

I `vani` **non** hanno `_uid`. La decisione, con le sue motivazioni, è in §8.12.

#### Chi genera gli `_uid`: il client

Gli `_uid` li genera **il client** con `crypto.randomUUID()`, al momento della creazione
dell'oggetto. Non il server.

Il motivo è che il documento è un albero che il client costruisce e rinvia intero: se
l'assegnazione fosse del server, ogni creazione avrebbe una finestra in cui l'oggetto esiste
lato client senza identità — e in quella finestra ci stanno un undo, un secondo `PUT`
coalescato (§8.2), o un altro oggetto creato. Generando lato client, l'identità nasce insieme
all'oggetto e non c'è nessun momento in cui una modifica locale riguarda qualcosa di anonimo.
Le collisioni non sono un rischio pratico: sono UUID v4 da un CSPRNG.

⚠ `crypto.randomUUID()` esiste **solo in secure context**: HTTPS, oppure `localhost`. In
produzione TLS è già obbligatorio (§6) e in sviluppo si lavora su localhost, quindi è
coperto — ma se qualcuno prova l'app su `http://10.x.x.x` la funzione è `undefined` e le
creazioni fallirebbero. Va gestito con un errore esplicito all'avvio («serve HTTPS»),
non con un fallback a `Math.random()`: un UUID debole è peggio di un errore visibile.

#### Regole di validazione del server

Sul `PUT /api/inventory` il server confronta gli `_uid` del documento ricevuto con quelli
della versione corrente, e **rifiuta con 422** senza scrivere niente se:

| Caso | Perché è un errore |
|---|---|
| `_uid` mancante su un'entità **esistente** | il client ha perso l'identità: dato distrutto, non modifica |
| `_uid` duplicato nel documento | l'albero non è più univoco: ogni diff diventa ambiguo |
| `_uid` mutato su un'entità esistente | equivale a cancella+crea mascherato da modifica |
| `_uid` presente ma sconosciuto alla versione corrente | client disallineato, o tentativo di iniettare storia |
| `_uid` non conforme a UUID | rifiuto sintattico |

Il `PUT` normale **non fa mai backfill**. Un `_uid` mancante non viene generato dal server:
se lo facesse, un client vecchio che azzera gli `_uid` verrebbe interpretato come «ha
cancellato tutto e ricreato tutto», e il rifiuto è precisamente la protezione che serve.

Entità **nuove** (nessun `_uid` corrispondente nella versione corrente) sono l'unico caso
in cui un `_uid` mai visto è legittimo: viene accettato così com'è, e diventa un evento
`add` (§8.10).

#### L'unica eccezione: l'importer

Lo script di import iniziale da `inventario.js` **può** fare backfill degli `_uid`
mancanti: è un'operazione una-volta-sola, offline, su dati che non hanno ancora storia da
proteggere. È un percorso di codice separato dall'API, non un flag sull'endpoint: la
differenza fra «popolo un DB vuoto» e «accetto una scrittura da un client» non va affidata
a un booleano che qualcuno può passare per sbaglio.

Lo stesso vale per un'eventuale migrazione futura che introduca `_uid` su entità che non
li hanno: migrazione Alembic, non `PUT`.

#### Fix necessario nel frontend

`saveDraft` dei dispositivi ([:2925](handoff/Sala%20Server%20v2.dc.html#L2925)) costruisce
un oggetto nuovo e sostituisce il precedente:

```js
const dev = { id, name: dr.name.trim(), type: dr.type, /* … */ u, h };
```

Così **perde ogni campo che non elenca**, `_uid` compreso — e col server di §8.4 ogni
modifica di dispositivo diventerebbe un 422. Va cambiato in uno spread dell'originale:

```js
const orig = target.devices.find(d => d.id === S.selDev) || {};
const dev  = { ...orig, id, name: dr.name.trim(), /* … */ u, h };
// nuovo dispositivo: orig è {} e l'_uid va generato
if (!dev._uid) dev._uid = crypto.randomUUID();
```

Il percorso dei rack usa `Object.assign(t, …)`
([:2809](handoff/Sala%20Server%20v2.dc.html#L2809)) su un oggetto esistente e conserva già
i campi sconosciuti: per la modifica va bene così, ma la **creazione** di rack, sale e siti
va comunque integrata con la generazione dell'`_uid` (rispettivamente
[:2110](handoff/Sala%20Server%20v2.dc.html#L2110) per i siti e
[:2136](handoff/Sala%20Server%20v2.dc.html#L2136) per le sale).

Attenzione anche all'import tabellare CSV/XLSX
([:2751](handoff/Sala%20Server%20v2.dc.html#L2751)): crea e aggiorna dispositivi in blocco,
quindi deve generare `_uid` per i nuovi e **preservarli** per gli aggiornati. È il percorso
che tocca più righe in una volta, quindi quello dove un `_uid` perso fa più danno.

#### Corrispondenza per l'import da foglio

`_uid` è l'**unica** identità. In particolare `device.id` non lo è: deriva dal nome, è
modificabile, e **non è univoco a livello globale** — due rack possono contenere due
dispositivi con lo stesso `id`.

| Riga | Come si identifica | Spostamento fra rack |
|---|---|---|
| con `_uid` | identità certa, ricerca su tutto l'inventario | **consentito** |
| senza `_uid` | `id`, poi `nome`, **solo dentro il rack di destinazione** | **vietato** |

Senza `_uid` la corrispondenza serve ad aggiornare, mai a spostare: una riga legacy che
combacia con un dispositivo in un **altro** rack viene rifiutata con l'indicazione di
aggiungere la colonna `_uid`. Accettarla vorrebbe dire spostare un dispositivo basandosi su
qualcosa che non è identità — ed è esattamente ciò che il codice precedente faceva in
silenzio, cancellando gli omonimi in tutto l'inventario prima di reinserire.

Zero candidati nel rack e nessuno altrove → dispositivo nuovo. Candidati multipli, o zero
nel rack ma presenti altrove → **rifiuto**, non un tentativo di indovinare.

Due rifiuti ulteriori, entrambi casi in cui l'esito dipenderebbe altrimenti da una scelta
arbitraria:

- **`id` e `nome` che indicano dispositivi diversi**, anche dentro lo stesso rack di
  destinazione: la riga è contraddittoria. Preferire una delle due chiavi vorrebbe dire
  decidere quale informazione dell'utente ignorare, e l'esito dipenderebbe dalla precedenza
  scelta nel codice.
- **Due righe che puntano allo stesso dispositivo** — per `_uid` uguale o perché risolvono
  alla stessa entità per vie diverse: la seconda sovrascriverebbe la prima e il risultato
  dipenderebbe dall'ordine delle righe nel foglio. Il tracciamento è un insieme di
  «rivendicazioni» passato a `matchDeviceForImport`; una riga *nuova* non rivendica nulla,
  quindi due righe nuove distinte restano ammesse.

#### Stato: implementato

| Cosa | Dove |
|---|---|
| logica di identità (generazione, validazione, match, mappatura colonne) | `handoff/identity.js` |
| migrazione una-volta-sola del seed | `tools/migrate-seed-uids.mjs` (197 entità) |
| verifica durevole del seed | `tools/verify-seed-migration.mjs` + `tools/seed-migration.expected.json` |
| fixture neutre rispetto al linguaggio | `fixtures/identity/*.json` (32) |
| test JS (fixture + specifica frontend) | `tools/identity-tests.mjs` (85 test) |
| validatore e motore di diff in Python | `backend/app/identity/` |
| test Python (fixture + proprietà) | `backend/tests/` (175 test) |
| cablaggio nell'app reale | `tools/identity-ui-test.py` |
| round-trip semantico XLSX | `tools/xlsx-roundtrip-test.py` |

Percorsi di ricostruzione coperti: creazione di sito, sala, rack e dispositivo; modifica
manuale di dispositivo (`saveDraft`); voci di manuale (`manSave`); import guidato CSV/XLSX
(analisi e applicazione); import di backup JSON; export CSV/XLSX/JSON con colonna `_uid`.

Quattro difetti latenti trovati mentre si copriva questi percorsi, tutti corretti:

1. `saveDraft` rimuoveva il dispositivo con `filter(d => d.id !== id)`. Poiché gli `id` non
   sono univoci fra rack, modificare un dispositivo poteva cancellarne un omonimo altrove.
   Ora il filtro è per `_uid`.
2. Il foglio XLSX di export («Dispositivi») usa intestazioni maiuscole, ma l'import le
   minuscolizza: quel foglio **è** ri-importabile. Senza `_uid` un giro export→import
   sostituiva l'identità di ogni dispositivo. Ora la colonna c'è.
3. Nello stesso foglio l'altezza si chiama `Altezza U`, che l'import non riconosceva
   (cercava `h`): ri-importandolo **tutte le altezze tornavano a 1**. Risolto con una mappa
   di alias di intestazione (`normalizeHeaders`).
4. Peggiore del precedente e trovato cercandolo: le **etichette degli stati non coincidono
   con le chiavi** (`manutenzione` → «In manutenzione»). L'import confrontava con le chiavi,
   quindi ri-importando il foglio ogni dispositivo in manutenzione o in dismissione
   **tornava ad "attivo"**, azzerando in silenzio il ciclo di vita. Risolto con `parseStato`
   / `parseTipo`, che accettano sia chiavi sia etichette.

`tools/xlsx-roundtrip-test.py` dimostra la preservazione **semantica** del giro
export→import sull'app reale: 86 dispositivi, altezze non predefinite (2, 3, 4 e 6 U) e
stati non predefiniti, confrontati campo per campo. Nessun dispositivo perso, creato o
spostato. Unica differenza ammessa e dichiarata: il giro **materializza i default
documentati** (un dispositivo senza `stato` esce come `attivo`, che è già il valore che la
UI mostra), quindi il confronto è sul valore efficace, non sulla presenza della chiave.

#### Verifica durevole della migrazione

Il controllo non dipende da git — deve restare valido dopo il commit, dopo un rebase e su
una copia senza storia. `tools/seed-migration.expected.json` committa:

- i **conteggi per tipo** (location 3, room 6, rack 102, device 86 — totale 197);
- lo **SHA-256 della forma canonica** del seed: documento con gli `_uid` rimossi
  ricorsivamente e le chiavi ordinate a ogni livello.

Così un dato alterato o perso cambia lo SHA, entità aggiunte o rimosse cambiano i conteggi, e
un cambiamento dei soli `_uid` è ignorato per costruzione — è ciò che rende il controllo
indipendente dai valori casuali generati dalla migrazione. Verificato che lo SHA reagisca
davvero a una modifica di dato, alla rimozione di un dispositivo e a un cambio di geometria
di un vano, e che resti invariato al cambio di un `_uid`.

Se il seed cambia legittimamente: verificare il diff, poi
`node tools/verify-seed-migration.mjs --update`.

#### Test di round-trip obbligatori

La perdita di un `_uid` è un errore **silenzioso**: l'app funziona, l'audit mente, e ci si
accorge mesi dopo quando lo storico serve davvero. Quindi non basta il codice, servono
test che chiudano il giro:

1. **modifica dispositivo** — carica documento, modifica un dispositivo via `saveDraft`,
   `PUT`, ricarica: `_uid` identico, l'evento è `update` e non `add`+`delete`
2. **rinomina rack** — cambia `id` di un rack: `_uid` identico, evento `rename`
3. **spostamento dispositivo** fra rack (drag, [:1832](handoff/Sala%20Server%20v2.dc.html#L1832)):
   `_uid` identico, evento `move`
4. **creazione** di sito, sala, rack, dispositivo: `_uid` presente, conforme a UUID, unico
5. **import tabellare** su righe esistenti: nessun `_uid` cambiato, nessun `add` spurio
6. **undo/redo** dopo ognuna delle precedenti: gli `_uid` sopravvivono al giro nello stack
7. **regressione negativa** — un documento con un `_uid` rimosso a mano deve dare 422,
   non essere accettato in silenzio

I test 1-6 vanno fatti girare sul frontend reale (Playwright è già in dipendenza di
sviluppo per §5, vedi §6), non solo sul modello dati: il bug di `saveDraft` è precisamente
un bug del frontend, e un test che esercita solo l'API non lo vedrebbe.

#### Deep link

I deep link (`#rack=loc/room/rackid`,
[:1124](handoff/Sala%20Server%20v2.dc.html#L1124)) andranno spostati su `_uid`, altrimenti
si rompono al primo rinomino. Non è bloccante per la fase 1, ma va fatto prima di
distribuire link salvati agli utenti.

### 8.5 Foto immutabili con garbage collection degli orfani

Le foto sono indirizzate dal contenuto (`sha256` **univoco**) e **immutabili**: nessun
`UPDATE`. Caricare due volte la stessa immagine restituisce la stessa riga — non è una
gentilezza, è ciò che evita che un giro di controllo in sala raddoppi lo spazio.

L'identità applicativa è un **UUID**; nel documento versionato c'è solo quello. Mai base64,
mai `data:` URL, mai percorsi di filesystem, mai URL fornite dal client, mai il nome del file
originale — che è testo scelto da chi carica e non deve arrivare né in una colonna né in
un'intestazione. Lo schema congelato (§8.16) continua a rifiutare un `foto` che contenga un
`data:` URL o un valore che non sia un UUID.

#### Riferimenti espliciti, non una scansione del documento

```text
inventory_photo_refs (inventory_version, photo_id)   PK su entrambi
```

Ogni volta che si committa una versione si estraggono gli UUID referenziati, si verifica che
esistano tutti — altrimenti `photo_not_found`, 422, e **nessuna** versione né audit — e si
inseriscono le righe **nella stessa transazione** di versione, audit e testa.

Due motivi, il secondo dei quali è il difetto peggiore possibile qui:

1. La GC deve poter chiedere «serve ancora?» in modo esatto. Una scansione del testo
   (`doc::text LIKE '%uuid%'`) dipende dalla serializzazione, costa la lettura di tutte le
   versioni, e sbaglia in silenzio il giorno in cui un UUID compare in un campo di testo.
2. **Le versioni storiche contano.** Guardare solo l'inventario corrente sembrerebbe
   naturale:

   ```text
   v20 → foto A          v21 → foto B (sostituita)
   ```

   Con la sola testa, A «non serve più» e la GC ne cancella i byte. Poi qualcuno torna alla
   v20 e trova un riquadro rotto — e i byte non si ricostruiscono. Un ritorno alla v20 deve
   mostrare A **senza ripristinare né ricostruire niente**.

La camminata che estrae gli UUID è **generica** (qualunque chiave `foto` a qualsiasi
profondità), non strutturale: un riferimento dimenticato significa una foto viva che diventa
cancellabile, mentre un riferimento in più costa una riga. La direzione dell'errore non è
simmetrica, e la camminata segue quella asimmetria.

⚠ La chiave esterna `inventory_photo_refs.photo_id → photos(id)` è **senza `ON DELETE`**:
il database RIFIUTA di cancellare una foto referenziata. È la difesa che regge se la query
della GC viene riscritta male, e copre anche l'intreccio con una scrittura concorrente —
sotto READ COMMITTED la sottoquery potrebbe non vedere un riferimento appena inserito, e in
quel caso è il vincolo a far fallire la cancellazione. Un giro di GC fallito si ripete
domani; dei byte cancellati non tornano.

#### Caricare non aggancia

```text
POST /api/photos (multipart, SOLO admin) → UUID
  → l'UUID entra nella bozza del rack
    → PUT /api/inventory normale, versionato
      → SOLO ADESSO la modifica del rack è salvata
```

Il caricamento è riservato agli **amministratori**: le foto appartengono alla gestione dei
rack, che è già amministrativa, e un operatore che non può creare un armadio non deve poter
consumare spazio binario con immagini che non riesce nemmeno a collegare.

Un `PUT` fallito o in conflitto lascia una foto orfana. È previsto, e l'interfaccia non deve
dire «salvato» dopo il solo caricamento: la modifica del rack è salvata quando
`/api/inventory` conferma la versione nuova.

#### Validazione: tre livelli di sfiducia

L'estensione non si guarda mai; il `Content-Type` del multipart si usa solo per pretendere
che sia **coerente** con i byte (un disaccordo si rifiuta, non si corregge in silenzio); i
byte si annusano e poi si decodificano con una libreria vera.

Elenco chiuso: **JPEG, PNG, WebP**. L'SVG è escluso e non è pignoleria — è un documento XML
che il browser esegue, e servito dalla nostra origine parlerebbe con la sessione dell'utente.
Una fotografia di un armadio non è mai un SVG.

Limiti: 10 MB per il file, ~40 megapixel per l'immagine **decodificata**. Il secondo non è il
primo: un PNG di 40 kB può dichiarare 40000×40000 pixel e pretendere gigabyte per essere
aperto, quindi il controllo si fa sull'intestazione, prima di allocare.

Si **ricodifica sempre**: i byte conservati sono quelli che abbiamo prodotto noi. Ne
seguono tre cose volute — i metadati sparaiscono (GPS compreso: la foto di un rack scattata
col telefono porta la posizione del CED), l'orientamento EXIF viene applicato ai pixel invece
di restare un campo da onorare, e il tipo dichiarato nella risposta è quello del nostro
codificatore, quindi non è influenzabile dal chiamante.

#### Lettura

`GET /api/photos/{uuid}` pretende una sessione autenticata non ristretta (§8.26); qualunque
ruolo può leggere. Nessuna URL pubblica: «tanto l'UUID non è indovinabile» non è controllo
d'accesso, è un segreto che finisce nella cronologia del browser e nei log di un proxy.

```text
Cache-Control: private, max-age=31536000, immutable
X-Content-Type-Options: nosniff
Content-Disposition: inline            (senza filename)
```

`immutable` si può dichiarare perché l'identità È il contenuto: sostituire la foto di un rack
significa un UUID diverso, quindi una URL diversa — la cache non va invalidata, va ignorata.
`private` è la parte che conta: un proxy aziendale condiviso non deve conservare una copia di
fotografie dell'infrastruttura di un cliente servibile a chiunque passi da lì.

#### Nessun `DELETE` via HTTP

Non esiste `DELETE /api/photos/{id}`. Le azioni dell'utente cambiano solo i riferimenti; la
cancellazione fisica appartiene esclusivamente alla GC. Così un amministratore non può
rompere il ripristino di un altro.

#### Garbage collection

Due condizioni, entrambe necessarie:

```text
    nessuna riga in inventory_photo_refs
  E created_at più vecchio della finestra di grazia (24 ore)
```

La grazia copre la finestra in cui una foto è legittimamente orfana: caricata e non ancora
referenziata. Succede a ogni conflitto sul salvataggio, a ogni modulo chiuso senza salvare, a
ogni sessione interrotta. **Mai** cancellare una foto solo perché la testa non la referenzia
più. Se un giorno si introduce la potatura delle versioni, eliminare una versione porta via i
suoi riferimenti (`ON DELETE CASCADE`) e solo allora la foto può diventare eleggibile: la
retention delle versioni determina di fatto quella delle foto.

Gira nel `tsm-worker` (§8.41) ma è un lavoro **logicamente indipendente**: tabella
`maintenance_runs` propria con la stessa forma di `scheduler_runs` (chiave sulla data locale,
quindi recupero e protezione dall'ora ripetuta), orario proprio, e nessuna dipendenza da
`notifications.enabled` — spegnere gli avvisi non deve riempire il disco. Una volta al giorno
basta; non gira a ogni richiesta HTTP.

#### Due ruoli di database, non uno

```text
tsm_api     photos: SELECT, INSERT          (mai UPDATE, mai DELETE)
tsm_worker  photos: SELECT, DELETE          (mai INSERT, mai UPDATE)
```

`DELETE` su `photos` è l'**unico privilegio di cancellazione di tutto lo schema**, e sta solo
nel ruolo del worker. Con un ruolo unico finirebbe anche a chi serve richieste HTTP, e un
difetto in una rotta potrebbe cancellare byte che una versione storica referenzia. Nessuno
dei due riceve la password del proprietario dello schema; l'API non ha alcun privilegio su
`maintenance_runs`. Le concessioni che la 0008 aveva dato a `tsm_api` sulle tabelle del
worker vengono ritirate.

#### Audit

Eventi derivati dal server: `photos.uploaded`, `photos.deduplicated` (due azioni distinte —
una sola direbbe che sono stati conservati byte nuovi anche quando non è vero) e
`photos.gc.collected`, **una riga per giro** con conteggio esatto ed elenco troncato. Mai i
byte, mai base64, mai i metadati EXIF, mai il nome del file locale. Che il riferimento `foto`
di un rack è cambiato lo dice l'audit dell'inventario, come per ogni altro campo.

⚠ Asimmetria deliberata rispetto all'audit del worker delle notifiche: là un guasto del
registro si ingoia, perché la posta è già partita e fingere il contrario farebbe rimandare il
messaggio. Qui l'audit sta nella stessa transazione della cancellazione, e se non si scrive la
cancellazione non avviene: cancellare byte senza lasciarne traccia è esattamente ciò che un
registro esiste per impedire.

### 8.6 Utenze disattivate, non cancellate

`audit.user_id` referenzia `users`. Cancellare fisicamente un utente rompe la tracciabilità,
che è esattamente il motivo per cui l'audit è stato spostato sul server.

Quindi: nessun `DELETE`, solo `disabled_at`. L'azione «Rimossa utenza» della UI
([:2329](handoff/Sala%20Server%20v2.dc.html#L2329)) diventa una disattivazione. Disattivare
**revoca subito tutte le sessioni** dell'utente, altrimenti resta operativo fino alla
scadenza del cookie. Il login rifiuta gli utenti disabilitati.

Lo `username` resta unico anche fra i disabilitati: riusare il nome di un utente disattivato
va gestito come riattivazione esplicita, non come inserimento (che darebbe un errore di
unique incomprensibile all'admin).

Un blocco necessario: **non si può disattivare l'ultimo admin attivo**, e non si può
disattivare sé stessi. Verificato dentro la transazione, non prima.

### 8.7 Password SMTP gestita dall'operations

Non sta nel documento, non sta nel DB, non compare in nessuna risposta dell'API. Non è
"write-only": è **assente dallo schema**, che è più forte — un campo che non esiste non può
essere restituito per errore in un refactoring.

L'API la legge all'invio da `/run/secrets/smtp_password` (Docker secret, file 0400 di root).
`PUT /api/settings` accetta host, porta, utente, mittente, TLS; il campo password non è
previsto e viene rifiutato se presente.

La UI mostra al posto del campo un testo tipo *«gestita dall'operations»* più il pulsante
di prova invio: `POST /api/notifications/test` dimostra che la credenziale funziona senza
mai esporla. La rotazione è una procedura operativa (sostituzione del file + restart), da
mettere nel runbook.

### 8.8 Worker notifiche a istanza singola

Il job scadenze non deve mai inviare due volte. Una sola replica non basta: restart,
esecuzioni manuali e un `docker compose up --scale api=2` distratto bastano a duplicare.

Due difese, entrambe necessarie:

1. **Mutua esclusione** — `pg_try_advisory_lock(<chiave del job>)` in apertura. Chi non
   prende il lock esce senza fare niente. Funziona anche a più repliche, e non lascia lock
   appesi se il processo muore (l'advisory lock cade con la sessione).
2. **Idempotenza** — ogni invio inserisce in `notifications_sent`
   `(device_uid, kind, due_date, sent_on)` con primary key. Un secondo tentativo va in
   conflitto e viene scartato. Così un crash a metà giro riparte senza duplicare la
   prima metà.

`job_runs` registra l'ultima esecuzione riuscita: senza, un job che smette di girare non
si nota — e un sistema di avvisi scadenze che tace è indistinguibile da uno che non ha
niente da dire. Vale la pena un controllo che segnali se `last_ok_at` è più vecchio di
~36 ore.

### 8.9 Metadati di audit derivati dal server, non dal client

Oggi `persist()` passa una stringa già confezionata (`'Spostato rack R01 (Backend)'`) e il
record porta `ruolo: this.state.role`, cioè **il client dichiara chi è e cosa ha fatto**.
Come audit non vale niente: è esattamente il dato che un client compromesso falsifica.

Sul server sono autorevoli solo:

| Campo | Da dove |
|---|---|
| `user_id`, `username` | sessione |
| `role` | tabella `users` al momento della richiesta |
| `ts` | orologio del server |
| `ip` | connessione |
| `detail`, `scopes` | **diff calcolato dal server** fra versione precedente e nuova |

La stringa del client si conserva in `client_hint` — utile per leggere il registro con le
parole della UI, mai come prova. La colonna si chiama così di proposito: chi la interroga
deve vedere dal nome che non è attendibile.

Il vantaggio pratico è che il diff serve già all'autorizzazione (§8.3), quindi l'audit
verbalizza *quello che è cambiato davvero* invece di quello che il client sostiene, e costa
quasi zero in più. Sparisce anche il limite FIFO a 500 righe
([:1401](handoff/Sala%20Server%20v2.dc.html#L1401)): su tabella la retention è per data.

### 8.10 Motore di diff identity-aware

Il componente centrale del backend: lo usano l'autorizzazione (§8.3), l'audit (§8.9) e il
sync di fase 2 (§1). Va scritto una volta, con i suoi test, prima di tutto ciò che lo usa.

#### Il confronto è per `_uid`, non per posizione né per percorso

Regola vincolante: **due entità si confrontano se e solo se hanno lo stesso `_uid`**.
Mai per indice di array, mai per percorso JSON, mai per codice di business.

Perché le alternative non funzionano:

| Approccio | Dove si rompe |
|---|---|
| indice di array | inserire un rack in testa fa sembrare modificati *tutti* i rack successivi |
| percorso JSON (`locations[0].sale[1]…`) | un `reorder` di sale diventa una modifica di massa; uno spostamento di dispositivo diventa `structure` invece di `devices` (§8.3) |
| codice di business (`R01`) | un `rename` diventa `delete` + `add`: storico perso, che è il problema che §8.4 esiste per evitare |

Un diff generico tipo JSON-Patch produce operazioni sulla *forma* del documento; qui serve
sapere cosa è successo al *dominio*. Sono due cose diverse e solo la seconda si può
autorizzare e verbalizzare.

L'algoritmo è quindi: per ogni livello, indicizza base e nuovo per `_uid` in due mappe,
poi confronta le mappe. Le posizioni negli array servono solo a rilevare il `reorder`.
È lineare, non ha bisogno di euristiche di somiglianza, e non può confondere due entità
distinte perché gli `_uid` sono univoci (garantito da §8.4).

#### Eventi di dominio

Sei tipi. Ognuno porta `entity` (`location` | `room` | `rack` | `device`), `uid`, `scope`
(§8.3) e `path`, un percorso leggibile ricostruito dai codici *al momento del diff* e
destinato solo alla lettura umana dell'audit.

| Evento | Quando | Campi propri |
|---|---|---|
| `add` | `_uid` presente nel nuovo, assente nella base | `parentUid`, `snapshot` |
| `delete` | `_uid` presente nella base, assente nel nuovo | `parentUid`, `snapshot` (dalla base) |
| `update` | stesso `_uid`, cambiano attributi che non sono codice, genitore o posizione | `changes: { campo: [prima, dopo] }` |
| `rename` | stesso `_uid`, cambia il codice di business (`id`) o il `name` | `from`, `to` |
| `move` | stesso `_uid`, cambia il genitore **oppure** la posizione | `fromParentUid`, `toParentUid`, `fromPos`, `toPos` |
| `reorder` | l'insieme dei figli è identico ma cambia l'ordine | `parentUid`, `from: [uid…]`, `to: [uid…]` |

Precisazioni che evitano ambiguità in implementazione:

- **`rename` è separato da `update`** anche se tecnicamente è un cambio di attributo, perché
  è il caso che rompe l'identità basata sul codice: averlo come evento distinto rende
  esplicito nell'audit «questo oggetto è lo stesso, ha solo cambiato nome».
- **`move` copre due cose**: cambio di genitore (dispositivo da un rack all'altro, rack da
  una sala all'altra) e cambio di posizione a genitore invariato (`x`/`y` di un rack dopo un
  drag, `u` di un dispositivo fra slot). Entrambe si leggono come «spostato» nella UI, e i
  campi `fromPos`/`toPos` distinguono i due casi senza moltiplicare gli eventi.
- **Un'entità può produrre più eventi nello stesso `PUT`**: rinominare un rack *e* spostarlo
  dà `rename` + `move` sullo stesso `_uid`. Non vanno fusi: gli ambiti potrebbero differire,
  e l'autorizzazione lavora per evento.
- **`reorder` si emette solo se non c'è nessun `add`/`delete` fra i fratelli**, altrimenti
  l'ordine è cambiato come conseguenza e segnalarlo è rumore. Serve al caso reale
  «Riordinata sala» ([:1426](handoff/Sala%20Server%20v2.dc.html#L1426)).
- **`vani`, `notifiche`, `smtp`** non hanno `_uid`: si confrontano per valore e producono un
  `update` sul genitore (la sala) o sulla chiave di impostazione. Le **voci di manuale**
  invece hanno `_uid` e sono entità a pieno titolo (`entity: 'manual'`, ambito `manuale`):
  vedi la decisione in §8.12.
- Un `PUT` che non produce **nessun** evento non è un errore ma non deve creare una versione:
  si risponde 200 con la versione corrente invariata. Altrimenti un client che ri-salva senza
  modifiche gonfia lo storico e l'audit di righe vuote.

Forma indicativa di un evento:

```json
{ "event": "move", "entity": "device", "scope": "devices",
  "uid": "9f1c…", "path": "pomezia-g0 / Backend / R02 / srv-db-01",
  "fromParentUid": "4ab2…", "toParentUid": "77de…",
  "fromPos": { "u": 32 }, "toPos": { "u": 18 } }
```

#### Stato: implementato, non ancora agganciato

`backend/app/identity/` — **puro**: nessun FastAPI, nessun SQLAlchemy, nessuna transazione.
L'aggancio (autorizzazione per ambito, commit atomico, audit) è ai punti 5-6 di §9.

| Modulo | Contenuto |
|---|---|
| `model.py` | attraversamento, vocabolario delle entità, ambiti, campi posizione/etichetta |
| `validator.py` | validazione dell'identità, stessi codici del frontend |
| `diff.py` | eventi di dominio |

**Determinismo garantito**, perché l'audit deve essere riproducibile:

- eventi ordinati per `(tipo di entità, tipo di evento, uid, uid del genitore)`;
- chiavi dei dizionari di modifiche ordinate;
- i sotto-documenti incorporati nei payload (per esempio i `vani` dentro un update di sala)
  vengono **canonicalizzati ricorsivamente**. Senza questo, l'ordine delle chiavi dell'input
  finirebbe nell'output e due richieste equivalenti produrrebbero JSON di audit diversi:
  «deterministico» deve voler dire identico byte per byte, non solo semanticamente uguale.
  Un test lo verifica rimescolando l'ordine delle chiavi in ingresso.

#### Fixture condivise: il contratto fra i due linguaggi

`fixtures/identity/*.json` — 32 casi, ognuno con `before`, `after`, `expectedValid`,
`expectedErrorCodes` e, dove ha senso, `expectedEvents`. Consumate **da entrambe** le suite:
JavaScript verifica validità e codici, Python verifica anche gli eventi.

Le aspettative sono **scritte a mano** (in `tools/make-identity-fixtures.mjs`, che genera i
file), non calcolate da un motore di diff: derivarle dall'implementazione significherebbe
verificarla contro sé stessa.

Coperti: `add`, `delete`, `update`, `rename`, `move` (fra genitori e di sola posizione),
`reorder` (rack e sale), `rename`+`move` sulla stessa entità, soppressione del `reorder` per
add e per delete, cancellazione a cascata, inserimento in testa che **non** deve toccare i
fratelli, non-modifica con lista vuota, update dei `vani` come update della sala, voci di
manuale, impostazioni — e ogni codice di rifiuto. Due test parametrizzati falliscono se un
tipo di evento o un codice di errore resta senza fixture.

Questi test sono la specifica eseguibile dell'autorizzazione: se il diff sbaglia a
classificare, §8.3 concede o nega i permessi sbagliati, e nessun test di endpoint lo
mostrerebbe con la stessa chiarezza.

### 8.11 Commit atomico della versione

Il lock ottimistico di §3 va implementato bene, altrimenti non protegge niente.

**La versione corrente non è `max(version)`**, è `inventory_head.version`. Ogni `PUT` apre
una transazione e come prima cosa prende il lock sulla riga singleton:

```sql
BEGIN;

-- 1. serializza gli scrittori: chi arriva secondo aspetta qui, non fallisce
SELECT version FROM inventory_head WHERE id FOR UPDATE;

-- 2. confronto col baseVersion del client → se diverso: ROLLBACK e 409
-- 3. diff, validazione _uid (§8.4), autorizzazione per ambito (§8.3)
--    → se rifiutato: ROLLBACK e 403/422

INSERT INTO inventory (version, doc, author_id) VALUES ($new, $doc, $user);
UPDATE inventory_head SET version = $new, updated_at = now() WHERE id;
INSERT INTO audit (user_id, username, role, ip, inventory_version,
                   scopes, events, client_hint)
       VALUES (…, $new, …);

COMMIT;
```

Tutte e tre le scritture nella **stessa transazione**. Non è pignoleria:

- versione senza audit = una modifica non tracciata, cioè esattamente il buco che spostare
  l'audit sul server doveva chiudere;
- audit senza versione = il registro racconta una modifica che non è mai avvenuta;
- `inventory_head` aggiornato senza la riga in `inventory` = la FK lo impedisce, ma senza
  transazione unica ci sarebbe una finestra in cui `GET` legge una versione inesistente.

Il `FOR UPDATE` sul singleton serve anche a rendere la decisione del 409 **race-free**.
Senza lock, sotto `READ COMMITTED`, due `PUT` concorrenti leggono entrambi la stessa
versione corrente, entrambi la giudicano valida, e il secondo scopre il conflitto solo come
violazione di chiave primaria su `inventory.version` — un errore 500 travestito, invece del
409 che il client sa gestire. Con il lock, il secondo scrittore *aspetta*, poi rilegge la
versione aggiornata e restituisce un 409 corretto.

`SELECT … FOR UPDATE` su una riga sola è la scelta giusta rispetto alle alternative:
`SERIALIZABLE` costringerebbe a gestire i retry per `serialization_failure` su ogni scrittura,
e un advisory lock non partecipa alla transazione (verrebbe rilasciato a fine sessione, non
a fine transazione). Il costo è che le scritture dell'inventario sono strettamente seriali —
per un'applicazione con pochi operatori, irrilevante.

Il nuovo numero di versione si ricava da `inventory_head.version + 1` **dentro** la
transazione, non da una sequenza: una sequenza avanzerebbe anche sui rollback, e i buchi
renderebbero meno leggibile lo storico che gli utenti vedono in «versioni».

Bootstrap: la migrazione che crea le tabelle inserisce la versione 0 con il documento vuoto
(o l'importer inserisce la 1 con i dati iniziali) e la riga di `inventory_head`. `PUT`
presuppone che la testa esista; non deve gestire il caso «tabella vuota», che sarebbe un
percorso raro e quindi mai testato davvero.

### 8.12 `vani` e voci di manuale: decisione esplicita

Domanda posta perché la fase 2 normalizza entrambi in tabelle proprie, e lasciarla implicita
significherebbe scoprirla durante la migrazione.

#### `vani` → **value object posseduti dalla sala**, senza `_uid`

Non è una preferenza estetica, è quello che dice il codice. I vani nascono con la sala
([:2176](handoff/Sala%20Server%20v2.dc.html#L2176), un unico vano che copre la stanza) e da
lì in poi vengono **soltanto disegnati** ([:1565](handoff/Sala%20Server%20v2.dc.html#L1565)
e [:1589](handoff/Sala%20Server%20v2.dc.html#L1589)). Non esiste interfaccia per aggiungerne,
rimuoverne o modificarne uno: non hanno codice, non hanno nome, l'utente non può nominarne
uno né distinguerlo da un altro.

Di conseguenza nessuna domanda di audit su un vano è distinguibile da «è cambiata la
geometria della sala», e dare identità a oggetti che il client non ha modo di gestire
singolarmente sarebbe un obbligo di conservazione senza alcun beneficio.

Conseguenza per la fase 2: la tabella `vani` mantiene una chiave primaria surrogata come
**identità di riga**, non come identità di dominio. Non viene mai esposta al client e le
righe si **riscrivono in blocco** quando cambia la geometria della sala (`DELETE` dei vani
della sala + `INSERT` dei nuovi, nella stessa transazione). Se un giorno comparirà un editor
di vani, allora serviranno gli `_uid` — e sarà una migrazione, non una sorpresa.

#### Voci di manuale → **entità identificate**, con `_uid`

Il contrario, per ragioni simmetriche: gli admin le creano, modificano ed eliminano una per
una, hanno un `id` e un titolo, la fase 2 le normalizza in `manual_entries`, e la UI le
indirizza singolarmente.

E soprattutto `manSave` ([:1336](handoff/Sala%20Server%20v2.dc.html#L1336)) ricostruiva
l'oggetto da zero e lo sostituiva nell'array — **esattamente la stessa classe di bug di
`saveDraft`**, trovata solo perché la domanda è stata posta. Senza `_uid` ogni modifica di
una voce di manuale sarebbe stata un delete+add.

Ambito per l'autorizzazione: `manuale` (admin), come già in §8.3.

### 8.13 `schemaVersion`: la forma del documento, non la revisione

Due numeri diversi, da non confondere mai:

| | Cosa conta | Quando cambia |
|---|---|---|
| `inventory_head.version` (§8.11) | le modifiche ai **dati** | a ogni salvataggio |
| `schemaVersion` (documento) | la **forma** del documento | solo quando cambia la struttura |

La revisione cresce continuamente e non dice nulla su come interpretare il contenuto; la
versione di schema determina se il documento è interpretabile senza migrazione. Un inventario
alla revisione 900 è ancora `schemaVersion: 1`.

Regole del percorso normale (`PUT`, import JSON):

| Caso | Esito |
|---|---|
| `schemaVersion` = corrente | accettato |
| assente | `schema_version_missing` — precede l'introduzione del campo, serve migrazione |
| più vecchia | `schema_version_too_old` — serve una migrazione esplicita |
| più recente | `schema_version_too_new` — il client conosce una forma che il server non sa interpretare |
| non intero | `schema_version_invalid` |

Come per gli `_uid` (§8.4), **il percorso normale non aggiorna lo schema in silenzio**: rifiuta
e rimanda alla migrazione. Solo `tools/migrate-seed-uids.mjs` porta un documento alla versione
corrente. La canonicalizzazione (§8.14) non inventa il campo, per la stessa ragione.

⚠ Attenzione a `True` in Python: è un `int`, quindi un booleano passerebbe per la versione 1 se
non lo si escludesse esplicitamente. C'è un test.

Nota sul campo `versione: 3` presente nel seed: era un contatore informale del prototipo,
senza semantica applicata da nessuna parte. Resta dov'è per non alterare i dati, ma **non** è
la versione di schema.

Implementazione: `backend/app/identity/schema.py`, `handoff/identity.js`.

### 8.14 Forma canonica: i default documentati, materializzati

Il prototipo tratta l'assenza di certi campi come equivalente a un default: `d.stato ||
'attivo'`, `d.h || 1`, `TYPES[d.type] || TYPES.altro`, `(rk.seriali || [])`. Un dispositivo
senza `stato` e uno con `stato: "attivo"` sono la stessa cosa per l'applicazione e per l'utente.

Senza canonicalizzazione quella equivalenza diventa rumore: l'import che scrive esplicitamente
i default produrrebbe un `update` **per ogni dispositivo**, un audit pieno di modifiche che non
sono modifiche, e uno SHA del seed che cambia senza che sia cambiato nulla.

Quindi la regola: **canonicalizzare prima di confrontare e prima di calcolare hash.** Il diff
(§8.10) canonicalizza entrambi i documenti in ingresso; la verifica del seed canonicalizza
prima di hashare.

Proprietà garantite, con test dedicati:

- **pura** — non modifica l'input;
- **idempotente** — `canonicalise(canonicalise(d)) == canonicalise(d)`;
- **non inventa `_uid`** — il backfill è solo della migrazione (§8.4);
- **non inventa `schemaVersion`** — un documento senza versione va rifiutato (§8.13);
- **non inventa `notifiche` / `smtp`** — completa i sotto-campi solo se l'oggetto esiste già,
  altrimenti il primo salvataggio riporterebbe modifiche che l'utente non ha fatto;
- **non materializza `smtp.password`** — non vive nel documento (§8.7) e reintrodurla come
  stringa vuota la rimetterebbe nello schema;
- **conserva i falsy espliciti** — `""`, `0` e `false` sono valori dell'utente, non assenze.
  Solo `None`/`undefined` viene sostituito.

Le due tabelle di default (`ENTITY_DEFAULTS`, `SETTINGS_DEFAULTS`) esistono in doppia copia —
`backend/app/identity/canonical.py` e `handoff/identity.js` — e vanno tenute allineate. Ogni
voce corrisponde a un `|| default` che l'applicazione già applica in lettura: materializzarlo
non cambia il significato del documento, lo rende esplicito.

Nota: l'applicazione **non** canonicalizza a runtime, per non modificarne il comportamento in
questo commit. La canonicalizzazione è lato server (diff) e lato verifica (hash). È il motivo
per cui `tools/xlsx-roundtrip-test.py` deve ancora dichiarare che il giro export→import
materializza i default.

### 8.15 Politica di autorizzazione: pura, sugli eventi

`backend/app/authz/policy.py` — **pura**: consuma la lista di eventi del diff (§8.10) e
decide. Nessun FastAPI, nessun database, nessuna sessione: chi è l'utente e che ruolo ha lo
stabilisce il chiamante. L'aggancio è il punto 6 di §9.

| Ruolo | Può |
|---|---|
| `view` | **niente**: nessuna scrittura sull'inventario |
| `edit` | sui **dispositivi**: `add`, `update`, `rename`, `move`, `delete` |
| `admin` | tutto: dispositivi, rack, sale, siti, voci di manuale, impostazioni |
| rollback | **solo `admin`** |

**Tutto o niente.** Si esamina l'insieme *completo* degli eventi e se anche uno solo è vietato
l'intera modifica viene respinta. Applicare la parte consentita significherebbe scrivere un
documento che l'utente non ha composto e lasciare l'inventario in uno stato che nessuno ha
chiesto. Il caso che lo rende evidente è la **cascata**: eliminare un rack produce il `delete`
del rack più un `delete` per ogni dispositivo contenuto — i delete di dispositivo sarebbero
concessi a `edit`, quello del rack no, e una politica che guardasse un evento alla volta
lascerebbe passare metà operazione.

Scelte che vale la pena rendere esplicite:

- **`reorder` non è concesso a `edit`**, nemmeno sui dispositivi: riordinare una collezione è
  disposizione, che il README assegna alla struttura. Un operatore *sposta* (`move`), non
  riordina.
- **`admin` è il default per ciò che non è previsto.** Un tipo di entità o di evento nuovo
  nasce ristretto, invece di diventare scrivibile per distrazione.
- **Un ruolo ignoto è un errore**, non un permesso vuoto: si fallisce in chiuso
  (`unknown_role`). Vale anche per `"Admin"` con la maiuscola.
- **Insieme vuoto = consentito** a qualunque ruolo, `view` compreso: non è una scrittura, e un
  `PUT` che non produce eventi non deve nemmeno creare una versione (§8.10).
- **Il rollback non si autorizza per ambito** perché tocca tutto: è un'operazione a sé
  (`authorize_rollback`).

Violazioni leggibili dalla macchina e in ordine deterministico:
`{code, role, entity, event, scope, requiredRole, uid, message}`. `requiredRole` serve al
client per dire «serve un amministratore» invece di un rifiuto generico. Vengono riportate
**tutte**, non solo la prima.

Fixture in `fixtures/policy/` (32), consumate da `backend/tests/test_policy.py`. Le più
significative non elencano gli eventi a mano ma li ricavano dal **motore di diff reale** via
`fromIdentityFixture`, così cascate, `rename`+`move` e `reorder` sono quelli che il motore
produce davvero e non quelli che immaginavo producesse. Coperti: insiemi vuoti per i tre
ruoli, ogni evento di dispositivo per `edit`, mescolanze consentito/vietato (una e più
violazioni), cascata negata a `edit` e concessa a `admin`, `reorder` di rack e di sale,
soppressione del `reorder` che non deve generare violazioni fantasma, `vani` come update di
sala, voci di manuale, impostazioni, ruolo ignoto e i tre casi di rollback.

### 8.16 Schema congelato del documento per il percorso normale

Il `PUT` normale accetta **soltanto** la forma corrente. Allowlist alla radice —
`schemaVersion`, `locations`, `manuale` — e nient'altro. Una chiave nuova va aggiunta di
proposito, insieme al codice che la gestisce.

| Rifiutato | Perché |
|---|---|
| `users`, `utenti` | vivono nella tabella `users` (§8.6) |
| `audit`, `registro` | l'audit è lato server (§8.9) |
| `settings`, `notifiche`, `smtp` | vivono nella tabella `settings` (§8.7) |
| `versione` | contatore informale del prototipo, sostituito da `schemaVersion` (§8.13) |
| qualunque chiave `*password*`, a ogni profondità | nessuna credenziale in un JSONB versionato, servito ai client e conservato per sempre |
| `foto` con un `data:` URL | le foto stanno nella tabella `photos` (§8.5); nel documento va il loro id |
| `foto` che non è un UUID | riferimento non valido |
| `_uid` mancanti | §8.4 |
| `schemaVersion` diverso da quello in testa | un salvataggio non fa evolvere lo schema (§8.13) |
| documento oltre il limite di dimensione | ogni versione è una riga append-only: un documento gonfio si moltiplica per il numero di salvataggi |
| numeri che JSONB non conserva, a ogni profondità | `json_number_not_roundtrippable`: vedi l'invariante del magazzino qui sotto |

#### L'invariante del magazzino

> Ogni **valore** e ogni **chiave** accettati dal `PUT` normale devono essere
> **rappresentabili senza perdite** da PostgreSQL JSONB, secondo la semantica del
> digest canonico del repository.

Due implementazioni della stessa regola, non due rattoppi indipendenti:

| | che cosa fa PostgreSQL | come si manifestava | codice |
|---|---|---|---|
| **numeri** | cambia il valore in **silenzio** | digest registrato diverso dal documento riletto: no-op non riconosciuto, diff attribuito all'utente, fase 2B che aborta | `json_number_not_roundtrippable` |
| **testo** | **rifiuta** la scrittura | `INSERT` fallito a metà salvataggio: **500** invece di 422 | `json_string_not_roundtrippable` |

Le due regole rispondono a domande diverse su tipi diversi, ma la **visita del documento è una sola** (`representable.py`): un documento percorso due volte in due modi diversi lascia scoperta una metà, ed è esattamente così che il buco è nato - la ricerca delle password percorre solo i valori dei dizionari, e gli elementi delle liste non li guardava nessuno.

##### I numeri: che cosa PostgreSQL cambia, misurato

Il repository dava l'invariante per vera, e **non lo era**: `inventory_versions.doc` è JSONB, JSONB tiene i numeri in
`numeric`, e `numeric` non ha il segno dello zero né la notazione esponenziale.
Misurato:

```text
-0.0                    →  0.0
1e+16, 1e+20, 1.5e+300  →  10000000000000000, …           (interi!)
Infinity, -Infinity, NaN→  PostgreSQL li rifiuta          (500 all'INSERT)
10.0, 0.30000000000000004, 1e-09, 5e-324, interi di ogni misura   →  intatti ✔
```

Il confronto è sulla **serializzazione**, non sul valore: `-0.0 == 0.0` è vero in
Python e `json.dumps` scrive due cose diverse, cioè due digest diversi. Una verifica
scritta con `==` avrebbe dichiarato fedele proprio il caso che non lo è — è successo,
alla prima versione della sonda.

##### Perché non si ricalcola il digest

Ricalcolarlo dopo la rilettura sarebbe registrare come «accettato» un documento
**diverso da quello inviato** — la stessa cosa che «rifiutare, non ripulire in
silenzio» esiste per impedire, fatta a un livello più basso e in modo invisibile. Le
conseguenze di un digest che non sopravvive alla rilettura sono concrete:

- il **no-op canonico** (§8.18) non riconosce più un documento identico, e un secondo
  invio identico crea una versione nuova;
- il **diff** (§8.10) attribuisce all'utente una modifica fatta da PostgreSQL;
- il confronto dei digest della **fase 2B** (§8.42) trova l'incoerenza e si rifiuta di
  costruire la proiezione, perché non ha un riferimento di cui fidarsi. È così che il
  problema è stato scoperto.

Quindi: se il magazzino non può conservare un valore, **il documento si rifiuta prima
di persisterlo**, con `json_number_not_roundtrippable`, il percorso del campo e non il
documento inviato. Il rifiuto avviene al passo 1 di §8.17, quindi **prima** del lock
della testa, del diff di identità, della versione, dell'audit e di qualunque scrittura
sulla proiezione. Un test lo prova nel modo che conta: con la riga di testa bloccata da
un'altra transazione, il rifiuto arriva **immediato** invece di scadere sul lock.

##### La regola è una previsione, l'oracolo è il database

La regola è pura per necessità — gira prima di qualunque accesso al database — ma non è
un'approssimazione scritta a mano: un test su PostgreSQL reale la confronta con il
database su un corpus di 37 valori, e se i due dissentono su uno solo quel test è
rosso.

Il confine non è una soglia scelta a mano: è il punto in cui `repr` passa alla
notazione esponenziale **positiva**, cioè in cui PostgreSQL scriverebbe un intero.
`1234567890123456.0` passa, `1e+16` no. Gli esponenti **negativi** (`1e-09`,
`2.5e-05`, `5e-324`) passano tutti, perché la scala resta.

##### Perché la proiezione può usare `extra` e l'istantanea non può

Sono due promesse diverse. La proiezione (§8.42) è **derivata**: un valore che una
colonna tipizzata non può contenere viaggia in `extra` (JSONB) e il documento si
riassembla identico; e la proiezione si ricostruisce da zero quando si vuole.

L'istantanea **è il documento**, immutabile e per sempre. Non ha un `extra` in cui
mettere ciò che non entra: è l'unica copia. Un ripiego qui vorrebbe dire conservare
accanto al documento una correzione da riapplicare a ogni lettura — cioè un secondo
formato canonico, e due formati canonici divergono. Per l'istantanea l'unica risposta
corretta è rifiutare in ingresso.

⚠ **La regola è una sola**, condivisa con la mappa relazionale: la domanda «questo
numero sopravvive a un giro attraverso `numeric`?» è la stessa per una colonna
`numeric` e per JSONB, perché JSONB i numeri li tiene in `numeric`. Due
implementazioni divergerebbero sui casi limite, cioè proprio dove la regola serve. Ciò
che cambia fra i due usi non è la regola: è la **conseguenza**.

⚠ Le versioni scritte **prima** di questa correzione restano come sono — sono
immutabili, e riscriverle sarebbe cambiare la storia. Su una testa così la
ricostruzione della proiezione **aborta** con `digest_della_versione_incoerente`, che è
la diagnosi corretta; un test la riproduce inserendo la riga direttamente, come è nata.

##### Il testo: che cosa PostgreSQL conserva, misurato

Corpus di 34 stringhe provate **sia come valore sia come chiave** (ASCII, italiano
accentato, greco, cirillico, CJK, arabo, emoji BMP e non-BMP, sequenze combinanti,
newline, CR, CRLF, tab, controlli U+0001/U+001F/U+007F, U+2028, BOM, noncaratteri
U+FFFE/U+FFFF/U+FDD0, coppie surrogate valide, piano 16). **Sopravvive tutto tranne due
famiglie:**

```text
"a\u0000b"   NUL, in qualsiasi posizione   il DATABASE rifiuta
"a\ud800b"   surrogato spaiato             la CODIFICA rifiuta, prima del database
```

I due meccanismi sono diversi e vale la pena saperlo, ma per chi salva sono la stessa
cosa: il documento non si può conservare. Un surrogato spaiato **ci arriva davvero**,
perché `json.loads` accetta quel letterale senza protestare e produce una `str` che non
è codificabile in UTF-8.

Due cose che **non** sono un problema di rappresentabilità, e che i test fissano perché
la differenza conta: PostgreSQL **non normalizza** Unicode (una `e` più un accento
combinante torna in due code point, non precomposta - se normalizzasse sarebbe una
modifica silenziosa come quella dei numeri), e jsonb **riordina le chiavi e collassa i
duplicati**, che il digest canonico rende irrilevante. «Torna diverso» e «torna con un
significato diverso» sono due cose distinte.

##### Non si ripulisce, e per il testo meno che mai

Nessuna normalizzazione, nessun `strip`, nessuna sostituzione: sarebbe salvare un
documento diverso da quello inviato, e su un nome sarebbe una modifica che l'utente vede
nel registro **attribuita a sé**. Un controllo statico verifica che la regola non
contenga `unicodedata`, `normalize`, `.replace(` o `.strip()`.

##### Le CHIAVI sono dati dell'utente

Il modello delle entità è aperto (§8.42): un campo ignoto sopravvive al salvataggio e
finirà in `extra`. Una chiave ignota non è diversa, e una chiave non scrivibile fa
fallire l'inserimento come un valore. Questo non passa:

```json
{"_uid": "...", "campoNormale": "va bene", "chiave\u0000rotta": "anche questo va bene"}
```

Una chiave non rappresentabile **non si può nemmeno nominare** nell'errore: il percorso
diventa `locations[0].sale[0].<chiave n.3>`, cioè genitore più posizione. Vale anche
per i percorsi dei valori che quella chiave contiene.

##### Una capability, consumatori diversi

«PostgreSQL conserva questa stringa?» ha una risposta sola, e la usa anche la mappa
relazionale (`_is_str`). Ciò che cambia è la **conseguenza**, e per il testo è più
stretta che per i numeri: un numero non rappresentabile trova posto in `extra`, che è
JSONB e lossless; una stringa che PostgreSQL rifiuta **non entra nemmeno in `extra`**.
Perciò nella proiezione non è `carried_verbatim` (avviso, «integro ma non
interrogabile») ma `text_not_representable` (**errore**): quella riga non si può
scrivere affatto.

⚠ **Asimmetria con i numeri, e va detta:** non esistono e non possono esistere versioni
storiche con testo rotto, perché PostgreSQL non le avrebbe accettate - mentre i numeri
non rappresentabili *sono* entrati prima della correzione. Il controllo sul modello
relazionale è quindi irraggiungibile dalla testa, e serve perché `validate_model` gira
anche su modelli che non vengono dal database, e perché la fase 2C deve fermarsi lì
invece di scoprirlo da un errore di psycopg a metà transazione.

Implementazione: `backend/app/inventory/{json_numbers,json_strings,representable}.py`;
test `test_json_numbers.py` e `test_json_strings.py` (puri, la previsione),
`test_snapshot_numbers_pg.py` e `test_snapshot_strings_pg.py` (PostgreSQL reale,
l'oracolo).

**Rifiutare, non ripulire in silenzio.** Uno scarto silenzioso nasconde un client vecchio,
una migrazione dimenticata o un tentativo — e in tutti e tre i casi si vuole saperlo. E
ripulire vorrebbe dire salvare un documento *diverso* da quello inviato, facendo divergere
ciò che il client crede di avere salvato da ciò che c'è nel database.

Le chiavi ignote hanno un codice a parte (`unknown_root_key`) da quelle estratte
(`forbidden_root_key`): le cause sono diverse — un client sperimentale contro una migrazione
non fatta — e il messaggio deve dirlo.

La ricerca di password e foto **non si fida della struttura**: percorre tutto il documento a
qualsiasi profondità, perché una credenziale nascosta in un ramo che lo schema non prevede è
esattamente il caso che conta.

`strip_legacy_fields()` consuma e toglie quelle radici, e **solo la migrazione la chiama**.
Per un documento legacy contenere quei campi è normale: rimuoverli è il lavoro previsto.

Implementazione: `backend/app/inventory/document.py`.

### 8.17 Repository dell'inventario: la transazione

`backend/app/inventory/repository.py`. Nessun endpoint HTTP, nessuna autenticazione: chi è
l'attore lo dichiara il chiamante (`Actor`). Il chiamante possiede anche la transazione, così
il repository si compone con altre scritture senza aprire transazioni annidate di nascosto.

Ordine, che è la sostanza e non una preferenza:

| # | Passo | Nota |
|---|---|---|
| 1 | schema del documento, limite di dimensione, **fedeltà numerica** (§8.16) | **prima** del database: un documento malformato non deve prendere un lock — provato con la testa bloccata da un'altra transazione |
| 2 | canonicalizzazione del candidato | §8.14 |
| 3 | lock e caricamento della testa | `SELECT … FOR UPDATE` |
| 4 | confronto con `baseVersion` | race-free grazie al lock |
| 5 | validazione della transizione di identità | §8.4 |
| 6 | generazione degli eventi | §8.10 |
| 7 | autorizzazione dell'insieme **completo** | §8.15 |
| 8 | inserimento di versione e audit | stessa transazione |
| 9 | aggiornamento della testa | stessa transazione |
| 10 | commit | del chiamante |

Fra il 4 e il 7 si inserisce il **no-op canonico**: se lo SHA della forma canonica del
candidato è identico a quello della testa, si restituisce la versione corrente e non si
scrive nulla — né versione né audit. Un salvataggio che non cambia niente non deve gonfiare
lo storico, e con la canonicalizzazione questo copre anche il client che scrive
esplicitamente i default.

#### Modifica rispetto a §8.11: la versione la genera il database

§8.11 prevedeva `inventory_head.version + 1` calcolato in applicazione, per evitare i buchi.
La scelta è cambiata: `inventory_versions.version` è una **identity bigint**. Motivo: il
numero è unico per costruzione anche se qualcuno aggirasse il lock, e non richiede un
read-modify-write. I buchi (una transazione annullata consuma un valore) sono irrilevanti,
perché il client confronta la versione per **uguaglianza** con la testa: non conta gli
incrementi né presume che siano contigui.

#### ⚠ Il lock non deve contenere una JOIN

Trovato dal test di concorrenza su Postgres reale, e vale la pena scriverlo perché sarebbe
stato un guasto intermittente in produzione.

La prima implementazione bloccava la testa e leggeva il documento in un'unica query:

```sql
SELECT h.version, v.doc FROM inventory_head h
  JOIN inventory_versions v ON v.version = h.version
 WHERE h.id IS TRUE FOR UPDATE OF h;
```

Sotto `READ COMMITTED`, quando un `SELECT … FOR UPDATE` aspetta una transazione concorrente,
al risveglio Postgres rivaluta la qualificazione della riga **bloccata** sulla sua versione
aggiornata (EvalPlanQual), ma **le altre tabelle del join restano lette con lo snapshot
originale**. Il perdente non vedeva la versione appena inserita dal vincitore, il join non
trovava nulla, e il risultato era «inventario non inizializzato» invece di un conflitto.

Rimedio: bloccare la sola riga di testa, poi leggere il documento con una query separata, che
parte da uno snapshot di comando nuovo e vede quanto il vincitore ha committato.

#### Attore: istantanea, non riferimento

`actor_username` e `actor_role` vengono **copiati** nella versione e nell'audit.
`actor_user_id` è un uuid opzionale — nullable e per ora senza foreign key, perché la tabella
`users` arriva con l'autenticazione; la FK va aggiunta in quella migrazione.

Sono istantanee perché l'audit deve raccontare chi era quella persona *allora*: deve
sopravvivere alla disattivazione dell'utenza (§8.6) e a un cambio di ruolo.

#### Il testo del client è solo testo

`client_hint` è troncato (500 caratteri) e non descrive nulla di autorevole: ciò che è
cambiato lo dice `audit.events`, calcolato dal server (§8.9). È una stringa non attendibile
che finisce in una colonna, e non deve poter diventare un vettore di volume.

#### Bootstrap

Percorso dedicato una-volta-sola, che fallisce se la testa esiste già — separato dal
salvataggio per la stessa ragione per cui il backfill degli `_uid` è uno script a parte
(§8.4): la differenza fra «popolo un database vuoto» e «accetto una scrittura» non va
affidata a un booleano che qualcuno può passare per sbaglio in una richiesta. È l'unico posto
dove `from_legacy=True` può consumare e togliere le radici estratte.

#### Test di integrazione su PostgreSQL reale

Nessun doppio: il comportamento che conta — `FOR UPDATE`, identity bigint, atomicità del
rollback — è comportamento del database. `tools/run-backend-tests.ps1` avvia un Postgres
dedicato; senza `TSM_DB_URL` i test PG si saltano e resta la suite pura.

Coperti: bootstrap e sua unicità (anche il vincolo singleton nel database), append-only,
versioni generate dal database e crescenti, `baseVersion` superata, no-op canonico e no-op da
soli default, no-op consentito a `view`, autorizzazione (`view` che non scrive, `edit` sulla
struttura, cascata negata a `edit` e concessa a `admin`), identità sostituita, radici vietate,
`schemaVersion` cambiato dal client, audit nella stessa transazione con eventi del server,
troncamento del `client_hint`, istantanea dell'attore, **guasti iniettati all'inserimento
dell'audit e all'aggiornamento della testa** (nessuno stato parziale sopravvive),
**scritture concorrenti con lo stesso `baseVersion`**, e la lettura corrente che usa la testa
anche quando esiste una versione più alta inserita fuori banda.

### 8.18 Il no-op precede il conflitto: PUT idempotente

L'ordine fra confronto di hash e confronto di `baseVersion` è una scelta di
semantica, non di stile.

Il caso reale: il commit va a buon fine, ma la risposta non arriva al client — rete, timeout,
scheda chiusa. Il client riprova con il **vecchio** `baseVersion` e lo **stesso** documento.
Confrontando prima il `baseVersion` gli si restituirebbe un conflitto per una scrittura che è
già la sua, e l'utente leggerebbe «modificato da un altro utente» a fronte della propria
modifica riuscita.

| Situazione | Esito |
|---|---|
| hash candidato == hash in testa | `200`, `changed=false`, versione corrente — **qualunque** `baseVersion` |
| hash diverso, `baseVersion` superato | `409` con `currentVersion` e `currentSha256` |
| hash diverso, `baseVersion` corrente | salvataggio normale |

Il 409 porta `currentSha256` così il client può decidere senza un secondo giro: confrontando
l'hash con il documento che ha in mano capisce se la testa è già quello che voleva scrivere.

#### ⚠ Conseguenza scoperta dai test: l'hash deve includere l'identità

Il digest canonico originariamente **rimuoveva** gli `_uid`. Era corretto quando la
validazione dell'identità precedeva il controllo di no-op. Invertito l'ordine, non lo è più:
un documento che sostituisce l'`_uid` di un dispositivo lasciando invariato tutto il resto
avrebbe lo stesso digest della testa, e sarebbe accettato come no-op — con un `200` e
`changed=false`. La sostituzione di identità che §8.4 esiste per rifiutare passerebbe in
silenzio.

L'identità è parte del significato del documento, quindi è parte del suo digest. Il caso
«solo gli `_uid` sono diversi» resta contenuto diverso e prosegue verso la validazione della
transizione, che lo rifiuta.

Restano **due digest diversi per due scopi diversi**, e non vanno confusi:

| Digest | `_uid` | Scopo |
|---|---|---|
| `canonical_sha256` (repository) | **inclusi** | riconoscere una richiesta ripetuta |
| `tools/verify-seed-migration.mjs` | **rimossi** | confrontare i dati fra rigenerazioni con identità casuali |

### 8.19 Append-only imposto dai privilegi, non dal codice

Finché l'API si collegava come proprietario dello schema, «append-only» era una promessa del
codice applicativo: un difetto, un `UPDATE` scritto per sbaglio o un'iniezione l'avrebbero
smentita senza che il database obiettasse. Ora sono due ruoli.

| | Ruolo | Cosa può |
|---|---|---|
| migrazioni, DDL, bootstrap | proprietario (`tsm`) | tutto |
| servire richieste | runtime (`tsm_api`) | vedi sotto |

| Tabella | Privilegi del runtime |
|---|---|
| `inventory_versions` | `SELECT`, `INSERT` — mai `UPDATE`, mai `DELETE` |
| `audit` | `SELECT`, `INSERT` — mai `UPDATE`, mai `DELETE` |
| `inventory_head` | `SELECT`, `UPDATE` — mai `INSERT`, mai `DELETE` |
| `users`, `sessions` | `SELECT`, `INSERT`, `UPDATE` — mai `DELETE` (§8.6) |
| `alembic_version` | `SELECT` (serve alla readiness) |

`INSERT` sulla testa è escluso di proposito: la riga nasce una volta sola, nel bootstrap, che
gira come proprietario. Così **«il bootstrap non passa da HTTP» non è una convenzione ma un
privilegio che l'API non ha.** Lo smoke test lo verifica con `has_table_privilege` sul database
in esecuzione, e i test di integrazione provano che ogni riscrittura della storia riceve
`permission denied`.

La password del ruolo non sta in una migrazione — finirebbe nel repository e nell'immagine. La
migrazione crea il ruolo senza password; `scripts/migrate.py` gliela imposta a ogni avvio
leggendola da un secret, così la rotazione è sostituire il file e riavviare. `ALTER ROLE …
PASSWORD` è un comando di utilità e non accetta parametri associati: si passa il valore con
`set_config` (una funzione, quindi parametrizzabile) e si cita con `format('%L')`, invece di
concatenare un segreto nel testo SQL.

Il container dell'API **non monta** la password del proprietario: non averla è metà della
difesa.

### 8.20 Nessun accesso anonimo, nessun ripiego di sviluppo

`require_actor` pretende una sessione valida e risponde `401`. Non esiste una variabile
d'ambiente, un header o un parametro che conceda `admin` senza autenticazione.

Il ripiego di sviluppo è pericoloso proprio perché **funziona**: sopravvive ai refactoring,
non fa fallire nessun test, e il giorno in cui una variabile è impostata male diventa un
accesso amministrativo anonimo. Il prototipo aveva già un difetto della stessa forma —
`_doLogin` concedeva `admin` quando l'elenco utenze era vuoto — e va rimosso, non riprodotto
sul server.

Nei test la dipendenza si sostituisce con `app.dependency_overrides`: esplicito, locale al
test, e impossibile da attivare per errore in produzione. Un test prova che header e parametri
plausibili (`X-Debug-Role`, `Authorization: Bearer admin`, `?role=admin&dev=1`) restano `401`.

Sessioni: token da 32 byte di CSPRNG, nel database **solo l'hash** — se il database venisse
letto da chi non deve, le sessioni attive non sarebbero dirottabili. `disabled_at` si
ricontrolla a ogni richiesta, così una disattivazione ha effetto subito e non alla scadenza
del cookie. Il cookie è `HttpOnly`, `SameSite=strict` e `Secure` per default: **senza HTTPS
non si entra**, che è il comportamento voluto. Per provare in locale su HTTP serve
`TSM_COOKIE_SECURE=false` — il default sicuro sta dalla parte giusta.

Password provvisoria: `/api/auth/me` risponde (il client deve poter sapere che serve il
cambio), tutto il resto risponde `403 password_change_required` (§8.1).

### 8.21 Mappa degli errori

| Errore di dominio | HTTP |
|---|---|
| sessione assente o non valida, credenziali errate | `401` |
| politica: evento non consentito al ruolo; password provvisoria | `403` |
| `baseVersion` superato con contenuto diverso; già inizializzato | `409` |
| documento oltre il limite; richiesta oltre il limite | `413` |
| schema del documento, identità, contratto | `422` |
| inventario non inizializzato, guasto non previsto | `503` |

Nessuna risposta contiene traceback, testo di errori SQL o il **contenuto** del documento
rifiutato: il documento può portare l'inventario di un cliente. Dei dettagli esce solo la
parte strutturale — codice, percorso, entità, evento, ambito, ruolo richiesto — e il resto
resta nei log, dove serve a chi opera e non a chi sonda. Un test verifica che le risposte non
contengano `traceback`, `psycopg`, `select `, `sqlalchemy` o percorsi del filesystem.

Credenziali errate e utenza inesistente danno lo **stesso** codice: distinguerli direbbe a chi
prova quali utenze esistono. Si verifica una password anche quando l'utenza non esiste, per
non rendere l'esistenza deducibile dal tempo di risposta.

### 8.22 Contratto HTTP congelato

```
GET  /api/inventory  → { version, schemaVersion, sha256, doc }
PUT  /api/inventory  ← { baseVersion, doc, action? }        action: ≤ 500 caratteri
                     → { version, schemaVersion, sha256, changed }
                       409 → { code: "version_conflict", currentVersion, currentSha256 }
```

`changed=true` per una modifica applicata, `changed=false` per un no-op canonico o per un
replay idempotente (§8.18). `action` è testo di visualizzazione non attendibile: il contratto
lo rifiuta oltre la lunghezza invece di troncarlo in silenzio, e ciò che è cambiato lo dice
`audit.events`, calcolato dal server (§8.9).

`Cache-Control: no-store` su tutte le risposte dell'inventario, comprese quelle di errore.

Un test asserisce l'**intera** superficie dell'API — sette percorsi — così una rotta comparsa
per distrazione fa fallire la suite. Nota di implementazione: in questa versione di FastAPI
`app.routes` contiene wrapper dei router inclusi e non espone `path`; la superficie stabile su
cui asserire è `app.openapi()`, che è anche quella che descrive il contratto.

### 8.23 Readiness: tre condizioni

Da quando le rotte dell'inventario esistono, «pronto» vuol dire tre cose insieme: database
raggiungibile, migrazioni **alla revisione attesa**, e testa dell'inventario presente. Una
istanza che rispondesse `200` con lo schema vecchio o senza inventario manderebbe in errore
ogni richiesta, e sarebbe un guasto molto più difficile da diagnosticare di un `503` sincero.
La risposta dice quale delle tre manca.

La revisione attesa si ricava dai file di migrazione, non da una costante scritta a mano: una
costante si dimentica di aggiornare, e allora la readiness direbbe «pronto» con lo schema
sbagliato — cioè esattamente il caso che deve segnalare.

⚠ **L'healthcheck di Compose usa `/api/health`, non `/api/ready`.** L'healthcheck di Docker
decide riavvii e ordine di avvio: legarlo alla readiness significherebbe che un'installazione
nuova resta `unhealthy` — e il web non parte — fino al bootstrap, che è un passo operativo
separato. La readiness è per il bilanciatore e per lo smoke test.

### 8.24 Limiti di dimensione a due livelli

`client_max_body_size 5m` in nginx e un middleware nell'applicazione che rifiuta su
`Content-Length`. Due livelli perché il primo vale solo per chi passa dal proxy, e il secondo
è proprio lo scenario in cui il primo non aiuta. Il terzo controllo è la validazione del
documento (§8.16), che risponde `413` per un documento oltre soglia anche in `chunked`, dove
`Content-Length` non c'è.

### 8.25 Audit degli eventi di autenticazione

Azioni registrate: `auth.login.success`, `auth.login.failure`, `auth.login.blocked`,
`auth.logout`, `auth.password.changed`, più `users.*` (§8.30).

Non si registra **nessuna credenziale**: né la password, né la sua lunghezza, né un suo hash.
Un hash in un registro consultabile è attaccabile offline, e la lunghezza restringe comunque il
campo. Un test raccoglie tutto l'audit dopo accessi riusciti, falliti e un cambio password, e
verifica che nessuna delle password usate compaia e che non ci sia nessun `$argon2`.

Per un tentativo fallito si registra l'utenza **tentata**: non è una credenziale, ed è
l'informazione che dice a chi legge se qualcuno prova nomi a caso o insiste su una persona
precisa. `audit.actor_role` è diventata nullable (migrazione `0005`) perché un accesso fallito
non ha un ruolo, e inventarne uno in un registro di audit è peggio di lasciare il campo vuoto.

#### ⚠ La registrazione dei fallimenti deve stare fuori dalla transazione

Difetto trovato dai test, e non ovvio. Un accesso fallito fa sollevare l'handler, quindi la
transazione della richiesta viene **annullata**: contatore del limitatore e riga di audit
sparivano insieme all'errore. Il limitatore non avrebbe mai contato nulla e i tentativi falliti
non sarebbero mai finiti nel registro — cioè esattamente i due dati per cui esistono.

Quelle scritture vanno in una transazione propria, che sopravvive al rollback. Un guasto nella
registrazione non deve trasformare un 401 in un 500: si logga e si prosegue. Non riuscire a
scrivere una riga di audit è un problema; lasciare entrare qualcuno perché il registro non era
scrivibile sarebbe peggio.

### 8.26 Password provvisoria: sessione valida ma ristretta

L'accesso con password provvisoria **riesce**: `200` con `authenticated=true` e
`mustChangePassword=true`. Non è un 403, perché la sessione esiste davvero e serve a fare una
cosa. `authenticated` è esplicito e non implicito nello stato HTTP: «200» da solo non direbbe
al client se può procedere o se deve prima cambiare la password.

Con quella sessione sono raggiungibili **tre** endpoint: `/auth/me`, `/auth/password`,
`/auth/logout`. Tutto il resto risponde `403 PASSWORD_CHANGE_REQUIRED`.

La restrizione è **strutturale, non un elenco di percorsi**: gli unici endpoint raggiungibili
sono quelli che non dipendono da `require_actor`. Un endpoint nuovo è ristretto per
costruzione, perché per fare qualcosa gli serve un attore. Un elenco si dimentica di
aggiornare; una dipendenza no.

Il cambio password revoca **tutte** le sessioni — compresa quella che lo sta facendo — pulisce
il cookie e obbliga a un accesso nuovo. Senza pulire il cookie il client crederebbe di essere
ancora autenticato e riceverebbe 401 alla richiesta successiva senza capire perché.

#### Stato mutabile riletto a ogni richiesta

La sessione dice **chi** è l'utente; non dice cosa può fare. Ruolo, disattivazione e obbligo di
cambio password si rileggono dal database a ogni richiesta. Conseguenze verificate dai test,
tutte senza un nuovo accesso: una promozione ha effetto subito, una retrocessione anche, una
disattivazione chiude la sessione, e impostare `must_change_pw` restringe una sessione già
aperta.

### 8.27 Validazione dell'origine, non CORS permissivo

Le richieste che **modificano stato** e **portano il cookie di sessione** devono avere un
`Origin` (o, in mancanza, un `Referer`) fra quelli configurati. Le letture no: pretendere
`Origin` su una GET romperebbe la navigazione senza guadagnare nulla. Le richieste senza cookie
no: senza cookie non c'è autorità da abusare, e pretenderlo romperebbe i client non-browser
senza proteggere niente.

Il cookie è già `SameSite=strict`, quindi un browser non lo invia da un altro sito. Questo è il
secondo livello: copre il caso stesso-sito ma origine diversa, e un eventuale difetto nella
gestione di SameSite.

**Non si abilita CORS con credenziali.** Non esiste un caso d'uso in cui un altro sito debba
chiamare questa API col cookie dell'utente, e abilitarlo smonterebbe da solo tutto il resto. Un
test verifica che non compaia nessun header `Access-Control-Allow-*`.

### 8.28 Limitazione degli accessi e resistenza all'enumerazione

Contatore **durevole** (`login_attempts`), non in memoria: in memoria si azzera a ogni riavvio
— che è precisamente il momento in cui chi insiste ne approfitta — e non sopravvive a più
repliche.

Due finestre, perché rispondono a due attacchi diversi:

| Finestra | Attacco che intercetta |
|---|---|
| per utenza (5 in 15 min) | molte password su UNA persona |
| per IP (20 in 15 min) | una password su MOLTE utenze, che il contatore per utenza non vedrebbe |

Si contano i soli tentativi **falliti**: un accesso riuscito non deve avvicinare al blocco,
altrimenti chi lavora normalmente verrebbe punito. Oltre soglia: `429` con `Retry-After`, e
anche la password giusta viene bloccata — il limitatore protegge l'utenza, non l'utente.

Resistenza all'enumerazione:

- **verifica Argon2 anche per utenze inesistenti**, con un hash finto generato all'avvio con
  gli stessi parametri. Senza, un'utenza inesistente risponderebbe in microsecondi e una
  esistente in decine di millisecondi: differenza misurabile da remoto;
- **un solo errore** per utenza inesistente, password errata e utenza disabilitata — i test
  confrontano le risposte e pretendono che siano identiche;
- **dimensioni limitate** su username e password: Argon2 su un input enorme è lavoro regalato a
  chi lo invia.

`X-Forwarded-For` si crede **solo se il peer è il proxy configurato**. Fidarsi dell'header da
chiunque significa lasciare che il client dichiari il proprio indirizzo, e con esso aggiri il
limitatore per IP cambiando una stringa a ogni tentativo. Un test invia un `X-Forwarded-For`
falsificato da un peer non fidato e verifica che non venga registrato.

### 8.29 TLS e rifiuto di partire in modo insicuro

nginx termina TLS su 8443; 8080 risponde solo `301`. Un servizio raggiungibile in chiaro
sarebbe un servizio in cui non si può entrare, dato che il cookie è `Secure`.

**In produzione un cookie di sessione non `Secure` fa fallire l'avvio.** Non è un avviso nei
log, che nessuno legge fino al giorno dopo: `TSM_ENV=production` con `TSM_COOKIE_SECURE=false`
solleva all'import e il processo non parte. Meglio un servizio che non parte di uno che parte in
modo insicuro, perché il primo si nota subito.

La deroga per lo sviluppo in HTTP sta in `compose.dev.yaml`, che dichiara
`TSM_ENV=development`. Un file separato invece di una variabile: la deroga si attiva
scrivendola sulla riga di comando, e nessuno la eredita da un `.env` dimenticato su un server.
`web/nginx.dev.conf` è **generato** da quello di produzione, con
`tools/sync-nginx-dev.py --check` a fare da guardia, così le due configurazioni differiscono
solo nel TLS e non divergono su allowlist, proxy e limiti.

⚠ Inciampo operativo, uguale a quello dei secret: la chiave privata arriva nel container coi
permessi che ha sull'host, e nginx gira come **uid 101**. Una chiave `0640 root:root` dà
`Permission denied` all'avvio. `tools/make-dev-tls.ps1` genera il certificato di sviluppo con
`chown 101:101`; in produzione va fatto lo stesso col certificato aziendale.

### 8.30 Gestione delle utenze: nessuna cancellazione

```
GET   /api/users?includeDisabled=       elenco
POST  /api/users                        crea con password provvisoria
PATCH /api/users/{id}                   ruolo e profilo
POST  /api/users/{id}/disable           disattivazione logica + revoca sessioni
POST  /api/users/{id}/enable            riattivazione
POST  /api/users/{id}/reset-password    password provvisoria + revoca sessioni
```

Solo `admin`, col ruolo **riletto adesso** (§8.26): una revoca di privilegi ha effetto dalla
richiesta successiva.

**Non esiste `DELETE`**, su nessuna rotta dell'API — un test lo verifica sull'intero schema
OpenAPI. `audit.actor_user_id` punta a `users`, quindi cancellare romperebbe la tracciabilità
(§8.6), e il ruolo di runtime non ha nemmeno il privilegio (§8.19): anche una rotta scritta per
errore in futuro non riuscirebbe a cancellare niente.

Regole difese dentro la transazione, con `FOR UPDATE`:

- **l'ultimo amministratore attivo non si può togliere**, né disattivandolo né retrocedendolo.
  Sono due modi di ottenere lo stesso danno — un sistema che nessuno può amministrare — e il
  secondo è quello che si dimentica;
- **non si disattiva la propria utenza**;
- **username unico anche fra i disabilitati**: riusare il nome di un disattivato è una
  riattivazione esplicita, e il messaggio lo dice invece di restituire un errore di vincolo;
- **disable e reset revocano le sessioni**: senza, chi era collegato continuerebbe a operare, e
  un reset chiesto perché la password è compromessa non servirebbe a nulla.

La password provvisoria torna **una volta sola** nella risposta e non viene registrata da
nessuna parte. `PATCH` usa `exclude_unset`: una modifica del solo ruolo non azzera il profilo.

### 8.31 Porte standard, altrimenti HSTS scavalca il reindirizzamento

In produzione l'host pubblica **80 e 443**. Dentro il container nginx resta su
8080/8443, perché gira come uid 101 e un processo non-root non può aprire porte sotto
1024: il mapping di Docker fa il resto.

Non è estetica. **HSTS si applica all'host e conserva la porta esplicita della
richiesta.** Con il servizio esposto su `http://host:8080`, un browser che ha già visto
HSTS trasforma quella URL in `https://host:8080` — dove nginx parla in chiaro. Il
reindirizzamento non viene mai raggiunto e la richiesta fallisce in modo
incomprensibile: l'utente vede un errore TLS su un indirizzo che «funzionava ieri».

Conseguenze applicate:

- il reindirizzamento usa `$host` e **non** `$http_host`, così non fabbrica una porta
  esplicita;
- HSTS sta **solo** sul listener HTTPS di produzione;
- la configurazione di sviluppo, che usa porte non standard, **non manda HSTS** — e
  l'assenza è documentata come deliberata, altrimenti qualcuno la aggiunge «per
  coerenza» e blocca l'accesso in chiaro su `localhost` per tutti quelli che l'hanno
  visitato una volta;
- `web/nginx.dev.conf` è generato da quello di produzione con marcatori nel file, non
  copiando un blocco di venti righe: la copia era già divergente al primo commento
  modificato. `tools/sync-nginx-dev.py --check` fa da guardia.

### 8.32 Semantica transazionale dell'autenticazione

Cinque proprietà, ognuna verificata iniettando un guasto nel punto dove conterebbe
(`backend/tests/test_transactions_pg.py`):

| Operazione | Regola |
|---|---|
| sessione + audit del login riuscito | **atomici** |
| password + revoca sessioni + audit | **atomici** |
| mutazione utenza + revoca + audit | **atomici** |
| audit del login **fallito** | indipendente, e non trasforma un 401 in 500 |
| persistenza del limitatore | se non scrive, il login **fallisce chiuso** |

Le prime tre stanno nella transazione della richiesta e cadono insieme: una sessione
senza la sua riga di audit è un accesso non tracciato, e una password cambiata senza
revoca lascia aperte le sessioni che il cambio doveva chiudere.

Le ultime due si distinguono, e la distinzione è il punto:

- **L'audit di un tentativo fallito è "best effort".** Deve sopravvivere al rollback
  (§8.25), ma se non si scrive la risposta resta 401. Non riuscire a scrivere una riga
  di registro è un problema; trasformare «credenziali errate» in «errore del server»
  è peggio, perché nasconde al client l'informazione vera.
- **Il contatore del limitatore no: quello deve persistere.** Se i tentativi non si
  possono contare il limitatore non esiste, e non esistere *in silenzio* significa
  tentativi illimitati mentre le risposte continuano a dire «riprova». Si risponde
  `503` (`rate_limiter_unavailable`) e non si entra — nemmeno con la password giusta,
  così l'errore non diventa un oracolo su quali credenziali erano valide.

### 8.33 Frontend: integrazione con l'API

`handoff/api.js` — client con URL **sempre relative** (`/api/...`). L'app è servita
dallo stesso nginx che fa da proxy, quindi non esiste un host da configurare; un URL
assoluto sarebbe un valore da tenere allineato fra ambienti e, con `SameSite=strict`,
farebbe anche cadere il cookie. `credentials: 'same-origin'`, mai `'include'`.

#### Avvio

`GET /api/auth/me` **prima di qualunque dato**. Sul 401 si mostra il login e non si
chiede nulla d'altro; con `mustChangePassword` si mostra solo il cambio password;
l'inventario si carica **solo** dopo un'autenticazione senza restrizioni. Nel prototipo
il login era un cancello disegnato sopra dati già caricati: ora l'ordine è invertito.

Finché `/auth/me` non ha risposto si mostra «Verifica della sessione…»: mostrare subito
il login lo farebbe lampeggiare a chi è già autenticato.

#### Cosa è stato tolto

- **il confronto delle password nel browser** e il ripiego che concedeva `admin` con
  l'elenco utenze vuoto: l'autorità è del server (§8.20);
- **`utenti`** dal documento — stanno in `users`, si gestiscono con `/api/users`;
- **`registro`** — l'audit è del server e lo calcola dal diff (§8.9);
- **`notifiche` e `smtp`**, password compresa — stanno in `settings`, e il server
  rifiuta un documento che li contenga (§8.16).

I pannelli corrispondenti restano ma dicono che la gestione è lato server, invece di
offrire controlli che non salvano niente.

#### Scritture serializzate

`InventoryWriter`: **una sola PUT in volo, una sola in attesa**. `persist()` viene
chiamata anche durante drag e resize, e la versione è una sequenza stretta: due PUT in
parallelo dallo stesso client fanno 409 contro sé stesse. Il documento è completo,
quindi coalescere significa scartare l'intermedio — le posizioni intermedie di un
trascinamento non hanno valore storico. `baseVersion` si aggiorna con quella che
risponde il server, anche su no-op.

**«Salvato» si scrive solo dopo la conferma del server.** Prima di quella non si può
affermare che i dati esistano da qualche parte, e l'indicatore mostra
«salvataggio…» finché la PUT è in volo. `beforeunload` avvisa se si chiude con
scritture in sospeso.

#### Errori distinti

| Stato | Trattamento |
|---|---|
| 401 | sessione scaduta → si torna al login |
| 403 `password_change_required` | si impone il cambio password |
| 403 altro | permesso negato, con il ruolo richiesto se il server lo indica |
| 409 | **si ricarica esplicitamente** l'inventario e si smette di scrivere |
| 413 | documento troppo grande |
| 422 | dati rifiutati, con i codici dei problemi |
| 503 | servizio non pronto, modifiche non salvate |
| rete assente | distinto da una risposta del server |

Sul 409 la coda si **ferma**: continuare a scrivere sovrascriverebbe il lavoro di
un'altra persona. Si ricarica e si avvisa che le modifiche non salvate vanno riapplicate.

#### ⚠ Difetto trovato dal test end-to-end: `X-Forwarded-Proto`

La validazione di origine (§8.27), in mancanza di `TSM_PUBLIC_ORIGIN`, confrontava
`Origin` con `request.url.scheme` + `Host`. Dietro un proxy che termina TLS quello
schema è `http` — è la connessione fra nginx e l'API, non quella del browser — quindi
`Origin: https://host` non combaciava e **ogni richiesta che modifica stato veniva
rifiutata con 403**.

Si manifestava solo dopo il login, perché la richiesta di login non porta ancora il
cookie e il controllo la salta: il sintomo era «entro ma non riesco a cambiare la
password». Chiamando FastAPI direttamente non si vedeva affatto.

Ora si usa `X-Forwarded-Proto` (e `X-Forwarded-Host`) **solo se il peer è il proxy
fidato**, come per l'IP del client: altrimenti sarebbe il chiamante a decidere da quale
origine sembra arrivare.

#### Prova nel browser vero

`tools/browser-e2e-test.py` gira su Chrome attraverso nginx e TLS **sulla porta 443**,
non contro FastAPI. Metà di ciò che va verificato vive fra browser e proxy: il cookie
`Secure` che senza HTTPS non parte, HSTS, il reindirizzamento da HTTP, la validazione
di origine, l'allowlist statica. Un test contro l'API salterebbe tutto questo e direbbe
che funziona — ed è precisamente il difetto qui sopra.

Copre: reindirizzamento e HSTS, avvio via `/auth/me`, login su 401, credenziali errate
con messaggio generico, **cambio password forzato** con ritorno al login, caricamento
dell'inventario solo dopo, attributi del cookie (`Secure`, `HttpOnly`, `SameSite`), un
salvataggio reale con una sola PUT, persistenza verificata ricaricando la pagina,
allowlist statica, logout e inaccessibilità dell'inventario dopo il logout.

### 8.34 Intestazioni `X-Forwarded-*`: non falsificabili

Le `X-Forwarded-*` sono le uniche affermazioni che l'API accetta *da noi* su una
richiesta: schema, host e IP del client. Se il chiamante può scriverle, può scegliere
da dove sembra arrivare — e con esso aggirare la validazione di origine (§8.27) e il
limitatore per IP (§8.28).

Regola: **nginx le sovrascrive sempre.** Mai accodare, mai lasciar passare.

```nginx
proxy_set_header X-Forwarded-For   $remote_addr;   # NON $proxy_add_x_forwarded_for
proxy_set_header X-Forwarded-Host  $host;          # va scritto ANCHE se uguale a Host
proxy_set_header X-Forwarded-Proto $scheme;
```

#### Due difetti reali, trovati scrivendo questo test

**1. `$proxy_add_x_forwarded_for` accoda.** Era la configurazione in uso. Quella
variabile aggiunge l'IP reale *in coda* a un eventuale header del client, quindi un
`X-Forwarded-For: 203.0.113.77` inviato dall'esterno arrivava all'API come
`203.0.113.77, 172.20.0.1`. L'API leggeva la **prima** voce — quella scelta
dall'attaccante — e la registrava come IP del client: limitatore per IP aggirabile
cambiando una stringa a ogni tentativo.

Corretto su due lati indipendenti: nginx sovrascrive, e l'API legge l'**ultima** voce
della catena invece della prima. Fra due letture equivalenti quando c'è un solo proxy,
si sceglie quella che regge se un giorno qualcuno accodasse.

**2. `X-Forwarded-Host` non era impostato affatto**, quindi quello del client passava
intatto — e l'API, che si fida degli header quando il peer è il proxy, costruiva
l'origine attesa con un valore scelto dall'attaccante. Bastava mandare
`X-Forwarded-Host: malintenzionato.example` insieme a `Origin: https://malintenzionato.example`
perché i due combaciassero e la validazione di origine passasse.

**3. La porta dell'API pubblicata su `127.0.0.1:8000` era una scorciatoia che
scavalcava il proxy.** «Solo loopback» sembrava innocuo e non lo era: le richieste
dall'host al container attraversano il bridge di Docker e arrivano con sorgente
`172.20.0.1`, cioè **dentro** `TSM_TRUSTED_PROXIES`. L'API le considerava provenienti
dal proxy e credeva alle loro `X-Forwarded-*`: chiunque sull'host potiva dichiarare il
proprio IP. In produzione la porta non è più pubblicata; l'API si raggiunge da nginx e,
per la diagnostica, con `docker compose exec`. `compose.dev.yaml` la ripubblica in
sviluppo, con il motivo scritto accanto.

#### Il test: comportamento **e** configurazione

`tools/proxy-security-test.py`, attraverso nginx vero sulla 443. Chiamare FastAPI
direttamente non serve: da lì il peer non è fidato, gli header vengono ignorati, e il
test passerebbe anche con nginx configurato male — che è esattamente come il difetto è
sopravvissuto fino a qui.

Il test verifica entrambi i livelli, e la ragione è concreta: le due difese sono
indipendenti e **ognuna da sola nasconde la regressione dell'altra**. Rimettendo
`$proxy_add_x_forwarded_for`, l'API che legge l'ultima voce continua a comportarsi
correttamente: il comportamento è a posto mentre la configurazione è tornata
vulnerabile, e basterebbe un cambio sul lato API perché si aprissero insieme. Quindi:

- **configurazione** — si leggono le sole direttive `proxy_set_header` (i commenti
  nominano `$proxy_add_x_forwarded_for` per spiegare perché non si usa, e un controllo
  sul testo grezzo confonderebbe la spiegazione col difetto), su `nginx.conf` **e**
  `nginx.dev.conf`;
- **comportamento** — l'IP falsificato non compare in `login_attempts` né in `audit`;
  un `Origin` estraneo con `X-Forwarded-Host` falsificato riceve 403; un
  `X-Forwarded-Proto: http` falsificato non abilita un `Origin` http; e non esiste una
  porta dell'API pubblicata da cui scavalcare tutto.

Verificato che il test fallisca su ciascuna delle due regressioni **separatamente**: un
test di regressione che non fallisce quando si reintroduce il difetto non sta
proteggendo niente.

### 8.35 Interfaccia di amministrazione delle utenze

Frontend puro sopra le rotte già esistenti di §8.30. Nessuna utenza nel documento
dell'inventario: si legge e si scrive solo da `/api/users`.

#### Identità: sempre l'UUID

Elenco, modifica, disattivazione, riattivazione e reimpostazione usano l'`id` UUID
della riga. Mai l'username, mai la posizione nell'elenco: il primo è rinominabile e la
seconda cambia a ogni ricarica, quindi bastano due richieste che si incrociano per agire
sull'utenza sbagliata.

#### Il server è l'autorità, non una copia delle sue regole

- Le politiche (ultimo amministratore attivo, non disattivare sé stessi, username già
  in uso) **non sono riprodotte in JavaScript**: si invia e si mostra il rifiuto. Una
  seconda copia delle regole nel client si sarebbe scostata dalla prima, e la copia
  sbagliata sarebbe stata quella che l'utente vede.
- **Nessuna rimozione o retrocessione ottimistica**: la riga cambia solo perché
  l'elenco è stato **ricaricato dal server** dopo la conferma. L'oggetto inviato non è
  quello canonico — il server normalizza, applica regole e può rifiutare in parte.
- Dopo una modifica che riguarda l'amministratore stesso si richiede
  **`/api/auth/me`** e si adegua l'interfaccia a quello che il server dichiara
  *adesso*. Se il ruolo non basta più, il pannello si chiude **con un messaggio**: un
  pannello che scompare senza spiegazione sembra un guasto.

#### Nessuna cancellazione, da nessuna parte

Nessun pulsante di eliminazione, nessuna chiamata `DELETE`. Le utenze disattivate
**restano visibili** con lo stato «disattivata» e l'azione di riattivazione: nasconderle
le farebbe sembrare cancellate, che è esattamente l'impressione da evitare quando
l'audit continua a citarle.

#### Password provvisorie

Mostrate **una volta**, in un riquadro dedicato, con un pulsante di copia esplicito.
Vivono solo in stato React e solo finché il riquadro è aperto: mai nel documento
dell'inventario, mai in `localStorage` o `sessionStorage`, mai nella URL, mai nei log o
in console, mai nei dettagli dell'audit. Alla chiusura il valore viene **cancellato**
dallo stato, e il test verifica che scompaia anche dall'HTML.

Se il browser nega la clipboard si dice di copiarla a mano, invece di confermare una
copia che non è avvenuta.

#### Doppio invio

Ogni azione si disabilita mentre la sua richiesta è in volo, e la chiave è **per
pulsante** (`reset:<uuid>`), non un booleano globale: una riga che lavora non deve
bloccare l'intero pannello.

Il test riproduce un doppio clic **vero**, cioè due eventi nello stesso task del
browser. Con due `dispatch_event` separati non provava nulla: la creazione riuscita
chiude il form, quindi il secondo clic non trovava più il pulsante e il test passava per
il motivo sbagliato.

#### Errori distinti

Tutti i casi richiesti passano da `describeError`: 401 → login, 403
`password_change_required` → cambio forzato, 403 → autorità insufficiente con il ruolo
richiesto, 409, 413, 422, 429, 503.

⚠ Difetto trovato dal test: **409 non è un caso solo.** Lo stesso stato copre il
conflitto di versione dell'inventario, l'utenza già esistente e la protezione
dell'ultimo amministratore, e il mapping li trattava tutti come il primo. Chi provava a
creare un'utenza con un nome già in uso leggeva «un'altra persona ha salvato prima di
te» — un messaggio non solo inutile ma falso. Ora si distingue per `code`.

#### Testo, non markup

I campi di profilo e i messaggi del server passano dall'interpolazione `{{ }}` del
runtime, che li inserisce come **testo**. Nessun `innerHTML`, nessun rendering di
markup che arriva dal server o da un altro utente.

#### Prova nel browser

`tools/users-ui-test.py` (51 controlli) attraverso nginx e TLS. Copre creazione,
modifica di ruolo e profilo, disattivazione, riattivazione, reimpostazione, utenza
duplicata, doppio clic su creazione e reimpostazione, ciclo di vita della password
provvisoria, protezione dell'ultimo amministratore, autoretrocessione con rilettura
dell'autorità, e il rifiuto delle rotte a un non-amministratore — compresi header
(`X-Role`, `Authorization`) e parametri di query falsificati, che restano inefficaci
perché il ruolo lo rilegge il server dalla sessione.

Il test **non è ripetibile** su uno stato già usato, perché finisce retrocedendo
l'amministratore: c'è una precondizione che lo dice con chiarezza e
`tools/run-users-ui-test.ps1` ricrea lo stato prima di partire. E il rapporto degli
esiti si stampa **sempre**, anche se il test si interrompe a metà: senza,
un'eccezione nascondeva tutto ciò che era già stato verificato e si finiva a
indovinare.

### 8.36 Registro di audit: API e vista

`GET /api/audit`, solo amministratori, sola lettura. Il registro non passa mai dal
documento dell'inventario e il client non ne ricostruisce voci: quello che si vede è
ciò che il server ha registrato, o niente.

#### Ordinamento e cursore: due campi, non uno

`ORDER BY ts DESC, id DESC`, e il cursore porta **entrambi**. Il timestamp da solo non
basta: più eventi condividono lo stesso istante — un salvataggio ne produce diversi
nella stessa transazione — e un cursore sul solo `ts` salterebbe o ripeterebbe le righe
a cavallo fra due pagine. Il predicato è un confronto fra **tuple**,
`(a.ts, a.id) < (:cur_ts, :cur_id)`, che corrisponde all'ordinamento per costruzione
invece che per coincidenza.

Il cursore è **opaco** al confine HTTP: base64url di `v1|<iso-utc>|<id>`. La versione è
dentro il valore per poter cambiare formato senza che un client con un cursore vecchio
riceva risultati sbagliati in silenzio. La decodifica è severa e ogni anomalia dà
`422 invalid_cursor`: un cursore manomesso deve produrre un errore riconoscibile, non
una pagina «quasi giusta» che salta righe senza dirlo. `?cursor=` vuoto vale come
«prima pagina», che è ciò che intende un client che invia il parametro vuoto.

Si chiede sempre **una riga in più** della pagina: la sua esistenza dice che c'è un
seguito, senza un `COUNT` che su una tabella che cresce costerebbe quanto la query.

#### Filtri tipizzati, non un linguaggio

`from`, `to`, `username`, `event`, `result`. Niente espressioni libere, niente JSON
arbitrario: la superficie è chiusa e ciò che non è previsto viene rifiutato con
`422 invalid_filter`. `event` accetta la categoria o l'azione completa — `auth` prende
tutta la famiglia `auth.*` — e ammette solo i caratteri che le azioni usano davvero.

Dimensione pagina: 50 di default, 200 massimo, valori non validi o negativi rifiutati
con `invalid_page_size`.

⚠ I parametri arrivano alla rotta come **stringhe** e li valida il nostro parser. Se
fossero dichiarati tipizzati sarebbe FastAPI a rifiutarli per primo, con una forma di
errore diversa dalla nostra: il client riceverebbe due formati a seconda di quale
controllo scatta per primo. Così il codice è sempre uno dei nostri, ed è stabile.

#### Istantanea storica dell'attore

`actor_username` e `actor_role` sono copiati nella riga al momento dell'evento (§8.30):
un utente rinominato, retrocesso o disattivato resta attribuibile con il nome e il ruolo
che **aveva allora**. Tre test lo verificano rinominando, disattivando e retrocedendo
dopo il fatto.

#### Riservatezza: due ripuliture, non una

`sanitize()` gira **in scrittura** e di nuovo **in serializzazione**. Non è ridondanza:
il registro è alimentato da produttori diversi e ne arriveranno altri. La prima evita
che una chiave di troppo finisca su disco; la seconda evita che esca comunque se è già
finita su disco o se un produttore ha aggirato la prima. Una sola delle due lascia
scoperta metà del problema.

Si guarda il **nome** della chiave (`password`, `secret`, `token`, `hash`, `credential`,
…) e solo forme di valore inequivocabili (hash Argon2/bcrypt, DSN con credenziali):
indovinare cosa «sembra» un segreto produce falsi negativi sui segreti nuovi e falsi
positivi su tutto il resto. Un test inserisce un dettaglio velenoso **grezzo**,
scavalcando la ripulitura in scrittura, e verifica che la risposta non lo contenga —
e che il campo innocuo accanto sia ancora lì, perché una ripulitura che cancella tutto
non è una difesa ma un guasto.

`clientHint` si restituisce com'è, in un campo suo: è testo del client, **non** la
descrizione autorevole dell'evento, che è `event` più `detail` calcolati dal server.

#### Indice e privilegi

Un solo indice composito `(ts DESC, id DESC)`, che è esattamente l'ordinamento e il
predicato della paginazione. Indici sui filtri si aggiungeranno quando i numeri lo
giustificheranno, non prima.

Il ruolo di runtime ha `SELECT` e `INSERT`, e la migrazione **revoca esplicitamente**
`UPDATE`, `DELETE` e `TRUNCATE`. Un test si connette *davvero* come `tsm_api` e verifica
che i tre comandi ricevano `permission denied`, e che l'inserimento continui a
funzionare — la sola lettura non basterebbe, altrimenti nessun evento verrebbe più
registrato.

#### La vista

Pannello di sola lettura, visibile solo agli amministratori, con il 403 del server come
autorità finale. Ha sostituito il vecchio elenco «di sessione», che era ricostruito lato
client, spariva al ricaricamento e non provava niente.

- **«Carica altri»** invece del caricamento integrale. L'elenco corrente resta visibile
  mentre arriva la pagina successiva, e il pulsante è disabilitato durante la richiesta:
  due clic accoderebbero due pagine con lo **stesso** cursore, e la seconda
  duplicherebbe le righe della prima.
- **Il cursore si azzera a ogni cambio di filtro**: proseguire da un cursore ottenuto
  con altri filtri darebbe una pagina di un elenco che non esiste più.
- **Orari nel fuso locale**, con l'API in UTC. È l'unico modo perché due amministratori
  in fusi diversi parlino dello stesso evento; la conversione avviene solo per la
  lettura.
- **Tutto come testo**, tramite l'interpolazione del runtime. Il dettaglio strutturato
  non si riversa nella tabella — una riga con dentro un JSON di mille caratteri rende
  l'elenco illeggibile — ma si apre in un riquadro su richiesta.
- Stati distinti per caricamento, elenco vuoto, filtro non valido, 401, 403, 422, 503.

#### Test

`backend/tests/test_audit_api_pg.py` (50, PostgreSQL reale) e
`tools/audit-ui-test.py` (24, browser via nginx/TLS).

Due cose imparate scrivendoli, entrambe finite nel codice dei test:

- **Il limitatore ha morso il test.** La prima versione generava 60 accessi falliti per
  avere più di una pagina: superata la soglia per IP, veniva bloccato anche l'accesso
  dell'amministratore e il test non entrava più. Ora il volume si genera con accessi
  **riusciti**, che per scelta non contano ai fini del blocco (§8.28), e i pochi
  fallimenti servono al filtro per esito. `tools/run-audit-ui-test.ps1` riparte pulito,
  perché la finestra del limitatore dura 15 minuti e sopravvive alle esecuzioni.
- **`is_visible()` non vuol dire raggiungibile.** Il controllo «sono entrato» guardava un
  pulsante dell'applicazione, che dietro il pannello di login resta «visibile» per
  Playwright pur essendo coperto: passava anche quando l'accesso era stato rifiutato con
  429. Ora si verifica la sessione con `/api/auth/me`.

### 8.37 Test distruttivi: consenso esplicito

`tools/destructive_guard.py`. I test end-to-end che cambiano davvero i dati — utenze,
password, inventario — richiedono `--allow-destructive` a ogni esecuzione, e rifiutano
un obiettivo non locale salvo `--force-remote`.

Non è un ostacolo per chi sa cosa sta facendo: sono due parole sulla riga di comando.
Serve a rendere impossibile lanciarli *per sbaglio* contro qualcosa che somiglia alla
produzione, che finora era impedito solo dalla memoria di chi digitava.

### 8.38 Impostazioni tipizzate e invio di prova

`GET`/`PUT /api/settings` e `POST /api/notifications/test`, solo amministratori. Lo
**scheduler non è in questo commit**: prima la configurazione e la consegna devono
essere stabili, poi si automatizza qualcosa che manda posta da sé.

#### Schema chiuso, non una tabella chiave/valore

Un editor di coppie chiave/valore è comodo per chi lo scrive e indifendibile per chi
lo mantiene: nulla impedisce che un giorno dentro ci finisca `smtp.password`, e da
quel momento la password è in un campo che l'API restituisce a chiunque possa
leggere le impostazioni. I campi ammessi sono quindi un elenco finito —
`enabled`, `timezone`, `warningDays`, `recipients`, `schedule` — e tutto il resto
viene rifiutato.

Le chiavi che *somigliano* a un segreto (`password`, `secret`, `token`, `hash`, …)
sono rifiutate a **qualunque profondità**, con un codice proprio, usando lo stesso
vocabolario della ripulitura dell'audit (§8.36): una sola definizione di «somiglia a
un segreto» per tutta l'applicazione. Tecnicamente è ridondante — un campo
sconosciuto viene già rifiutato — ma la ridondanza è il punto: il giorno in cui lo
schema crescerà di un sotto-oggetto, quel sotto-oggetto nascerà già protetto invece
di dipendere dall'attenzione di chi lo aggiunge.

I messaggi d'errore nominano il **campo**, mai il valore: un valore rifiutato può
essere proprio il segreto che si sta cercando di tenere fuori.

#### `PUT` sostituisce, non modifica parzialmente

Il blocco `notifications` va inviato **completo**. Con i campi facoltativi «assente»
e «falso» diventano indistinguibili, e un client che dimentica `enabled` spegnerebbe
le notifiche senza volerlo. Con tutti i campi obbligatori il caso non esiste, e
`enabled: false` è un valore esplicito che attraversa la canonicalizzazione intatto —
verificato da un test, perché è precisamente l'errore che un `bool(...)` o un `or`
commette in silenzio e nella direzione peggiore.

`version`, `smtp` e `updatedAt` sono di sola lettura e vengono rifiutati con un
codice **dedicato**: il client non ha inventato un campo, ne ha rimandato indietro
uno che l'API produce. La concorrenza si gestisce con l'intestazione, non con un
campo nel corpo che si può dimenticare.

#### Canonicalizzazione: cosa si ordina e cosa no

`warningDays` viene deduplicato e **ordinato**: `[30, 7]` e `[7, 30]` sono la stessa
configurazione — un insieme di finestre — e senza un ordine deterministico due
salvataggi equivalenti sembrerebbero diversi e farebbero salire la revisione a vuoto.

`recipients` **non** viene ordinato: l'ordine è quello scritto
dall'amministratore, ed è informazione sua. Si normalizzano gli spazi e il dominio in
minuscolo; la parte locale resta com'è, perché per l'RFC 5321 è sensibile alle
maiuscole e cambiarla significa consegnare a un indirizzo diverso da quello scritto.
I duplicati si cercano invece confrontando tutto in minuscolo: nessun server reale
tratta `Mario@x.it` e `mario@x.it` come due persone, e accettarli entrambi manderebbe
due copie di ogni avviso.

Il fuso orario si valida costruendo davvero uno `ZoneInfo` — un elenco scritto a mano
invecchierebbe, e `ZoneInfo` è la stessa fonte che userà lo scheduler. `tzdata` è in
`requirements.txt` e non è facoltativo: senza, l'unica fonte è
`/usr/share/zoneinfo` del sistema, che nelle immagini `slim` può mancare, e il
guasto si manifesterebbe in produzione come «Europe/Rome non è un fuso valido».

#### Revisione monotona, `ETag`/`If-Match`

Riga **unica** in `settings`, con la singolarità imposta da un `CHECK (id = 1)`: è un
vincolo del database, non una convenzione del codice. Il ruolo di runtime ha `SELECT`
e `UPDATE` e **non** `INSERT`, come per `inventory_head` (§8.19): la riga nasce nella
migrazione, una volta sola.

Il blocco `FOR UPDATE` si prende **prima** di confrontare la revisione. Leggere,
confrontare e poi scrivere senza blocco lascerebbe una finestra in cui due richieste
con la stessa revisione attesa passano entrambe il controllo, e la seconda
sovrascriverebbe la prima con la benedizione del meccanismo che dovrebbe impedirlo.

Un **no-op canonico non incrementa** la revisione. Se la incrementasse, aprire la
schermata e premere Salva senza toccare niente farebbe fallire il salvataggio di un
collega che aveva la pagina aperta: un conflitto inventato, che insegna a ignorare
quelli veri. Un no-op non scrive nemmeno una riga di audit — non è accaduto niente.

⚠ **`W/"4"` va accettato, e l'ha scoperto il browser.** La prima versione ammetteva
solo l'ETag forte, per il motivo giusto sulla carta: la RFC 9110 impone il confronto
forte per `If-Match`. In pratica non funzionava. Il modulo gzip di nginx
**indebolisce** l'ETag quando comprime: il server manda `"4"`, il browser — che
dichiara `Accept-Encoding: gzip` — riceve `W/"4"`, e può solo rimandare quello che ha
ricevuto. Ogni salvataggio dall'interfaccia riceveva 422 mentre le stesse chiamate da
uno script, che non chiede la compressione, funzionavano: un difetto invisibile a
qualunque test sull'API. Accettare la forma debole è anche corretto nel merito — la
distinzione forte/debole riguarda l'identità byte per byte della rappresentazione,
mentre qui il validatore è un numero di revisione, che la compressione non cambia.
`*` resta rifiutato: quello significa davvero «qualunque versione va bene», cioè
l'ultimo-che-scrive-vince con un'intestazione davanti.

#### La modifica e la sua traccia stanno o cadono insieme

`UPDATE` e riga di audit nella **stessa** transazione, quella della richiesta. Una
configurazione cambiata senza sapere da chi è precisamente ciò che l'audit esiste per
impedire; un test fa fallire la scrittura dell'audit e verifica che la modifica non
resti.

Nel dettaglio finiscono i **nomi** dei campi cambiati, non i valori: i destinatari
sono indirizzi di persone e non c'è ragione di duplicarli in ogni riga di registro.

#### SMTP: dell'operations, non dell'interfaccia

Host, porta, modalità TLS, mittente e utenza sono variabili d'ambiente; la password è
un secret montato, come quella del database (§8.7). L'API espone **una** cosa:
`smtp: {"configured": true|false}`.

Non è avarizia. Un oggetto `smtp` che accetta `host` e `username` è l'oggetto in cui,
un giorno, qualcuno aggiunge `password`: se non esiste un posto dove metterla, non ci
finisce. Non escono mai la password, il suo hash, il percorso del secret, la password
del database né una stringa di connessione, e un test lo verifica sull'intera
risposta.

#### L'invio di prova non è un relay

`POST /api/notifications/test` non accetta **niente**: né `to`, né `subject`, né un
corpo. Destinatari e testo vengono dalle impostazioni **salvate** e dal codice del
server. Provare una configurazione non ancora committata direbbe che funziona
qualcosa che non è quello che poi verrà usato.

La prova usa al massimo **tre** destinatari fra quelli configurati: se il relay
funziona per tre funziona per tutti, e la differenza è solo quanta posta si genera
premendo un pulsante.

Limitazione **separata** da quella degli accessi (§8.28), perché protegge da un danno
diverso: non chi indovina password, ma una sessione di amministratore compromessa —
o un pulsante premuto in un ciclo — usata per generare posta. Tre invii per utenza e
dieci complessivi all'ora; il limite complessivo non è ridondante, perché con il solo
limite per attore N amministratori moltiplicherebbero il tetto. La prenotazione conta
e scrive nella **stessa** transazione sotto `pg_advisory_xact_lock`: sotto READ
COMMITTED due richieste simultanee conterebbero entrambe «due invii finora» e ne
partirebbero tre. La riga si scrive **prima** dell'invio, così un invio che va in
timeout viene contato — che è proprio il caso in cui qualcuno riprova.

Gli errori escono come **categoria** da un elenco chiuso (`timeout`,
`connection_failed`, `auth_failed`, `recipients_refused`, `sender_refused`,
`tls_failed`, `protocol_error`). Il testo di un'eccezione di `smtplib` contiene
l'host del relay, a volte l'utenza e la risposta completa del server: resta nei log.

⚠ **`smtplib.SMTPException` deriva da `OSError`**, e così `ssl.SSLError` e
`TimeoutError`. Una clausola `except OSError` messa troppo in alto intercetta quindi
anche gli errori di protocollo e li etichetta «connessione non riuscita»: la risposta
manderebbe chi legge a controllare la rete mentre il relay ha risposto benissimo,
dicendo no. L'ordine delle clausole va dal più specifico al più generico, e un test
fissa la gerarchia perché è una proprietà della libreria, non del nostro codice.

#### L'asimmetria dell'audit dell'invio

È la parte che merita più attenzione, perché riguarda un'azione che il database non
può annullare.

- Invio **riuscito**, audit non riuscito → si risponde **successo**, con
  `auditRecorded: false`, e il guasto va nei log a livello di errore. Rispondere
  «non riuscito» farebbe riprovare il client, e ogni tentativo manderebbe un altro
  messaggio vero a persone vere.
- Invio **fallito**, audit non riuscito → la risposta resta il fallimento. Qui non
  c'è niente di irreversibile da proteggere.

Entrambi i versi hanno un test. Un invio di prova, riuscito o fallito, non cambia mai
le impostazioni.

#### La schermata

Nel pannello ⚙ Impostazioni, solo per gli amministratori, con il 403 del server come
autorità finale. Ha sostituito i campi SMTP del prototipo, che raccoglievano host,
utenza e **password in chiaro** dentro il documento dell'inventario.

- **Nessun campo password**, e un test cerca `input[type=password]` per esserne sicuro.
- L'ETag si conserva e si rimanda; si ricarica a **ogni** apertura del pannello,
  perché fra due aperture qualcun altro può aver salvato.
- «Salvato» si dice **solo** con la risposta in mano, e dopo si rilegge il documento
  canonico dal server: la normalizzazione è sua, e la schermata deve mostrare quello
  che è stato scritto, non quello che si è digitato.
- Su **409** si ricarica la versione del server e si dice cos'è accaduto. Non si
  rimandano gli stessi dati: cancellerebbero il lavoro di un altro amministratore,
  che è esattamente ciò che l'ETag serve a impedire.
- Validazione lato client per comodità — dire «manca la chiocciola» senza un giro di
  rete — ma **l'autorità è del server**: un test manda un fuso che il client non sa
  giudicare e verifica che arrivi al server e torni rifiutato.
- Ogni pulsante è disabilitato mentre la **sua** richiesta è in volo: due Salva
  manderebbero due `PUT` con lo stesso `If-Match` (e la seconda riceverebbe un
  conflitto contro la prima), due prove manderebbero due messaggi veri.
- **Nessun invio di prova automatico** dopo un salvataggio, ed esiti di salvataggio e
  di prova in **aree distinte**: sono due operazioni diverse, e un'area sola farebbe
  credere che l'esito di una dica qualcosa dell'altra.

#### Test

`backend/tests/test_settings_schema.py` (pura), `test_settings_api_pg.py` e
`test_notifications_test_pg.py` (PostgreSQL reale), `tools/settings-ui-test.py`
(43 controlli nel browser via nginx/TLS) e `tools/run-settings-ui-test.ps1`, che
riparte pulito perché la revisione è cumulativa e il limite degli invii è orario.

Tre difetti trovati scrivendo questi test, tutti finiti nel codice:

- **L'ETag debole di nginx** (sopra). Solo il browser poteva trovarlo.
- **Doppia serializzazione nella migrazione.** Passando una stringa già serializzata
  a un parametro tipizzato JSONB, nella colonna finiva una *stringa* JSON invece di un
  oggetto. Da Python non si notava — `json.loads` la apriva — ma
  `data -> 'notifications'` restituiva NULL, e l'avrebbe scoperto lo scheduler mesi
  dopo come «non trova destinatari». Il `load()` non lo accomoda più: se la colonna
  non contiene un oggetto, l'API **fallisce** invece di arrangiarsi, perché
  arrangiarsi sposta il guasto altrove e più tardi.
- **Un test che verificava sé stesso.** La prima versione di
  «la migrazione inserisce il documento giusto» girava dopo una fixture che
  *sovrascriveva* quella riga: passava senza aver mai guardato ciò che la migrazione
  scrive. Ora c'è una fixture che fa `downgrade` e `upgrade` veri — e come effetto
  collaterale è l'unica cosa che prova che `downgrade()` funziona.

### 8.39 Il client di prova parla HTTPS

Il `TestClient` di Starlette usa `http://testserver` per default, e il cookie di
sessione è `Secure`: `http.cookiejar` non invia un cookie `Secure` su `http://`. La
conseguenza non era solo qualche 401 al posto di un 403.

La validazione di origine (§8.27) scatta **solo** sulle richieste che portano il
cookie di sessione. Senza cookie non veniva mai esercitata: i test che la riguardavano
passavano perché il controllo non avveniva. Un test che passa senza eseguire ciò che
dichiara di verificare è peggio di un test rosso.

`backend/tests/conftest.py` fornisce ora `api_client()` e `ORIGIN`, entrambi su
`https://testserver`. L'alternativa — far girare la suite con
`TSM_COOKIE_SECURE=false` — è stata scartata: renderebbe verdi i test cambiando la
configurazione dell'applicazione, e la configurazione di produzione non verrebbe più
provata da nessuno.

Nello stesso spirito, `starlette` e `httpx` sono ora **pinnati**. Non lo erano perché
sono dipendenze transitive, e nel frattempo `starlette` è passata alla 1.x, che
pretende `httpx2`: la suite non si raccoglieva più. Una dipendenza non pinnata
trasforma il tempo che passa in un cambio di comportamento (§6). I runner dei test nel
browser hanno preso `--build`: senza, provavano l'immagine costruita l'ultima volta,
cioè codice vecchio.

### 8.40 Archiviazione di produzione e avvio del servizio

Due dischi su `tsm-prd-01`: il sistema con Docker, le immagini, i log e lo swap sul
primo; i dati durevoli di PostgreSQL sul secondo, montato su `/srv/tsm-data`, con
i dati in `/srv/tsm-data/postgres`. `/var/lib/docker` **resta sul disco 1**: le
immagini si ricaricano, i dati no.

Runbook operativo completo: `deploy/README.md`.

#### Il volume resta un volume, ma i byte vanno sul disco 2

```yaml
volumes:
  pgdata:
    driver: local
    driver_opts: { type: none, o: bind, device: /srv/tsm-data/postgres }
```

Il percorso è **letterale**. Un `${TSM_DATA_DIR:-/srv/tsm-data/postgres}`
sembrerebbe più flessibile e sarebbe un pericolo: una variabile dimenticata in un
`.env` o nell'ambiente di una shell sposterebbe il database altrove in silenzio,
e il momento in cui si scopre è quello in cui «i dati non ci sono più». Le
macchine senza il secondo disco aggiungono `-f compose.storage-dev.yaml`, che è un
atto visibile sulla riga di comando.

`postgres` è una **sottodirectory** del punto di mount, non il punto di mount: un
filesystem appena creato non è vuoto, e `initdb` rifiuta una directory non vuota.

#### Il guasto che tutto questo esiste per impedire

Con il bind, se il secondo disco non è montato PostgreSQL può inizializzare sul
**filesystem di root**. Nessun errore, servizio apparentemente sano, dati sul
disco che si riempie e che il backup del volume dati non copre.

Misurando, il pericolo si è rivelato più preciso di come lo si immaginava:

| Situazione | Cosa fa Docker |
|---|---|
| `/srv/tsm-data/postgres` **assente** | il bind **fallisce**, il container non parte |
| `/srv/tsm-data/postgres` **presente**, mount assente | il bind riesce, `initdb` scrive **su `/`** |

La prima riga è una difesa gradita e non pianificata; la seconda è il caso
frequente — la directory l'ha creata l'amministratore seguendo il runbook, e poi
il disco non è stato montato per un riavvio, una modifica a `fstab` o un device
che ha cambiato nome. Il test la riproduce e la dimostra prima di dimostrare il
rimedio: un test che verifica solo il rifiuto non dice se il pericolo era reale.

#### Preflight: fallisce chiuso, e non aggiusta niente

`deploy/preflight.sh` gira **prima** di Compose e rifiuta l'avvio se qualcosa non
torna. In particolare **non crea la directory dei dati quando il mount manca**: un
secondo disco assente è un guasto, non una condizione da aggirare. Non formatta,
non partiziona, non tocca `fstab`, non scarica immagini.

Ogni prerequisito ha un **codice di uscita stabile**, così un'automazione
distingue «manca il disco» da «manca il certificato» senza leggere il testo:
`10`–`12` Docker, `20`–`28` archiviazione, `30`–`31` secret e TLS, `40`–`42`
configurazione non di produzione. Elenco completo nell'intestazione dello script e
in `deploy/README.md`.

Tre controlli meritano una nota.

- **Non è il filesystem di root**: si confrontano sia la `SOURCE` di `findmnt` sia
  `st_dev`. Due segnali indipendenti perché `findmnt` da solo non distinguerebbe un
  bind del filesystem di root montato su `/srv/tsm-data`, che «è un mountpoint» e
  passerebbe.
- **Scrivibilità**: non si guardano i permessi, si **scrive davvero** come utente
  di PostgreSQL attraverso Docker. Sotto SELinux enforcing l'etichetta sbagliata
  dà `EACCES` con permessi perfetti, e un controllo sui soli permessi lo
  mancherebbe.
- **Identità di PostgreSQL letta dall'immagine**, mai assunta. Nell'immagine
  `postgres:17-alpine` l'uid è **70**, non il 999 delle immagini Debian che quasi
  tutte le guide danno per scontato: scriverlo a mano avrebbe prodotto una
  directory di proprietà di un utente inesistente, e un errore che parla di
  permessi invece di uid.

⚠ **Il preflight legge la configurazione con `awk`, non con un interprete.** La
prima versione usava `python3` con PyYAML e ricadeva su `grep` quando mancava.
Sulla VM di prova `python3` *esiste ma è rotto*: `command -v` diceva sì,
l'estrazione restituiva stringa vuota, e «nessuna porta pubblicata» risultava vero
perché non si era letto niente. Un preflight che si distrae proprio sul controllo
che deve fare è peggio di un preflight assente. `docker compose config` produce
YAML normalizzato, quindi un estrattore basato sull'indentazione è deterministico
e non dipende da nulla.

#### SELinux: l'etichetta deve essere persistente

Tipo atteso `container_file_t`. Il preflight verifica **anche** che esista la
regola in `semanage fcontext`, non solo l'etichetta corrente: un `chcon` funziona
subito e si perde alla prima rietichettatura del filesystem — un `restorecon -R /`,
un aggiornamento della policy — e da quel momento PostgreSQL non scrive più, in un
giorno in cui nessuno ha toccato TSM.

#### Unità systemd

`RequiresMountsFor=/srv/tsm-data` fa dedurre a systemd l'unità `.mount`
corrispondente e la rende una dipendenza necessaria: senza il disco, il servizio
non parte. Senza, all'avvio della macchina si vincerebbe una corsa — Docker pronto
prima del mount. Non basta comunque da solo, perché un mount può esserci e puntare
alla cosa sbagliata: per quello c'è `ExecStartPre`.

`Restart=no` di proposito: se il preflight rifiuta, il motivo è strutturale e
riprovare ogni trenta secondi riempirebbe i log nascondendo la diagnosi. Nessun
secret nell'unità né sulla riga di comando — finirebbero in `systemctl cat` e nel
journal — e nessun `EnvironmentFile`, che sposterebbe gli stessi valori in un
altro file di testo con un livello di indirezione in più.

#### Se il disco sparisce a servizio avviato

`RequiresMountsFor` protegge l'**avvio**, e nessuna configurazione di systemd rende
sicura la rimozione a caldo di un filesystem sotto un database in esecuzione:
descrittori aperti, pagine sporche, WAL da scrivere. L'applicazione non tenta
alcun recupero automatico, e in particolare non reinizializza niente — un `initdb`
automatico su una directory vuota trasformerebbe un guasto di archiviazione in una
perdita di dati.

Il comportamento atteso è: allarme **immediato** del monitoraggio se
`/srv/tsm-data` non è più un mount o è passato in sola lettura; `/api/ready` che
fallisce; e l'amministratore che **ferma** lo stack invece di scrivere su un mount
degradato.

#### Spazio

Il preflight **rifiuta** sotto 5 GiB liberi e **avvisa** a 70% e 85%: bloccare
l'avvio di un servizio che sta lavorando bene perché il disco è pieno all'86%
sarebbe un danno, non una protezione.

⚠ 100 GB nel guest non sono 100 GB sul datastore. I dischi sono thin provisioned:
un datastore pieno si manifesta nel guest come errori di I/O o come filesystem in
sola lettura **con `df` che mostra spazio in abbondanza**. Nessun controllo dentro
la VM può vederlo; resta responsabilità del monitoraggio dell'infrastruttura.

#### Backup

Veeam, a livello di VM. **Entrambi i dischi virtuali devono appartenere alla VM
protetta e rientrare nel job.** Un job che copia solo il disco di sistema produce
un ripristino che parte, sembra sano e non contiene nessun dato: il tipo di backup
che si scopre incompleto il giorno del ripristino.

#### Test

- `tools/storage-config-test.py` — 86 controlli sulle **dichiarazioni**: volume
  ancorato, nessuna porta pubblicata per `db` e `api`, immagini fuori dal disco
  dati, override di sviluppo che disancora e non tocca altro, unità systemd
  (direttive, non commenti), codici del preflight, e completezza del runbook.
- `tools/storage-e2e-test.sh` + `tools/run-storage-e2e-test.ps1` — 31 controlli sul
  **comportamento**, con un filesystem vero: immagine da 7 GiB, `mkfs.ext4`, mount
  su `/srv/tsm-data`, stack di produzione avviato, scrittura verificata sul disco
  dedicato, poi smontaggio, controprova del danno, rifiuto del preflight,
  rimontaggio e dati ancora al loro posto.

Due cose imparate scrivendo i test, entrambe finite nel codice:

- **`st_dev` non risponde alla domanda giusta.** Su un filesystem overlay un file
  appena creato riporta un `st_dev` diverso da quello della sua stessa directory:
  il test dichiarava «non è sul filesystem di root» mentre i dati erano
  esattamente là. Si usa `findmnt -T`, che dice quale mount contiene un percorso.
- **`local a="$1" b="$a"` non è affidabile.** In alcune versioni di bash `local`
  crea tutti i nomi prima di valutare le assegnazioni, e con `set -u` il
  riferimento nella stessa istruzione aborta con «unbound variable». Le
  dichiarazioni sono separate.

E una che è finita in `.gitattributes`: gli script di shell **devono** essere LF.
Due file riscritti da uno strumento Windows sono arrivati alla macchina Linux con
CRLF, e `set -o pipefail` è diventato `set -o pipefail\r` → «invalid option name».
Il repository si modifica su Windows e si esegue su Oracle Linux: la regola non è
una preferenza di stile.

### 8.41 Worker delle notifiche di scadenza

`tsm-worker`: servizio Compose a parte, stessa immagine dell'API, comando
diverso. Manda **un digest** al giorno con le scadenze in avvicinamento.

#### Processo separato, e uno solo

Dentro FastAPI, il numero di scheduler dipenderebbe dal numero di worker di
Uvicorn: `--workers 4` produrrebbe quattro copie di ogni avviso, e il difetto
comparirebbe il giorno in cui qualcuno scala l'API per un motivo che con le
notifiche non c'entra niente.

`replicas: 1` è una dichiarazione d'intenti — non impedisce un
`docker compose run` a mano né due host puntati allo stesso database. La garanzia
è un **lock consultivo di sessione**: il secondo worker esce con un messaggio
invece di restare vivo e silenzioso.

⚠ `conn.close()` di SQLAlchemy **non** rilascia il lock: restituisce la
connessione al pool e la sessione col database resta aperta. In un processo che
muore non fa differenza, ma il lock sopravviveva alla fine del test che l'aveva
preso e faceva fallire il successivo. Da qui `release_singleton()`, chiamato
anche all'uscita ordinata del worker.

`/api/ready` **non** guarda il worker. Con il worker fermo l'applicazione resta
usabile: non partono gli avvisi, che è un guasto diverso. Legarli significherebbe
che un worker fermo fa togliere l'API dal bilanciatore. Il worker ha un battito
suo in `worker_heartbeat`, letto da `scripts/worker_health.py` — che è anche
l'healthcheck del container, quindi verifica «ha fatto un giro di recente **e**
vede il database», non «il processo è vivo».

#### Perché non APScheduler

La sostanza della richiesta — processo a parte, `zoneinfo`, recupero delle
esecuzioni perdute, nessuna dipendenza dal «misfire» in memoria — è rispettata,
ma senza la libreria. Con APScheduler la pianificazione vivrebbe in **due** posti:
la sua idea in memoria di «prossima esecuzione» e il registro durevole
`scheduler_runs`, che è quello che decide davvero. Due fonti di verità sullo
stesso fatto divergono, e quella che si legge nel codice non sarebbe quella che
comanda. Il ciclo è un `sleep` ogni cinque minuti; la domanda «tocca eseguire?»
ha una sola risposta, nel database.

#### Il calendario è locale, e il registro è per data locale

`garanzia` e `supporto` sono valori con la sola data. Si confrontano con la data
di calendario nel **fuso configurato**: a Roma le 00:30 locali sono ancora «ieri»
in UTC, e un promemoria a 30 giorni scatterebbe il giorno sbagliato per metà
dell'anno.

`scheduler_runs` ha la **data locale come chiave primaria**. Da questa forma
seguono tre comportamenti, senza codice dedicato:

| Situazione | Cosa accade |
|---|---|
| macchina spenta all'ora prevista | alla riaccensione la riga di oggi non c'è → il giro parte (recupero) |
| 29 marzo, le 02:30 locali **non esistono** | alle 03:05 l'orologio da parete ha superato 02:30 → il giro parte lo stesso giorno |
| 25 ottobre, le 02:30 accadono **due volte** | la seconda trova la riga di oggi conclusa → un digest, non due |

Il conflitto sulla chiave non è un `DO NOTHING`: riprende una riga rimasta **non
conclusa**. Un giro interrotto a metà lascerebbe la riga di oggi senza
`finished_at`, e un `DO NOTHING` direbbe «già fatto» perdendo l'intera giornata.

#### Recupero e precedenza fra soglie

Una scadenza è dovuta quando `0 <= giorni_rimanenti <= N` per almeno una soglia
`N` — **non** `giorni_rimanenti == N`. Pretendere il giorno esatto significherebbe
che una macchina spenta quel giorno perde il promemoria per sempre; il recupero è
una conseguenza della disuguaglianza, non un meccanismo a parte.

Da sola, però, la disuguaglianza produrrebbe tre email dopo un'assenza lunga. Per
ogni gruppo `(dispositivo, tipo, data)` si manda quindi **la soglia più urgente
fra quelle applicabili e non ancora inviate**, e le più larghe si marcano
`superseded`:

```text
warningDays = [90, 30, 7]      macchina spenta dal giorno 35 al giorno 5
→ la 90 era già stata mandata
→ parte la 7 (una email)
→ la 30 diventa «superata»
```

La più urgente è anche la più informativa: contiene i giorni che restano davvero.
Senza assenze, le tre soglie producono tre avvisi in tre momenti diversi — la
precedenza non sopprime il progresso normale, e un test lo fissa.

**Elementi già scaduti** (`giorni_rimanenti < 0`): esclusi. Un avviso su una
scadenza passata è un prodotto diverso — si ripete ogni giorno per sempre, o no? —
e questo commit non lo decide. Restano nella vista Scadenze. `warningDays = 0`
resta vietato da un `CHECK`: sarebbe un cambiamento di prodotto, non un effetto
collaterale.

#### Idempotenza durevole

Lo stato «già inviato» è nel database, non nella memoria del processo — che si
azzera precisamente quando la domanda diventa importante.

```text
UNIQUE (entity_uid, expiry_kind, expiry_date, threshold_days)
```

Il vincolo fa due lavori. Impedisce a due worker di creare lo stesso promemoria; e
poiché la **data** è parte della chiave, cambiare la scadenza di un dispositivo
apre un ciclo di vita nuovo senza una riga di codice dedicata.

#### Con SMTP non esiste esattamente-una-volta

```text
il relay accetta il messaggio
↓
il processo muore
↓
il database non ha ancora registrato «inviato»
```

Al ritentativo può partire un duplicato. **Non si dichiara
esattamente-una-volta**: si preferisce *almeno una volta* a una scadenza mai
comunicata. Il rischio si riduce, non si elimina:

- `Message-ID` generato dal server e **riusato a ogni ritentativo**, così un
  client di posta riconosce il duplicato invece di mostrare un secondo avviso;
- una consegna logica per digest, mai una nuova a ogni tentativo;
- tentativi contati **prima** dell'invio — contarli dopo renderebbe illimitati i
  tentativi proprio nel caso in cui l'invio fa morire il processo;
- attesa crescente e massimo cinque tentativi, poi la consegna passa a
  `retry_exhausted` e i promemoria tornano liberi con un'attesa di sei ore: un
  relay rotto non deve far ricomporre un digest a ogni giro, per sempre.

Lo stato si chiamava `abandoned`, e il nome diceva una cosa falsa. Chiude la
**consegna**, non il promemoria: passata l'attesa il promemoria torna eleggibile
e finisce in un digest nuovo, con un `Message-ID` nuovo — perché è un avviso
nuovo, non il ritentativo di quello vecchio. Un nome che promette la fine di
qualcosa che riprende sei ore dopo porta chi legge il registro a concludere che
un avviso è stato perso. Il test
`test_retry_exhausted_is_not_terminal_the_reminder_comes_back` fissa la
differenza: prima dell'attesa non parte niente, dopo parte una consegna nuova, e
quella vecchia resta chiusa.

Un fallimento di posta **non** marca i promemoria come inviati, e un fallimento
del database non fa dichiarare un invio che non è avvenuto.

#### Il digest, e il testo non attendibile

Un messaggio, non uno per dispositivo: trenta avvisi rendono la casella
inutilizzabile proprio quando c'è più da guardare, e la prima cosa che fa chi li
riceve è creare una regola che li sposta in una cartella — cioè disattivare la
notifica senza dirlo a nessuno. Raggruppato per tipo (garanzia, supporto) e per
urgenza.

Solo i campi che servono: nome, posizione, data, giorni rimanenti, soglia.
**Non** le note, che sono testo libero che nessuno ha chiesto di spedire.

Nomi di dispositivo, rack, sala e sito li scrive un utente. Non finiscono **mai**
in un'intestazione, e i caratteri di controllo si sostituiscono prima di comporre
il corpo. Un dispositivo chiamato `<b>srv-x</b>\r\nBcc: qualcuno@altrove.example`
compare nel corpo con quel nome — non si censura, è il suo nome — e non aggiunge
nessun destinatario: verificato con un server di posta vero, che ha visto
esattamente cinque `RCPT TO`.

#### Destinatari

Il worker usa **tutti** i destinatari salvati. Il tetto di tre è una misura
anti-abuso del solo endpoint di prova interattivo (§8.38): omettere destinatari da
un avviso reale significherebbe che qualcuno non riceve la notifica che ha
chiesto. Un test mette i due limiti a confronto nello stesso stato.

#### Impostazioni

Rilette a ogni giro. Con `enabled = false` non si manda niente **e non si registra
niente**: nemmeno righe «in attesa», così riaccendendo le notifiche si rivaluta da
zero e parte una sola email — la soglia più urgente — invece di uno scarico di
arretrati.

Cambiare i destinatari non fa rimandare ciò che è già stato consegnato: l'identità
del promemoria è la scadenza, non la configurazione. L'istantanea dei destinatari
si registra sulla consegna come **impronta e conteggio**, non come elenco: a chi
legge il registro serve sapere se la configurazione era quella, non avere una
seconda copia di indirizzi di persone.

#### Sorgente dei dati

Fase 1: si legge `inventory_head`, si guardano i dispositivi canonici in Python e
si estraggono `garanzia` e `supporto`. Nessuna tabella di dispositivi in SQL: la
fase 2 potrà sostituire la scansione con query indicizzate **senza cambiare la
semantica delle notifiche**, che è la ragione per cui le due cose restano
separate.

⚠ Il seed di produzione **non ha nessuna data di scadenza** — verificato. Un
worker che gira sul seed e non manda niente non dimostra di funzionare: dimostra
che non ci sono dati. Da qui `fixtures/expiry/build.py`, che genera un inventario
con date **relative** a una data di riferimento (le date fisse smettono di provare
ciò che dicono il giorno dopo) e copre: scadenza oggi, soglia esatta, dentro la
finestra senza il giorno esatto, già scaduto, scaduto ieri, campi vuoti, date
illeggibili, un nome ostile, e **due dispositivi con lo stesso `id` di business e
`_uid` diversi** — perché l'identità è l'`_uid`, e negli inventari importati da
fogli di calcolo gli `id` ripetuti sono la norma.

#### Test

`backend/tests/test_expiry_scan.py` (50, pura) e `test_worker_pg.py`
(52, PostgreSQL reale con finto server di posta). Più una verifica end-to-end sullo
stack reale: worker in container, PostgreSQL vero, server di posta vero, digest
consegnato a cinque destinatari, secondo giro che non manda niente, secondo worker
che rifiuta il lock.

Tre cose imparate scrivendo i test:

- **Un test passava per il motivo sbagliato.** «Un promemoria già consegnato non
  si rimanda» girava con tutte e tre le finestre, e il giorno dopo partiva
  comunque un digest — corretto! — perché `srv-91` entra nella finestra da 90.
  Il test ora isola la variabile con una finestra sola, e un test nuovo fissa il
  comportamento giusto: la voce che entra in finestra si manda una volta, senza
  ripetere quelle già avvisate.
- **Il lock consultivo sopravvive a `close()`** (sopra): il difetto era nel test,
  il fatto meritava una funzione.
- **`last_run_date` restava sempre NULL.** Il giro non riportava la propria data
  locale, quindi il campo che il monitoraggio guarda non si popolava mai:
  invisibile ai test di invio, visibile solo leggendo lo stato. Ora `TickResult`
  la porta e due test la fissano.

---

### 8.42 Fase 2: normalizzazione relazionale

#### L'architettura, congelata

```text
tabelle normalizzate     → stato operativo CORRENTE, autorevole
inventory_versions.doc   → istantanee storiche canoniche, immutabili, per sempre
```

La fase 2 **non cancella la storia in JSON**, e non la riscrive. Un ripristino
carica un'istantanea storica, sincronizza le tabelle correnti su di essa e crea
una versione **nuova**: la storia non si modifica, ci si aggiunge. È
l'append-only (§8.19) applicato un livello più su.

Le due rappresentazioni rispondono a due domande diverse. «Com'è l'inventario
adesso, e quali dispositivi scadono entro trenta giorni?» è una query, e vuole
tabelle e indici. «Com'era il 14 marzo, e chi l'ha cambiato?» è un'istantanea, e
vuole un documento intero che nessuno può aver alterato dopo.

#### Fase 2A — schema e mappa pura ✔ fatto

Migrazione `0010_normalised` (tabelle, vincoli, privilegi di sola lettura) e
`app/inventory/relational.py` + `relational_validate.py` (mappa pura). **`GET` e
`PUT` non cambiano**, niente popola le tabelle, niente le legge.

##### L'identità è l'`_uid`

Chiave primaria di ogni entità identificata: siti, sale, rack, dispositivi, voci
di manuale. Codice del rack e identificativo del dispositivo restano **attributi
mutabili**: una rinomina è un `rename` che conserva l'identità, e una chiave
primaria sul codice trasformerebbe ogni rinomina in «entità diversa», spezzando la
storia proprio nel caso che §8.4 esiste per proteggere.

##### Unicità con AMBITO, e differibile

```text
inventory_locations       UNIQUE (code)                    ambito: documento
inventory_rooms           UNIQUE (location_uid, code)      ambito: sito
inventory_racks           UNIQUE (room_uid, code)          ambito: sala
inventory_manual_entries  UNIQUE (code)                    ambito: documento
+ UNIQUE (genitore, ordinal) su ogni collezione
```

Tutti `DEFERRABLE INITIALLY IMMEDIATE`. Scambiare il codice di due rack è
legittimo e a metà transazione i due valori collidono; senza `DEFERRABLE` l'unica
via sarebbe un valore di comodo intermedio, cioè uno stato che non è mai stato
vero scritto nel database per aggirare un vincolo. `INITIALLY IMMEDIATE` resta il
default perché un errore che compare sullo statement colpevole si diagnostica, uno
che compare al commit no.

⚠ **Nessun vincolo su `(rack_uid, code)` per i dispositivi**, e non è una
dimenticanza. L'identificativo arriva dall'import tabellare, dove due righe con lo
stesso identificativo di asset nello stesso rack sono un caso reale; il validatore
di identità le tollera da sempre e l'interfaccia non le impedisce. Vincolarle
farebbe rifiutare alla fase 2C documenti che la fase 1 accetta — un cambio di
comportamento introdotto di straforo. `validate_model` lo segnala come **avviso**,
e diventerà una decisione di prodotto quando qualcuno vorrà prenderla.

##### L'ordine è un dato

Colonna `ordinal` esplicita su ogni collezione, e `assemble` ordina per quella.
L'ordine delle righe che PostgreSQL restituisce senza `ORDER BY` non è definito, e
un riordino è un evento di dominio (§8.10): affidarsi all'ordine fisico
produrrebbe eventi `reorder` che nessuno ha causato, al primo `VACUUM`.

##### I vani restano un value object

Sono già stati classificati così (§8.12) e la classificazione regge: nessuna
identità immutabile visibile all'utente, nessun CRUD indipendente, nessuna
semantica di spostamento, nessuna interrogazione globale. Restano **JSONB
posseduto dalla sala**, e la geometria della porta resta annidata nel vano — che
può averne due (`porta`, `porta2`): il seed di produzione ne contiene già un caso.

Una tabella `vani` più una tabella `porte` costerebbero due join per disegnare una
pianta, un ordinale in più da mantenere e due cascate da gestire, in cambio di
nessuna garanzia: non esiste alcun vincolo di integrità fra un vano e il resto del
mondo. **Normalizzare serve all'integrità e all'interrogabilità, non a trasformare
ogni oggetto annidato in una tabella.** Stessa regola per i `blocchi` di una voce
di manuale.

##### Il documento è APERTO: la conseguenza che decide il progetto

Lo schema congelato (§8.16) vincola le chiavi di **radice**, non i campi delle
entità: `validate_document` pretende un `_uid` valido e univoco e non dice nulla
sulle altre chiavi. Il frontend, di suo, deriva ogni entità dall'esistente proprio
perché «i campi sconosciuti e i metadati futuri sopravvivono» (§8.4).

Una mappa che elencasse le colonne e buttasse il resto sarebbe quindi **lossy per
costruzione**: basterebbe un campo aggiunto dall'interfaccia perché l'invariante
cada, e cadrebbe in produzione, sul documento di un cliente. Ogni entità ha perciò
una colonna `extra` (JSONB) che porta ciò che le colonne non rappresentano, più una
regola che vale nei due versi:

```text
la colonna vale NULL  ⇔  la chiave è in `extra`
```

Un documento aperto può contenere `u: "45"` o `seriali: ["ok", 12345]`. Una colonna
NOT NULL con un valore di comodo più la copia in `extra` darebbe una tabella
interrogabile che risponde il falso, che è peggio di una che dichiara di non
sapere. Le uniche colonne sempre valorizzate sono quelle che generiamo noi: `uid`,
il riferimento al genitore, `ordinal`.

Sul seed di produzione **nessun campo finisce in `extra`** — verificato da un
test: la normalizzazione è completa sui dati veri, e il carrello serve ai casi che
non ci sono ancora.

##### `garanzia` e `supporto` sono TESTO, non `date`

L'inventario reale contiene «in attesa», date malformate e caselle vuote. Una
colonna `date` costringerebbe a scartare o a reinterpretare quei valori, cioè a
perdere il dato per farlo entrare in un tipo. Il posto dove si decide che una data
non è leggibile è già lo scanner delle scadenze (§8.41), e la validazione del
modello **usa il suo parser**: così l'avviso significa esattamente «il worker
ignorerà questa data» invece di essere una seconda idea di «data valida» che
divergerà sui casi limite.

##### L'invariante

```text
canonicalise(assemble(normalise(doc))) == canonicalise(doc)
⇒ canonical_sha256 uguale
⇒ diff_documents(originale, giro completo) == []
```

Le tre asserzioni non sono ridondanti: la prima confronta strutture, la seconda la
serializzazione canonica (ordine delle chiavi, forma dei numeri), la terza il
significato secondo il motore di diff — cioè ciò che l'utente vedrebbe nel
registro. Più una quarta, meno ovvia: **anche il MODELLO deve tornare identico**.
Se il giro perdesse un valore da una colonna e lo ritrovasse in `extra`, i due
documenti resterebbero uguali e le tabelle no — il difetto sarebbe invisibile
esattamente dove conta.

##### Due gravità nella validazione

`ERROR` — il modello non rappresenta fedelmente lo stato: `_uid` duplicati o
malformati, genitori inesistenti, codici duplicati dove è vietato, ordinali
duplicati, `extra` che ombreggia una colonna, foto inesistente, `schemaVersion`
assente. Un `ERROR` deve fermare una migrazione o una scrittura.

`WARNING` — lo stato è rappresentato correttamente ma la tabella non lo può
interrogare (`carried_verbatim`), o il valore è fuori da un vocabolario noto
(`invalid_enum`, `invalid_date`), o è un identificativo di dispositivo ripetuto.
Un `WARNING` non ferma niente: l'inventario reale è pieno di caselle scritte a
mano, e rifiutarle vorrebbe dire perdere il dato invece di correggerlo.

#### Fase 2B — popolamento del solo head, con confronto dei digest ✔ fatto

Migrazione `0011_projection` (colonne data derivate, stato della proiezione),
`app/inventory/projection.py` (costruzione, rilettura, verifica) e
`backend/scripts/project.py` (il comando). **`GET`, `PUT`, readiness, scheduler e
frontend non cambiano di una riga, e nessuno di loro consuma la proiezione.**

##### La procedura, in ordine

1. **lock** della riga di testa (`FOR UPDATE`), come fa un salvataggio (§8.11);
2. lettura del documento e del digest **registrato** di quella versione;
3. il digest registrato deve combaciare con quello ricalcolato;
4. `normalise` + `validate_model`: nessun errore, o si aborta **prima di scrivere**;
5. si svuota la proiezione e la si riscrive per intero, riga di stato compresa;
6. si rilegge **da SQL** e si riassembla;
7. il modello riletto deve essere uguale a quello scritto **e** il digest del
   documento riassemblato deve combaciare con quello registrato.

Il passo 1 è ciò che rende «atomica sotto la testa bloccata» una frase con un
significato: un `PUT` concorrente aspetta lì, quindi la proiezione non può
rispecchiare una testa cambiata sotto di lei. Due test lo provano nei due sensi, con
`lock_timeout` per trasformare l'attesa in un errore osservabile invece di un test
che si blocca.

Il passo 7 è la ragione di tutto il resto: un popolamento «che sembra andato bene»
non vale niente. E sono **due** confronti, non uno. Il digest dice che il documento
è quello; il modello dice che le TABELLE sono quelle. Se un valore uscisse da una
colonna e rientrasse in `extra`, i due documenti resterebbero identici e il digest
combacerebbe — mentre la parte interrogabile, che è il motivo per cui la
normalizzazione esiste, sarebbe diversa.

Solo la testa viene normalizzata. Le versioni storiche restano istantanee JSON e
**non vengono riscritte**: sono immutabili per definizione, e riscriverle
significherebbe cambiare la storia per farla combaciare con una mappa nuova, che è
l'esatto contrario del motivo per cui esistono.

##### Un comando, non una migrazione di dati e non un servizio

```text
python scripts/project.py --status     che versione rispecchia (sola lettura)
python scripts/project.py --verify     riassembla da SQL e confronta (sola lettura)
python scripts/project.py --rebuild    ricostruisce, e aborta se non torna
```

Gira come **proprietario dello schema**: la `0011` non concede scrittura a nessun
ruolo di runtime, e le `REVOKE` esplicite mettono l'intenzione nello schema. Un test
prova che `tsm_api` e `tsm_worker`, provandoci, ottengono «permission denied».

Una migrazione di dati si esegue una volta sola, all'avvio, senza che nessuno la
guardi, e se aborta ferma il deployment. Questo popolamento deve poter essere
rieseguito, deve confrontare un digest, deve poter dire di no, e il suo esito deve
essere **letto** da una persona. Un servizio, dall'altra parte, lo eseguirebbe da
solo: manterrebbe aggiornata una rappresentazione che nessuno legge, e i guasti si
scoprirebbero il giorno in cui qualcuno comincia a leggerla.

I codici di uscita distinguono due domande diverse. `--verify` vale 0 se le tabelle
riassemblano **la versione che dichiarano di rispecchiare** (fedeltà) e 1 altrimenti;
una proiezione **vecchia** non è un errore e non cambia il codice, perché in fase 2B
non esserlo è normale — la sincronizzazione a ogni salvataggio è la 2C. Confondere
attualità e fedeltà significherebbe far suonare un allarme a ogni `PUT`.

##### Una proiezione vecchia si vede

`inventory_state` è diventata `inventory_projection_state`: il nome vecchio diceva
«stato dell'inventario», che è falso — lo stato dell'inventario è la testa. La riga
registra `head_version` **e** `head_sha256`, entrambi `NOT NULL`, e l'assenza della
riga resta il modo di dire «non rispecchia nulla». La versione dice *quale*
istantanea; il digest dice *che cosa* si è verificato in quel momento, ed è il
confronto che scopre una proiezione modificata a mano o un ripristino parziale.

Dopo un `PUT` la proiezione resta indietro **per progetto**, con il documento vecchio
e non con metà del nuovo, e `--status` lo dice in italiano citando la fase 2C, così
chi legge non va a cercare un guasto che non c'è.

##### `garanzia_date` e `supporto_date`: colonne derivate

`garanzia` e `supporto` restano **testo** (§8.42 sopra). Ma finché la data esiste
solo come testo, «quali dispositivi scadono entro trenta giorni» non è una query: è
la scansione dell'intero documento in Python che fa lo scanner delle scadenze
(§8.41). Le due colonne aggiungono la forma interrogabile senza toccare quella
autorevole:

```text
garanzia       testo dell'utente, autorevole, torna nel documento
garanzia_date  data interpretata, derivata, NON torna nel documento
```

L'interpretazione usa **il parser dello scanner**, non uno scritto in SQL. Una colonna
`GENERATED ALWAYS AS` o un `CHECK` con l'espressione sarebbero stati la scelta ovvia e
sbagliata: una seconda idea di «data valida» diverge dalla prima, e diverge proprio
sui casi limite, che sono i valori che l'inventario reale contiene. Il `CHECK` si
limita a ciò che si può dire senza reimplementare il parser: `garanzia_date IS NULL OR
garanzia IS NOT NULL` — una data interpretata non può sopravvivere al testo da cui è
stata interpretata. Gli indici sono **parziali** (`WHERE ... IS NOT NULL`): la domanda
implica il non-nullo, e nel seed reale la maggior parte dei dispositivi non ha date.

⚠ **L'invariante del giro completo non può vedere una data derivata sbagliata**: non
tornando nel documento, lascia il digest identico. La vede solo `validate_model`, che
la chiama `derived_mismatch` — ed è un `ERROR`. Per la stessa ragione `verify()` non
guarda solo i digest: se lo facesse, l'unico difetto che l'invariante non copre
sarebbe anche l'unico che lo strumento fatto per coprirlo non guarda.

Un test prova che la query SQL sulle date restituisce **esattamente** lo stesso
insieme di `due_items`: se divergessero, il giorno in cui la vista Scadenze passerà a
SQL gli avvisi cambierebbero senza che nessuno abbia cambiato niente.

##### Quattro cose che questo commit ha trovato

- **Legare un `float` a una colonna `numeric` è lossy.** Misurato con una sonda
  contro PostgreSQL vero, non ragionato: `10.0` torna `10` (intero!) e
  `0.30000000000000004` torna `0.3`. psycopg lo manda come `float8`, e la
  conversione a `numeric` perde la scala. Si lega `Decimal(repr(v))`, e la scala
  conservata è ciò che permette di sapere, rileggendo, se il valore era un intero o
  un float. Le due metà del contratto (`to_column_number` /
  `from_column_number`) stanno accanto al predicato che le giustifica, perché
  separarle vorrebbe dire che `_is_num` promette una fedeltà che dipende da codice
  scritto altrove.
- **Restano due numeri che `numeric` non può restituire**, e stanno in `extra`: i
  float che `repr` scrive con esponente positivo (`1e+16`, `1e+20`), che tornerebbero
  interi, e `-0.0`, perché `numeric` non ha il segno dello zero. Il secondo lo aveva
  dichiarato «fedele» la prima versione della sonda, che confrontava con `==`:
  `-0.0 == 0.0` è vero, e `json.dumps` scrive due cose diverse — cioè due digest
  diversi.
- **`u` e `h` sono `integer`, cioè int32.** `u: 3000000000` non è un caso teorico da
  cui difendersi: è un `INSERT` che fallisce con «integer out of range» a metà del
  popolamento, per un dato che la fase 1 ha sempre accettato.
- ⚠ **JSONB perde `1e+20` e `-0.0`, e `inventory_versions.doc` È jsonb.** Misurato:
  diventano `100000000000000000000` e `0.0`. Quindi un documento con quei valori
  viene salvato, ma il digest **registrato** al salvataggio non corrisponde più al
  documento che si rilegge. **Non è un difetto della proiezione**: è una proprietà
  del magazzino delle istantanee, che il confronto dei digest ha reso visibile. Il
  passo 3 aborta dicendo esattamente questo, invece di ricalcolare il digest in
  silenzio — che sarebbe coprire il caso in cui un'istantanea immutabile non
  corrisponde al suo digest. Il difetto a monte (un `PUT` con quei numeri restituisce
  al client un documento diverso da quello inviato) è stato **chiuso subito dopo**,
  dove andava chiuso: nella validazione dello schema congelato (§8.16, invariante del
  magazzino). Da allora un documento così non diventa più una versione; quelle
  scritte prima restano, e su una testa così la ricostruzione aborta — che è la
  diagnosi corretta, non un guasto della proiezione.

Più il difetto trovato dal primo tentativo: la riga di stato era scritta **alla
fine**, e sembrava più prudente — «nessuna riga dichiara una proiezione fedele finché
non lo è». Era sbagliato, perché quella riga porta anche `schemaVersion`, `has_manual`
e `root_extra`, cioè la radice del documento: scritta dopo la rilettura, il passo 6
rileggeva una radice vuota e il confronto falliva su una differenza che il popolamento
non aveva commesso. La prudenza non serviva — un abort SOLLEVA e la transazione va in
rollback, quindi una riga che esiste è una riga la cui verifica è passata. È il
rollback a garantirlo, non l'ordine degli statement.

##### E chi non deve consumarla

Un controllo statico verifica che **soltanto `projection.py`** scriva le tabelle, e
che né le rotte, né la readiness, né il worker nominino la proiezione o le sue
tabelle. `projection` **non** è riesportata da `app/inventory/__init__.py`, che
`app/api/inventory.py` importa: riesportarla la renderebbe raggiungibile dal percorso
delle richieste con un `import` scritto per sbaglio.

I test di comportamento provano la stessa cosa dal lato opposto, e nel modo più
forte possibile: si ricostruisce la proiezione del seed, si **cancella un sito dalle
tabelle**, e `GET /api/inventory` restituisce ancora tutti e 102 i rack. La readiness
resta a tre condizioni (§8.23) con la proiezione vuota e con la proiezione vecchia:
farla diventare la quarta significherebbe che il servizio non parte perché una
rappresentazione che nessuno legge non è aggiornata.

#### Fase 2C — il `PUT` sincronizza, in una transazione sola ⟵ prossimo

Un salvataggio dovrà, **atomicamente**: validare e canonicalizzare come oggi,
sincronizzare le tabelle normalizzate, inserire l'istantanea immutabile, scrivere
l'audit e registrare i riferimenti alle foto (§8.5). Se una qualsiasi di queste
scritture non riesce, non ne sopravvive nessuna — è lo stesso ordine di §8.11, con
un passo in più.

Qui servirà `SET CONSTRAINTS ALL DEFERRED` sui rinomini e sui riordini, ed è il
momento in cui i vincoli differibili guadagnano il loro costo.

#### Fase 2D — il `GET` legge da SQL, ma non subito

Il passaggio avviene **solo dopo** che la rappresentazione in ombra ha dimostrato
ripetutamente di essere uguale alla testa canonica. Prima la proiezione si
mantiene e si confronta senza servirla; poi, quando il confronto è verde da
abbastanza tempo e su dati veri, `GET` passa all'assemblaggio da SQL.

Non è prudenza generica: un `GET` che assembla male restituisce un documento
plausibile, il client lo rimanda con un `PUT`, e la differenza diventa una
versione nuova con un contenuto che nessuno ha scritto. Il confronto in ombra è
ciò che rende quel guasto visibile mentre è ancora innocuo.

#### Test

```text
test_relational_mapper.py     puro         la mappa, i predicati, le derivate
test_relational_schema_pg.py  PostgreSQL   forma, vincoli, privilegi
test_projection_pg.py         PostgreSQL   popolamento, rilettura, digest, lock
```

**Ventiquattro documenti** sotto esame, fra cui il **seed di produzione** (nelle due
forme: come sta nel repository, con le radici legacy, e come sta nel database dopo il
bootstrap), l'**inventario delle scadenze**, e le varianti che coprono rinomine,
spostamenti, riordini, default espliciti e impliciti, stringhe vuote/zeri/`False`,
`manuale` assente contro vuoto, rack con e senza foto, `foto: null` esplicito, campi
ignoti a ogni livello, valori non tipizzabili, date rotte e date buone, vocabolari
sconosciuti, codici duplicati, geometria di sala complicata, numeri e interi che le
colonne non possono contenere.

La suite pura li verifica in memoria; quella su PostgreSQL li fa passare **dal
database vero**, uno per uno, confrontando il digest riassemblato con quello
**registrato** nella versione. Sono due prove diverse: la prima dice che la mappa non
perde niente, la seconda che non lo perde nemmeno il giro attraverso JSONB, i
`numeric`, i `text[]` e gli `uuid` — che è il passaggio dove un numero cambia forma e
un `uuid` cambia tipo.

Tre cose che i test della fase 2A hanno trovato:

- **La mappa era lossy su `seriali`.** `isinstance(v, list)` diceva
  «rappresentabile» per una colonna `text[]`, ma `["ok", 12345]` in un `text[]`
  diventa `{"ok","12345"}`: il numero torna indietro come stringa e l'invariante
  cade **in silenzio**. Da qui i predicati espliciti al posto degli elenchi di
  tipi, e `_is_str_list` distinto da `_is_json_list`.
- **`bool` è un `int` in Python.** Senza cura, `u: True` sarebbe finito in una
  colonna intera come 1, e il diff l'avrebbe riportato come una modifica
  dell'utente.
- **La riga di stato sparisce con `TRUNCATE inventory_versions CASCADE`**, perché
  `head_version` la referenzia. Invece di seminarla e vederla scomparire, la tabella
  è a zero-o-una riga e **l'assenza è il dato**: senza versioni non c'è niente da
  rispecchiare.

Più il test che tiene insieme i due file — colonne SQL contro campi delle dataclass,
confrontati per ogni entità, e ogni campo che deve stare in esattamente una categoria
fra mappato, generato e derivato — e quello che prova la cosa centrale **provando a
violarla**: dopo un salvataggio reale le tabelle normalizzate restano vuote e lo stato
della proiezione non registra niente.

E le controprove del metodo, che sono la parte che vale di più:
`test_the_invariant_is_capable_of_failing` costruisce una mappa che butta `extra` e
pretende che l'invariante **cada**; `test_a_rebuild_that_does_not_round_trip_aborts`
rompe `assemble` e pretende l'abort più un database intatto. Senza di loro, un
confronto scritto male — che confronta la cosa sbagliata, o due volte lo stesso
oggetto — sarebbe indistinguibile da un confronto soddisfatto, e tutta la suite
passerebbe senza dimostrare niente.

### 8.43 Politica delle password e Argon2id

TSM usa la password come **unico fattore** di autenticazione normale. Non c'è un
secondo fattore, non c'è SSO, e in rete chiusa non ci sarà nemmeno un servizio
esterno da interrogare. Tutto il peso sta quindi su tre cose: quanto è difficile
indovinare la password, quanto costa provarne una, e che cosa resta nel database.

La politica vive in **un** modulo, `app/auth/passwords.py`. Prima viveva in tre
posti e diceva tre cose diverse: il cambio password chiedeva dieci caratteri, la
creazione da amministratore non chiedeva niente, il bootstrap nemmeno. Una politica
distribuita vale quanto il suo punto più debole, e il punto più debole era quello
che crea il **primo amministratore**.

#### Che cosa si chiede a una password

| | |
|---|---|
| minimo | **15** code point, misurati dopo la normalizzazione |
| massimo | **128** code point, **rifiutati** e mai troncati |
| spazi | ammessi, anche iniziali e finali, e mai rimossi |
| composizione | **nessun requisito**: né maiuscole, né cifre, né simboli |
| scadenza periodica | **nessuna** |
| lista locale | uguaglianza sull'intera password, mai sottostringa |

Le due assenze sono scelte, non dimenticanze.

**Niente regole di composizione.** Obbligare maiuscole, cifre e simboli sposta il
costo sull'utente e la sicurezza da nessuna parte: produce `Estate2026!` e poi
`Estate2026!!`. Il lavoro lo fanno la lunghezza minima alta — che rende una
passphrase la scelta naturale — e la lista dei valori prevedibili.

**Niente scadenza periodica.** Un cambio forzato ogni novanta giorni produce
`Estate2026!` che diventa `Autunno2026!`, cioè trasforma una password buona in una
sequenza indovinabile. Il cambio si impone dopo un **evento**: reimpostazione
amministrativa, password provvisoria, sospetto di compromissione. Sono eventi, non
date sul calendario.

**Si rifiuta, non si tronca.** Troncare a 128 renderebbe equivalenti due password
diverse: chi ne digita 200 crederebbe di averne 200, e chiunque ne conoscesse le
prime 128 entrerebbe. Un rifiuto è visibile e recuperabile.

#### La normalizzazione Unicode, e perché non è del chiamante

Prima di calcolare e prima di verificare, la password passa da **NFC**. La stessa
funzione, in tutti i punti: impostazione, cambio, sostituzione di una provvisoria,
verifica all'accesso, confronto con la lista.

Il motivo è un difetto che si vedrebbe solo in produzione e solo su una
piattaforma: la stessa persona, la stessa tastiera, e un sistema operativo che
consegna `città` in forma decomposta (`citta` + accento combinante) invece che
composta. Senza normalizzazione l'accesso viene negato e **niente risulta sbagliato
da nessuna parte** — non c'è un errore da leggere, non c'è una riga di log che dica
perché.

Per questo `hash_password` e `verify_password` normalizzano **dentro**, e non
chiedono al chiamante di farlo: se fosse compito di chi chiama, basterebbe un punto
che se ne dimentica. Misurato: NFC può anche **accorciare** una password (27 code
point → 26), quindi la lunghezza si misura una volta sola, dopo la normalizzazione.

Ciò che la normalizzazione **non** fa: non toglie gli spazi ai bordi, non applica
`casefold`, non rimuove caratteri di controllo, non sostituisce niente. Una password
è una sequenza di code point scelta dall'utente, non un campo da ripulire — e
ripulire significherebbe accettare all'accesso qualcosa di diverso da quello che è
stato impostato. Il `casefold` esiste in un solo posto: il confronto con la lista,
perché `Password` e `password` sono la stessa scelta debole ma restano due password
diverse per Argon2.

#### Argon2id: i parametri sono fissati qui

```
algoritmo      Argon2id          (non Argon2i, non Argon2d)
versione       0x13 (v=19)
memoria        65536 KiB = 64 MiB
iterazioni     3
parallelismo   4
output         32 byte
sale           16 byte, generati dalla libreria per OGNI hash
```

Sono i valori della seconda configurazione raccomandata da RFC 9106 («low memory»)
e **superano** il minimo richiesto di 19456 KiB / t=2 / p=1. Latenza misurata di una
verifica in container: **~72 ms**, invisibile per un accesso interattivo e proibitiva
per chi provasse a indovinare in massa. La configurazione più forte era già in uso e
si conserva: declassarla al minimo riscriverebbe in **peggio** ogni hash esistente
al primo accesso, perché la riscrittura scatta su qualunque differenza di parametri,
non solo in salita.

Coincidono con i default di `argon2-cffi` 25.1.0, ma **coincidere per scelta e
coincidere per caso sono cose diverse**. I default di una libreria cambiano quando
cambiano le raccomandazioni, e un aggiornamento di dipendenza non deve poter
spostare la sicurezza dell'applicazione — né in basso, né in alto a sorpresa, perché
anche un irrobustimento non voluto trasformerebbe ogni accesso in una riscrittura di
hash che l'operatore non ha deciso.

#### Il sale, e perché non ha una colonna

Il sale lo genera `PasswordHasher.hash` da un CSPRNG, uno nuovo per ogni chiamata, e
finisce **dentro** l'hash codificato:

```
$argon2id$v=19$m=65536,t=3,p=4$<sale>$<digest>
```

Non è un segreto e non ha bisogno di una colonna sua. Non si passa a mano, non si
deriva dallo username o dall'id, non ne esiste uno globale: tutte varianti che
renderebbero **confrontabili fra loro** gli hash di utenti diversi, che è
precisamente ciò che il sale serve a impedire. Nel database c'è `password_hash` e
nient'altro; una colonna in più sarebbe un posto dove qualcuno, un giorno, scrive la
cosa sbagliata.

#### Pepper: non c'è

**TSM non usa un pepper.** Il progetto attuale è Argon2id con sale unico per
password, e basta così.

Non è pigrizia: un pepper è un segreto **operativo**, e un segreto operativo senza
una storia di rotazione è un debito che si paga tutto insieme. Prima di
introdurne uno servono quattro cose definite — dove è conservato (fuori da
PostgreSQL, altrimenti non aggiunge niente), come si ruota, come si recupera se si
perde, e che effetto ha sugli hash esistenti (risposta: li rende tutti inservibili,
quindi la rotazione richiede una migrazione al prossimo accesso di ognuno). Senza
quelle quattro risposte, un pepper è un modo di trasformare un guasto del gestore
dei segreti in «nessuno può più entrare».

#### La lista locale

`app/auth/password-blocklist.txt`, letto all'avvio, dentro `app/` così che la `COPY`
dell'immagine lo porti già. Nessuna API di password compromesse: in rete chiusa non
si può interrogare niente, e un percorso configurabile sarebbe un percorso che in
produzione può puntare a un file assente — cioè un controllo che si disattiva in
silenzio. Se il file manca, si solleva.

Il confronto è di **uguaglianza** sull'intera password, dopo NFC e `casefold`. Non
di inclusione: cercare le voci dentro la password sembra più severo ed è
controproducente, perché rifiuterebbe `il gatto dorme sul tetto di casa` per la
parola `casa` — colpendo esattamente le passphrase lunghe che la politica vuole
incoraggiare, e lasciando passare `Estate2026!` che non contiene nessuna voce. Chi
sceglie una voce della lista la sceglie intera.

Con il minimo a 15 caratteri, `password` e `123456` cadono già per **lunghezza**. Il
lavoro vero della lista è l'altra cosa: che cosa scrive una persona a cui il sistema
ha appena chiesto quindici caratteri. Le risposte sono poche e ricorrenti — la
password corta scritta due volte (`passwordpassword`), una camminata più lunga sulla
tastiera (`qwertyuiopasdfgh`), una frase fatta, il nome del programma
(`trustservermanager`, `trusttechnologies`, `saleserverpomezia`), un valore «da
cambiare» che nessuno ha cambiato (`changethispassword`, `passwordprovvisoria`).
Sono quelle le voci che contano, e sono quelle che una lista pensata per un minimo
di otto caratteri non contiene.

Si aggiunge una regola che nessun file può contenere: la password **uguale allo
username**, che dipende dall'utenza.

La lista si applica quando una password si **imposta**, mai quando si verifica.
Aggiungere una voce non invalida nessun hash esistente, ed è ciò che rende il file
manutenibile: chi ha già una password che oggi finisce in lista non deve scoprirlo
durante un accesso, da dove non c'è via d'uscita.

#### Password provvisorie

Le genera il **server**, sempre: 24 byte da CSPRNG = **192 bit** di entropia, oltre
il minimo richiesto di 128, resi in 32 caratteri URL-safe che si copiano e si
incollano senza ambiguità. Nessuna rotta accetta una password scelta
dall'amministratore, quindi non esiste un campo del contratto con cui indebolirla.
La generazione vive accanto alla politica e ci passa attraverso: erano 12 byte — 96
bit, **sotto** il minimo — proprio perché quel numero stava in un altro file,
lontano dalla lunghezza minima delle password.

Una provvisoria: torna **una volta sola** nella risposta, non viene mai registrata
in chiaro, non entra in audit, log, inventario né nella memoria persistente del
browser (vive solo nello stato React del riquadro che la mostra), e imposta
`must_change_pw`.

#### Il primo cambio, e i cambi ordinari

Una sola strada, `change_own_password`, per il cambio dopo una provvisoria e per
quello ordinario. Non sono due flussi: le regole sono le stesse, e averne due li
farebbe divergere — il ramo «provvisoria» è quello che si scrive in fretta, ed è
quello dove un controllo si dimentica.

L'ordine dei controlli è deliberato:

1. **la password attuale, per prima.** Senza, chi si impossessa di una sessione
   aperta — un portatile lasciato sbloccato — cambierebbe la password senza
   conoscere quella vecchia, e l'accesso diventerebbe suo. C'è anche una seconda
   ragione: chi possiede una sessione ma non la password potrebbe altrimenti
   **sondare la politica e la lista** inviando password nuove e leggendo i codici di
   errore;
2. la politica completa sulla nuova;
3. che non sia **identica** a quella attuale — confrontata con l'hash memorizzato,
   così vale anche fra due forme Unicode diverse dello stesso valore.

Il terzo controllo conta soprattutto per la provvisoria: senza, `must_change_pw` si
azzererebbe lasciando in uso esattamente il valore che l'amministratore ha
comunicato a voce o scritto in un messaggio, e che quindi conosce anche lui.

Un cambio riuscito revoca **tutte** le sessioni, compresa quella che lo sta
facendo, e il cookie viene rimosso: serve un accesso nuovo. Una reimpostazione
amministrativa fa lo stesso, non richiede né espone la password precedente, e
registra soltanto che è avvenuta.

Non esiste uno storico delle password oltre al confronto con quella attuale. Un
sottosistema di storico richiederebbe di conservare più hash per utente — cioè più
materiale attaccabile — per impedire una rotazione fra due valori che la politica
già scoraggia togliendo la scadenza periodica.

#### Aggiornamento degli hash vecchi

Dopo un accesso **riuscito**, se `check_needs_rehash` dice che i parametri
memorizzati non sono quelli correnti, l'hash si ricalcola e si riscrive nella stessa
transazione della richiesta. È l'unico momento in cui la password in chiaro esiste
insieme a un hash verificato, quindi l'unico in cui si può fare.

Per l'utente non cambia niente: nessun errore, nessun obbligo di cambio, nessuna
latenza percepibile oltre a un secondo calcolo. Chi ha una password valida non deve
pagare in usabilità il fatto che i parametri di ieri fossero più deboli di quelli di
oggi. Nel registro va il **fatto** (`auth.password.rehashed`), con dettaglio vuoto:
né l'hash vecchio, né il nuovo, né i parametri di partenza, che direbbero a chi
legge il registro quanto era debole quell'utenza fino a un istante prima.

Lo decide la libreria leggendo i parametri dentro l'hash, non una nostra ispezione
della stringa: è l'unica fonte che resta corretta se un giorno cambiassimo i
parametri.

#### La politica non indebolisce la resistenza all'enumerazione

L'accesso resta come in §8.28: un solo errore per utenza inesistente, password
errata e utenza disabilitata; verifica Argon2 con un hash finto anche quando
l'utenza non esiste, generato con gli **stessi** parametri di quelli reali (se
restasse indietro, un'utenza inesistente costerebbe meno di una esistente e la
differenza di tempo tornerebbe misurabile); limitazione dei tentativi.

La lista **non si consulta all'accesso**. Se lo facesse, chi prova imparerebbe due
cose: che quel valore è in lista, e — dal fatto stesso che il controllo è avvenuto —
che l'utenza esiste. Un rifiuto di politica sulla rotta del cambio password non
consuma il budget dei tentativi di accesso: sbagliare la password attuale è un
tentativo, cercare una password nuova che vada bene no.

#### Codici stabili

| codice | significato |
|---|---|
| `password_too_short` | meno di `MIN_LENGTH` code point normalizzati |
| `password_too_long` | più di `MAX_LENGTH`; rifiutata, non troncata |
| `password_blocklisted` | in lista, o uguale allo username |
| `password_not_encodable` | surrogato spaiato: non codificabile in UTF-8 |
| `password_unchanged` | identica a quella attuale |

Tutti su **422**, non 403: `AuthError` significa «non ti è permesso», e qui il
problema è il valore inviato. Il client deve poter distinguere «rifà il login» da
«scegli un'altra password», e per questo `PasswordRejected` non discende da
`AuthError`.

Nessun messaggio contiene la password rifiutata. Nominano il limite — quanti
caratteri servono — mai il valore: una password rifiutata è comunque un segreto, e
spesso è quella *quasi* giusta di quella persona. Il messaggio della lista non dice
nemmeno **dove** il valore è stato trovato: «compare in una raccolta di credenziali
diffuse» direbbe a chi prova che quel valore è vero da qualche altra parte, e la
stessa persona riusa le password altrove.

#### Due difetti trovati da questa verifica

**Un hash illeggibile faceva sollevare invece di negare.** `verify_password`
intercettava `VerifyMismatchError`, `InvalidHashError` e `ValueError`, ma **non**
`VerificationError`, che è ciò che `argon2-cffi` solleva quando non riesce a
*decodificare* l'hash — corrotto, troncato da una migrazione, di un altro algoritmo.
La rotta di accesso rispondeva 503 invece di negare l'accesso, e l'eccezione
arrivava **prima** della registrazione del tentativo, quindi quei tentativi non
venivano contati dal limitatore. Ora tutte e tre le famiglie sono intercettate e la
risposta è «no»: un hash che non si può leggere non autentica nessuno.

**Un surrogato spaiato in una password nuova.** `hash_password` sollevava
`UnicodeEncodeError`, che nessuno mappa e che diventa un 503 per un dato ricevuto. E
se un hash fosse comunque nato, l'utenza sarebbe stata **inaccessibile per sempre**:
`verify_password` intercetta `ValueError`, di cui `UnicodeEncodeError` è sottoclasse,
quindi ogni accesso successivo avrebbe risposto «credenziali errate» senza che
niente fosse errato.

Misurando si è scoperto che su HTTP non arriva: `pydantic-core` è scritto in Rust, e
una `str` di Rust deve essere UTF-8 valido, quindi la validazione del corpo rifiuta
prima con il 422 generico `invalid_body`. La strada in cui il controllo serve
davvero è un'altra e non passa da pydantic: `os.environ` decodifica i byte
dell'ambiente con `surrogateescape`, quindi una `TSM_BOOTSTRAP_PASSWORD` che
contenga un byte non valido in UTF-8 — un testo Latin-1 incollato in un file di unit
systemd — **diventa** una stringa con surrogati spaiati. Senza il controllo,
`bootstrap.py` moriva con un traceback; con il controllo dice che cosa non va.

#### Il NUL non è un caso speciale, e non si riusa la regola dell'istantanea

Misurato: per Argon2 un NUL dentro una password è **innocuo** e **non tronca** — due
password che differiscono solo dopo il NUL non si verificano a vicenda, a differenza
di qualche implementazione C di bcrypt.

Quindi qui **non** si riusa `is_representable_text` (§8.16), pur essendo la
domanda apparentemente la stessa. Quella regola chiede se PostgreSQL conserva una
stringa in `text`/`jsonb` e comprende il NUL; una password non finisce in nessuna
colonna — ci finisce il suo **hash**, che è ASCII. Riusare la regola
dell'istantanea rifiuterebbe password legittime per un motivo che qui non esiste, ed
è il modo in cui una regola condivisa a torto fa danni. Le due parti in comune —
i surrogati — sono coperte da una regola narrativamente separata e con un codice
proprio.

#### Bootstrap di produzione: che cosa sarà, e che cosa non è ancora

Il database di produzione finale **non** nascerà con `admin / admin`.
L'inizializzazione finale creerà l'amministratore di bootstrap, genererà una
password provvisoria da CSPRNG, imposterà `must_change_pw`, mostrerà la provvisoria
**una volta sola** a chi installa, conserverà soltanto il suo hash Argon2id, e
imporrà una password nuova al primo accesso. È già il comportamento di
`scripts/bootstrap.py`: la password si legge da `TSM_BOOTSTRAP_PASSWORD` oppure si
genera, in entrambi i casi viene **validata** contro la politica, e un valore debole
fa fallire il bootstrap con un codice invece di creare il primo amministratore con
una password che qualcuno indovina.

La **purga e il bootstrap dei dati di produzione non si fanno in questo commit**:
restano un cancello di rilascio finale, dopo la fase 2.

### 8.43.1 Come è verificato

`tests/test_passwords.py` (puro) prova la **regola**: la scelta esplicita di
Argon2id, i parametri contro soglie minime tenute in costanti **separate** — se il
test confrontasse i valori con se stessi, abbassarli resterebbe verde — i confini
14/15/128/129, l'assenza di regole di composizione, le due forme Unicode canoniche,
il NUL che non tronca, il surrogato, la lista, e il sale: la stessa password produce
cento hash con cento sali diversi, tutti verificabili.

`tests/test_password_policy_pg.py` prova le **strade**, su PostgreSQL reale: che
ogni percorso che stabilisce una password passi dalla politica (cambio proprio,
creazione da amministratore, reimpostazione, creazione di servizio); che un rifiuto
non lasci **niente** — non un hash cambiato, non una sessione revocata, non una riga
di audit; che tre sessioni contemporanee cadano insieme; che un hash storico più
debole venga riscritto dopo un accesso riuscito e **una sola volta**; e che la
provvisoria non compaia in audit, log, risposte o elenchi. Quest'ultimo si verifica
cercando il valore in **tutte** le colonne testuali di **tutte** le tabelle — con un
controllo di sanità che scrive di proposito un marcatore e pretende che la stessa
ricerca lo trovi, perché una ricerca scritta male e un database pulito danno lo
stesso risultato.

La §13 di `tools/storage-config-test.py` copre il **negativo**, che nessun test può
coprire: che nessun altro punto costruisca un `PasswordHasher` con parametri propri,
che l'accesso non consulti la lista, che il sale non sia scelto a mano, che non
esistano pepper o segreti d'ambiente nel calcolo, che il numero della lunghezza
minima non sia duplicato nella rotta, e che il suggerimento del frontend coincida
con la politica. Sei di questi controlli sono stati verificati **mutando di proposito
il codice** per accertarsi che sappiano fallire — e quattro di essi, alla prima
stesura, passavano per il motivo sbagliato: cercavano una sottostringa che risponde a
una domanda diversa (`.strip()` che appartiene alla lettura del file della lista,
`password` che compare in `temporaryPassword`, `min_length` che appartiene allo
username, e un letterale con le virgolette che `ast.unparse` normalizza).

### 8.44 Fase 2C: scrittura doppia atomica

Dalla fase 2C ogni salvataggio mantiene **due** rappresentazioni nella stessa
transazione:

1. l'**istantanea JSONB immutabile** con la sua storia, che resta l'unica fonte di
   verità e l'unica che `GET` legge;
2. la **proiezione relazionale** dello stato corrente, che nessuno legge ancora.

#### L'invariante

Dopo ogni `PUT` che cambia qualcosa:

```
projection_state.head_version == inventory_head.version
projection_state.head_sha256  == inventory_versions.canonical_sha256 (della testa)
canonicalise(assemble(proiezione)) == documento immutabile in testa
digest(assemble(proiezione))       == canonical_sha256 della testa
```

Una transazione che non può dimostrarle tutte e quattro **si annulla per intero**.
Non esiste uno stato committato in cui la testa JSON è avanzata e la proiezione no,
né il contrario.

Il meccanismo è il **rollback**, non l'ordine degli statement. L'ordine serve a dare
messaggi d'errore sensati e a rispettare le dipendenze di chiave esterna; la garanzia
sta nel fatto che qualunque passo può sollevare e portarsi via tutto. È la stessa
distinzione già fatta per la riga di stato nella 2B, e vale la pena ripeterla perché è
il punto su cui si sbaglia: leggere una sequenza di statement e concludere «qui è
sicuro» è un ragionamento che si rompe al primo riordino.

#### L'ordine della transazione

Fuori dal database (rifiutare un documento malformato non deve prendere un lock):
schema, radici congelate, segreti e foto, rappresentabilità numerica (§8.16),
rappresentabilità testuale e delle chiavi, canonicalizzazione, limiti di dimensione.

Dentro **una** transazione:

1. lock di `inventory_head` con `SELECT … FOR UPDATE`;
2. lettura di versione, documento e digest **registrato** della testa;
3. **la proiezione deve già rispecchiare quella testa**, o si rifiuta;
4. no-op canonico: digest uguale → `changed=false`, nessuna scrittura;
5. confronto con `baseVersion`;
6. transizione di identità;
7. eventi di dominio deterministici;
8. autorizzazione dell'insieme completo;
9. esistenza delle foto referenziate;
10. modello relazionale del candidato e sua coerenza;
11. inserimento dell'istantanea immutabile, con la versione generata dal database;
12. **sincronizzazione** della proiezione al modello del candidato;
13. riga di stato con versione, digest, versione della mappa e metadati di radice;
14. **rilettura da SQL**;
15. modello riletto == modello scritto; modello riletto coerente; documento
    riassemblato == istantanea; digest == digest dell'istantanea;
16. `inventory_photo_refs` per la versione nuova;
17. audit;
18. avanzamento della testa;
19. commit.

Il passo 3 sta **prima** del no-op, e non è un dettaglio: un no-op è una risposta di
*successo*, e restituirla mentre la proiezione è vecchia direbbe al client «tutto in
ordine» da un backend che ha smesso di mantenere una delle due rappresentazioni. Se
la richiesta ripetuta arrivasse proprio in quel momento, il difetto resterebbe
invisibile esattamente al cliente che sta riprovando.

#### Precondizione: si fallisce chiuso, e non ci si cura da soli

La 2B ammetteva deliberatamente una proiezione vecchia. La 2C no. Se la proiezione è
assente, vecchia di versione, vecchia di digest, o scritta da una versione della mappa
diversa, ogni salvataggio è rifiutato con **`projection_not_current`** → **503**.

Il rimedio è esplicito: `project.py --rebuild`, come proprietario dello schema.
L'API **non si ripara da sola**, e la ragione non è la sincronizzazione — che
produrrebbe comunque lo stato giusto, essendo una sostituzione integrale. È che
finché la proiezione non rispecchia la testa, l'applicazione **non sta facendo quello
che dichiara**; ripararlo di nascosto, al primo salvataggio di un utente qualunque,
farebbe passare il sistema da «disallineato e visibile» ad «allineato», cancellando
ogni traccia del fatto che per un certo tempo non lo era — e con essa l'unica
occasione di chiedersi perché. Un disallineamento ha una causa: una migrazione a metà,
una scrittura fuori dall'API, un `--rebuild` mai eseguito, un ripristino parziale da
backup.

#### Sostituzione integrale, non differenza incrementale

La sincronizzazione **svuota e riscrive**. È la scelta più difficile da sbagliare, e a
questa scala non costa niente di misurabile: il seed reale è di 197 righe.

- **Produce per costruzione lo stato del candidato.** Una differenza incrementale
  produce «lo stato precedente più le modifiche che ho saputo calcolare», che è la
  stessa cosa solo se il calcolo è completo. Aggiunte, rimozioni, aggiornamenti,
  ridenominazioni, spostamenti fra genitori, riordini e
  ridenominazione-più-spostamento nello stesso `PUT` sono sei occasioni di sbagliare
  che così non esistono.
- **Rende innocui gli scambi di chiavi ambito.** Due rack che si scambiano il `code`
  nella stessa sala violerebbero `uq_rack_code` a metà di un `UPDATE` incrementale —
  il vincolo è `DEFERRABLE INITIALLY IMMEDIATE` proprio per sopravvivere a quel
  momento. Cancellando prima e inserendo dopo il conflitto non nasce: non serve
  appoggiarsi al rinvio, e non serve ricordarsi che servirebbe.
- **`DELETE`, non `TRUNCATE`.** `TRUNCATE` prende un lock `ACCESS EXCLUSIVE` che
  bloccherebbe anche i lettori — oggi non ce ne sono, la 2D ne avrà — e richiede un
  privilegio che non si è concesso. La cascata dai siti porta via sale, rack e
  dispositivi; le voci di manuale non hanno genitore.

Il costo è righe morte a ogni salvataggio: a duecento righe, lavoro di autovacuum
invisibile. Se un giorno fossero centomila, `synchronise` è il posto dove cambiare
strategia — e i test la interrogano dal **comportamento**, non
dall'implementazione, quindi resterebbero validi.

#### Una sola verifica per i due scrittori

`synchronise` è un corpo solo, usato dal salvataggio e da `rebuild`. Se il
salvataggio avesse una verifica propria, copiata dalla ricostruzione, il giorno in cui
una delle due si irrobustisce l'altra resterebbe indietro — e sarebbe quella sul
percorso delle richieste, cioè quella che protegge i dati degli utenti invece di un
comando che un sistemista lancia a mano.

#### `mapper_version`: il guasto che il digest non vede

La proiezione è una rappresentazione **derivata**, e una derivata è valida solo
rispetto al codice che l'ha prodotta. Se domani un campo passasse da `extra` a una
colonna tipizzata, le righe già scritte riassemblerebbero **lo stesso documento** —
quindi lo stesso digest, quindi nessun allarme dal confronto — mentre le query per cui
la colonna esiste non troverebbero niente. Il confronto dei digest è cieco esattamente
lì.

`inventory_projection_state.mapper_version` registra quale mappa ha scritto la
proiezione; il salvataggio e la readiness pretendono che sia quella corrente. Si
incrementa quando cambia la **distribuzione** dei dati fra le colonne, non quando si
aggiunge un test o si rinomina una variabile.

La colonna nasce **NULL e senza default**. Le righe della 2B non dichiarano nessuna
mappa, e noi non sappiamo quale le ha scritte: lo sappiamo per deduzione — ce n'è
stata una sola — ma «per deduzione» non è un dato. Scrivere `1` al loro posto
significherebbe inventare un'informazione che nessuno ha registrato per far tornare un
controllo. NULL è la verità, e fa fallire chiuso: la proiezione va ricostruita, che è
il passo di attivazione previsto.

#### Il bootstrap proietta anche lui

`repository.bootstrap` scrive entrambe le rappresentazioni come un salvataggio. È una
**deviazione dalla specifica**, decisa e riportata: senza, un database appena
inizializzato avrebbe una testa e nessuna proiezione, e il primo `PUT` di un utente
riceverebbe 503 finché qualcuno non eseguisse `--rebuild` a mano. Un'installazione
nuova nascerebbe rotta e il rimedio sarebbe un passo che nessuno ha motivo di
sospettare. Il bootstrap gira già come proprietario dello schema, quindi non chiede
nessun privilegio nuovo.

#### Privilegi (migrazione 0012)

La 0010 aveva **negato** la scrittura ai ruoli di runtime scrivendo perché: «i
privilegi di scrittura li concede la fase 2C, con il codice che li usa». Questa è
quella migrazione.

| | `tsm_api` | `tsm_worker` |
|---|---|---|
| tabelle della proiezione | `SELECT, INSERT, UPDATE, DELETE` | `SELECT` |
| stato della proiezione | `SELECT, INSERT, UPDATE, DELETE` | `SELECT` |
| `TRUNCATE` su entrambe | **no** | **no** |
| `inventory_versions` | `SELECT, INSERT` — mai `UPDATE`, mai `DELETE` | `SELECT` |
| `audit` | append-only | append-only |
| `photos` | `SELECT, INSERT` — mai `UPDATE`/`DELETE` dei byte | `SELECT, DELETE` (GC) |
| tabelle del worker | nessun accesso | come prima |

`inventory_versions` resta immutabile, e in fase 2C acquista un **secondo mestiere**:
è il riferimento contro cui la proiezione si verifica. Poterla riscrivere renderebbe
quella verifica una tautologia.

Il worker resta in sola lettura: le colonne data derivate esistono per le query, e il
passaggio dello scanner è una decisione successiva con i suoi test, non un effetto
collaterale di questo commit.

#### Foto: stato corrente e raggiungibilità storica sono due cose

`inventory_racks.photo_id` è la foto **corrente** e cambia con lo stato.
`inventory_photo_refs` sono le dipendenze **storiche**, una riga per versione, e sono
ciò che la GC guarda.

```
v20 → FOTO_A          racks.photo_id = FOTO_B
v21 → FOTO_B          inventory_photo_refs: v20→A, v21→B  →  la GC conserva A
```

Appiattirli renderebbe la foto di una versione storica cancellabile appena il rack ne
monta un'altra, e un rollback a quella versione mostrerebbe un riquadro rotto per
sempre.

**Conseguenza scoperta popolando le tabelle:** `inventory_racks.photo_id` è una
seconda chiave esterna verso `photos`, quindi il database ora **rifiuta** di cancellare
una foto che lo stato corrente usa — anche al proprietario dello schema. Non è un
ostacolo: è una difesa in più che regge se la query della GC venisse riscritta male, e
non può scattare durante un giro normale, perché ogni foto referenziata dai rack ha per
costruzione una riga in `inventory_photo_refs` per la versione in testa (le due
scritture stanno nella stessa transazione). Ha però un effetto pratico: le fixture che
azzerano lo stato devono svuotare la proiezione **prima** di cancellare le foto.

#### Colonne data derivate

`garanzia`/`supporto` in testo restano **autoritative** per la ricostruzione del
documento; `garanzia_date`/`supporto_date` restano derivate e solo per le query. Si
popolano col parser dello scanner delle scadenze — non un secondo parser, che
divergerebbe sui casi limite, che sono i valori che l'inventario reale contiene. Un
valore non interpretabile lascia la colonna a `NULL` e **conserva** il testo: la fase 2
non è più severa della fase 1.

`validate_model` continua a vedere una data derivata sbagliata, ed è l'unico controllo
che possa: il digest è cieco a queste colonne, perché non tornano nel documento.

Un test di integrazione confronta i risultati di una query SQL sulle date con
`due_items` calcolato dal documento: se dissentissero, la colonna interrogabile
risponderebbe una cosa e le notifiche un'altra, e nessuna delle due saprebbe di avere
torto.

#### Che cosa NON cambia

- **`GET /api/inventory` legge il JSON.** Il passaggio a SQL è la 2D, e avviene solo
  dopo che il confronto è stato verde ripetutamente su dati veri. Un `GET` che
  assembla male restituisce un documento plausibile, il client lo rimanda con un
  `PUT`, e la differenza diventa una versione nuova con un contenuto che nessuno ha
  scritto.
- I contratti di `GET` e `PUT` sono invariati; il frontend non è stato toccato e non
  sa che la proiezione esista.
- Lo scheduler delle notifiche legge il documento.
- Nessun endpoint di query, nessuna pulizia dei dati di produzione.

#### Readiness

«Pronto» ora vuol dire cinque cose: database raggiungibile, migrazioni al livello
atteso, inventario inizializzato, stato della proiezione presente con una mappa
supportata, e versione/digest uguali a quelli della testa.

Un backend che risponde «pronto» con la proiezione vecchia **mente**: rifiuterà tutte
le scritture. `GET` funzionerebbe ancora — legge il JSON — ed è proprio questo che
renderebbe il guasto difficile da vedere: l'applicazione sembra viva e non si può
salvare.

Si controlla lo **stato**, non la fedeltà: tre confronti fra valori già registrati,
cioè tre query. Riassemblare l'inventario a ogni sonda costerebbe quanto un `--verify`
completo, ripetuto ogni pochi secondi per sempre. La fedeltà la dimostrano la verifica
transazionale dopo ogni scrittura e `project.py --verify`.

#### `project.py` dopo la 2C

`--rebuild` non è più il modo normale di tenere aggiornata la proiezione. Gli restano
due mestieri, entrambi da proprietario: il passo di **attivazione**, e il
**ripristino** dopo un guasto (una scrittura fuori dall'API, un ripristino parziale,
una versione della mappa cambiata da un aggiornamento).

`--verify` resta lo strumento **indipendente**: la verifica automatica dopo ogni
scrittura dimostra che *quel* salvataggio era fedele, non che lo sia ancora oggi. Da
questa fase esce con 1 anche su una proiezione soltanto **vecchia** — in 2B era
normale, adesso è lo stato in cui l'API rifiuta le scritture. Fedeltà e attualità
restano riportate separatamente, perché hanno cause diverse: un difetto del codice
contro un comando mancante.

### 8.44.1 Sequenza di aggiornamento (documentazione, NON si esegue ancora)

Deployment su un host solo con Compose: si preferisce una **finestra di manutenzione
esplicita** invece di fingere che serva la complessità di una migrazione senza
interruzione. Un rolling upgrade con due versioni dell'API attive
contemporaneamente — una che mantiene la proiezione e una che non la conosce —
produrrebbe esattamente lo stato che l'invariante vieta.

1. fermare le scritture dell'applicazione;
2. applicare la migrazione di schema e privilegi (`0012_dual_write`);
3. eseguire `project.py --rebuild` come **proprietario dello schema**;
4. pretendere che `project.py --verify` riesca;
5. avviare l'API della fase 2C;
6. pretendere la readiness verde;
7. riaprire l'accesso.

Il passo 3 non è facoltativo e non è automatizzabile dall'API: la colonna
`mapper_version` nasce NULL, quindi finché non si ricostruisce ogni salvataggio è
rifiutato con `projection_not_current`. È deliberato — vedi la precondizione.

**Non si distribuisce ancora.** Il rilascio in produzione resta bloccato fino a: fase
2C completa, fase 2D completa, funzionalità SQL/query completa, controllo finale di
funzionalità e sicurezza, pulizia e bootstrap del dataset di produzione, e suite
completa verde in ambiente pulito.

### 8.44.2 Come è verificato

`tests/test_dual_write_pg.py` — 101 test su PostgreSQL vero, nessuno saltato:

- **ogni documento** di prova (venticinque: seed di produzione, inventario delle
  scadenze, voci di manuale, `vani` come oggetti-valore, campi ignoti in `extra`,
  valori falsi espliciti, `seriali` di tipi misti, date valide e rotte, numeri ostili,
  interi fuori scala) passa sia dal bootstrap sia da un `PUT` reale, e ogni volta si
  verifica l'invariante **completo** con una funzione sola — averla in un posto è ciò
  che rende impossibile scrivere un test nuovo che ne verifica tre quarti;
- le forme di modifica: aggiunte, rimozioni, ridenominazioni con identità conservata,
  spostamenti fra genitori, riordini, scambio di codici ambito,
  ridenominazione-più-spostamento nello stesso `PUT`, righe non toccate, `id`
  duplicati;
- foto: sostituzione della corrente con conservazione della storica, la GC che
  conserva la foto di una versione vecchia, e l'invariante che rende la chiave esterna
  non attivabile dalla GC;
- date derivate, valori non interpretabili conservati, e la query SQL che concorda con
  `due_items`;
- no-op canonico che non scrive **niente** — compreso il timestamp di
  sincronizzazione, che è l'unica cosa che rivelerebbe una riscrittura con contenuto
  identico — replay idempotente di una risposta persa, conflitto di `baseVersion`;
- la precondizione: stato assente, vecchio di versione, vecchio di digest, mappa
  sbagliata, mappa NULL della 2B, e il controllo che precede il no-op;
- **dodici iniezioni di guasto**, ognuna su una funzione reale del percorso:
  svuotamento, inserimento, riga di stato, rilettura, confronto dei modelli,
  riassemblaggio, validazione, inserimento della versione, audit, testa, riferimenti
  alle foto, e un guasto del **database** (violazione di chiave esterna a metà della
  sincronizzazione). Dopo ognuna si confronta una fotografia di *tutto* — testa,
  versioni, audit, riferimenti storici, tutte le righe della proiezione, riga di
  stato — così un test nuovo non può dimenticare una tabella;
- concorrenza: due salvataggi in parallelo dalla stessa versione, con la prova che il
  perdente riceve un conflitto normale e che lo stato finale è allineato;
- `GET` che serve ancora il JSON, con i **tre digest** che coincidono (risposta,
  riassemblaggio dalla proiezione, digest registrato in testa), e il contratto del
  filo invariato.

Le **17 mutazioni**: nove sul comportamento (il salvataggio che non sincronizza, il
bootstrap che non proietta, la precondizione assente o spostata dopo il no-op, i
confronti disattivati, lo svuotamento rimosso, la versione della mappa ignorata, la
readiness che non guarda) e otto sui controlli statici. Ognuna deve far diventare
rossi i test nominati; cento test verdi non dimostrano che la scrittura doppia
funzioni, dimostrano che i test passano.

La §14 di `tools/storage-config-test.py` copre il negativo: che nessun altro percorso
scriva la proiezione, che `GET` non sia passato a SQL, che lo scanner non sia passato
alle colonne derivate, che la readiness possa chiedere lo stato ma **non** riassemblare,
che la 0012 non conceda `TRUNCATE` né tocchi l'immutabilità delle istantanee.

Una mutazione ha trovato un difetto nelle sonde stesse: i controlli d'ordine usavano
`source.index(...)` nudo, e togliendo `require_current` lo strumento moriva con un
`ValueError` invece di stampare un `[FAIL]` leggibile — l'invariante era violata e chi
leggeva l'output vedeva un guasto della sonda. Ora l'ordine si verifica con un
guardiano di presenza.

### 8.45 Fase 2D: l'inventario si legge da SQL

`GET /api/inventory` non restituisce più `inventory_versions.doc`. Restituisce il
documento **riassemblato dalle tabelle normalizzate**, e lo fa solo dopo aver
dimostrato che quel documento è la versione in testa.

Il modello che ne risulta, e le quattro cose non sono ridondanti:

| | ruolo |
|---|---|
| tabelle normalizzate | stato operativo **corrente**, autorevole |
| `inventory_versions.doc` | storia immutabile, e **giudice** della coerenza |
| `inventory_head` | puntatore alla revisione corrente |
| `inventory_projection_state` | dichiarazione di *quale* testa le tabelle rappresentano |

Il contratto HTTP **non cambia di una virgola**: stesse quattro chiavi
(`version`, `schemaVersion`, `sha256`, `doc`), stesso `Cache-Control: no-store`,
stesse restrizioni di autenticazione e di password provvisoria, nessuna modifica al
frontend. Cambia la fonte, non la forma.

#### Che cosa pretende ogni lettura

Cinque passi, in `projection.current_document`, tutti dentro un solo istante del
database:

1. **la testa**: numero di versione e `canonical_sha256` **registrato**. Non il
   documento — di `inventory_versions` si legge solo metadato;
2. **l'attualità** (`require_current`): la proiezione deve dichiarare esattamente
   quella coppia, con la versione della mappa corrente. Altrimenti 503
   `projection_not_current`;
3. **le righe** (`read_model`): le cinque tabelle più la riga di stato;
4. **la coerenza** (`validate_model`) sul modello riletto, colonne **derivate**
   comprese. Altrimenti 503 `projection_inconsistent`;
5. **il giro completo**: `assemble` più digest, che deve combaciare con la testa **e**
   con ciò che la proiezione dichiara di aver verificato quando è stata scritta.

Il passo 4 non è una ripetizione del passo 5. `garanzia_date` e `supporto_date` non
tornano nel documento: una data interpretata male lascia il documento identico byte per
byte e il digest uguale. È il punto cieco trovato in fase 2B, e il passo 4 è l'unico
posto in cui si vede — anche in lettura, adesso.

Il passo 5 confronta con **due** riferimenti indipendenti. Il passo 2 ha già provato che
i due combaciano, quindi formalmente è ridondante: si scrive esplicito perché se un
giorno l'attualità si allentasse, questo reggerebbe da solo.

#### Perché la verifica completa a ogni `GET`, per ora

A questa scala (~200 entità identificate, 197 righe nel seed reale) costa quanto una
query in più. In cambio, una corruzione manuale di una riga viene scoperta **prima** che
dati di infrastruttura sbagliati arrivino a un utente. Le misure sono più sotto; se un
giorno diventasse misurabilmente costosa, si ottimizza **da misure**, non
preventivamente. Nessuna cache è stata introdotta.

#### Due codici, e perché non uno

| codice | significato | rimedio |
|---|---|---|
| `projection_not_current` | condizione **dichiarata**: la proiezione dice una versione vecchia, o nessuna, o una mappa che non gira più | `project.py --rebuild` |
| `projection_inconsistent` | la dichiarazione è **falsa**: le tabelle riassemblano qualcos'altro | **indagare**, non ricostruire |

Entrambi 503 — la richiesta era valida, è il backend che non è in grado di servirla — e
in nessuno dei due casi esce un documento. I dettagli (digest, `uid`, nomi di campo)
restano nei log: sono frammenti dell'inventario di un cliente.

Appiattirli su un codice solo direbbe a chi opera «esegui `--rebuild`» anche nel secondo
caso, dove una ricostruzione cancellerebbe le prove di una corruzione di cui non si
conosce ancora la causa. Nessun percorso dell'applicazione può produrre il secondo
stato: la fase 2C dimostra il giro completo dentro la transazione di ogni scrittura.
Quindi la causa è **fuori** — una scrittura fatta a mano, un ripristino parziale, un
guasto del supporto — oppure è un difetto della mappa.

#### Nessun ripiego sull'istantanea, e perché è la decisione più importante

La reazione istintiva a «il riassemblaggio non torna» sarebbe restituire il JSON
immutabile: funzionerebbe, l'utente vedrebbe l'inventario giusto, nessuno aprirebbe un
ticket. Ed è esattamente per questo che è vietato — il ripiego nasconde il difetto che
tutta la fase 2 esiste per scoprire, e lo nasconde fino al giorno in cui qualcuno
interrogherà le tabelle per una domanda importante.

C'è anche un guasto peggiore da evitare: servire il documento riassemblato **sbagliato**.
Ha la forma giusta, i campi giusti, i codici giusti — è *plausibile*. Il client lo
rimanda con un `PUT`, e la corruzione diventa una versione nuova firmata da un utente
che non ha modificato niente. Da qui la regola: un `GET` che non può provare il giro
completo non restituisce niente.

È anche l'unica proprietà di questa fase che un test di comportamento **non sa provare**:
un ripiego produce la risposta giusta in tutti i casi normali. Perciò c'è un controllo
statico che verifica, sull'albero sintattico, che nessun gestore d'errore della rotta
`return`-i qualcosa.

#### Lo snapshot coerente, che è la parte che porta il peso

Un `GET` fa sette letture (testa, digest, stato, siti, sale, rack, dispositivi, voci di
manuale, identificativi delle foto). Sotto READ COMMITTED ognuna vede un istante diverso:
un `PUT` che committa nel mezzo produrrebbe un documento fatto di due versioni, o — più
spesso — un 503 «proiezione incoerente» a fronte di attività perfettamente normale. Due
persone che lavorano sullo stesso CED lo producono da sole.

Quindi la lettura gira in una transazione **`REPEATABLE READ, READ ONLY`**, e su una
**connessione dedicata**. La connessione della richiesta non è utilizzabile, per tre
ragioni indipendenti:

- ha già aperto la transazione e l'autenticazione ci ha già eseguito degli statement:
  l'isolamento si dichiara prima del primo statement;
- `READ ONLY` la escluderebbe comunque, perché `resolve_session` **scrive**
  (`last_seen_at`) a ogni richiesta e per progetto (§8.26);
- promuovere *tutte* le richieste a REPEATABLE READ romperebbe il salvataggio: il `PUT`
  si serializza con `SELECT … FOR UPDATE` e traduce il perdente in un 409 pulito; in
  REPEATABLE READ prenderebbe invece un errore di serializzazione del database (§8.11).

Nessun lock sulla testa: una lettura non deve fermare una scrittura. E `READ ONLY` non è
decorativo — è PostgreSQL a rifiutare qualunque scrittura su quella transazione, quindi
un difetto futuro che provasse a scrivere mentre serve una lettura verrebbe fermato dal
database.

#### Due connessioni per un `GET`, e il pool che deve essere due

Costo dichiarato: un `GET` tiene **due connessioni insieme** (una per l'autenticazione,
una per lo snapshot). Da questo segue un difetto che si scopre facendo l'aritmetica, e
che in produzione si presenterebbe soltanto sotto carico.

Con un pool solo è la classica **acquisizione a due fasi**. Capienza predefinita di
SQLAlchemy: `pool_size=5` + `max_overflow=10` = 15. Threadpool di Starlette per gli
endpoint sincroni: 40 lavoratori. Quindici `GET` simultanei prendono tutte e quindici le
connessioni per autenticarsi, poi aspettano tutti la seconda — e a liberarne una dovrebbe
essere qualcuno che è già in attesa. Trenta secondi di blocco, poi il timeout del pool.

Quindi lo snapshot viene da un **engine separato** (`get_read_engine()`), con il suo
pool. Così l'attesa non si chiude in cerchio: i portatori della prima connessione sono al
massimo quanti la capienza del primo pool, e il secondo pool ne ha altrettante. Da cui
l'invariante:

    capienza(pool di lettura) >= capienza(pool delle richieste)

C'è un test che la controlla, e ce n'è un altro che **dimostra lo stallo**: riducendo
entrambi i pool a una connessione, un `GET` riesce con due pool e fallisce con uno.

⚠ Anche questo era sbagliato nella prima stesura del test, e nella direzione peggiore.
Con `require_actor` sostituito da una lambda, `get_connection` non viene risolta affatto
— FastAPI risolve solo le dipendenze che servono — quindi il `GET` teneva una connessione
sola e il test dichiarava che lo stallo non esisteva. Serve una **sessione vera**: è la
catena `require_actor → current_user → get_connection` a tenere la prima connessione.

#### Tre domande, tre costi, e la separazione è voluta

| | che cosa verifica | quanto costa | quando gira |
|---|---|---|---|
| readiness | versione, digest, versione della mappa | tre confronti fra valori registrati | ogni pochi secondi, per sempre |
| `GET /api/inventory` | il giro completo | un riassemblaggio | una volta per richiesta |
| `project.py --verify` | il giro completo, su richiesta | un riassemblaggio | quando una persona lo chiede |

La conseguenza va detta esplicitamente, perché sembra una lacuna e non lo è: **una
colonna corrotta a mano lascia la readiness verde** e fa cadere il `GET`. La readiness
guarda ciò che è dichiarato; la fedeltà la verifica ogni `GET` per conto proprio.

E `project.py` resta **indipendente dalla rotta**. La tentazione sarebbe far diventare
`--verify` una chiamata a `GET /api/inventory`, che ormai fa la stessa verifica: sarebbe
meno codice e sarebbe sbagliato. Uno strumento diagnostico che dipende dal servizio che
deve diagnosticare non si può usare nel guasto che conta.

L'invariante operativa da controllare dopo un aggiornamento:

    `GET` risponde 200  ∧  `--verify` esce 0  ∧  GET.sha256 == digest della testa

#### Il ripristino di una versione precedente

Non esiste un percorso di ripristino, e non deve esistere: si rilegge il documento
storico e si **salva**. Passa dal salvataggio normale, quindi eredita gratuitamente
l'invariante della scrittura doppia, la validazione dell'identità, l'audit e i
riferimenti alle foto — e non c'è una seconda implementazione che possa restare
indietro.

Un ripristino crea una versione **nuova**: le righe storiche non si toccano mai. La foto
della versione abbandonata resta protetta dai suoi riferimenti storici anche quando lo
stato corrente non la monta più — confondere la foto corrente
(`inventory_racks.photo_id`) con la raggiungibilità storica (`inventory_photo_refs`)
farebbe cancellare la foto di una versione passata appena il rack ne monta un'altra
(§8.5).

#### Una correzione a `--verify`

`verify` confrontava la proiezione col digest **registrato** nella versione, senza
verificare che quel digest descrivesse ancora il documento che gli sta accanto. Era un
buco preciso: un `UPDATE` a mano su `inventory_versions.doc` che lasciasse intatto il
digest passava la verifica — l'imputato assolto perché il giudice era stato corrotto.
`rebuild` quel controllo lo faceva da sempre. Dalla fase 2D conta di più, perché
l'istantanea non è solo storia: è il riferimento contro cui ogni `GET` si misura.

#### Che cosa la fase 2D NON fa

Endpoint di ricerca SQL, endpoint di capacità, endpoint di scadenze su SQL, lettura del
worker delle notifiche dalle colonne derivate, pulizia dei dati di produzione, bootstrap
finale, deployment. Lo scanner delle scadenze continua a leggere il **documento**, e c'è
un test che lo dimostra corrompendo le colonne derivate e verificando che trovi ancora le
stesse scadenze.

### 8.45.1 Prestazioni misurate

Percorso completo — client HTTPS → nginx/TLS → FastAPI → PostgreSQL → assemblaggio
relazionale → canonicalizzazione e digest → risposta JSON. Stack Compose reale su un
host solo, `uvicorn` con **un** lavoratore (come in produzione), trenta letture per il
seed e venti per ciascuna scala sintetica.

| scala | righe | documento | mediana | p95 | min–max |
|---|---|---|---|---|---|
| **seed di produzione** | 197 | 42 KiB | **39 ms** | 44 ms | 26–50 ms |
| ×5 | 985 | 212 KiB | 99 ms | 123 ms | 74–150 ms |
| ×15 | 2 955 | 636 KiB | 265 ms | 343 ms | 228–413 ms |
| ×40 | 7 880 | 1 695 KiB | 1 024 ms | 1 357 ms | 920–3 406 ms |

Alla scala che ci riguarda — il seed vero, 3 siti / 6 sale / 102 rack / 86 dispositivi
— la verifica completa a ogni `GET` costa **39 ms mediani**. Non c'è niente da
ottimizzare, e non si ottimizza: nessuna cache è stata introdotta.

#### Dove vanno i millisecondi (misurato dentro il container, al ×40)

| parte | tempo | quota |
|---|---|---|
| `read_model` — le sette letture SQL | 150 ms | 18% |
| **`validate_model`** | **594 ms** | **70%** |
| `assemble` | 27 ms | 3% |
| digest (canonicalise + dumps + sha256) | 73 ms | 9% |
| totale `current_document` | 820 ms | |

Il costo **non** è SQL e **non** è il digest: è l'82% Python, e dentro quell'82% è
quasi tutto `validate_model`. È l'informazione che serve il giorno in cui servisse —
ottimizzare il digest o le query darebbe il 27% nel caso migliore.

#### Perché la concorrenza non aiuta, misurato

Dieci `GET` in parallelo al ×40: **9,9 s** in totale contro 1,2 s per una lettura
sola, cioè 8,2× — praticamente nessun parallelismo. La causa **non** è la
serializzazione per sessione, che era la prima ipotesi: dieci letture da dieci
sessioni diverse danno lo stesso numero di dieci letture dalla stessa (9,9 s contro
9,5 s). È il GIL, con un lavoratore `uvicorn` e un carico all'82% Python.

Alla scala di produzione la stessa misura non è un problema (39 ms per lettura), e
per un CED con una manciata di operatori non lo diventa. Se un giorno lo diventasse,
la leva è `--workers` — non una cache.

#### Il termine quadratico in `validate_model`

Misurato, non dedotto. `_check_collection` viene invocata per ogni rack con
`[d for d in model.devices if d.rack_uid == K.uid]`: una scansione di **tutti** i
dispositivi per **ogni** rack, e lo stesso per le sale sui rack. È un termine
O(rack × dispositivi).

La prova è che a **parità di righe** il tempo dipende dalla FORMA:

| forma | righe | `validate_model` |
|---|---|---|
| 4 rack × 500 dispositivi | 2 004 | 41 ms |
| 20 rack × 100 dispositivi | 2 020 | 40 ms |
| 100 rack × 20 dispositivi | 2 100 | 44 ms |
| 500 rack × 4 dispositivi | 2 500 | 68 ms |
| 2 000 rack × 1 dispositivo | 4 000 | 157 ms |

Le righe raddoppiano, il tempo quadruplica: il prodotto `rack × dispositivi` passa da
8 000 a 4 000 000. Con un dispositivo per rack l'esponente misurato sulle righe sale
da 1,16 (250 rack) a 1,50 (2 000 rack), cioè il termine quadratico prende il
sopravvento.

**Non si ottimizza ora**, e non è una svista:

- alla scala di produzione il prodotto è 102 × 86 = 8 772, cioè il caso da 40 ms della
  tabella qui sopra, dentro un `GET` da 39 ms totali. Non c'è un problema da risolvere;
- non è codice della 2D. `validate_model` è della 2B (§8.42) e il percorso di
  **scrittura** lo paga già a ogni `PUT`: ottimizzarlo qui non sarebbe una modifica
  del percorso di lettura, sarebbe una modifica di una funzione condivisa e provata,
  fatta senza un problema che la motivi.

Se un giorno servisse, la correzione è locale e ovvia: indicizzare i figli per
genitore una volta (`dict` da `rack_uid` a lista) invece di riscandire. Sta scritto
qui perché il giorno in cui qualcuno misurerà di nuovo, questa è la prima cosa da
guardare — e non la si deve riscoprire.

### 8.45.2 Come è verificato

`tests/test_get_from_sql_pg.py` — 66 test su PostgreSQL vero, nessuno saltato:

- **il meccanismo**: si chiede a PostgreSQL in che isolamento sta girando
  (`SHOW transaction_isolation`, `SHOW transaction_read_only`), si prova a scrivere e si
  pretende che il **database** rifiuti, e si verifica che la connessione tornata nel pool
  non resti di sola lettura — se lo restasse, un `PUT` fallirebbe a intermittenza in
  produzione con un errore che non nomina la causa;
- **il pool**: l'invariante di capienza, e la dimostrazione dello stallo con entrambi i
  pool ridotti a una connessione — con due pool il `GET` riesce, con uno risponde 503
  `unavailable` (esaurimento di risorse, non un problema dei dati: la distinzione conta
  per chi legge i log);
- **ogni documento** di prova (ventiquattro: seed di produzione, scadenze, voci di
  manuale, geometria di sala con vani e porte, campi ignoti in `extra`, valori falsi
  espliciti, `foto: null` esplicito, `seriali` di tipi misti, date rotte, enum fuori
  vocabolario, `id` duplicati nello stesso rack, codici scambiati, riordini, numeri
  ostili, interi fuori scala) viene servito e confrontato **byte per byte** con
  l'istantanea;
- **la prova diretta della fase**: si manomette `inventory_versions.doc` lasciando
  intatto il suo digest, e il `GET` restituisce il nome **vero** — quello delle tabelle.
  Se leggesse l'istantanea, restituirebbe la manomissione;
- **il giro del client**: leggere e risalvare lo stesso documento è un no-op, su tutte le
  fixture. Se il riassemblaggio differisse anche in un solo campo, ogni apertura di
  pagina creerebbe una versione nuova con un contenuto che nessuno ha scritto;
- **la precondizione**: stato assente, vecchio di versione, vecchio di digest, mappa
  non supportata, mappa `NULL` della 2B, inventario mai inizializzato (che ha il suo
  codice, `not_bootstrapped`), e il rimedio che ripristina la lettura;
- **la corruzione manuale**, da proprietario dello schema: colonna tipizzata, `extra`,
  ordinale (scambiato in un `UPDATE` unico, perché il vincolo è
  `DEFERRABLE INITIALLY IMMEDIATE`), genitore, metadati di radice, `root_extra`, riga
  cancellata, e una proiezione **interamente** di un altro documento;
- **il punto cieco**, con la dimostrazione e non con l'affermazione: si corrompe
  `garanzia_date`, si **verifica che il digest sia ancora quello della testa** — cioè che
  il controllo dei digest sia davvero cieco a questa corruzione — e solo dopo si pretende
  il 503. Senza il primo passo il test potrebbe passare perché la corruzione ha cambiato
  il documento, cioè provando qualcos'altro;
- **la concorrenza**: il `GET` fermato fra la lettura della testa e quella delle tabelle
  mentre un altro utente committa la versione N+1, con la pretesa di un documento N
  completo e coerente; la prova che il `PUT` **committa dentro la pausa** (se il `GET` lo
  bloccasse, il test andrebbe in stallo); e venticinque letture sotto scritture continue,
  ognuna coerente con sé stessa;
- **il ripristino**: v1 con FOTO_A, v2 con FOTO_B, v3 un'altra modifica, ripristino a v1
  → nasce v4, la proiezione porta FOTO_A, la storia è intatta e FOTO_B resta protetta;
- **il contratto**: 401 senza autenticazione (con l'inventario *non* inizializzato, così
  un ordine invertito rivelerebbe lo stato del servizio a chi non è autenticato), 403 con
  password provvisoria — esercitando la dipendenza vera e non un doppio — le quattro
  chiavi, i tipi, `Cache-Control: no-store`;
- **la separazione dei costi**: la readiness resta verde su una colonna corrotta mentre
  il `GET` cade. Se diventasse 503 anche lì, avrebbe cominciato a fare il lavoro del
  `GET` a ogni sonda.

Le **18 mutazioni**: dodici sul comportamento (il `GET` che torna a leggere l'istantanea,
la precondizione non pretesa, il confronto del digest disattivato, la coerenza del
modello non controllata, l'isolamento a READ COMMITTED, la sola lettura non dichiarata,
un ripiego sull'istantanea aggiunto, la readiness che riassembla, `--verify` che non
controlla l'oracolo, la scrittura doppia interrotta, lo snapshot che torna al pool
delle richieste, i due engine che diventano lo stesso oggetto) e sei sui controlli
statici.

Una mutazione ha trovato un difetto in un controllo statico: cercare
`projection.current_document` nella **rotta** restava verde quando la funzione veniva
rinominata, perché la rotta continuava a nominare qualcosa che non c'era più. Ora il
controllo pretende anche che la funzione esista. È lo stesso difetto del guardiano di
presenza in `ordered()`, trovato allo stesso modo: mutando.

E una l'ha trovato in un test: la sostituzione di `read_model` per fermare il `GET` a
metà veniva rieseguita dal `PUT` concorrente (che passa da `synchronise`, che chiama
`read_model`), quindi ogni scrittore ne avviava un altro e il secondo restava in attesa
del lock del primo — stallo, e un 503 generico che accusava il codice invece del test.

⚠ E lo strumento delle mutazioni ha avuto **quattro** difetti propri, tutti nella
direzione peggiore — «le protezioni non funzionano» quando funzionavano: leggeva il
codice di uscita di `tail` invece di quello di `pytest` (una pipe restituisce l'esito
dell'ultimo comando); confrontava i nomi dei test per uguaglianza mentre `--tb=no` vi
accoda il motivo; li confrontava a 80 colonne, dove pytest li tronca; e cercava
«passed» in un riepilogo che pytest 9.1 **non stampa** quando tutto passa. Da qui la
passata di **controllo** senza mutazioni, che adesso lo strumento esegue per primo: una
sonda che non sa vedere il verde non ha nessun diritto di dichiarare il rosso, e questa
non lo sapeva.

La §15 di `tools/storage-config-test.py` copre il negativo: che il `GET` non abbia più
nessuna strada per l'istantanea, che nessun gestore d'errore restituisca un documento,
che lo snapshot sia dichiarato `REPEATABLE READ, READ ONLY`, che la connessione della
richiesta **non** sia stata promossa, che l'attore sia risolto prima della fabbrica dello
snapshot, che la readiness non riassembli, che `--verify` non sia «chiama il `GET`», che
il worker e il frontend non sappiano niente della proiezione, e che lo snapshot prenda
la connessione dal pool separato.

Sullo **stack reale** (HTTPS → nginx → API → PostgreSQL), oltre alle misure di §8.45.1:
il digest del seed è rimasto `7fdbf3d8e42c…`; un `PUT` con
`Rack «2D» — letto da SQL 🚀` è tornato identico *dalle tabelle*; manomettendo
`inventory_versions.doc` il `GET` ha restituito il nome vero e `--verify` è uscito 1
con `digest_della_versione_incoerente`; la proiezione svuotata ha dato 503
`projection_not_current` con readiness 503, e una colonna corrotta 503
`projection_inconsistent` con readiness **verde**; cinque `GET` non hanno toccato
`synchronised_at`. Le suite `smoke`, `proxy-security`, le quattro d'interfaccia e la
E2E del browser sono verdi, e dopo le scritture vere della E2E la proiezione ha
seguito fino alla versione 2 da sola.

### 8.46 Fase 2E: interrogazioni SQL sulla proiezione

Tre endpoint di sola lettura sopra le tabelle normalizzate:

    GET /api/inventory/search     ?q=&limit=&cursor=
    GET /api/inventory/capacity
    GET /api/inventory/expiries   ?warningDays=&limit=&cursor=

Il principio che governa tutto il resto: **rendere interrogabili in SQL i concetti che
l'applicazione ha già, senza cambiarne il significato**. Il riferimento semantico è il
frontend per ricerca e capacità, lo scanner delle scadenze per l'interpretazione delle
date. PostgreSQL sa fare molte cose che il JavaScript non fa; usarle cambierebbe il
risultato, e il risultato è il prodotto.

`GET /api/inventory` **resta**, e resta il percorso di fedeltà completa. Il frontend non
è stato ricablato (§18 del requisito): la 2E prova le implementazioni sul server, e la
sostituzione dei calcoli lato client è un commit successivo e delimitato. Il worker delle
notifiche continua a leggere il documento (§19).

#### 8.46.1 Le semantiche legacy scoperte

Questa è la parte che vale più del codice. Ogni voce è stata estratta leggendo
l'implementazione che gira, e ogni voce è coperta da fixture di parità.

**Ricerca — la barra globale.** `q` viene messo in minuscolo e ripulito, poi:

- se `parseIpQuery(q)` riconosce una forma di rete → si cercano **solo i dispositivi**
  per intervallo di indirizzo. I rack non partecipano affatto, nemmeno uno che si
  chiamasse `10.0.0.1`: il codice è `if (!ipRange && (rk.id...))`;
- altrimenti → **sottostringa, senza distinzione di maiuscole**, sui dispositivi in
  `name, model, ip, serial, owner` e sui rack in `id, name, seriali[]`.

Le scoperte che contano:

| | |
|---|---|
| i campi del dispositivo sono **cinque** | `id`, `type`, `stato` e `note` **non** sono cercati. Sembra una dimenticanza, ma aggiungerli darebbe più risultati sul server che nel browser |
| un IP **esatto** non è una forma di rete | `parseIpQuery` gestisce CIDR, intervallo e jolly; per `10.0.0.1` restituisce `null`, quindi si cerca come **testo** — e come sottostringa, per cui `10.0.0.1` trova anche `10.0.0.100` |
| `ipToNum` è **solo IPv4** | dotted-quad, ottetti ≤ 255. Un dispositivo con `2001:db8::1` non si trova per rete (si trova per testo) |
| il jolly accetta 1-3 ottetti | `10.*`, `10.0.*`, `10.0.2.*`; non `10.0.2.3.*` |
| CIDR allineato alla rete | `start = floor(base/size)*size`, quindi `10.0.0.5/24` cerca `10.0.0.0-255` |
| la vista Inventario è **un'altra** ricerca | quattordici filtri per colonna, su valori **tradotti** (`Attivo`, `In manutenzione`). Non è questa, e non è stata riprodotta — vedi le deviazioni |

**Capacità — la vista Capacità.** `used_u` **non è** `SUM(h)`. Il frontend costruisce un
vettore di occupazione per rack e conta gli **slot distinti**:

```js
const occ = new Array(rk.u + 1).fill(false);
for (const d of rk.devices) { … for (let k = d.u; k < d.u + (d.h || 1); k++)
                                  if (k <= rk.u) occ[k] = true; }
for (let k = 1; k <= rk.u; k++) { if (occ[k]) { rkUsed++; run = 0; } else { … } }
```

Da cui:

| caso | comportamento |
|---|---|
| due dispositivi **sovrapposti** | gli slot in comune contano **una volta** |
| un dispositivo che **sporge** | tagliato a `rk.u` |
| `h` nullo o `0` | vale 1 (`d.h \|\| 1`) |
| `h` negativo | non occupa niente (il ciclo non parte) |
| slot iniziale ≤ 0 | fuori dal conteggio, che va da 1 a `rk.u` |
| dispositivi **dismessi** | **occupano**: il ramo che dovrebbe escluderli è un blocco **vuoto** (`if (…) {}`) |

⚠ L'ultima riga è la scoperta più imbarazzante e la più importante: la stessa
applicazione **esclude** i dismessi dalle scadenze (con un `continue` vero) e li
**include** nella capacità. È quasi certamente un difetto del frontend; è il
comportamento attuale, ed è stato riprodotto invece di corretto. Correggerlo cambierebbe
un numero mostrato agli utenti, da un commit che dichiara di non cambiare comportamento.

⚠ E il **raggruppamento per fila** usa `rk.row || '—'`, cioè la stringa «—» come
segnaposto per «nessuna fila». Nel seed di produzione esiste un rack la cui fila **è**
«—» (`CS-Q01`): la sentinella collide col dato, e i due finiscono nello stesso gruppo.
Riprodotto, sentinella compresa.

⚠ Esistono **tre** implementazioni di «U occupate» nel frontend, e non concordano:

| dove | formula |
|---|---|
| vista Capacità | slot distinti, tagliati a `rk.u` |
| scheda rack a destra | `SUM(d.h \|\| 1)` — conta due volte le sovrapposizioni |
| export Excel | `SUM(d.h \|\| 1)` — come sopra |

L'endpoint riproduce la **vista Capacità**, perché è la funzione «capacità». Le altre due
restano come sono, e la divergenza è qui perché qualcuno la troverà.

**Scadenze — la vista Scadenze.** Dismessi saltati; garanzia e supporto sono due righe
distinte per lo stesso dispositivo; valori vuoti o non interpretabili **non compaiono** e
non sono un guasto; si ordina per data crescente e **tutti** i livelli compaiono
(scaduto, entro N giorni, futuro). La soglia è la costante 90.

⚠ Qui ci sono **due** implementazioni nel backend stesso, e divergono:

| | vista Scadenze (frontend) | `due_items` (worker) |
|---|---|---|
| interpretazione delle date | `new Date(v)`, permissiva | `parse_expiry`, `YYYY-MM-DD` esatto |
| dispositivi dismessi | saltati | **inclusi** |
| elementi già scaduti | mostrati, livello «scaduta» | **esclusi** |
| istante di riferimento | `Date.now()` | data di calendario nel fuso configurato |
| soglie | 90 fisso | elenco configurabile |

L'endpoint segue la **vista Scadenze** per che cosa restituisce, e `parse_expiry` per
come interpreta le date — perché §10 del requisito impone di usare le colonne derivate, e
quelle colonne le ha scritte `parse_expiry`. Conseguenza misurata: alcune forme che la
vista Scadenze mostra **non** compaiono nella query.

    2027-3-15   2027/03/15   March 15, 2027   2027-03-15T10:00:00Z   2027-03   2027
    2027-02-30  ← V8 non la rifiuta: la ROTOLA al 2 marzo

Otto forme, elencate per esteso in `test_the_date_parsing_divergence_is_exactly_this_set`.
Non si è aggiunto un secondo interprete di date: due idee di «data valida» divergono, e
divergono sui casi limite. Meglio una divergenza **nota** fra due strati che due parser
che si credono d'accordo.

⚠ E `today` è una data di **calendario** nel fuso configurato, non un istante. Il
frontend fa `Math.round((dt - Date.now())/86400000)`, quindi il suo conteggio dipende
dall'ora del giorno e può differire di uno dal nostro. I due coincidono **esattamente**
a mezzanotte locale, che è la condizione sotto cui le fixture li confrontano.

#### 8.46.2 Dove SQL ha richiesto una gestione speciale

**Nessun `inet`, e nessuna colonna derivata nuova.** Il progetto originale prevedeva una
colonna `inet`. Non c'è, per tre ragioni che si tengono insieme:

1. `ipToNum` è IPv4 e rifiuta tutto il resto; `inet` accetterebbe anche IPv6 e le forme
   abbreviate, cioè aggiungerebbe semantica che il prodotto non ha;
2. una colonna derivata nuova cambia la distribuzione dei dati fra colonne, quindi
   obbligherebbe ad alzare `MAPPER_VERSION` — e con essa a una finestra di manutenzione
   con `--rebuild`, per una query che a questa scala funziona con una scansione;
3. l'aritmetica si scrive come **espressione** (`queries.ipnum_sql`) ed è esatta.

**`strpos`, non `LIKE`.** `LIKE` attribuisce un significato a `%` e `_`, che in una
casella di ricerca sono caratteri normali: con `LIKE` una query contenente `%`
troverebbe tutto. C'è un test dedicato.

**Unione di intervalli, non `generate_series`.** Enumerare gli slot sarebbe la traduzione
ovvia della capacità e sarebbe un difetto: `rack.u` è un `integer` senza massimo, e un
`generate_series` su un rack da due miliardi di U produrrebbe due miliardi di righe dentro
una richiesta HTTP. L'unione di intervalli (gaps-and-islands con funzioni finestra) costa
quanto i **dispositivi** e sull'altezza non fa nessuna ipotesi. C'è un test che porta un
rack a 2 000 000 000 U e pretende una risposta in meno di cinque secondi.

**`extra` non si cerca.** Un valore che la mappa non ha potuto mettere in una colonna
tipizzata sta in `extra`, e `validate_model` lo segnala con `carried_verbatim`, il cui
messaggio dice: «quel campo, per questa riga, non risponde a una query». Questo modulo
rispetta quella dichiarazione. Conseguenza misurata: un rack i cui `seriali` contengono un
numero porta l'intero array in `extra`, e quei seriali non si trovano — mentre il
frontend, che fa `String(sn)`, li trova.

**Documenti che il frontend non sa calcolare.** Tre corpora di parità portano `quirks`,
tutti trovati facendo girare il generatore e non leggendo il codice:

| documento | cosa fa il frontend |
|---|---|
| `rack.u` assente | `new Array(NaN)` → **RangeError** |
| `rack.u` = 3 000 000 000 | alloca tre miliardi di elementi → **memoria esaurita** (ha ucciso il generatore) |
| `rack.u` = `"45"` | non solleva: **coerce**, e il totale della sala diventa la stringa `'04545'` |

Là non c'è parità da misurare: si verifica che lo SQL risponda con numeri sensati e si
registra la divergenza. Nota collaterale: `3 000 000 000` non entra nemmeno in un
`integer`, quindi la mappa lo porta in `extra` e per lo SQL quel rack non ha altezza.

**Il documento canonico è il riferimento, non quello grezzo.** La canonicalizzazione non
riordina soltanto: **riempie**. Un dispositivo scritto senza `type` diventa
`type: "altro"`, e i campi assenti diventano stringhe vuote. Il frontend non vede mai un
documento non canonico — li riceve da `GET /api/inventory` — quindi le attese di parità si
calcolano sul canonico. La prima stesura le calcolava sul grezzo, e il test è diventato
rosso su `device.type`: `null` da una parte, `"altro"` dall'altra. Non era un difetto
dello SQL, era un difetto del banco di prova. Da qui la catena a tre passi del generatore.

#### 8.46.3 Indici: **nessuno aggiunto**, e le misure che lo dicono

§13 del requisito chiede di aggiungerli deliberatamente, dichiarando quale query
sostengono, e di non aggiungerne uno solo perché compariva nel documento di progetto.
Quattro candidati, quattro rifiuti motivati:

| candidato | verdetto |
|---|---|
| date di scadenza | **già presenti**: `ix_device_garanzia_date`/`ix_device_supporto_date`, PARZIALI su `IS NOT NULL`, dalla 0011. Il piano li usa |
| chiavi esterne dei genitori | **già presenti**: `ix_device_rack`, `ix_rack_room`, `ix_room_location`, dalla 0010 |
| `lower(name)` per la ricerca | **inutile**: la ricerca è una **sottostringa** e nessun btree la serve. Con l'indice presente il piano resta `Seq Scan` |
| espressione IP | **misurato ma non giustificato**: a 1720 dispositivi porta la ricerca per rete da 14,7 a 8,4 ms. Sei millisecondi su quindici non sono un problema, e §13 lo dice: «a sequential scan that completes in milliseconds is acceptable» |

⚠ E una scoperta sui tre indici che la 0010 aveva creato «per le interrogazioni per cui la
normalizzazione esiste»: `ix_device_code`, `ix_device_ip`, `ix_device_serial` sono btree
sulla colonna grezza, e la ricerca vera è una sottostringa. Per l'endpoint di ricerca
quei tre indici sono **inerti**. Non si rimuovono — fuori scopo, e servirebbero a un
confronto per uguaglianza che un giorno potrebbe esistere — ma non stanno sostenendo la
ricerca che c'è.

Se un giorno servisse, il primo da aggiungere è quello sull'espressione IP, e
l'espressione da indicizzare è `queries.ipnum_sql('ip')`. Un test verifica che sia
indicizzabile, creandolo e distruggendolo, così la via d'uscita non si scopre inesistente
il giorno in cui serve.

#### 8.46.4 Prestazioni misurate

Percorso completo: client HTTPS → nginx/TLS → FastAPI → PostgreSQL. Stack Compose reale,
un lavoratore `uvicorn`, 25-30 richieste per riga, **a statistiche assestate** (vedi
l'avvertenza sotto).

| | seed (197 righe) | ×10 (1 970) | ×30 (5 910) | risposta al ×30 |
|---|---|---|---|---|
| `search` testo (`q=srv`) | **16 ms** | 25 ms | 33 ms | 96 KiB |
| `search` una lettera (`q=a`) | **19 ms** | 27 ms | 39 ms | 78 KiB |
| `search` CIDR `/16` | **18 ms** | 29 ms | 35 ms | 95 KiB |
| `search` jolly `10.*` | **17 ms** | 28 ms | 32 ms | 95 KiB |
| `capacity` | **16 ms** | 35 ms | 84 ms | 615 KiB |
| `expiries` | **16 ms** | 62 ms | 75 ms | 570 KiB |

Alla scala che ci riguarda tutto sta **sotto i 20 ms mediani**, e a trenta volte quella
scala tutto sta sotto i 100 ms. Nessuna cache è stata introdotta e nessuna ottimizzazione
è stata fatta. Il p95 non supera 1,4× la mediana in nessuna riga.

Nota di metodo: le righe del `×30` vanno misurate **su uno stack già assestato**. Una
sonda che scrive 5 910 righe e misura subito dopo misura autoanalyze, non le query —
vedi l'avvertenza qui sotto.

La ripartizione al ×30, misurata dentro il processo dell'API:

| strato | `search` | `capacity` | `expiries` |
|---|---|---|---|
| funzione (SQL + costruzione) | 25 ms | 47 ms | 48 ms |
| validazione del `response_model` | 0,1 ms | 0,0 ms | 0,6 ms |
| codifica JSON | 0,2 ms | 4,5 ms | 2,6 ms |

Pydantic e la codifica JSON sono **rumore**: il costo è la funzione, e dentro la funzione
è SQL. Il resto della latenza end-to-end è trasporto (TLS, nginx, il client).

⚠ **L'avvertenza che vale più della tabella.** Le prime misure davano `capacity` a
459 ms e `search` a 413 ms al ×30 — sette volte i valori qui sopra. La causa non era il
codice: erano misure prese **subito dopo la scrittura di 5 910 righe**, mentre autoanalyze
stava ancora lavorando e i piani si basavano su statistiche vecchie. A statistiche
assestate: 65-84 ms e 37-39 ms. Un `ANALYZE` esplicito a quel punto non cambia più nulla
(65,1 → 64,2 ms), perché autoanalyze aveva già finito.

Lo stesso effetto aveva prodotto una misura da **471 ms** su `q.search` in una sonda
precedente, non riproducibile in un esperimento controllato (14,7 ms). Le tre misure
«misteriose» di questo commit hanno tutte questa spiegazione, ed è stata verificata:
`pg_stat_user_tables` mostrava `last_autoanalyze` già valorizzato quando i numeri sono
tornati normali.

Questo ha una conseguenza operativa reale, e viene dalla fase 2C: `synchronise`
**cancella e reinserisce tutte le righe a ogni salvataggio**, quindi ogni scrittura
grande apre una finestra di qualche secondo in cui le statistiche sono vecchie e i piani
sono peggiori. A 197 righe è invisibile; a 5 910 è un rallentamento transitorio di circa
sette volte. Non si è fatto niente per rimediare — non è un problema alla scala di
produzione, e un `ANALYZE` nel percorso di una richiesta sarebbe una scrittura — ma è il
primo posto da guardare se un giorno qualcuno segnala «a volte è lento dopo un
salvataggio». Il posto naturale dove metterlo, se servisse, è `project.py --rebuild`, che
gira già come proprietario.

Tre misure «misteriose» di questo commit hanno tutte la stessa spiegazione, ed è questa.

#### 8.46.5 Il bilancio delle connessioni (§14 del requisito)

Il conto è in testa a `app/db.py`, dove sta accanto al codice che lo determina:

    per processo   (pool_size + max_overflow) × 2  =  (5 + 10) × 2  =  30
    in totale      30 × lavoratori uvicorn         =  30 × 1        =  30

Nessun terzo pool: le tre interrogazioni usano quello di **lettura** della fase 2D. Più il
worker delle notifiche e il servizio `migrate` quando qualcuno esegue un comando; il
`max_connections` predefinito di PostgreSQL è 100, quindi c'è margine ampio.

⚠ Aumentare i lavoratori `uvicorn` non è una modifica locale: `--workers N` moltiplica per
N **entrambi** i pool, e con N=4 si arriva a 120 connessioni possibili, cioè oltre
`max_connections`. Il guasto che ne segue è «FATAL: sorry, too many clients already» su
richieste qualunque, sotto carico. Chi tocca quel numero deve ricalcolare insieme: i due
`pool_size`/`max_overflow`, `max_connections`, e la memoria della macchina. Le misure non
danno nessuna ragione per aumentarli.

#### 8.46.6 Come è verificato

`tests/test_queries_pg.py` — 142 test su PostgreSQL vero, nessuno saltato. La parte che
porta il peso è la **parità**: le attese non sono scritte a mano, sono calcolate facendo
girare **il JavaScript che gira nel browser**, copiato alla lettera in
`tools/make-query-fixtures.mjs`. Ventinove corpora, ventisei con parità **stretta**.

⚠ Questo rovescia la scelta di `make-identity-fixtures.mjs`, che scrive le attese a mano
di proposito. Là il rischio è che i test verifichino l'implementazione contro sé stessa;
qui il rischio è l'opposto — attese scritte a mano dimostrerebbero che lo SQL corrisponde
alla mia **lettura** del JavaScript, che è precisamente ciò di cui non ci si può fidare in
una migrazione di comportamento. Un controllo statico verifica che le sedici righe
dichiarate «VERBATIM» esistano ancora identiche nell'HTML: se il frontend cambia, il
controllo diventa rosso e le fixture vanno rigenerate.

La catena di generazione è a **tre passi**, e il passo di mezzo esiste per la scoperta
sulla canonicalizzazione:

    1. node   tools/make-query-fixtures.mjs --emit-docs   → _raw.json
    2. python tools/canonicalise-query-docs.py            → _canonical.json
    3. node   tools/make-query-fixtures.mjs               → i 29 corpora

Coperto: i cinque campi cercati e i tre dei rack; maiuscole, sottostringhe a metà parola e
a cavallo di uno spazio, Unicode (`Núñez`, `Ätna`, `città`), id duplicati, campi vuoti;
`%` e `_` come caratteri normali; tutte le forme IP (esatta, CIDR, intervallo con e senza
spazi e invertito, jolly a 1-3 ottetti, `/33`, `/0`, ottetti > 255, IPv6, testo,
indirizzi con spazi attorno); capacità vuota, parziale, piena, multi-U, sovrapposta,
sporgente, slot ≤ 0, `h` nullo/zero/negativo, altezze 1 e 47, dismessi, raggruppamento per
fila con e senza etichetta e con la sentinella; scadenze oggi/ieri/domani/90/91/molto
scadute/molto future, entrambe sullo stesso dispositivo, «in attesa», vuote, assenti,
dismesso contro in-dismissione, id di business duplicati; paginazione che percorre tutto
esattamente una volta; cursore rotto, di un'altra query, di un'altra versione; `limit` e
`warningDays` fuori intervallo; 401, 403, `no-store`, i tre ruoli; il 503 su proiezione
non attuale per tutte e tre le rotte, senza nomi di tabella nella risposta; e la prova che
le query **non** riassemblano il documento (una colonna corrotta fa cadere
`GET /api/inventory` e non le query).

Un test chiude il cerchio sul filtro della parità: la marcatura `isoStrict` del generatore
è una **riscrittura** di `parse_expiry` in JavaScript, e un test Python la confronta con
`parse_expiry` vero su ogni data di ogni corpus. Senza quello, filtrare le righe che il
backend non interpreta sarebbe confrontare lo SQL con sé stesso.

La §16 di `tools/storage-config-test.py` copre il negativo: `strpos` e non `LIKE`, nessun
`to_tsquery`, nessun `inet`, nessun `generate_series`, nessuna colonna derivata nuova,
`MAPPER_VERSION` non alzata, cursore a chiave e non `OFFSET`, nessun terzo engine, le tre
rotte di sola lettura senza parametri che nominino SQL o colonne, il worker non migrato,
il frontend non ricablato, e nessuna migrazione nuova.

### 8.47 Fase 2F: il worker delle notifiche legge la proiezione

Cambia **una cosa sola**: da dove lo scanner delle scadenze prende i dati.

    prima    inventory_versions.doc  →  due_items(doc)                →  promemoria
    dopo     proiezione relazionale  →  candidates.due_items_from_…   →  promemoria
                                                                          ↑
                                                       identico, riga per riga

Tutto ciò che sta a destra della freccia — soglia più urgente, soglie superate, identità
del promemoria, idempotenza durevole, cinque tentativi con backoff, `Message-ID` stabile,
cooldown dopo l'esaurimento, destinatari, `scheduler_runs`, audit — non è stato toccato. È
l'affermazione che questa fase esiste per poter fare, e il modo in cui è provata non è un
test nuovo: sono i **53 test di consegna di `test_worker_pg.py`, passati senza una
modifica**. Riscriverli per accomodare la migrazione avrebbe distrutto l'unica prova
disponibile; il posto dove verificarlo è il `git diff` di quel file, che è vuoto.

A monte, invece, sono comparse tre condizioni che fanno **fallire chiuso**: la proiezione deve
rispecchiare la testa, il modello relazionale deve essere **coerente** (§8.47.4), e
l'inventario non deve essersi mosso sotto il calcolo. In nessuno dei tre casi parte un avviso,
e in nessuno dei tre la giornata è persa.

#### 8.47.1 La semantica di `due_items`, scoperta e conservata

`due_items` è l'oracolo, non la mia lettura di `due_items`. Il confronto si fa chiamando la
funzione vera dentro il test, su sedici corpora per cinque insiemi di finestre: **zero
divergenze di selezione**. Quello che c'era da scoprire:

**La finestra.** `0 <= giorni_rimanenti <= max(warningDays)`, non `giorni == N`. La
disuguaglianza *è* il recupero: una macchina spenta il giorno del promemoria non lo perde.
In SQL diventa `data BETWEEN :oggi AND :oggi + max`, che è anche la sola forma che permette
di interrogare la finestra invece di leggere tutto.

**Gli scaduti sono esclusi** (`giorni < 0`). La vista Scadenze li elenca. Divergenza
deliberata, §8.48.

**I dismessi NON sono esclusi.** `due_items` scorre `walk(doc)` e **non guarda `stato`**: una
macchina dismessa con la garanzia in scadenza ha sempre prodotto un promemoria, e continua a
produrlo. La vista Scadenze invece li salta. È la ragione per cui il worker **non** usa
`GET /api/inventory/expiries`: quell'endpoint riproduce la vista, e usarlo avrebbe cambiato
il prodotto travestendo la modifica da migrazione tecnica.

**Le date le legge chi le ha scritte.** Si usano `garanzia_date` / `supporto_date`, calcolate
da `parse_expiry` — lo stesso parser dello scanner (§8.44, `relational.DERIVED`). Non è una
comodità: interpretare qui il testo grezzo significherebbe due idee di «data valida» nello
stesso processo, e due idee divergono sui casi limite, che sono precisamente quelli che un
inventario compilato a mano produce. Il testo grezzo resta il dato autorevole e non si tocca.

Il parser fa `.strip()`, quindi `«  2026-08-15  »` **è** una data; e valida il calendario,
quindi `2027-02-30` non lo è. Sette forme che il frontend interpreta e il backend no restano
fuori da entrambi i backend, enumerate una per una in un test (§8.46, §8.48).

**Il nome del dispositivo** è `obj.get("name") or obj.get("id") or "(senza nome)"`, poi
`str()`. La falsità è quella di Python: `""`, `0`, `False`, `[]`, `{}` fanno passare al
candidato successivo. Riprodotto in **Python** e non in SQL, e per una ragione precisa: un
`name: 42` non è una stringa, quindi la mappa relazionale lo mette in `extra` e la colonna
è NULL. Guardare solo la colonna avrebbe fatto sparire quel nome e mostrato l'id al suo
posto — una divergenza invisibile in ogni inventario ben formato e visibile in quelli
importati da un foglio di calcolo, cioè quasi tutti. La query restituisce `extra -> 'name'`
accanto alla colonna e la catena si applica in Python, dove viveva.

**Il contesto (sito / sala / rack) sono gli `id`, non i nomi**, perché `walk` compone il
percorso con `f"{L['id']} / {R['id']} / {K['id']} / {V['id']}"` e `_context` lo rispezza.
Conservato, `str(None)` incluso: un sito senza `id` mostrava «None» nell'avviso, e
correggerlo qui cambierebbe il testo di un avviso reale senza che nessuno l'abbia chiesto.

**Id duplicati**: due dispositivi con lo stesso `id` di business e `_uid` diversi restano
due entità di promemoria indipendenti. Anche due *gemelli* con id e nome identici nello
stesso rack: l'identità è l'`_uid`, e un raggruppamento per etichetta perderebbe una riga
senza che nessuno lo noti — quella che scade.

#### 8.47.2 L'unica divergenza voluta: gli id che contengono `/`

Il percorso di `walk` era **una stringa sola**, e `_context` la rispezzava su ogni `/`. Un id
che contiene uno `/` rompeva quel giro:

| | id | vecchio (troncato) | nuovo (dalle JOIN) |
|---|---|---|---|
| rack | `10.0.0.0/24` | rack = `10.0.0.0` | rack = `10.0.0.0/24` |
| sito | `a/b` | sito=`a`, sala=`b`, **rack=`sala-5`** | sito=`a/b`, sala=`sala-5`, rack=`R05` |

Nella seconda riga il campo «rack» dell'avviso conteneva il nome della **sala**: tutte le
parti scalate di un posto. La JOIN ha il valore intero e lo restituisce intero, come la §7
della fase 2F chiede. Riprodurre il troncamento avrebbe voluto dire scrivere codice nuovo il
cui unico scopo è corrompere un valore che il database ha già giusto.

Non cambia **mai** quali scadenze sono dovute — solo come si legge la posizione nel corpo del
messaggio — e i due valori sono fissati fianco a fianco in
`test_a_slash_in_a_code_is_no_longer_truncated`, così la differenza sta in un file di test
invece che in una frase di un rapporto. Registrata in §8.48.

#### 8.47.3 Coerenza: una revisione sola, e il rifiuto di operare

**Lo snapshot.** I candidati si leggono in `REPEATABLE READ, READ ONLY`
(`db.read_snapshot`). La lettura è multipla — testa, stato, quattro tabelle — e sotto READ
COMMITTED un `PUT` che committa nel mezzo darebbe candidati di due versioni con la revisione
di una terza.

⚠ L'isolamento si dichiara adesso in **un posto solo**, `app/db.py`. Fino alla 2E stava nella
dipendenza FastAPI, che era l'unico lettore; dalla 2F i lettori sono due *processi*, e due
dichiarazioni dello stesso isolamento divergono in silenzio — una delle due continuerebbe a
funzionare sotto READ COMMITTED e il difetto comparirebbe solo quando qualcuno salva mentre
qualcun altro legge. Un controllo statico conta le dichiarazioni e pretende che siano una.

**La precondizione.** `require_valid_model` — la stessa funzione che usa il `GET` — fa
QUATTRO passi, nell'ordine in cui costano meno:

1. la testa esiste, e `inventory_versions` ha la sua riga → `NotBootstrappedError`;
2. la proiezione la **dichiara**: versione, digest, versione della mappa → tre query, nessun
   riassemblaggio, `ProjectionNotCurrentError`. Prima di leggere una riga di entità, così il
   caso «proiezione non mantenuta» costa tre query invece di una lettura completa;
3. le **righe**, con `read_model`;
4. la **coerenza del modello**, con `validate_model` → `ProjectionInconsistentError`.

Il passo 4 è quello che la fase 2F ha aggiunto, ed è §8.47.4. Se uno dei quattro fallisce:
**nessun invio**, e nessun ripiego su `inventory_versions.doc`. Il ripiego funzionerebbe,
nessuno aprirebbe un ticket, e coprirebbe esattamente il difetto di coerenza che la fase 2
esiste per scoprire (§8.45).

⚠ I due codici restano **distinti**, e non è cosmetica: `projection_not_current` si ripara
con `--rebuild` ed è quasi sempre operativo (un aggiornamento senza ricostruzione);
`projection_inconsistent` significa che la dichiarazione è FALSA, nessun percorso
dell'applicazione può produrlo, e la risposta giusta è **indagare** — un `UPDATE` a mano, un
ripristino parziale, un guasto del supporto. Schiacciarli in un codice solo manderebbe chi
legge il registro a cercare la causa nel posto sbagliato.

`require_valid_model` è stata **estratta** da `current_document`, che ora la chiama: la
precondizione esiste in un posto e non in due. È lo stesso motivo per cui `require_current_head`
fu estratta nella 2E — una precondizione copiata è una precondizione che un giorno differisce
in una delle copie, e sarà quella che nessuno guarda. Un controllo statico pretende che
entrambi i chiamanti la usino e che nessuno dei due abbia una seconda copia di
`validate_model`.

⚠ E il giro **non si conclude**. La riga di `scheduler_runs` di oggi resta senza
`finished_at`, che è lo stato che `claim_run` sa riprendere: appena la proiezione è riparata,
il tick successivo rifà il giro di oggi. Concluderlo con un esito «non attuale» sarebbe stato
più ordinato da leggere e avrebbe **perso la giornata** — `claim_run` avrebbe risposto
«already_ran_today» fino a mezzanotte.

**La guardia sulla revisione.** I candidati portano `(versione, digest)`; prima di prenotare
promemoria e consegna, la transazione di scrittura ricontrolla che siano ancora quelli
(`candidates.unchanged`). Se l'inventario si è mosso: si abbandona senza mandare e senza
concludere, e il tick successivo ricalcola. Il controllo guarda *anche* l'attualità della
proiezione, perché fra i due momenti può essere partito un `--rebuild`.

⚠ **Non blocca `inventory_head`.** Un `SELECT … FOR UPDATE` qui terrebbe la riga di testa
bloccata per tutta la consegna SMTP — cioè per un timeout di rete — e fermerebbe i
salvataggi di tutti. Resta quindi una finestra fra il controllo e il `commit` dopo l'invio: un
`PUT` che committa lì dentro fa partire un avviso vecchio di una revisione. È il costo
dichiarato di non bloccare, e resta molto più stretto della finestra di prima, quando nessuno
controllava niente. Un test salva **da dentro** la consegna e dimostra che la testa è libera.

**Il ritentativo** ricompone i nomi dalla proiezione, con la stessa chiave `(uid, tipo,
data)`: un promemoria si ricompone solo se quel dispositivo esiste ancora e ha ancora quella
data per quel tipo. `_rebuild_selection` ha adesso **tre** risposte, e la distinzione è
necessaria: lista piena → si compone; lista vuota → i promemoria non hanno più riscontro e la
consegna si chiude; `None` → non si sa, perché la proiezione non è attuale, e il ritentativo
si **rinvia senza consumare un tentativo** (i cinque tentativi esistono per un relay guasto,
non per una proiezione da ricostruire).

#### 8.47.4 Il punto cieco delle colonne derivate, chiuso

Le colonne `garanzia_date` / `supporto_date` sono **derivate**: non tornano nel documento
riassemblato. Da qui la proprietà scomoda:

> azzerare o falsificare una data derivata lascia il documento **identico byte per byte**,
> quindi il digest uguale, la versione ferma e la versione della mappa invariata.

Tutte e tre le condizioni di `require_current` restano soddisfatte. La proiezione si dichiara
attuale, **e lo è** — nel senso che quel controllo può misurare. È il punto cieco trovato
nella fase 2B, e per il worker era il guasto peggiore possibile: la query non trova quella
scadenza, il giro conclude `nothing_due`, il battito dice `healthy`, e **l'avviso non parte
mai senza che niente lo dica**. Un sistema di allerta che non allerta e si dichiara sano.

La prima stesura della 2F non lo chiudeva, e lo registrava come limite. Le due sezioni della
specifica si contraddicevano sul caso — la §12 («il worker gira una volta al giorno, la
correttezza conta più dei millisecondi») contro la §3 («non caricare l'intera proiezione») — e
la contraddizione l'ha risolta il committente: **si valida**. È la scelta giusta, e la
misura qui sotto dice quanto costa.

Quindi, dentro lo **stesso** snapshot del giro: `read_model` e poi `validate_model`, che è
l'unico controllo che guarda le colonne derivate. Se produce ERRORI:

- nessun messaggio SMTP — non si apre nemmeno la connessione;
- nessuna consegna creata;
- nessun promemoria registrato, quindi **nessuna soglia superata**;
- nessun tentativo di consegna consumato;
- il giro di oggi **non concluso**, quindi ripreso dal tick successivo **nello stesso giorno
  locale** appena la proiezione è riparata;
- codice `projection_inconsistent`, distinto da `projection_not_current`.

Gli **avvisi** non bloccano. `garanzia = "in attesa"` è un valore che una persona scrive in
una casella di testo: fermare gli avvisi di scadenza per quello significherebbe che un campo
compilato male su un dispositivo spegne le notifiche di **tutti** gli altri — il modo di
trasformare una difesa in un guasto. Gli avvisi risalgono però fino al chiamante, perché sono
precisamente i valori che spiegano perché una scadenza attesa non compare nel digest.

⚠ Tre forme di corruzione, tutte rifiutate, e la seconda è la peggiore:

| corruzione | effetto senza validazione |
|---|---|
| `garanzia_date` azzerata, testo valido | l'avviso non parte mai |
| `garanzia_date` **falsificata** a un'altra data valida | l'avviso parte per il **giorno sbagliato** |
| `supporto_date` falsificata | idem, sull'altra colonna |

La seconda è la ragione per cui non basta controllare che la data *ci sia*: un avviso con la
data sbagliata è peggio di nessun avviso — manda qualcuno a rinnovare un contratto con tre
anni di anticipo, o a non rinnovarlo affatto.

Il **digest** del documento riassemblato non viene ricalcolato dal worker (lo fa il `GET`, che
deve servirlo). Non serve al caso che questa sezione chiude — il digest è cieco alle colonne
derivate per costruzione — e costerebbe `assemble` più la serializzazione canonica dell'intero
documento. Se un domani si volesse anche quella copertura, il posto è
`require_valid_model` e il costo è misurabile in un pomeriggio.

#### 8.47.5 Forma della query, e gli indici

Due rami uniti da `UNION ALL`, uno per tipo di scadenza:

```sql
SELECT 'garanzia', d.uid, d.garanzia_date, …etichette…
  FROM inventory_devices d
  JOIN inventory_racks k ON k.uid = d.rack_uid
  JOIN inventory_rooms r ON r.uid = k.room_uid
  JOIN inventory_locations l ON l.uid = r.location_uid
 WHERE d.garanzia_date >= :oggi AND d.garanzia_date <= :fino
UNION ALL  -- lo stesso per `supporto`
```

⚠ **Non** `WHERE garanzia_date BETWEEN … OR supporto_date BETWEEN …`. Con l'`OR` PostgreSQL
non può usare nessuno dei due indici parziali e scandisce la tabella; con due rami ne usa uno
per ramo. E l'`UNION ALL` produce già la forma giusta — una riga per (dispositivo, tipo) —
che è ciò che `devices_with_expiries` restituiva col suo ciclo su `EXPIRY_KINDS`.

`JOIN` interne e non `LEFT JOIN`: le tre chiavi esterne sono `NOT NULL`, e un test lo pretende
dallo schema invece di fidarsi del commento — se una diventasse annullabile, una `JOIN` interna
farebbe sparire dei promemoria **in silenzio**, il guasto peggiore per un sistema di avvisi.

**Nessun indice nuovo**, e nessuna migrazione. `ix_device_garanzia_date` e
`ix_device_supporto_date` esistono dalla 0011 e sono **parziali**
(`WHERE … IS NOT NULL`); il pianificatore li scegli già alla scala di produzione.

⚠ Qui la misura mi ha corretto due volte, e vale la pena che resti scritto:

1. avevo previsto una scansione sequenziale alla scala di produzione — «duecento righe stanno
   in una pagina». Vale per un indice sull'**intera** tabella; questi sono parziali, e nel
   seed la grande maggioranza dei dispositivi non ha date, quindi l'indice contiene una
   manciata di voci e leggerlo costa meno che leggere tutte le righe. La `postgresql_where`
   della 0011 non era un dettaglio di forma;
2. il primo test «a scala grande» gonfiava la tabella **conservando le date**, quindi la
   finestra da 7 giorni prendeva ancora un terzo delle righe e il piano restava (con ragione)
   sequenziale. La misura diceva «l'indice non serve», che era vero per quella distribuzione
   e falso in generale. Serve una finestra **selettiva**, non solo una tabella grande.

#### 8.47.6 Prestazioni misurate

Confronto fra i **percorsi completi**: vecchio = leggi `inventory_versions.doc` +
`due_items(doc)`; nuovo = snapshot + `require_valid_model` + interrogazione. Mediana di 15
esecuzioni, seed di produzione con le date iniettate, inventario ingrandito **documento
compreso** perché altrimenti il confronto non sarebbe alla stessa scala.

| scala | dispositivi | documento | candidati | VECCHIO | NUOVO |
|---|---|---|---|---|---|
| produzione | 86 | 15 KiB | 21 | 2,2 ms | **14,0 ms** |
| ×10 | 860 | 131 KiB | 63 | 14,6 ms | **101,7 ms** |
| ×30 | 2 580 | 385 KiB | 63 | 43,1 ms | **423,2 ms** |

⚠ **Va detto senza attenuazioni: il percorso nuovo è più lento del vecchio a ogni scala, e
il divario cresce.** La versione senza validazione era piatta (≈3,5 ms a ogni scala) e più
veloce del vecchio da ×10 in su; la validazione cancella quel vantaggio. Non è un
compromesso mascherato: è il prezzo scelto per non tacere su una scadenza, e per un processo
che gira **una volta al giorno** 423 ms al trentuplo della scala di produzione non è un
numero che qualcuno noterà.

Dove va il tempo:

| scala | righe | testa+stato | `read_model` | `validate_model` | query SQL | totale |
|---|---|---|---|---|---|---|
| produzione | 86 | 1,0 ms | 5,6 ms | 3,5 ms | 2,9 ms | 13,0 ms |
| ×10 | 860 | 0,8 ms | 34,1 ms | 56,8 ms | 5,1 ms | 96,7 ms |
| ×30 | 2 580 | 0,7 ms | 98,9 ms | **305,9 ms** | 13,7 ms | 419,3 ms |

Tre cose che si leggono da questa tabella:

1. **`validate_model` domina, e cresce più che linearmente**: 30× le righe danno ~87× il
   tempo. È il termine quadratico già misurato nella fase 2D (§8.45.1) e non è stato toccato
   qui. È anche la prima cosa che darebbe fastidio se l'inventario crescesse molto: a 100×
   la scala di produzione sarebbero ~3,4 s, ancora accettabili per un giro al giorno, ma è il
   posto dove guardare;
2. **la query SQL dei candidati resta economica** — da 2,9 a 13,7 ms — e continua a usare gli
   indici parziali. La migrazione della sorgente ha fatto il suo lavoro: ciò che costa adesso
   è la garanzia, non la lettura;
3. **la precondizione strutturale è gratis** (≈1 ms, costante): tre query su metadati già
   materializzati. È la ragione per cui l'ordine dei quattro passi conta — il caso «proiezione
   non mantenuta» si scopre in 1 ms invece di 400.

`EXPLAIN (ANALYZE, BUFFERS)` della query dei candidati a ×30, finestra 90/30/7:
pianificazione 3,0 ms, esecuzione 3,9 ms, `Index Scan` su entrambe le colonne data, 40 e 21
righe lette, tutto da cache (`shared hit`, zero `read`).

#### 8.47.7 Privilegi: nessuna concessione nuova

`tsm_worker` aveva **già** tutto ciò che serve: `SELECT` su `inventory_head` e
`inventory_versions` (0009), sulle cinque tabelle della proiezione (0010), su
`inventory_projection_state` (0011), ribadite dalla 0012 insieme alle `REVOKE` che lo tengono
in sola lettura. Quindi **nessuna migrazione 0013**.

La 2F non cambia i privilegi: li **verifica**. La matrice di `test_photos_api_pg.py` è
cresciuta di 32 righe — otto tabelle in lettura, e per ognuna `INSERT`/`UPDATE`/`DELETE`/
`TRUNCATE` negate — e `test_worker_sql_pg.py` aggiunge la prova col fatto: `SET LOCAL ROLE
tsm_worker`, poi cinque scritture che PostgreSQL respinge con «permission denied».

⚠ `SET ROLE` e non una connessione nuova: le migrazioni creano i ruoli di runtime
`LOGIN NOINHERIT` e **senza password** (la assegna l'operations al deploy, da un file di
secret), quindi in prova non esiste nessuna credenziale con cui collegarsi come
`tsm_worker`. La prima stesura ci provava e finiva in uno `skip` — un test saltato somiglia
troppo a un test passato, e questo avrebbe taciuto proprio sul privilegio che deve
sorvegliare.

#### 8.47.8 Come è verificato

`tests/test_worker_sql_pg.py` — **224 test** su PostgreSQL vero, nessuno saltato. Più i **53
di `test_worker_pg.py` non modificati**, che sono la metà che conta.

La parte che porta il peso è la parità, e la sua forma è diversa da quella della 2E: là
l'oracolo era JavaScript nel browser e serviva un generatore in Node; qui l'oracolo è
`expiry.due_items`, che è Python, puro, e si chiama dentro il test. Sedici corpora
(`fixtures/expiry/parity.py`) più il seed e il seed con le date, per cinque insiemi di
finestre. Un test in `test_get_from_sql_pg.py` pretende che `due_items` resti indipendente
dalle colonne derivate: se dipendesse dalle stesse colonne, la parità confronterebbe
l'implementazione con sé stessa.

Coperto: i confini di 90/30/7 e i giorni immediatamente dentro e fuori; il recupero; scaduti,
oggi, futuri fuori finestra; garanzia e supporto separatamente e insieme sullo stesso
dispositivo, incluso il caso «garanzia scaduta, supporto in finestra» che un filtro per
dispositivo invece che per (dispositivo, tipo) sbaglierebbe; dismessi in cinque stati; date
rotte in quattordici forme, con e senza spazi, non-stringhe comprese; la catena
dell'etichetta ramo per ramo, `name` numerico/zero/falso/lista/dizionario; sito, sala e rack
senza id e con id numerico; gli id con lo `/`; id duplicati e gemelli identici; albero su due
siti, tre sale, quattro rack; dispositivo spostato e data cambiata; inventario vuoto e rami
vuoti; nome ostile con `\r\n`; tre fusi al cambio d'ora e al confine d'anno; finestre
assenti e finestra massima (3 650 giorni).

Il fallimento chiuso: proiezione assente, di versione vecchia, con digest sbagliato, con mappa
non supportata — per ognuna, nessun invio, nessun promemoria, nessuna consegna, giro **non
concluso**, e il recupero automatico dopo un `--rebuild`. Più il database irraggiungibile, con
solo l'engine di lettura rotto così che il guasto cada dove la 2F ha messo la lettura nuova.

**Il gruppo che porta il peso della validazione** (§8.47.4) è costruito in tre passi, e il
primo non è facoltativo:

1. **la premessa**: si prova che la corruzione di una colonna derivata è *davvero* invisibile —
   documento identico byte per byte, `currency().current` ancora vero. Senza questo passo i due
   successivi dimostrerebbero che una guardia funziona, non che serviva;
2. **il rifiuto**: testo `garanzia` valido e `garanzia_date` cancellata →
   `projection_inconsistent`, zero connessioni SMTP aperte, zero promemoria, zero consegne,
   giro non concluso;
3. **il recupero nello STESSO giorno locale**: si ripara con `rebuild`, si riesegue senza
   spostare l'orologio di un giorno — che sarebbe la scorciatoia che rende il test verde senza
   provare niente — e si pretende che il digest contenga **esattamente** le scadenze che la
   corruzione aveva nascosto.

Più le tre forme di corruzione derivata (cancellata, falsificata su `garanzia`, falsificata su
`supporto`), la prova che gli avvisi del modello **non** bloccano e risalgono al chiamante, e
la prova che validazione e interrogazione stanno nello **stesso** snapshot — una corruzione
committata da fuori mentre lo snapshot è aperto non deve essere vista, altrimenti fra i due
passi ci sarebbe una finestra, e sarebbe proprio quella che la validazione esiste per
chiudere.

⚠ Due test hanno dovuto essere **riscritti perché non sorvegliavano quello che dicevano**.
`test_the_notification_worker_still_reads_the_document` esisteva in due copie (2C e 2D) come
allarme: «se un giorno lo scanner leggesse `garanzia_date`, sarebbe una decisione presa di
proposito». La 2F è avvenuta e **l'allarme non è suonato**, perché la migrazione non ha
cambiato `expiry.py`: le ha girato attorno, mettendo la sorgente nuova in `candidates.py`. Un
controllo statico su un modulo non sorveglia il comportamento di un altro. I due test
adesso verificano ciò che possono davvero verificare — che `expiry.py` è rimasto puro, cioè
utilizzabile come oracolo — e il fatto sul comportamento lo prova un test che corrompe le
colonne e pretende che il digest cambi.

**Prova di efficacia: 30 mutazioni, tutte intercettate.** La §9 della fase 2F chiede di
mutare la parità «dove praticabile»; si è mutato tutto — la semantica dello scanner,
l'ordinamento, la catena dell'etichetta, la precondizione, il guardiano della revisione,
gli esiti del worker, l'isolamento. Passata di controllo verde prima di ogni cosa, perché
un «INTERCETTATA» misurato su una base rossa non significa niente.

Tre sono sfuggite alla prima passata, e nessuna delle tre per il motivo che sembrava:

1. **`UNION ALL` → `UNION`.** Sfuggita a `pytest` perché con la chiave primaria nella
   `SELECT` la deduplicazione non può togliere righe: è un mutante *equivalente per il
   risultato*, non per il costo. La intercettano i controlli **statici** — che la prima
   passata dell'impianto non eseguiva. Difetto dell'impianto, non della copertura;
   corretto, e adesso ogni mutazione gira contro entrambe le suite.
2. **Il contesto del ritentativo ignora la data.** La mutazione era **mal fatta da me**:
   *aggiungeva* una chiave invece di togliere la data dalla chiave, quindi non
   indeboliva niente e il verde era corretto. Rifatta come si deve — le date prese dai
   promemoria invece che dal dispositivo — la intercetta
   `test_changing_an_expiry_date_drops_the_stale_reminder_from_a_retry`.
3. **Il guardiano confronta la versione ma non il digest.** Questa era una **lacuna
   vera**, e la spiegazione comoda era «mutante equivalente»: `inventory_head.version`
   punta a una riga immutabile, quindi versione uguale implica digest uguale. Il
   ragionamento regge *finché l'immutabilità tiene* — e l'immutabilità la impone un
   privilegio, non una legge di natura. Un test nuovo costruisce lo stato in cui il
   confronto conta: digest della versione **e** digest dichiarato dalla proiezione
   cambiati allo stesso valore falso, versione ferma. La proiezione risulta attuale,
   la versione non si è mossa, e l'unica cosa che vede la differenza è la riga mutata.

⚠ La lezione della terza vale più delle altre due: «mutante equivalente» è la
classificazione che si vorrebbe sempre poter usare, e due volte su tre qui era sbagliata.

**Il percorso di validazione: 13 mutazioni in più.** Aggiunto `validate_model` al worker, si è
mutato anche quello: la precondizione che torna alla sola attualità, gli errori ignorati, gli
avvisi resi bloccanti, la validazione spostata fuori dallo snapshot, i due codici schiacciati
in uno, il giro concluso invece che lasciato aperto, il ritentativo che chiude la consegna, il
tentativo consumato dal rinvio, il `GET` che si riscrive la sua copia. **Dodici intercettate su
tredici**, e la tredicesima ha insegnato qualcosa:

- **il ritentativo su modello incoerente** sfuggiva perché il test gemello guastava la
  `mapper_version` — che è «non attuale» — e nessun test guastava il *modello*. Il caso
  scoperto era esattamente quello che la 2F esiste per chiudere. Test nuovo;
- **il controllo sui riferimenti alle foto** (`known_photo_ids=None`) sfugge, e la prima
  reazione — «buco di copertura, serve un test con un riferimento pendente» — si è rivelata
  impossibile da soddisfare: `inventory_racks.photo_id` ha una **chiave esterna** verso
  `photos`, quindi un riferimento pendente non può esistere nella proiezione e quel ramo di
  `validate_model` è irraggiungibile da qui. Mutante equivalente **dimostrato**, non
  dichiarato: il test che l'accompagna fissa la chiave esterna, così se qualcuno la togliesse
  il buco tornerebbe reale e visibile.

La §17 di `tools/storage-config-test.py` copre il negativo (**21 controlli** nuovi, 310 in
totale): il modulo della sorgente esiste e sta in `app/notifications/`, legge le colonne
derivate, non contiene un secondo parser di date, non filtra i dismessi, interroga una
finestra, usa `UNION ALL`, pretende la proiezione attuale, non blocca la testa; `expiry.py` è
rimasto puro; nessun ripiego sull'istantanea in nessun modulo del worker; l'isolamento è
dichiarato una volta sola; nessuna migrazione 0013 e le `REVOKE` ancora scritte; l'endpoint
non è stato piegato alla semantica del worker; il frontend non è ricablato; la readiness non
guarda lo stato del worker. **Tre controlli delle fasi precedenti sono stati rovesciati**
esplicitamente, con il commento che dice perché: dicevano «il worker non è passato a SQL», che
era la delimitazione giusta della 2C, della 2D e della 2E, ed è la decisione presa nella 2F.

### 8.48 Registro del debito semantico

Incoerenze **reali e misurate** dell'applicazione, scoperte nelle fasi 2E e 2F. Nessuna era
stata corretta allora: correggerle mentre si migrava avrebbe mescolato una modifica di
prodotto con una migrazione tecnica, e reso impossibile dire quale delle due ha cambiato un
comportamento.

⚠ **La fase 2G le risolve.** Il registro resta, e resta con il comportamento vecchio
scritto accanto: serve a chi legge un ticket di sei mesi fa, o un export prodotto prima
della 2G, e non capisce perché i numeri non tornano. Ogni voce porta l'esito.

Le colonne: **cosa** era il comportamento misurato, **dove** viveva, **esito** che cosa è
adesso.

Un controllo statico pretende due cose, e la seconda è quella che conta: che **nessuna**
voce resti senza esito, e che le voci ancora aperte siano un SOTTOINSIEME di quelle che il
requisito autorizza a restare aperte (la 14, il seed senza scadenze). Sottoinsieme e non
uguaglianza, di proposito: chiudere la 14 non deve far diventare rosso un test — aprirne
una nuova sì.

#### Ricerca

| # | cosa | dove | esito |
|---|---|---|---|
| 1 | Un IP esatto **non** è una query di rete: `parseIpQuery` gestisce solo CIDR, intervalli e jolly, quindi `10.0.0.1` è una ricerca testuale e combacia anche con `10.0.0.100`. | frontend + endpoint | **RISOLTA** (2G). `exact` è una forma riconosciuta per IPv4 e IPv6: `10.0.0.1` significa quell'indirizzo. `10.0.0` resta testo, e resta giusto che lo sia — mezzo indirizzo è un prefisso. §8.50.6 |
| 2 | La ricerca per rete è **solo IPv4** (`ipToNum`): un dispositivo IPv6 è irraggiungibile per intervallo, raggiungibile per testo. | frontend + endpoint | **RISOLTA** (2G). IPv6 esatto e CIDR IPv6. Intervalli e jolly IPv6 **non** esistono, per decisione: `2001:db8::*` non ha un significato che qualcuno abbia chiesto. §8.50.6 |
| 3 | In modalità rete i **rack non partecipano affatto**, nemmeno quello il cui codice è un indirizzo. | frontend + endpoint | **RISOLTA — confermata come voluta** (2G, §5 del requisito). Un rack che si chiama «10.0.0.1» non è una macchina con quell'indirizzo: restituirlo a chi cerca un host è un falso positivo che *sembra* una risposta. Era un comportamento giusto per caso; adesso è una scelta. |
| 4 | I campi cercati sono cinque (`name, model, ip, serial, owner`): `id`, `type`, `stato` e `note` **non** si cercano, e nessuna interfaccia lo dice. | frontend + endpoint | **RISOLTA** (2G). Nove campi: `id, name, model, ip, serial, owner, tipo, stato, presenza`. Le `note` restano fuori **per decisione** — testo libero e lungo, che renderebbe qualunque parola comune un risultato di massa. §8.50.6 |

#### Capacità

| # | cosa | dove | esito |
|---|---|---|---|
| 5 | «U usate» ha **tre** implementazioni che non concordano: la vista Capacità conta gli **slot distinti occupati**, il pannello del rack e l'export XLSX fanno `SUM(h)`. Sovrapposizioni e sporgenze danno numeri diversi. | frontend (3 posti) | **RISOLTA** (2G). Una definizione: `domain.rack_capacity` / `DOM.rackCapacity`, slot U distinti. I tre posti la chiamano. Un controllo statico vieta il ritorno di `SUM(h)`, e le fixture di capacità riportano `sumH` accanto a `usedU` **solo dove differisce**, con un test che pretende la differenza. §8.50.3 |
| 6 | I dispositivi **dismessi occupano spazio** nella vista Capacità, per un blocco vuoto: `if ((d.stato \|\| 'attivo') === 'dismesso') {}`. La vista Scadenze li esclude con un `continue` vero. Lo stesso stato ha due significati nella stessa applicazione. | frontend + endpoint | **RISOLTA** (2G), e la domanda era sbagliata. Lo stato OPERATIVO non dice se un apparato occupa uno slot: lo dice la **presenza fisica**, che è un campo nuovo. `dismesso + presente` occupa, `qualunque + rimosso` no. Il ramo vuoto dava per caso la risposta giusta nella metà dei casi. §8.50.2 |
| 7 | Il raggruppamento per fila usa la sentinella `rk.row \|\| '—'`, e il seed di produzione contiene un rack la cui fila **è** `—` (`CS-Q01`): si fonde con i rack senza fila. | frontend + endpoint | **RISOLTA** (2G). `domain.row_group` tiene separata la CHIAVE del gruppo dall'ETICHETTA mostrata; la chiave contiene un byte NUL, che nessun valore di documento può contenere (§8.31). L'interfaccia mostra ancora «—» in entrambi i casi: cambia solo ciò che considera lo stesso gruppo. §8.50.5 |

#### Scadenze

| # | cosa | dove | esito |
|---|---|---|---|
| 8 | La vista Scadenze e lo scanner del worker **non sono d'accordo**: la vista salta i dismessi ed elenca gli scaduti, il worker fa l'opposto. La 2F segue il worker (§8.47), l'endpoint segue la vista. | frontend / worker | **RISOLTA** (2G). Restano diverse **per scelta**, non per caso, e ognuna fa ciò che serve alla sua domanda: la vista è ISPETTIVA (mostra tutto, dismessi compresi), il worker è AZIONABILE (`0 <= giorni <= finestra`, e non i dismessi). La risposta della vista porta `notifiable` accanto a ogni riga, così la differenza si legge invece di essere sapere di pochi. §8.50.8 |
| 9 | Il frontend interpreta le date con `new Date(v)`, i due backend con `parse_expiry`. Sette forme sono visibili nella vista e **invisibili** a worker ed endpoint, e sono queste: `2027-3-15`, `2027/03/15`, `March 15, 2027`, `2027-03-15T10:00:00Z`, `2027-03`, `2027`, `2027-02-30` (V8 lo fa scorrere al 2 marzo). `15/03/2027` è rifiutato da **entrambi** (V8 legge il mese 15). | frontend vs backend | **RISOLTA** (2G). Un interprete solo, `domain.parse_expiry`, e il frontend lo chiama. Un test lo pretende sull'IDENTITÀ della funzione, non sul comportamento. Il corpus porta le sette forme con l'attesa `null` **e la controprova** che `new Date` le accettava — senza quella, il corpus dimostrerebbe solo che il parser rifiuta qualcosa. §8.50.7 |
| 10 | Un avviso per una scadenza **già passata** non esiste: si ripeterebbe ogni giorno per sempre, o no? Nessuna fase lo decide. | worker | **RISOLTA — NON PREVISTA.** Non è una funzione che manca: è una funzione che il prodotto dichiara di non avere, e la differenza conta perché la prima si recupera e la seconda si spiega. Contratto finale: gli scaduti restano ISPEZIONABILI nella vista Scadenze col livello `expired`; il worker agisce solo su `0 <= giorni <= finestra`; nessun avviso retrospettivo è richiesto. Gli scaduti si guardano, non si ricevono. |

#### Etichette e contesto

| # | cosa | dove | esito |
|---|---|---|---|
| 11 | `id` non è obbligatorio nello schema del documento, e il percorso di `walk` lo interpolava in una f-string: un sito o un rack senza `id` compare nel digest come la stringa **`"None"`**. Conservato nella 2F. | worker | **RISOLTA** (2G, §9 del requisito). La catena è nome → codice → «(senza nome)», e «None» non compare mai. Il test che pretendeva «None» è stato ROVESCIATO, non cancellato: la forma vecchia resta scritta accanto. §8.50.9 |
| 12 | Gli id che contengono `/` erano troncati dal rispezzamento del percorso; dalla 2F le JOIN restituiscono il valore intero (§8.47.2). | worker | **RISOLTA** dalla 2F, **rafforzata** dalla 2G. Il contesto è fatto di tre valori separati e nessuno costruisce più quella stringa. ⚠ Il test si appoggiava al fatto che il codice comparisse nell'etichetta, e dalla 2G l'etichetta preferisce il nome: il corpus porta ora un rack e un sito con lo `/` nel codice **e senza nome**, che è l'unico caso in cui un troncamento tornerebbe visibile. |
| 13 | Una corruzione delle **colonne derivate** non muove né digest né versione: era invisibile alla guardia del worker, e gli avvisi smettevano senza che niente lo dicesse. | worker | **RISOLTA** dalla 2F: `validate_model` dentro lo stesso snapshot, e rifiuto con `projection_inconsistent` (§8.47.4). ⚠ La 2G aggiunge una derivata (`ip_addr`) e quindi ALLARGA la superficie: una `ip_addr` corrotta darebbe ricerche sbagliate, e le tre interrogazioni **non** eseguono `validate_model` (costa troppo per richiesta). La rete di sicurezza resta `project.py --verify`. Dichiarato in §8.50.11. |

#### Coerenza e dati

| # | cosa | dove | esito |
|---|---|---|---|
| 14 | Il **seed di produzione non ha nessuna scadenza**: 86 dispositivi, zero `garanzia` e zero `supporto`. Prima della 2F non esisteva nessun modo di provare il worker su dati di forma reale. | dati | **APERTA, blocco al rilascio.** La 2F l'ha aggirata con un corpus derivato (`seed-dated`), la 2G ci ha aggiunto le presenze, la 2H non la tocca: un corpus costruito da me non è un dato reale. ⚠ Qualificazione richiesta prima del rilascio: popolare uno scenario di scadenze DETERMINISTICO e di forma reale, e percorrere il cammino completo degli avvisi su quello — non su un caso costruito per far passare un test. Vedi §7. |
| 15 | La sincronizzazione della 2C cancella e reinserisce **ogni riga a ogni salvataggio**, quindi ogni scrittura grande apre qualche secondo di statistiche del pianificatore vecchie (§8.46). Invisibile alla scala di produzione. | backend | **CARATTERISTICA DICHIARATA**, non debito semantico. Riclassificata: nessun numero sullo schermo cambia e nessuna implementazione è in disaccordo con un'altra — è un costo di scrittura, quindi un fatto operativo (§8.46), non una discrepanza. Resta scritta qui perché questo registro è anche il posto dove si legge che cosa è stato guardato e archiviato. |
| 16 | ⚠ **NUOVA, scoperta dalla 2G.** `rack.u` nel documento è un intero JSON senza massimo; la colonna della proiezione è `integer`. Un rack più alto di 2³¹ finisce in `extra` e la colonna resta NULL, quindi la capacità **dallo SQL** riporta quel rack senza altezza mentre il modello puro — che legge il documento — calcola sul valore vero. | backend | **RISOLTA — limite di dominio dichiarato e APPLICATO.** `rack.u` deve stare nell'intervallo intero positivo che la proiezione sostiene: `domain.RACK_U_MIN..RACK_U_MAX`, cioè `1..2147483647`. Un valore fuori viene RIFIUTATO dalla convalida normale del documento, prima di persistere, col codice stabile `rack_u_out_of_range`. Non si passa a `bigint`: sarebbe cambiare il tipo di una colonna — quindi versione della mappa e ricostruzione — per un dato che l'interfaccia non produce e che nel browser esaurisce la memoria della scheda. Il limite non DIVERGE più dal comportamento: dove due letture davano due numeri, ora il documento non entra. §8.51.1 |

⚠ Bilancio dopo la **2H**: chiuse le voci **da 1 a 13, più la 16**. La 10 è chiusa come
funzione NON PREVISTA (una decisione, non un rinvio), la 15 è stata riclassificata a
caratteristica operativa, la 16 è chiusa perché il limite ora si applica invece di essere
soltanto scritto.

Resta aperta **una** voce: la **14**, il seed senza scadenze, e resta un **blocco al
rilascio**. Non è una discrepanza semantica — è la funzione più delicata del prodotto non
ancora esercitata su dati che qualcuno ha scritto davvero.

Nessuna incoerenza semantica visibile all'utente resta aperta. Se ne comparisse una,
§14 del requisito della 2G la dichiara un blocco al rilascio, e il posto dove scriverla
è questa tabella.

### 8.49 Mappatura dei commit della fase 2

⚠ Nota permanente. Quattro commit della fase 2 portano nel **soggetto** il nome della fase
**precedente** ai propri contenuti: uno scarto di uno propagatosi da `2aa5dcf` in avanti. I
commit sono **pubblicati**, quindi i soggetti restano come sono: riscrivere la storia
pubblicata per riparare del testo costerebbe più di quanto valga, e chiunque abbia già il
ramo si troverebbe con due storie diverse.

**Gli hash sono l'autorità.** Questa tabella, non i soggetti, dice quale commit è quale fase.

| commit | fase reale | soggetto |
|---|---|---|
| `21e6dfc` | **2C** — doppia scrittura atomica della proiezione | corretto |
| `2aa5dcf` | **2D** — l'inventario si legge da SQL | ⚠ dice «2C» |
| `d47b7f2` | **2E** — interrogazioni SQL sulla proiezione | ⚠ dice «2D» |
| `bc74a04` | **2F** — implementazione iniziale del worker su SQL | ⚠ dice «2E» |
| `9673966` | **2F** — guardia finale di coerenza della proiezione | corretto |

Quindi: le sezioni §8.44 (2C), §8.45 (2D), §8.46 (2E) e §8.47 (2F) di questo documento
descrivono i contenuti indicati nella colonna «fase reale». Il soggetto di un commit non è
una fonte affidabile per la fase, in questo intervallo di storia.

Non si fa `--force-push` e non si fa `--amend` su questi commit.

### 8.50 Fase 2G: una semantica sola

Fino alla 2E il contratto del prodotto era **il comportamento misurato del prototipo**.
Era la scelta giusta: cambiare la semantica durante una migrazione tecnica avrebbe reso
impossibile dire quale delle due cose aveva cambiato un numero sullo schermo. Ma era una
scelta a scadenza, e la 2G è la scadenza.

Il conto di ciò che divergeva, dal registro §8.48: **tre** definizioni di «U occupate»,
**due** interpreti di data, **due** elenchi di campi cercabili, **due** idee di
«dismesso», una sentinella di raggruppamento che collideva col dato, e un indirizzo IP
esatto che trovava la macchina sbagliata.

#### 8.50.1 Come è fatto: un modello, tre implementazioni, un contratto in dati

    backend/app/domain.py        Python, e per estensione SQL
    handoff/domain.js            JavaScript, per il frontend
    fixtures/domain/*.json       il CONTRATTO: dati, non codice

⚠ **Le attese delle fixture sono scritte a mano**, e questo è il cardine. Il generatore
della 2E (`make-query-fixtures.mjs`) CALCOLAVA le attese copiando alla lettera il
JavaScript del frontend, e allora era giusto: ciò che andava dimostrato era la parità con
il comportamento che girava, e scrivere le attese a mano avrebbe dimostrato soltanto che
lo SQL corrispondeva alla mia lettura del prototipo.

Qui la domanda è rovesciata. Il comportamento del prototipo non è più il riferimento — è
ciò che si sta sostituendo — e l'attesa è una decisione di prodotto. Calcolarla da una
delle due implementazioni renderebbe il contratto **vacuo**: se sbagliassero entrambe
allo stesso modo, nessun test diventerebbe rosso.

Da qui la regola per chi tocca `tools/make-domain-fixtures.mjs`: *un'attesa non si
aggiorna perché un test è rosso.* Si aggiorna quando la decisione cambia, e allora il
rosso è il messaggio che l'implementazione non l'ha ancora seguita — in tutte e tre le
suite contemporaneamente.

L'unica eccezione, dichiarata: `addresses-fuzz.json`. Quattromilacinquecento forme mutate
non si benedicono a mano, e non è quello il loro scopo — servono a dimostrare che le due
implementazioni non divergono su nessuna. I verdetti li scrive il generatore (cioè
JavaScript) e la suite Python pretende di produrre gli stessi: è un confronto
**differenziale**, non un giudizio di prodotto. I casi che portano una decisione stanno
tutti in `addresses.json`, con le attese a mano.

⚠ Il confronto differenziale ha fatto il suo lavoro subito, e non era un esercizio: ha
trovato **tre difetti** nella mia prima stesura del parser JavaScript nel giro di un
minuto — i gruppi IPv6 renderizzati alla rovescia (`::1` diventava `1::`, e i valori
numerici combaciavano comunque, quindi solo il testo lo mostrava), `1.2.3.4::` accettato
dove Python rifiuta, e la forma puntata usata anche per gli IPv4-*compatible* invece dei
soli IPv4-*mapped*. Poi, sulle 4567 forme mutate, una quarta classe: gli zeri iniziali
nell'IPv4 incorporato in un IPv6. Nessuna rilettura del codice ci sarebbe arrivata.

#### 8.50.2 Presenza fisica ≠ stato operativo

`stato` descrive il **ciclo di vita operativo**: `attivo`, `manutenzione`,
`dismissione`, `dismesso`. Vocabolario invariato.

`presenza` è nuova e descrive la **presenza fisica**: `presente`, `rimosso`.

    presente   l'hardware occupa ancora il suo slot nel rack
    rimosso    l'hardware è stato portato via

Le due domande sono **indipendenti**, e la tabella delle sei combinazioni non è una
formalità:

| stato | presenza | significato |
|---|---|---|
| attivo | presente | in servizio e installato |
| manutenzione | presente | installato, in manutenzione |
| dismissione | presente | installato, in via di dismissione |
| dismesso | presente | **fuori servizio ma ancora nel rack** |
| dismesso | rimosso | fuori servizio e portato via |
| attivo | rimosso | in servizio altrove, non ancora ri-registrato |

⚠ **La presenza non si deduce dallo stato**, e dedurla sarebbe il difetto peggiore
possibile in questa fase. Un `dismesso` senza `presenza` canonicalizza a
`dismesso + presente`: l'inventario di prima della 2G non registra le rimozioni, quindi
l'unica cosa che si sa di quelle macchine è che *nessuno ha detto* che sono state portate
via. Dedurre `rimosso` da `dismesso` libererebbe d'un colpo unità rack che in sala sono
occupate — e il primo a scoprirlo sarebbe chi arriva con un apparato nuovo e non trova
posto.

Un test lo pretende in forma verificabile e non a parole: per ogni stato esistono
entrambe le presenze, e per ogni presenza entrambe le risposte a «occupa». Se qualcuno
reintroducesse una deduzione, una delle due mappe diventerebbe costante e il test lo
direbbe.

Un apparato `rimosso` **non si cancella** dall'inventario. Conserva `_uid`, seriale, id
di business, nome, modello, referente, stato, note e l'ultimo rack in cui stava. È
deliberato: serve al riscontro incrociato dell'hardware, che è la ragione per cui un
inventario esiste.

⚠ `schemaVersion` **non** cambia, e la ragione è che non ne ha bisogno. Un documento
senza `presenza` resta interpretabile, perché l'assenza ha un significato dichiarato: è
la stessa condizione di `stato`, `h` e `type`, che hanno sempre avuto un default e non
hanno mai richiesto una versione nuova. Alzarla avrebbe imposto una migrazione del
documento a tutti i client per un campo che si può omettere.

Cambia invece il **digest canonico del seed**, perché la forma canonica ha un campo in
più per dispositivo. È atteso, e `tools/verify-seed-migration.mjs --update` lo registra
dopo che è stato guardato a mano.

#### 8.50.3 Capacità: una definizione, e il conto di quelle che ha sostituito

> `used_u` = numero di slot U fisici **DISTINTI** occupati da dispositivi la cui
> `presenza` non è `rimosso`.

Le tre implementazioni che c'erano (§8.48 voce 5):

| dove | formula | sbagliava su |
|---|---|---|
| vista Capacità | slot distinti, `if (dismesso) {}` vuoto | i rimossi |
| pannello del rack | `SUM(h)` | sovrapposizioni, sporgenze, rimossi |
| export XLSX | `SUM(h)`, e `NaN%` con `rk.u = 0` | idem, più la divisione per zero |

Le cinque regole fisiche, adesso scritte in un posto (`domain.slot_span`):

  - `h` assente o `0` vale 1 — è il `d.h || 1` di sempre;
  - `h` **negativo** non occupa niente: `-3` da U10 sarebbe un intervallo rovesciato, e
    inventargli un verso vorrebbe dire decidere al posto di chi ha digitato male;
  - slot iniziale `<= 0` sta **fuori** dal rack: i rack si contano da 1;
  - la sporgenza oltre la cima si **taglia**: un 4U montato a U44 di un rack da 45 occupa
    due unità, perché sono due quelle che esistono;
  - `u` o `h` non interi non occupano niente: non si arrotonda un dato che non è un
    numero di slot.

⚠ Il costo è quello dei **dispositivi**, non dell'altezza del rack. `rack.u` è un
`integer` senza massimo e il corpus `oversized-integers` ne contiene uno da 3 000 000 000:
enumerare gli slot sarebbe la traduzione ovvia e sarebbe un guasto — nel browser esaurisce
la memoria della scheda, in SQL produce tre miliardi di righe dentro una richiesta HTTP.
In SQL è un'unione di intervalli con funzioni finestra, in Python e JavaScript una
fusione di intervalli ordinati.

⚠ **Il test che rende rossa la reintroduzione di `SUM(h)`.** Non basta che `used_u` sia
giusto: bisogna che il corpus **distingua** le due definizioni. Le fixture di capacità
riportano `sumH` accanto a `usedU` solo dove differisce, e un test pretende
`sumH != usedU` su ognuna di quelle righe. Un corpus in cui le due formule coincidono
passerebbe anche con l'implementazione sbagliata — e un test che non può fallire non
protegge niente.

Lo stesso test ha trovato una fixture mal progettata: il caso `h = 0` dichiarava
`sumH: 0`, mentre la formula legacy era `sum(d.h || 1)` e quindi dava 1, cioè lo stesso
valore degli slot distinti. Quel caso **non distingue** le due definizioni, e adesso lo
dice invece di far passare un confronto vacuo.

#### 8.50.4 Percentuale: aritmetica intera, HALF-UP

Tre linguaggi, tre risposte diverse sulla metà esatta:

    JavaScript   Math.round(0.5)  =  1     (metà verso l'alto)
    Python       round(0.5)       =  0     (metà al pari, «del banchiere»)
    PostgreSQL   round(0.5)       =  1     (metà lontano da zero)

Un rack da 8 U con 1 U occupata è al 12,5%: il frontend mostrava **13** e Python avrebbe
detto **12**. Nessuno dei due è sbagliato in sé; averli entrambi lo è.

    floor(used * 100 / total + 1/2)  ==  (used * 200 + total) // (total * 2)

La forma a destra non contiene divisioni in virgola mobile, quindi non contiene nemmeno il
loro arrotondamento: le tre implementazioni danno lo stesso intero **per costruzione**,
non per fortuna. Un totale nullo o negativo dà 0 — un rack alto zero unità non è occupato
al 100%, non ha unità.

Un test esercita ogni `used/total` con `total` fino a 400: sono i casi in cui il risultato
cade esattamente su una metà, cioè quelli in cui `floor(x + 0.5)` in virgola mobile può
sbagliare per un epsilon. E una controprova pretende che `round()` di Python dia una
risposta DIVERSA su almeno un caso del corpus: senza, il corpus dimostrerebbe soltanto che
`percent` restituisce dei numeri.

#### 8.50.5 File: l'identità del gruppo non è l'etichetta

Il prototipo raggruppava per `rk.row || '—'`. È una **sentinella che collide col dato**:
nel seed di produzione esiste un rack la cui fila È «—» (CS-Q01), e finiva nel gruppo di
tutti quelli senza fila. Il totale di unità libere di quella fila era la somma di due cose
diverse.

`domain.row_group` restituisce quattro campi: `assigned`, `value`, `key`, `label`.

  - `key` identifica il gruppo e contiene un **byte NUL**, che nessun valore di documento
    può contenere (`json_strings.is_representable_text` lo rifiuta, §8.31). È la stessa
    tecnica dei separatori di chiave in `identity.js`;
  - `label` è ciò che si mostra, e per una fila non impostata resta «—».

L'interfaccia non cambia aspetto. Cambia soltanto ciò che considera lo stesso gruppo.

⚠ Il NUL non è un dettaglio di implementazione: è la **ragione** per cui la collisione non
può ripresentarsi. Con un separatore stampabile, un rack la cui fila valesse esattamente
quel separatore ricreerebbe il difetto — la stessa storia, con un carattere diverso. Un
test lo pretende, e pretende anche che il carattere sia irrappresentabile in un documento.

Il gruppo «senza fila» si ordina per **ultimo**: è il residuo, non una fila che si chiama
«—», e metterlo in testa lo farebbe leggere come la prima fila della sala.

#### 8.50.6 Ricerca: una grammatica di indirizzi, e nove campi

**Testo.** Sottostringa **letterale**, senza distinzione di maiuscole. `strpos` su
`lower(...)` e non `LIKE`: `LIKE` attribuisce un significato a `%` e `_`, che in una
casella di ricerca sono caratteri normali — con `LIKE` una query contenente `%`
troverebbe tutto. Nove campi del dispositivo (`id, name, model, ip, serial, owner, tipo,
stato, presenza`) e tre del rack (`id, name, seriali`).

`tipo`, `stato` e `presenza` si cercano nel **valore memorizzato** (`server`, `attivo`,
`rimosso`), non nell'etichetta tradotta: le etichette vivono nell'interfaccia e cambiano
con la lingua, i valori sono il dato. E passano dal loro **default**: un dispositivo senza
`stato` è `attivo` e va trovato cercando «attivo», perché è così che l'interfaccia lo
mostra e così che la proiezione lo memorizza.

Le `note` restano fuori **per decisione**: testo libero e lungo, che renderebbe qualunque
parola comune un risultato di massa. È una scelta rivedibile, non una dimenticanza, e la
differenza sta scritta nel contratto.

⚠ **`extra` partecipa**, dalla 2G. Un valore che la mappa non ha potuto mettere in una
colonna tipizzata sta in `extra` (§8.42), e nella 2E la ricerca non lo guardava: un rack i
cui `seriali` contengono un numero porta l'intero array in `extra`, e i suoi seriali non si
trovavano — mentre l'utente li vedeva sullo schermo. Era registrata come «stranezza», e
resta una **risposta sbagliata**. Adesso ogni campo cercabile si guarda nella colonna
**oppure** in `extra`, che è la stessa regola che `candidates.py` applica alle etichette.

**Indirizzi.** Una grammatica sola (`domain.parse_address_query`):

    10.0.0.1                esatto IPv4
    2001:db8::1             esatto IPv6
    10.0.2.0/24             CIDR IPv4
    2001:db8::/32           CIDR IPv6
    10.0.0.1 - 10.0.0.99    intervallo IPv4
    10.0.*                  jolly IPv4

⚠ **`10.0.0.1` non trova più `10.0.0.100`.** Era il difetto più visibile del prodotto: un
IP esatto non era una forma riconosciuta, quindi finiva nella ricerca testuale, e
`10.0.0.1` è una sottostringa di `10.0.0.100`. Chi cercava una macchina precisa riceveva
la sua vicina di sottorete.

⚠ **Non esistono intervalli né jolly IPv6**, e non si inventano: `2001:db8::*` dovrebbe
voler dire «un gruppo qualsiasi» o «il resto dell'indirizzo»? Ogni risposta è una
grammatica nuova che nessuno ha chiesto, e sbagliarla vorrebbe dire mostrare all'utente
una rete diversa da quella che ha cercato. Restano testo.

⚠ **Le famiglie non si mescolano.** Un jolly `10.0.*` non trova `::a00:1` anche se quel
valore numerico coincide: sono due spazi di indirizzamento. È anche l'ordinamento che
PostgreSQL dà al tipo `inet` — prima la famiglia, poi l'indirizzo — quindi la regola è la
stessa nei tre posti in cui viene applicata, e un test la pretende **dal database** invece
di fidarsi di questa frase.

`10.0.0` continua a essere testo, e a trovare `10.0.0.1`, `10.0.0.100`… Non è
un'incoerenza: mezzo indirizzo non è un indirizzo, e chi lo scrive sta cercando un
prefisso.

**In modalità indirizzo i rack non partecipano** (§8.48 voce 3, confermata come voluta).

#### 8.50.7 Date: un interprete, e la controprova che ne servivano meno

`domain.parse_expiry`: `YYYY-MM-DD` esatto, spazi intorno tollerati, niente altro.
Autorevole per lo scanner, per la colonna derivata, per l'endpoint e — dalla 2G — per il
frontend, che usava `new Date(v)`.

Un test lo pretende sull'**identità** dell'oggetto funzione, non sul comportamento: due
funzioni equivalenti oggi divergono domani, e divergono sui casi limite.

Le sette forme che `new Date` accettava e il backend no stanno nel corpus con l'attesa
`null`, **e con la controprova** che `new Date` le accetta davvero. Senza quella, il
corpus dimostrerebbe soltanto che il parser rifiuta qualcosa, non che rifiuta qualcosa che
l'implementazione precedente accettava. Per `2027-02-30` la controprova è più forte:
`new Date` non rifiuta, la fa **scorrere al 2 marzo**. Una data inesistente diventava una
data esistente, e chi gestisce il contratto l'avrebbe scoperto il 2 marzo.

⚠ **Il valore grezzo non si riscrive mai.** `supporto = "March 15, 2027"` resta
nell'inventario esattamente com'è, e si limita a non essere una scadenza riconosciuta.
`validate_model` lo segnala come AVVISO — non errore — con un messaggio che dice la
conseguenza: «nessuna vista la mostrerà come scadenza e il worker non ne manderà avvisi».

⚠ Lo spostamento del parser ha chiuso un difetto **che era già lì**: in Python `\d`
combacia con OGNI cifra decimale Unicode, quindi `２０２７-０３-１５` (cifre a larghezza
intera) era una data per il backend e non per il frontend, dove `\d` è ASCII. Non l'ha
trovato una rilettura: l'ha trovato il confronto fra le due implementazioni sul corpus
condiviso. Adesso la classe è `[0-9]` in entrambi.

⚠ **I giorni sono una differenza fra due date di calendario**, intera. Il frontend faceva
`Math.round((dt - Date.now()) / 86400000)`, che non è un conteggio di giorni: dipende
dall'ora del giorno, e nella notte del cambio dell'ora una differenza di 23 o 25 ore si
arrotondava a un giorno **per caso**. `daysFromCivil` (algoritmo di Howard Hinnant) dà
l'intero esatto senza toccare `Date`, e il corpus porta due casi che attraversano i
passaggi dell'ora legale del 2026 e del 2027.

L'anno `0000` è rifiutato da entrambe le implementazioni: non esiste nel calendario
gregoriano e `datetime.date` non lo rappresenta. La prima stesura della fixture lo dava
per valido, e il confronto fra le due implementazioni ha mostrato che divergevano.

#### 8.50.8 Scadenze e notifiche: due domande, decise

    vista Scadenze   ISPETTIVA
                     «quali informazioni di scadenza può ispezionare un operatore?»
                     → tutte quelle valide: scadute, di oggi, future
                     → **compresi i dismessi**, con i filtri per stato e presenza

    worker           AZIONABILE
                     «quale scadenza ATTUALMENTE AZIONABILE richiede un'email?»
                     → 0 <= giorni <= finestra più larga
                     → **non i dismessi**

Prima ognuna faceva l'opposto dell'altra **senza che nessuno l'avesse deciso** (§8.48 voce
8). Adesso restano diverse per scelta, e ognuna fa ciò che serve alla sua domanda.

Le due decisioni di prodotto:

  1. **`dismesso` non genera più avvisi nuovi.** Nessuno deve rinnovare la garanzia di un
     apparato che non tornerà in servizio. `attivo`, `manutenzione` e `dismissione`
     restano idonei, perché «in dismissione» significa che la decisione non è ancora
     conclusa;
  2. **la vista Scadenze mostra i dismessi.** Un apparato dismesso ha un contratto che
     scade, e chi fa l'inventario dei contratti deve poterlo vedere.

⚠ La **presenza fisica non decide l'idoneità**. Un apparato portato in un altro sito ha
la garanzia che scade comunque, e chi la rinnova ha bisogno di saperlo. La presenza decide
l'occupazione dello spazio; lo stato decide gli avvisi. Un test incrocia le due dimensioni
sul corpus — `dismesso + presente`, `dismesso + rimosso`, `attivo + rimosso`,
`dismissione + rimosso` — perché senza l'incrocio nessuno può distinguere «guardo lo
stato» da «guardo la presenza».

⚠ Un valore di stato **fuori vocabolario resta idoneo**. Escluderlo a naso vorrebbe dire
spegnere gli avvisi di un apparato per un campo compilato male, ed è il verso sbagliato in
cui sbagliare.

⚠ L'idoneità si applica anche al **ritentativo**. Se un dispositivo diventa `dismesso` fra
la creazione del promemoria e il ritentativo, la sua voce esce dal digest — come esce
quella di chi ha corretto la garanzia. È la stessa regola: non si manda un avviso su
qualcosa che nel frattempo ha smesso di richiederlo.

La risposta dell'endpoint porta `notifiable` accanto a ogni riga e nei totali. È
l'informazione che spiega la differenza fra le due viste **dentro la risposta**, senza
obbligare chi legge a conoscerla: un dismesso compare con `notifiable: false`.

I filtri `stato` e `presenza` sono il modo di fare la domanda ristretta:
`?stato=dismesso&presenza=rimosso` è l'elenco dei contratti di ciò che è stato portato
via, cioè il riscontro incrociato per cui i dismessi si conservano invece di essere
cancellati (§8.48 voce 8, §8 del requisito).

⚠ Un filtro **fuori vocabolario è 422, non zero risultati**. `?stato=dismessi` al plurale
darebbe un elenco vuoto, e chi lo legge concluderebbe che non ci sono apparati dismessi:
plausibile e falsa. Un errore dice che la domanda era scritta male, ed è l'unica delle due
risposte che porta a correggerla.

#### 8.50.9 Etichette: mai un valore dell'implementazione

Catena: **nome mostrabile → codice di business → «(senza nome)»**. Mai `None`, mai
`undefined`, mai `null`.

Che cosa può essere un'etichetta, dichiarato invece che dedotto — perché le due
implementazioni non hanno la stessa idea di «vuoto», e la differenza si vede sui dati
importati da un foglio di calcolo:

  - una **stringa** non vuota sì, anche di soli spazi: è ciò che l'utente ha scritto;
  - un **numero** diverso da zero sì, in forma decimale (`name: 42` → «42»);
  - zero, `false`, `null`, **elenchi e oggetti** no. `String([])` in JavaScript è la
    stringa vuota e `str([])` in Python è «[]»: due etichette diverse per lo stesso dato,
    cioè esattamente ciò che questa fase elimina. Nessuno dei due è un'etichetta, e la
    risposta giusta è passare al candidato successivo;
  - `42.0` è «42» e non «42.0»: `String(42.0)` in JavaScript dà la prima forma, ed è
    quella che l'utente ha visto nell'interfaccia da sempre.

⚠ **Il contesto resta strutturato.** Sito, sala e rack sono tre campi separati fino a chi
li mostra, e nessuno costruisce più la stringa `«sito / sala / rack»`. Era il difetto delle
voci 11 e 12 del registro: un id con uno `/` veniva troncato e ogni pezzo dopo di lui
scalava di un posto.

⚠ Una conseguenza da dichiarare: **il testo degli avvisi cambia**. La catena preferisce il
nome al codice, quindi un sito con codice `pomezia-g0` e nome «Pomezia — G0» compare nel
digest col nome. Per un'email a una persona è la forma giusta, e l'API continua a
restituire `code`, `name` e `label` separati, così un client che vuole il codice lo ha.

Il test che pretendeva «None» è stato **rovesciato, non cancellato**: la forma vecchia
resta scritta accanto, così chi legge vede che cosa è cambiato e perché. E il test dello
`/` ha dovuto cambiare appoggio: si basava sul fatto che il codice comparisse
nell'etichetta, e con il nome che vince non poteva più accorgersi di un troncamento —
passerebbe anche con l'implementazione rotta. Il corpus porta ora un rack e un sito con lo
`/` nel codice **e senza nome**, che è l'unico caso in cui un troncamento tornerebbe
visibile, più una controprova che riproduce il vecchio impacchettamento e verifica che
dia un risultato diverso.

#### 8.50.10 Migrazione, versione della mappa, e il punto cieco che si allarga

**Migrazione 0013.** Due colonne su `inventory_devices`, di due specie diverse:

  - `presenza text` — colonna **tipizzata**, con una chiave nel documento. Torna nel
    documento come `stato`. **Nessun `CHECK`** sul vocabolario: l'inventario reale arriva
    da fogli di calcolo e contiene sempre qualche valore fuori elenco, e un vincolo qui
    farebbe RIFIUTARE alla proiezione un documento che la fase 1 accetta — cioè
    cambierebbe il comportamento del prodotto di straforo. `validate_model` lo segnala
    come avviso, e il messaggio dice la conseguenza operativa: la capacità lo conterà come
    PRESENTE, perché solo «rimosso» libera lo slot. **Nessun indice**: la domanda è «quali
    NON sono rimossi», vera per la quasi totalità delle righe;
  - `ip_addr inet` — colonna **derivata**, come `garanzia_date`. Indice parziale
    (`WHERE ip_addr IS NOT NULL`).

⚠ Perché `inet` va bene adesso e nella 2E era stato escluso. La 2E lo scartò per una
ragione buona: `inet` ha una grammatica PROPRIA — accetta `10.1` come `10.0.0.1` e
`10.0.0.0/8` come indirizzo — e usarla avrebbe aggiunto semantica che il prodotto non ha,
in un solo posto dei tre. Quell'obiezione riguardava l'idea di far interpretare a
PostgreSQL il testo dell'utente, **e resta valida**.

Qui non succede: la colonna la scrive `domain.parse_address`, e in `ip_addr` arriva solo
una forma già canonica. PostgreSQL riceve indirizzi normalizzati e li **confronta**, che è
quello che sa fare meglio di qualunque espressione. Sostituisce l'espressione della 2E —
nove `btrim` e otto `split_part` per riga, valutata a ogni ricerca — con due confronti su
una colonna indicizzata.

**`MAPPER_VERSION` 1 → 2.** È il caso per cui quel numero esiste. Le righe scritte dalla
mappa vecchia riassemblerebbero lo STESSO documento — quindi lo stesso digest — mentre
`presenza` starebbe in `extra` e `ip_addr` sarebbe vuota: la vista Capacità non troverebbe
la presenza e la ricerca non troverebbe l'indirizzo. Il digest non può accorgersene
(§8.44), la versione della mappa sì.

Conseguenza operativa: **dopo la migrazione la proiezione si dichiara NON attuale** finché
non gira `project.py --rebuild`. Le rotte di lettura rispondono 503 con
`projection_not_current`, che è un errore **con un rimedio** — servire righe in cui la
presenza non esiste sarebbe la risposta sbagliata. Non è una migrazione di dati: le due
colonne nascono NULL e le riempie la ricostruzione. Nessun `UPDATE` di massa, nessuna
riscrittura del documento. Un controllo statico lo pretende dalla migrazione.

⚠ **Il punto cieco delle derivate si allarga, e va detto.** La 2F l'ha chiuso per il
worker: `validate_model` dentro lo stesso snapshot, una volta al giorno (§8.47.4). Le tre
interrogazioni interattive **non** possono pagare quel costo a ogni richiesta, e adesso
c'è una derivata in più: una `ip_addr` corrotta a mano darebbe ricerche sbagliate senza
che niente lo dica, perché il digest è cieco alle derivate per costruzione. La rete di
sicurezza resta `project.py --verify`, ed è dichiarata nel docstring del modulo invece di
essere scoperta.

#### 8.50.11 Prestazioni misurate

Mediana di 15 esecuzioni, seed di produzione con date, indirizzi e presenze
iniettati in modo deterministico. Le scale si ottengono moltiplicando i DISPOSITIVI, con
le date distanziate: senza, una finestra di 90 giorni conterrebbe una frazione costante
delle righe e il piano resterebbe sequenziale per selettività invece che per scala —
misurerei la cosa sbagliata.

| misura | produzione (86) | ×10 (860) | ×30 (2 580) |
|---|---|---|---|
| ricerca testuale | 5,2 ms | 13,6 ms | 18,9 ms |
| ricerca indirizzo esatto | 3,0 ms | 1,9 ms | 2,9 ms |
| ricerca CIDR IPv4 /16 | 4,9 ms | 13,9 ms | 19,7 ms |
| ricerca CIDR IPv6 /32 | 2,1 ms | 6,3 ms | 12,4 ms |
| capacità | 4,7 ms | 5,5 ms | 8,5 ms |
| scadenze | 5,4 ms | 13,8 ms | 15,8 ms |
| scadenze filtrate | 3,0 ms | 3,6 ms | 6,3 ms |

⚠ **Che cosa dicono e che cosa non dicono.** A questa scala i tempi sono dominati dal
costo FISSO di una risposta — la revisione, l'apertura dello snapshot, la costruzione dei
dizionari in Python — non dal lavoro sul database. Si vede da due cose: la ricerca per
indirizzo esatto NON cresce (3,0 → 1,9 → 2,9 ms: la variazione è rumore), e la capacità
cresce di quattro millisecondi mentre le righe si moltiplicano per trenta. Sono numeri
che dicono «va bene», non «va bene perché la query è veloce».

Il numero che dice qualcosa sul lavoro fatto è la CLAUSOLA, isolata dal resto:

| | produzione | ×10 | ×30 |
|---|---|---|---|
| confronto `inet` su colonna (2G) | **0,4 ms** | **0,5 ms** | **0,7 ms** |
| espressione `btrim`/`split_part` (2E) | 1,0 ms | 4,4 ms | 11,5 ms |

⚠ La differenza non è «più veloce del 16×»: è **piatta contro lineare**. L'espressione
della 2E si valutava su ogni riga — nove `btrim` e otto `split_part` — quindi il costo
seguiva il numero di dispositivi; il confronto fra due `inet` su una colonna indicizzata
non lo fa. A 2 580 dispositivi sono 11 millisecondi risparmiati su una query che ne
impiega venti, cioè non un problema che qualcuno aveva; a 100× sarebbe la differenza fra
una ricerca e un'attesa.

⚠ **E l'indice si usa dove serve, non sempre.** Il piano dipende dalla selettività, ed è
giusto che dipenda:

    CIDR /16 — 1 950 righe su 2 580 (75%)
      Seq Scan on inventory_devices  (actual time=0.014..0.651 rows=1950)
        Filter: ((ip_addr >= '10.0.0.0') AND (ip_addr <= '10.0.255.255'))
      Execution Time: 0.800 ms

    indirizzo ESATTO — 1 riga su 2 580
      Bitmap Heap Scan  (actual time=0.020..0.020 rows=1)
        -> Bitmap Index Scan on ix_device_ip_addr  (actual time=0.015..0.015 rows=1)
      Execution Time: 0.060 ms

Su un predicato che combacia col 75% delle righe un indice è **più lento** della
scansione, e il pianificatore ha ragione a ignorarlo. La domanda per cui l'indice esiste
è l'indirizzo esatto — quella che la 2G ha aggiunto — e là lo usa, con 3 buffer letti
invece di 58. Misurare solo il CIDR avrebbe portato alla conclusione sbagliata: «l'indice
non serve».

**Il worker.** Il percorso del worker non è cambiato nella sua parte costosa: la
validazione del modello resta il termine dominante e superlineare misurato in §8.47.6
(305,9 ms a ×30). La 2G aggiunge il filtro sui dismessi — che RIDUCE le righe lette — e
una colonna derivata in più da verificare, il cui costo è lineare e sotto il rumore.

**Il frontend.** `rackCapacity` sostituisce due `SUM(h)` e un vettore di occupazione. Il
vettore era `new Array(rk.u + 1)` per rack, cioè un'allocazione proporzionale
all'ALTEZZA; la fusione di intervalli è proporzionale ai DISPOSITIVI. Sul seed reale
(102 rack, 86 dispositivi) la differenza non è osservabile; sul corpus
`oversized-integers` la versione vecchia esauriva la memoria della scheda e questa no,
che è una differenza di specie e non di grado.

#### 8.50.12 Come è verificato

**Suite e conteggi.**

| | prima (2F) | dopo (2G) |
|---|---|---|
| test Python | 2 619 | **3 055** |
| controlli statici | 313 | **337** |
| controlli del contratto (JavaScript) | — | **541** |
| test di identità (JavaScript) | 120 | 120 |

Tutte verdi, zero salti. I 436 test in più sono, in ordine di peso:
`test_domain_contract.py` (il contratto in Python), `test_domain_sql_pg.py` (il
contratto in SQL), più i corpora estesi di `test_worker_sql_pg.py` e le riscritture di
`test_queries_pg.py`.

⚠ **Una lezione sul metodo di misura, e vale la pena scriverla.** Due volte, durante
questa fase, ho letto «suite verde» da un comando della forma
`pytest … | grep -E "^FAILED" | head`, e due volte era falso: la pipeline mascherava il
codice di uscita di pytest e l'output non arrivava. La suite aveva **63 fallimenti**. Il
modo affidabile è catturare dentro il container e leggere il file:

    python -m pytest -q > /tmp/out.txt 2>&1; echo "EXIT=$?"; grep -c '^FAILED' /tmp/out.txt

È il genere di errore che rende inutile tutto il resto: un rapporto che dice «verde»
sulla base di una misura rotta è peggio di nessun rapporto.

**Il contratto, e come si accorge di una divergenza.** `fixtures/domain/*.json` è
eseguito da tre suite indipendenti. Ha trovato, durante la fase, difetti che nessuna
rilettura avrebbe trovato:

| trovato dove | che cos'era |
|---|---|
| corpus differenziale degli indirizzi | i gruppi IPv6 renderizzati **alla rovescia** in JavaScript (`::1` → `1::`); i valori numerici combaciavano, solo il testo lo mostrava |
| corpus differenziale | `1.2.3.4::` accettato in JavaScript, rifiutato da `ipaddress` |
| corpus differenziale | forma puntata usata anche per gli IPv4-*compatible* invece dei soli *mapped* |
| fuzzing (4 567 forme) | zeri iniziali nell'IPv4 **incorporato** in un IPv6: accettati di qua, rifiutati di là |
| corpus delle date | `\d` in Python combacia con le cifre Unicode: `２０２７-０３-１５` era una data per il backend e non per il frontend — **difetto preesistente** |
| corpus delle date | l'anno `0000`: valido in JavaScript, non rappresentabile da `datetime.date` |
| corpus di capacità | quattro mie attese sbagliate a mano (`largestFreeRun` calcolato male in tre casi) e un caso che **non distingueva** `SUM(h)` dagli slot distinti |
| `test_domain_sql_pg.py` | `inet_out` stampa la maschera (`10.0.0.1/32`): serve `host()`, non `::text` |
| `test_domain_sql_pg.py` | PostgreSQL scrive `::10.0.0.1` dove Python scrive `::a00:1` — stesso indirizzo, scrittura diversa |

Le ultime due riguardano solo il testo e non il comportamento del prodotto: il confronto
avviene fra valori `inet` e la rilettura passa da `psycopg`. Sono FISSATE in un test
invece di essere attenuate, così se un giorno qualcuno mettesse `host(ip_addr)` in una
risposta scoprirebbe da lì che non è la forma canonica di Python.

**I test che possono diventare rossi se il difetto torna** (§13 del requisito). Un test
che verifica il comportamento giusto non basta: deve poter fallire quando il vecchio
comportamento ritorna. Per ognuno degli otto difetti dell'elenco:

| difetto | come il test se ne accorge |
|---|---|
| falso positivo dell'IP esatto | `test_an_exact_ip_no_longer_matches_its_own_prefix` pretende che `10.0.0.1` NON trovi `10.0.0.100`, **e** la controprova che come sottostringa combaciava |
| ramo vuoto dei dismessi in capacità | le fixture di presenza incrociano stato e presenza; un test pretende che per ogni stato esistano entrambe le presenze |
| `SUM(h)` contro gli slot distinti | le fixture riportano `sumH` dove differisce, e un test pretende `sumH != usedU` su ognuna: un corpus in cui coincidono non protegge |
| sentinella `—` | `test_lo_sql_separa_la_fila_non_impostata_da_quella_che_vale_trattino` pretende TRE gruppi, di cui due con la stessa etichetta |
| rollover di `new Date` | il test JavaScript verifica che `new Date("2027-02-30")` **scorra davvero** al 2 marzo, e che il contratto la rifiuti |
| contesto impacchettato su `/` | il corpus porta un rack e un sito con lo `/` nel codice **e senza nome** — l'unico caso in cui un troncamento tornerebbe visibile — più una controprova che riproduce l'impacchettamento |
| arrotondamento diverso | `test_la_percentuale_non_usa_l_arrotondamento_di_python` pretende che `round()` dia un risultato DIVERSO su almeno un caso |
| hardware rimosso che occupa | fixture `RIMOSSO` e `dismesso + rimosso`, con `sumH` dichiarato per mostrare la differenza |

**Sul campo, sullo stack vero.** Stack completo in Compose, seed di produzione,
migrazioni e `--rebuild`, 24 controlli sulla semantica della 2G più le suite esistenti.

    project.py --verify
      mappa        versione 2
      fedeltà      OK: le tabelle riassemblano la versione che dichiarano
      attualità    OK: rispecchia la testa, con una mappa supportata

⚠ **La dimostrazione che vale più delle altre.** Due dispositivi nello STESSO rack, con
la STESSA data di scadenza, nello stesso giorno — uno `attivo`, uno
`dismesso + presente`:

    WORKER (azionabili):
      fw-01        garanzia  2026-08-30   10gg  [Pomezia — G0 / Backend / Rack R01 — Core]

    VISTA Scadenze (ispettiva):
      fw-01        garanzia  2026-08-30   10gg  stato=attivo    presenza=presente  notifiable=True
      sw-core-01   garanzia  2026-08-30   10gg  stato=dismesso  presenza=presente  notifiable=False

    totali della vista: {expired: 0, warning: 2, future: 0, notifiable: 1}

Le due domande danno risposte diverse sugli stessi dati, e la risposta della vista
**dice** perché. Si legge anche la conseguenza di §9: il contesto è
«Pomezia — G0 / Backend / Rack R01 — Core», cioè i NOMI, e l'em dash dentro un nome
arriva intatto — nessuna stringa impacchettata e nessuno spezzamento.

E la presenza fisica, su un rack reale del seed:

| | prima | dopo |
|---|---|---|
| `R01` | 5/45 U, 5 dispositivi, 0 rimossi | **4/45 U**, 5 dispositivi, **1 rimosso** |

Un apparato marcato `rimosso` ha liberato la sua unità **senza essere cancellato**: il
conteggio dei dispositivi non cambia, `removedCount` sale a 1. È esattamente ciò che §8
chiede — l'hardware portato via resta nell'inventario per il riscontro incrociato, e
smette di occupare spazio.

Verificato inoltre sullo stack: `10.0.0.1` trova esattamente chi ha quell'indirizzo e
nessun prefisso; il CIDR `/24` è un sovrainsieme; nessun rack in modalità indirizzo; la
ricerca per `presenza` e per `stato` trova il dispositivo marcato; `?stato=dismessi` al
plurale è 422; tutte le percentuali coincidono con l'aritmetica intera HALF-UP; il rack
`CS-Q01` la cui fila **è** «—» esce come gruppo `assigned: true` distinto.

⚠ Un passo dimenticato che vale la pena scrivere: `handoff/domain.js` non era
nell'allowlist di nginx né nella `COPY` del `Dockerfile` del web. Il modulo sarebbe
stato 404 nel browser — l'applicazione non è servita da un bundler, ma da un elenco di
file esplicito (§6), e un file nuovo va aggiunto a mano in tre posti. Trovato provando
sullo stack, non da un test: nessuna suite Python può accorgersene.

**Mutazioni: 29 applicate, 29 intercettate.** Ogni mutazione gira contro quattro
strati, dal più economico al più costoso, e si ferma al primo che la prende — così il
rapporto dice anche DOVE è stata presa:

| strato | quante |
|---|---|
| contratto Python (`test_domain_contract.py`) | 17 |
| controlli statici | 5 |
| suite su PostgreSQL | 5 |
| contratto JavaScript | 2 |

Le mutazioni sono i difetti dell'elenco §13 reintrodotti a mano: `SUM(h)`,
`Math.round`, `round()` di Python, la sentinella `—`, `new Date`, il filtro sui
dismessi in entrambi i versi, l'indirizzo esatto che non è una forma, le famiglie che
si mescolano, gli elenchi come etichette, il filtro fuori vocabolario che dà zero
righe invece di 422.

⚠ **Due mutazioni sono state RIFATTE**, e in entrambi i casi perché la mutazione non
riproduceva il difetto:

  1. `row_group` in JavaScript, mutando **un solo ramo**. Le due chiavi restavano
     diverse comunque, quindi la mutazione sfuggiva senza che ci fosse niente da cui
     sfuggire. Il difetto originale — `rk.row || '—'` — collassava ENTRAMBI i rami
     sull'etichetta, e solo mutandoli entrambi il test diventa rosso. «Mutazione più
     piccola» sembra sempre «mutazione migliore», e qui la minima non era il difetto;
  2. lo stesso errore, nella fase 2F, su una mutazione della guardia della proiezione.
     Due volte lo stesso inciampo è un modo di lavorare, non una distrazione: **una
     mutazione va scritta guardando il difetto, non il codice**.

⚠ **E un incidente dello strumento, che ha invalidato sei esiti.** Lo strumento
fotografa i file all'avvio e ripristina la fotografia dopo ogni mutazione; ho corretto
`_text_match` mentre girava, la correzione è stata ripristinata via, e da quel momento
una fixture era rossa per OGNI mutazione — quindi ogni «intercettata dalla suite
PostgreSQL» era inattendibile. Riesaminati uno per uno e rieseguito il giro intero da
pulito. Due rimedi, scritti nello strumento: il ripristino passa da `git checkout`
(resistente anche a un'uccisione a metà, che è precisamente come ho lasciato una
mutazione nel codice una volta) e il giro pretende un albero già committato.

Il difetto che era rimasto nel codice, in quell'occasione, l'ha trovato il **controllo
di integrità** del giro successivo — «codice intatto» prima della prima mutazione. È il
controllo che sembra cerimoniale finché non serve.

**Suite esistenti, tutte verdi sullo stack:** `smoke-test.py`,
`proxy-security-test.py`, `browser-e2e-test.py` (23 controlli, compreso «nessun errore
JavaScript non gestito», che è ciò che dimostra che `domain.js` si carica e i percorsi
di rendering funzionano).


### 8.51 Fase 2H: il frontend chiede invece di calcolare

Dalla 2G il prodotto ha **una** semantica, scritta in due linguaggi e fissata da un
contratto in dati. Il frontend però continuava a calcolare da sé: ricerca, capacità e
scadenze giravano nel browser, e l'unica garanzia era che il loro risultato COINCIDESSE
con quello delle rotte SQL. La 2H toglie il calcolo e lascia la domanda.

⚠ Quella garanzia è la ragione per cui questa fase è stata noiosa, ed era lo scopo di
tenerle separate: nessun numero sullo schermo è cambiato passando al server. Se le due
cose fossero avvenute insieme, una differenza non si sarebbe potuta attribuire.

#### 8.51.1 Il limite dichiarato dell'altezza di un rack (voce 16 del registro)

Prima della migrazione, la pulizia del registro. La voce 16 diceva: `rack.u` è un intero
JSON senza massimo, la colonna della proiezione è `integer`, quindi un rack da tre
miliardi di unità viaggia in `extra` e la colonna resta NULL — e da lì nascono **due
numeri diversi** per la stessa domanda. Era dichiarata e non chiusa.

Adesso è chiusa nel modo in cui si chiudono i limiti: **non lasciando entrare il dato**.

    domain.RACK_U_MIN = 1
    domain.RACK_U_MAX = 2147483647      # l'`integer` della proiezione
    → `rack_u_out_of_range`, dal cancello del documento, prima di persistere

Tre scelte da leggere insieme, perché la regola si capisce solo dalle tre:

**Dove.** In `validate_normal_document`, non in `validate_model`. Quest'ultima serve a
due cose — fare da cancello al `PUT` e provare l'INTEGRITÀ della proiezione — e un `u`
fuori scala non è una proiezione rotta: la proiezione conserva il valore in `extra`,
fedelmente. Metterlo là avrebbe fatto dichiarare «incoerente» una proiezione sana, e un
dato storico avrebbe potuto far rispondere 503 a delle letture che funzionano. Il
cancello vieta di scriverne di nuovi; l'avviso `carried_verbatim` continua a descrivere
quelli che esistessero già.

**Che cosa.** Il «no» copre un solo caso: un INTERO fuori intervallo. Un `u` assente
passa (il default canonico mette 45); un `u` non intero passa — `'45'`, `4.5`, `true` —
e non per indulgenza: `_as_int` li rifiuta e la colonna resta NULL, quindi SQL e modello
puro vedono ENTRAMBI un rack senza altezza. Nessuna divergenza, quindi niente da
rifiutare in nome di questa regola. La regola non dice «`u` deve essere un intero», dice
«`u` non deve poter significare due numeri diversi».

**Che cosa NON si fa.** Non si passa a `bigint`. Sarebbe cambiare il tipo di una colonna
— quindi la versione della mappa e una ricostruzione — per un dato che l'interfaccia non
può produrre (il form stringe a 1..60) e che nel browser esaurisce la memoria della
scheda.

⚠ **Il prezzo, dichiarato.** Due corpora condivisi portavano un'altezza che ora non è
salvabile: `empty-zero-false` aveva `u: 0` e `oversized-integers` aveva `u: 3 000 000
000`. Sono stati portati dentro il limite, NON esclusi: quei documenti esistono per
difendere gli zeri espliciti e gli interi che una colonna non tiene, e escluderli
avrebbe perso quella copertura per intero. L'intero enorme è passato sullo slot di un
DISPOSITIVO (`u: -3 000 000 000`), che è la stessa colonna `integer` sfondata dal lato
opposto; lo zero esplicito resta su cinque campi. E l'adattamento è **verificato**:
`test_l_altezza_adattata_era_davvero_da_adattare` pretende che l'originale sia rifiutato
e per quel codice — se un giorno il limite si alzasse, quel test diventa rosso e dice che
l'adattamento non serve più, invece di restare una modifica silenziosa.

Conseguenza: le fixture di parità della 2E sono state rigenerate con la catena a tre
passi. Il diff è di 376 righe aggiunte e 20 togliete, e le aggiunte sono quasi tutte
`presenza: "presente"` — la canonicalizzazione della 2G, che quei corpora non avevano mai
visto perché non erano stati rigenerati allora.

#### 8.51.2 Le due regole, in un posto solo: `handoff/queries.js`

Chiedere un calcolo a un server mentre l'utente continua a lavorare introduce due
problemi che non esistevano quando il calcolo era locale. Nessuno dei due è difficile;
entrambi sono invisibili finché la rete è veloce, che è il motivo per cui vanno scritti
una volta e non cinque.

**Regola 1 — una risposta vecchia non sovrascrive una nuova.** L'utente digita `ov-`,
poi `dism-presente`. Se la prima risposta arriva dopo la seconda, l'elenco mostrato
risponde a una domanda che nessuno sta più facendo, e niente sullo schermo lo dice. Si
risolve con un contatore di generazione PIÙ l'annullamento: il contatore decide chi ha
diritto di scrivere il risultato, `AbortController` evita di pagare una risposta che
verrà scartata. Serve il contatore anche con l'abort, perché fra `abort()` e il rifiuto
della promessa passa del tempo.

**Regola 2 — un risultato di una revisione non si mescola con un'altra.** Ogni risposta
porta `version` e `sha256` dell'inventario su cui è stata calcolata. Se non combaciano
con quelli del documento in memoria, il risultato descrive un inventario diverso da
quello sullo schermo. ⚠ Il confronto è su **entrambi** i valori: dopo un rollback
esistono due revisioni con lo stesso NUMERO e contenuto diverso, ed è esattamente il caso
in cui un client sbaglierebbe in silenzio.

Quando divergono: si ricarica l'inventario per il cammino già esistente e si riprova
**una** volta. Se ancora non combacia, si dichiara il disaccordo e non si mostra niente.

⚠ **Il ciclo che questo ha prodotto, e come è stato trovato.** La prima stesura chiudeva
il ciclo dentro `queries.run` con `maxReloads`, e sembrava sufficiente. Non lo era: il
ricaricamento chiamava `_aggiornaInterrogazioniAperte`, che RILANCIAVA la stessa
interrogazione, che riceveva di nuovo un disaccordo, che ricaricava. Un ciclo indiretto,
con `maxReloads` che faceva regolarmente il suo lavoro dentro ogni singola chiamata.
L'ha trovato il test del browser sulla revisione, che intercetta la risposta e ne
falsifica la versione: la vista mostrava i numeri vecchi invece dell'errore, e nel log
dell'API si vedeva la fila di richieste. La correzione è di una riga —
`_loadInventory({ perRiconciliare: true })` non rilancia le interrogazioni — e la ragione
sta scritta accanto: chi ha chiesto il ricaricamento riprova da sé, e rilanciare al suo
posto significa non arrendersi mai.

#### 8.51.3 L'architettura scelta, e cosa resta locale

⚠ **Non zero calcolo locale, uno.** Il requisito (§6) chiede che non ci sia più di UNA
implementazione di capacità nel frontend, non che non ce ne sia nessuna, e la differenza
è pratica: il pannello del rack e la dashboard si ridisegnano a ogni trascinamento del
mouse, e una richiesta per rack sarebbe un'esplosione di richieste per disegnare una
barra.

| chi | come | perché |
|---|---|---|
| vista Capacità | `GET /api/inventory/capacity` | è una vista, si apre e si legge |
| vista Scadenze | `GET /api/inventory/expiries` | idem, e le date le interpreta il backend |
| ricerca globale | `GET /api/inventory/search` | idem |
| vista Dismessi | `GET …/search?stato=dismesso` | è la ricerca finale con un filtro |
| anteprima avvisi | `…/expiries?warningDays=N` → `totals.notifiable` | è la domanda del worker |
| pannello del rack | `DOM.rackCapacity` | si ridisegna a ogni interazione |
| dashboard | `DOM.rackCapacity` | idem, ed è la vista iniziale |
| export XLSX/CSV | `DOM.rackCapacity` | è client-side per costruzione |
| pastiglia «scaduta» nel pannello del rack | `DOM.parseExpiry` via `scadLevel` | una richiesta per dispositivo per colorare un'etichetta |
| colore delle celle garanzia/supporto nell'inventario tabellare | idem | idem, su una tabella locale |

Una funzione, tre chiamanti, e le fixture language-neutral a garantire che concordi con
lo SQL. In più un test del browser confronta i due sullo stesso inventario, rack per
rack: è la prova che §14 chiede, e non è una frase — se divergessero, quel test dice su
quale rack.

⚠ **Le ultime due righe sono una deviazione, e va dichiarata.** §13 chiede di togliere
«l'interpretazione delle date della vista Scadenze», e quella è tolta: la vista non
chiama più nessun parser. Restano due usi di `DOM.parseExpiry` che non sono quella
vista — la pastiglia «SCADUTA» accanto a un dispositivo nel pannello del rack, e il
colore di una cella nell'inventario tabellare. Sono presentazione di un rack e di una
tabella locale, e passano dall'UNICO parser del prodotto: non possono divergere dalla
risposta dell'endpoint, perché è lo stesso codice che il backend usa (le fixture lo
fissano da entrambi i lati). Farli passare dall'endpoint significherebbe una richiesta
per dispositivo per colorare un'etichetta.

**Divisione di lavoro: l'interrogazione dice QUALI, il documento dice COME SI SCRIVE.**
Le viste Scadenze e Dismessi leggono dalla risposta tutto ciò che è INTERPRETAZIONE
(`level`, `daysRemaining`, `notifiable`, `stato`, `presenza`, le etichette della catena
del dominio) e dal documento già in memoria i campi puramente descrittivi (referente,
modello, seriale, note). L'alternativa era allargare la risposta a ogni colonna che una
vista mostra, e la risposta sarebbe cresciuta con l'interfaccia. L'`uid` è il ponte, e la
regola 2 garantisce che le due sorgenti siano la stessa revisione.

**Estensione di API (una sola, dichiarata).** `GET /api/inventory/search` accetta
`stato` e `presenza`: stesso vocabolario, stessi default, stesso `_reject_unknown` delle
scadenze. Nessuna interpretazione nuova. Due conseguenze decise: con un filtro attivo i
RACK non partecipano (un rack non ha stato né presenza: mostrarlo in un elenco filtrato
significherebbe mostrare una riga che il filtro non ha guardato), e una `q` vuota diventa
legittima (con un filtro la domanda è stata posta). Nessuna migrazione, nessun indice,
nessun privilegio nuovo.

#### 8.51.4 La vista Dismessi: una lettura, non un archivio

Nessuna tabella nuova e nessun endpoint proprio — un controllo statico lo pretende. È
`search?stato=dismesso`, e distingue le due combinazioni che la 2G ha reso esprimibili:

    dismesso + presente    fuori servizio, ANCORA installato — occupa spazio fisico
    dismesso + rimosso     portato via — non occupa, e lo slot mostrato è «l'ultimo»

Mostra nome, codice, seriale, modello, referente, ultimo sito/sala/rack, presenza,
garanzia e supporto; filtra per presenza; cerca con la stessa semantica di sempre. Nessuna
cella può contenere `undefined`, `null` o `None`: un valore dell'implementazione in una
colonna di riscontro incrociato è peggio di una cella vuota, perché sembra un dato.

**La presentazione dei rimossi (§10).** Il rack conserva la relazione — è il motivo per
cui questi record non si cancellano — ma la vista FISICA mostra ciò che è installato: le
fasce sulla pianta 3D non disegnano più un apparato rimosso, perché disegnarlo direbbe
che quello spazio è pieno mentre la capacità dice che è libero. Il pannello del rack lo
mostra tratteggiato con l'etichetta «RIMOSSO» e conta i rimossi a parte («1 rimosso, non
occupa»), che è l'informazione senza la quale il conto dei dispositivi e le U occupate
sembrano non tornare. `rack_uid` non si cancella per nascondere una riga.

#### 8.51.5 Che cosa è stato tolto

    ricerca locale sull'albero            → l'endpoint
    `DOM.parseAddressQuery/addressMatches` nel frontend  → l'endpoint
    `DOM.deviceMatches/rackMatches`       → l'endpoint
    calcolo della vista Capacità          → l'endpoint
    `DOM.rowGroup/compareRowGroups` nel frontend  → l'endpoint
    discesa dell'albero delle Scadenze    → l'endpoint
    anteprima delle notifiche calcolata   → `totals.notifiable`
    la seconda forma della query (`toLowerCase`)  → non serve più a nessuno

⚠ **Nessun ripiego locale, in nessun ramo.** Se un endpoint risponde 503 la vista lo
DICE, con un messaggio che distingue `projection_not_current` (manutenzione dimenticata,
si ripara con `--rebuild`) da `projection_inconsistent` (dato da guardare). Calcolare i
numeri nel browser «per sicurezza» sarebbe indistinguibile da una vista che funziona, e
nasconderebbe un guasto della proiezione proprio a chi lo può riparare. È anche il modo
esatto in cui la duplicazione tornerebbe: nessuno scriverebbe «una seconda semantica»,
tutti scriverebbero «una rete di sicurezza». Un controllo statico e un test del browser
lo pretendono.

I controlli statici rovesciati sono tre, e la loro storia è la storia della fase 2:

| controllo | 2D/2E/2G | 2H |
|---|---|---|
| «il frontend non sa che la proiezione esista» | pretendeva l'ignoranza | pretende che non conosca la FORMA, ma sappia quando è rotta |
| «il frontend non è ricablato alle rotte» | pretendeva che non lo fosse | pretende che chiami tutte e tre |
| «il frontend calcola in locale» | pretendeva il calcolo condiviso | pretende UNA sola implementazione locale |

#### 8.51.6 Le misure, nel browser (§16 del requisito)

`tools/queries-perf.py`, sul seed di **produzione** (3 siti, 6 sale, 102 rack, 86
dispositivi), attraverso nginx e TLS. Non è un test e non ha soglie: è una misura, e
serve a distinguere «millisecondi» da «secondi» e «una richiesta» da «cento».

| | richieste | latenza | risposta |
|---|---|---|---|
| avvio (contatore scadenze) | 1 | 16 ms | 258 B |
| ricerca «srv» (3 battute) | **1** | 19 ms | 8 701 B (15 esiti) |
| ricerca «s» (50 esiti, il limite) | 1 | 22 ms | 24 883 B |
| **vista Capacità, 102 rack** | **1** | 23 ms | 27 708 B |
| vista Scadenze | 1 | 18 ms | 258 B |
| vista Dismessi | 1 | 17 ms | 193 B |
| trascinamento sulla pianta (40 movimenti) | **0** | — | — |
| `GET /api/inventory` (per confronto) | 1 | 22 ms | 40 449 B |

Dal clic al contenuto sullo schermo: **34–35 ms** per tutte e tre le viste, di cui una
ventina è la richiesta.

Le tre righe che rispondono alle domande del requisito:

  - **nessun N+1.** La vista Capacità disegna 102 rack con **una** richiesta. Era il
    difetto classico da evitare, e si vede contando;
  - **il ritardo di digitazione funziona.** Tre caratteri, una richiesta. Su
    «dism-presente» (tredici caratteri) il test dell'interfaccia pretende `1 <= n <= 3`;
  - **l'interazione sulla pianta non chiede niente.** Zero richieste in quaranta
    movimenti del mouse: è la ragione per cui il pannello del rack e la dashboard
    continuano a usare l'aiuto locale, e questa riga è la misura di quella scelta.

⚠ Due numeri da leggere con attenzione, entrambi onesti e nessuno dei due rassicurante:

  - la risposta della Capacità pesa **il 68 % del documento intero** (27,7 KB su 40,4).
    A questa scala non è un problema — sono trenta millisecondi — ma non è una vittoria
    di banda: è una richiesta in più che porta un sottoinsieme già noto. Il guadagno
    della migrazione non è il traffico, è che il numero è **uno**;
  - la vista Scadenze risponde in 258 byte perché il seed di produzione non ha
    **nessuna** scadenza. La misura è quindi il costo di un elenco vuoto, e lo dice: è
    la voce 14 del registro che si ripresenta come un numero.

Nessuna cache lato client, di proposito: §16 la ammette solo se una misura la richiede,
e nessuna di queste la richiede.

#### 8.51.7 La verifica

| che cosa | quanto | dove |
|---|---|---|
| suite Python | **3092** test | `backend/tests/` |
| controlli statici | **346** | `tools/storage-config-test.py` |
| contratto di dominio, JavaScript | **560** controlli | `tools/domain-contract-tests.mjs` |
| contratto del client delle interrogazioni | **45** controlli | `tools/query-client-tests.mjs` |
| identità, JavaScript | **120** test | `tools/identity-tests.mjs` |
| **interfaccia nel browser, via nginx** | **67** controlli | `tools/queries-ui-test.py` |
| catena completa nel browser | invariata | `tools/browser-e2e-test.py` |
| proxy, smoke, impostazioni, foto, utenze, registro | invariate | `tools/*-test.py` |

⚠ I sessantasette controlli del browser sono **portanti**, non decorativi: provano cose
che nessuna suite può provare, perché vivono fra l'interfaccia e il server. In
particolare: che una risposta superata non finisca sullo schermo (query A lenta, query B
veloce, deve restare B), che un risultato di un'altra revisione NON si mostri, che un
`PUT` VERO durante una richiesta in volo porti alla riconciliazione e ai numeri della
revisione nuova, che un 503 non faccia tornare il calcolo locale, e che l'aiuto locale e
l'endpoint diano lo stesso «U usate» rack per rack sullo stesso inventario.

⚠ Due di quei controlli rispondono a una domanda che nessun altro strato pone: **il
modulo che nginx SERVE soddisfa il corpus?** Il contratto in node gira sul file su
disco, le suite Python sul modulo importato; nessuno dei due si accorgerebbe di un
`domain.js` servito da un'immagine web vecchia, o di un modulo dimenticato
nell'allowlist — che in questo progetto è già accaduto due volte. Il test esegue quindi
ottantasei casi del corpus DENTRO la pagina, sul modulo che il browser ha caricato,
passando le fixture come argomento (non sono servite da nginx, e non devono esserlo).

⚠ **Due difetti veri, trovati dal browser e da nient'altro.** Vale la pena dire perché
nessun altro strato li avrebbe visti.

**Il ciclo di ricaricamenti.** Descritto in §8.51.2. Le suite Python non lo vedono
perché il ciclo passa dal componente React; il contratto del client non lo vede perché
lì `reloadInventory` è una funzione finta che non rilancia niente — ed era giusto che
non lo facesse, altrimenti quel test avrebbe misurato il componente invece della
regola. Serviva l'interfaccia vera.

**Una sala con `vani: []` uccideva il render.** `room.vani || [...]`: un array vuoto è
VERO in JavaScript, quindi la lista restava vuota, `vaniG[0]` era `undefined` e il
render moriva con «Cannot read properties of undefined (reading 'x')» — l'intera
applicazione, non solo la pianta. E `vani: []` non è una forma inventata: è il **default
canonico** della sala, quindi qualunque documento che non li dichiari passa la
validazione, viene salvato, e poi non si può più aprire. Il seed di produzione ha i vani
in tutte e sei le sale, ed è la sola ragione per cui nessuno l'aveva mai visto. Il
difetto precede la 2H di molte fasi; l'ha scoperto un documento di prova costruito da
zero, che è precisamente ciò che un seed reale non fa mai.

Più tre errori miei, corretti mentre accadevano e degni di nota perché sono tre modi
diversi di sbagliare:

  - `q=` **vuota veniva omessa** dall'URL (`_qs` salta i valori vuoti — giusto per un
    filtro, sbagliato per un parametro obbligatorio), quindi la vista Dismessi senza
    testo riceveva 422. Nessuna suite Python poteva vederlo: là `search` si chiama con
    `q=""` in Python, e la costruzione dell'URL non esiste;
  - la **latenza** misurata era negativa di un miliardo e ottocento milioni di
    millisecondi: `timing.startTime` di Playwright è un'epoca assoluta e `responseEnd` è
    un offset. Il numero era così assurdo da non poter essere creduto, ed è la sola
    ragione per cui non è finito in un rapporto;
  - «tempo fino al disegno» misurava **la mia attesa**: `click()` più
    `wait_for_timeout(3000)`, e usciva 3050 ms per tutte e tre le viste. Un numero che
    non misura niente è peggio di un numero assente, perché sembra un dato.

#### 8.51.8 Le mutazioni, e la lacuna che hanno trovato

Venti mutazioni, sei strati dal più economico al più costoso:

    1. controlli statici                ~3 s
    2. contratto del client (JS)        ~2 s
    3. contratto di dominio (JS)        ~3 s
    4. contratto di dominio (Python)   ~15 s
    5. suite su PostgreSQL             ~90 s
    6. il browser, dove serve         ~250 s

⚠ **Il primo giro ne ha fatte sfuggire SEI su diciassette**, e questa è la parte del
rapporto che vale più delle altre — perché è l'unica in cui lo strumento ha detto
qualcosa che non sapevo.

| mutazione sfuggita | che cosa significava |
|---|---|
| i rack partecipano a un elenco filtrato | nessun test provava che un elenco filtrato non porti rack |
| lo `stato` si confronta senza il default | nessun test cercava `?stato=attivo` su un dispositivo con `stato: ""` |
| una `q` vuota con un filtro non dà niente | il caso della vista Dismessi senza testo era provato **solo dal browser** |
| un valore fuori vocabolario diventa un elenco vuoto | nessun test pretendeva il 422 su `?stato=dismessi` |
| il cursore non porta i filtri | nessun test riusava un cursore con filtri diversi |
| l'annullamento non annulla più | il contratto provava `cancel()`, non l'annullamento su richiesta nuova |

Cinque su sei sono **la stessa lacuna**: l'estensione di API della 2H — i filtri
`stato` e `presenza` — era verificata soltanto attraverso il test del browser. Un
comportamento provato solo là è provato dove costa più eseguire, dove il rapporto si
legge peggio, e dove un fallimento non dice quale riga di SQL è sbagliata.

La sesta è più sottile e vale la pena scriverla per intero. Il contratto del client
provava l'annullamento **esplicito** (`cancel()`, quando la vista si chiude) e la
proprietà «una risposta superata non si mostra» — che il contatore di generazione
soddisfa da solo. Non provava l'annullamento su **richiesta nuova**: scartare il
RISULTATO e non aspettare la RISPOSTA sono due cose diverse, e i test guardavano solo
la prima. Senza l'abort, digitare tredici caratteri lascia dodici richieste in volo che
il server calcola per intero e il browser scarica. Il test nuovo guarda il SEGNALE della
richiesta precedente, che è l'unico posto dove la differenza si vede.

Aggiunti sette test SQL (`test_domain_sql_pg.py`, sezione 3-bis) e un test JavaScript.
**Secondo giro: 20 mutazioni su 20 intercettate**, e ognuna dallo strato che le
compete — le cinque dei filtri dalla suite PostgreSQL, non dal browser.

⚠ **Le tre mutazioni del frontend le ha prese solo il BROWSER**, ed è la risposta alla
domanda per cui quello strato esiste: far tornare la ricerca a filtrare l'albero
caricato, mostrare un risultato di un'altra revisione, o omettere di nuovo la `q` vuota
dall'URL non rende rosso nessun test Python, nessun contratto e nessun controllo
statico. Se i sessantasette controlli dell'interfaccia non ci fossero, quelle tre
regressioni arriverebbero in produzione senza che niente lo dica.

| strato | intercettate |
|---|---|
| contratto del client (JS) | 6 |
| suite su PostgreSQL | 6 |
| contratto di dominio (Python) | 4 |
| controlli statici | 1 |
| il browser | 3 |

⚠ **Una mutazione è stata intercettata da un BLOCCO**, non da un rosso: togliere il
limite ai tentativi di riconciliazione manda il contratto del client in un ciclo
infinito. Un blocco è una rilevazione legittima — il test non finisce, quindi la suite
non passa — ma costava dieci minuti di attesa col timeout di default. Il timeout dello
strato JavaScript è ora novanta secondi: un giro sano dura due secondi, quindi novanta
sono trenta volte il necessario, e un ciclo si dichiara in un minuto e mezzo.

⚠ **Due lezioni di metodo, dallo strumento e non dal codice.**

La prima: `subprocess.run(timeout=…)` uccide il CLIENT `docker`, non il CONTAINER. Un
contenitore rimasto a girare in un ciclo infinito consuma CPU per ore e rallenta tutto
ciò che viene dopo — l'ho trovato per caso, «Up 3 hours», mentre guardavo altro. E
ripulirlo a mano durante un giro è pericoloso: se si uccide un contenitore che sta
davvero eseguendo uno strato, quella mutazione viene registrata come intercettata da
quello strato, e l'esito è falso senza sembrarlo. Nel giro in cui è successo ho potuto
escluderlo con l'evidenza — la mutazione in corso è stata registrata dalla suite
PostgreSQL, che gira DOPO gli strati JavaScript, quindi i contenitori uccisi non erano
suoi — ma è un ragionamento che non si dovrebbe dover fare.

La seconda, la stessa della 2G e vale ripeterla: **non si modifica il codice mentre lo
strumento gira.** Il ripristino è `git checkout`, quindi una modifica non committata
fatta nel frattempo viene cancellata; e se rompe un test, resta rotta per tutte le
mutazioni successive.

## 9. Ordine di lavoro proposto






1. ~~Vendorizzare React → l'app parte in rete chiusa~~ ✔ **fatto** (§5)
2. ~~Scheletro backend: Compose (reti, volumi, secret), Postgres, Alembic, FastAPI,
   `/api/health` + `/api/ready`, container non-root~~ ✔ **fatto** — vedi `backend/README.md`
3. ~~**Generazione degli `_uid` lato client** (§8.4, §8.12): `crypto.randomUUID()` sui
   percorsi di creazione, fix di `saveDraft` e `manSave`, identità nell'import tabellare,
   rifiuto dei backup JSON legacy, migrazione una-volta-sola del seed, test~~
   ✔ **fatto** — `handoff/identity.js`, `tools/migrate-seed-uids.mjs`,
   `tools/identity-tests.mjs`, `tools/identity-ui-test.py`
4. ~~**Motore di diff identity-aware** (§8.10) e validatore, puri, con fixture condivise e
   test~~ ✔ **fatto** — `backend/app/identity/`, `fixtures/identity/`, `backend/tests/`.
   Deliberatamente NON agganciato a FastAPI né a Postgres: è il passo 6.
4-bis. ~~**Default canonici** (§8.14), **`schemaVersion`** (§8.13) e **politica di
   autorizzazione** pura sugli eventi (§8.15), con fixture e test~~ ✔ **fatto** —
   `backend/app/identity/{canonical,schema}.py`, `backend/app/authz/`, `fixtures/policy/`
5. ~~Auth: users con `disabled_at` (§8.6), sessioni, login, cambio password provvisoria~~
   ✔ **fatto** — `backend/app/auth/`, migrazione `0004_users_sessions`. Resta la gestione
   utenze da parte degli admin (creazione, disattivazione, riattivazione via API).
5-bis. ~~**Schema congelato del documento** (§8.16), **eventi non supportati** distinti da
   quelli ristretti (§8.15), **repository atomico** e migrazione Alembic (§8.17), con test di
   integrazione su Postgres reale~~ ✔ **fatto** — `backend/app/inventory/`,
   `migrations/versions/0002_inventory.py`, `backend/tests/test_repository_pg.py`.
   Senza rotte HTTP: le espone il punto 6.
6. ~~`GET`/`PUT /api/inventory`: contratto congelato (§8.22), mappa degli errori (§8.21),
   attore obbligatorio (§8.20), no-store e limiti di dimensione (§8.24), readiness a tre
   condizioni (§8.23), append-only dai privilegi (§8.19), bootstrap come CLI~~
   ✔ **fatto** — `backend/app/api/`, `backend/scripts/`, `web/`, `0003_runtime_role`
6-bis. ~~Hardening: sessione ristretta con password provvisoria (§8.26), stato mutabile
   riletto, validazione dell'origine (§8.27), limitazione e anti-enumerazione (§8.28), TLS e
   rifiuto di partire in modo insicuro (§8.29), audit dell'autenticazione (§8.25)~~ ✔ **fatto**
6-ter. ~~Gestione delle utenze da parte degli admin (§8.30)~~ ✔ **fatto** —
   `backend/app/api/users.py`, `backend/app/auth/users.py`
6-quater. ~~Correzioni di confine (porte standard §8.31, codice `password_change_required`
   in minuscolo, semantica transazionale §8.32) e **integrazione del frontend** con
   l'API (§8.33)~~ ✔ **fatto** — `handoff/api.js`, `tools/browser-e2e-test.py`.
   Da qui in avanti i dati dell'utente sono durevoli e passano dal server.
6-quinquies. ~~Regressione sulle intestazioni `X-Forwarded-*` (§8.34): nginx le
   sovrascrive, l'API si fida solo del proxy, e la porta dell'API non è più pubblicata~~
   ✔ **fatto** — `tools/proxy-security-test.py`
7. ~~Interfaccia di amministrazione delle utenze su `/api/users` (§8.35)~~
   ✔ **fatto** — `tools/users-ui-test.py`, `tools/run-users-ui-test.ps1`
8. ~~**API e vista del registro** su `GET /api/audit` (§8.36), più il consenso
   esplicito per i test distruttivi (§8.37)~~ ✔ **fatto** —
   `backend/app/audit/`, `tools/audit-ui-test.py`, `tools/destructive_guard.py`
9. ~~`/api/settings` con schema tipizzato e `If-Match`, più l'endpoint di prova invio
   (§8.38), e il client di prova su HTTPS (§8.39)~~ ✔ **fatto** —
   `backend/app/settings/`, `backend/app/notifications/`, `0007_settings`,
   `tools/settings-ui-test.py`. **Senza scheduler**, di proposito.
10. ~~Layout di archiviazione in produzione: volume ancorato al secondo disco,
    preflight che fallisce chiuso, unità systemd con `RequiresMountsFor` (§8.40)~~
    ✔ **fatto** — `deploy/`, `compose.storage-dev.yaml`,
    `tools/storage-config-test.py`, `tools/storage-e2e-test.sh`
11. ~~**Worker delle notifiche di scadenza** con lock, idempotenza durevole,
    digest e recupero (§8.8, §8.41)~~ ✔ **fatto** —
    `backend/app/notifications/{expiry,reminders,digest,worker}.py`,
    `0008_reminders`, servizio Compose `worker`, `fixtures/expiry/`
12. ~~Foto su `/api/photos` + GC (§8.5): riferimenti espliciti per versione,
    validazione e ricodifica delle immagini, deduplicazione sul contenuto, GC nel
    worker con ruolo di database separato, integrazione frontend~~ ✔ **fatto** —
    `backend/app/photos/`, `backend/app/api/photos.py`, `0009_photos`,
    `tools/photos-ui-test.py`
13. ~~**Fase 2A**: schema relazionale (`0010_normalised`) e mappa pura
    documento ↔ modello, con l'invariante del giro completo (§8.42). `GET` e `PUT`
    invariati, niente popola e niente legge le tabelle~~ ✔ **fatto** —
    `backend/app/inventory/relational{,_validate}.py`, `fixtures/relational/`,
    `test_relational_mapper.py`, `test_relational_schema_pg.py`
14. ~~**Fase 2B**: popolamento della sola testa con confronto dei digest e abort
    (§8.42), colonne data derivate, comando esplicito del proprietario~~
    ✔ **fatto** — `0011_projection`, `app/inventory/projection.py`,
    `backend/scripts/project.py`, `test_projection_pg.py`. Le versioni storiche non
    si riscrivono, e niente consuma la proiezione.
14-bis. ~~**Fedeltà numerica dell'istantanea** (§8.16): un documento accettato deve
    essere rileggibile identico da JSONB, o si rifiuta con
    `json_number_not_roundtrippable` prima di qualunque scrittura~~ ✔ **fatto** —
    `app/inventory/json_numbers.py`, `test_json_numbers.py`,
    `test_snapshot_numbers_pg.py`. Chiude il difetto che il confronto dei digest
    della fase 2B aveva reso visibile.
14-ter. ~~**Fedeltà testuale dell'istantanea** (§8.16): valori **e chiavi**, con
    la stessa capability condivisa dalla mappa relazionale~~ ✔ **fatto** —
    `app/inventory/{json_strings,representable}.py`, `test_json_strings.py`,
    `test_snapshot_strings_pg.py`. Chiude il 500 che PostgreSQL restituiva per un
    byte NUL o un surrogato spaiato in un nome.
14-quater. ~~**Password e Argon2id** (§8.43): politica unica (15–128 code point,
    NFC, nessuna composizione, nessuna scadenza), parametri Argon2id pinnati e
    superiori al minimo, lista locale, provvisorie da 192 bit, riscrittura degli hash
    invecchiati~~ ✔ **fatto** — `app/auth/passwords.py`,
    `app/auth/password-blocklist.txt`, `test_passwords.py`,
    `test_password_policy_pg.py`, §13 di `storage-config-test.py`. Verifica e
    irrobustimento: chiude il 503 su un hash illeggibile e quello su un surrogato
    spaiato, e la divergenza fra i tre punti che stabilivano una password.
15. ~~**Fase 2C**: il `PUT` sincronizza le tabelle e inserisce istantanea, audit e
    riferimenti alle foto in una transazione sola~~ ✔ **fatto** (§8.44) —
    `0012_dual_write`, `app/inventory/{projection,digest}.py`, `repository.py`,
    `test_dual_write_pg.py`, §14 di `storage-config-test.py`. Precondizione che
    fallisce chiuso (`projection_not_current`), `mapper_version`, readiness estesa,
    sostituzione integrale sotto la testa bloccata. Nessuno LEGGE ancora la
    proiezione.
16. ~~**Fase 2D**: il `GET` assembla da SQL, solo dopo che la rappresentazione in
    ombra ha dimostrato ripetutamente di combaciare con la testa canonica~~
    ✔ **fatto** (§8.45) — `projection.current_document`, snapshot dedicato
    `REPEATABLE READ, READ ONLY` in `api/deps.py`, `projection_inconsistent`,
    `test_get_from_sql_pg.py`, §15 di `storage-config-test.py`. Le tabelle
    normalizzate sono lo stato corrente autorevole; l'istantanea JSONB resta storia e
    **giudice**, senza nessun ripiego automatico. Contratto HTTP invariato.
17. ~~**Endpoint di query SQL**: ricerca, capacità, scadenze da SQL~~ ✔ **fatto**
    (§8.46) — `app/inventory/queries.py`, `app/api/queries.py`,
    `tools/make-query-fixtures.mjs`, 29 corpora di parità, `test_queries_pg.py`, §16 di
    `storage-config-test.py`. Semantica del frontend riprodotta alla lettera,
    divergenze misurate e documentate, **zero indici nuovi** perché le misure non ne
    giustificano nessuno. Frontend e worker NON ricablati.
18. ~~**Fase 2F**: il worker delle notifiche prende i candidati dalla proiezione~~
    ✔ **fatto** (§8.47) — `app/notifications/candidates.py`, `db.read_snapshot`,
    guardia sulla revisione, `fixtures/expiry/parity.py`, `test_worker_sql_pg.py`,
    §17 di `storage-config-test.py`. Semantica di `due_items` conservata alla lettera
    (**zero divergenze** su 16 corpora × 5 insiemi di finestre), una sola divergenza
    voluta e documentata, **nessuna migrazione e nessun privilegio nuovo**. I 53 test
    di consegna di `test_worker_pg.py` passano **non modificati**: è la prova che solo
    la sorgente è cambiata.
19. ~~**Fase 2G — debito semantico**: una semantica sola~~ ✔ **fatto** (§8.50) —
    `app/domain.py`, `handoff/domain.js`, `fixtures/domain/*.json`, migrazione
    `0013_domain`, `MAPPER_VERSION` 2. Chiuse le voci da 1 a 9 del registro più la 11:
    `presenza` separata da `stato`, una definizione di «U usate», percentuale HALF-UP
    intera, gruppi di fila strutturali, indirizzo esatto e IPv6, un interprete di date
    solo, Scadenze ispettiva contro worker azionabile, etichette senza «None».
    3055 test, 337 controlli statici, 544 controlli di contratto in JavaScript, 29
    mutazioni intercettate su 29.

    ⚠ Dopo l'aggiornamento serve un `project.py --rebuild` (§3.8.1 di
    `deploy/README.md`): la versione della mappa è cambiata.

20. ~~**Fase 2H — migrazione del frontend** alle rotte nuove, più la vista
    Dismessi~~ ✔ **fatto** (§8.51) — `handoff/queries.js`, `tools/queries-ui-test.py`,
    `tools/query-client-tests.mjs`, `tools/queries-perf.py`. Ricerca, capacità e
    scadenze non si calcolano più nel browser; resta UNA implementazione locale di
    capacità (pannello del rack, dashboard, export), dichiarata e legata al contratto
    dalle fixture. Chiusa anche la voce 16 del registro: `rack.u` fuori dall'intervallo
    sostenuto è rifiutato in scrittura (`rack_u_out_of_range`).

    **Nessuna migrazione, nessun indice, nessun privilegio nuovo**: la sola estensione
    di API è `stato`/`presenza` sulla ricerca, col vocabolario che già esisteva.

    ⚠ Due difetti veri trovati dai test del browser, e nessuno dei due era visibile
    dalle suite: un CICLO indiretto di ricaricamenti fra la riconciliazione della
    revisione e il rilancio delle interrogazioni aperte, e una sala con `vani: []` —
    che è il default canonico — che faceva morire il render dell'intera applicazione.

21. **Il buco dei dati di seed** (§8.48 voce 14, §7): il seed di produzione non ha
    nessuna scadenza, quindi la funzione più delicata del prodotto non è mai stata
    esercitata su dati che qualcuno ha scritto davvero. **Blocco al rilascio**, ed è
    l'ULTIMA voce aperta del registro. La qualificazione richiesta: uno scenario di
    scadenze deterministico e di forma reale, e il cammino completo degli avvisi
    percorso su quello.

22. **Rilascio**: purga dei dati di prova, baseline di produzione, deploy sulla VM.
    Non iniziato, e dipende dal punto 21.
7. Aggancio frontend (gli 8 punti di §4) e sequenza di avvio autenticata (§8.1)
   → **da qui i dati sono durevoli**
8. Coda di scrittura serializzata lato client (§8.2)
9. Immagine web con allowlist dei file statici (§6): esclusione di `inventario.js`,
   dello standalone e della documentazione. Da fare **dopo** il punto 7.
10. Foto su `/api/photos` + GC (§8.5)
11. Seed realistico con le date (§7), poi job scadenze con lock e idempotenza (§8.8),
    che ora ha una configurazione da cui partire (§8.38)
12. Fase 2: normalizzazione e endpoint di query, a frontend invariato

Il cuore sono i punti 3, 4, 6 e 7. L'ordine fra 3 e 6 non è negoziabile: il server che
rifiuta gli `_uid` mancanti (§8.4) presuppone un client che li genera, e invertirli
significa avere per un po' un endpoint che accetta documenti senza identità — cioè
esattamente lo stato che §8.4 esiste per rendere impossibile.

Se autorizzazione per ambito e `_uid` non ci sono dal primo giorno in cui i dati diventano
durevoli, poi si ricostruiscono su dati già scritti: la stessa migrazione, ma con i dati
veri dentro e senza sapere quali rinomini sono avvenuti nel frattempo.
