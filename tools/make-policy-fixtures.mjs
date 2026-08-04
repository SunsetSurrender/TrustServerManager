// ============================================================
// make-policy-fixtures.mjs — genera fixtures/policy/*.json
//
// Fixture della politica di autorizzazione (§8.15). Consumate dalla suite
// Python (backend/tests/test_policy.py); il formato è JSON neutro, pronto per
// un eventuale specchio JavaScript.
//
// Due modi di indicare gli eventi:
//   - `events`: lista esplicita, per i casi minimi
//   - `fromIdentityFixture`: nome di una fixture in fixtures/identity/, i cui
//     eventi vengono calcolati dal motore di diff reale. È la forma più forte —
//     cascate, rename+move e reorder arrivano dal diff, non da una lista scritta
//     a mano che potrebbe non corrispondere a ciò che il motore produce.
//
// Le aspettative sono SCRITTE A MANO: derivarle dalla politica significherebbe
// verificarla contro sé stessa.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/make-policy-fixtures.mjs
// ============================================================
import { mkdirSync, writeFileSync, readdirSync, rmSync } from 'node:fs';

const OUT = 'fixtures/policy';

const DEV = 'dddddddd-0000-4000-8000-00000000000a';
const RACK = 'cccccccc-0000-4000-8000-00000000000a';
const ROOM = 'bbbbbbbb-0000-4000-8000-000000000001';

const ev = (entity, event, uid, scope) => ({ entity, event, uid, scope });
const dev = (event, uid = DEV) => ev('device', event, uid, 'devices');
const rack = (event, uid = RACK) => ev('rack', event, uid, 'structure');
const room = (event, uid = ROOM) => ev('room', event, uid, 'structure');

const forbid = (entity, event, requiredRole = 'admin') =>
  ({ code: 'forbidden_for_role', entity, event, requiredRole });

const fixtures = [];
const add = (f) => fixtures.push(f);

// ---------------------------------------------------------- insiemi vuoti
for (const role of ['view', 'edit', 'admin']) {
  add({
    name: `empty-${role}-allowed`,
    description: `Insieme di eventi vuoto con ruolo ${role}: consentito. Non è una `
               + `scrittura, e un PUT che non produce eventi non crea nemmeno una versione.`,
    role, events: [], expectedAllowed: true, expectedViolations: [],
  });
}

// ---------------------------------------------------------- view: mai scritture
add({
  name: 'view-device-update-forbidden',
  description: 'view non può scrivere sull\'inventario, nemmeno il più innocuo update.',
  role: 'view', events: [dev('update')],
  expectedAllowed: false,
  expectedViolations: [forbid('device', 'update', 'edit')],
});

add({
  name: 'view-every-device-event-forbidden',
  description: 'Nessun evento sui dispositivi è concesso a view.',
  role: 'view',
  events: [dev('add'), dev('update'), dev('rename'), dev('move'), dev('delete')],
  expectedAllowed: false,
  expectedViolations: [
    forbid('device', 'add', 'edit'),
    forbid('device', 'delete', 'edit'),
    forbid('device', 'move', 'edit'),
    forbid('device', 'rename', 'edit'),
    forbid('device', 'update', 'edit'),
  ],
});

// ---------------------------------------------------------- edit: dispositivi
for (const e of ['add', 'update', 'rename', 'move', 'delete']) {
  add({
    name: `edit-device-${e}-allowed`,
    description: `edit può eseguire '${e}' su un dispositivo.`,
    role: 'edit', events: [dev(e)], expectedAllowed: true, expectedViolations: [],
  });
}

add({
  name: 'edit-device-reorder-forbidden',
  description: 'Il riordino non è fra gli eventi concessi a edit: riordinare una collezione '
             + 'è disposizione, non operatività sui dispositivi. Un operatore sposta '
             + '(move), non riordina.',
  role: 'edit', events: [ev('device', 'reorder', null, 'devices')],
  expectedAllowed: false,
  expectedViolations: [forbid('device', 'reorder', 'admin')],
});

add({
  name: 'edit-structure-forbidden',
  description: 'edit non tocca la struttura: rack, sale e siti sono di admin.',
  role: 'edit',
  events: [rack('update'), room('rename'), ev('location', 'add', 'aaaaaaaa-0000-4000-8000-000000000001', 'structure')],
  expectedAllowed: false,
  expectedViolations: [
    forbid('location', 'add'),
    forbid('rack', 'update'),
    forbid('room', 'rename'),
  ],
});

