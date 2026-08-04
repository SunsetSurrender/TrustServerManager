// ============================================================
// identity-tests.mjs — test JavaScript della logica di identità
//
// Due parti:
//
//  1. GUIDATA DALLE FIXTURE — fixtures/identity/*.json, le stesse consumate
//     dalla suite Python (backend/tests/). Qui si verificano validità e codici
//     di errore; gli eventi di dominio li verifica il motore di diff, che vive
//     solo lato Python.
//
//  2. SPECIFICA DEL FRONTEND — corrispondenza per l'import da foglio e
//     mappatura di intestazioni e valori: logica che esiste solo qui, perché
//     serve all'applicazione.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/identity-tests.mjs
// ============================================================
import { readdirSync, readFileSync } from 'node:fs';
import {
  isUid, newUid, walkEntities, validateDocument, validateAgainstBase,
  preserveIdentity, matchDeviceForImport,
  normalizeHeaders, parseStato, parseTipo,
  canonicalise, canonicalSort, stripUids, ENTITY_DEFAULTS,
  checkSchemaVersion, CURRENT_SCHEMA_VERSION,
  SCHEMA_VERSION_MISSING, SCHEMA_VERSION_TOO_OLD,
  SCHEMA_VERSION_TOO_NEW, SCHEMA_VERSION_INVALID,
} from '../handoff/identity.js';

let pass = 0;
const failures = [];

const ok = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else {
    failures.push(name);
    console.log(`  [FAIL] ${name}${detail ? '\n         → ' + detail : ''}`);
  }
};

const clone = (o) => JSON.parse(JSON.stringify(o));

// =====================================================================
// 1. Fixture condivise
// =====================================================================
console.log('\n— fixture condivise (contratto con la suite Python) —');

const FIXDIR = 'fixtures/identity';
const fixtures = readdirSync(FIXDIR).filter((f) => f.endsWith('.json'))
  .sort()
  .map((f) => JSON.parse(readFileSync(`${FIXDIR}/${f}`, 'utf8')));

ok(`fixture caricate (${fixtures.length})`, fixtures.length > 0, FIXDIR);

for (const fx of fixtures) {
  const errors = validateAgainstBase(fx.before, fx.after);
  const valid = errors.length === 0;
  ok(`${fx.name}: validità ${fx.expectedValid}`, valid === fx.expectedValid,
     JSON.stringify(errors.slice(0, 2)));
  if (!fx.expectedValid) {
    const got = [...new Set(errors.map((e) => e.code))].sort();
    const missing = fx.expectedErrorCodes.filter((c) => !got.includes(c));
    ok(`${fx.name}: codici ${JSON.stringify(fx.expectedErrorCodes)}`, missing.length === 0,
       `attesi ${JSON.stringify(fx.expectedErrorCodes)}, ottenuti ${JSON.stringify(got)}`);
  }
}

// Il motore di diff è lato Python: qui si verifica solo che le fixture
// dichiarino gli eventi, così un'aggiunta senza aspettative non passa inosservata.
const withEvents = fixtures.filter((f) => f.expectedEvents !== null);
ok('ogni fixture valida dichiara gli eventi attesi',
   withEvents.length === fixtures.filter((f) => f.expectedValid).length,
   `${withEvents.length} con eventi, ${fixtures.filter((f) => f.expectedValid).length} valide`);

// =====================================================================
// 2. Specifica del frontend
// =====================================================================
console.log('\n— fondamentali —');
ok('newUid produce un UUID valido', isUid(newUid()));
ok('isUid rifiuta un UUID non v4', !isUid('11111111-1111-1111-1111-111111111111'));
ok('isUid rifiuta stringhe arbitrarie', !isUid('non-un-uuid') && !isUid('') && !isUid(null));

console.log('\n— preserveIdentity —');
{
  const orig = { _uid: 'aaaaaaaa-0000-4000-8000-000000000001', id: 'x', name: 'x',
                 campo_futuro: 'da conservare' };
  const out = preserveIdentity(orig, { name: 'y' });
  ok('conserva l\'_uid', out._uid === orig._uid);
  ok('conserva i campi sconosciuti', out.campo_futuro === 'da conservare');
  ok('applica la modifica', out.name === 'y');
  const nuovo = preserveIdentity(null, { id: 'z', name: 'z' });
  ok('genera l\'_uid per le entità nuove', isUid(nuovo._uid));
}

// ---------------------------------------------------------------- import
const RA = 'cccccccc-0000-4000-8000-00000000000a';
const RB = 'cccccccc-0000-4000-8000-00000000000b';
const DA = 'dddddddd-0000-4000-8000-00000000000a';
const DB = 'dddddddd-0000-4000-8000-00000000000b';

