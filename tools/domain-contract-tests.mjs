// ============================================================
// domain-contract-tests.mjs — il lato JavaScript del contratto di dominio (fase 2G)
//
// Esegue fixtures/domain/*.json contro handoff/domain.js. La suite Python
// (backend/tests/test_domain_contract.py) esegue le STESSE fixture contro
// backend/app/domain.py, e i test su PostgreSQL le eseguono contro lo SQL.
//
// Tre implementazioni, un contratto: se una devia, è la sua suite che diventa rossa e
// le altre due restano verdi. È questa asimmetria che rende il contratto utile —
// finché le attese venivano calcolate da una delle implementazioni, una svista
// condivisa non poteva emergere.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/domain-contract-tests.mjs
// ============================================================
import { readFileSync } from 'node:fs';
import * as D from '../handoff/domain.js';

const DIR = 'fixtures/domain';
const load = (name) => JSON.parse(readFileSync(`${DIR}/${name}.json`, 'utf8'));

let pass = 0;
const failures = [];
const ok = (name, cond, detail = '') => {
  if (cond) { pass++; }
  else {
    failures.push(name);
    console.log(`  [FAIL] ${name}${detail ? '\n         → ' + detail : ''}`);
  }
};
const eq = (name, got, want) =>
  ok(name, JSON.stringify(got) === JSON.stringify(want),
     `atteso ${JSON.stringify(want)}, ottenuto ${JSON.stringify(got)}`);

const isoDate = (s) => {
  const p = D.parseExpiry(s);
  if (!p) throw new Error(`fixture con data non ISO: ${s}`);
  return p;
};

// ============================================================
console.log('\n1. presenza fisica e stato operativo');
// ============================================================
for (const c of load('presence').cases) {
  eq(`presenza/stato: ${c.name} → stato`, D.statoOf(c.device), c.stato);
  eq(`presenza/stato: ${c.name} → presenza`, D.presenzaOf(c.device), c.presenza);
  eq(`presenza/stato: ${c.name} → occupa`, D.occupiesSpace(c.device), c.occupies);
  eq(`presenza/stato: ${c.name} → avvisa`, D.notifies(c.device), c.notifies);
}

// ============================================================
console.log('2. capacità: slot U distinti');
// ============================================================
for (const c of load('capacity').cases) {
  const cap = D.rackCapacity(c.rackU, c.devices);
  eq(`capacità: ${c.name} → usedU`, cap.usedU, c.usedU);
  eq(`capacità: ${c.name} → freeU`, cap.freeU, c.freeU);
  eq(`capacità: ${c.name} → blocco libero`, cap.largestFreeRun, c.largestFreeRun);
  eq(`capacità: ${c.name} → percentuale`, D.percent(cap.usedU, cap.totalU), c.percent);
  if (c.slots !== undefined) {
    eq(`capacità: ${c.name} → insieme degli slot`,
       [...D.occupiedSlots(c.rackU, c.devices)].sort((a, b) => a - b), c.slots);
  }
  // ⚠ Dove la fixture porta `sumH`, la vecchia definizione DEVE dare un numero
  // diverso: è ciò che rende il test capace di diventare rosso se qualcuno riscrive
  // `SUM(h)`. Un confronto che trovasse i due uguali sarebbe inutile.
  if (c.sumH !== undefined) {
    const sumH = c.devices.reduce((t, d) => t + (d.h === undefined || d.h === null || d.h === 0 ? 1 : d.h), 0);
    eq(`capacità: ${c.name} → SUM(h) è il valore sbagliato dichiarato`, sumH, c.sumH);
    ok(`capacità: ${c.name} → SUM(h) DIVERGE da usedU`, sumH !== c.usedU,
       `sumH ${sumH} e usedU ${c.usedU} coincidono: il caso non distingue le due definizioni`);
  }
}

// ============================================================
console.log('3. percentuale HALF-UP');
// ============================================================
for (const c of load('percent').cases) {
  eq(`percentuale: ${c.used}/${c.total}`, D.percent(c.used, c.total), c.percent);
}

