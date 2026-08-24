# Manifesto del rilascio — Trust Technologies Server Manager

Questo documento dice **che cosa, esattamente, è stato qualificato**, e con quale
esito. Non contiene segreti: nessuna password, nessun hash, nessuna credenziale del
relay SMTP.

Una qualificazione vale per gli artefatti su cui è stata eseguita e per nessun
altro. Se cambia il codice o una configurazione che finisce su un host, cambia
l'identità del rilascio e le prove che la riguardano vanno rifatte. Per questo i
numeri qui sotto sono digest e non nomi di versione.

    Data della qualificazione       2026-08-24
    Fase                            2J — qualificazione della candidata al rilascio
    Esito                           B. SOFTWARE QUALIFICATO — RESTANO CANCELLI OPERATIVI
                                    (vedi «Esito» in fondo)

---

## 1. Identità del rilascio

Si rigenera con `python tools/release-identity.py`, e `--check` la giudica.

### 1.1 Riproducibile dal repository

| che cosa | valore |
|---|---|
| commit | `23d47dc1bd952e3b87282bb0e088d4771d409266` |
| **albero del rilascio** | `18f712556844e6ba21b32603d2aa431f06569da60cbf5ea4f2197b292dd387cb` |
| file distribuiti | 117 |
| digest canonico della baseline | `febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1` |
| artefatto della baseline | 42 602 byte (nel commit) |
| revisione delle migrazioni | `0013_domain` (13 migrazioni) |
| versione della mappa relazionale | 2 |
| `schemaVersion` del documento | 1 |
| base dell'API | `python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91` |
| base del web | `nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10` |
| base del database | `postgres@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193` |
| React (vendorizzato) | `d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd` |
| React DOM (vendorizzato) | `35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d` |

**L'albero del rilascio** è il digest dei soli 117 file che finiscono su un host —
`backend/app`, `backend/migrations`, `backend/scripts`, i due Dockerfile,
`compose.yaml`, `deploy/preflight.sh`, `deploy/tsm.service`, l'artefatto della
baseline, e i file che l'immagine web serve. Esiste per una ragione pratica: dopo la
qualificazione si scrive documentazione, e ogni riga di documentazione muove il
commit. Se l'identità fosse solo il commit, ogni aggiunta a questo manifesto
invaliderebbe formalmente la qualificazione che il manifesto descrive — e nessuno
rifarebbe due giorni di prove per una frase. Il commit dice **da dove** viene il
rilascio; l'albero dice **che cosa** verrebbe installato. Un commit che tocca solo
`tools/`, `backend/tests/` o un `.md` lascia questo numero identico.

⚠ Il digest si calcola sui **blob del commit**, non sui file su disco. Il
repository si modifica su Windows e si esegue su Linux, `core.autocrlf` è attivo, e
`.gitattributes` impone LF a `.sh`, `.service`, `.conf` e `Dockerfile` — non a
`.py`, `.json`, `.js`, `.css`, `.html`. **39 dei 117 file differiscono fra copia di
lavoro Windows e commit, per il solo `\r`.** Conseguenza dichiarata: le immagini del
rilascio si costruiscono su un host Linux a partire da `git archive`, non dalla copia
di lavoro Windows. Un'immagine costruita là contiene i CRLF e non è l'immagine che
questo commit produce in produzione. È una caratteristica dichiarata, non un
difetto: entrambe funzionano, ma solo una è quella qualificata.

### 1.2 Identità del build (NON ricostruibile dalla sorgente)

Costruite sull'host di qualificazione da `git archive` del commit qui sopra. Due
build della stessa sorgente non danno lo stesso Id — nei livelli finiscono date —
quindi questi numeri identificano il binario provato, si confrontano e non si
ricalcolano.

    tsm-api:0.2.0    sha256:54061db1d26a56e5235029d3852ba640106a9bfd5589d029b06ed80c8fb9a168
    tsm-web:0.2.0    sha256:2d22b6e27bb3d3f3036a485ead23a26a20b5a780ac9bca44448ba208a325c541
    postgres         sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193

Il trasferimento in rete chiusa è stato esercitato: `docker save` dei tre, `docker
rmi`, `docker load`, e **Id identici dopo il giro**. È il percorso di
`deploy/README.md` §3.2.