// -------------------------------------------- mescolanza consentito/vietato
add({
  name: 'edit-mixed-permitted-and-forbidden',
  description: 'Un evento consentito e uno vietato: l\'INTERA modifica è respinta. '
             + 'Applicare solo la parte concessa scriverebbe un documento che l\'utente '
             + 'non ha composto.',
  role: 'edit', events: [dev('update'), room('rename')],
  expectedAllowed: false,
  expectedViolations: [forbid('room', 'rename')],
});

add({
  name: 'edit-mixed-many-permitted-one-forbidden',
  description: 'Cinque eventi legittimi sui dispositivi e un solo rack toccato: respinta. '
             + 'La violazione riportata è una sola, quella effettiva.',
  role: 'edit',
  events: [dev('add'), dev('update'), dev('move'), dev('rename'), dev('delete'), rack('move')],
  expectedAllowed: false,
  expectedViolations: [forbid('rack', 'move')],
});

add({
  name: 'edit-multiple-violations-all-reported',
  description: 'Più eventi vietati: si riportano TUTTI, non solo il primo. Il client deve '
             + 'poter dire all\'utente tutto ciò che serve, in una volta.',
  role: 'edit', events: [rack('add'), room('update'), ev('manual', 'update', 'eeeeeeee-0000-4000-8000-000000000001', 'manuale')],
  expectedAllowed: false,
  expectedViolations: [
    forbid('manual', 'update'),
    forbid('rack', 'add'),
    forbid('room', 'update'),
  ],
});

// ------------------------------------------- dagli eventi del motore reale
add({
  name: 'edit-move-device-between-racks-allowed',
  description: 'Spostamento fra rack: ambito devices, quindi consentito a edit anche se '
             + 'tocca i sottoalberi di due rack. È il caso che un diff per percorso '
             + 'classificherebbe come structure, negandolo per errore.',
  role: 'edit', fromIdentityFixture: 'move-device-between-racks',
  expectedAllowed: true, expectedViolations: [],
});

add({
  name: 'edit-rename-and-move-allowed',
  description: 'rename + move sulla stessa entità: due eventi, entrambi sui dispositivi, '
             + 'entrambi concessi a edit.',
  role: 'edit', fromIdentityFixture: 'rename-and-move',
  expectedAllowed: true, expectedViolations: [],
});

add({
  name: 'edit-cascade-delete-forbidden',
  description: 'CASCATA: eliminare un rack produce il delete del rack più quello di ogni '
             + 'dispositivo contenuto. I delete di dispositivo sarebbero concessi a edit, '
             + 'ma quello del rack no: l\'intera modifica è respinta. È il caso in cui una '
             + 'politica che guarda un evento alla volta lascerebbe passare metà '
             + 'operazione.',
  role: 'edit', fromIdentityFixture: 'delete-rack-cascades-to-devices',
  expectedAllowed: false,
  expectedViolations: [forbid('rack', 'delete')],
});

add({
  name: 'admin-cascade-delete-allowed',
  description: 'La stessa cascata con ruolo admin: consentita.',
  role: 'admin', fromIdentityFixture: 'delete-rack-cascades-to-devices',
  expectedAllowed: true, expectedViolations: [],
});

add({
  name: 'edit-reorder-racks-forbidden',
  description: 'REORDER dei rack: struttura, quindi vietato a edit.',
  role: 'edit', fromIdentityFixture: 'reorder-racks',
  expectedAllowed: false,
  expectedViolations: [forbid('rack', 'reorder')],
});

add({
  name: 'admin-reorder-racks-allowed',
  description: 'Lo stesso riordino con ruolo admin: consentito.',
  role: 'admin', fromIdentityFixture: 'reorder-racks',
  expectedAllowed: true, expectedViolations: [],
});

add({
  name: 'edit-reorder-rooms-forbidden',
  description: 'Riordino delle sale ("Riordinata sala" della UI): struttura, vietato a edit.',
  role: 'edit', fromIdentityFixture: 'reorder-rooms',
  expectedAllowed: false,
  expectedViolations: [forbid('room', 'reorder')],
});

