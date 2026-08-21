// ============================================================
// make-domain-fixtures.mjs — genera fixtures/domain/*.json
//
// Le fixture del DOMINIO sono il contratto della fase 2G: una semantica sola, che
// Python/SQL e JavaScript devono soddisfare entrambi.
//
// ⚠ Le attese qui sono SCRITTE A MANO, come in make-identity-fixtures.mjs e al
// contrario di make-query-fixtures.mjs.
//
// La differenza non è stilistica, è la ragione per cui questa fase esiste. Quel
// generatore CALCOLAVA le attese copiando alla lettera il JavaScript del frontend,
// perché ciò che andava dimostrato era la parità con il comportamento che girava:
// scrivere le attese a mano avrebbe dimostrato soltanto che lo SQL corrispondeva alla
// mia lettura del prototipo. Qui la domanda è rovesciata. Il comportamento del
// prototipo NON è più il contratto — è ciò che la fase 2G sostituisce — e l'attesa è
// una decisione di prodotto. Calcolarla da una delle due implementazioni renderebbe
// il contratto vacuo: se sbagliassero entrambe allo stesso modo, nessun test
// diventerebbe rosso.
//
// Da qui la regola per chi modifica questo file: **un'attesa non si aggiorna perché
// un test è rosso.** Si aggiorna quando la decisione di prodotto cambia, e allora il
// test rosso è il messaggio che l'implementazione non l'ha ancora seguita.
//
// L'unica eccezione, dichiarata: `addresses-fuzz.json`. Cinquemila forme mutate non si
// possono benedire a mano, e non è quello il loro scopo — servono a dimostrare che le
// DUE implementazioni non divergono su nessuna. I verdetti li scrive questo
// generatore (cioè JavaScript), e la suite Python pretende di produrre gli stessi: è
// un confronto differenziale, non un giudizio di prodotto. I casi che portano una
// decisione stanno tutti in `addresses.json`, con le attese a mano.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/make-domain-fixtures.mjs
// ============================================================
import { mkdirSync, writeFileSync, readdirSync, rmSync, existsSync } from 'node:fs';
import { parseAddress, parseAddressQuery } from '../handoff/domain.js';

const OUT = 'fixtures/domain';

if (existsSync(OUT)) {
  for (const f of readdirSync(OUT)) if (f.endsWith('.json')) rmSync(`${OUT}/${f}`);
}
mkdirSync(OUT, { recursive: true });

const written = [];
function emit(name, payload) {
  writeFileSync(`${OUT}/${name}.json`, JSON.stringify(payload, null, 1) + '\n', 'utf8');
  written.push(name);
}

// ============================================================
// 1. PRESENZA — stato operativo e presenza fisica sono indipendenti
// ============================================================
//
// L'elenco copre le sei combinazioni del requisito §1 più i modi in cui il campo può
// mancare. `occupies` è la risposta alla domanda fisica, `notifies` a quella
// operativa: nessuna delle due si deduce dall'altra, ed è esattamente ciò che le
// attese qui sotto fissano — le due colonne cambiano in modo indipendente.
emit('presence', {
  _nota: "stato operativo (attivo|manutenzione|dismissione|dismesso) e presenza fisica "
       + "(presente|rimosso) sono ORTOGONALI. occupies dipende solo dalla presenza, "
       + "notifies solo dallo stato. L'assenza di presenza canonicalizza a presente.",
  cases: [
    { name: 'attivo + presente',
      device: { stato: 'attivo', presenza: 'presente' },
      stato: 'attivo', presenza: 'presente', occupies: true, notifies: true },
    { name: 'manutenzione + presente',
      device: { stato: 'manutenzione', presenza: 'presente' },
      stato: 'manutenzione', presenza: 'presente', occupies: true, notifies: true },
    { name: 'dismissione + presente',
      device: { stato: 'dismissione', presenza: 'presente' },
      stato: 'dismissione', presenza: 'presente', occupies: true, notifies: true },
    { name: 'dismesso + presente: fuori servizio ma occupa ancora lo slot',
      device: { stato: 'dismesso', presenza: 'presente' },
      stato: 'dismesso', presenza: 'presente', occupies: true, notifies: false },
    { name: 'dismesso + rimosso: non occupa e non avvisa',
      device: { stato: 'dismesso', presenza: 'rimosso' },
      stato: 'dismesso', presenza: 'rimosso', occupies: false, notifies: false },
    { name: 'attivo + rimosso: portato via ma ancora in servizio altrove',
      device: { stato: 'attivo', presenza: 'rimosso' },
      stato: 'attivo', presenza: 'rimosso', occupies: false, notifies: true },
    { name: 'legacy senza presenza: presente per difetto',
      device: { stato: 'attivo' },
      stato: 'attivo', presenza: 'presente', occupies: true, notifies: true },
    { name: 'legacy dismesso senza presenza: NON si deduce rimosso',
      device: { stato: 'dismesso' },
      stato: 'dismesso', presenza: 'presente', occupies: true, notifies: false },
    { name: 'presenza vuota: il default, come stato vuoto',
      device: { stato: '', presenza: '' },
      stato: 'attivo', presenza: 'presente', occupies: true, notifies: true },
    { name: 'presenza null',
      device: { stato: null, presenza: null },
      stato: 'attivo', presenza: 'presente', occupies: true, notifies: true },
    { name: 'presenza fuori vocabolario: si conserva e occupa',
      device: { stato: 'attivo', presenza: 'in transito' },
      stato: 'attivo', presenza: 'in transito', occupies: true, notifies: true },
    { name: 'stato fuori vocabolario: si conserva e avvisa',
      device: { stato: 'rottamato', presenza: 'presente' },
      stato: 'rottamato', presenza: 'presente', occupies: true, notifies: true },
    { name: 'presenza numerica da foglio di calcolo',
      device: { stato: 'attivo', presenza: 0 },
      stato: 'attivo', presenza: '0', occupies: true, notifies: true },
    { name: 'presenza booleana: non e un valore del vocabolario, vale il default',
      device: { stato: 'attivo', presenza: false },
      stato: 'attivo', presenza: 'presente', occupies: true, notifies: true },
    { name: 'maiuscole: il vocabolario e sensibile, Rimosso NON e rimosso',
      device: { stato: 'attivo', presenza: 'Rimosso' },
      stato: 'attivo', presenza: 'Rimosso', occupies: true, notifies: true },
  ],
});