---

## 2. La struttura della baseline, in forma rivedibile

3 siti, 6 sale, **102 rack, tutti vuoti**, 0 dispositivi, 0 voci di manuale, 0
riferimenti a foto, 18 numeri di inventario di armadio, 7 vani.

**Pomezia — G0** — 4 sale, 61 rack

- **Backend** — 10 rack, 417 U, file A×4 B×6, 1 vani
  <br>`Rack R01 — Core, Rack R02 — Database, Rack R03 — Virtualizzazione, Armadio UPS, Rack R04 — Applicativi, Rack R05 — Storage SAN, Rack R06 — Backup, Rack R07 — Kubernetes, Rack R08 — GPU, Rack R09 — Test`
- **Centro Stella** — 7 rack, 276 U, file A×6 (nessuna)×1, 1 vani
  <br>`Rack CS-R01, Rack CS-R02, Rack CS-R03, Rack CS-R04, Rack CS-R05, Rack CS-R06, Quadro elettrico`
- **Frontend** — 20 rack, 867 U, file A×10 B×9 (nessuna)×1, 1 vani
  <br>`Rack FE-R01 … Rack FE-R19, Armadio tecnico`
- **Saletta Nuova** — 24 rack, 1080 U, file A×6 B×11 C×7, 2 vani
  <br>`Rack SN-R01 … Rack SN-R24`

**Pomezia — H0** — 1 sala, 24 rack · **Sede di Roma** — 1 sala, 17 rack

L'elenco completo rack per rack, con le U, le file e i vani, sta in
`deploy/baseline/MANIFEST.md`, che è il documento pensato per essere letto da una
persona.

### 2.1 ⚠ CONFERME STRUTTURALI UMANE ANCORA APERTE

Sono **cancelli al rilascio**, non note. Nessuna automazione può risolverle: il
generatore le ha segnalate proprio perché dai dati non si distinguono da un errore.

| voce | che cosa dicono i dati | che cosa serve |
|---|---|---|
| **`Rack R09 — Test`** (Pomezia G0 / Backend) | fa parte di una serie coerente R01…R09, con U e fila regolari | un operatore deve confermare che è un rack **reale** che appartiene alla produzione — oppure va **rimosso con una modifica della baseline rivista a parte** |
| **ordine delle file in Saletta Nuova** | la sequenza delle etichette è `AAAAAABBBBBBBBBBCCCCCCBC`: SN-R23 torna a «B» e SN-R24 a «C» dopo il blocco «C» | un operatore deve **riconoscere questa struttura come la fonte di verità attuale** se resta invariata. Dai dati è indistinguibile da un «C» digitato «B» |

Nessuna delle due è stata toccata: correggerle vorrebbe dire inventare struttura di
produzione. La baseline qualificata è quella con il digest dichiarato sopra, e
queste due voci restano da chiudere **prima** dell'installazione reale.

---

## 3. L'ambiente della qualificazione

Riproduce `tsm-prd-01`; le differenze sono dichiarate, non nascoste.

| | previsto per tsm-prd-01 | qualificazione | rilevante? |
|---|---|---|---|
| hostname | `tsm-prd-01` | `tsm-prd-01` | — |
| sistema | Oracle Linux + systemd | Ubuntu 24.04 + systemd | il percorso systemd/fstab è lo stesso; **SELinux non è enforcing qui**, quindi i codici 27/28 del preflight non sono stati esercitati |
| vCPU | 4 | 8 | non vincolante: il carico è di poche operazioni |
| RAM | 8 GB | 7,6 GB | equivalente |
| swap | 8 GB | 2 GB | **non equivalente**: non è stata provata la pressione di memoria |
| disco sistema | 100 GB thin | 1007 GB | non vincolante |
| disco dati | 100 GB thin su `/srv/tsm-data` | **24 GB, filesystem ext4 dedicato su `/srv/tsm-data`** | la semantica del mount e la soglia dei 5 GiB sono le stesse; la capacità no |
| porte host | 80→8080, 443→8443 | idem, verificate dall'esterno | — |
| kernel | proprio della VM | condiviso con l'host (WSL2) | **il riavvio è un riavvio completo dello spazio utente** (systemd rieseguito, mount da fstab, container ricreati), **non un boot a freddo del kernel** |
| backup | Veeam | istantanea coerente del volume + ripristino | equivalente per l'integrità applicativa; **la configurazione dei job Veeam resta da fare** |

