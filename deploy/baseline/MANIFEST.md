# Manifesto strutturale della baseline di produzione

⚠ **Generato** da `tools/make-production-baseline.py`. Non si modifica a mano:
si modifica la struttura, si rigenera, e si RILEGGE questo file.

Serve a una domanda che nessun test può porre: *questa è la struttura giusta?*
Un sito di prova rimasto dentro passa qualunque validazione di schema; una sala
vera dimenticata fuori pure. Quindi va letta da una persona, sito per sito.

- digest canonico: `febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1`
- siti: **3**
- sale: **6**
- rack: **102**
- dispositivi: **0** (rimossi: 86)
- voci di manuale: **0** (chiave `manuale` assente: è il manuale vuoto canonico)
- seriali di armadio conservati: **18**
- vani (geometria) conservati: **7**
- seriali corretti dal rilievo (`rack-serials.json`): **0** rack

## ⚠ Da confermare

Due tipi di segnalazione, e nessuna delle due rimuove niente.

**NOMI** che contengono una parola tipica dei dati di prova: in un CED vero un
rack che si chiama «Test» è spesso un rack vero, che ospita i sistemi di
collaudo. Ma la differenza fra quello e un rack di sviluppo rimasto dentro non
la può stabilire un programma, e uno solo dei due casi deve finire in
produzione.

**FILE** le cui etichette non sono raggruppate nell'ordine del documento: un
rack di fila «B» che compare dopo il blocco «C». In un inventario cresciuto per
accodamento è normale — due rack aggiunti dopo e messi in fondo — e in un
inventario battuto a mano è un errore di digitazione. Dai dati i due casi non
si distinguono.

Chi conosce le sale conferma riga per riga:

- [ ] RACK 'Rack R09 — Test' (`R09`) in Pomezia — G0/Backend — contiene «test»
- [ ] FILA in Pomezia — G0/Saletta Nuova — 'Rack SN-R23': fila «B» dopo «C» (sequenza: AAAAAABBBBBBBBBBCCCCCCBC)
- [ ] FILA in Pomezia — G0/Saletta Nuova — 'Rack SN-R24': fila «C» dopo «B» (sequenza: AAAAAABBBBBBBBBBCCCCCCBC)

## Struttura

### Pomezia — G0 — `pomezia-g0`

`_uid` c731f700-b920-4deb-9886-6b1364bf57b4

#### Sala Backend — `backend`

`_uid` 5dea9ef5-ea20-4087-af0c-e5d925b7641c · 4.25×4.99 m · 1 vano · 10 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack R01 — Core | `R01` | A | 45 | — |
| 2 | Rack R02 — Database | `R02` | A | 45 | — |
| 3 | Rack R03 — Virtualizzazione | `R03` | A | 45 | — |
| 4 | Armadio UPS | `UPS` | A | 12 | — |
| 5 | Rack R04 — Applicativi | `R04` | B | 45 | — |
| 6 | Rack R05 — Storage SAN | `R05` | B | 45 | — |
| 7 | Rack R06 — Backup | `R06` | B | 45 | — |
| 8 | Rack R07 — Kubernetes | `R07` | B | 45 | — |
| 9 | Rack R08 — GPU | `R08` | B | 45 | — |
| 10 | Rack R09 — Test | `R09` | B | 45 | — |

#### Sala Centro Stella — `centro-stella`

`_uid` f38e181b-77f6-434f-86ab-5cbe35e5b3d4 · 2.79×4.97 m · 1 vano · 7 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack CS-R01 | `CS-R01` | A | 45 | — |
| 2 | Rack CS-R02 | `CS-R02` | A | 45 | — |
| 3 | Rack CS-R03 | `CS-R03` | A | 45 | — |
| 4 | Rack CS-R04 | `CS-R04` | A | 45 | — |
| 5 | Rack CS-R05 | `CS-R05` | A | 45 | — |
| 6 | Rack CS-R06 | `CS-R06` | A | 45 | — |
| 7 | Quadro elettrico | `CS-Q01` | — | 6 | — |

#### Sala Frontend — `frontend`