// ============================================================
// 2. CAPACITÀ — slot U DISTINTI occupati
// ============================================================
//
// `usedU` è il numero di slot fisici distinti occupati da dispositivi la cui presenza
// non è `rimosso`. Le attese sono contate a mano, slot per slot: è l'unico modo di
// non riprodurre l'errore che si sta correggendo.
//
// `sumH` è riportato accanto SOLO dove differisce, e serve a una cosa sola: rendere
// visibile che il difetto è tornato. Un test che confronta `usedU` con `sumH` e li
// trova uguali su tutti i casi non può diventare rosso quando qualcuno riscrive
// `SUM(h)`.
emit('capacity', {
  _nota: "usedU = slot U DISTINTI occupati da dispositivi con presenza != rimosso. "
       + "sumH e la vecchia definizione sbagliata, riportata dove differisce perche "
       + "un test possa dimostrare di distinguerle.",
  cases: [
    { name: 'due dispositivi contigui',
      rackU: 45,
      devices: [{ u: 1, h: 2 }, { u: 3, h: 1 }],
      usedU: 3, freeU: 42, largestFreeRun: 42, percent: 7,
      slots: [1, 2, 3] },

    { name: 'SOVRAPPOSTI: lo slot in comune conta una volta sola',
      rackU: 10,
      devices: [{ u: 1, h: 3 }, { u: 3, h: 3 }],
      usedU: 5, freeU: 5, largestFreeRun: 5, percent: 50,
      slots: [1, 2, 3, 4, 5], sumH: 6 },

    { name: 'SPORGENZA: un 4U a U9 di un rack da 10 occupa due unita',
      rackU: 10,
      devices: [{ u: 9, h: 4 }],
      usedU: 2, freeU: 8, largestFreeRun: 8, percent: 20,
      slots: [9, 10], sumH: 4 },

    { name: 'h = 0 vale 1',
      rackU: 10, devices: [{ u: 5, h: 0 }],
      usedU: 1, freeU: 9, largestFreeRun: 5, percent: 10, slots: [5],
      _nota: "nessun sumH: la formula legacy era `d.h || 1`, quindi anche lei dava 1. "
           + "Questo caso NON distingue le due definizioni, e dichiararlo e meglio "
           + "che scrivere un confronto che non puo fallire." },

    { name: 'h assente vale 1',
      rackU: 10, devices: [{ u: 5 }],
      usedU: 1, freeU: 9, largestFreeRun: 5, percent: 10, slots: [5] },

    { name: 'h null vale 1',
      rackU: 10, devices: [{ u: 5, h: null }],
      usedU: 1, freeU: 9, largestFreeRun: 5, percent: 10, slots: [5] },

    { name: 'h NEGATIVO non occupa niente',
      rackU: 10, devices: [{ u: 5, h: -3 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [], sumH: -3 },

    { name: 'slot iniziale 0: la parte sotto il rack non esiste',
      rackU: 10, devices: [{ u: 0, h: 3 }],
      usedU: 2, freeU: 8, largestFreeRun: 8, percent: 20, slots: [1, 2], sumH: 3 },

    { name: 'slot iniziale negativo, interamente fuori',
      rackU: 10, devices: [{ u: -5, h: 3 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [], sumH: 3 },

    { name: 'slot iniziale oltre la cima',
      rackU: 10, devices: [{ u: 11, h: 2 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [], sumH: 2 },

    { name: 'RIMOSSO: non occupa, e questa e la voce che il difetto cancellava',
      rackU: 10,
      devices: [{ u: 1, h: 2, presenza: 'rimosso' }, { u: 5, h: 1 }],
      usedU: 1, freeU: 9, largestFreeRun: 5, percent: 10, slots: [5], sumH: 3 },

    { name: 'dismesso + PRESENTE occupa: e la voce che il ramo vuoto del prototipo '
          + 'lasciava per caso corretta, e che ora e corretta per decisione',
      rackU: 10,
      devices: [{ u: 1, h: 2, stato: 'dismesso', presenza: 'presente' }],
      usedU: 2, freeU: 8, largestFreeRun: 8, percent: 20, slots: [1, 2] },

    { name: 'dismesso + rimosso non occupa',
      rackU: 10,
      devices: [{ u: 1, h: 2, stato: 'dismesso', presenza: 'rimosso' }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [], sumH: 2 },

    { name: 'manutenzione occupa: lo stato operativo non muove la fisica',
      rackU: 10, devices: [{ u: 4, h: 2, stato: 'manutenzione' }],
      usedU: 2, freeU: 8, largestFreeRun: 5, percent: 20, slots: [4, 5] },

    { name: 'legacy senza presenza occupa',
      rackU: 10, devices: [{ u: 4, h: 2, stato: 'dismesso' }],
      usedU: 2, freeU: 8, largestFreeRun: 5, percent: 20, slots: [4, 5] },

    { name: 'rack vuoto: libero per intero',
      rackU: 42, devices: [],
      usedU: 0, freeU: 42, largestFreeRun: 42, percent: 0, slots: [] },

    { name: 'rack pieno',
      rackU: 4, devices: [{ u: 1, h: 4 }],
      usedU: 4, freeU: 0, largestFreeRun: 0, percent: 100, slots: [1, 2, 3, 4] },

    { name: 'buco in mezzo: il blocco contiguo piu ampio e quello sopra',
      rackU: 20,
      devices: [{ u: 1, h: 2 }, { u: 6, h: 1 }],
      usedU: 3, freeU: 17, largestFreeRun: 14, percent: 15, slots: [1, 2, 6] },

    { name: 'due isole ADIACENTI: nessuno slot libero fra loro',
      rackU: 20,
      devices: [{ u: 1, h: 5 }, { u: 6, h: 5 }],
      usedU: 10, freeU: 10, largestFreeRun: 10, percent: 50,
      slots: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] },

    { name: 'rack di altezza 0: non ha unita, non e occupato al 100%',
      rackU: 0, devices: [{ u: 1, h: 1 }],
      usedU: 0, freeU: 0, largestFreeRun: 0, percent: 0, slots: [] },

    { name: 'rack di altezza negativa',
      rackU: -5, devices: [{ u: 1, h: 1 }],
      usedU: 0, freeU: 0, largestFreeRun: 0, percent: 0, slots: [] },

    { name: 'rack di altezza null',
      rackU: null, devices: [{ u: 1, h: 1 }],
      usedU: 0, freeU: 0, largestFreeRun: 0, percent: 0, slots: [] },

    { name: 'u non intero: non si arrotonda un dato che non e uno slot',
      rackU: 10, devices: [{ u: 2.5, h: 1 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [] },

    { name: 'u testuale: nel documento sarebbe finito in extra, e la colonna e NULL',
      rackU: 10, devices: [{ u: '3', h: 1 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [] },

    { name: 'u booleano: true non e 1',
      rackU: 10, devices: [{ u: true, h: 1 }],
      usedU: 0, freeU: 10, largestFreeRun: 10, percent: 0, slots: [] },

    { name: 'RACK ENORME: nessuna enumerazione degli slot, il conto e sugli estremi',
      rackU: 3000000000,
      devices: [{ u: 1, h: 2 }, { u: 2999999999, h: 5 }],
      usedU: 4, freeU: 2999999996, largestFreeRun: 2999999996, percent: 0,
      _nota: "3 miliardi di slot: enumerarli esaurisce la memoria del browser e "
           + "produce 3 miliardi di righe in SQL. Nessun campo slots: e il caso in "
           + "cui materializzare l'insieme e proprio la cosa da non fare." },

    { name: 'tre sovrapposti, uno rimosso, uno sporgente',
      rackU: 12,
      devices: [{ u: 2, h: 3 }, { u: 3, h: 4, presenza: 'rimosso' },
                { u: 11, h: 6 }, { u: 4, h: 1 }],
      usedU: 5, freeU: 7, largestFreeRun: 6, percent: 42,
      slots: [2, 3, 4, 11, 12], sumH: 14 },
  ],
});

// ============================================================
// 2-bis. ALTEZZA DEL RACK — il limite dichiarato, e cosa NON è dentro il limite
// ============================================================
//
// Chiude la voce 16 del registro (§8.48). Il caso da leggere due volte non è
// `2147483648` — è `'45'`: una stringa **passa**, e non per distrazione. La regola
// non dice «u deve essere un intero», dice «u non deve poter significare due numeri
// diversi». Con `'45'` la colonna resta NULL e `_as_int` restituisce `null`: SQL e
// modello puro vedono entrambi un rack senza altezza, e non c'è nessuna divergenza da
// impedire. Con `2147483648` la colonna resta NULL ma il documento porta il numero
// vero: SQL calcola su un rack senza altezza, il modello puro su tre miliardi di U.
//
// Se un domani la regola diventasse «u deve essere un intero», questi tre casi
// diventerebbero rossi e direbbero esattamente quale decisione è cambiata.
emit('rack-height', {
  _nota: "rackHeightSupported(u): il «no» copre SOLO l'intero fuori intervallo, che e "
       + "l'unico caso in cui SQL e modello puro darebbero due numeri diversi. "
       + "Assente e non-intero passano perche entrambe le implementazioni li leggono "
       + "come «nessuna altezza». Il limite e quello dell'`integer` della proiezione.",
  min: 1,
  max: 2147483647,
  cases: [
    { u: 45, supported: true, _meta: "il valore di 98 rack su 102 nel seed" },
    { u: 1, supported: true, _meta: "il minimo: un rack da una unita e un rack" },
    { u: 2147483647, supported: true, _meta: "il massimo esatto, dentro" },
    { u: 2147483648, supported: false, _meta: "il massimo + 1: primo intero che la colonna non tiene" },
    { u: 3000000000, supported: false,
      _meta: "il caso del corpus oversized-integers, quello che ha fatto scoprire la voce 16" },
    { u: -2147483649, supported: false, _meta: "fuori anche dal lato negativo" },
    { u: 0, supported: false, _meta: "un rack alto zero non e un rack: sotto il minimo" },
    { u: -1, supported: false, _meta: "altezza negativa: sotto il minimo" },
    { u: null, supported: true, _meta: "assente: il default canonico mette 45" },
    { u: '45', supported: true,
      _meta: "STRINGA: passa. La colonna resta NULL e _as_int da null, quindi SQL e "
           + "modello puro vedono entrambi «nessuna altezza». Nessuna divergenza." },
    { u: 4.5, supported: true, _meta: "non intero: come sopra, nessuna divergenza da impedire" },
    { u: true, supported: true, _meta: "booleano: non e 1, e non e un'altezza" },
  ],
});

// ============================================================
// 3. PERCENTUALE — HALF-UP, e i casi che i tre linguaggi sbagliano diversamente
// ============================================================
//
// I `.5` esatti sono il cuore di §3. Ogni riga con `_meta` dice quale linguaggio
// darebbe una risposta diversa se si usasse il suo arrotondamento nativo.
emit('percent', {
  _nota: "percent(used,total) = floor(used*100/total + 1/2), aritmetica intera. "
       + "Le righe con _meta sono le meta esatte: e la dove Math.round, round() di "
       + "Python e round() di SQL non sono d'accordo.",
  cases: [
    { used: 1, total: 8, percent: 13,
      _meta: 'esattamente 12.5 — JS 13, Python round() 12. Il contratto e 13.' },
    { used: 3, total: 8, percent: 38,
      _meta: 'esattamente 37.5 — JS 38, Python round() 38 (per caso: 37.5 -> 38 al pari).' },
    { used: 1, total: 200, percent: 1, _meta: 'esattamente 0.5 — Python round() darebbe 0.' },
    { used: 3, total: 200, percent: 2, _meta: 'esattamente 1.5 — Python round() darebbe 2.' },
    { used: 5, total: 200, percent: 3, _meta: 'esattamente 2.5 — Python round() darebbe 2.' },
    { used: 7, total: 200, percent: 4, _meta: 'esattamente 3.5 — Python round() darebbe 4.' },
    { used: 1, total: 40, percent: 3, _meta: 'esattamente 2.5' },
    { used: 21, total: 40, percent: 53, _meta: 'esattamente 52.5' },
    { used: 0, total: 45, percent: 0 },
    { used: 45, total: 45, percent: 100 },
    { used: 1, total: 45, percent: 2, _meta: '2.222 -> 2' },
    { used: 22, total: 45, percent: 49, _meta: '48.888 -> 49' },
    { used: 23, total: 45, percent: 51, _meta: '51.111 -> 51' },
    { used: 1, total: 3, percent: 33 },
    { used: 2, total: 3, percent: 67, _meta: '66.666 -> 67' },
    { used: 0, total: 0, percent: 0, _meta: 'nessuna unita: 0, non 100 e non una divisione per zero.' },
    { used: 5, total: 0, percent: 0 },
    { used: 5, total: -1, percent: 0 },
    { used: -1, total: 10, percent: 0 },
    { used: 15, total: 10, percent: 150,
      _meta: 'oltre il 100%: non si taglia qui. Chi mostra una barra la limita da se; '
           + 'un numero limitato in silenzio nasconderebbe un inventario incoerente.' },
    { used: 1, total: 3000000000, percent: 0 },
    { used: 1500000000, total: 3000000000, percent: 50 },
    { used: null, total: 45, percent: 0 },
    { used: 1.5, total: 45, percent: 0, _meta: 'non intero: non e un conteggio di slot.' },
  ],
});

// ============================================================
// 4. FILE — l'identità del gruppo non è l'etichetta mostrata
// ============================================================
emit('rows', {
  _nota: "La chiave del gruppo distingue «fila non impostata» da «fila il cui valore e "
       + "letteralmente —». L'etichetta mostrata resta — in entrambi i casi. Il rack "
       + "CS-Q01 del seed di produzione e il caso reale.",
  cases: [
    { name: 'fila ordinaria', row: 'A', assigned: true, value: 'A', label: 'A' },
    { name: 'fila non impostata (assente)', row: null, assigned: false, value: null,
      label: '—' },
    { name: 'fila non impostata (stringa vuota, il default canonico)', row: '',
      assigned: false, value: null, label: '—' },
    { name: 'fila il cui VALORE e —: gruppo proprio, non il residuo', row: '—',
      assigned: true, value: '—', label: '—' },
    { name: 'fila numerica', row: 3, assigned: true, value: '3', label: '3' },
    { name: "fila di soli spazi: e un valore scritto, non un'assenza", row: ' ',
      assigned: true, value: ' ', label: ' ' },
    { name: 'fila con un trattino ordinario, che non e la sentinella', row: '-',
      assigned: true, value: '-', label: '-' },
    { name: 'fila unicode', row: 'Fila Ø', assigned: true, value: 'Fila Ø',
      label: 'Fila Ø' },
  ],
  distinctGroups: {
    _nota: "Le due chiavi DEVONO differire: e il difetto §4, e un test che confronta "
         + "solo le etichette non puo diventare rosso quando torna.",
    unset: null,
    literalDash: '—',
    mustDiffer: true,
  },
  ordering: {
    _nota: "I gruppi dichiarati in ordine, il residuo per ULTIMO: e il residuo, non "
         + "una fila che si chiama —.",
    input: ['B', null, 'A', '—', ''],
    expectedLabels: ['A', 'B', '—', '—'],
    expectedAssigned: [true, true, true, false],
  },
});

// ============================================================
// 5. SCADENZE — un interprete di date solo
// ============================================================
//
// Le sette forme che `new Date` accettava e il backend no sono qui in blocco, con
// l'attesa `null`. `2027-02-30` è quella che conta più delle altre: V8 non la
// rifiuta, la fa SCORRERE al 2 marzo.
emit('expiries', {
  _nota: "parseExpiry accetta solo YYYY-MM-DD, con spazi intorno tollerati. Nessuna "
       + "altra forma, nessuna euristica, nessun new Date. Il valore grezzo resta "
       + "nell'inventario com'e: rifiutato come SCADENZA, mai riscritto.",
  parse: [
    { raw: '2027-03-15', date: '2027-03-15' },
    { raw: '  2027-03-15  ', date: '2027-03-15', _meta: 'spazi intorno tollerati' },
    { raw: '\t2027-03-15\n', date: '2027-03-15' },
    { raw: '2024-02-29', date: '2024-02-29', _meta: 'bisestile vero' },
    { raw: '2000-02-29', date: '2000-02-29', _meta: 'divisibile per 400: bisestile' },
    { raw: '1900-02-29', date: null, _meta: 'divisibile per 100 e non per 400: NON bisestile' },
    { raw: '2100-02-29', date: null },
    { raw: '2027-02-30', date: null,
      _meta: 'ROLLOVER: new Date la fa scorrere al 2 marzo. E la ragione per cui '
           + 'new Date non puo essere una validazione.' },
    { raw: '2027-04-31', date: null },
    { raw: '2027-13-01', date: null },
    { raw: '2027-00-01', date: null },
    { raw: '2027-01-00', date: null },
    { raw: '2027-01-32', date: null },
    { raw: '2027-3-15', date: null, _meta: 'mese a una cifra: new Date la accetta' },
    { raw: '2027-03-5', date: null },
    { raw: '2027/03/15', date: null, _meta: 'barre: new Date le accetta' },
    { raw: '15/03/2027', date: null },
    { raw: '03-15-2027', date: null },
    { raw: 'March 15, 2027', date: null, _meta: 'nome del mese: new Date lo accetta' },
    { raw: '2027-03-15T10:00:00Z', date: null, _meta: 'istante, non data di business' },
    { raw: '2027-03-15 10:00:00', date: null },
    { raw: '2027-03', date: null, _meta: 'anno e mese: new Date li accetta' },
    { raw: '2027', date: null, _meta: 'solo anno: new Date lo accetta' },
    { raw: '', date: null },
    { raw: '   ', date: null },
    { raw: 'in attesa', date: null, _meta: 'valore reale scritto a mano: resta, non e una data' },
    { raw: 'n/a', date: null },
    { raw: '0000-01-01', date: null,
      _meta: 'anno 0000: non esiste nel calendario gregoriano e datetime.date non lo '
           + 'rappresenta. La prima stesura della fixture lo dava per valido e il '
           + 'confronto fra le due implementazioni ha mostrato che divergevano.' },
    { raw: '0001-01-01', date: '0001-01-01', _meta: 'il primo anno rappresentabile' },
    { raw: '9999-12-31', date: '9999-12-31' },
    { raw: '+2027-03-15', date: null },
    { raw: '2027-03-15Z', date: null },
    { raw: '２０２７-０３-１５', date: null, _meta: 'cifre a larghezza intera: non sono \\d nel contratto' },
  ],
  parseNonString: [
    { raw: null, date: null },
    { raw: 20270315, date: null, _meta: 'un numero non e una data: la colonna resterebbe NULL' },
    { raw: true, date: null },
  ],
  days: {
    _nota: "daysRemaining e la differenza fra DUE DATE DI CALENDARIO, in giorni interi. "
         + "Mai Math.round((dt - Date.now())/86400000): quello dipende dall'ora del "
         + "giorno e nella notte del cambio dell'ora si arrotonda per caso.",
    cases: [
      { today: '2026-08-20', expiry: '2026-08-20', days: 0 },
      { today: '2026-08-20', expiry: '2026-08-21', days: 1 },
      { today: '2026-08-20', expiry: '2026-08-19', days: -1 },
      { today: '2026-08-20', expiry: '2026-09-19', days: 30 },
      { today: '2026-08-20', expiry: '2026-11-18', days: 90 },
      { today: '2026-10-24', expiry: '2026-10-26', days: 2,
        _meta: 'attraversa il ritorno all\'ora solare in Europa (25 ottobre 2026): 49 '
             + 'ore di calendario, e comunque 2 giorni.' },
      { today: '2027-03-27', expiry: '2027-03-29', days: 2,
        _meta: 'attraversa il passaggio all\'ora legale: 47 ore, e comunque 2 giorni.' },
      { today: '2024-02-28', expiry: '2024-03-01', days: 2, _meta: 'anno bisestile' },
      { today: '2023-02-28', expiry: '2023-03-01', days: 1 },
      { today: '2026-12-31', expiry: '2027-01-01', days: 1 },
      { today: '2026-08-20', expiry: '2036-08-20', days: 3653 },
    ],
  },
  level: {
    _nota: "Livello ISPETTIVO della vista Scadenze: mostra tutto. warning e la soglia "
         + "della vista (90 nel frontend), non le finestre del worker.",
    cases: [
      { days: -1, warning: 90, level: 'expired' },
      { days: -400, warning: 90, level: 'expired' },
      { days: 0, warning: 90, level: 'warning', _meta: 'scade OGGI: ancora in finestra' },
      { days: 90, warning: 90, level: 'warning', _meta: 'estremo compreso' },
      { days: 91, warning: 90, level: 'future' },
      { days: 0, warning: 0, level: 'warning' },
      { days: 1, warning: 0, level: 'future' },
    ],
  },
  notificationDue: {
    _nota: "Regola del worker: 0 <= giorni <= almeno una soglia. Gli scaduti NO. "
         + "L'idoneita per STATO e un'altra cosa e sta in notifications.json.",
    cases: [
      { days: 0, windows: [90, 30, 7], due: true },
      { days: 7, windows: [90, 30, 7], due: true },
      { days: 90, windows: [90, 30, 7], due: true },
      { days: 91, windows: [90, 30, 7], due: false },
      { days: -1, windows: [90, 30, 7], due: false,
        _meta: 'scaduto: nessun avviso nuovo. Resta visibile in Scadenze.' },
      { days: 45, windows: [7], due: false },
      { days: 5, windows: [7], due: true },
      { days: 0, windows: [], due: false, _meta: 'nessuna finestra configurata' },
      { days: 0, windows: [0], due: true },
      { days: 1, windows: [0], due: false },
    ],
  },
});

// ============================================================
// 6. IDONEITÀ AGLI AVVISI — stato sì, presenza no
// ============================================================
emit('notifications', {
  _nota: "Solo lo STATO decide l'idoneita a un avviso nuovo. dismesso NO; attivo, "
       + "manutenzione e dismissione SI. La presenza fisica non c'entra: una garanzia "
       + "scade anche a magazzino, e chi la rinnova deve saperlo.",
  cases: [
    { device: { stato: 'attivo', presenza: 'presente' }, eligible: true },
    { device: { stato: 'attivo', presenza: 'rimosso' }, eligible: true,
      _meta: 'portato via: la garanzia scade comunque' },
    { device: { stato: 'manutenzione', presenza: 'presente' }, eligible: true },
    { device: { stato: 'manutenzione', presenza: 'rimosso' }, eligible: true },
    { device: { stato: 'dismissione', presenza: 'presente' }, eligible: true,
      _meta: 'in dismissione: la decisione non e conclusa, il contratto vale ancora' },
    { device: { stato: 'dismissione', presenza: 'rimosso' }, eligible: true },
    { device: { stato: 'dismesso', presenza: 'presente' }, eligible: false,
      _meta: 'occupa lo slot ma non genera piu promemoria di rinnovo' },
    { device: { stato: 'dismesso', presenza: 'rimosso' }, eligible: false },
    { device: {}, eligible: true, _meta: 'legacy senza stato: attivo' },
    { device: { stato: '' }, eligible: true },
    { device: { stato: 'DISMESSO' }, eligible: true,
      _meta: 'il vocabolario e sensibile alle maiuscole: DISMESSO non e dismesso, '
           + 'e un valore fuori elenco resta idoneo invece di essere escluso a naso' },
  ],
});

// ============================================================
// 7. INDIRIZZI — attese a mano sui casi che portano una decisione
// ============================================================
emit('addresses', {
  _nota: "Una grammatica sola. exact IPv4/IPv6, CIDR IPv4/IPv6, intervallo IPv4, "
       + "jolly IPv4. Niente intervalli ne jolly IPv6: non si inventa una grammatica. "
       + "null significa «non e un indirizzo, cercalo come testo», NON «zero risultati».",
  parse: [
    { raw: '10.0.0.1', family: 4, text: '10.0.0.1' },
    { raw: '0.0.0.0', family: 4, text: '0.0.0.0' },
    { raw: '255.255.255.255', family: 4, text: '255.255.255.255' },
    { raw: '010.0.0.1', family: 4, text: '10.0.0.1',
      _meta: 'zeri iniziali: ipToNum li accettava, togliere una forma che funzionava '
           + 'sarebbe una regressione' },
    { raw: ' 10.0.0.1 ', family: 4, text: '10.0.0.1' },
    { raw: '256.0.0.1', family: null },
    { raw: '10.0.0', family: null, _meta: 'mezzo indirizzo: e testo, e come testo trova 10.0.0.x' },
    { raw: '10.0.0.1.2', family: null },
    { raw: '10.0.0.1%eth0', family: null },
    { raw: '10.0.0.1/32', family: null, _meta: 'un prefisso non e un host' },
    { raw: '::1', family: 6, text: '::1' },
    { raw: '::', family: 6, text: '::' },
    { raw: '2001:db8::1', family: 6, text: '2001:db8::1' },
    { raw: '2001:0db8:0000:0000:0000:0000:0000:0001', family: 6, text: '2001:db8::1',
      _meta: 'forma estesa: stesso indirizzo, forma canonica compressa' },
    { raw: '2001:DB8::1', family: 6, text: '2001:db8::1', _meta: 'maiuscole ammesse' },
    { raw: '::ffff:10.0.0.1', family: 6, text: '::ffff:10.0.0.1',
      _meta: 'IPv4-mapped: si SCRIVE col quartetto puntato' },
    { raw: '0:0:0:0:0:ffff:c0a8:1', family: 6, text: '::ffff:192.168.0.1',
      _meta: 'stesso indirizzo scritto in esadecimale: la forma canonica e quella puntata' },
    { raw: '::1.2.3.4', family: 6, text: '::102:304',
      _meta: 'IPv4-COMPATIBLE (senza ffff): NON si scrive puntato' },
    { raw: '1:2:3:4:5:6:7:8', family: 6, text: '1:2:3:4:5:6:7:8' },
    { raw: '1:2:3:4:5:6:7::', family: 6, text: '1:2:3:4:5:6:7:0',
      _meta: ':: per un solo gruppo si scrive per esteso' },
    { raw: '1:2:3:4:5:6:7:8:9', family: null },
    { raw: '1:2:3:4:5:6:7:8::', family: null },
    { raw: '1::2::3', family: null, _meta: 'due compressioni' },
    { raw: '1:::2', family: null },
    { raw: '12345::1', family: null, _meta: 'gruppo a cinque cifre' },
    { raw: '1.2.3.4::', family: null, _meta: 'IPv4 non in ultima posizione' },
    { raw: '::ffff:010.0.0.1', family: null,
      _meta: 'zeri iniziali nell IPv4 INCORPORATO: forma nuova di questa fase, niente '
           + 'da conservare, e ipaddress in Python la rifiuta' },
    { raw: '::ffff:1.2.3.4.5', family: null },
    { raw: 'fe80::1%eth0', family: null, _meta: 'identificatore di zona: privo di senso in un inventario' },
    { raw: 'gggg::1', family: null },
    { raw: ':', family: null },
    { raw: '', family: null },
    { raw: 'srv-01', family: null },
  ],
  query: [
    { raw: '10.0.0.1', kind: 'exact', family: 4, lo: '10.0.0.1', hi: '10.0.0.1' },
    { raw: '2001:db8::1', kind: 'exact', family: 6, lo: '2001:db8::1', hi: '2001:db8::1' },
    { raw: '10.0.2.0/24', kind: 'cidr', family: 4, lo: '10.0.2.0', hi: '10.0.2.255' },
    { raw: '10.0.2.5/24', kind: 'cidr', family: 4, lo: '10.0.2.0', hi: '10.0.2.255',
      _meta: 'la base si azzera sul prefisso: e la rete, non l\'host' },
    { raw: '10.0.0.0/0', kind: 'cidr', family: 4, lo: '0.0.0.0', hi: '255.255.255.255' },
    { raw: '10.0.0.7/32', kind: 'cidr', family: 4, lo: '10.0.0.7', hi: '10.0.0.7' },
    { raw: '10.0.0.0/33', kind: null, _meta: 'prefisso impossibile: testo, e come testo non trova niente' },
    { raw: '2001:db8::/32', kind: 'cidr', family: 6, lo: '2001:db8::',
      hi: '2001:db8:ffff:ffff:ffff:ffff:ffff:ffff' },
    { raw: '2001:db8::5/32', kind: 'cidr', family: 6, lo: '2001:db8::',
      hi: '2001:db8:ffff:ffff:ffff:ffff:ffff:ffff' },
    { raw: '::/0', kind: 'cidr', family: 6, lo: '::',
      hi: 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff' },
    { raw: '::/129', kind: null },
    { raw: '10.0.0.1-10.0.0.9', kind: 'range', family: 4, lo: '10.0.0.1', hi: '10.0.0.9' },
    { raw: '10.0.0.9-10.0.0.1', kind: 'range', family: 4, lo: '10.0.0.1', hi: '10.0.0.9',
      _meta: 'estremi rovesciati: si riordinano' },
    { raw: '10.0.0.1 - 10.0.0.9', kind: 'range', family: 4, lo: '10.0.0.1', hi: '10.0.0.9' },
    { raw: '2001:db8::1-2001:db8::9', kind: null,
      _meta: 'NON esistono intervalli IPv6: non si inventa una grammatica' },
    { raw: '10.0.*', kind: 'wildcard', family: 4, lo: '10.0.0.0', hi: '10.0.255.255' },
    { raw: '10.*', kind: 'wildcard', family: 4, lo: '10.0.0.0', hi: '10.255.255.255' },
    { raw: '10.0.0.*', kind: 'wildcard', family: 4, lo: '10.0.0.0', hi: '10.0.0.255' },
    { raw: '*', kind: null },
    { raw: '300.*', kind: null },
    { raw: '2001:db8::*', kind: null, _meta: 'NON esistono jolly IPv6' },
    { raw: '10.0.0', kind: null, _meta: 'mezzo indirizzo: testo' },
    { raw: '', kind: null },
    { raw: '10.0.0.1/24', kind: 'cidr', family: 4, lo: '10.0.0.0', hi: '10.0.0.255' },
  ],
  matches: {
    _nota: "IL CASO CHE VALE PIU DI TUTTI: 10.0.0.1 esatto NON trova 10.0.0.100. Era "
         + "un falso positivo da sottostringa, e chi cercava una macchina precisa "
         + "riceveva la sua vicina di sottorete.",
    cases: [
      { query: '10.0.0.1', ip: '10.0.0.1', match: true },
      { query: '10.0.0.1', ip: '10.0.0.100', match: false, _meta: 'IL difetto §13' },
      { query: '10.0.0.1', ip: '10.0.0.10', match: false },
      { query: '10.0.0.1', ip: '110.0.0.1', match: false },
      { query: '10.0.0.1', ip: ' 10.0.0.1 ', match: true, _meta: 'spazi nel dato' },
      { query: '10.0.0.1', ip: '010.0.0.1', match: true },
      { query: '10.0.0.1', ip: null, match: false },
      { query: '10.0.0.1', ip: 'srv-01', match: false },
      { query: '2001:db8::1', ip: '2001:0db8::0001', match: true },
      { query: '2001:db8::1', ip: '2001:db8::10', match: false },
      { query: '10.0.2.0/24', ip: '10.0.2.255', match: true },
      { query: '10.0.2.0/24', ip: '10.0.3.0', match: false },
      { query: '10.0.*', ip: '10.0.255.255', match: true },
      { query: '10.0.*', ip: '10.1.0.0', match: false },
      { query: '10.0.*', ip: '::a00:1', match: false,
        _meta: 'FAMIGLIE SEPARATE: stesso valore numerico, spazi di indirizzamento diversi' },
      { query: '::/0', ip: '10.0.0.1', match: false,
        _meta: 'tutto IPv6 non contiene nessun IPv4' },
      { query: '0.0.0.0/0', ip: '2001:db8::1', match: false },
      { query: '10.0.0.1-10.0.0.9', ip: '10.0.0.5', match: true },
      { query: '10.0.0.1-10.0.0.9', ip: '10.0.0.10', match: false },
    ],
  },
});

// ============================================================
// 8. RICERCA TESTUALE
// ============================================================
emit('search', {
  _nota: "Sottostringa LETTERALE senza distinzione di maiuscole. I campi del "
       + "dispositivo sono nove e comprendono tipo, stato e presenza; le note NO. "
       + "I rack si cercano per id, name e seriali. % e _ sono caratteri normali.",
  deviceFields: ['id', 'name', 'model', 'ip', 'serial', 'owner', 'tipo', 'stato', 'presenza'],
  rackFields: ['id', 'name', 'seriali'],
  device: [
    { name: 'per id', device: { id: 'srv-db-01', name: 'Database' }, q: 'srv-db', match: true },
    { name: 'id: era ASSENTE dalla ricerca del prototipo',
      device: { id: 'ASSET-4471', name: 'Nodo' }, q: 'asset-4471', match: true },
    { name: 'per nome', device: { name: 'Nodo Alfa' }, q: 'alfa', match: true },
    { name: 'maiuscole indifferenti', device: { name: 'NODO ALFA' }, q: 'alfa', match: true },
    { name: 'query in maiuscolo', device: { name: 'nodo alfa' }, q: 'ALFA', match: true,
      _meta: 'chi cerca normalizza la query: il contratto e su una needle minuscola, '
           + 'e chi chiama la abbassa. La fixture porta la needle GREZZA e il test la '
           + 'abbassa come fa il chiamante.' },
    { name: 'per modello', device: { name: 'x', model: 'Dell R660' }, q: 'r660', match: true },
    { name: 'per seriale', device: { name: 'x', serial: 'SN-99' }, q: 'sn-99', match: true },
    { name: 'per referente', device: { name: 'x', owner: 'Team Infra' }, q: 'infra', match: true },
    { name: 'per ip come TESTO parziale', device: { name: 'x', ip: '10.0.0.100' },
      q: '10.0.0', match: true },
    { name: 'per tipo: era ASSENTE dalla ricerca del prototipo',
      device: { name: 'x', type: 'firewall' }, q: 'firewall', match: true },
    { name: 'tipo per DIFETTO: un dispositivo senza type e «altro»',
      device: { name: 'x' }, q: 'altro', match: true },
    { name: 'per stato: era ASSENTE dalla ricerca del prototipo',
      device: { name: 'x', stato: 'manutenzione' }, q: 'manutenzione', match: true },
    { name: 'stato per DIFETTO: senza stato si trova cercando attivo',
      device: { name: 'x' }, q: 'attivo', match: true },
    { name: 'per presenza', device: { name: 'x', presenza: 'rimosso' }, q: 'rimosso',
      match: true },
    { name: 'presenza per DIFETTO', device: { name: 'x' }, q: 'presente', match: true },
    // ⚠ Questi tre casi sono stati aggiunti DOPO aver trovato il difetto rileggendo il
    // codice: la traduzione SQL applicava il default PRIMA di guardare `extra`, quindi
    // un `type: 42` — che non e una stringa, finisce in `extra` e lascia la colonna
    // NULL — si cercava come «altro» invece che come «42». Il contratto dice colonna,
    // poi extra, poi default; nessuna fixture lo copriva.
    { name: 'tipo NUMERICO: sta in extra, e si cerca per il suo valore',
      device: { name: 'x', type: 42 }, q: '42', match: true,
      _meta: 'la colonna e NULL e il valore sta in extra: il default NON deve vincere' },
    { name: 'tipo numerico: NON si cerca come il default',
      device: { name: 'x', type: 42 }, q: 'altro', match: false },
    { name: 'stato NUMERICO: stessa regola',
      device: { name: 'x', stato: 7 }, q: '7', match: true },
    { name: 'le NOTE non si cercano',
      device: { name: 'x', note: 'da sostituire entro giugno' }, q: 'giugno',
      match: false, _meta: 'decisione di questa fase: testo libero e lungo' },
    { name: 'nessun campo combacia', device: { name: 'Nodo', model: 'Dell' }, q: 'zzz',
      match: false },
    { name: '% e un carattere LETTERALE',
      device: { name: 'Sconto 50%' }, q: '%', match: true },
    { name: '% non e un jolly', device: { name: 'Nodo Alfa' }, q: '%', match: false,
      _meta: 'con LIKE questa query troverebbe tutto' },
    { name: '_ e un carattere LETTERALE', device: { name: 'nodo_alfa' }, q: '_', match: true },
    { name: '_ non e un jolly', device: { name: 'nodo-alfa' }, q: '_', match: false },
    { name: 'unicode', device: { name: 'Sala Ø nord' }, q: 'ø', match: true },
    { name: 'em dash nel nome', device: { name: 'Nodo — primario' }, q: '—', match: true },
    { name: 'la barra nel codice non si spezza', device: { id: '10.0.0.0/24' },
      q: '0.0/24', match: true },
    { name: 'query vuota: nessun risultato, come la casella vuota',
      device: { name: 'Nodo' }, q: '', match: false },
    { name: 'campo numerico', device: { name: 42 }, q: '42', match: true },
    { name: 'campo booleano non si cerca', device: { name: true }, q: 'true', match: false },
  ],
  rack: [
    { name: 'per id', rack: { id: 'CS-Q01' }, q: 'cs-q', match: true },
    { name: 'per nome', rack: { id: 'K1', name: 'Armadio rete' }, q: 'armadio', match: true },
    { name: 'per seriale', rack: { id: 'K1', seriali: ['AB-1', 'AB-2'] }, q: 'ab-2',
      match: true },
    { name: 'seriali vuoti', rack: { id: 'K1', seriali: [] }, q: 'ab', match: false },
    { name: 'seriali assenti', rack: { id: 'K1' }, q: 'ab', match: false },
    { name: 'seriale numerico: si cerca comunque',
      rack: { id: 'K1', seriali: [4471] }, q: '4471', match: true,
      _meta: 'nella 2E questi seriali non si trovavano, perche l\'array intero finiva '
           + 'in extra. Adesso si cercano: e una delle divergenze risolte.' },
    { name: 'id senza rack: niente combacia, e non solleva',
      rack: {}, q: 'k1', match: false },
  ],
});

// ============================================================
// 9. ETICHETTE
// ============================================================
emit('labels', {
  _nota: "nome mostrabile → codice di business → «(senza nome)». MAI None, undefined, "
       + "null. Il contesto strutturale resta in campi SEPARATI: mai una stringa unica "
       + "spezzata dopo.",
  device: [
    { device: { name: 'Nodo Alfa', id: 'srv-01' }, label: 'Nodo Alfa' },
    { device: { name: '', id: 'srv-01' }, label: 'srv-01' },
    { device: { name: null, id: 'srv-01' }, label: 'srv-01' },
    { device: { id: 'srv-01' }, label: 'srv-01' },
    { device: { name: '', id: '' }, label: '(senza nome)' },
    { device: {}, label: '(senza nome)' },
    { device: { name: null, id: null }, label: '(senza nome)',
      _meta: 'MAI «None»: era il comportamento della 2F, conservato allora di '
           + 'proposito e risolto adesso' },
    { device: { name: 42 }, label: '42' },
    { device: { name: 0, id: 'srv-01' }, label: 'srv-01', _meta: 'zero non e un\'etichetta' },
    { device: { name: 42.0 }, label: '42',
      _meta: 'String(42.0) e «42» in JavaScript e «42.0» con str() in Python: si '
           + 'sceglie la forma che l\'utente ha sempre visto' },
    { device: { name: 1.5 }, label: '1.5' },
    { device: { name: false, id: 'srv-01' }, label: 'srv-01' },
    { device: { name: [], id: 'srv-01' }, label: 'srv-01',
      _meta: 'String([]) e «» in JavaScript e «[]» con str() in Python: nessuno dei '
           + 'due e un\'etichetta, si passa al candidato dopo' },
    { device: { name: {}, id: 'srv-01' }, label: 'srv-01' },
    { device: { name: ' ' }, label: ' ', _meta: 'soli spazi: e cio che l\'utente ha scritto' },
    { device: { name: 'Nodo — primario' }, label: 'Nodo — primario' },
    { device: { name: 'Sala Ø' }, label: 'Sala Ø' },
    { device: { name: 'a/b' }, label: 'a/b', _meta: 'la barra non si spezza' },
  ],
  rack: [
    { rack: { name: 'Armadio rete', id: 'K1' }, label: 'Armadio rete' },
    { rack: { name: '', id: 'K1' }, label: 'K1' },
    { rack: { id: '10.0.0.0/24' }, label: '10.0.0.0/24',
      _meta: 'IL caso della 2F: il percorso impacchettato lo troncava a 10.0.0.0' },
    { rack: {}, label: '(senza nome)' },
  ],
  room: [
    { room: { nome: 'Sala Backend', id: 'backend' }, label: 'Sala Backend' },
    { room: { nome: '', id: 'backend' }, label: 'backend' },
    { room: {}, label: '(senza nome)' },
  ],
  location: [
    { location: { nome: 'Pomezia G0', id: 'pomezia-g0' }, label: 'Pomezia G0' },
    { location: { nome: null, id: 'pomezia-g0' }, label: 'pomezia-g0' },
    { location: {}, label: '(senza nome)' },
  ],
  context: {
    _nota: "Il contesto e STRUTTURATO: tre campi, mai una stringa unica da spezzare. "
         + "Il corpus porta i valori che il vecchio impacchettamento corrompeva.",
    cases: [
      { location: { id: 'pomezia-g0', nome: 'Pomezia G0' },
        room: { id: 'backend', nome: 'Sala Backend' },
        rack: { id: '10.0.0.0/24' },
        labels: { location: 'Pomezia G0', room: 'Sala Backend', rack: '10.0.0.0/24' } },
      { location: { id: 'a/b' }, room: { id: 'c/d' }, rack: { id: 'e/f' },
        labels: { location: 'a/b', room: 'c/d', rack: 'e/f' },
        _meta: 'tre barre: con l\'impacchettamento su / diventavano sei pezzi e tutto scalava' },
      { location: {}, room: {}, rack: {},
        labels: { location: '(senza nome)', room: '(senza nome)', rack: '(senza nome)' } },
      { location: { nome: 'Sito —' }, room: { nome: '—' }, rack: { id: '—' },
        labels: { location: 'Sito —', room: '—', rack: '—' } },
      { location: { nome: 'Ø' }, room: { nome: 'Ærø' }, rack: { id: '✓' },
        labels: { location: 'Ø', room: 'Ærø', rack: '✓' } },
    ],
  },
});

// ============================================================
// 10. INDIRIZZI, corpus DIFFERENZIALE
// ============================================================
//
// ⚠ Questo è l'unico file con attese CALCOLATE, e il suo scopo è diverso: non
// giudicare un comportamento, ma dimostrare che le due implementazioni non divergono
// su nessuna delle forme mutate. I verdetti li produce domain.js; la suite Python
// pretende di produrre gli stessi.
//
// Il corpus è generato con un seme FISSO: nessun test deve dipendere da quale numero
// casuale è uscito oggi.
const SEEDS = [
  '10.0.0.1', '0.0.0.0', '255.255.255.255', '010.0.0.1', '256.0.0.1', '10.0.0',
  '10.0.0.1.2', '10.0.0.', '.10.0.0.1', '10.0.0.-1', '10.0.0.1%eth0', '10.0.0.1/32',
  '10.0.*', '10.*', '10.0.0.*', '*', '10.0.0.1-10.0.0.9', '10.0.0.9-10.0.0.1',
  '10.0.2.0/24', '10.0.0.0/0', '10.0.0.0/33', '2001:db8::/32', '::/0', '::/129',
  '::', '::1', '1::', '2001:db8::1', '2001:0db8:0000:0000:0000:0000:0000:0001',
  'fe80::1', 'fe80::1%eth0', '::ffff:10.0.0.1', '::ffff:0:10.0.0.1',
  '1:2:3:4:5:6:7:8', '1:2:3:4:5:6:7', '1:2:3:4:5:6:7:8:9', '1:2:3:4:5:6:7::',
  '1:2:3:4:5:6:7:8::', '::2:3:4:5:6:7:8', '1::2::3', '1:::2', '12345::1',
  '1:2:3:4:5:6:1.2.3.4', '::1.2.3.4', '1.2.3.4::', '::ffff:1.2.3.4.5',
  '0:0:0:0:0:ffff:c0a8:1', '2001:DB8::1', 'ABCD::EF', 'gggg::1', '1:2', ':', ':::',
  '', ' ', 'srv-01', '10', 'abc', '%', '_', '::ffff:010.0.0.1',
];
const ALFA = '0123456789abcdefABCDEF:.*-/ %gx'.split('');

// PRNG deterministico (mulberry32), per non dipendere da come una versione di node
// implementa Math.random: il corpus committato deve essere lo stesso per chiunque lo
// rigeneri.
//
// ⚠ La prima stesura usava un congruenziale lineare con modulo potenza di due, e i
// bit bassi ciclavano: 4600 mutazioni producevano 2644 forme distinte invece di ~4900.
// Non era sbagliato, era MENO COPERTURA di quanta il conteggio faceva credere.
let seedState = 20260820;
const rnd = () => {
  seedState = (seedState + 0x6d2b79f5) | 0;
  let t = seedState;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = (arr) => arr[Math.floor(rnd() * arr.length)];

const fuzz = new Set(SEEDS);
for (let i = 0; i < 4000; i++) {
  const src = pick(SEEDS) || '1';
  const s = src.split('');
  const edits = 1 + Math.floor(rnd() * 3);
  for (let e = 0; e < edits; e++) {
    const op = rnd();
    if (op < 0.34 && s.length) s[Math.floor(rnd() * s.length)] = pick(ALFA);
    else if (op < 0.67) s.splice(Math.floor(rnd() * (s.length + 1)), 0, pick(ALFA));
    else if (s.length) s.splice(Math.floor(rnd() * s.length), 1);
  }
  fuzz.add(s.join(''));
}
for (let i = 0; i < 600; i++) {
  const groups = [];
  for (let g = 0; g < 8; g++) groups.push(Math.floor(rnd() * 0x10000).toString(16));
  const n = Math.floor(rnd() * 9);
  if (n) {
    const at = Math.floor(rnd() * (8 - n + 1));
    fuzz.add(groups.slice(0, at).join(':') + '::' + groups.slice(at + n).join(':'));
  } else {
    fuzz.add(groups.join(':'));
  }
  const octets = [];
  for (let o = 0; o < 4; o++) octets.push(String(Math.floor(rnd() * 300)));
  fuzz.add(octets.join('.'));
}

const inputs = [...fuzz].sort();
const verdicts = {};
for (const t of inputs) {
  const a = parseAddress(t);
  const q = parseAddressQuery(t);
  verdicts[t] = {
    address: a === null ? null : { family: a.family, value: a.value.toString(), text: a.text },
    query: q === null ? null
      : { family: q.family, kind: q.kind, lo: q.lo.text, hi: q.hi.text,
          loValue: q.lo.value.toString(), hiValue: q.hi.value.toString() },
  };
}
emit('addresses-fuzz', {
  _nota: "CORPUS DIFFERENZIALE. Attese CALCOLATE da handoff/domain.js, non scritte a "
       + "mano: qui non si giudica un comportamento, si pretende che le due "
       + "implementazioni non divergano su nessuna forma. I casi che portano una "
       + "DECISIONE stanno in addresses.json, con le attese a mano. Seme fisso: "
       + "nessun test deve dipendere da quale numero casuale e uscito oggi.",
  _generato: 'tools/make-domain-fixtures.mjs',
  count: inputs.length,
  verdicts,
});

console.log(`scritti ${written.length} corpora in ${OUT}/`);
for (const n of written) console.log(`  ${n}.json`);