La composizione usata è `compose.yaml` **e nient'altro**: nessun
`compose.dev.yaml`, nessun `compose.storage-dev.yaml`, nessun
`compose.override.yaml`. È l'unica differenza sostanziale rispetto alla camera
bianca della fase 2I, che usava l'override di sviluppo perché la macchina non aveva
un secondo disco.

---

## 4. Totali delle prove

### 4.1 Suite committate, eseguite tutte

| suite | esito |
|---|---|
| Python (`backend/tests`, 39 file) | **3246 / 3246** |
| controlli statici e di configurazione | **433 / 433** |
| contratto di dominio (JavaScript) | **560 / 560** |
| client delle interrogazioni (JavaScript) | **45 / 45** |
| identità (JavaScript) | **120 / 120** |
| smoke (privilegi, non-root, allowlist, TLS) | **46 / 46** |
| sicurezza del proxy e delle intestazioni inoltrate | **19 / 19** |
| archiviazione end-to-end (mount vero, con controprova) | **31 / 31** |
| offline: risorse statiche, con controllo negativo | **12 / 12** |
| giro export→import XLSX | **15 / 15** |
| browser: e2e | **29 / 29** |
| browser: utenze | **51 / 51** |
| browser: audit | **24 / 24** |
| browser: impostazioni | **40 / 40** |
| browser: foto | **17 / 17** |
| browser: interrogazioni e semantica | **67 / 67** |
| browser: camera bianca sulla baseline | **47 / 47** |

### 4.2 Prove aggiunte dalla 2J

| prova | esito |
|---|---|
| qualificazione del rilascio, §9 + §14 su installazione appena inizializzata | **30 / 30** |
| qualificazione del rilascio, §5 + §16 + §12 + §27 + §30 | **82 / 82** |
| riavvio dell'host: verifica dopo il riavvio | **17 / 17** |
| riavvii controllati: nginx, API, worker, PostgreSQL, tutto, stop/start | **17 / 17** |
| matrice dei guasti (12 iniezioni) | **73 / 73** |
| installazione completa **senza internet** | **25 / 25** |
| backup e ripristino | **15 / 15** (due esecuzioni) |
| notifiche end-to-end con la fixture di qualificazione | **18 / 18** |
| mutazioni | **18 / 18 intercettate** |

Nessuno salto non giustificato. Le uniche cose non esercitate sono dichiarate in §3
(SELinux, pressione di swap, capacità dei dischi) e in §6 (voce 14B).

### 4.3 Misure

A riposo, quattro container: **web 9 MiB, API 64 MiB, worker 44 MiB, database 38
MiB** — circa 155 MiB in tutto su 7,6 GiB. Disco dati: 48 MiB su una baseline
appena installata.

Tempi di risposta attraverso nginx e TLS, mediana e p95 su 15 ripetizioni:

| richiesta | mediana | p95 |
|---|---|---|
| `GET /api/inventory` | 21,1 ms | 26,9 ms |
| `GET /api/inventory/search` | 14,4 ms | 34,3 ms |
| `GET /api/inventory/capacity` | 12,7 ms | 18,6 ms |
| `GET /api/inventory/expiries` | 13,1 ms | 14,4 ms |
| `GET /api/ready` | 8,8 ms | 10,8 ms |

Confronto con le misure della fase 2D (39 ms mediani per un `GET`): nessun
peggioramento; il `GET` è più rapido perché la baseline non ha dispositivi. Nessuna
soglia è stata inventata: si controlla soltanto che nulla sia fuori scala di un
ordine di grandezza.

### 4.4 Bilancio delle connessioni

    pool delle richieste     5 + 10 = 15
    pool delle letture       5 + 10 = 15
    lavoratori uvicorn       1
    per processo             30
    worker delle notifiche   1
    margine operazioni       5
    ---------------------------------
    richiesto                36
    max_connections          100  (3 riservate al superuser)

