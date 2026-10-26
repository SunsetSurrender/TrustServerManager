// ============================================================
// xlsx-reader-tests.mjs — il lettore di frontend/xlsx.js su file scritti da Excel
//
// L'import dei dispositivi legge il PRIMO foglio con `leggiXlsx`. Excel, a
// differenza di `creaXlsx`, scrive le celle vuote ma formattate in forma
// auto-chiusa (`<c r="A2" s="3"/>`) e a volte righe intere così (`<row r="9"/>`).
// Il lettore le trattava come celle aperte: il contenuto della cella successiva
// finiva nella colonna precedente e ogni campo della riga scivolava di un posto
// (la `location` dentro `_uid`, l'`ip` dentro `modello`, …).
//
// Il test costruisce l'archivio a mano, con le forme che produce Excel, e
// verifica che ogni valore resti nella sua colonna.
//
// Uso:
//   node tools/xlsx-reader-tests.mjs
// ============================================================
import { crc32, deflateRawSync } from 'node:zlib';
import { creaXlsx, leggiXlsx } from '../frontend/xlsx.js';

let pass = 0;
const failures = [];
const eq = (name, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; return; }
  failures.push(name);
  console.log(`  [FAIL] ${name}\n         → ottenuto ${JSON.stringify(got)}\n         → atteso   ${JSON.stringify(want)}`);
};

// ZIP minimo, entry compresse con deflate come fa Excel (metodo 8).
function zip(files) {
  const parts = [], central = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const nameB = Buffer.from(name), raw = Buffer.from(text), data = deflateRawSync(raw);
    const crc = crc32(raw);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(8, 8);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(data.length, 18); local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameB.length, 26);
    const cd = Buffer.alloc(46);
    cd.writeUInt32LE(0x02014b50, 0); cd.writeUInt16LE(20, 4); cd.writeUInt16LE(20, 6); cd.writeUInt16LE(8, 10);
    cd.writeUInt32LE(crc, 16); cd.writeUInt32LE(data.length, 20); cd.writeUInt32LE(raw.length, 24);
    cd.writeUInt16LE(nameB.length, 28); cd.writeUInt32LE(offset, 42);
    parts.push(local, nameB, data);
    central.push(cd, nameB);
    offset += 30 + nameB.length + data.length;
  }
  const cdBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8); end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(cdBuf.length, 12); end.writeUInt32LE(offset, 16);
  const all = Buffer.concat([...parts, cdBuf, end]);
  return all.buffer.slice(all.byteOffset, all.byteOffset + all.length);
}

const sheet = (rows) => '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + rows + '</sheetData></worksheet>';
const shared = (arr) => '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="' + arr.length + '" uniqueCount="' + arr.length + '">' +
  arr.map(s => '<si><t>' + s + '</t></si>').join('') + '</sst>';
const s = (ref, i, st = '1') => `<c r="${ref}" s="${st}" t="s"><v>${i}</v></c>`;

// --- 1. celle vuote formattate, in testa, in mezzo e in coda ---------------
{
  const sst = ['_uid', 'location', 'sala', 'rack', 'nome', 'modello', 'ip', 'pomezia-g0', 'backend', 'R01', 'srv-01', '10.0.0.1'];
  const xml = sheet(
    '<row r="1" spans="1:7">' + [0, 1, 2, 3, 4, 5, 6].map((i, k) => s('ABCDEFG'[k] + 1, i)).join('') + '</row>' +
    // _uid vuoto (nuovo dispositivo), modello vuoto: entrambi auto-chiusi
    '<row r="2" spans="1:7"><c r="A2" s="2"/>' + s('B2', 7) + s('C2', 8) + s('D2', 9) + s('E2', 10) + '<c r="F2" s="2"/>' + s('G2', 11) + '</row>' +
    // riga vuota ma formattata, auto-chiusa
    '<row r="3" spans="1:7" s="2" customFormat="1"/>' +
    // cella numerica e coda vuota formattata
    '<row r="4" spans="1:7"><c r="A4" s="2"/>' + s('B4', 7) + s('C4', 8) + s('D4', 9) + s('E4', 10) + '<c r="F4"><v>42</v></c><c r="G4" s="2"/></row>');
  const buf = zip({ 'xl/sharedStrings.xml': shared(sst), 'xl/worksheets/sheet1.xml': xml });
  const t = await leggiXlsx(buf);
  eq('intestazioni', t.intestazioni, ['_uid', 'location', 'sala', 'rack', 'nome', 'modello', 'ip']);
  eq('celle vuote auto-chiuse: nessuno scivolamento', t.righe[0], ['', 'pomezia-g0', 'backend', 'R01', 'srv-01', '', '10.0.0.1']);
  eq('riga auto-chiusa saltata, la successiva intatta', t.righe[1], ['', 'pomezia-g0', 'backend', 'R01', 'srv-01', '42', '']);
  eq('numero di righe', t.righe.length, 2);
}

