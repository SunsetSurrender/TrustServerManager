// ============================================================
// domain.js — il modello semantico UNICO dell'inventario (fase 2G)
//
// Gemello di backend/app/domain.py. Le due implementazioni non si copiano a vicenda:
// rispondono entrambe a fixtures/domain/*.json, che è il CONTRATTO — dati, non codice,
// e scritti da una decisione di prodotto invece che ricavati da un'implementazione.
//
// Fino alla fase 2E la semantica del prodotto era il comportamento misurato del
// prototipo, e questo file è dove quel comportamento smette di essere il contratto.
// Il conto di ciò che divergeva (BACKEND-PLAN.md §8.48): tre definizioni di «U
// occupate», due interpreti di data, due elenchi di campi cercabili, due idee di
// «dismesso».
//
//   stato e presenza   ciclo di vita operativo e presenza FISICA, separati
//   capacità           slot U distinti occupati, una definizione sola
//   percentuale        arrotondamento HALF-UP deterministico
//   fila               identità del gruppo, distinta dall'etichetta mostrata
//   indirizzi          una grammatica sola: IP esatti, CIDR, intervalli, jolly
//   scadenze           un interprete di date solo, `YYYY-MM-DD`
//   idoneità           chi genera un avviso e chi no
//   etichette          nome → codice → «(senza nome)», mai `undefined`
//
// NON ha dipendenze e non tocca il DOM: gira identico nel browser e in node, ed è
// così che tools/domain-contract-tests.mjs lo esercita.
//
// Riferimento: BACKEND-PLAN.md §8.50.
// ============================================================

// ---------------------------------------------------------------- 1. vocabolari

export const DEVICE_TYPES = ['server', 'rete', 'storage', 'firewall', 'alimentazione', 'altro'];

/** Ciclo di vita OPERATIVO: dice se un apparato è in servizio, non dove sta. */
export const DEVICE_STATES = ['attivo', 'manutenzione', 'dismissione', 'dismesso'];

/**
 * Presenza FISICA: dice se l'apparato occupa ancora il suo slot nel rack.
 *
 * ⚠ È la separazione introdotta dalla fase 2G, ed esiste perché le due domande hanno
 * risposte indipendenti: «dismesso» significa fuori servizio, e un apparato fuori
 * servizio può stare fisicamente nel rack per mesi, occupando unità che nessuno può
 * assegnare a qualcos'altro.
 */
export const DEVICE_PRESENCES = ['presente', 'rimosso'];

export const DEFAULT_TYPE = 'altro';
export const DEFAULT_STATO = 'attivo';
export const DEFAULT_PRESENZA = 'presente';
export const DEFAULT_H = 1;

/** Il valore che NON occupa spazio. Scritto al negativo: un terzo valore futuro
 *  («in transito») deve occupare per difetto, non sparire dal conteggio. */
export const PRESENZA_ABSENT = 'rimosso';

/** Stati che non generano più promemoria di rinnovo. `dismissione` resta idoneo:
 *  significa che la decisione non è ancora conclusa. */
export const NOTIFY_INELIGIBLE_STATES = ['dismesso'];

/** Etichetta finale. Mai `undefined`, mai `null`: sono valori dell'implementazione. */
export const NO_NAME = '(senza nome)';

/** Come si MOSTRA una fila non impostata. È un'etichetta, non un'identità. */
export const ROW_UNSET_LABEL = '—';

const get = (obj, key) => (obj === null || obj === undefined ? undefined : obj[key]);

/**
 * `(value || default)` per i campi a vocabolario, con una regola sola in tre
 * linguaggi. `''` vale il default (è la falsità di JavaScript, e `stato: ""` ha
 * sempre significato «attivo»); un valore non stringa si restituisce in forma di
 * testo, così la validazione lo può segnalare invece di vederlo sparire nel default.
 */
function falsyString(value, dflt) {
  if (value === null || value === undefined) return dflt;
  if (typeof value === 'string') return value === '' ? dflt : value;
  if (typeof value === 'boolean') return dflt;
  if (typeof value === 'number') return numberText(value);
  return String(value);
}

export function statoOf(device) { return falsyString(get(device, 'stato'), DEFAULT_STATO); }

