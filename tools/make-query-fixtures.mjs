// ============================================================
// make-query-fixtures.mjs — genera fixtures/query/*.json
//
// Le fixture sono il CONTRATTO di PARITÀ fra:
//   - l'implementazione ESISTENTE, che è il frontend (handoff/Sala Server v2.dc.html)
//   - le query SQL della fase 2E (backend/app/inventory/queries.py)
//
// ⚠ Perché qui le attese si CALCOLANO, al contrario di make-identity-fixtures.mjs.
//
// Quel generatore scrive le attese a mano di proposito: lì il rischio è che i test
// verifichino l'implementazione contro sé stessa. Qui il rischio è l'opposto e la
// scelta si rovescia. Ciò che va dimostrato è che DUE implementazioni indipendenti
// — il JavaScript che gira oggi nel browser e lo SQL nuovo — danno la stessa
// risposta. Se le attese le scrivessi a mano, dimostrerei che lo SQL corrisponde
// alla mia LETTURA del JavaScript, che è precisamente la cosa di cui non ci si può
// fidare in una migrazione di comportamento.
//
// Quindi gli algoritmi qui sotto sono COPIATI ALLA LETTERA dal frontend, e un
// controllo statico in tools/storage-config-test.py verifica che le righe copiate
// esistano ancora identiche nell'HTML. Se il frontend cambia, il controllo diventa
// rosso e queste fixture vanno rigenerate: è l'unico modo di accorgersi che il
// riferimento semantico si è spostato.
//
// Uso:
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tools/make-query-fixtures.mjs
// ============================================================
import { mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync, existsSync }
  from 'node:fs';

const OUT = 'fixtures/query';

// UUID leggibili, v4 come pretende la validazione dell'identità (§8.4).
let _n = 0;
const U = (prefix) => {
  _n += 1;
  const tail = String(_n).padStart(12, '0');
  return `${prefix.repeat(8).slice(0, 8)}-0000-4000-8000-${tail}`;
};

// ============================================================
// 1. GLI ALGORITMI ESISTENTI, COPIATI ALLA LETTERA
// ============================================================
//
// Ogni blocco porta il riferimento alla sua sede nel frontend. Le righe marcate
// «VERBATIM» sono confrontate carattere per carattere dal controllo statico: non
// vanno riformattate, nemmeno per allinearle meglio.

// --- handoff/Sala Server v2.dc.html, `static ipToNum(ip)` ---
function ipToNum(ip) {
  /* VERBATIM */ const m = String(ip || '').trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  /* VERBATIM */ if (!m) return null;
  /* VERBATIM */ const p = [+m[1], +m[2], +m[3], +m[4]];
  /* VERBATIM */ if (p.some(x => x > 255)) return null;
  /* VERBATIM */ return ((p[0] * 256 + p[1]) * 256 + p[2]) * 256 + p[3];
}

// --- handoff/Sala Server v2.dc.html, `static parseIpQuery(q)` ---
function parseIpQuery(q) {
  q = String(q || '').trim();
  let m = q.match(/^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\/(\d{1,2})$/);
  if (m) {
    /* VERBATIM */ const base = ipToNum(m[1]), bits = +m[2];
    /* VERBATIM */ if (base === null || bits > 32) return null;
    /* VERBATIM */ const size = Math.pow(2, 32 - bits);
    /* VERBATIM */ const start = Math.floor(base / size) * size;
    /* VERBATIM */ return [start, start + size - 1];
  }
  m = q.match(/^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*-\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/);
  if (m) {
    /* VERBATIM */ const a = ipToNum(m[1]), b = ipToNum(m[2]);
    /* VERBATIM */ if (a === null || b === null) return null;
    /* VERBATIM */ return [Math.min(a, b), Math.max(a, b)];
  }
  m = q.match(/^((?:\d{1,3}\.){1,3})\*$/);
  if (m) {
    /* VERBATIM */ const parts = m[1].split('.').filter(Boolean).map(Number);
    /* VERBATIM */ if (parts.some(x => x > 255)) return null;
    /* VERBATIM */ const lo = [...parts], hi = [...parts];
    /* VERBATIM */ while (lo.length < 4) { lo.push(0); hi.push(255); }
    /* VERBATIM */ return [ipToNum(lo.join('.')), ipToNum(hi.join('.'))];
  }
  return null;
}

// --- ricerca globale: il corpo di `render()` intorno a «ricerca globale» ---
//
// Restituisce i risultati NELL'ORDINE del frontend: per ogni rack, prima
// l'eventuale risultato del rack e poi i suoi dispositivi, scendendo l'albero.
function legacySearch(doc, rawQuery) {
  const locations = doc.locations || [];
  const q = String(rawQuery == null ? '' : rawQuery).trim().toLowerCase();
  const results = [];
  const ipRange = q ? parseIpQuery(q) : null;
  const devHit = (d) => {
    if (ipRange) {
      const n = ipToNum(d.ip);
      /* VERBATIM */ return n !== null && n >= ipRange[0] && n <= ipRange[1];
    }
    /* VERBATIM */ return [d.name, d.model, d.ip, d.serial, d.owner].some(v => (v || '').toLowerCase().includes(q));
  };
  if (q) {
    for (const L of locations) for (const R of (L.sale || [])) for (const rk of (R.racks || [])) {
      /* VERBATIM */ if (!ipRange && (rk.id.toLowerCase().includes(q) || (rk.name || '').toLowerCase().includes(q) || (rk.seriali || []).some(sn => String(sn).toLowerCase().includes(q)))) {
        results.push({
          kind: 'rack',
          rack: ctxRack(rk), room: ctxRoom(R), location: ctxLoc(L),
          deviceCount: (rk.devices || []).length,
        });
      }
      for (const d of (rk.devices || [])) {
        if (devHit(d)) {
          results.push({
            kind: 'device',
            device: ctxDev(d),
            rack: ctxRack(rk), room: ctxRoom(R), location: ctxLoc(L),
          });
        }
      }
    }
  }
  return { ipRange, results };
}