⚠ **Cambiare `--workers` non è una modifica locale.** Moltiplica per N *entrambi* i
pool: con la capienza attuale basta N=4 per superare `max_connections`. Chi lo tocca
deve ricalcolare insieme i due pool, `max_connections` e la memoria della macchina
(ogni connessione PostgreSQL è un processo). Nessuna misura di questa fase dà una
ragione per aumentarli.

---

## 5. Matrice dei guasti

Dodici iniezioni, ognuna con l'attesa **dichiarata prima** di eseguirla. Per ogni
guasto previsto esiste un codice stabile: **nessun 500 inspiegato**.

| guasto | che cosa si osserva | stato | rimedio |
|---|---|---|---|
| PostgreSQL non disponibile | readiness 503 `database:not-ready`; `GET` 503; **liveness 200** | nessun cambio | `systemctl restart tsm` |
| limitatore dei tentativi non scrivibile | login **503 `rate_limiter_unavailable`**, nessuna sessione | nessun cambio | ripristinare `INSERT` su `login_attempts`; verificare spazio e privilegi |
| hash della password corrotto | 401 generico, tentativo **registrato** | nessun cambio | reimpostare la password dell'utenza |
| SMTP non configurato | `POST /api/notifications/test` → 503 `smtp_not_configured` | nessuna consegna marcata | configurare `TSM_SMTP_*` (operations) |
| proiezione non attuale | readiness 503; `GET` 503 `projection_not_current` | storia intatta | `project.py --rebuild`, subito appropriato |
| proiezione incoerente | `GET` 503 `projection_inconsistent`; il worker **rifiuta**; `--verify` lo rileva | storia intatta | **prima** verificare e conservare la prova, **poi** `--rebuild` come recupero esplicito |
| secondo worker | esce con 3 e lo dice | nessun avviso duplicato | nessuno: è il comportamento voluto |
| materiale TLS assente | preflight **31**; `systemctl start` fallisce; nessun container | nessun cambio | ripristinare il certificato, uid 101, 0400 |
| disco dati non montato (montabile) | preflight a mano **20**; `systemctl start` **monta il disco da sé** (`RequiresMountsFor`) e parte | nessun cambio | nessuno: systemd lo ripara |
| disco dati **inesistente** | `systemctl start` fallisce; **nessuna directory creata su `/`**; nessun container | nessun cambio | ripristinare il volume, `mount -a` |
| spazio libero sotto la soglia | preflight **26** | nessun cambio | liberare spazio; soglie di allerta 70% e 85% |
| carico concorrente 2× la capienza di un pool | 30 `GET` simultanei, tutti 200, 0,9 s, **nessuno stallo** | nessun cambio | nessuno |

---

## 6. Voce 14 del registro: 14A e 14B

La voce si divide, come il requisito della 2J prevede.

**14A — semantica e cammino applicativo delle notifiche: SUPERATA.**
La fixture di qualificazione (`fixtures/qualification/notification-scenario.json`,
21 apparati di forma reale, atteso dichiarato caso per caso) installata come seme di
un ambiente proprio, con data di riferimento **2026-09-01** iniettata e finestra di
30 giorni. Cammino completo sullo stack vero — proiezione, colonne derivate,
selezione dei candidati, filtro sullo stato, digest, conversazione SMTP con un relay
di prova, stato della consegna:

- **14 scadenze dovute, 14 avvisate**, esattamente l'atteso dichiarato;
- **un solo** messaggio, ai **due** destinatari configurati e a nessun altro;
- `Message-ID` presente; consegna in stato `sent`;
- secondo giro nello stesso giorno: `already_ran_today`, **nessun doppione**;
- l'apparato con garanzia **e** supporto produce **due** promemoria;
- una data illeggibile **non** produce una data derivata, e il testo si conserva;
- contesto con la barra e Unicode (`Sala Röntgen — 21 °C ✓`) intatti nella proiezione;
- SMTP guasto → `connection_failed` e consegna `pending`: **non** viene marcata
  consegnata.

**14B — accettazione del relay SMTP di produzione: APERTA.**
Superare la prova con un relay controllato **non** dimostra il relay aziendale, la
rete, né l'elenco dei destinatari. Fino a 14B:

- le notifiche di produzione restano **disattivate**;
- 14B si chiude con un invio di prova attraverso il **relay reale** verso un
  **destinatario di prova approvato**, usando l'invio di verifica già previsto
  (`POST /api/notifications/test`, limitato di proposito);
- **non** servono dispositivi finti in produzione per chiuderla;
- non si manda posta di qualificazione all'elenco completo dei destinatari.

---

## 7. Caratteristiche dichiarate

Non sono difetti: sono fatti noti, misurati, e archiviati come tali.

1. **Statistiche del pianificatore dopo un salvataggio grande.** La
   sincronizzazione della fase 2C cancella e reinserisce le righe a ogni
   salvataggio, quindi una scrittura grande lascia qualche secondo di statistiche
   vecchie. Alla scala di produzione è immateriale; la procedura di diagnosi sta in
   `deploy/README.md`. Non è stata ridisegnata durante la qualificazione, come il
   requisito chiede.
2. **CRLF nella copia di lavoro Windows.** Vedi §1.1: le immagini del rilascio si
   costruiscono da `git archive` su Linux.
3. **Le sessioni sopravvivono a un riavvio**, perché vivono in PostgreSQL. È
   voluto: un riavvio del servizio non deve buttare fuori tutti gli operatori.
4. **`/index.html` risponde 404.** La pagina si serve su `/`; il percorso diretto
   cade nel default-deny dell'allowlist. È il comportamento voluto.
5. **La suite delle utenze richiede un'installazione pulita.** Usa nomi utente
   fissi, presume che l'utenza creata sia l'ultima riga, e alla fine retrocede
   l'amministratore per provare la protezione contro l'autoretrocessione. Va
   eseguita per prima, o su uno stato sacrificabile.
6. **La finestra di esclusività del worker è di un giro.** Dopo un riavvio di
   PostgreSQL il lock consultivo si perde e viene ripreso al giro successivo (fino a
   300 s). Prima della 2J non veniva ripreso affatto.

---

## 8. Prerequisiti operativi che restano

Sono i cancelli che questa fase **non** poteva chiudere, perché non riguardano il
software.

| # | cancello | chi |
|---|---|---|
| 1 | **conferma umana delle due voci strutturali** di §2.1 | operatore / responsabile del CED |
| 2 | **14B**: invio di prova attraverso il relay SMTP reale a un destinatario approvato | operations |
| 3 | provisioning della VM reale `tsm-prd-01` con **due dischi** e i tagli dichiarati (compreso **8 GB di swap**) | infrastruttura |
| 4 | certificato **aziendale** in `secrets/tls` (qui è stato usato un autofirmato) | infrastruttura |
| 5 | configurazione e **prova** dei job Veeam sulla VM | infrastruttura |
| 6 | verifica dei controlli **SELinux** (codici 27 e 28 del preflight) su Oracle Linux enforcing | infrastruttura |
| 7 | trasferimento delle immagini in rete chiusa e `docker load` sull'host reale | operations |

---

## 9. Esito

**B. SOFTWARE QUALIFICATO — RESTANO CANCELLI OPERATIVI.**

Tutti i cancelli del software e della candidata al rilascio sono superati: 3246 test
Python, 433 controlli statici, 725 controlli di contratto in JavaScript, 17 suite
committate tutte verdi, 112 controlli di qualificazione del rilascio, 73 iniezioni
di guasto, 34 controlli di riavvio, 25 di funzionamento offline, 15 di ripristino, 18
di notifica, 18 mutazioni su 18 intercettate. L'installazione nasce dalla baseline
approvata, in un ambiente che riproduce la forma dell'host di produzione, senza
override di sviluppo, e sopravvive a un riavvio senza interventi.

Non è **C. PRONTO PER IL DISPIEGAMENTO CONTROLLATO** perché restano i sette
prerequisiti di §8, e due di essi — la conferma strutturale umana e la 14B — sono
decisioni che nessun test può prendere.

**Non è stato distribuito niente.** Nessuna VM di produzione è stata predisposta,
nessun segreto è stato copiato su un host reale, nessun database di produzione è
stato inizializzato, nessun relay SMTP reale è stato aperto.
