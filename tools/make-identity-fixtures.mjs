// ============================================================
// make-identity-fixtures.mjs — genera fixtures/identity/*.json
//
// Le fixture sono il CONTRATTO neutro rispetto al linguaggio fra:
//   - tools/identity-tests.mjs        (JavaScript: validità e codici di errore)
//   - backend/tests/test_*.py         (Python: validità, codici ED eventi)
//
// Questo script è solo comodità di generazione: la verità sono i file JSON
// committati. Le aspettative (`expectedValid`, `expectedErrorCodes`,
// `expectedEvents`) sono SCRITTE A MANO qui, non calcolate da un motore di
// diff — altrimenti i test verificherebbero l'implementazione contro sé stessa.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/make-identity-fixtures.mjs
// ============================================================
import { mkdirSync, writeFileSync, readdirSync, rmSync } from 'node:fs';

const OUT = 'fixtures/identity';

// UUID leggibili: la cifra dopo il terzo gruppo è '4' (v4) e il quarto gruppo
// inizia con '8', come pretende la validazione.
const U = {
  loc1:  'aaaaaaaa-0000-4000-8000-000000000001',
  loc2:  'aaaaaaaa-0000-4000-8000-000000000002',
  room1: 'bbbbbbbb-0000-4000-8000-000000000001',
  room2: 'bbbbbbbb-0000-4000-8000-000000000002',
  rackA: 'cccccccc-0000-4000-8000-00000000000a',
  rackB: 'cccccccc-0000-4000-8000-00000000000b',
  rackC: 'cccccccc-0000-4000-8000-00000000000c',
  devA:  'dddddddd-0000-4000-8000-00000000000a',
  devB:  'dddddddd-0000-4000-8000-00000000000b',
  devC:  'dddddddd-0000-4000-8000-00000000000c',
  man1:  'eeeeeeee-0000-4000-8000-000000000001',
  fresh: 'ffffffff-0000-4000-8000-00000000000f',
  other: 'ffffffff-0000-4000-8000-00000000000e',
  dup1:  'ffffffff-0000-4000-8000-00000000000c',
  dup2:  'ffffffff-0000-4000-8000-00000000000d',
};

const dev = (uid, id, extra = {}) => ({
  _uid: uid, id, name: id, type: 'server', stato: 'attivo',
  model: '', ip: '', serial: '', owner: '', u: 10, h: 1, ...extra,
});

const base = () => ({
  // `schemaVersion` è la forma del documento (§8.13); `versione` è il residuo
  // informale del prototipo, senza semantica.
  schemaVersion: 1,
  versione: 3,
  notifiche: { email: 'a@b.c', giorni: 30, attive: false },
  manuale: [{ _uid: U.man1, id: 'man-1', titolo: 'Voce uno', blocchi: [] }],
  locations: [
    {
      _uid: U.loc1, id: 'sito-1', nome: 'Sito 1',
      sale: [
        {
          _uid: U.room1, id: 'sala-1', nome: 'Sala 1', w: 6, h: 5,
          vani: [{ x: 0, y: 0, w: 6, h: 5 }],
          racks: [
            { _uid: U.rackA, id: 'R01', name: 'Rack R01', row: 'A', u: 45,
              x: 0.5, y: 0.5, w: 0.6, h: 0.8,
              devices: [dev(U.devA, 'srv-01', { u: 10 }), dev(U.devB, 'srv-02', { u: 20 })] },
            { _uid: U.rackB, id: 'R02', name: 'Rack R02', row: 'A', u: 45,
              x: 1.5, y: 0.5, w: 0.6, h: 0.8,
              devices: [dev(U.devC, 'srv-03', { u: 30 })] },
            { _uid: U.rackC, id: 'R03', name: 'Rack R03', row: 'B', u: 45,
              x: 2.5, y: 0.5, w: 0.6, h: 0.8, devices: [] },
          ],
        },
        {
          _uid: U.room2, id: 'sala-2', nome: 'Sala 2', w: 4, h: 4,
          vani: [{ x: 0, y: 0, w: 4, h: 4 }], racks: [],
        },
      ],
    },
    { _uid: U.loc2, id: 'sito-2', nome: 'Sito 2', sale: [] },
  ],
});

const clone = (o) => JSON.parse(JSON.stringify(o));
const racksOf = (d) => d.locations[0].sale[0].racks;
const rackBy = (d, uid) => racksOf(d).find((r) => r._uid === uid);
const devBy = (d, uid) => {
  for (const r of racksOf(d)) { const f = r.devices.find((x) => x._uid === uid); if (f) return f; }
  return null;
};

