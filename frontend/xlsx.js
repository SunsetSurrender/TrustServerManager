// ============================================================
// xlsx.js — lettura/scrittura Excel senza dipendenze esterne
// Scrittura: ZIP con entry "stored" (nessuna compressione) + CRC32
// Lettura:   unzip stored/deflate via DecompressionStream('deflate-raw')
// ============================================================

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) { let c = i; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[i] = c >>> 0; }
  return t;
})();
const crc32 = (buf) => {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = CRC[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
};
const enc = new TextEncoder();
const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const colName = (n) => { let s = ''; n++; while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = (n - r - 1) / 26; } return s; };

function zip(files) {
  const parts = [], central = [];
  let offset = 0;
  for (const { name, data } of files) {
    const nameB = enc.encode(name);
    const crc = crc32(data);
    const local = new Uint8Array(30 + nameB.length);
    const dv = new DataView(local.buffer);
    dv.setUint32(0, 0x04034b50, true); dv.setUint16(4, 20, true); dv.setUint16(6, 0, true);
    dv.setUint16(8, 0, true); dv.setUint16(10, 0, true); dv.setUint16(12, 0, true);
    dv.setUint32(14, crc, true); dv.setUint32(18, data.length, true); dv.setUint32(22, data.length, true);
    dv.setUint16(26, nameB.length, true); dv.setUint16(28, 0, true);
    local.set(nameB, 30);
    parts.push(local, data);
    const cd = new Uint8Array(46 + nameB.length);
    const cv = new DataView(cd.buffer);
    cv.setUint32(0, 0x02014b50, true); cv.setUint16(4, 20, true); cv.setUint16(6, 20, true);
    cv.setUint32(16, crc, true); cv.setUint32(20, data.length, true); cv.setUint32(24, data.length, true);
    cv.setUint16(28, nameB.length, true); cv.setUint32(42, offset, true);
    cd.set(nameB, 46);
    central.push(cd);
    offset += local.length + data.length;
  }
  const cdSize = central.reduce((a, b) => a + b.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, files.length, true); ev.setUint16(10, files.length, true);
  ev.setUint32(12, cdSize, true); ev.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
}