// --- capacità: il corpo di `render()` intorno a «capacità globale» ---
//
// ⚠ PRE-CONTROLLO che non fa parte del legacy, e serve a non uccidere il generatore.
//
// `new Array(rk.u + 1).fill(false)` alloca un elemento per unità rack. Con
// `u = 3000000000` — che il backend accetta, perché `3e9` è un intero e lo schema
// congelato non mette un massimo — questo esaurisce la memoria del processo: non
// solleva, MUORE. Non è un'ipotesi, è come ho scoperto il caso: il generatore è
// finito in «JavaScript heap out of memory» sul documento `oversized-integers`.
//
// Quindi si guarda prima, e si SOLLEVA con un messaggio che dice cosa è successo. La
// conseguenza da tenere a mente è che per questi documenti non esiste un
// comportamento legacy da riprodurre: il frontend non sa calcolarli.
const MAX_U_FATTIBILE = 1_000_000;

function guardiaCapacita(doc) {
  for (const L of (doc.locations || [])) for (const R of (L.sale || []))
    for (const rk of (R.racks || [])) {
      const u = rk.u;
      // ⚠ Una STRINGA passa: `new Array('45' + 1)` è `new Array('451')`, cioè un
      // array di UN elemento — non solleva e non alloca niente. Il legacy prosegue e
      // produce numeri coerciti (`tot` diventa la stringa '04545'). Quello È il
      // comportamento attuale, e va registrato, non nascosto dietro un'eccezione mia.
      if (typeof u !== 'string' && !Number.isInteger(u)) {
        throw new Error(`RangeError previsto: rack ${rk.id} ha u=${JSON.stringify(u)}, `
          + `e new Array(u + 1) non ha una lunghezza valida`);
      }
      if (typeof u === 'number' && u + 1 > MAX_U_FATTIBILE) {
        throw new Error(`memoria esaurita: rack ${rk.id} ha u=${u}, e `
          + `new Array(${u + 1}).fill(false) allocherebbe ${u + 1} elementi`);
      }
    }
}

function legacyCapacity(doc) {
  guardiaCapacita(doc);
  const locations = doc.locations || [];
  return locations.map(L => ({
    locationUid: L._uid,
    rooms: (L.sale || []).map(R => {
      let tot = 0, used = 0, bestRack = null, bestFree = 0;
      const byRow = {};
      const racks = [];
      for (const rk of (R.racks || [])) {
        /* VERBATIM */ tot += rk.u;
        /* VERBATIM */ const occ = new Array(rk.u + 1).fill(false);
        /* VERBATIM */ for (const d of rk.devices) { if ((d.stato || 'attivo') === 'dismesso') {} for (let k = d.u; k < d.u + (d.h || 1); k++) if (k <= rk.u) occ[k] = true; }
        /* VERBATIM */ let rkUsed = 0, run = 0, maxRun = 0;
        /* VERBATIM */ for (let k = 1; k <= rk.u; k++) { if (occ[k]) { rkUsed++; run = 0; } else { run++; if (run > maxRun) maxRun = run; } }
        /* VERBATIM */ used += rkUsed;
        /* VERBATIM */ if (maxRun > bestFree) { bestFree = maxRun; bestRack = rk.id; }
        /* VERBATIM */ const rw = rk.row || '—';
        /* VERBATIM */ if (!byRow[rw]) byRow[rw] = { tot: 0, used: 0 };
        /* VERBATIM */ byRow[rw].tot += rk.u; byRow[rw].used += rkUsed;
        racks.push({
          uid: rk._uid, code: rk.id, u: num(rk.u), usedU: rkUsed,
          largestFreeRun: maxRun, deviceCount: (rk.devices || []).length,
          row: rk.row == null ? null : rk.row,
        });
      }
      /* VERBATIM */ const pct = tot ? used / tot : 0;
      return {
        roomUid: R._uid,
        totalU: num(tot), usedU: used, rackCount: (R.racks || []).length,
        occupancyPercent: Math.round(pct * 100),
        bestRackCode: bestRack, bestFreeRun: bestFree,
        // ⚠ La chiave del raggruppamento esce GREZZA, sentinella compresa.
        //
        // Il frontend raggruppa per `rk.row || '—'`, cioè usa la stringa «—» come
        // segnaposto per «nessuna fila». Nel seed di produzione esiste un rack la cui
        // fila È «—» (CS-Q01), quindi la sentinella COLLIDE col dato e i due finiscono
        // nello stesso gruppo. La prima stesura di questo generatore rimappava «—» a
        // `null`, che nascondeva la collisione: il test confrontava una chiave inventata
        // qui con quella dello SQL, e la differenza sembrava un difetto dello SQL.
        rows: Object.entries(byRow).map(([rw, v]) => ({
          row: rw, totalU: num(v.tot), usedU: v.used,
        })),
        racks,
      };
    }),
  }));
}