/**
 * Presenza fisica. `presente` se assente o vuota.
 *
 * ⚠ Non si deduce dallo stato. Un `dismesso` senza `presenza` è `dismesso + presente`:
 * l'inventario di prima della 2G non registra le rimozioni, quindi l'unica cosa che si
 * sa di quelle macchine è che nessuno ha detto che sono state portate via. Dedurre
 * `rimosso` da `dismesso` libererebbe d'un colpo unità che in sala sono occupate.
 */
export function presenzaOf(device) { return falsyString(get(device, 'presenza'), DEFAULT_PRESENZA); }

export function tipoOf(device) { return falsyString(get(device, 'type'), DEFAULT_TYPE); }

/** Occupa unità fisiche del rack? Solo la presenza decide: un apparato in
 *  manutenzione che sta nel rack occupa lo spazio di un apparato che sta nel rack. */
export function occupiesSpace(device) { return presenzaOf(device) !== PRESENZA_ABSENT; }

/** Può generare NUOVI avvisi di scadenza? Solo lo stato operativo decide: la
 *  presenza fisica non c'entra, una garanzia scade anche a magazzino. */
export function notifies(device) { return NOTIFY_INELIGIBLE_STATES.indexOf(statoOf(device)) < 0; }

// ------------------------------------------------------------------ 2. capacità

/** Interi soltanto: `true` non è 1, `'3'` non è 3, `3.5` non è uno slot. */
function asInt(v) {
  return (typeof v === 'number' && Number.isInteger(v)) ? v : null;
}

/**
 * Intervallo `[lo, hi]` di slot occupati da un dispositivo, oppure `null`.
 *
 * Le cinque regole fisiche, in un posto solo:
 *   - `h` assente o `0` vale 1 (è il `d.h || 1` di sempre);
 *   - `h` NEGATIVO non occupa niente: `-3` da U10 sarebbe un intervallo rovesciato,
 *     e inventargli un verso vorrebbe dire decidere al posto di chi ha digitato male;
 *   - slot iniziale `<= 0` sta fuori dal rack: i rack si contano da 1;
 *   - la sporgenza oltre la cima si TAGLIA: un 4U a U44 di un rack da 45 occupa due
 *     unità, perché sono due quelle che esistono;
 *   - `u` o `h` non interi non occupano niente.
 */
export function slotSpan(u, h, rackU) {
  const height = asInt(rackU);
  const start = asInt(u);
  if (height === null || height < 1 || start === null) return null;
  let units = asInt(h);
  if (units === null || units === 0) units = DEFAULT_H;
  const lo = Math.max(start, 1);
  const hi = Math.min(start + units - 1, height);
  return lo > hi ? null : [lo, hi];
}

function spansOf(rackU, devices) {
  const out = [];
  for (const d of devices || []) {
    if (!occupiesSpace(d)) continue;
    const span = slotSpan(get(d, 'u'), get(d, 'h'), rackU);
    if (span) out.push(span);
  }
  out.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
  return out;
}

/**
 * L'insieme degli slot occupati. Insieme, non somma: è tutta la differenza.
 *
 * ⚠ `SUM(h)` sbaglia in tre modi, tutti reali: due dispositivi sovrapposti contano
 * due volte lo stesso slot; uno che sporge conta unità che non esistono; uno
 * `rimosso` conta comunque. L'insieme risponde alla domanda fisica — quali unità di
 * questo rack non sono assegnabili a un apparato nuovo.
 *
 * ⚠ Materializza uno slot per unità: da usare su rack di altezza plausibile. Il
 * conteggio per la vista Capacità passa da `rackCapacity`, che lavora sugli estremi e
 * non teme il rack da tre miliardi di unità del corpus `oversized-integers`.
 */
export function occupiedSlots(rackU, devices) {
  const slots = new Set();
  for (const [lo, hi] of spansOf(rackU, devices)) {
    for (let k = lo; k <= hi; k++) slots.add(k);
  }
  return slots;
}

/**
 * Capacità di un rack: `{ totalU, usedU, freeU, largestFreeRun }`.
 *
 * Costa quanto i DISPOSITIVI, non quanto l'altezza del rack. Enumerare gli slot
 * sarebbe la traduzione ovvia e sarebbe un guasto: nel browser esaurisce la memoria
 * della scheda, in una richiesta HTTP produce tre miliardi di righe.
 */
