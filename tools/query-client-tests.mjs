// ============================================================
// query-client-tests.mjs — le due regole di handoff/queries.js (fase 2H)
//
// Prova le due proprietà su cui poggia tutta la migrazione del frontend, e le prova
// SENZA browser: sono logica, non interfaccia, e un test che le verifica in un
// browser vero le verificherebbe più lentamente e con meno controllo sull'ordine
// degli arrivi — che è precisamente ciò che va controllato.
//
// Il test del browser (tools/browser-e2e-test.py) prova la stessa cosa attraverso
// nginx: là si prova che l'interfaccia è collegata a questa logica, qui che la logica
// è giusta. Sono due domande diverse e servono entrambe.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/query-client-tests.mjs
// ============================================================
import {
  ERROR, MISMATCH, OK, QueryClient, STALE, debounce, revisionOf, sameRevision,
} from '../handoff/queries.js';

let pass = 0;
const failures = [];
const ok = (name, cond, detail = '') => {
  if (cond) pass++;
  else {
    failures.push(name);
    console.log(`  [FAIL] ${name}${detail ? '\n         → ' + detail : ''}`);
  }
};
const eq = (name, got, want) =>
  ok(name, JSON.stringify(got) === JSON.stringify(want),
     `atteso ${JSON.stringify(want)}, ottenuto ${JSON.stringify(got)}`);

const attesa = (ms) => new Promise((r) => setTimeout(r, ms));
const REV = { version: 7, sha256: 'a'.repeat(64) };
const risposta = (extra = {}) => ({ version: REV.version, sha256: REV.sha256, ...extra });

// ============================================================
console.log('\n1. la revisione di una risposta');
// ============================================================
eq('revisionOf: risposta completa', revisionOf(risposta()), REV);
eq('revisionOf: senza sha', revisionOf({ version: 1 }), null);
eq('revisionOf: sha vuoto', revisionOf({ version: 1, sha256: '' }), null);
eq('revisionOf: versione testuale', revisionOf({ version: '1', sha256: 'x' }), null);
eq('revisionOf: null', revisionOf(null), null);

ok('sameRevision: identiche', sameRevision(REV, { ...REV }));
ok('sameRevision: sha diverso a pari versione NON è la stessa',
   !sameRevision(REV, { version: 7, sha256: 'b'.repeat(64) }));
ok('sameRevision: versione diversa', !sameRevision(REV, { version: 8, sha256: REV.sha256 }));
ok('sameRevision: con null', !sameRevision(REV, null) && !sameRevision(null, REV));

// ⚠ La controprova del confronto su ENTRAMBI i valori. Senza di lei questo file
// dimostrerebbe che una funzione confronta due oggetti; con lei dimostra che il caso
// del rollback — due revisioni con lo stesso NUMERO e contenuto diverso — non passa.
{
  const dopoRollback = { version: 7, sha256: 'c'.repeat(64) };
  ok('rollback: stesso numero, contenuto diverso → revisioni diverse',
     !sameRevision(REV, dopoRollback),
     'un confronto sul solo numero di versione accetterebbe questo caso');
}

// ============================================================
console.log('2. una risposta vecchia non sovrascrive una nuova');
// ============================================================
//
// ⚠ Il test che il requisito chiede per nome (§3): A parte, B parte, B TORNA, A torna
// dopo. Deve restare B.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const ordine = [];

  // A: lenta. B: veloce. Il ritardo è deterministico, non una gara.
  const A = client.run('search', () => attesa(60).then(() => risposta({ query: 'A' })));
  await attesa(5);
  const B = client.run('search', () => attesa(5).then(() => risposta({ query: 'B' })));

  const [ra, rb] = await Promise.all([A, B]);
  ordine.push(ra.status, rb.status);

  eq('A (partita prima, tornata dopo) è dichiarata superata', ra.status, STALE);
  eq('B (l\'ultima chiesta) è quella da consumare', rb.status, OK);
  eq('B porta il proprio risultato', rb.payload.query, 'B');
  ok('A non porta nessun risultato da mostrare', ra.payload === undefined,
     'una risposta superata che porta ancora dei dati è una tentazione: '
     + 'basta un ramo distratto e finisce sullo schermo');
  eq('due richieste, due esiti', ordine.length, 2);
}

