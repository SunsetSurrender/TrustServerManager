// ============================================================
// verify-seed-migration.mjs — il seed migrato è ancora quello atteso?
//
// La migrazione degli `_uid` deve aggiungere identità e NIENTE ALTRO.
//
// La verifica NON dipende da git: si appoggia a valori attesi committati in
// tools/seed-migration.expected.json — conteggi per tipo e SHA-256 della
// **forma canonica** del seed, cioè il documento con gli `_uid` rimossi
// ricorsivamente e le chiavi ordinate. Così il controllo resta valido dopo il
// commit, dopo un rebase e su una copia del repo senza storia.
//
// Cosa cattura:
//   - un dato del seed alterato o perso        → cambia lo SHA canonico
//   - entità aggiunte o rimosse                → cambiano i conteggi
//   - identità mancanti, duplicate o malformate → validateDocument
//   - `_uid` finiti sui vani                    → controllo dedicato
//
// Cosa NON cattura, per costruzione: la modifica di un `_uid` (è esattamente
// ciò che la forma canonica ignora). Quello lo copre il fatto che gli `_uid`
// sono committati: un cambiamento si vede nel diff del seed.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/verify-seed-migration.mjs
//   ... --update    per rigenerare i valori attesi (solo con un seed verificato a mano)
// ============================================================
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { validateDocument, walkEntities, isUid } from '../handoff/identity.js';

const EXPECTED = 'tools/seed-migration.expected.json';
const update = process.argv.includes('--update');

const { DATI: seed } = await import(new URL('../handoff/inventario.js', import.meta.url).href);

/** Forma canonica: `_uid` via, chiavi ordinate, serializzazione stabile. */
const canonical = (v) => {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === 'object') {
    const out = {};
    for (const k of Object.keys(v).sort()) if (k !== '_uid') out[k] = canonical(v[k]);
    return out;
  }
  return v;
};

const canonicalJson = JSON.stringify(canonical(seed));
const canonicalSha256 = createHash('sha256').update(canonicalJson, 'utf8').digest('hex');

const ents = walkEntities(seed);
const counts = {};
for (const e of ents) counts[e.kind] = (counts[e.kind] || 0) + 1;

if (update) {
  const payload = {
    _commento: 'Valori attesi per tools/verify-seed-migration.mjs. Rigenerare SOLO con ' +
               '--update e dopo aver verificato a mano il cambiamento del seed.',
    counts,
    total: ents.length,
    canonicalSha256,
    canonicalNote: 'SHA-256 di JSON.stringify del seed con gli _uid rimossi ricorsivamente ' +
                   'e le chiavi ordinate alfabeticamente a ogni livello.',
  };
  writeFileSync(EXPECTED, JSON.stringify(payload, null, 2) + '\n', 'utf8');
  console.log(`Valori attesi scritti in ${EXPECTED}:`);
  console.log(`  totale ${ents.length}, sha ${canonicalSha256}`);
  process.exit(0);
}

let expected;
try {
  expected = JSON.parse(readFileSync(EXPECTED, 'utf8'));
} catch (err) {
  console.error(`Impossibile leggere ${EXPECTED}: ${err.message}`);
  console.error('Generarlo con --update dopo aver verificato il seed.');
  process.exit(2);
}

const idErrors = validateDocument(seed);
const vaniWithUid = [];
for (const L of seed.locations || [])
  for (const R of L.sale || [])
    for (const V of R.vani || []) if (V._uid) vaniWithUid.push(`${L.id}/${R.id}`);

const checks = [
  ['identità valide e univoche', idErrors.length === 0,
   JSON.stringify(idErrors.slice(0, 3))],
  ['ogni entità ha un _uid conforme', ents.every((e) => isUid(e.uid)),
   ents.filter((e) => !isUid(e.uid)).slice(0, 3).map((e) => e.path).join(', ')],
  ['tutti gli _uid distinti', new Set(ents.map((e) => e.uid)).size === ents.length,
   `${new Set(ents.map((e) => e.uid)).size} distinti su ${ents.length}`],
  ['i vani non hanno _uid', vaniWithUid.length === 0, vaniWithUid.join(', ')],
  ['conteggi per tipo invariati',
   JSON.stringify(counts) === JSON.stringify(expected.counts),
   `atteso ${JSON.stringify(expected.counts)}, trovato ${JSON.stringify(counts)}`],
  ['totale entità invariato', ents.length === expected.total,
   `atteso ${expected.total}, trovato ${ents.length}`],
  ['dati invariati a meno degli _uid (SHA canonico)',
   canonicalSha256 === expected.canonicalSha256,
   `atteso ${expected.canonicalSha256}\n           trovato ${canonicalSha256}`],
];

console.log('='.repeat(70));
let ok = true;
for (const [name, passed, detail] of checks) {
  console.log(`  [${passed ? 'PASS' : 'FAIL'}] ${name}`);
  if (!passed && detail) console.log(`         → ${detail}`);
  ok &&= passed;
}
console.log('-'.repeat(70));
console.log('  entità per tipo:', JSON.stringify(counts));
console.log('  sha canonico:   ', canonicalSha256);
console.log('='.repeat(70));
console.log('RISULTATO:', ok ? 'SEED VERIFICATO' : 'IL SEED È CAMBIATO');
if (!ok) {
  console.log('\nSe il cambiamento è voluto: verificarlo nel diff, poi rigenerare con');
  console.log('  node tools/verify-seed-migration.mjs --update');
}
process.exit(ok ? 0 : 1);
