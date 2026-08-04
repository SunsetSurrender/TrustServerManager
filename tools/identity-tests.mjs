// ============================================================
// identity-tests.mjs — test della logica di identità in isolamento
//
// Copre i casi richiesti: create, edit, rename, move, aggiornamento e
// aggiunta da foglio, export/import JSON, undo/redo, _uid duplicati,
// malformati, mancanti e sostituzione di identità.
//
// La logica sta in handoff/identity.js proprio per essere verificabile qui,
// senza browser e senza HTTP. Il cablaggio nell'interfaccia è verificato
// separatamente da tools/identity-ui-test.py.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/identity-tests.mjs
// ============================================================
import {
  isUid, newUid, walkEntities, validateDocument, validateAgainstBase,
  preserveIdentity, matchDeviceForImport,
} from '../handoff/identity.js';

let pass = 0;
const failures = [];

const ok = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${name}`); }
  else { failures.push(`${name}${detail ? ' → ' + detail : ''}`); console.log(`  [FAIL] ${name}${detail ? '\n         → ' + detail : ''}`); }
};

const codes = (errs) => errs.map((e) => e.code).sort();
const clone = (o) => JSON.parse(JSON.stringify(o));

// ---- documento di base minimo ma completo -------------------------------
const U = {
  loc: '11111111-1111-4111-8111-111111111111',
  room: '22222222-2222-4222-8222-222222222222',
  rackA: '33333333-3333-4333-8333-333333333333',
  rackB: '44444444-4444-4444-8444-444444444444',
  devA: '55555555-5555-4555-8555-555555555555',
  devB: '66666666-6666-4666-8666-666666666666',
  man: '77777777-7777-4777-8777-777777777777',
};

const base = () => ({
  versione: 3,
  manuale: [{ _uid: U.man, id: 'man-1', titolo: 'Voce', blocchi: [] }],
  locations: [{
    _uid: U.loc, id: 'sito', nome: 'Sito',
    sale: [{
      _uid: U.room, id: 'sala', nome: 'Sala', w: 5, h: 4,
      vani: [{ x: 0, y: 0, w: 5, h: 4 }],       // senza _uid: value object
      racks: [
        { _uid: U.rackA, id: 'R01', name: 'Rack R01', u: 45, x: 0, y: 0, w: 0.6, h: 0.8,
          devices: [
            { _uid: U.devA, id: 'srv-01', name: 'srv-01', type: 'server', u: 10, h: 1 },
            { _uid: U.devB, id: 'srv-02', name: 'srv-02', type: 'server', u: 20, h: 1 },
          ] },
        { _uid: U.rackB, id: 'R02', name: 'Rack R02', u: 45, x: 1, y: 0, w: 0.6, h: 0.8, devices: [] },
      ],
    }],
  }],
});

const devIn = (doc, rackId, uid) =>
  doc.locations[0].sale[0].racks.find((r) => r.id === rackId).devices.find((d) => d._uid === uid);

console.log('\n— fondamentali —');
ok('il documento di base è valido', validateDocument(base()).length === 0,
   JSON.stringify(validateDocument(base())));
ok('walkEntities conta le entità giuste', walkEntities(base()).length === 7,
   `trovate ${walkEntities(base()).length}, attese 7 (1 loc + 1 sala + 2 rack + 2 dev + 1 manuale)`);
ok('i vani non sono entità', !walkEntities(base()).some((e) => e.kind === 'vano'));
ok('newUid produce un UUID valido', isUid(newUid()));

console.log('\n— create —');
{
  const next = clone(base());
  const nuovo = preserveIdentity(null, { id: 'srv-03', name: 'srv-03', type: 'server', u: 30, h: 1 });
  next.locations[0].sale[0].racks[0].devices.push(nuovo);
  ok('create: _uid generato e conforme', isUid(nuovo._uid));
  ok('create: add autentico accettato', validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}

console.log('\n— edit —');
{
  const next = clone(base());
  const d = devIn(next, 'R01', U.devA);
  d.custom_futuro = 'metadato che il client non conosce';   // deve sopravvivere
  const patched = preserveIdentity(d, { model: 'Dell R760' });
  ok('edit: _uid conservato', patched._uid === U.devA, patched._uid);
  ok('edit: campi sconosciuti conservati', patched.custom_futuro === 'metadato che il client non conosce');
  ok('edit: campo aggiornato', patched.model === 'Dell R760');
  Object.assign(d, patched);
  ok('edit: accettato dalla validazione', validateAgainstBase(base(), next).length === 0);
}

console.log('\n— rename —');
{
  const next = clone(base());
  const d = devIn(next, 'R01', U.devA);
  Object.assign(d, preserveIdentity(d, { id: 'srv-01', name: 'srv-01-rinominato' }));
  ok('rename: _uid conservato', devIn(next, 'R01', U.devA).name === 'srv-01-rinominato');
  ok('rename: accettato', validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));

  // rinomina anche del codice di business
  const n2 = clone(base());
  const d2 = devIn(n2, 'R01', U.devA);
  Object.assign(d2, preserveIdentity(d2, { id: 'srv-99', name: 'srv-99' }));
  ok('rename del codice: accettato con _uid invariato', validateAgainstBase(base(), n2).length === 0,
     JSON.stringify(validateAgainstBase(base(), n2)));
}

console.log('\n— move —');
{
  const next = clone(base());
  const racks = next.locations[0].sale[0].racks;
  const d = racks[0].devices.find((x) => x._uid === U.devA);
  racks[0].devices = racks[0].devices.filter((x) => x._uid !== U.devA);
  racks[1].devices.push(d);
  ok('move fra rack: _uid conservato', !!devIn(next, 'R02', U.devA));
  ok('move fra rack: accettato', validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}

console.log('\n— sostituzione di identità (deve essere RIFIUTATA) —');
{
  // stesso codice, _uid nuovo, vecchio svanito: è il caso che distrugge lo storico
  const next = clone(base());
  const racks = next.locations[0].sale[0].racks;
  racks[0].devices = racks[0].devices.filter((x) => x._uid !== U.devA);
  racks[0].devices.push({ _uid: newUid(), id: 'srv-01', name: 'srv-01', type: 'server', u: 10, h: 1 });
  ok('delete+add con stesso codice → identity_replacement',
     codes(validateAgainstBase(base(), next)).includes('identity_replacement'),
     JSON.stringify(validateAgainstBase(base(), next)));
}
{
  // _uid nuovo su un codice ancora in uso
  const next = clone(base());
  next.locations[0].sale[0].racks[0].devices.push(
    { _uid: newUid(), id: 'srv-01', name: 'srv-01', type: 'server', u: 40, h: 1 });
  ok('riuso del codice di business → business_key_reuse',
     codes(validateAgainstBase(base(), next)).includes('business_key_reuse'),
     JSON.stringify(validateAgainstBase(base(), next)));
}
{
  // spostamento mascherato: codice uguale, altro rack, vecchio uid svanito
  const next = clone(base());
  const racks = next.locations[0].sale[0].racks;
  racks[0].devices = racks[0].devices.filter((x) => x._uid !== U.devA);
  racks[1].devices.push({ _uid: newUid(), id: 'srv-01', name: 'srv-01', type: 'server', u: 10, h: 1 });
  ok('move mascherato da delete+add → identity_replacement',
     codes(validateAgainstBase(base(), next)).includes('identity_replacement'),
     JSON.stringify(validateAgainstBase(base(), next)));
}
{
  // rack: rinomina che sostituisce l'identità
  const next = clone(base());
  next.locations[0].sale[0].racks[0] =
    { _uid: newUid(), id: 'R01', name: 'Rack R01', u: 45, x: 0, y: 0, w: 0.6, h: 0.8, devices: [] };
  ok('rack: _uid sostituito → rifiutato',
     validateAgainstBase(base(), next).length > 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}
{
  // cancellazione autentica: nessun rimpiazzo, deve passare
  const next = clone(base());
  const racks = next.locations[0].sale[0].racks;
  racks[0].devices = racks[0].devices.filter((x) => x._uid !== U.devA);
  ok('delete autentico accettato', validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}
{
  // delete di A + add di B non correlato: due eventi legittimi
  const next = clone(base());
  const racks = next.locations[0].sale[0].racks;
  racks[0].devices = racks[0].devices.filter((x) => x._uid !== U.devA);
  racks[0].devices.push({ _uid: newUid(), id: 'srv-nuovo', name: 'srv-nuovo', type: 'server', u: 10, h: 1 });
  ok('delete + add non correlati accettati', validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}

console.log('\n— _uid duplicati, malformati, mancanti —');
{
  const next = clone(base());
  next.locations[0].sale[0].racks[1].devices.push(
    { _uid: U.devA, id: 'clone', name: 'clone', type: 'server', u: 5, h: 1 });
  ok('duplicate_uid rilevato', codes(validateDocument(next)).includes('duplicate_uid'),
     JSON.stringify(validateDocument(next)));
}
{
  const next = clone(base());
  devIn(next, 'R01', U.devA)._uid = 'non-un-uuid';
  ok('malformed_uid rilevato', codes(validateDocument(next)).includes('malformed_uid'));
}
{
  const next = clone(base());
  delete devIn(next, 'R01', U.devA)._uid;
  ok('missing_uid rilevato', codes(validateDocument(next)).includes('missing_uid'));
}
{
  const legacy = clone(base());
  for (const L of legacy.locations) { delete L._uid;
    for (const R of L.sale) { delete R._uid;
      for (const K of R.racks) { delete K._uid; for (const d of K.devices) delete d._uid; } } }
  for (const m of legacy.manuale) delete m._uid;
  const errs = validateDocument(legacy);
  ok('backup legacy senza _uid: rifiutato in blocco',
     errs.length === 7 && errs.every((e) => e.code === 'missing_uid'),
     `${errs.length} errori: ${JSON.stringify(codes(errs))}`);
}
{
  const next = clone(base());
  // UUID v1 al posto di v4: la forma non è quella attesa
  devIn(next, 'R01', U.devA)._uid = '11111111-1111-1111-1111-111111111111';
  ok('UUID non-v4 rifiutato', codes(validateDocument(next)).includes('malformed_uid'));
}

console.log('\n— import da foglio —');
{
  const doc = base();
  const m = matchDeviceForImport(doc, { _uid: U.devA, nome: 'nome-cambiato' });
  ok('foglio: match per _uid anche se il nome è cambiato',
     m.match && m.match.uid === U.devA && !m.ambiguous, JSON.stringify(m.reason));
}
{
  const doc = base();
  const m = matchDeviceForImport(doc, { nome: 'srv-01' });
  ok('foglio: match per nome se univoco', m.match && m.match.uid === U.devA, m.reason);
}
{
  // stesso nome in due rack: senza _uid la corrispondenza è ambigua
  const doc = base();
  doc.locations[0].sale[0].racks[1].devices.push(
    { _uid: newUid(), id: 'srv-01-bis', name: 'srv-01', type: 'server', u: 5, h: 1 });
  const m = matchDeviceForImport(doc, { nome: 'srv-01' });
  ok('foglio: nome duplicato → ambiguo, non indovinato', m.ambiguous && !m.match, m.reason);
}
{
  const doc = base();
  const m = matchDeviceForImport(doc, { nome: 'mai-visto' });
  ok('foglio: nome sconosciuto → nuovo', !m.match && !m.ambiguous, m.reason);
}
{
  const doc = base();
  const m = matchDeviceForImport(doc, { _uid: 'non-un-uuid', nome: 'srv-01' });
  ok('foglio: _uid malformato → rifiutato, non ricade sul nome', m.ambiguous && !m.match, m.reason);
}
{
  const doc = base();
  const m = matchDeviceForImport(doc, { _uid: newUid(), nome: 'srv-01' });
  ok('foglio: _uid inesistente → rifiutato', m.ambiguous && !m.match, m.reason);
}
{
  // aggiornamento da foglio: l'oggetto derivato conserva identità e campi extra
  const doc = base();
  const d = devIn(doc, 'R01', U.devA);
  d.campo_extra = 'x';
  const updated = preserveIdentity(d, { name: 'srv-01', model: 'nuovo modello', u: 12, h: 1 });
  ok('foglio: aggiornamento conserva _uid e campi extra',
     updated._uid === U.devA && updated.campo_extra === 'x' && updated.model === 'nuovo modello');
}
{
  // aggiunta da foglio
  const next = clone(base());
  const nuovo = preserveIdentity(null, { id: 'srv-foglio', name: 'srv-foglio', type: 'server', u: 35, h: 1 });
  next.locations[0].sale[0].racks[1].devices.push(nuovo);
  ok('foglio: aggiunta accettata con _uid nuovo',
     isUid(nuovo._uid) && validateAgainstBase(base(), next).length === 0,
     JSON.stringify(validateAgainstBase(base(), next)));
}

console.log('\n— export / import JSON —');
{
  const doc = base();
  const round = JSON.parse(JSON.stringify(doc));     // export → import
  ok('JSON round-trip: valido', validateDocument(round).length === 0);
  ok('JSON round-trip: nessun cambio di identità', validateAgainstBase(doc, round).length === 0);
  const uidsBefore = walkEntities(doc).map((e) => e.uid).sort();
  const uidsAfter = walkEntities(round).map((e) => e.uid).sort();
  ok('JSON round-trip: gli _uid sono identici',
     JSON.stringify(uidsBefore) === JSON.stringify(uidsAfter));
}

console.log('\n— undo / redo —');
{
  // lo stack di undo tiene copie profonde: l'identità deve attraversarle intatta
  const v0 = base();
  const v1 = clone(v0);
  Object.assign(devIn(v1, 'R01', U.devA), preserveIdentity(devIn(v1, 'R01', U.devA), { model: 'M2' }));
  const v2 = clone(v1);
  Object.assign(devIn(v2, 'R01', U.devB), preserveIdentity(devIn(v2, 'R01', U.devB), { model: 'M3' }));

  const undoStack = [clone(v0), clone(v1)];
  const undone = undoStack[undoStack.length - 1];              // undo → v1
  ok('undo: identità intatte', validateAgainstBase(v2, undone).length === 0,
     JSON.stringify(validateAgainstBase(v2, undone)));
  const redone = clone(v2);                                     // redo → v2
  ok('redo: identità intatte', validateAgainstBase(undone, redone).length === 0);
  ok('undo/redo: gli _uid non cambiano mai',
     JSON.stringify(walkEntities(v0).map((e) => e.uid)) ===
     JSON.stringify(walkEntities(redone).map((e) => e.uid)));
}

console.log('\n— voci di manuale (entità identificate) —');
{
  const next = clone(base());
  const voce = next.manuale[0];
  const patched = preserveIdentity(voce, { titolo: 'Titolo cambiato' });
  ok('manuale: _uid conservato in modifica', patched._uid === U.man);
  next.manuale[0] = patched;
  ok('manuale: modifica accettata', validateAgainstBase(base(), next).length === 0);

  const n2 = clone(base());
  n2.manuale[0] = { _uid: newUid(), id: 'man-1', titolo: 'Voce', blocchi: [] };
  ok('manuale: sostituzione di identità rifiutata',
     validateAgainstBase(base(), n2).length > 0,
     JSON.stringify(validateAgainstBase(base(), n2)));
}

console.log('\n' + '='.repeat(70));
console.log(`${pass} test passati, ${failures.length} falliti`);
if (failures.length) {
  console.log('\nFalliti:');
  for (const f of failures) console.log('  - ' + f);
}
console.log('='.repeat(70));
process.exit(failures.length ? 1 : 0);
