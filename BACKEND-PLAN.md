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
  username       citext UNIQUE NOT NULL,
  role           text NOT NULL CHECK (role IN ('view','edit','admin')),
  password_hash  text NOT NULL,                    -- argon2id
  must_change_pw boolean NOT NULL DEFAULT false,    -- ex pwTemp
  nome           text, cognome text, telefono text, team text,
  disabled_at    timestamptz,
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

-- append-only: la versione corrente è quella con version massima
CREATE TABLE inventory (
  version    integer PRIMARY KEY,
  doc        jsonb NOT NULL,
  action     text,                                 -- la stringa già passata a persist()
  author_id  uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE photos (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mime        text NOT NULL,
  bytes       bytea NOT NULL,
  sha256      text NOT NULL,
  size_bytes  integer NOT NULL,
  uploaded_by uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit (
  id       bigserial PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  user_id  uuid REFERENCES users(id),
  username citext,                                 -- denormalizzato: sopravvive alla cancellazione
  role     text,
  action   text NOT NULL,
  detail   jsonb,
  ip       inet
);
CREATE INDEX ON audit (ts DESC);

CREATE TABLE settings (            -- notifiche + campi SMTP non segreti
  key   text PRIMARY KEY,
  value jsonb NOT NULL
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

---

## 3. API

```
POST   /api/auth/login              → cookie di sessione HttpOnly+Secure+SameSite=Strict
POST   /api/auth/logout
GET    /api/auth/me                 → { username, role, must_change_pw }
POST   /api/auth/password           → cambio password propria, azzera must_change_pw

GET    /api/users                        (admin)
POST   /api/users                        (admin) → crea con password provvisoria
PATCH  /api/users/{id}                   (admin)
DELETE /api/users/{id}                   (admin)

GET    /api/inventory               → { version, doc }
PUT    /api/inventory               → { baseVersion, doc, action }
                                      200 { version } | 409 { currentVersion }
GET    /api/inventory/versions      → [{ version, ts, author, action }]
GET    /api/inventory/versions/{v}  → { version, doc }
POST   /api/inventory/rollback/{v}       (admin) → nuova versione = copia della v

POST   /api/photos                  → multipart, → { id }
GET    /api/photos/{id}             → bytes, Cache-Control immutable
DELETE /api/photos/{id}

GET    /api/audit?limit&offset&from&to
GET    /api/settings   PUT /api/settings  (admin)   -- password SMTP write-only, mai in GET
POST   /api/notifications/test           (admin)
GET    /api/health     GET /api/ready
```

Concorrenza: **last-write-wins con lock ottimistico**. Il `PUT` che arriva con un
`baseVersion` non più corrente riceve 409 e la UI propone il ricaricamento. Per una squadra
di pochi operatori è la scelta giusta; endpoint granulari si aggiungono in fase 2 solo se
l'editing simultaneo diventa un problema reale.

---

## 4. Modifiche al frontend (fase 1)

Sette punti, tutti localizzati:

1. **`componentDidMount`** ([:1120](handoff/Sala%20Server%20v2.dc.html#L1120)) — al posto di
   `import('./inventario.js')`, un `GET /api/inventory`. `inventario.js` sopravvive solo come
   seed per lo script di import iniziale.
2. **`persist(data, azione)`** ([:1391](handoff/Sala%20Server%20v2.dc.html#L1391)) — resta
   ottimistica (`setState` immediato), aggiunge `PUT /api/inventory` con `baseVersion`.
   Su 409: avviso "modificato da un altro utente" + ricarica. `dirty` passa da
   "non esportato" a "salvataggio in corso / fallito".
3. **`_doLogin()`** ([:1351](handoff/Sala%20Server%20v2.dc.html#L1351)) — `POST /api/auth/login`.
   Va **rimosso** il confronto password lato client e il fallback che concede `admin`
   quando l'elenco utenze è vuoto ([:1358](handoff/Sala%20Server%20v2.dc.html#L1358)).
4. **Foto rack** — `POST /api/photos`, `foto` diventa una URL invece di un dataURL.
5. **Registro** — legge da `/api/audit` invece che da `data.registro`.
6. **Undo/redo** — resta client-side; le versioni server sono la rete di sicurezza.
7. **React vendorizzato** — vedi §5.

---

## 5. Rete chiusa: dipendenze da eliminare

`support.js` carica React da unpkg.com a runtime
([support.js:1143-1146](handoff/support.js#L1143)). **In rete chiusa l'app non parte.**
Vale anche per il build "standalone": ho verificato che React non è inlinizzato, c'è solo la URL.

Servono due soli file, perché `x-import` non è mai usato dall'app e quindi
`@babel/standalone` non viene mai richiesto:

```
react@18.3.1/umd/react.production.min.js
react-dom@18.3.1/umd/react-dom.production.min.js
```

Si copiano nell'immagine e si caricano **prima** di `support.js`: il runtime controlla
`if (w.React && w.ReactDOM) return Promise.resolve()` e salta la CDN. Nessuna patch al runtime.

```html
<script src="./vendor/react.production.min.js"></script>
<script src="./vendor/react-dom.production.min.js"></script>
<script src="./support.js"></script>
```

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
| `tsm-web` | `nginx:1.27-alpine` | static `handoff/` + `vendor/` React, proxy `/api` → `api:8000`, TLS |
| `tsm-api` | `python:3.13-slim` | FastAPI + uvicorn, multi-stage, utente non-root |
| `postgres` | `postgres:17-alpine` | volume `pgdata`, **nessuna porta pubblicata sull'host** |

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

## 8. Ordine di lavoro proposto

1. Vendorizzare React → l'app parte in rete chiusa (mezza giornata, sblocca ogni collaudo)
2. Repo backend + Compose + Postgres + Alembic, `/api/health` verde
3. Auth: users, sessioni, login, cambio password provvisoria + rimozione bypass client
4. `GET`/`PUT /api/inventory` versionato + script di import da `inventario.js`
5. Aggancio frontend (i 7 punti di §4) → **da qui i dati sono durevoli**
6. Foto su `/api/photos`, audit server-side, settings + secret SMTP
7. Seed realistico con le date, poi job scadenze + email
8. Fase 2: normalizzazione e endpoint di query, a frontend invariato
```
