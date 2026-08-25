# Fase 2K-A — cancelli pre-distribuzione

Stato al 2026-08-25. Rilascio in esame: commit `23d47dc`, albero
`18f7125…`, baseline `febafb7e…` (documento). **Nessuna modifica al
rilascio qualificato.**

## Esito: **A — BLOCCATO**

Non per un difetto del software. Per l'assenza del bersaglio: **`tsm-prd-01`
non esiste.**

```
Resolve-DnsName tsm-prd-01              → DNS name does not exist
Resolve-DnsName tsm-prd-01.par-tec.it   → non risolto
Test-Connection  tsm-prd-01             → nessuna risposta
```

§2 chiede di misurare la VM in esecuzione e di non fidarsi della
documentazione di provisioning. Non c'è VM da misurare, quindi §2 e
tutto ciò che ne dipende — §3 mount, §4 SELinux, §5 immagini caricate,
§6 artefatti trasferiti, §7 certificato, §8 segreti, §9 esposizione,
§12 systemd, §14 manifesto — non hanno un soggetto.

Questo non è un ostacolo da aggirare: **la fase non può concludersi
positivamente senza l'host.** Ciò che si è potuto fare è preparare
l'accettazione perché, esistendo la VM, sia un comando e non una
settimana.

---

## Registro dei cancelli

| # | cancello | stato | chi lo chiude |
|---|---|---|---|
| §1A | «Rack R09 — Test» reale? | **APERTO** | operations CED Pomezia G0 |
| §1B | ordine file Saletta Nuova | **CONFERMATO** | — |
| §2 | VM `tsm-prd-01` misurata | **BLOCCATO** — host inesistente | infrastruttura |
| §2b | swap 8 GiB | **BLOCCATO** | infrastruttura |
| §3 | `/srv/tsm-data` su disco dedicato | **BLOCCATO** | infrastruttura |
| §4 | SELinux Enforcing + codici 27/28 | **BLOCCATO** — mai esercitati | infrastruttura + questo strumento |
| §5 | Docker/Compose e immagini caricate | **BLOCCATO** | infrastruttura |
| §6 | artefatti trasferiti e verificati | **PRONTO** — strumento esistente | — |
| §7 | certificato TLS aziendale | **BLOCCATO** — non disponibile | sicurezza/PKI |
| §8 | segreti di produzione | **BLOCCATO** | operations, sull'host |
| §9 | esposizione 80/443 | **BLOCCATO** | rete |
| §10 | VEEAM-A politica preparata | **APERTO** | infrastruttura backup |
| §10b | VEEAM-B backup+ripristino reali | **POST-INSTALLAZIONE** | infrastruttura backup |
| §11 | 14B prerequisiti relay SMTP | **APERTO** — dati assenti | messaggistica |
| §12 | `tsm.service` installato, fermo e disabilitato | **BLOCCATO** | operations |
| §13 | baseline `--check` | **RINVIATO** (motivo sotto) | — |
| §14 | manifesto di accettazione | **PRONTO** — lo produce lo strumento | — |
| §15 | nessuna deriva del rilascio | **VERIFICATO** | — |

### §13 — perché rinviato e non «fatto»

`production_baseline.py --check` è non distruttivo per progetto, ma
interroga il database: `gate_schema(conn)` e i conteggi delle tabelle
hanno bisogno di una connessione. Per avere una connessione serve il
cluster avviato, e avviarlo significa `initdb` sul disco dei dati —
esattamente lo stato che §13 vieta.

Quindi non si esegue, e non si finge di averlo eseguito. Le sue
verifiche che **non** toccano il database sono già coperte altrove:

- digest del file della baseline → §6 dello strumento di accettazione;
- `TSM_BOOTSTRAP_PASSWORD` non impostata → §8;
- `TSM_ENV=production` e nessuna deroga di sviluppo → preflight (40/42).

`--allow-initialise` non è stato eseguito e non va eseguito in 2K-A.

### §15 — i commit di questa fase NON sono un nuovo rilascio

**Dichiarazione esplicita.** I commit di pre-distribuzione e di
strumentazione — quelli della Fase 2J dopo `23d47dc`, e i tre di questa
fase — **non costituiscono un nuovo rilascio applicativo.** L'identità
del rilascio resta:

| | |
|---|---|
| commit | `23d47dc1bd952e3b87282bb0e088d4771d409266` |
| albero (117 file) | `18f712556844e6ba21b32603d2aa431f06569da60cbf5ea4f2197b292dd387cb` |
| baseline (documento) | `febafb7e8b9ceac5ef69ead1e66a5add7a62a79d1c8186935430754f8066a1b1` |
| migrazione · mapper · schemaVersion | `0013_domain` · 2 · 1 |

