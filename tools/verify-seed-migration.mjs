// ============================================================
// verify-seed-migration.mjs — il seed migrato è equivalente all'originale?
//
// La migrazione degli `_uid` deve aggiungere identità e NIENTE ALTRO. Questo
// script lo dimostra: rimuove ricorsivamente gli `_uid` dal seed migrato e lo
// confronta in profondità con l'originale. Qualunque differenza è un dato
// perso o alterato.
//
// Uso:
//   git show :handoff/inventario.js > /tmp/orig.mjs     (o HEAD:...)
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine \
//     node tools/verify-seed-migration.mjs /tmp/orig.mjs
// ============================================================
import { validateDocument, walkEntities, isUid } from '../handoff/identity.js';

const origPath = process.argv[2];
if (!origPath) {
  console.error('uso: node tools/verify-seed-migration.mjs <percorso-seed-originale.mjs>');
  process.exit(2);
}

const { DATI: migrated } = await import(new URL('../handoff/inventario.js', import.meta.url).href);
const { DATI: original } = await import(
  origPath.startsWith('/') || /^[A-Za-z]:/.test(origPath)
    ? `file://${origPath.replace(/\\/g, '/')}`
    : new URL(origPath, `file://${process.cwd()}/`).href
);

const stripUid = (v) => {
  if (Array.isArray(v)) return v.map(stripUid);
  if (v && typeof v === 'object') {
    const out = {};
    for (const k of Object.keys(v)) if (k !== '_uid') out[k] = stripUid(v[k]);
    return out;
  }
  return v;
};

/** Confronto profondo indipendente dall'ordine delle chiavi. */
const diffs = [];
const deepEq = (a, b, path = '') => {
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)) return diffs.push(`${path}: array vs non-array`);
    if (a.length !== b.length) return diffs.push(`${path}: lunghezza ${a.length} vs ${b.length}`);
    a.forEach((x, i) => deepEq(x, b[i], `${path}[${i}]`));
    return;
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a).sort();
    const kb = Object.keys(b).sort();
    for (const k of new Set([...ka, ...kb])) {
      if (!(k in a)) { diffs.push(`${path}.${k}: assente nell'originale-normalizzato`); continue; }
      if (!(k in b)) { diffs.push(`${path}.${k}: assente nel migrato-normalizzato`); continue; }
      deepEq(a[k], b[k], `${path}.${k}`);
    }
    return;
  }
  if (a !== b) diffs.push(`${path}: ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);
};

deepEq(stripUid(migrated), stripUid(original), 'DATI');

const checks = [];
checks.push(['seed migrato: identità valide e univoche', validateDocument(migrated).length === 0,
             JSON.stringify(validateDocument(migrated).slice(0, 3))]);
checks.push(['equivalenza dei dati a meno degli _uid', diffs.length === 0,
             diffs.slice(0, 8).join(' | ')]);

const ents = walkEntities(migrated);
checks.push(['ogni entità ha un _uid conforme', ents.every((e) => isUid(e.uid)),
             ents.filter((e) => !isUid(e.uid)).slice(0, 3).map((e) => e.path).join(', ')]);
checks.push(['tutti gli _uid distinti', new Set(ents.map((e) => e.uid)).size === ents.length,
             `${new Set(ents.map((e) => e.uid)).size} distinti su ${ents.length}`]);

// I vani NON devono avere identità.
const vaniWithUid = [];
for (const L of migrated.locations || [])
  for (const R of L.sale || [])
    for (const V of R.vani || []) if (V._uid) vaniWithUid.push(`${L.id}/${R.id}`);
checks.push(['i vani non hanno _uid', vaniWithUid.length === 0, vaniWithUid.join(', ')]);

const byKind = {};
for (const e of ents) byKind[e.kind] = (byKind[e.kind] || 0) + 1;

console.log('='.repeat(70));
let ok = true;
for (const [name, passed, detail] of checks) {
  console.log(`  [${passed ? 'PASS' : 'FAIL'}] ${name}`);
  if (!passed && detail) console.log(`         → ${detail}`);
  ok &&= passed;
}
console.log('-'.repeat(70));
console.log('  entità per tipo:', JSON.stringify(byKind));
console.log('='.repeat(70));
console.log('RISULTATO:', ok ? 'MIGRAZIONE VERIFICATA' : 'CI SONO DIFFERENZE');
process.exit(ok ? 0 : 1);
