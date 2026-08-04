# Trust Technologies Server Manager — Handoff sviluppo

Prototipo frontend completo per la gestione delle sale server (CED) multi-sito,
pronto per l'aggancio a un backend. Ultimo aggiornamento: luglio 2026.

## File del pacchetto
- `Sala Server v2.dc.html` — applicazione completa (markup + logica in un unico file; richiede `support.js` accanto)
- `inventario.js` — **modulo dati** (ES module, export `DATI`): unica fonte di verità del prototipo
- `xlsx.js` — lettore/scrittore Excel autonomo (ZIP+CRC32 in scrittura, DecompressionStream in lettura): nessuna dipendenza esterna
- `fonts/` — Public Sans + Roboto Mono (woff2) + fonts.css: nessuna chiamata a Google Fonts, ok in rete chiusa
- `trust-logo.png` / `trust-logo-dark.png` — logo per tema chiaro / scuro
- `Sale Server Pomezia (standalone).html` — build tutto-in-uno offline (demo e consultazione, doppio click)

Credenziali di collaudo: **admin / admin** (utenza marcata nel dato, da rimuovere in produzione).

## Modello dati (DATI)
```
{
  versione: number,
  utenti: [{ email, ruolo, password, pwTemp, nome, cognome, telefono, team }],
                                        // ruolo: view | edit | admin; pwTemp = password provvisoria
  notifiche: { email, giorni, attive },  // promemoria scadenze
  smtp: { host, porta, utente, password, mittente, tls },
  manuale: [{ id, titolo, blocchi: [{ titolo, paragrafi: [] }] }],   // voci aggiunte dagli admin
  registro: [{ ts, ruolo, azione }],     // audit log client-side (max 500, FIFO)
  locations: [{
    id, nome,                            // sito (pomezia-g0, pomezia-h0, oriolo-romano)
    sale: [{
      id, nome, w, h, area, dim,         // dimensioni in METRI
      segnaposto?,                       // true = planimetria indicativa
      vani: [{ x, y, w, h, porta?, porta2? }],       // porta: { lato, x|y, w }
      racks: [{
        id, name, row, u,                // u = unità totali (standard 45)
        x, y, w, h,                      // posizione in pianta, metri
        seriali: [],                     // 1-2 numeri di serie (matching asset, mostrati in pianta)
        foto?,                           // dataURL
        devices: [{
          id, name, type,                // server | rete | storage | firewall | alimentazione | altro
          stato,                         // attivo | manutenzione | dismissione | dismesso
          model, ip, serial, owner,
          garanzia, supporto,            // date ISO di scadenza
          note, u, h                     // u = slot dal basso, h = altezza in U
        }]
      }]
    }]
  }]
}
```

## Punti di aggancio backend (in ordine di priorità)
1. **Persistenza** — ogni scrittura passa da `persist(next, azione)`; la lettura iniziale è in
   `componentDidMount` (import di `inventario.js`). Sostituire con GET/PUT verso l'API.
   Lo stack di undo/redo è in memoria: valutare un versionamento server-side.
   Spostare il registro modifiche lato server per un audit affidabile.
2. **Auth e ruoli** — `_doLogin()` verifica utenza/password sull'elenco `utenti`: sostituire con il
   sistema di autenticazione aziendale. `state.user.ruolo` porta il privilegio, i flag `canEdit` /
   `canAdmin` / `isAdminUser` sono calcolati in un punto solo di `renderVals`. Il toggle
   Visualizzazione/Editing riguarda solo i dati delle sale, non i permessi.
   Password provvisoria: flag `pwTemp`, il cambio forzato al primo accesso è già gestito lato UI.
3. **Job scadenze → email** — cron giornaliero: legge i device con `garanzia`/`supporto` entro
   `notifiche.giorni` e invia via `smtp`. ⚠ la password SMTP è in chiaro nel JSON: spostarla in un secret.
4. **Import/Export** — CSV o XLSX dei dispositivi con intestazioni: location, sala, rack, nome, tipo,
   stato, modello, ip, seriale, referente, u, h, garanzia, supporto, note. JSON = intero DATI (backup).
   L'import è in due fasi (analisi con anteprima → conferma) e non scrive nulla senza "Applica".

## Funzionalità implementate lato UI
Login con privilegi e password provvisoria; pagina Profilo (dati personali, password, gestione utenze);
dashboard di riepilogo (KPI, occupazione complessiva e per sala, urgenze, siti); pianta 2D/3D con
orbita, zoom, pan, rotazione 90°, dispositivi visibili sui rack 3D; drag & drop di rack (sposta e
ridimensiona) e di dispositivi tra slot U; CRUD completo di siti, sale, rack (con doppio seriale) e
dispositivi; ricerca globale testo + range IP (CIDR, intervallo, wildcard); inventario tabellare
ordinabile/filtrabile con colonne configurabili; viste Capacità (U libere e miglior slot contiguo) e
Scadenze; stati con ciclo di vita attivo → manutenzione → dismissione → dismesso; confronto tra
rilievi (JSON/CSV/XLSX) con report discrepanze scaricabile in Excel o CSV; export Excel formattato a
tre fogli, CSV, JSON e stampa; scheda rack stampabile; import guidato con validazione e anteprima;
undo/redo con profondità configurabile; tre temi (Scuro, Chiaro, TIM); manuale d'uso integrato con
voci personalizzabili dagli admin; scorciatoie / Ctrl+F, D, V, Ctrl+E, Ctrl+B, Ctrl+Z, Ctrl+Y;
accessibilità: contrasto AA, navigazione da tastiera, focus visibile, prefers-reduced-motion.

## Note tecniche
- Coordinate della pianta in metri, rendering in percentuale: qualsiasi risoluzione è supportata
- Colori: accento per tema (`--accent`/`--accentSoft`), rosso TIM #e2231a; tipi e stati hanno palette dedicate.
  La classe del tema va su `document.documentElement`: gli accenti usati negli style object sono calcolati in JS
  perché il cambio tema si applichi anche ai nodi già montati
- Font locali in `fonts/`: nessuna richiesta di rete a runtime
- Digitalizzazione planimetrie: oggi manuale (crea sala con le misure, poi i rack posizionandoli col mouse).
  Per l'automazione da PDF/immagine serve un passo server-side di vettorizzazione: il modello dati è già pronto
  (vani + racks in metri), quindi l'endpoint dovrebbe restituire esattamente quella struttura