const dev = (uid, name, extra = {}) =>
  ({ _uid: uid, id: name, name, type: 'server', stato: 'attivo', u: 10, h: 1, ...extra });

const docWith = (aDevs, bDevs) => ({
  locations: [{ _uid: 'aaaaaaaa-0000-4000-8000-000000000001', id: 's', nome: 'S', sale: [
    { _uid: 'bbbbbbbb-0000-4000-8000-000000000001', id: 'r', nome: 'R', vani: [], racks: [
      { _uid: RA, id: 'R01', name: 'R01', u: 45, x: 0, y: 0, w: 0.6, h: 0.8, devices: aDevs },
      { _uid: RB, id: 'R02', name: 'R02', u: 45, x: 1, y: 0, w: 0.6, h: 0.8, devices: bDevs },
    ] }] }],
});
const rackOf = (d, uid) => d.locations[0].sale[0].racks.find((r) => r._uid === uid);

console.log('\n— import da foglio: identità —');
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { _uid: DA, nome: 'nome-cambiato' }, rackOf(d, RA));
  ok('_uid vince sul nome cambiato', m.match && m.match.uid === DA && !m.ambiguous, m.reason);
}
{
  // Con l'_uid lo spostamento fra rack è ammesso: l'identità è certa.
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { _uid: DA, nome: 'srv-01' }, rackOf(d, RB));
  ok('_uid consente lo spostamento fra rack', m.match && m.match.uid === DA, m.reason);
}
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { _uid: 'ffffffff-0000-4000-8000-00000000000f', nome: 'srv-01' }, rackOf(d, RA));
  ok('_uid inesistente rifiutato, non ricade sul nome', m.ambiguous && !m.match, m.reason);
}
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { _uid: 'non-un-uuid', nome: 'srv-01' }, rackOf(d, RA));
  ok('_uid malformato rifiutato', m.ambiguous && !m.match, m.reason);
}

console.log('\n— import da foglio: riga legacy senza _uid —');
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { nome: 'srv-01' }, rackOf(d, RA));
  ok('aggiorna se univoco NEL rack', m.match && m.match.uid === DA, m.reason);
}
{
  // Il caso che prima spostava silenziosamente il dispositivo.
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { nome: 'srv-01' }, rackOf(d, RB));
  ok('NON sposta fra rack senza _uid', m.ambiguous && !m.match, m.reason);
  ok('  e lo dice esplicitamente', /_uid/.test(m.reason), m.reason);
}
{
  // id non è univoco a livello globale: due rack possono avere lo stesso id.
  const d = docWith([dev(DA, 'srv-01')], [dev(DB, 'srv-01')]);
  const m = matchDeviceForImport(d, { id: 'srv-01', nome: 'srv-01' }, rackOf(d, RA));
  ok('id ripetuto fra rack: risolve nel rack di destinazione',
     m.match && m.match.uid === DA, m.reason);
  const m2 = matchDeviceForImport(d, { id: 'srv-01', nome: 'srv-01' }, rackOf(d, RB));
  ok('  e nell\'altro rack risolve all\'altro', m2.match && m2.match.uid === DB, m2.reason);
}
{
  // stesso nome DUE volte nello stesso rack: ambiguo
  const d = docWith([dev(DA, 'srv-01'), { ...dev(DB, 'altro'), name: 'srv-01' }], []);
  const m = matchDeviceForImport(d, { nome: 'srv-01' }, rackOf(d, RA));
  ok('nome duplicato nel rack: ambiguo', m.ambiguous && !m.match, m.reason);
}
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { nome: 'mai-visto' }, rackOf(d, RB));
  ok('nome sconosciuto: nuovo', !m.match && !m.ambiguous && m.isNew, m.reason);
}
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { nome: 'srv-01' }, null);
  ok('senza rack di destinazione e senza _uid: rifiuto', m.ambiguous, m.reason);
}
{
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, {}, rackOf(d, RA));
  ok('riga senza _uid, id e nome: rifiuto', m.ambiguous, m.reason);
}

