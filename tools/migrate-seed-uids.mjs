// ============================================================
// migrate-seed-uids.mjs — backfill UNA VOLTA SOLA degli `_uid` nel seed
//
// È l'UNICO percorso autorizzato a generare identità per dati preesistenti
// (BACKEND-PLAN.md §8.4). L'applicazione a runtime non lo fa mai: aprire o
// ricaricare l'app non deve poter fabbricare identità sostitutive.
//
// Perché uno script separato e non un flag sul percorso normale: la differenza
// fra «popolo dati che non hanno ancora storia» e «accetto una scrittura da un
// client» non va affidata a un booleano che qualcuno può passare per sbaglio.
//
// È IDEMPOTENTE: gli `_uid` già presenti vengono conservati, si generano solo
// quelli mancanti. Rieseguirlo non cambia le identità già assegnate.
//
// Il file precedente non viene salvato a parte: è in git, che è il posto giusto
// per la versione di prima.
//
// Uso (node non serve installato sulla macchina: gira in container)
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/migrate-seed-uids.mjs
//
// Scrive handoff/inventario.js. Il file diventa un letterale esplicito invece
// di un generatore: una volta che ogni entità ha un'identità stabile da
// versionare, il seed è dato, non codice.
// ============================================================
import { randomUUID } from 'node:crypto';
import { writeFileSync } from 'node:fs';

const SEED_URL = new URL('../handoff/inventario.js', import.meta.url);
const SEED_OUT = 'handoff/inventario.js';

const { DATI: doc } = await import(SEED_URL.href);
const { validateDocument, CURRENT_SCHEMA_VERSION, checkSchemaVersion } =
  await import(new URL('../handoff/identity.js', import.meta.url).href);

// La migrazione è anche l'unico posto che può PORTARE il documento alla versione
// di schema corrente: il percorso normale rifiuta e rimanda qui (§8.13).
const schemaBefore = doc.schemaVersion ?? null;
doc.schemaVersion = CURRENT_SCHEMA_VERSION;

let generated = 0;
let preserved = 0;

const stamp = (obj, kind, path) => {
  if (obj._uid) { preserved++; return; }
  obj._uid = randomUUID();
  generated++;
  if (process.env.VERBOSE) console.log(`  + ${kind.padEnd(8)} ${path}`);
};

/** `_uid` come prima chiave: rende leggibile il diff del seed. */
const uidFirst = (obj) => {
  const { _uid, ...rest } = obj;
  const out = { _uid };
  for (const k of Object.keys(rest)) out[k] = rest[k];
  return out;
};

doc.locations = (doc.locations || []).map((L) => {
  stamp(L, 'location', L.id);
  L.sale = (L.sale || []).map((R) => {
    stamp(R, 'room', `${L.id}/${R.id}`);
    R.racks = (R.racks || []).map((K) => {
      stamp(K, 'rack', `${L.id}/${R.id}/${K.id}`);
      K.devices = (K.devices || []).map((V) => {
        stamp(V, 'device', `${L.id}/${R.id}/${K.id}/${V.id}`);
        return uidFirst(V);
      });
      return uidFirst(K);
    });
    // I `vani` non vengono toccati: value object posseduti dalla sala, senza
    // identità propria. Motivazione in fondo a handoff/identity.js.
    return uidFirst(R);
  });
  return uidFirst(L);
});

// Solo se la chiave esiste già: la migrazione non inventa parti di documento.
if (Array.isArray(doc.manuale)) {
  doc.manuale = doc.manuale.map((M) => {
    stamp(M, 'manual', M.id);
    return uidFirst(M);
  });
}

// ---- verifica prima di scrivere: nessun duplicato, tutti conformi ----
const errors = [...validateDocument(doc), ...checkSchemaVersion(doc)];
if (errors.length) {
  console.error('MIGRAZIONE ANNULLATA — il documento risultante non è valido:');
  for (const e of errors) console.error(`  ${e.code}: ${e.message}`);
  process.exit(1);
}

const total = generated + preserved;

const header = `// ============================================================
// INVENTARIO SALE SERVER — livello dati (seed)
//
// GENERATO da tools/migrate-seed-uids.mjs — non modificare gli \`_uid\` a mano.
//
// Ogni location, sala, rack, dispositivo e voce di manuale porta un \`_uid\`:
// UUID v4 immutabile che è la vera identità dell'entità. I codici (\`id\`:
// R01, srv-db-01) sono rinominabili e NON sono identità.
// Vedi BACKEND-PLAN.md §8.4 e handoff/identity.js.
//
// I \`vani\` non hanno \`_uid\`: sono la geometria della sala, non entità.
//
// \`schemaVersion\` è la forma del documento (§8.13), da non confondere con la
// revisione ottimistica dell'inventario. Il campo \`versione\` è un residuo
// informale del prototipo e non ha semantica.
//
// In produzione questo modulo è sostituito dalle chiamate all'API
// (GET /api/inventory) e non entra nell'immagine web (§6 del piano).
//
// Entità con identità: ${total} · schemaVersion: ${CURRENT_SCHEMA_VERSION}
// ============================================================

export const DATI = `;

writeFileSync(SEED_OUT, header + JSON.stringify(doc, null, 2) + ';\n', 'utf8');

console.log(`Seed migrato: ${generated} _uid generati, ${preserved} conservati, ${total} totali.`);
console.log(`schemaVersion: ${schemaBefore ?? '(assente)'} → ${CURRENT_SCHEMA_VERSION}`);
