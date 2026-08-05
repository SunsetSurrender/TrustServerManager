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

Le foto sono content-addressed su `sha256` e **immutabili**: nessun `UPDATE`. Caricare due
volte la stessa immagine restituisce la stessa riga.

Non possono avere un `DELETE` su richiesta: le **versioni storiche dell'inventario le
referenziano**, e cancellare i byte trasformerebbe un rollback in una foto rotta. Togliere
una foto da un rack significa togliere il riferimento nella versione nuova; le versioni
vecchie continuano a puntare ai byte.

I byte li libera un job periodico:

```sql
DELETE FROM photos p
WHERE p.created_at < now() - interval '24 hours'     -- grazia per gli upload in corso
  AND NOT EXISTS (
    SELECT 1 FROM inventory i
    WHERE i.doc::text LIKE '%' || p.id::text || '%'  -- in fase 2: JOIN su racks.photo_id
  );
```

La finestra di grazia serve perché una foto caricata è orfana fino al `PUT` che la
referenzia: senza, la GC cancella gli upload appena fatti. La retention delle versioni
determina di fatto quella delle foto — vanno decise insieme, non separatamente.

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
| 1 | schema del documento e limite di dimensione | **prima** del database: un documento malformato non deve prendere un lock |
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
6-bis. Rimozione del bypass client in `_doLogin` e aggancio del frontend all'API: è il passo
   che rende i dati durevoli per l'utente.
6-ter. **TLS davanti a nginx.** Il cookie di sessione è `Secure`, quindi senza HTTPS non si
   entra: è l'ultimo pezzo mancante prima di un uso reale.
7. Aggancio frontend (gli 8 punti di §4) e sequenza di avvio autenticata (§8.1)
   → **da qui i dati sono durevoli**
8. Coda di scrittura serializzata lato client (§8.2)
9. Immagine web con allowlist dei file statici (§6): esclusione di `inventario.js`,
   dello standalone e della documentazione. Da fare **dopo** il punto 7.
10. Foto su `/api/photos` + GC (§8.5), settings con secret SMTP fuori dall'API (§8.7)
11. Seed realistico con le date (§7), poi job scadenze con lock e idempotenza (§8.8)
12. Fase 2: normalizzazione e endpoint di query, a frontend invariato

Il cuore sono i punti 3, 4, 6 e 7. L'ordine fra 3 e 6 non è negoziabile: il server che
rifiuta gli `_uid` mancanti (§8.4) presuppone un client che li genera, e invertirli
significa avere per un po' un endpoint che accetta documenti senza identità — cioè
esattamente lo stato che §8.4 esiste per rendere impossibile.

Se autorizzazione per ambito e `_uid` non ci sono dal primo giorno in cui i dati diventano
durevoli, poi si ricostruiscono su dati già scritti: la stessa migrazione, ma con i dati
veri dentro e senza sapere quali rinomini sono avvenuti nel frattempo.