`_uid` dffe39f3-5e30-4388-9295-4352b191a311 · 4.89×7.5 m · 1 vano · 20 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack FE-R01 | `FE-R01` | A | 45 | — |
| 2 | Rack FE-R02 | `FE-R02` | A | 45 | — |
| 3 | Rack FE-R03 | `FE-R03` | A | 45 | — |
| 4 | Rack FE-R04 | `FE-R04` | A | 45 | — |
| 5 | Rack FE-R05 | `FE-R05` | A | 45 | — |
| 6 | Rack FE-R06 | `FE-R06` | A | 45 | — |
| 7 | Rack FE-R07 | `FE-R07` | A | 45 | — |
| 8 | Rack FE-R08 | `FE-R08` | A | 45 | — |
| 9 | Rack FE-R09 | `FE-R09` | A | 45 | — |
| 10 | Rack FE-R10 | `FE-R10` | A | 45 | — |
| 11 | Rack FE-R11 | `FE-R11` | B | 45 | — |
| 12 | Rack FE-R12 | `FE-R12` | B | 45 | — |
| 13 | Rack FE-R13 | `FE-R13` | B | 45 | — |
| 14 | Rack FE-R14 | `FE-R14` | B | 45 | — |
| 15 | Rack FE-R15 | `FE-R15` | B | 45 | — |
| 16 | Rack FE-R16 | `FE-R16` | B | 45 | — |
| 17 | Rack FE-R17 | `FE-R17` | B | 45 | — |
| 18 | Rack FE-R18 | `FE-R18` | B | 45 | — |
| 19 | Rack FE-R19 | `FE-R19` | B | 45 | — |
| 20 | Armadio tecnico | `FE-Q01` | — | 12 | — |

#### Sala Saletta Nuova — `saletta-nuova`

`_uid` 9752009f-cf79-4d25-8e28-1295cdd8f7f3 · 4.23×14.2 m · 2 vani · 24 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack SN-R01 | `SN-R01` | A | 45 | — |
| 2 | Rack SN-R02 | `SN-R02` | A | 45 | — |
| 3 | Rack SN-R03 | `SN-R03` | A | 45 | — |
| 4 | Rack SN-R04 | `SN-R04` | A | 45 | — |
| 5 | Rack SN-R05 | `SN-R05` | A | 45 | — |
| 6 | Rack SN-R06 | `SN-R06` | A | 45 | — |
| 7 | Rack SN-R07 | `SN-R07` | B | 45 | — |
| 8 | Rack SN-R08 | `SN-R08` | B | 45 | — |
| 9 | Rack SN-R09 | `SN-R09` | B | 45 | — |
| 10 | Rack SN-R10 | `SN-R10` | B | 45 | — |
| 11 | Rack SN-R11 | `SN-R11` | B | 45 | — |
| 12 | Rack SN-R12 | `SN-R12` | B | 45 | — |
| 13 | Rack SN-R13 | `SN-R13` | B | 45 | — |
| 14 | Rack SN-R14 | `SN-R14` | B | 45 | — |
| 15 | Rack SN-R15 | `SN-R15` | B | 45 | — |
| 16 | Rack SN-R16 | `SN-R16` | B | 45 | — |
| 17 | Rack SN-R17 | `SN-R17` | C | 45 | — |
| 18 | Rack SN-R18 | `SN-R18` | C | 45 | — |
| 19 | Rack SN-R19 | `SN-R19` | C | 45 | — |
| 20 | Rack SN-R20 | `SN-R20` | C | 45 | — |
| 21 | Rack SN-R21 | `SN-R21` | C | 45 | — |
| 22 | Rack SN-R22 | `SN-R22` | C | 45 | — |
| 23 | Rack SN-R23 | `SN-R23` | B | 45 | — |
| 24 | Rack SN-R24 | `SN-R24` | C | 45 | — |

### Pomezia — H0 — `pomezia-h0`

`_uid` 2e6ef8ca-bc00-45c7-8f45-105df2fca4a3

#### Sala H0 — `h0`