export function rackCapacity(rackU, devices) {
  const height = asInt(rackU);
  if (height === null || height < 1) {
    return { totalU: Math.max(height || 0, 0), usedU: 0, freeU: 0, largestFreeRun: 0 };
  }

  // Fusione degli intervalli: qui le sovrapposizioni smettono di contare due volte.
  // Si fondono anche gli ADIACENTI (`lo <= hiPrec + 1`), così fra due isole rimaste
  // distinte c'è sempre almeno uno slot libero — ed è ciò che rende il calcolo dei
  // buchi qui sotto una sottrazione e non una ricerca.
  const islands = [];
  for (const [lo, hi] of spansOf(height, devices)) {
    const last = islands.length ? islands[islands.length - 1] : null;
    if (last && lo <= last[1] + 1) last[1] = Math.max(last[1], hi);
    else islands.push([lo, hi]);
  }

  let used = 0;
  for (const [lo, hi] of islands) used += hi - lo + 1;

  let largest = 0;
  let cursor = 1;
  for (const [lo, hi] of islands) {
    if (lo - cursor > largest) largest = lo - cursor;
    cursor = hi + 1;
  }
  if (height + 1 - cursor > largest) largest = height + 1 - cursor;

  return {
    totalU: height,
    usedU: used,
    freeU: Math.max(0, height - used),
    largestFreeRun: largest,
  };
}

/**
 * Percentuale intera di occupazione, arrotondata HALF-UP.
 *
 * ⚠ Aritmetica INTERA, e non `Math.round(used / total * 100)`. Tre linguaggi, tre
 * risposte diverse sulla metà esatta:
 *
 *     JavaScript   Math.round(0.5) = 1     (metà verso l'alto)
 *     Python       round(0.5)      = 0     (metà al pari, «del banchiere»)
 *     PostgreSQL   round(0.5)      = 1     (metà lontano da zero)
 *
 * Un rack da 8 U con 1 U occupata è al 12,5%: il frontend mostrava 13, Python avrebbe
 * detto 12. Nessuno dei due è sbagliato in sé; averli entrambi lo è.
 *
 *     floor(used * 100 / total + 1/2)  ==  (used * 200 + total) / (total * 2)
 *
 * La forma a destra non contiene divisioni in virgola mobile, quindi non contiene
 * nemmeno il loro arrotondamento: le tre implementazioni danno lo stesso intero per
 * costruzione, non per fortuna.
 */
export function percent(used, total) {
  const u = asInt(used);
  const t = asInt(total);
  if (u === null || t === null || t <= 0 || u <= 0) return 0;
  return Math.floor((u * 200 + t) / (t * 2));
}

// ----------------------------------------------------------------- 3. file (row)

/**
 * Il gruppo «fila» di un rack: `{ assigned, value, key, label }`.
 *
 * ⚠ `key` NON è `label`. Il prototipo raggruppava per `rk.row || '—'`: una SENTINELLA
 * che collide col dato, perché nel seed di produzione esiste un rack la cui fila è
 * letteralmente «—» (CS-Q01). Quel rack finiva nel gruppo «senza fila» insieme a
 * tutti quelli che non hanno una fila, e il totale di unità libere di quella fila era
 * la somma di due cose diverse.
 *
 * `key` contiene un byte NUL, che nessun valore di documento può contenere (§8.31) —
 * la stessa tecnica dei separatori di chiave in identity.js. `label` resta «—» per
 * una fila non impostata: l'interfaccia non cambia aspetto, cambia soltanto ciò che
 * considera lo stesso gruppo.
 */
export function rowGroup(rack) {
  const raw = (typeof rack === 'string' || rack === null || rack === undefined)
    ? rack : get(rack, 'row');
  let value = null;
  if (typeof raw === 'string') value = raw === '' ? null : raw;
  else if (typeof raw === 'number') value = numberText(raw);
  else if (raw !== null && raw !== undefined && typeof raw !== 'boolean') value = String(raw);
  if (value === null) {
    return { assigned: false, value: null, key: '\u0000none', label: ROW_UNSET_LABEL };
  }
  return { assigned: true, value, key: '\u0000row\u0000' + value, label: value };
}

/** Ordine dei gruppi: prima le file dichiarate, poi «senza fila». Il gruppo senza
 *  fila va per ULTIMO: è il residuo, non una fila che si chiama «—». */
export function compareRowGroups(a, b) {
  if (a.assigned !== b.assigned) return a.assigned ? -1 : 1;
  if (!a.assigned) return 0;
  return a.value < b.value ? -1 : (a.value > b.value ? 1 : 0);
}