**I percorsi distribuiti restano byte-identici all'albero qualificato.**
Non è una promessa: è una misura, e si rifà a ogni commit.

Verificato al momento della scrittura: l'albero del rilascio calcolato su
HEAD coincide con `18f7125…` su 117 file, e nessuno dei tre commit di
questa fase tocca un percorso distribuito — zero, per tutti e tre.

Tutti e tre sono **puramente additivi**: due nuovi file in `tools/`, due
nuovi documenti in `deploy/`, nessuna modifica a un file esistente.
Quindi non hanno alcun modo di alterare un byte distribuito, e la cosa si
vede dal `--stat` senza bisogno di fidarsi di questa frase.

Qui non ci sono SHA a mano di proposito: un documento che li elenca
diventa falso al primo riscrittura della cronologia — è già accaduto
mentre lo si scriveva. La proprietà si riafferma col comando qui sotto.

Da rifare — non da assumere — a ogni ripresa della fase:

```bash
python tools/release-identity.py --json     # albero == 18f7125…, 117 file
git diff --name-only 23d47dc..HEAD -- compose.yaml backend/ web/ handoff/ \
    deploy/preflight.sh deploy/tsm.service \
    deploy/baseline/production-baseline.json     # deve essere vuoto
```

Se quel `git diff` elenca qualcosa, l'identità del rilascio è cambiata:
si riesegue la qualificazione dei cancelli interessati, e questo
documento non vale più.

Perché la distinzione è necessaria e non pedanteria: §4 della Fase 2J
poneva il paradosso di un'identità che cambia quando si scrive la
documentazione dopo la qualificazione. La risposta sono **due numeri** —
il commit dice *da dove viene* il rilascio, l'albero dice *cosa
verrebbe installato*. Un commit che toccasse solo `tools/`,
`backend/tests/` o dei `.md` lascia il secondo invariato, e questo è
verificabile invece che dichiarato.

---

## Strumenti prodotti in questa fase

### `tools/release-bundle.sh`

Costruisce il pacchetto per l'host e lo verifica all'arrivo. Due
proprietà che `scp -r` non ha:

1. **I file escono da git, non dal disco.** Con `core.autocrlf=true` e un
   `.gitattributes` che copre `.sh`/`.conf` ma non `.json`, la baseline
   sul disco Windows ha CRLF: il suo digest è `9443191a…` invece di
   `c22bcb4e…`. Copiata così, il documento nel container viene letto con
   i `\r` dentro e il digest canonico non torna — il cancello della
   baseline fallisce all'installazione, per soli 1552 byte di `\r`.
2. **L'elenco di ciò che non deve partire è meccanico.** Dieci artefatti
   vietati (override di sviluppo, `nginx.dev.conf`, il sink SMTP, i
   generatori di certificati di sviluppo): se uno finisce nel pacchetto,
   lo strumento si ferma. Lo strumento di accettazione ne cerca undici —
   i dieci più `deploy/baseline/fixture.json`, che non può comparire nel
   pacchetto ma potrebbe essere già sull'host.

Distingue **identità** e **documentazione**: `compose.yaml`,
`preflight.sh`, `tsm.service` e la baseline escono dal commit
qualificato; README e manifesti da HEAD, perché non fanno parte
dell'identità del rilascio e all'operatore serve la versione più recente.

### `tools/preinstall-acceptance.sh`

Misura l'host e lo confronta col rilascio. Non installa, non avvia, non
crea stato applicativo. Esegue il **preflight vero** e ne interpreta i
diciassette codici di errore invece di riscriverne i controlli: due copie
delle stesse regole divergono, e quella che sbaglia è sempre quella che
non gira in produzione.

`--selinux-gate` esercita i codici **27** e **28**, mai esercitati in
Fase 2J perché WSL non fa girare SELinux enforcing: rompe l'etichetta
della directory dei dati, pretende l'uscita 27, ripristina, **confronta
il contesto ripristinato con quello originale**, e ripete con la regola
`semanage` rimossa per il 28. Il ripristino è verificato, non dato per
fatto — un ripristino dato per fatto è il difetto trovato nel backup
della fase 2J.

---

## Cosa serve per riprendere

1. **La VM.** 4 vCPU, 8 GiB RAM, **8 GiB di swap**, due dischi thin da
   100 GB, RHEL-family con SELinux **Enforcing**, `/srv/tsm-data` da
   `fstab` su filesystem dedicato.