// --- scadenze: il corpo di `render()` intorno a «vista scadenze dedicata» ---
//
// `nowMs` sostituisce `Date.now()`: una fixture che dipende dall'orologio non è una
// fixture. Vedi la nota sull'equivalenza con la data di calendario del backend nel
// piano (§8.46).
function legacyExpiries(doc, nowMs) {
  const locations = doc.locations || [];
  const entries = [];
  for (const L of locations) for (const R of (L.sale || [])) for (const rk of (R.racks || []))
    for (const d of (rk.devices || [])) {
      /* VERBATIM */ if ((d.stato || 'attivo') === 'dismesso') continue;
      /* VERBATIM */ for (const [kind, val] of [['Garanzia', d.garanzia], ['Supporto', d.supporto]]) {
        /* VERBATIM */ if (!val) continue;
        /* VERBATIM */ const dt = new Date(val);
        /* VERBATIM */ if (isNaN(dt)) continue;
        entries.push({ dt, val, kind, d, L, R, rk });
      }
    }
  /* VERBATIM */ entries.sort((a, b) => a.dt - b.dt);
  const out = [];
  for (const en of entries) {
    const giorni = Math.round((en.dt.getTime() - nowMs) / 86400000);
    /* VERBATIM */ const lv = giorni < 0 ? 2 : (giorni <= 90 ? 1 : 0);
    out.push({
      kind: en.kind.toLowerCase(),
      raw: en.val,
      daysRemaining: giorni,
      level: lv === 2 ? 'expired' : (lv === 1 ? 'warning' : 'future'),
      // ⚠ Il BACKEND interpreta questa data? Vedi `isoStrict`.
      isoStrict: isoStrict(en.val) !== null,
      device: ctxDev(en.d), rack: ctxRack(en.rk),
      room: ctxRoom(en.R), location: ctxLoc(en.L),
    });
  }
  return out;
}

// --- la regola del BACKEND, non del frontend ---
//
// ⚠ Questa è l'unica funzione del file che NON viene dal frontend: è
// `app/notifications/expiry.py::parse_expiry` — `YYYY-MM-DD` esatto, con validità di
// calendario — riscritta in JavaScript per poter MARCARE quali attese del frontend il
// backend è in grado di riprodurre.
//
// Riscrivere una regola è precisamente ciò che questo progetto evita di solito, quindi
// c'è la contromisura: un test Python confronta questa marcatura con `parse_expiry`
// vero su OGNI valore di OGNI corpus. Se le due divergono il test è rosso, e la
// riscrittura non può restare sbagliata in silenzio.
//
// Serve perché `new Date(v)` del frontend accetta molto di più: `2026-2-3`,
// `2027/03/15`, `March 15, 2027`. Senza questa marcatura la parità delle scadenze
// sarebbe rossa su ogni corpus che contiene una data scritta a mano — cioè su
// `rel-dated-devices`, che ne ha due — e la causa non si distinguerebbe da un difetto
// dello SQL.
function isoStrict(v) {
  if (typeof v !== 'string') return null;
  const m = v.trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;
  // Validità di calendario come `datetime.date(y, mo, d)`: se il 29 febbraio di un
  // anno non bisestile «rotola» al primo marzo, i campi non tornano e la data non vale.
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1
      || dt.getUTCDate() !== d) return null;
  return `${m[1]}-${m[2]}-${m[3]}`;
}

// ============================================================
// 2. contesto restituito: identità immutabile + dato di visualizzazione
// ============================================================
//
// §7: ogni risultato si identifica con l'`_uid`, e porta abbastanza contesto da
// localizzarlo senza una seconda ricerca. Mai il documento intero.

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const str = (v) => (typeof v === 'string' ? v : null);

const ctxDev = (d) => ({
  uid: d._uid, code: str(d.id), name: str(d.name), type: str(d.type),
  stato: str(d.stato), u: num(d.u), h: num(d.h),
});
const ctxRack = (rk) => ({ uid: rk._uid, code: str(rk.id), name: str(rk.name) });
const ctxRoom = (R) => ({ uid: R._uid, code: str(R.id), nome: str(R.nome) });
const ctxLoc = (L) => ({ uid: L._uid, code: str(L.id), nome: str(L.nome) });

// ============================================================
// 3. i corpora
// ============================================================

const dev = (over = {}) => ({
  _uid: U('d'), id: over.id || 'srv-x', name: 'srv-x', u: 1, h: 1, ...over,
});
const rack = (over = {}) => ({
  _uid: U('c'), id: over.id || 'R01', name: 'Rack', u: 42,
  x: 0.2, y: 0.2, w: 0.6, h: 1.2, devices: [], ...over,
});
const room = (over = {}) => ({
  _uid: U('b'), id: over.id || 'sala-1', nome: 'Sala 1', w: 10, h: 8,
  vani: [], racks: [], ...over,
});
const loc = (over = {}) => ({
  _uid: U('a'), id: over.id || 'pomezia', nome: 'Pomezia', sale: [], ...over,
});
const document_ = (locations) => ({ schemaVersion: 1, locations });