add({
  name: 'edit-add-rack-forbidden-with-suppressed-reorder',
  description: 'Inserimento di rack in testa: il reorder è soppresso, resta il solo add, '
             + 'che a edit è vietato. Verifica anche che la soppressione non introduca '
             + 'violazioni fantasma.',
  role: 'edit', fromIdentityFixture: 'reorder-suppressed-by-add',
  expectedAllowed: false,
  expectedViolations: [forbid('rack', 'add')],
});

add({
  name: 'edit-vani-change-forbidden',
  description: 'I vani non hanno identità: un cambio di geometria è un update sulla SALA, '
             + 'quindi struttura e quindi admin. Un operatore non ridisegna le stanze.',
  role: 'edit', fromIdentityFixture: 'update-vani-is-room-update',
  expectedAllowed: false,
  expectedViolations: [forbid('room', 'update')],
});

add({
  name: 'edit-manual-entry-forbidden',
  description: 'Le voci di manuale sono di admin.',
  role: 'edit', fromIdentityFixture: 'update-manual-entry',
  expectedAllowed: false,
  expectedViolations: [forbid('manual', 'rename')],
});

add({
  name: 'edit-settings-forbidden',
  description: 'Le impostazioni sono di admin.',
  role: 'edit', fromIdentityFixture: 'update-settings',
  expectedAllowed: false,
  expectedViolations: [forbid('settings', 'update')],
});

add({
  name: 'edit-no-change-allowed',
  description: 'Documento identico: nessun evento, quindi niente da autorizzare.',
  role: 'edit', fromIdentityFixture: 'no-change',
  expectedAllowed: true, expectedViolations: [],
});

add({
  name: 'admin-everything-allowed',
  description: 'admin può tutto: un campione di ogni tipo di entità ed evento.',
  role: 'admin',
  events: [dev('add'), dev('delete'), rack('rename'), room('move'),
           ev('location', 'update', 'aaaaaaaa-0000-4000-8000-000000000001', 'structure'),
           ev('manual', 'add', 'eeeeeeee-0000-4000-8000-000000000001', 'manuale'),
           ev('settings', 'update', null, 'settings'),
           ev('rack', 'reorder', null, 'structure')],
  expectedAllowed: true, expectedViolations: [],
});

// ---------------------------------------------------------- ruolo ignoto
add({
  name: 'unknown-role-rejected',
  description: 'Un ruolo non riconosciuto non è un permesso vuoto: è un errore. '
             + 'Fallire in chiuso, non in aperto.',
  role: 'superuser', events: [dev('update')],
  expectedAllowed: false,
  expectedViolations: [{ code: 'unknown_role', requiredRole: 'admin' }],
});

// ---------------------------------------------------------- rollback
add({
  name: 'rollback-admin-allowed',
  description: 'Il rollback è di admin.',
  operation: 'rollback', role: 'admin',
  expectedAllowed: true, expectedViolations: [],
});
add({
  name: 'rollback-edit-forbidden',
  description: 'Il rollback sostituisce l\'inventario in blocco: non si può autorizzare per '
             + 'ambito, perché tocca tutto. Resta di admin anche per edit.',
  operation: 'rollback', role: 'edit',
  expectedAllowed: false,
  expectedViolations: [{ code: 'rollback_forbidden', entity: 'inventory',
                         event: 'rollback', requiredRole: 'admin' }],
});
add({
  name: 'rollback-view-forbidden',
  description: 'A maggior ragione per view.',
  operation: 'rollback', role: 'view',
  expectedAllowed: false,
  expectedViolations: [{ code: 'rollback_forbidden', entity: 'inventory',
                         event: 'rollback', requiredRole: 'admin' }],
});

// =====================================================================
mkdirSync(OUT, { recursive: true });
for (const f of readdirSync(OUT)) if (f.endsWith('.json')) rmSync(`${OUT}/${f}`);

const names = new Set();
for (const f of fixtures) {
  if (names.has(f.name)) throw new Error(`fixture duplicata: ${f.name}`);
  names.add(f.name);
  writeFileSync(`${OUT}/${f.name}.json`, JSON.stringify(f, null, 2) + '\n', 'utf8');
}

const allowed = fixtures.filter((f) => f.expectedAllowed).length;
console.log(`${fixtures.length} fixture di policy in ${OUT}/ `
          + `(${allowed} consentite, ${fixtures.length - allowed} respinte)`);