`_uid` ba5977a1-db44-4927-a9ba-bce366130605 · 7.58×8.18 m · 1 vano · 25 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack H0-R01 | `H0-R01` | A | 45 | — |
| 2 | Rack H0-R02 | `H0-R02` | A | 45 | — |
| 3 | Rack H0-R03 | `H0-R03` | A | 45 | — |
| 4 | Rack H0-R04 | `H0-R04` | A | 45 | — |
| 5 | Rack H0-R05 | `H0-R05` | A | 45 | — |
| 6 | Rack H0-R06 | `H0-R06` | A | 45 | — |
| 7 | Rack H0-R07 | `H0-R07` | A | 45 | — |
| 8 | Rack H0-R08 | `H0-R08` | A | 45 | — |
| 9 | Rack H0-R09 | `H0-R09` | B | 45 | — |
| 10 | Rack H0-R10 | `H0-R10` | B | 45 | — |
| 11 | Rack H0-R11 | `H0-R11` | B | 45 | — |
| 12 | Rack H0-R12 | `H0-R12` | B | 45 | — |
| 13 | Rack H0-R13 | `H0-R13` | B | 45 | — |
| 14 | Rack H0-R14 | `H0-R14` | B | 45 | — |
| 15 | Rack H0-R15 | `H0-R15` | B | 45 | — |
| 16 | Rack H0-R16 | `H0-R16` | B | 45 | — |
| 17 | Rack H0-R17 | `H0-R17` | C | 45 | — |
| 18 | Rack H0-R18 | `H0-R18` | C | 45 | — |
| 19 | Rack H0-R19 | `H0-R19` | C | 45 | — |
| 20 | Rack H0-R20 | `H0-R20` | C | 45 | — |
| 21 | Rack H0-R21 | `H0-R21` | C | 45 | — |
| 22 | Rack H0-R22 | `H0-R22` | C | 45 | — |
| 23 | Rack H0-R23 | `H0-R23` | C | 45 | — |
| 24 | Rack H0-R24 | `H0-R24` | C | 45 | — |
| 25 | Armadio a muro | `H0-Q01` | — | 12 | — |

### Oriolo Romano — `oriolo-romano`

`_uid` a9e8b864-01e0-41b3-85ea-a52b1fadc5b2

#### Sala Sala Oriolo — `sala-oriolo`

`_uid` 84222257-5bb3-405d-bdf8-de4bf2f12e28 · 9.2×6 m · 1 vano · 16 rack

| # | nome | codice | fila | U | seriali armadio |
|---|---|---|---|---|---|
| 1 | Rack RO-A01 | `RO-A01` | A | 45 | 2020047937 |
| 2 | Rack RO-A02 | `RO-A02` | A | 45 | 2020053938 |
| 3 | Rack RO-A03 | `RO-A03` | A | 45 | 2020044486 |
| 4 | Rack RO-A04 | `RO-A04` | A | 45 | 2020044485 |
| 5 | Rack RO-A05 | `RO-A05` | A | 45 | 2006029486 |
| 6 | Rack RO4066 | `RO4066` | A | 45 | 2006004084 |
| 7 | Rack RO4037 | `RO4037` | A | 45 | 2006004088 |
| 8 | Rack RO4067 | `RO4067` | A | 45 | 2006004090 |
| 9 | Rack RO4082 | `RO4082` | A | 45 | 2006004091 |
| 10 | Rack R11024 | `R11024` | A | 45 | 2008034260 |
| 11 | Rack RO-RETE | `RO-RETE` | A | 45 | 2012170418 |
| 12 | Rack R11115 | `R11115` | B | 45 | 2012170515 |
| 13 | Rack RO4495 | `RO4495` | B | 45 | 2004126690, 2020044487 |
| 14 | Rack RO-B03 | `RO-B03` | B | 45 | 2020062936, 2020062935 |
| 15 | Rack RO-B04 | `RO-B04` | B | 45 | 2006029736 |
| 16 | Rack RO-B05 | `RO-B05` | B | 45 | 2006004089 |

## Che cosa è stato tolto

Elenco completo, generato: se qui compare qualcosa che doveva restare, la
baseline è sbagliata e va rigenerata dopo aver corretto la struttura.

- sito pomezia-g0/backend/Rack R01 — Core: 5 dispositivi
- sito pomezia-g0/backend/Rack R02 — Database: 3 dispositivi
- sito pomezia-g0/backend/Rack R03 — Virtualizzazione: 3 dispositivi
- sito pomezia-g0/backend/Armadio UPS: 1 dispositivi
- sito pomezia-g0/backend/Rack R04 — Applicativi: 3 dispositivi
- sito pomezia-g0/backend/Rack R05 — Storage SAN: 3 dispositivi
- sito pomezia-g0/backend/Rack R06 — Backup: 3 dispositivi
- sito pomezia-g0/backend/Rack R07 — Kubernetes: 4 dispositivi
- sito pomezia-g0/backend/Rack R08 — GPU: 2 dispositivi
- sito pomezia-g0/backend/Rack R09 — Test: 2 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-A01: 2 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-A02: 1 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-A03: 1 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-A04: 1 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-A05: 4 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO4066: 8 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO4037: 10 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO4067: 4 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO4082: 9 dispositivi
- sito oriolo-romano/sala-oriolo/Rack R11024: 7 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-RETE: 7 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO4495: 1 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-B03: 1 dispositivi
- sito oriolo-romano/sala-oriolo/Rack RO-B04: 1 dispositivi
- radice: 'utenti' (legacy del prototipo, §8.16)
- radice: 'versione' (legacy del prototipo, §8.16)