// --- SEARCH: testo, maiuscole, sottostringhe, duplicati, vuoti, Unicode ---
function corpusSearchText() {
  const d1 = dev({ id: 'srv-01', name: 'SRV-Web-01', model: 'PowerEdge R750',
                   ip: '10.0.2.15', serial: 'ABC123', owner: 'Rossi' });
  const d2 = dev({ id: 'srv-01', name: 'srv-web-02', model: 'poweredge r640',
                   ip: '10.0.2.16', serial: 'abc999', owner: 'ROSSI' });
  const d3 = dev({ id: 'sw-01', name: 'Switch Núñez — città', model: '',
                   ip: '', serial: '', owner: null, type: 'rete' });
  const d4 = dev({ id: 'nas-01', name: 'NAS Ätna', model: 'Synology',
                   ip: '10.0.3.1', serial: 'Ätna-1', owner: 'Bianchi' });
  const rA = rack({ id: 'R01', name: 'Fila A primo', seriali: ['RK-AAA', 'RK-BBB'],
                    devices: [d1, d2] });
  const rB = rack({ id: 'R02', name: 'rossi ha nominato questo rack',
                    seriali: [], devices: [d3, d4] });
  return {
    name: 'search-text',
    note: 'sottostringhe, differenze di maiuscole, id duplicati, campi vuoti, Unicode. '
        + 'Nota: `owner: null` e `model: ""` non devono mai combaciare con una query non vuota.',
    doc: document_([loc({ sale: [room({ racks: [rA, rB] })] })]),
    searches: ['srv', 'SRV', 'Srv-Web', 'poweredge', 'PowerEdge R750', 'rossi',
               'ROSSI', 'R01', 'r01', 'RK-AAA', 'rk-bbb', 'Fila A', 'núñez',
               'NÚÑEZ', 'città', 'ätna', 'Ätna-1', 'abc', 'ABC123', 'zzz', '',
               '   ', 'a'],
  };
}

// --- SEARCH: tutte le forme IP che il frontend accetta ---
function corpusSearchIp() {
  const mk = (id, ip) => dev({ id, name: id, ip });
  const devices = [
    mk('d-10-0-0-1', '10.0.0.1'),
    mk('d-10-0-0-99', '10.0.0.99'),
    mk('d-10-0-0-100', '10.0.0.100'),
    mk('d-10-0-1-1', '10.0.1.1'),
    mk('d-10-0-2-0', '10.0.2.0'),
    mk('d-10-0-2-255', '10.0.2.255'),
    mk('d-10-1-0-0', '10.1.0.0'),
    mk('d-11-0-0-0', '11.0.0.0'),
    mk('d-192', '192.168.1.10'),
    mk('d-spazi', '  10.0.0.7  '),
    mk('d-vuoto', ''),
    mk('d-assente', undefined),
    mk('d-testo', 'DHCP'),
    mk('d-mal', '10.0.0'),
    mk('d-fuori', '10.0.0.999'),
    mk('d-ipv6', '2001:db8::1'),
    mk('d-doppio', '10.0.0.1'),
  ];
  const rA = rack({ id: '10.0.0.1', name: '10.0.0.1', seriali: ['10.0.0.1'],
                    devices });
  // ⚠ Un rack il cui CODICE contiene letteralmente la query di rete.
  //
  // Senza di lui, «in modalita' IP i rack non partecipano» non e' osservabile: una
  // ricerca `10.0.0.0/24` non combacerebbe con nessun codice di rack nemmeno se i rack
  // partecipassero, quindi una mutazione che li fa partecipare non cambia niente. Con
  // questo rack la differenza si vede. L'ha trovata una mutazione sfuggita.
  const rB = rack({ id: '10.0.0.0/24', name: 'armadio 10.0.*',
                    seriali: ['10.0.0.0/24'], devices: [] });
  return {
    name: 'search-ip',
    note: 'forme IP: esatta, CIDR, intervallo, jolly, non interpretabili, IPv6 (che '
        + '`ipToNum` NON riconosce), vuote, con spazi, duplicate. Il rack si chiama '
        + '«10.0.0.1» di proposito: in modalità intervallo IP i rack NON combaciano.',
    doc: document_([loc({ sale: [room({ racks: [rA, rB] })] })]),
    searches: ['10.0.0.1', '10.0.0.0/24', '10.0.0.0/16', '10.0.2.0/24',
               '10.0.0.1-10.0.0.99', '10.0.0.99-10.0.0.1', '10.0.0.1 - 10.0.0.100',
               '10.0.*', '10.0.2.*', '10.*', '11.*', '10.0.0.0/33', '10.0.0.0/0',
               '999.0.0.0/24', '2001:db8::1', 'DHCP', '10.0.0', '10.0.0.7',
               '10.0.0.999', '256.0.*'],
  };
}