// ============================================================
console.log('4. file: identità del gruppo ≠ etichetta');
// ============================================================
{
  const f = load('rows');
  for (const c of f.cases) {
    const g = D.rowGroup({ row: c.row });
    eq(`fila: ${c.name} → assegnata`, g.assigned, c.assigned);
    eq(`fila: ${c.name} → valore`, g.value, c.value);
    eq(`fila: ${c.name} → etichetta`, g.label, c.label);
  }
  const unset = D.rowGroup({ row: f.distinctGroups.unset });
  const dash = D.rowGroup({ row: f.distinctGroups.literalDash });
  ok('fila: «non impostata» e «valore —» sono gruppi DISTINTI',
     unset.key !== dash.key, `entrambe le chiavi sono ${JSON.stringify(unset.key)}`);
  eq('fila: e mostrano la stessa etichetta', [unset.label, dash.label],
     [D.ROW_UNSET_LABEL, D.ROW_UNSET_LABEL]);

  const groups = f.ordering.input.map((r) => D.rowGroup({ row: r }));
  const seen = new Map();
  for (const g of groups) if (!seen.has(g.key)) seen.set(g.key, g);
  const ordered = [...seen.values()].sort(D.compareRowGroups);
  eq('fila: ordine dei gruppi (etichette)', ordered.map((g) => g.label),
     f.ordering.expectedLabels);
  eq('fila: ordine dei gruppi (assegnata)', ordered.map((g) => g.assigned),
     f.ordering.expectedAssigned);
}

// ============================================================
console.log('5. scadenze: un interprete di date solo');
// ============================================================
{
  const f = load('expiries');
  for (const c of f.parse) {
    const got = D.parseExpiry(c.raw);
    const text = got === null ? null
      : `${String(got.y).padStart(4, '0')}-${String(got.m).padStart(2, '0')}-${String(got.d).padStart(2, '0')}`;
    eq(`data: ${JSON.stringify(c.raw)}`, text, c.date);
  }
  for (const c of f.parseNonString) {
    eq(`data non stringa: ${JSON.stringify(c.raw)}`, D.parseExpiry(c.raw), c.date);
  }
  // ⚠ La controprova che dà senso a tutto il blocco: `new Date` accetta davvero
  // quelle forme. Senza, il corpus dimostrerebbe soltanto che il parser rifiuta
  // qualcosa, non che rifiuta qualcosa che l'implementazione precedente accettava.
  const accettateDaDate = ['2027-3-15', '2027/03/15', 'March 15, 2027',
                           '2027-03-15T10:00:00Z', '2027-03', '2027'];
  for (const raw of accettateDaDate) {
    ok(`controprova: new Date("${raw}") NON è NaN, e il contratto la rifiuta`,
       !Number.isNaN(new Date(raw).getTime()) && D.parseExpiry(raw) === null);
  }
  const rollover = new Date('2027-02-30');
  ok('controprova: new Date("2027-02-30") scorre al 2 marzo invece di rifiutare',
     !Number.isNaN(rollover.getTime()) && rollover.getUTCMonth() === 2
     && rollover.getUTCDate() === 2 && D.parseExpiry('2027-02-30') === null,
     `new Date ha dato ${rollover.toISOString()}`);

  for (const c of f.days.cases) {
    eq(`giorni: ${c.today} → ${c.expiry}`,
       D.daysBetween(isoDate(c.expiry), isoDate(c.today)), c.days);
  }
  for (const c of f.level.cases) {
    eq(`livello: ${c.days}gg soglia ${c.warning}`,
       D.expiryLevel(c.days, c.warning), c.level);
  }
  for (const c of f.notificationDue.cases) {
    eq(`avviso dovuto: ${c.days}gg finestre ${JSON.stringify(c.windows)}`,
       D.notificationDue(c.days, c.windows), c.due);
  }
}

// ============================================================
console.log('6. idoneità agli avvisi');
// ============================================================
for (const c of load('notifications').cases) {
  eq(`idoneità: ${JSON.stringify(c.device)}`, D.notifies(c.device), c.eligible);
}