// L'annullamento esplicito: la vista si chiude mentre la richiesta è in volo.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const p = client.run('capacity', () => attesa(30).then(() => risposta()));
  client.cancel('capacity');
  const r = await p;
  eq('una richiesta annullata è superata, non un errore', r.status, STALE);
}

// ⚠ MUTAZIONE SFUGGITA: togliere `abort()` da `run` non rendeva rosso niente.
//
// I test di sopra provano che una risposta superata non viene USATA, e per quello basta
// il contatore di generazione. Ma non aspettare una risposta è una cosa diversa dal
// non usarla: senza l'abort, digitare tredici caratteri lascia dodici richieste in volo
// che il server calcola per intero e il browser scarica. La differenza si vede solo
// guardando il SEGNALE della richiesta precedente, ed è ciò che questo test fa.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const segnali = [];
  const p1 = client.run('search', (signal) => {
    segnali.push(signal);
    return attesa(60).then(() => risposta({ query: 'prima' }));
  });
  await attesa(10);
  const p2 = client.run('search', (signal) => {
    segnali.push(signal);
    return attesa(5).then(() => risposta({ query: 'seconda' }));
  });
  const [r1, r2] = await Promise.all([p1, p2]);

  eq('la prima è superata', r1.status, STALE);
  eq('la seconda si consuma', r2.status, OK);
  eq('due richieste, due segnali', segnali.length, 2);
  ok('una richiesta nuova ANNULLA quella in volo sullo stesso slot',
     segnali[0] && segnali[0].aborted === true,
     'il segnale della prima non risulta annullato: senza abort il server calcola '
     + 'per intero una risposta che nessuno leggerà, e il browser la scarica');
  ok('e quella nuova non è annullata',
     segnali[1] && segnali[1].aborted === false);
}

// Slot diversi non si annullano fra loro: la ricerca non deve uccidere la capacità.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const a = client.run('search', () => attesa(20).then(() => risposta({ n: 1 })));
  const b = client.run('capacity', () => attesa(5).then(() => risposta({ n: 2 })));
  const [ra, rb] = await Promise.all([a, b]);
  eq('interrogazioni diverse convivono (ricerca)', ra.status, OK);
  eq('interrogazioni diverse convivono (capacità)', rb.status, OK);
}

// Il segnale arriva davvero a chi esegue: senza, l'annullamento risparmierebbe solo
// la lettura del risultato e non la richiesta.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  let visto = null;
  const p = client.run('search', (signal) => {
    visto = signal;
    return attesa(30).then(() => risposta());
  });
  client.cancel('search');
  await p;
  ok('il fetcher riceve un AbortSignal', visto !== undefined && visto !== null);
  ok('e il segnale risulta annullato', visto && visto.aborted === true);
}

// ============================================================
console.log('3. un risultato di un\'altra revisione non si mostra');
// ============================================================
{
  // La revisione caricata avanza mentre la richiesta è in volo: è il caso del collega
  // che salva. Il primo giro trova la revisione vecchia, si ricarica, il secondo giro
  // trova quella nuova.
  let caricata = { version: 7, sha256: 'a'.repeat(64) };
  let ricaricamenti = 0;
  let giro = 0;
  const client = new QueryClient({
    getLoadedRevision: () => caricata,
    reloadInventory: async () => { ricaricamenti++; caricata = { version: 8, sha256: 'b'.repeat(64) }; },
  });
  const r = await client.run('search', async () => {
    giro++;
    // La prima risposta appartiene alla revisione 8, che il client non ha ancora.
    return { version: 8, sha256: 'b'.repeat(64), query: `giro${giro}` };
  });
  eq('dopo il ricaricamento il risultato si consuma', r.status, OK);
  eq('ha riprovato una volta sola', giro, 2);
  eq('e ha ricaricato una volta sola', ricaricamenti, 1);
  eq('il risultato mostrato è quello del secondo giro', r.payload.query, 'giro2');
}