// --- CAPACITY: vuoto, parziale, pieno, multi-U, posizioni limite, altezze ---
function corpusCapacity() {
  const vuoto = rack({ id: 'R-vuoto', u: 10, devices: [] });
  const parziale = rack({ id: 'R-parziale', u: 10, devices: [
    dev({ id: 'a', u: 1, h: 1 }), dev({ id: 'b', u: 5, h: 2 }),
  ] });
  const pieno = rack({ id: 'R-pieno', u: 4, devices: [
    dev({ id: 'p1', u: 1, h: 2 }), dev({ id: 'p2', u: 3, h: 2 }),
  ] });
  const sovrapposti = rack({ id: 'R-overlap', u: 10, devices: [
    dev({ id: 'o1', u: 2, h: 4 }), dev({ id: 'o2', u: 3, h: 4 }),
  ] });
  const oltre = rack({ id: 'R-oltre', u: 5, devices: [
    dev({ id: 'x1', u: 4, h: 10 }),
  ] });
  const limiti = rack({ id: 'R-limiti', u: 6, devices: [
    dev({ id: 'z0', u: 0, h: 2 }),
    dev({ id: 'zneg', u: -3, h: 2 }),
    dev({ id: 'h0', u: 4, h: 0 }),
    dev({ id: 'hneg', u: 5, h: -2 }),
  ] });
  const dismessi = rack({ id: 'R-dismessi', u: 8, devices: [
    dev({ id: 's1', u: 1, h: 2, stato: 'dismesso' }),
    dev({ id: 's2', u: 4, h: 1, stato: 'attivo' }),
  ] });
  const alto = rack({ id: 'R-47', u: 47, devices: [dev({ id: 'k1', u: 47, h: 1 })] });
  const basso = rack({ id: 'R-1', u: 1, devices: [dev({ id: 'k2', u: 1, h: 1 })] });
  const fileA = rack({ id: 'R-fila-A', u: 10, row: 'A',
                       devices: [dev({ id: 'f1', u: 1, h: 3 })] });
  const fileB = rack({ id: 'R-fila-B', u: 10, row: 'A',
                       devices: [dev({ id: 'f2', u: 1, h: 1 })] });
  const senzaFila = rack({ id: 'R-senza-fila', u: 10, devices: [] });
  // ⚠ Una sala la cui occupazione cade ESATTAMENTE su mezzo punto percentuale:
  // 1 U su 8 = 12,5%. `Math.round` di JavaScript da' 13, `round()` di Python da' 12
  // (arrotonda al pari). Senza questo caso la differenza fra i due arrotondamenti non
  // e' osservabile da nessun corpus, e una mutazione che li scambia sfugge — come e'
  // successo alla prima passata di mutazioni.
  const mezzoPunto = rack({ id: 'R-12-5', u: 8,
                            devices: [dev({ id: 'mp', u: 1, h: 1 })] });
  // E un secondo mezzo punto, verso il basso: 3 U su 8 = 37,5% -> 38 contro 38
  // (`round(37.5)` di Python da' 38 perche' 38 e' pari). Serve la COPPIA per
  // distinguere «arrotonda per eccesso» da «arrotonda al pari».
  const mezzoPunto2 = rack({ id: 'R-37-5', u: 8, devices: [
    dev({ id: 'mq', u: 1, h: 3 }),
  ] });
  return {
    name: 'capacity',
    note: 'vuoto, parziale, pieno, multi-U, sovrapposti (contano UNA volta), '
        + 'oltre l\'altezza del rack (tagliati), u=0 e negativo, h=0 e negativo, '
        + 'dismessi (che il frontend NON esclude: il blocco è vuoto), altezze 1 e 47, '
        + 'raggruppamento per fila con e senza etichetta, e due sale la cui '
        + 'percentuale cade su mezzo punto (12,5% e 37,5%) per distinguere '
        + "l'arrotondamento di JavaScript da quello di Python.",
    doc: document_([
      loc({ id: 'sito-1', sale: [
        room({ id: 'sala-a', racks: [vuoto, parziale, pieno, sovrapposti] }),
        room({ id: 'sala-b', racks: [oltre, limiti, dismessi] }),
      ] }),
      loc({ id: 'sito-2', sale: [
        room({ id: 'sala-c', racks: [alto, basso] }),
        room({ id: 'sala-d', racks: [fileA, fileB, senzaFila] }),
        room({ id: 'sala-vuota', racks: [] }),
        room({ id: 'sala-12-5', racks: [mezzoPunto] }),
        room({ id: 'sala-37-5', racks: [mezzoPunto2] }),
      ] }),
    ]),
  };
}