2. **Il certificato aziendale** con SAN che contenga il nome con cui gli
   operatori raggiungeranno TSM.
3. **La risposta su R09.**
4. **VEEAM-A**: nome della politica, ambito, pianificazione, retention,
   se è VM-consistent o application-consistent, e il responsabile.
5. **I dati del relay SMTP**: host, porta, modalità TLS, mittente,
   meccanismo di autenticazione, e **un destinatario di prova
   approvato** — non la lista completa dei destinatari finali.

Poi:

```bash
tools/release-bundle.sh --out /tmp/tsm-rc --with-images   # su una macchina con le immagini
# trasferimento del pacchetto
tools/release-bundle.sh --verify /opt/tsm                  # sull'host
tools/preinstall-acceptance.sh --dir /opt/tsm \
    --selinux-gate --manifest /root/accettazione.md
```

Uscita 0 ⇒ **B — pronto per la distribuzione controllata**, con 14B e
VEEAM-B ancora aperti come cancelli post-installazione. Fino a quando
non passano, notifiche disattivate.

---

## Invarianti da non cambiare

**Il pacchetto si estrae dal contenuto degli oggetti git.** `git show
<commit qualificato>:<percorso>`, mai una copia da un checkout di
piattaforma. È la ragione d'essere dello strumento: nessuna
trasformazione di fine-riga o di workspace può alterare i byte
qualificati. Se qualcuno un giorno sostituisce quell'estrazione con un
`cp` o un `scp` «perché è più semplice», la baseline arriva con 1552 byte
di `\r` e il digest canonico non torna. Non è un dettaglio di stile.

**L'ambiente di qualificazione WSL resta fermo ma registrato.** La distro
`Ubuntu-24.04` non va rimossa per ora: serve come controllo negativo
verificabile per gli strumenti di questa fase, e `wsl --unregister`
è irreversibile. Stato atteso: `Stopped`, `tsm.service` `inactive` e
`disabled`, segreti di qualificazione distrutti.

**Fino alla chiusura di 14B le notifiche di produzione restano
disattivate**, e VEEAM-B non si considera soddisfatto dal ripristino
sintetico della Fase 2J.

---

## Note di prova su questi strumenti

Onestà su cosa è stato eseguito dove.

Eseguito per davvero: `release-bundle.sh` costruisce e verifica il
pacchetto, e i suoi controlli sono stati **falsificati** — un byte
alterato in `compose.yaml` produce KO, un artefatto vietato piantato nel
pacchetto produce KO.

`preinstall-acceptance.sh` è stato eseguito sull'host di qualificazione
WSL come **controllo negativo**: 37 OK, 16 KO, 5 aperti, e ognuno dei 16
è una deviazione reale di quella macchina da `tsm-prd-01` (swap 2 GiB,
8 vCPU, SELinux assente, cluster preesistente, cinque artefatti vietati,
segreti distrutti dopo la 2J, nessun TLS). Uno script che dice «pronto»
su una macchina così non sarebbe servito a niente.

Difetti degli strumenti trovati eseguendoli, non leggendoli:

- il controllo dei CR usava `grep -q $'\r'`, che **non trova nulla**
  quando la grep considera `\r\n` il terminatore di riga: passava un file
  CRLF in silenzio. Sostituito con un confronto di conteggi di byte, che
  nessuna interpretazione dei fine-riga può alterare;
- il conteggio dei controlli superati dal preflight cercava `OK` mentre
  il preflight stampa `[ok]`: leggeva zero per costruzione, e uno zero
  per costruzione ha l'aspetto di un'informazione;
- la contro-prova della spazzata dei segreti confondeva «non c'era
  niente da cercare» con «la ricerca è rotta». Sono tre esiti, non due;
- confrontava il digest del **file** della baseline con quello del
  **documento**: due numeri diversi. Il secondo è
  `canonical_sha256(canonicalise(doc))` e non è calcolabile da una
  shell — lo verifica il prodotto;
- l'host WSL di qualificazione si chiama **anch'esso** `tsm-prd-01`, e il
  controllo dell'hostname vi passava. Il nome non è una prova d'identità:
  aggiunto un controllo sul kernel, perché misurare la macchina sbagliata
  e crederci è l'errore operativo più probabile di questa fase.

**Non esercitato da nessuna parte:** i percorsi SELinux 27 e 28. Il
codice esiste e non ha mai girato, perché non esiste ancora una macchina
con SELinux enforcing su cui girare. È scritto qui perché nessuno lo
consideri verificato.