async function unzip(buffer) {
  const u8 = new Uint8Array(buffer);
  const dv = new DataView(u8.buffer);
  const out = {};
  // scorro le entry dal central directory (più affidabile)
  let eocd = u8.length - 22;
  while (eocd >= 0 && dv.getUint32(eocd, true) !== 0x06054b50) eocd--;
  if (eocd < 0) throw new Error('archivio non valido');
  const n = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const dec = new TextDecoder();
  for (let i = 0; i < n; i++) {
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const nameLen = dv.getUint16(p + 28, true);
    const extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    const lho = dv.getUint32(p + 42, true);
    const name = dec.decode(u8.subarray(p + 46, p + 46 + nameLen));
    const lNameLen = dv.getUint16(lho + 26, true);
    const lExtraLen = dv.getUint16(lho + 28, true);
    const start = lho + 30 + lNameLen + lExtraLen;
    const raw = u8.subarray(start, start + csize);
    if (method === 0) out[name] = dec.decode(raw);
    else {
      const ds = new DecompressionStream('deflate-raw');
      const blob = await new Response(new Blob([raw]).stream().pipeThrough(ds)).arrayBuffer();
      out[name] = dec.decode(new Uint8Array(blob));
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

// ---- Scrittura: un foglio per ogni { nome, intestazioni, righe, note? } ----
export function creaXlsx(fogli) {
  const files = [];
  const sheetXml = (f) => {
    const cols = f.intestazioni.map((h, i) => {
      const w = Math.min(46, Math.max(11, String(h).length + 4, ...f.righe.slice(0, 200).map(r => String(r[i] == null ? '' : r[i]).length + 2)));
      return '<col min="' + (i + 1) + '" max="' + (i + 1) + '" width="' + w + '" customWidth="1"/>';
    }).join('');
    const row = (vals, rIdx, style) => '<row r="' + rIdx + '"' + (style ? ' s="' + style + '" customFormat="1"' : '') + '>' +
      vals.map((v, i) => {
        const ref = colName(i) + rIdx;
        const num = v !== '' && v != null && !isNaN(v) && typeof v !== 'boolean' && String(v).trim() !== '' && /^-?\d+([.,]\d+)?$/.test(String(v));
        if (num) return '<c r="' + ref + '"' + (style ? ' s="' + style + '"' : '') + '><v>' + String(v).replace(',', '.') + '</v></c>';
        return '<c r="' + ref + '" t="inlineStr"' + (style ? ' s="' + style + '"' : '') + '><is><t xml:space="preserve">' + esc(v) + '</t></is></c>';
      }).join('') + '</row>';
    const lastCol = colName(f.intestazioni.length - 1);
    const body = [row(f.intestazioni, 1, 1)].concat(f.righe.map((r, i) => row(r, i + 2, 0))).join('');
    return '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<sheetPr><outlinePr/></sheetPr>' +
      '<dimension ref="A1:' + lastCol + (f.righe.length + 1) + '"/>' +
      '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' +
      '<sheetFormatPr defaultRowHeight="15"/><cols>' + cols + '</cols>' +
      '<sheetData>' + body + '</sheetData>' +
      '<autoFilter ref="A1:' + lastCol + (f.righe.length + 1) + '"/></worksheet>';
  };

  files.push({ name: '[Content_Types].xml', data: enc.encode('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + fogli.map((f, i) => '<Override PartName="/xl/worksheets/sheet' + (i + 1) + '.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>').join('') + '</Types>') });
  files.push({ name: '_rels/.rels', data: enc.encode('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>') });
  files.push({ name: 'xl/workbook.xml', data: enc.encode('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + fogli.map((f, i) => '<sheet name="' + esc(f.nome).slice(0, 31) + '" sheetId="' + (i + 1) + '" r:id="rId' + (i + 1) + '"/>').join('') + '</sheets></workbook>') });
  files.push({ name: 'xl/_rels/workbook.xml.rels', data: enc.encode('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + fogli.map((f, i) => '<Relationship Id="rId' + (i + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet' + (i + 1) + '.xml"/>').join('') + '<Relationship Id="rId' + (fogli.length + 1) + '" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>') });
  files.push({ name: 'xl/styles.xml', data: enc.encode('<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F3864"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border/><border><bottom style="thin"><color rgb="FF999999"/></bottom></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>') });
  fogli.forEach((f, i) => files.push({ name: 'xl/worksheets/sheet' + (i + 1) + '.xml', data: enc.encode(sheetXml(f)) }));
  return zip(files);
}

// ---- Lettura: restituisce { intestazioni, righe } del primo foglio ----
export async function leggiXlsx(buffer) {
  const parts = await unzip(buffer);
  const shared = [];
  const ss = parts['xl/sharedStrings.xml'];
  if (ss) {
    const items = ss.split(/<si[ >]/).slice(1);
    for (const it of items) {
      const texts = [...it.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(m => m[1]);
      shared.push(texts.join('').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
    }
  }
  const sheetName = Object.keys(parts).find(k => /^xl\/worksheets\/sheet\d+\.xml$/.test(k));
  if (!sheetName) throw new Error('nessun foglio trovato');
  const xml = parts[sheetName];
  const rows = [];
  for (const rm of xml.matchAll(/<row[^>]*>([\s\S]*?)<\/row>/g)) {
    const cells = [];
    for (const cm of rm[1].matchAll(/<c r="([A-Z]+)\d+"([^>]*)>([\s\S]*?)<\/c>/g)) {
      let col = 0;
      for (const ch of cm[1]) col = col * 26 + (ch.charCodeAt(0) - 64);
      col--;
      const attrs = cm[2] || '', inner = cm[3] || '';
      let val = '';
      if (/t="s"/.test(attrs)) {
        const iv = (inner.match(/<v>([\s\S]*?)<\/v>/) || [])[1];
        val = shared[parseInt(iv, 10)] || '';
      } else if (/t="inlineStr"/.test(attrs)) {
        val = [...inner.matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(m => m[1]).join('');
      } else {
        val = (inner.match(/<v>([\s\S]*?)<\/v>/) || [])[1] || '';
      }
      cells[col] = String(val).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
    }
    rows.push(Array.from(cells, x => x == null ? '' : x));
  }
  const intestazioni = (rows.shift() || []).map(x => String(x).trim());
  return { intestazioni, righe: rows.filter(r => r.some(c => String(c).trim())) };
}