// --- 2. stringhe inline, entità XML ------------------------------------------
{
  const xml = sheet('<row r="1"><c r="A1" t="inlineStr"><is><t>nome</t></is></c><c r="B1" t="inlineStr"><is><t>note</t></is></c></row>' +
    '<row r="2"><c r="A2" t="inlineStr"><is><t>a&amp;b</t></is></c><c r="B2" s="3"/></row>');
  const t = await leggiXlsx(zip({ 'xl/worksheets/sheet1.xml': xml }));
  eq('inline + entità + coda auto-chiusa', t.righe[0], ['a&b', '']);
}

// --- 3. il giro con lo scrittore dell'applicazione resta invariato -----------
{
  const blob = creaXlsx([{ nome: 'Dispositivi', intestazioni: ['location', 'sala', 'u'], righe: [['pomezia-g0', 'backend', '12'], ['', 'x', '']] }]);
  const t = await leggiXlsx(await blob.arrayBuffer());
  eq('giro creaXlsx → leggiXlsx', [t.intestazioni, t.righe], [['location', 'sala', 'u'], [['pomezia-g0', 'backend', '12'], ['', 'x', '']]]);
}

// --- 4. il primo foglio è quello di workbook.xml, non il primo nell'archivio
{
  const wbXml = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
    '<sheet name="Dispositivi" sheetId="1" r:id="rId2"/><sheet name="Istruzioni" sheetId="2" r:id="rId3"/></sheets></workbook>';
  const rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
    '<Relationship Id="rId3" Type="x/worksheet" Target="worksheets/sheet2.xml"/>' +
    '<Relationship Id="rId2" Type="x/worksheet" Target="worksheets/sheet1.xml"/></Relationships>';
  const one = (v) => sheet(`<row r="1"><c r="A1" t="inlineStr"><is><t>${v}</t></is></c></row>`);
  // sheet2 PRIMA di sheet1 nell'archivio, come fa LibreOffice
  const buf = zip({ 'xl/workbook.xml': wbXml, 'xl/_rels/workbook.xml.rels': rels,
                    'xl/worksheets/sheet2.xml': one('istruzioni'), 'xl/worksheets/sheet1.xml': one('dispositivi') });
  eq('primo foglio secondo workbook.xml', (await leggiXlsx(buf)).intestazioni, ['dispositivi']);
  // senza workbook.xml: il numero più basso, non il primo nell'archivio
  const buf2 = zip({ 'xl/worksheets/sheet2.xml': one('istruzioni'), 'xl/worksheets/sheet1.xml': one('dispositivi') });
  eq('ripiego: sheet1 anche se non è il primo', (await leggiXlsx(buf2)).intestazioni, ['dispositivi']);
}

// --- 5. entità: &apos; e numeriche decodificate, e decodificate UNA volta -----
{
  const sst = ["l&apos;host", '&amp;lt;tag&amp;gt;', 'caff&#232;'];
  const xml = sheet('<row r="1">' + s('A1', 0) + s('B1', 1) + s('C1', 2) +
    '<c r="D1" t="inlineStr"><is><t>a&amp;amp;b</t></is></c></row>');
  const t = await leggiXlsx(zip({ 'xl/sharedStrings.xml': shared(sst), 'xl/worksheets/sheet1.xml': xml }));
  eq('entità decodificate una volta', t.intestazioni, ["l'host", '&lt;tag&gt;', 'caffè', 'a&amp;b']);
}

console.log('\n' + '='.repeat(70));
console.log(`controlli passati: ${pass}   falliti: ${failures.length}`);
console.log(failures.length ? 'RISULTATO: il lettore XLSX NON regge i file di Excel' : 'RISULTATO: il lettore XLSX regge le forme scritte da Excel');
process.exit(failures.length ? 1 : 0);