// --- EXPIRIES: valide, invalide, assenti, garanzia/supporto, livelli, duplicati ---
function corpusExpiries(refDate) {
  const giorno = (delta) => {
    const t = new Date(refDate + 'T00:00:00Z');
    t.setUTCDate(t.getUTCDate() + delta);
    return t.toISOString().slice(0, 10);
  };
  const devices = [
    dev({ id: 'e-oggi', name: 'oggi', garanzia: giorno(0) }),
    dev({ id: 'e-ieri', name: 'ieri', garanzia: giorno(-1) }),
    dev({ id: 'e-scaduta', name: 'scaduta', garanzia: giorno(-400) }),
    dev({ id: 'e-domani', name: 'domani', supporto: giorno(1) }),
    dev({ id: 'e-90', name: 'novanta', garanzia: giorno(90) }),
    dev({ id: 'e-91', name: 'novantuno', garanzia: giorno(91) }),
    dev({ id: 'e-futura', name: 'futura', supporto: giorno(500) }),
    dev({ id: 'e-entrambe', name: 'entrambe',
          garanzia: giorno(10), supporto: giorno(200) }),
    dev({ id: 'e-attesa', name: 'in attesa', supporto: 'in attesa' }),
    dev({ id: 'e-vuota', name: 'vuota', garanzia: '', supporto: '' }),
    dev({ id: 'e-assente', name: 'assente' }),
    dev({ id: 'e-dismesso', name: 'dismesso', stato: 'dismesso',
          garanzia: giorno(5) }),
    dev({ id: 'e-dismissione', name: 'in dismissione', stato: 'dismissione',
          garanzia: giorno(6) }),
    // Stesso `id` di business, identità diverse: due righe distinte.
    dev({ id: 'e-doppio', name: 'doppio uno', garanzia: giorno(20) }),
    dev({ id: 'e-doppio', name: 'doppio due', garanzia: giorno(20) }),
  ];
  return {
    name: 'expiries',
    note: 'garanzia e supporto, oggi/ieri/domani, soglia 90 e 91, molto scaduta, '
        + 'molto futura, entrambe sullo stesso dispositivo, «in attesa», stringa '
        + 'vuota, campo assente, dismesso (ESCLUSO dalla vista scadenze) e in '
        + 'dismissione (incluso), due dispositivi con lo stesso id di business.',
    refDate,
    doc: document_([loc({ sale: [room({
      racks: [rack({ id: 'R-scad', u: 42, devices }) ] })] })]),
  };
}

// --- EXPIRIES: date che il frontend interpreta e il backend NO ---
function corpusExpiryParsing() {
  const forme = [
    '2027-03-15', '2027-3-15', '2027/03/15', '15/03/2027', 'March 15, 2027',
    '2027-03-15T10:00:00Z', '2027-03', '2027', 'in attesa', 'da definire',
    '  2027-03-15  ', '2027-13-01', '2027-02-30', '0000-00-00', 'domani',
  ];
  const devices = forme.map((v, i) => dev({ id: `p${i}`, name: v, garanzia: v }));
  return {
    name: 'expiry-parsing',
    note: '⚠ Corpus di DIVERGENZA, non di parità. `new Date(v)` del frontend accetta '
        + 'molto più di `parse_expiry` del backend, che pretende `YYYY-MM-DD` esatto. '
        + 'Le attese qui sono quelle del FRONTEND: il test Python le confronta con lo '
        + 'SQL e DOCUMENTA quali forme divergono, invece di scoprirlo in produzione.',
    doc: document_([loc({ sale: [room({
      racks: [rack({ id: 'R-parse', u: 42, devices }) ] })] })]),
  };
}

// ============================================================
// 3-bis. le STRANEZZE: dove il legacy non è ben definito
// ============================================================
//
// Un documento che il backend accetta può essere qualcosa che il frontend non sa
// calcolare. Tre famiglie, tutte trovate facendo girare questo generatore e non
// leggendo il codice:
//
//   `rack.u` non intero      → `new Array(u + 1)` fa RangeError
//   `rack.u` enorme          → alloca e muore per memoria
//   `rack.u` stringa         → non solleva: COERCE. `tot += '45'` concatena, e il
//                              totale della sala diventa la stringa '04545'
//   campo cercato non stringa e VERO → `(v || '').toLowerCase()` fa TypeError
//
// Dove c'è una stranezza non si pretende parità: si registra, e il test Python
// documenta la divergenza invece di scoprirla in produzione. Dove l'elenco è vuoto la
// parità è STRETTA, e un solo campo diverso fa fallire il test.
const CAMPI_CERCATI = ['name', 'model', 'ip', 'serial', 'owner'];

function stranezze(doc) {
  const out = [];
  for (const L of (doc.locations || [])) for (const R of (L.sale || []))
    for (const rk of (R.racks || [])) {
      const u = rk.u;
      if (typeof u === 'string') {
        out.push({ where: 'rack.u', uid: rk._uid, value: u,
                   why: 'stringa: il legacy coerce e i totali della sala diventano '
                      + 'concatenazioni di stringhe' });
      } else if (!Number.isInteger(u)) {
        out.push({ where: 'rack.u', uid: rk._uid, value: u === undefined ? null : u,
                   why: 'non intero: new Array(u + 1) fa RangeError' });
      } else if (u + 1 > MAX_U_FATTIBILE) {
        out.push({ where: 'rack.u', uid: rk._uid, value: u,
                   why: 'enorme: il legacy alloca u+1 elementi ed esaurisce la memoria' });
      }
      if (typeof rk.id !== 'string') {
        out.push({ where: 'rack.id', uid: rk._uid, value: rk.id === undefined ? null : rk.id,
                   why: 'non stringa: rk.id.toLowerCase() fa TypeError nella ricerca' });
      }
      for (const sn of (rk.seriali || [])) {
        if (typeof sn !== 'string') {
          out.push({ where: 'rack.seriali[]', uid: rk._uid, value: sn,
                     why: 'non stringa: il legacy fa String(sn), il modello '
                        + 'relazionale porta tutto l\'array in `extra`' });
        }
      }
      for (const d of (rk.devices || [])) {
        for (const campo of CAMPI_CERCATI) {
          const v = d[campo];
          if (v !== undefined && v !== null && typeof v !== 'string' && v) {
            out.push({ where: `device.${campo}`, uid: d._uid, value: v,
                       why: 'valore VERO non stringa: (v || \'\').toLowerCase() fa '
                          + 'TypeError nella ricerca' });
          }
        }
        // `u` e `h` non interi: la colonna è `integer`, quindi il valore va in `extra`
        // e per lo SQL è NULL, mentre il frontend lo usa così com'è. Con `h: 1.5` il
        // frontend occupa DUE slot (`k < 1 + 1.5`), lo SQL uno (`h` nullo vale 1).
        for (const campo of ['u', 'h']) {
          const v = d[campo];
          if (v !== undefined && v !== null && !Number.isInteger(v)) {
            out.push({ where: `device.${campo}`, uid: d._uid, value: v,
                       why: 'non intero: va in `extra`, quindi per lo SQL è NULL, '
                          + 'mentre il frontend lo usa come numero' });
          }
        }
        // Date non stringa: `garanzia_date` deriva dalla COLONNA, che resta vuota.
        for (const campo of ['garanzia', 'supporto']) {
          const v = d[campo];
          if (v !== undefined && v !== null && typeof v !== 'string') {
            out.push({ where: `device.${campo}`, uid: d._uid, value: v,
                       why: 'non stringa: va in `extra`, quindi non esiste una data '
                          + 'derivata da interrogare' });
          }
        }
      }
    }
  return out;
}