// ============================================================
console.log('7. indirizzi');
// ============================================================
{
  const f = load('addresses');
  for (const c of f.parse) {
    const a = D.parseAddress(c.raw);
    eq(`indirizzo: ${JSON.stringify(c.raw)} → famiglia`,
       a === null ? null : a.family, c.family);
    if (c.family !== null) eq(`indirizzo: ${JSON.stringify(c.raw)} → forma canonica`, a.text, c.text);
  }
  for (const c of f.query) {
    const q = D.parseAddressQuery(c.raw);
    eq(`query indirizzo: ${JSON.stringify(c.raw)} → tipo`, q === null ? null : q.kind, c.kind);
    if (c.kind !== null) {
      eq(`query indirizzo: ${JSON.stringify(c.raw)} → famiglia`, q.family, c.family);
      eq(`query indirizzo: ${JSON.stringify(c.raw)} → estremi`, [q.lo.text, q.hi.text],
         [c.lo, c.hi]);
    }
  }
  for (const c of f.matches.cases) {
    eq(`combacia: ${JSON.stringify(c.query)} ∋ ${JSON.stringify(c.ip)}`,
       D.addressMatches(c.ip, D.parseAddressQuery(c.query)), c.match);
  }
}

// ============================================================
console.log('8. ricerca testuale');
// ============================================================
{
  const f = load('search');
  eq('ricerca: i campi del dispositivo sono quelli dichiarati',
     D.DEVICE_SEARCH_FIELDS, f.deviceFields);
  eq('ricerca: i campi del rack sono quelli dichiarati',
     D.RACK_SEARCH_FIELDS, f.rackFields);
  for (const c of f.device) {
    eq(`ricerca dispositivo: ${c.name}`,
       D.deviceMatches(c.device, c.q.toLowerCase()), c.match);
  }
  for (const c of f.rack) {
    eq(`ricerca rack: ${c.name}`, D.rackMatches(c.rack, c.q.toLowerCase()), c.match);
  }
  ok('ricerca: le note NON sono fra i campi cercabili',
     D.DEVICE_SEARCH_FIELDS.indexOf('note') < 0);
}

// ============================================================
console.log('9. etichette');
// ============================================================
{
  const f = load('labels');
  for (const c of f.device) eq(`etichetta dispositivo: ${JSON.stringify(c.device)}`,
                               D.deviceLabel(c.device), c.label);
  for (const c of f.rack) eq(`etichetta rack: ${JSON.stringify(c.rack)}`,
                             D.rackLabel(c.rack), c.label);
  for (const c of f.room) eq(`etichetta sala: ${JSON.stringify(c.room)}`,
                             D.roomLabel(c.room), c.label);
  for (const c of f.location) eq(`etichetta sito: ${JSON.stringify(c.location)}`,
                                 D.locationLabel(c.location), c.label);
  for (const c of f.context.cases) {
    eq(`contesto strutturato: ${JSON.stringify(c.rack)}`,
       { location: D.locationLabel(c.location), room: D.roomLabel(c.room),
         rack: D.rackLabel(c.rack) },
       c.labels);
  }
  // Nessuna etichetta può essere un valore dell'implementazione.
  const tutte = [...f.device.map((c) => c.label), ...f.rack.map((c) => c.label),
                 ...f.room.map((c) => c.label), ...f.location.map((c) => c.label)];
  ok('etichette: nessuna è «None», «undefined» o «null»',
     tutte.every((l) => l !== 'None' && l !== 'undefined' && l !== 'null'),
     JSON.stringify(tutte.filter((l) => ['None', 'undefined', 'null'].includes(l))));
}

// ============================================================
console.log('10. indirizzi: corpus differenziale');
// ============================================================
{
  const f = load('addresses-fuzz');
  let diff = 0;
  for (const [raw, want] of Object.entries(f.verdicts)) {
    const a = D.parseAddress(raw);
    const got = a === null ? null
      : { family: a.family, value: a.value.toString(), text: a.text };
    if (JSON.stringify(got) !== JSON.stringify(want.address)) {
      diff++;
      if (diff <= 5) console.log(`  [FAIL] fuzz ${JSON.stringify(raw)}: atteso `
        + `${JSON.stringify(want.address)}, ottenuto ${JSON.stringify(got)}`);
    }
  }
  ok(`corpus differenziale: ${f.count} forme, nessuna divergenza`, diff === 0,
     `${diff} divergenze`);
}

// ============================================================
console.log('\n' + '='.repeat(70));
console.log(`controlli passati: ${pass}   falliti: ${failures.length}`);
if (failures.length) {
  console.log('\nFALLITI:');
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
console.log('RISULTATO: contratto di dominio soddisfatto dal lato JavaScript');