console.log('\n— import da foglio: intestazioni e valori —');
{
  // È il difetto reale: l'export XLSX formattato scrive "Altezza U", l'import
  // cercava "h", e le altezze tornavano tutte a 1.
  const h = normalizeHeaders(['_uid', 'Location', 'Sala', 'Rack', 'Nome', 'Tipo', 'Stato',
                              'Modello', 'IP', 'Seriale', 'Referente', 'U', 'Altezza U',
                              'Garanzia', 'Supporto', 'Note']);
  ok('"Altezza U" → h', h.includes('h'), JSON.stringify(h));
  ok('tutte le colonne obbligatorie presenti',
     ['location', 'sala', 'rack', 'nome'].every((c) => h.includes(c)), JSON.stringify(h));
  ok('_uid riconosciuto', h[0] === '_uid', JSON.stringify(h));
  ok('U resta u', h.includes('u'), JSON.stringify(h));
}
{
  ok('intestazioni del modello tecnico invariate',
     JSON.stringify(normalizeHeaders(['location', 'sala', 'rack', 'nome', 'h', 'u']))
       === JSON.stringify(['location', 'sala', 'rack', 'nome', 'h', 'u']));
  ok('spazi e maiuscole normalizzati',
     JSON.stringify(normalizeHeaders(['  Nome  ', 'ALTEZZA   U'])) === JSON.stringify(['nome', 'h']));
}
{
  // Le etichette degli stati NON coincidono con le chiavi: senza mappatura un
  // re-import riportava manutenzione e dismissione ad "attivo".
  ok('"In manutenzione" → manutenzione', parseStato('In manutenzione') === 'manutenzione');
  ok('"In dismissione" → dismissione', parseStato('In dismissione') === 'dismissione');
  ok('"Dismesso" → dismesso', parseStato('Dismesso') === 'dismesso');
  ok('"Attivo" → attivo', parseStato('Attivo') === 'attivo');
  ok('chiavi accettate direttamente', parseStato('manutenzione') === 'manutenzione');
  ok('vuoto → fallback fornito', parseStato('', 'dismesso') === 'dismesso');
  ok('valore ignoto → fallback fornito', parseStato('qualcosa', 'manutenzione') === 'manutenzione');
}
{
  ok('"Server" → server', parseTipo('Server') === 'server');
  ok('"Alimentazione" → alimentazione', parseTipo('Alimentazione') === 'alimentazione');
  ok('tipo ignoto → fallback', parseTipo('Sconosciuto', 'altro') === 'altro');
  ok('vuoto → fallback fornito', parseTipo('', 'rete') === 'rete');
}

console.log('\n— forma canonica (gemella di backend/app/identity/canonical.py) —');
{
  const doc = docWith([{ _uid: DA, id: 'srv-01', name: 'srv-01', u: 10 }], []);
  const before = JSON.stringify(doc);
  const out = canonicalise(doc);
  ok('è pura: non modifica l\'input', JSON.stringify(doc) === before);

  const d0 = out.locations[0].sale[0].racks[0].devices[0];
  ok('materializza stato = attivo', d0.stato === 'attivo');
  ok('materializza h = 1', d0.h === 1);
  ok('materializza type = altro', d0.type === 'altro');
  ok('materializza i campi testuali a stringa vuota', d0.model === '' && d0.note === '');
  ok('materializza rack.seriali = []', Array.isArray(out.locations[0].sale[0].racks[0].seriali));

  const twice = canonicalise(out);
  ok('è idempotente',
     JSON.stringify(canonicalSort(out)) === JSON.stringify(canonicalSort(twice)));
}
{
  // Non deve inventare identità né versione di schema: quelle si rifiutano.
  const senzaUid = { locations: [{ id: 's', nome: 'S', sale: [] }] };
  const out = canonicalise(senzaUid);
  ok('non inventa _uid', out.locations[0]._uid === undefined);
  ok('non inventa schemaVersion', out.schemaVersion === undefined);
  ok('non inventa notifiche/smtp', out.notifiche === undefined && out.smtp === undefined);
}
{
  const doc = { schemaVersion: 1, locations: [], smtp: { host: 'mail' } };
  const out = canonicalise(doc);
  ok('completa i sotto-campi delle impostazioni esistenti', out.smtp.porta === 587);
  ok('NON materializza la password SMTP', !('password' in out.smtp));
}
{
  const doc = docWith([{ _uid: DA, id: 'd', name: 'd', u: 10, note: '', h: 3 }], []);
  const d0 = canonicalise(doc).locations[0].sale[0].racks[0].devices[0];
  ok('conserva i valori falsy espliciti', d0.note === '' && d0.h === 3);
}
{
  ok('la tabella dei default copre solo le entità con identità',
     JSON.stringify(Object.keys(ENTITY_DEFAULTS).sort()) ===
     JSON.stringify(['device', 'location', 'manual', 'rack', 'room']));
  ok('i vani non hanno default (non sono entità)', ENTITY_DEFAULTS.vano === undefined);
}
{
  const doc = docWith([{ _uid: DA, id: 'd', name: 'd', u: 10 }], []);
  ok('stripUids rimuove ogni _uid', !JSON.stringify(stripUids(doc)).includes('_uid'));
}