// -------------------------------------------------------------------- 4. scadenze

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * `YYYY-MM-DD` → `{ y, m, d }`, qualunque altra cosa → `null`.
 *
 * ⚠ **Senza `new Date`.** È il punto di §6, e non è pedanteria: `new Date` accetta
 * sette forme che il backend rifiuta, e una di queste le fa cambiare significato.
 *
 *     2027-3-15              mese e giorno a una cifra
 *     2027/03/15             barre
 *     March 15, 2027         nome del mese
 *     2027-03-15T10:00:00Z   istante, non data di business
 *     2027-03                anno e mese
 *     2027                   anno
 *     2027-02-30             ROLLOVER: V8 la fa scorrere al 2 marzo
 *
 * L'ultima è la ragione per cui `new Date` non può essere una validazione: trasforma
 * in silenzio una data inesistente in una che esiste, e chi gestisce il contratto
 * scoprirebbe la differenza il 2 marzo.
 *
 * Gli spazi intorno si tollerano — un valore incollato da un foglio di calcolo ne
 * porta spesso — ma niente di più.
 *
 * ⚠ Il valore GREZZO non si riscrive mai: `supporto = "March 15, 2027"` resta
 * nell'inventario com'è, e si limita a non essere una scadenza riconosciuta.
 */
export function parseExpiry(value) {
  if (typeof value !== 'string') return null;
  const m = ISO_DATE.exec(value.trim());
  if (!m) return null;
  const y = +m[1], mo = +m[2], d = +m[3];
  // ⚠ Anno da 0001 a 9999. Lo zero non è un anno del calendario gregoriano e
  // `datetime.date` in Python non lo rappresenta: accettarlo qui avrebbe fatto
  // interpretare `0000-01-01` al frontend e rifiutare al backend. Trovato dal
  // confronto fra le due implementazioni, non da una rilettura.
  if (y < 1) return null;
  if (mo < 1 || mo > 12 || d < 1) return null;
  if (d > daysInMonth(y, mo)) return null;
  return { y, m: mo, d };
}

/** `true` se l'anno è bisestile, con la regola gregoriana completa. */
export function isLeapYear(y) {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}

export function daysInMonth(y, m) {
  const lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (m === 2 && isLeapYear(y)) return 29;
  return lengths[m - 1];
}

/**
 * Giorni dal 1970-01-01 per una data di calendario. Intero, esatto, senza `Date`.
 *
 * ⚠ Serve perché `Math.round((dt - Date.now()) / 86400000)` non è un conteggio di
 * giorni: dipende dall'ora del giorno, e nella notte del cambio dell'ora una
 * differenza di 23 o 25 ore si arrotonda a 1 giorno per caso, non per costruzione. Il
 * backend calcola `(scadenza - oggi).days` fra due date di calendario; questo lo fa
 * dare la stessa risposta.
 *
 * Algoritmo di Howard Hinnant (`days_from_civil`), valido su tutto il calendario
 * gregoriano proiettato.
 */