const fixtures = [];
const add = (f) => fixtures.push(f);

// =====================================================================
// CASI VALIDI, con eventi di dominio attesi
// =====================================================================

add({
  name: 'add-device',
  description: 'Nuovo dispositivo in un rack esistente: un solo evento add.',
  before: base(),
  after: (() => { const d = base(); rackBy(d, U.rackC).devices.push(dev(U.fresh, 'srv-nuovo', { u: 5 })); return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{ event: 'add', entity: 'device', uid: U.fresh, scope: 'devices', parentUid: U.rackC }],
});

add({
  name: 'delete-device',
  description: 'Cancellazione autentica: nessun rimpiazzo, un solo evento delete.',
  before: base(),
  after: (() => { const d = base(); const r = rackBy(d, U.rackA); r.devices = r.devices.filter((x) => x._uid !== U.devA); return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{ event: 'delete', entity: 'device', uid: U.devA, scope: 'devices', parentUid: U.rackA }],
});

add({
  name: 'delete-plus-unrelated-add',
  description: 'Delete di A e add di un dispositivo NON correlato: due eventi legittimi, nessun rifiuto.',
  before: base(),
  after: (() => {
    const d = base(); const r = rackBy(d, U.rackA);
    r.devices = r.devices.filter((x) => x._uid !== U.devA);
    r.devices.push(dev(U.fresh, 'srv-diverso', { u: 12 }));
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [
    { event: 'add', entity: 'device', uid: U.fresh, scope: 'devices', parentUid: U.rackA },
    { event: 'delete', entity: 'device', uid: U.devA, scope: 'devices', parentUid: U.rackA },
  ],
});

add({
  name: 'update-device-attribute',
  description: 'Cambio di attributi non identificanti: un update con le differenze campo per campo.',
  before: base(),
  after: (() => { const d = base(); const x = devBy(d, U.devA); x.model = 'Dell R760'; x.ip = '10.0.0.5'; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'update', entity: 'device', uid: U.devA, scope: 'devices',
    changes: { ip: ['', '10.0.0.5'], model: ['', 'Dell R760'] },
  }],
});

add({
  name: 'update-device-height',
  description: 'L\'altezza in U è una dimensione, non una posizione: update, non move.',
  before: base(),
  after: (() => { const d = base(); devBy(d, U.devA).h = 4; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{ event: 'update', entity: 'device', uid: U.devA, scope: 'devices', changes: { h: [1, 4] } }],
});

add({
  name: 'rename-device',
  description: 'Rinomina di codice e nome a _uid invariato: evento rename, non delete+add.',
  before: base(),
  after: (() => { const d = base(); const x = devBy(d, U.devA); x.id = 'srv-01-new'; x.name = 'srv-01-new'; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'rename', entity: 'device', uid: U.devA, scope: 'devices',
    changes: { id: ['srv-01', 'srv-01-new'], name: ['srv-01', 'srv-01-new'] },
  }],
});

add({
  name: 'rename-rack',
  description: 'Rinomina di rack: ambito structure.',
  before: base(),
  after: (() => { const d = base(); rackBy(d, U.rackA).id = 'R01-bis'; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'rename', entity: 'rack', uid: U.rackA, scope: 'structure',
    changes: { id: ['R01', 'R01-bis'] },
  }],
});

add({
  name: 'move-device-between-racks',
  description: 'Spostamento fra rack: ambito devices anche se tocca due sottoalberi di rack. ' +
               'È il caso che un diff per percorso classificherebbe come structure.',
  before: base(),
  after: (() => {
    const d = base(); const a = rackBy(d, U.rackA), b = rackBy(d, U.rackB);
    const x = a.devices.find((y) => y._uid === U.devA);
    a.devices = a.devices.filter((y) => y._uid !== U.devA);
    b.devices.push(x);
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'move', entity: 'device', uid: U.devA, scope: 'devices',
    fromParentUid: U.rackA, toParentUid: U.rackB, fromPos: { u: 10 }, toPos: { u: 10 },
  }],
});

add({
  name: 'move-device-slot-same-rack',
  description: 'Cambio di slot U a rack invariato: sempre move, con solo la posizione diversa.',
  before: base(),
  after: (() => { const d = base(); devBy(d, U.devA).u = 40; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'move', entity: 'device', uid: U.devA, scope: 'devices',
    fromParentUid: U.rackA, toParentUid: U.rackA, fromPos: { u: 10 }, toPos: { u: 40 },
  }],
});

add({
  name: 'move-rack-position',
  description: 'Rack trascinato in pianta: move con coordinate, ambito structure.',
  before: base(),
  after: (() => { const d = base(); const r = rackBy(d, U.rackA); r.x = 3.25; r.y = 2.75; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'move', entity: 'rack', uid: U.rackA, scope: 'structure',
    fromParentUid: U.room1, toParentUid: U.room1,
    fromPos: { x: 0.5, y: 0.5 }, toPos: { x: 3.25, y: 2.75 },
  }],
});

add({
  name: 'rename-and-move',
  description: 'Rinomina E spostamento sulla stessa entità: DUE eventi distinti, non fusi. ' +
               'Gli ambiti potrebbero differire e l\'autorizzazione lavora per evento.',
  before: base(),
  after: (() => {
    const d = base(); const a = rackBy(d, U.rackA), b = rackBy(d, U.rackB);
    const x = a.devices.find((y) => y._uid === U.devA);
    x.id = 'srv-01-rinominato'; x.name = 'srv-01-rinominato'; x.u = 33;
    a.devices = a.devices.filter((y) => y._uid !== U.devA);
    b.devices.push(x);
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [
    { event: 'rename', entity: 'device', uid: U.devA, scope: 'devices',
      changes: { id: ['srv-01', 'srv-01-rinominato'], name: ['srv-01', 'srv-01-rinominato'] } },
    { event: 'move', entity: 'device', uid: U.devA, scope: 'devices',
      fromParentUid: U.rackA, toParentUid: U.rackB, fromPos: { u: 10 }, toPos: { u: 33 } },
  ],
});

add({
  name: 'reorder-racks',
  description: 'Riordino puro dei rack nella sala: insieme dei figli identico, ordine diverso.',
  before: base(),
  after: (() => { const d = base(); const r = racksOf(d); r.reverse(); return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'reorder', entity: 'rack', uid: null, scope: 'structure', parentUid: U.room1,
    from: [U.rackA, U.rackB, U.rackC], to: [U.rackC, U.rackB, U.rackA],
  }],
});

add({
  name: 'reorder-rooms',
  description: 'Riordino delle sale (azione reale della UI: "Riordinata sala").',
  before: base(),
  after: (() => { const d = base(); d.locations[0].sale.reverse(); return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'reorder', entity: 'room', uid: null, scope: 'structure', parentUid: U.loc1,
    from: [U.room1, U.room2], to: [U.room2, U.room1],
  }],
});

add({
  name: 'reorder-suppressed-by-add',
  description: 'Ordine cambiato MA con un add fra i fratelli: il reorder NON si emette, ' +
               'perché l\'ordine è cambiato come conseguenza e segnalarlo è rumore.',
  before: base(),
  after: (() => {
    const d = base(); const r = racksOf(d);
    r.unshift({ _uid: U.fresh, id: 'R00', name: 'Rack R00', row: 'A', u: 45,
                x: 0.1, y: 0.1, w: 0.6, h: 0.8, devices: [] });
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{ event: 'add', entity: 'rack', uid: U.fresh, scope: 'structure', parentUid: U.room1 }],
});

add({
  name: 'reorder-suppressed-by-delete',
  description: 'Ordine cambiato per via di una cancellazione: solo delete, nessun reorder. ' +
               'Si elimina il rack VUOTO, così la fixture isola la soppressione del reorder ' +
               'senza le cancellazioni a cascata dei dispositivi.',
  before: base(),
  after: (() => {
    const d = base();
    d.locations[0].sale[0].racks = racksOf(d).filter((r) => r._uid !== U.rackC).reverse();
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{ event: 'delete', entity: 'rack', uid: U.rackC, scope: 'structure', parentUid: U.room1 }],
});

add({
  name: 'delete-rack-cascades-to-devices',
  description: 'Eliminare un rack elimina anche i suoi dispositivi: un delete per ogni ' +
               'entità scomparsa. Gli eventi sono ordinati per tipo (rack prima di device) ' +
               'e poi per uid.',
  before: base(),
  after: (() => {
    const d = base();
    d.locations[0].sale[0].racks = racksOf(d).filter((r) => r._uid !== U.rackA);
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [
    { event: 'delete', entity: 'rack', uid: U.rackA, scope: 'structure', parentUid: U.room1 },
    { event: 'delete', entity: 'device', uid: U.devA, scope: 'devices', parentUid: U.rackA },
    { event: 'delete', entity: 'device', uid: U.devB, scope: 'devices', parentUid: U.rackA },
  ],
});

add({
  name: 'no-change',
  description: 'Documento identico: nessun evento. Un PUT senza modifiche non deve creare una versione.',
  before: base(), after: base(),
  expectedValid: true, expectedErrorCodes: [], expectedEvents: [],
});

add({
  name: 'update-vani-is-room-update',
  description: 'I vani non hanno identità: un cambio di geometria è un update sulla SALA.',
  before: base(),
  after: (() => { const d = base(); d.locations[0].sale[0].vani[0].w = 7; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'update', entity: 'room', uid: U.room1, scope: 'structure',
    changes: { vani: [[{ x: 0, y: 0, w: 6, h: 5 }], [{ x: 0, y: 0, w: 7, h: 5 }]] },
  }],
});

add({
  name: 'update-manual-entry',
  description: 'Le voci di manuale SONO entità identificate: update sul loro _uid, ambito manuale.',
  before: base(),
  after: (() => { const d = base(); d.manuale[0].titolo = 'Voce uno rivista'; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'rename', entity: 'manual', uid: U.man1, scope: 'manuale',
    changes: { titolo: ['Voce uno', 'Voce uno rivista'] },
  }],
});

add({
  name: 'update-settings',
  description: 'Le impostazioni non hanno identità: update su una pseudo-entità, ambito settings.',
  before: base(),
  after: (() => { const d = base(); d.notifiche.giorni = 45; return d; })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [{
    event: 'update', entity: 'settings', uid: null, scope: 'settings',
    changes: { 'notifiche.giorni': [30, 45] },
  }],
});

add({
  name: 'add-location-and-room',
  description: 'Creazione di sito e sala: due add, ordinati per tipo (location prima di room).',
  before: base(),
  after: (() => {
    const d = base();
    d.locations.push({ _uid: U.fresh, id: 'sito-3', nome: 'Sito 3',
      sale: [{ _uid: U.other, id: 'sala-3', nome: 'Sala 3', w: 3, h: 3, vani: [], racks: [] }] });
    return d;
  })(),
  expectedValid: true, expectedErrorCodes: [],
  expectedEvents: [
    { event: 'add', entity: 'location', uid: U.fresh, scope: 'structure', parentUid: null },
    { event: 'add', entity: 'room', uid: U.other, scope: 'structure', parentUid: U.fresh },
  ],
});

// =====================================================================
// CASI NON VALIDI — ogni codice di rifiuto
// =====================================================================

add({
  name: 'reject-missing-uid',
  description: 'Entità esistente privata dell\'_uid: dato distrutto, non modifica.',
  before: base(),
  after: (() => { const d = base(); delete devBy(d, U.devA)._uid; return d; })(),
  expectedValid: false, expectedErrorCodes: ['missing_uid'], expectedEvents: null,
});

add({
  name: 'reject-malformed-uid',
  description: '_uid che non è un UUID: rifiuto sintattico.',
  before: base(),
  after: (() => { const d = base(); devBy(d, U.devA)._uid = 'non-un-uuid'; return d; })(),
  expectedValid: false, expectedErrorCodes: ['malformed_uid'], expectedEvents: null,
});

add({
  name: 'reject-non-v4-uuid',
  description: 'UUID sintatticamente plausibile ma non v4: rifiutato come malformato.',
  before: base(),
  after: (() => { const d = base(); devBy(d, U.devA)._uid = '11111111-1111-1111-1111-111111111111'; return d; })(),
  expectedValid: false, expectedErrorCodes: ['malformed_uid'], expectedEvents: null,
});

add({
  name: 'reject-duplicate-uid',
  description: 'Stesso _uid su due entità: ogni diff diventa ambiguo.',
  before: base(),
  after: (() => { const d = base(); rackBy(d, U.rackC).devices.push(dev(U.devA, 'clone')); return d; })(),
  expectedValid: false, expectedErrorCodes: ['duplicate_uid'], expectedEvents: null,
});

add({
  name: 'reject-identity-replacement-same-parent',
  description: 'Delete + add con lo stesso codice nello stesso rack: rinomina mascherata. ' +
               'Deve conservare l\'_uid.',
  before: base(),
  after: (() => {
    const d = base(); const r = rackBy(d, U.rackA);
    r.devices = r.devices.filter((x) => x._uid !== U.devA);
    r.devices.push(dev(U.fresh, 'srv-01', { u: 10 }));
    return d;
  })(),
  expectedValid: false, expectedErrorCodes: ['identity_replacement'], expectedEvents: null,
});

add({
  name: 'reject-identity-replacement-cross-parent',
  description: 'Stesso codice in un altro rack con il vecchio _uid svanito: spostamento mascherato.',
  before: base(),
  after: (() => {
    const d = base(); const a = rackBy(d, U.rackA), b = rackBy(d, U.rackB);
    a.devices = a.devices.filter((x) => x._uid !== U.devA);
    b.devices.push(dev(U.fresh, 'srv-01', { u: 10 }));
    return d;
  })(),
  expectedValid: false, expectedErrorCodes: ['identity_replacement'], expectedEvents: null,
});

add({
  name: 'reject-business-key-reuse',
  description: '_uid nuovo su un codice ancora in uso da un\'entità presente.',
  before: base(),
  after: (() => { const d = base(); rackBy(d, U.rackA).devices.push(dev(U.fresh, 'srv-01', { u: 44 })); return d; })(),
  expectedValid: false, expectedErrorCodes: ['business_key_reuse'], expectedEvents: null,
});

add({
  name: 'reject-rack-identity-replacement',
  description: 'Rack sostituito con _uid nuovo e stesso codice.',
  before: base(),
  after: (() => {
    const d = base();
    d.locations[0].sale[0].racks[0] = { _uid: U.fresh, id: 'R01', name: 'Rack R01', row: 'A',
                                       u: 45, x: 0.5, y: 0.5, w: 0.6, h: 0.8, devices: [] };
    return d;
  })(),
  expectedValid: false,
  // I due dispositivi del rack sparito diventano anch'essi delete non spiegati;
  // il codice che conta è identity_replacement sul rack.
  expectedErrorCodes: ['identity_replacement'], expectedEvents: null,
});

add({
  name: 'reject-manual-identity-replacement',
  description: 'Voce di manuale ricostruita da zero: era il bug di manSave.',
  before: base(),
  after: (() => { const d = base(); d.manuale[0] = { _uid: U.fresh, id: 'man-1', titolo: 'Voce uno', blocchi: [] }; return d; })(),
  expectedValid: false, expectedErrorCodes: ['identity_replacement'], expectedEvents: null,
});

add({
  name: 'reject-ambiguous-replacement',
  description: 'Due dispositivi con lo stesso codice scompaiono e ne compare uno nuovo, ' +
               'in un rack dove quel codice non c\'era: a quale dei due corrisponde? ' +
               'Corrispondenza ambigua, si rifiuta invece di indovinare.',
  before: (() => {
    const d = base();
    // stesso codice di business in due rack diversi, con _uid distinti
    rackBy(d, U.rackA).devices.push(dev(U.dup1, 'srv-doppio', { u: 41 }));
    rackBy(d, U.rackB).devices.push(dev(U.dup2, 'srv-doppio', { u: 42 }));
    return d;
  })(),
  after: (() => {
    const d = base();
    // entrambi svaniti, uno nuovo con lo stesso codice in un terzo rack
    rackBy(d, U.rackC).devices.push(dev(U.fresh, 'srv-doppio', { u: 43 }));
    return d;
  })(),
  expectedValid: false, expectedErrorCodes: ['ambiguous_replacement'], expectedEvents: null,
});

add({
  name: 'reject-legacy-document-without-uids',
  description: 'Backup precedente agli _uid: rifiutato in blocco dal percorso normale. ' +
               'Va trattato una volta sola dallo script di migrazione.',
  before: base(),
  after: (() => {
    const d = base();
    const strip = (v) => {
      if (Array.isArray(v)) v.forEach(strip);
      else if (v && typeof v === 'object') { delete v._uid; Object.values(v).forEach(strip); }
    };
    strip(d);
    return d;
  })(),
  expectedValid: false, expectedErrorCodes: ['missing_uid'], expectedEvents: null,
});

// =====================================================================

mkdirSync(OUT, { recursive: true });
for (const f of readdirSync(OUT)) if (f.endsWith('.json')) rmSync(`${OUT}/${f}`);

const names = new Set();
for (const f of fixtures) {
  if (names.has(f.name)) throw new Error(`fixture duplicata: ${f.name}`);
  names.add(f.name);
  // Codici attesi ordinati e deduplicati: il confronto nei test è su insieme.
  if (f.expectedErrorCodes) f.expectedErrorCodes = [...new Set(f.expectedErrorCodes)].sort();
  writeFileSync(`${OUT}/${f.name}.json`, JSON.stringify(f, null, 2) + '\n', 'utf8');
}

const valid = fixtures.filter((f) => f.expectedValid).length;
console.log(`${fixtures.length} fixture scritte in ${OUT}/ (${valid} valide, ${fixtures.length - valid} di rifiuto)`);