console.log('\n— versione di schema —');
{
  ok('versione corrente accettata',
     checkSchemaVersion({ schemaVersion: CURRENT_SCHEMA_VERSION }).length === 0);
  ok('assente → legacy',
     checkSchemaVersion({}).map(e => e.code)[0] === SCHEMA_VERSION_MISSING);
  ok('più vecchia → migrazione richiesta',
     checkSchemaVersion({ schemaVersion: CURRENT_SCHEMA_VERSION - 1 }).map(e => e.code)[0]
       === SCHEMA_VERSION_TOO_OLD);
  ok('più recente → rifiutata',
     checkSchemaVersion({ schemaVersion: CURRENT_SCHEMA_VERSION + 1 }).map(e => e.code)[0]
       === SCHEMA_VERSION_TOO_NEW);
  for (const bad of ['1', 1.5, true, [], {}]) {
    ok(`non intero rifiutato: ${JSON.stringify(bad)}`,
       checkSchemaVersion({ schemaVersion: bad }).map(e => e.code)[0] === SCHEMA_VERSION_INVALID);
  }
  ok('indipendente dal campo `versione` del prototipo',
     checkSchemaVersion({ schemaVersion: CURRENT_SCHEMA_VERSION, versione: 99 }).length === 0);
  ok('il seed delle fixture dichiara la versione corrente',
     fixtures.every(f => checkSchemaVersion(f.before).length === 0));
}

console.log('\n— import: righe contraddittorie e duplicate —');
{
  // id e nome che puntano a dispositivi DIVERSI nello stesso rack.
  const d = docWith([dev(DA, 'srv-01'), dev(DB, 'srv-02')], []);
  const m = matchDeviceForImport(d, { id: 'srv-01', nome: 'srv-02' }, rackOf(d, RA));
  ok('id e nome discordanti → rifiuto', m.ambiguous && !m.match, m.reason);
  ok('  e il motivo lo spiega', /contraddittoria|diversi/.test(m.reason), m.reason);
}
{
  // id e nome concordanti sullo stesso dispositivo: nessun problema.
  const d = docWith([dev(DA, 'srv-01')], []);
  const m = matchDeviceForImport(d, { id: 'srv-01', nome: 'srv-01' }, rackOf(d, RA));
  ok('id e nome concordanti → accettato', m.match && m.match.uid === DA, m.reason);
}
{
  // due righe che puntano allo stesso _uid
  const d = docWith([dev(DA, 'srv-01')], []);
  const claimed = new Set();
  const m1 = matchDeviceForImport(d, { _uid: DA, nome: 'srv-01' }, rackOf(d, RA), claimed);
  ok('prima riga accettata', !!m1.match, m1.reason);
  claimed.add(m1.match.uid);
  const m2 = matchDeviceForImport(d, { _uid: DA, nome: 'srv-01' }, rackOf(d, RA), claimed);
  ok('seconda riga con lo stesso _uid → rifiuto', m2.ambiguous && !m2.match, m2.reason);
  ok('  e il motivo cita le righe duplicate', /duplicate/.test(m2.reason), m2.reason);
}
{
  // due righe che risolvono allo STESSO dispositivo per vie diverse
  const d = docWith([dev(DA, 'srv-01')], []);
  const claimed = new Set();
  const m1 = matchDeviceForImport(d, { nome: 'srv-01' }, rackOf(d, RA), claimed);
  claimed.add(m1.match.uid);
  const m2 = matchDeviceForImport(d, { _uid: DA, nome: 'altro-nome' }, rackOf(d, RA), claimed);
  ok('stesso dispositivo risolto per vie diverse → rifiuto', m2.ambiguous, m2.reason);
}
{
  // un nuovo dispositivo non "rivendica" niente: due righe nuove sono ammesse
  const d = docWith([dev(DA, 'srv-01')], []);
  const claimed = new Set();
  const a = matchDeviceForImport(d, { nome: 'nuovo-1' }, rackOf(d, RB), claimed);
  const b = matchDeviceForImport(d, { nome: 'nuovo-2' }, rackOf(d, RB), claimed);
  ok('due righe nuove distinte sono ammesse', a.isNew && b.isNew);
}

console.log('\n— undo / redo —');
{
  const v0 = docWith([dev(DA, 'srv-01')], []);
  const v1 = clone(v0);
  const t = rackOf(v1, RA).devices[0];
  Object.assign(t, preserveIdentity(t, { model: 'M2' }));
  ok('undo: identità intatte', validateAgainstBase(v1, clone(v0)).length === 0);
  ok('redo: identità intatte', validateAgainstBase(clone(v0), v1).length === 0);
  ok('gli _uid non cambiano mai',
     JSON.stringify(walkEntities(v0).map((e) => e.uid)) ===
     JSON.stringify(walkEntities(v1).map((e) => e.uid)));
}

console.log('\n' + '='.repeat(70));
console.log(`${pass} test passati, ${failures.length} falliti`);
if (failures.length) {
  console.log('\nFalliti:');
  for (const f of failures) console.log('  - ' + f);
}
console.log('='.repeat(70));
process.exit(failures.length ? 1 : 0);