export function daysFromCivil(y, m, d) {
  const yy = m <= 2 ? y - 1 : y;
  const era = Math.floor((yy >= 0 ? yy : yy - 399) / 400);
  const yoe = yy - era * 400;                                        // [0, 399]
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** Giorni rimanenti fra due date di calendario `{y,m,d}`. Intero, mai frazionario. */
export function daysBetween(target, today) {
  return daysFromCivil(target.y, target.m, target.d)
       - daysFromCivil(today.y, today.m, today.d);
}

/** La data di calendario di oggi nel fuso del BROWSER. Unico punto che legge
 *  l'orologio, e non fa parsing: `Date` qui serve a sapere che giorno è. */
export function todayLocal(now) {
  const dt = now || new Date();
  return { y: dt.getFullYear(), m: dt.getMonth() + 1, d: dt.getDate() };
}

/** `expired` / `warning` / `future` per la vista Scadenze. È il livello ISPETTIVO:
 *  la vista mostra tutto, perché la domanda è «cosa posso guardare». */
export function expiryLevel(daysRemaining, warningDays) {
  if (daysRemaining < 0) return 'expired';
  if (daysRemaining <= warningDays) return 'warning';
  return 'future';
}

/**
 * La regola del worker: `0 <= giorni <= almeno una soglia`.
 *
 * Non `giorni === N`: pretendere il giorno esatto significherebbe che una macchina
 * spenta il giorno del promemoria lo perde per sempre. Gli SCADUTI restano esclusi —
 * un avviso su una scadenza già passata si ripeterebbe ogni giorno per sempre, ed è
 * un prodotto diverso.
 */
export function notificationDue(daysRemaining, warningDays) {
  const days = (warningDays || []).filter((n) => typeof n === 'number');
  if (!days.length) return false;
  return daysRemaining >= 0 && daysRemaining <= Math.max(...days);
}

// ------------------------------------------------------------------ 5. indirizzi

const OCTET = '\\d{1,3}';
const IPV4_TEXT = OCTET + '\\.' + OCTET + '\\.' + OCTET + '\\.' + OCTET;
const RE_IPV4 = new RegExp('^' + IPV4_TEXT + '$');
const RE_V4_RANGE = new RegExp('^(' + IPV4_TEXT + ')\\s*-\\s*(' + IPV4_TEXT + ')$');
const RE_V4_WILDCARD = new RegExp('^((?:' + OCTET + '\\.){1,3})\\*$');
const RE_CIDR = /^(\S+)\/(\d{1,3})$/;
const RE_IPV6_SHAPE = /^[0-9A-Fa-f:.]+$/;
const RE_HEX_GROUP = /^[0-9A-Fa-f]{1,4}$/;

const V4_WIDTH = 32n;
const V6_WIDTH = 128n;

function parseIpv4Number(text) {
  if (!RE_IPV4.test(text)) return null;
  const parts = text.split('.').map(Number);
  if (parts.some((p) => p > 255)) return null;
  return { parts, value: BigInt(((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3]) };
}

/**
 * IPv6 testuale → BigInt, o `null`. Scritto a mano perché deve dare la STESSA
 * risposta di `ipaddress.IPv6Address` in Python, e le fixture lo verificano su un
 * corpus di forme ostili: `::` singolo, doppio, gruppi a 5 cifre, IPv4 in coda,
 * IPv4 in posizione sbagliata, identificatori di zona.
 *
 * ⚠ Nessun identificatore di zona (`fe80::1%eth0`): un indirizzo con lo scope è
 * valido su un'interfaccia e privo di significato in un inventario, dove serve a dire
 * quale macchina è quale.
 */
function parseIpv6Number(text) {
  if (!RE_IPV6_SHAPE.test(text)) return null;
  if (text.indexOf(':') < 0) return null;

  const halves = text.split('::');
  if (halves.length > 2) return null;
  const compressed = halves.length === 2;

  const head = halves[0] === '' ? [] : halves[0].split(':');
  const tail = (!compressed || halves[1] === '') ? [] : halves[1].split(':');
  if (head.some((p) => p === '') || tail.some((p) => p === '')) return null;

  // L'IPv4 puntato è ammesso SOLO come ULTIMO pezzo dell'indirizzo intero.
  //
  // ⚠ Quindi `compressed ? tail : head`, e NON «la coda se c'è, altrimenti la testa»:
  // in `1.2.3.4::` l'IPv4 sta nella testa, ma dopo di lui viene `::`, quindi non è
  // l'ultimo pezzo e la forma non è valida. La prima stesura lo accettava e Python lo
  // rifiutava — l'ha trovato il confronto differenziale fra le due implementazioni,
  // non una rilettura.
  const last = compressed ? tail : head;
  let v4 = null;
  if (last.length && last[last.length - 1].indexOf('.') >= 0) {
    const piece = last[last.length - 1];
    // ⚠ Zeri iniziali NON ammessi qui, al contrario di un IPv4 nudo.
    //
    // `010.0.0.1` si accetta come indirizzo IPv4 perché `ipToNum` lo accettava e
    // togliere una forma che funzionava sarebbe una regressione. `::ffff:010.0.0.1`
    // invece è una forma NUOVA di questa fase: non c'è niente da conservare, e
    // `ipaddress` in Python la rifiuta. Accettarla di qua e no di là era l'unica
    // divergenza che il fuzzing differenziale ha trovato su 5126 forme — dieci casi,
    // tutti di questa specie.
    if (!/^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(piece)) return null;
    v4 = parseIpv4Number(piece);
    if (v4 === null) return null;
    last.pop();
  }
  const groupTexts = head.concat(tail);
  if (groupTexts.some((p) => p.indexOf('.') >= 0)) return null;

  const groups = [];
  for (const p of groupTexts) {
    if (!RE_HEX_GROUP.test(p)) return null;
    groups.push(parseInt(p, 16));
  }

  const v4groups = v4 === null ? 0 : 2;
  const declared = groups.length + v4groups;
  if (compressed) {
    // `::` sta per almeno un gruppo a zero, quindi al massimo sette dichiarati.
    if (declared > 7) return null;
  } else if (declared !== 8) {
    return null;
  }

  let value = 0n;
  const headCount = head.length;
  const zeros = 8 - declared;
  const ordered = [];
  for (let i = 0; i < headCount; i++) ordered.push(groups[i]);
  if (compressed) for (let i = 0; i < zeros; i++) ordered.push(0);
  for (let i = headCount; i < groups.length; i++) ordered.push(groups[i]);
  for (const g of ordered) value = (value << 16n) | BigInt(g);
  if (v4 !== null) value = (value << 32n) | v4.value;
  return value;
}

/**
 * Forma canonica di un IPv6, IDENTICA a `str(ipaddress.IPv6Address(n))` in Python.
 *
 * Le tre regole di `ipaddress`, che non sono ovvie e che il confronto differenziale ha
 * dovuto insegnarmi una per una:
 *
 *   1. gruppi minuscoli senza zeri iniziali, dal più significativo al meno. La prima
 *      stesura li produceva ROVESCIATI (`::1` diventava `1::`), e i valori numerici
 *      combaciavano comunque: il difetto si vedeva solo confrontando il testo;
 *   2. la sequenza di zeri PIÙ LUNGA compressa in `::`, la prima a parità di
 *      lunghezza, e nessuna compressione di un gruppo solo (`0:2:3:4:5:6:7:8` resta
 *      per esteso);
 *   3. **solo** gli indirizzi IPv4-MAPPED (`::ffff:a.b.c.d`) si scrivono col quartetto
 *      puntato. `::1.2.3.4` — IPv4-compatible, senza `ffff` — diventa `::102:304`, ed
 *      è la forma che Python produce.
 */
function ipv6Text(value) {
  const groups = [];
  for (let i = 7; i >= 0; i--) groups.push(Number((value >> BigInt(i * 16)) & 0xffffn));

  // Regola 3: IPv4-mapped, e nient'altro.
  const mapped = groups[0] === 0 && groups[1] === 0 && groups[2] === 0
              && groups[3] === 0 && groups[4] === 0 && groups[5] === 0xffff;
  if (mapped) return '::ffff:' + ipv4Text(value & 0xffffffffn);

  let bestStart = -1, bestLen = 0, curStart = -1, curLen = 0;
  for (let i = 0; i < 8; i++) {
    if (groups[i] === 0) {
      if (curStart < 0) { curStart = i; curLen = 0; }
      curLen++;
      if (curLen > bestLen) { bestLen = curLen; bestStart = curStart; }
    } else { curStart = -1; curLen = 0; }
  }
  const hex = groups.map((g) => g.toString(16));
  if (bestLen < 2) return hex.join(':');
  const before = hex.slice(0, bestStart).join(':');
  const after = hex.slice(bestStart + bestLen).join(':');
  return before + '::' + after;
}

function ipv4Text(value) {
  const n = BigInt(value);
  return [(n >> 24n) & 255n, (n >> 16n) & 255n, (n >> 8n) & 255n, n & 255n]
    .map((b) => String(Number(b))).join('.');
}

/**
 * Testo → `{ family, value, text }`, o `null`. IPv4 e IPv6, niente prefissi.
 *
 * ⚠ È l'UNICO punto in cui il prodotto decide se un testo è un indirizzo. Il backend
 * ne deriva la colonna `ip_addr` della proiezione, quindi PostgreSQL non interpreta
 * mai il testo dell'utente: riceve solo la forma canonica prodotta qui. È il motivo
 * per cui `inet` è diventato utilizzabile senza portarsi dietro la sua grammatica,
 * che accetta `10.1` come `10.0.0.1` e `10.0.0.0/8` come indirizzo.
 *
 * Gli zeri iniziali negli ottetti IPv4 si accettano (`010.0.0.1` è `10.0.0.1`):
 * `ipToNum` li accettava, e togliere una forma che funzionava è una regressione.
 */
export function parseAddress(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text) return null;

  const v4 = parseIpv4Number(text);
  if (v4 !== null) {
    return { family: 4, value: v4.value, text: v4.parts.join('.') };
  }
  if (text.indexOf(':') >= 0) {
    const n = parseIpv6Number(text);
    if (n === null) return null;
    return { family: 6, value: n, text: ipv6Text(n) };
  }
  return null;
}

function addressAt(family, value) {
  return {
    family,
    value,
    text: family === 4 ? ipv4Text(value) : ipv6Text(value),
  };
}

/**
 * Query → `{ family, lo, hi, kind }`, oppure `null` («non è un indirizzo, cercalo
 * come testo»).
 *
 * Le forme, e solo queste:
 *
 *     10.0.0.1                esatto IPv4
 *     2001:db8::1             esatto IPv6
 *     10.0.2.0/24             CIDR IPv4
 *     2001:db8::/32           CIDR IPv6
 *     10.0.0.1 - 10.0.0.99    intervallo IPv4
 *     10.0.*                  jolly IPv4
 *
 * ⚠ NON esistono intervalli né jolly IPv6, e non si inventano: `2001:db8::*` dovrebbe
 * voler dire «un gruppo qualsiasi» o «il resto dell'indirizzo»? Ogni risposta è una
 * grammatica nuova che nessuno ha chiesto, e sbagliarla vorrebbe dire mostrare una
 * rete diversa da quella cercata.
 *
 * ⚠ `null` significa «cercalo come testo», NON «nessun risultato». La differenza si
 * vede su `10.0.0`: non è un indirizzo, e come testo trova `10.0.0.1`, `10.0.0.2`…
 * che è ciò che uno si aspetta scrivendo mezzo indirizzo.
 */
export function parseAddressQuery(raw) {
  if (typeof raw !== 'string') return null;
  const q = raw.trim();
  if (!q) return null;

  let m = RE_CIDR.exec(q);
  if (m) {
    const base = parseAddress(m[1]);
    const bits = BigInt(m[2]);
    if (!base) return null;
    const width = base.family === 4 ? V4_WIDTH : V6_WIDTH;
    if (bits > width) return null;
    const size = 1n << (width - bits);
    const start = (base.value / size) * size;
    return {
      family: base.family, kind: 'cidr',
      lo: addressAt(base.family, start),
      hi: addressAt(base.family, start + size - 1n),
    };
  }

  m = RE_V4_RANGE.exec(q);
  if (m) {
    const a = parseAddress(m[1]);
    const b = parseAddress(m[2]);
    if (!a || !b) return null;
    const flip = a.value > b.value;
    return { family: 4, kind: 'range', lo: flip ? b : a, hi: flip ? a : b };
  }

  m = RE_V4_WILDCARD.exec(q);
  if (m) {
    const parts = m[1].split('.').filter(Boolean).map(Number);
    if (parts.some((p) => p > 255)) return null;
    const lo = parts.slice(), hi = parts.slice();
    while (lo.length < 4) { lo.push(0); hi.push(255); }
    const low = parseAddress(lo.join('.'));
    const high = parseAddress(hi.join('.'));
    if (!low || !high) return null;
    return { family: 4, kind: 'wildcard', lo: low, hi: high };
  }

  const exact = parseAddress(q);
  if (exact) return { family: exact.family, kind: 'exact', lo: exact, hi: exact };

  return null;
}

/**
 * L'indirizzo di un dispositivo cade in questo intervallo?
 *
 * ⚠ La FAMIGLIA deve combaciare. Un jolly `10.0.*` non trova `::a00:1` anche se quel
 * valore numerico coincide: sono due spazi di indirizzamento, e confonderli farebbe
 * comparire in una ricerca di rete IPv4 macchine che non ci sono. È anche
 * l'ordinamento che PostgreSQL dà al tipo `inet`, quindi la regola è la stessa nei tre
 * posti in cui viene applicata.
 */
export function addressMatches(ipText, query) {
  if (!query) return false;
  const addr = parseAddress(ipText);
  if (!addr || addr.family !== query.family) return false;
  return query.lo.value <= addr.value && addr.value <= query.hi.value;
}

// ------------------------------------------------------------- 6. ricerca testuale

/**
 * Campi del DISPOSITIVO su cui cerca la barra globale.
 *
 * ⚠ `note` NON c'è, per decisione di questa fase: sono testo libero e lungo, e
 * includerle renderebbe qualunque parola comune un risultato di massa.
 *
 * `tipo`, `stato` e `presenza` si cercano nel VALORE MEMORIZZATO (`server`, `attivo`,
 * `rimosso`), non nell'etichetta tradotta: le etichette vivono nell'interfaccia e
 * cambiano con la lingua, i valori sono il dato. E passano dal loro default: un
 * dispositivo senza `stato` è `attivo` e va trovato cercando «attivo».
 */
export const DEVICE_SEARCH_FIELDS = ['id', 'name', 'model', 'ip', 'serial', 'owner',
                                     'tipo', 'stato', 'presenza'];

/** Campi del RACK. `seriali` è un elenco: combacia se combacia un elemento. */
export const RACK_SEARCH_FIELDS = ['id', 'name', 'seriali'];

/** Sottostringa LETTERALE, senza distinzione di maiuscole. `%` e `_` sono caratteri
 *  normali: chi li scrive in una casella di ricerca non ha scritto un modello. */
export function contains(haystack, needle) {
  if (haystack === null || haystack === undefined || typeof haystack === 'boolean') return false;
  return String(haystack).toLowerCase().indexOf(needle) >= 0;
}

export function deviceSearchValue(device, field) {
  if (field === 'tipo') return tipoOf(device);
  if (field === 'stato') return statoOf(device);
  if (field === 'presenza') return presenzaOf(device);
  return get(device, field);
}

export function deviceMatches(device, needle) {
  if (!needle) return false;
  return DEVICE_SEARCH_FIELDS.some((f) => contains(deviceSearchValue(device, f), needle));
}

/**
 * Il rack combacia con la query testuale?
 *
 * ⚠ I rack NON partecipano alla modalità indirizzo: un rack che si chiama «10.0.0.1»
 * non è una macchina con quell'indirizzo, e restituirlo a chi cerca un host sarebbe un
 * falso positivo di un genere particolarmente fastidioso — sembra una risposta.
 */
export function rackMatches(rack, needle) {
  if (!needle) return false;
  if (contains(get(rack, 'id'), needle) || contains(get(rack, 'name'), needle)) return true;
  const seriali = get(rack, 'seriali');
  if (Array.isArray(seriali)) return seriali.some((s) => contains(s, needle));
  return false;
}

// ---------------------------------------------------------------- 7. etichette

/** `String(n)` con la sola differenza che conta: `42.0` è «42» in JavaScript e
 *  «42.0» con `str()` in Python. Si scrive la forma di JavaScript, perché è quella
 *  che l'utente ha visto nell'interfaccia da sempre. */
function numberText(n) {
  if (!Number.isFinite(n)) return String(n);
  return String(n);
}

/**
 * Questo valore può essere un'etichetta per una persona?
 *
 * Regola esplicita, perché le due implementazioni non hanno la stessa idea di «vuoto»
 * e la differenza si vede sui dati importati da un foglio di calcolo:
 *
 *   - una STRINGA non vuota sì, anche di soli spazi: è ciò che l'utente ha scritto;
 *   - un NUMERO diverso da zero sì, in forma decimale (`name: 42` → «42»);
 *   - zero, `false`, `null`, elenchi e oggetti NO. `String([])` è la stringa vuota in
 *     JavaScript e `str([])` è «[]» in Python: due etichette diverse per lo stesso
 *     dato, cioè esattamente ciò che questa fase elimina.
 */
export function labelCandidate(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (typeof value === 'string') return value === '' ? null : value;
  if (typeof value === 'number') {
    if (value === 0 || !Number.isFinite(value)) return null;
    return numberText(value);
  }
  return null;
}

/**
 * Primo candidato utilizzabile, altrimenti «(senza nome)».
 *
 * L'ordine è quello del requisito: nome mostrabile, poi codice di business, poi il
 * ripiego. Mai `undefined`, mai `null`, mai «None»: in un'email a un cliente non è un
 * dato, è un difetto che si legge.
 */
export function label(...candidates) {
  for (const c of candidates) {
    const usable = labelCandidate(c);
    if (usable !== null) return usable;
  }
  return NO_NAME;
}

export function deviceLabel(device) { return label(get(device, 'name'), get(device, 'id')); }
export function rackLabel(rack) { return label(get(rack, 'name'), get(rack, 'id')); }
export function roomLabel(room) { return label(get(room, 'nome'), get(room, 'id')); }
export function locationLabel(loc) { return label(get(loc, 'nome'), get(loc, 'id')); }
