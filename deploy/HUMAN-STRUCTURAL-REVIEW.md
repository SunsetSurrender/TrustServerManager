# Revisione strutturale umana della baseline

Registro dei due quesiti che il codice non può decidere, richiesti da
Fase 2K-A §1. Ogni voce è `CONFERMATO` o `RICHIEDE MODIFICA DELLA
BASELINE`; finché una resta senza risposta il cancello è **aperto** e la
distribuzione non procede.

Baseline in esame — invariata dalla qualificazione:

| | |
|---|---|
| digest del documento | `febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1` |
| digest del file (byte, LF) | `c22bcb4ed3888ca19648169a2dd06545f15e159ea33154fb9e91a50e8a9909b6` |
| commit qualificato | `23d47dc1bd952e3b87282bb0e088d4771d409266` |
| contenuto | 3 siti · 6 sale · 102 rack · 0 dispositivi |

Perché lo strumento le ha segnalate: dai soli dati non si distinguono da
un errore di inserimento. Nessuna automazione ha il diritto di decidere
che «Test» significhi sviluppo — né che significhi produzione.

---

## 1A — «Rack R09 — Test»

**Stato: NON CONFERMATO — in attesa di CED operations.**

Il fatto misurato:

| | |
|---|---|
| percorso | `pomezia-g0` → sala `backend` → decimo rack |
| `_uid` | `02a57b83-7c5f-4288-abe2-fb114773eab1` |
| file | B · 45U · nessun dispositivo |
| vicini in fila B | R04 Applicativi · R05 Storage SAN · R06 Backup · R07 Kubernetes · R08 GPU |
| vicini in fila A | R01 Core · R02 Database · R03 Virtualizzazione · Armadio UPS |

Domanda: **R09 è un rack reale che deve restare nella struttura iniziale
di TSM?**

Il contesto non risponde da sé. Una sala in cui gli altri nove armadi
sono Core, Database, Virtualizzazione, Applicativi, SAN, Backup,
Kubernetes, GPU e UPS è una sala di produzione seria, e un rack di
collaudo accanto è del tutto normale — ma «Test» in un inventario è
anche il nome che si dà a una riga inserita per prova e mai rimossa.

Chi risponde: operations del CED di Pomezia G0.

Se la risposta è «non è reale»: **STOP.** Non si corregge qui. Si
rigenera la baseline con una modifica revisionata a parte, cambia il
digest pinnato, e si rieseguono i cancelli di qualificazione che
dipendono dalla baseline (camera bianca, notifiche 14A, ripristino).

---

## 1B — ordine delle file in Saletta Nuova

**Stato: CONFERMATO.**

Sequenza registrata, nell'ordine in cui i rack stanno nel documento:

```
AAAAAABBBBBBBBBBCCCCCCBC
```

Il fatto misurato: i 24 rack stanno in ordine di identificativo
`SN-R01…SN-R24`; le file sono contigue fino a `SN-R22` (fila C), poi
`SN-R23` è in fila **B** e `SN-R24` in fila **C**. Il «BC» finale non è
quindi un artefatto di ordinamento: è la numerazione che non segue il
raggruppamento per fila.

Risposta ricevuta: **la numerazione dei rack è incoerente in tutte le
sale, e non è un parametro rilevante.** La disposizione registrata è
quella reale e TSM la conserva verbatim — nessun riordino automatico.

Verifica di quell'affermazione contro i dati, perché una conferma che si
può controllare si controlla:

| sito | sala | rack | sequenza delle file |
|---|---|---|---|
| pomezia-g0 | backend | 10 | `AAAABBBBBB` |
| pomezia-g0 | centro-stella | 7 | `AAAAAA—` |
| pomezia-g0 | frontend | 20 | `AAAAAAAAAABBBBBBBBB—` |
| pomezia-g0 | saletta-nuova | 24 | `AAAAAABBBBBBBBBBCCCCCCBC` |
| pomezia-h0 | h0 | 25 | `AAAAAAAABBBBBBBBCCCCCCCC—` |
| oriolo-romano | sala-oriolo | 16 | `AAAAAAAAAAABBBBB` |

Saletta Nuova è la sola sala in cui l'incoerenza è **visibile** nella
stringa delle file, ma è la stessa incoerenza dappertutto: l'ordine dei
rack segue l'identificativo, non la geografia.

I tre `—` non sono lacune: sono `CS-Q01` quadro elettrico, `FE-Q01`
armadio tecnico e `H0-Q01` armadio a muro — apparati senza fila, com'è
corretto.

---

## Conseguenza per la distribuzione

1A aperto ⇒ il cancello §1 non è chiuso ⇒ Fase 2K-A resta **BLOCCATA**
su questa voce, indipendentemente dallo stato dell'infrastruttura.

Alla chiusura: aggiornare lo stato qui sopra, indicare chi ha risposto e
quando, e riportare la voce nel manifesto di accettazione
pre-installazione.