{
  // Il caso senza uscita: la revisione continua a cambiare. Non deve diventare un
  // ciclo di richieste.
  let n = 100;
  let ricaricamenti = 0;
  const client = new QueryClient({
    getLoadedRevision: () => ({ version: n, sha256: 'x'.repeat(64) }),
    reloadInventory: async () => { ricaricamenti++; n += 1; },
  });
  const r = await client.run('search', async () => ({
    version: n + 1, sha256: 'y'.repeat(64), query: 'mai uguale',
  }));
  eq('revisione irraggiungibile → mismatch, non un ciclo', r.status, MISMATCH);
  ok('il numero di ricaricamenti è limitato', ricaricamenti <= 1,
     `ricaricamenti=${ricaricamenti}: un tentativo per richiesta, non un ciclo`);
  ok('e non porta nessun risultato da mostrare', r.payload === undefined);
}

{
  // Nessun inventario caricato: non si mostra e non si ricarica in eterno.
  const client = new QueryClient({ getLoadedRevision: () => null });
  const r = await client.run('search', async () => risposta());
  eq('senza inventario caricato non si consuma', r.status, MISMATCH);
}

{
  // Una risposta SENZA revisione non è consumabile: significherebbe fidarsi.
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const r = await client.run('search', async () => ({ results: [] }));
  eq('risposta senza revisione → mismatch', r.status, MISMATCH);
}

// ============================================================
console.log('4. gli errori restano errori');
// ============================================================
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  const boom = Object.assign(new Error('503'), { status: 503, code: 'projection_not_current' });
  const r = await client.run('capacity', async () => { throw boom; });
  eq('un 503 è un errore', r.status, ERROR);
  eq('e porta l\'errore del server, non uno inventato qui', r.error.code, 'projection_not_current');
  ok('un errore NON porta un payload da mostrare', r.payload === undefined,
     'se lo portasse, un ramo distratto disegnerebbe la vista con dati inventati');
}

// ⚠ Il contatore serve a MISURARE, quindi deve contare. Il test del browser conta le
// richieste per battuta leggendo `stats.requests`: se restasse a zero, quella misura
// direbbe «nessuna richiesta» per un'interfaccia che ne fa dieci.
{
  const client = new QueryClient({ getLoadedRevision: () => REV });
  await client.run('a', async () => risposta());
  await client.run('a', async () => { throw new Error('x'); });
  eq('le richieste si contano', client.stats.requests, 2);
  eq('gli errori si contano', client.stats.errors, 1);
}

// ============================================================
console.log('5. il ritardo della digitazione');
// ============================================================
{
  let chiamate = [];
  const d = debounce((v) => chiamate.push(v), 20);
  d('s'); d('sr'); d('srv');
  ok('durante la digitazione non parte niente', chiamate.length === 0);
  ok('e il ritardo è dichiarato pendente', d.pending());
  await attesa(40);
  eq('parte una richiesta sola, con l\'ultimo testo', chiamate, ['srv']);

  chiamate = [];
  d('a'); d.flush();
  eq('flush non aspetta (è il tasto Invio)', chiamate, ['a']);
  ok('e dopo il flush non resta niente in sospeso', !d.pending());

  chiamate = [];
  d('b'); d.cancel();
  await attesa(40);
  eq('cancel annulla (la vista si chiude)', chiamate, []);
}

// ============================================================
console.log('\n' + '='.repeat(70));
console.log(`controlli passati: ${pass}   falliti: ${failures.length}`);
if (failures.length) {
  console.log('RISULTATO: il client delle interrogazioni non rispetta il contratto');
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log('RISULTATO: le due regole del client delle interrogazioni valgono');