// ============================================================
// 4. generazione
// ============================================================

//: Data di riferimento delle scadenze. Fissa: una fixture che dipende
//: dall'orologio non è una fixture.
const REF_DATE = '2026-08-10';

//: Istante di riferimento = MEZZANOTTE LOCALE a Roma del giorno di riferimento.
//:
//: ⚠ Non è una comodità, è la condizione che rende confrontabili le due
//: implementazioni. Il frontend calcola `Math.round((dt - Date.now())/86400000)`
//: con `dt` = mezzanotte UTC della data; il backend calcola
//: `(scadenza - oggi).days` con `oggi` = data di calendario nel fuso configurato.
//: I due coincidono ESATTAMENTE se l'istante di riferimento è la mezzanotte locale
//: e lo scarto del fuso è inferiore a 12 ore — perché allora
//: `giorni + offset/24` arrotonda a `giorni`. A metà giornata i due possono
//: differire di uno, e questo è documentato nel piano (§8.46) invece di essere
//: nascosto scegliendo un istante comodo.
//:
//: 2026-08-10 a Roma è ora legale: UTC+2, quindi la mezzanotte locale è
//: 2026-08-09T22:00:00Z.
const REF_NOW_MS = Date.parse('2026-08-09T22:00:00Z');

function build() {
  const corpora = [
    corpusSearchText(),
    corpusSearchIp(),
    corpusCapacity(),
    corpusExpiries(REF_DATE),
    corpusExpiryParsing(),
  ];

  // Il seed di PRODUZIONE, che è il corpus che conta più di tutti: è l'inventario
  // vero, con i suoi campi vuoti e i suoi nomi come li scrive chi lavora al CED.
  //
  // ⚠ Il seed NON contiene nessuna data di garanzia o supporto — zero valori non
  // vuoti. Per questo esiste `fixtures/expiry`, ed è per questo che la parità delle
  // scadenze si prova là e non qui: misurarla sul seed darebbe zero righe da
  // entrambe le parti, cioè un test verde che non ha confrontato niente.
  if (existsSync('fixtures/seed.json')) {
    corpora.push({
      name: 'seed',
      note: 'Il seed di produzione: 3 siti, 6 sale, 102 rack, 86 dispositivi. '
          + 'Nessuna data di scadenza (il corpus delle scadenze è un altro).',
      doc: JSON.parse(readFileSync('fixtures/seed.json', 'utf8')),
      searches: ['srv', 'R01', 'r0', '10.0', '10.0.0.0/16', '10.*', 'dell', 'hp',
                 'switch', 'nas', 'ups', 'zzz-inesistente'],
    });
  }

  // I documenti costruiti dai generatori PYTHON, riversati in JSON dal loro stesso
  // CLI. Sono le forme limite già usate dalle fasi 2B-2D: valori falsi espliciti,
  // campi ignoti, tipi «sbagliati», seriali di tipi misti, geometrie complicate.
  //
  // Passano da un file JSON e non da una riscrittura in JavaScript per la ragione di
  // sempre: due definizioni della stessa fixture divergono, e divergono sui casi
  // limite, che sono esattamente quelli per cui la fixture esiste.
  for (const [file, base] of [['fixtures/query/_documents.json', 'rel'],
                              ['fixtures/query/_expiry.json', 'expiry-fixture']]) {
    if (!existsSync(file)) {
      console.log(`⚠ ${file} assente: rigenerare con lo script Python (vedi il piano)`);
      continue;
    }
    const payload = JSON.parse(readFileSync(file, 'utf8'));
    const docs = base === 'expiry-fixture' ? { [base]: payload } : payload;
    for (const [nome, doc] of Object.entries(docs)) {
      if (!doc || !Array.isArray(doc.locations)) continue;
      // ⚠ ESCLUSO, e non per pigrizia: questo documento esiste per contenere `-0.0` e
      // `1e+20`, e nessuno dei due sopravvive a un giro attraverso JavaScript —
      // `JSON.stringify(-0)` è `"0"`. Il corpus che arriverebbe ai test sarebbe la
      // variante NORMALIZZATA, cioè un documento diverso con il nome di quello ostile:
      // coprirebbe meno di quanto dichiara, che è peggio di non coprire. Quel documento
      // è già provato dove conta, in `test_dual_write_pg.py` e nella suite dei numeri.
      if (nome === 'jsonb-hostile-numbers') continue;
      corpora.push({
        name: base === 'expiry-fixture' ? base : `rel-${nome}`,
        note: base === 'expiry-fixture'
          ? 'L\'inventario delle scadenze costruito da fixtures/expiry/build.py, '
            + 'cioè quello su cui il worker delle notifiche è già provato.'
          : `Documento «${nome}» da fixtures/relational/build.py.`,
        doc,
        searches: ['srv', 'R0', 'r0', '10.0', '10.0.*', 'a', '', 'Ätna', 'zzz'],
      });
    }
  }

  mkdirSync(OUT, { recursive: true });

  // --- passo 1: si riversano i documenti GREZZI e si esce ---
  //
  // Il passo 2 (Python) li canonicalizza. Vedi tools/canonicalise-query-docs.py per
  // il perché: il frontend non vede mai un documento non canonico, quindi misurare la
  // parità su uno sarebbe misurarla su un caso che in produzione non esiste.
  if (process.argv.includes('--emit-docs')) {
    const grezzi = {};
    for (const c of corpora) grezzi[c.name] = c.doc;
    writeFileSync(`${OUT}/_raw.json`,
                  JSON.stringify(grezzi, null, 2) + '\n', 'utf8');
    console.log(`${Object.keys(grezzi).length} documenti grezzi in ${OUT}/_raw.json`);
    console.log('ora: python tools/canonicalise-query-docs.py');
    return;
  }

  // --- passo 3: si usano i documenti CANONICI ---
  const canonicalPath = `${OUT}/_canonical.json`;
  if (!existsSync(canonicalPath)) {
    console.error(`${canonicalPath} assente. La catena è:\n`
      + '  1. node   tools/make-query-fixtures.mjs --emit-docs\n'
      + '  2. python tools/canonicalise-query-docs.py\n'
      + '  3. node   tools/make-query-fixtures.mjs');
    process.exitCode = 1;
    return;
  }
  const canonici = JSON.parse(readFileSync(canonicalPath, 'utf8'));
  for (const c of corpora) {
    if (!(c.name in canonici)) {
      console.error(`documento canonico assente per «${c.name}»: rieseguire il passo 1`);
      process.exitCode = 1;
      return;
    }
    c.doc = canonici[c.name];
  }

  // ⚠ I file che cominciano con `_` sono gli INGRESSI (i riversamenti dei generatori
  // Python e i canonici) e non si cancellano. La prima stesura di questo ciclo li
  // portava via insieme alle uscite, e la generazione successiva perdeva in silenzio
  // metà dei corpora — «0 documenti relazionali» sarebbe passato per un elenco vuoto.
  for (const f of readdirSync(OUT)) {
    if (f.endsWith('.json') && !f.startsWith('_')) rmSync(`${OUT}/${f}`);
  }

  // ⚠ Il legacy PUÒ SOLLEVARE, e quando succede non c'è una parità da misurare.
  //
  // `new Array(rk.u + 1)` fa `RangeError` per un'altezza non intera, negativa o
  // enorme; `(v || '').toLowerCase()` fa `TypeError` per un valore vero non stringa.
  // Sono documenti che il backend ACCETTA (§8.16 vieta altro) e che il frontend non
  // sa digerire. Registrarlo è l'unica cosa onesta: `null` come attesa vorrebbe dire
  // «il legacy restituisce niente», che è falso — il legacy si rompe.
  const prova = (fn) => {
    try { return { value: fn(), threw: null }; }
    catch (e) { return { value: null, threw: `${e.name}: ${e.message}` }; }
  };

  for (const c of corpora) {
    const quirks = stranezze(c.doc);
    const cap = prova(() => legacyCapacity(c.doc));
    const exp = prova(() => legacyExpiries(c.doc, REF_NOW_MS));
    const ricerche = (c.searches || []).map(q => {
      const r = prova(() => legacySearch(c.doc, q));
      return r.threw
        ? { q, legacyThrows: r.threw }
        : { q, ipRange: r.value.ipRange, results: r.value.results };
    });
    const out = {
      name: c.name,
      note: c.note,
      doc: c.doc,
      refDate: c.refDate || REF_DATE,
      refNowMs: REF_NOW_MS,
      warningDays: 90,
      quirks,
      search: ricerche,
      capacity: cap.value,
      capacityThrows: cap.threw,
      expiries: exp.value,
      expiriesThrows: exp.threw,
    };
    writeFileSync(`${OUT}/${c.name}.json`,
                  JSON.stringify(out, null, 2) + '\n', 'utf8');
    const rotte = ricerche.filter(r => r.legacyThrows).length;
    console.log(`${c.name}: ${ricerche.length} ricerche`
              + (rotte ? ` (${rotte} in cui il legacy solleva)` : '')
              + `, capacità ${cap.threw ? 'SOLLEVA' : cap.value.reduce((a, L) => a + L.rooms.length, 0) + ' sale'}`
              + `, ${exp.threw ? 'scadenze SOLLEVANO' : exp.value.length + ' scadenze'}`);
  }
  console.log(`\nscritte in ${OUT}/`);
}

build();
