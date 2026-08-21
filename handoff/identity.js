// ============================================================
// identity.js — identità immutabile delle entità dell'inventario
//
// Ogni location, sala, rack, dispositivo e voce di manuale porta un `_uid`:
// un UUID v4 generato dal client alla creazione e mai più modificato.
// I codici di business (`id`: R01, srv-db-01) sono rinominabili e quindi NON
// sono identità. Vedi BACKEND-PLAN.md §8.4.
//
// Questo modulo è la sede della logica di identità: il markup dell'applicazione
// la richiama, i test la esercitano in isolamento, e il backend potrà
// rispecchiare le stesse regole senza riscriverle a naso.
//
// NON ha dipendenze e non tocca il DOM: gira identico nel browser e in node.
// ============================================================

export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

// ---------------------------------------------------------- versione di schema
//
// `schemaVersion` è la FORMA del documento; la revisione dell'inventario conta
// invece le modifiche ai dati. Sono due cose diverse e vanno tenute separate:
// la revisione cresce a ogni salvataggio e non dice come interpretare il
// contenuto. Vedi BACKEND-PLAN.md §8.13.
//
// Il campo `versione` presente nel seed era un contatore informale del
// prototipo, senza semantica applicata: NON è la versione di schema.

export const CURRENT_SCHEMA_VERSION = 1;

export const SCHEMA_VERSION_MISSING = 'schema_version_missing';
export const SCHEMA_VERSION_TOO_OLD = 'schema_version_too_old';
export const SCHEMA_VERSION_TOO_NEW = 'schema_version_too_new';
export const SCHEMA_VERSION_INVALID = 'schema_version_invalid';

/** Errori sulla versione di schema. Lista vuota = accettabile dal percorso normale. */
export function checkSchemaVersion(doc) {
  const found = (doc || {}).schemaVersion;
  if (found === undefined || found === null) {
    return [{ code: SCHEMA_VERSION_MISSING, found: null, expected: CURRENT_SCHEMA_VERSION,
      message: 'documento senza schemaVersion: precede l\'introduzione della versione di '
             + 'schema. Va migrato una volta con lo script dedicato; il percorso normale '
             + 'non aggiorna lo schema in silenzio.' }];
  }
  if (typeof found !== 'number' || !Number.isInteger(found)) {
    return [{ code: SCHEMA_VERSION_INVALID, found, expected: CURRENT_SCHEMA_VERSION,
      message: `schemaVersion non è un intero: ${JSON.stringify(found)}` }];
  }
  if (found < CURRENT_SCHEMA_VERSION) {
    return [{ code: SCHEMA_VERSION_TOO_OLD, found, expected: CURRENT_SCHEMA_VERSION,
      message: `schemaVersion ${found} è più vecchia di ${CURRENT_SCHEMA_VERSION}: `
             + 'serve una migrazione esplicita.' }];
  }
  if (found > CURRENT_SCHEMA_VERSION) {
    return [{ code: SCHEMA_VERSION_TOO_NEW, found, expected: CURRENT_SCHEMA_VERSION,
      message: `schemaVersion ${found} è più recente di ${CURRENT_SCHEMA_VERSION}: il file `
             + 'proviene da una versione più nuova dell\'applicazione.' }];
  }
  return [];
}

// ------------------------------------------------------------- forma canonica
//
// L'applicazione tratta l'assenza di certi campi come equivalente a un default
// (`d.stato || 'attivo'`, `d.h || 1`). Canonicalizzare rende esplicita quella
// equivalenza, così il passaggio da assente a default non è una modifica.
// Il gemello Python è backend/app/identity/canonical.py: le due tabelle devono
// restare allineate. Vedi §8.14.

export const ENTITY_DEFAULTS = {
  location: { sale: [] },
  room: { racks: [], vani: [], area: '', dim: '', segnaposto: false },
  rack: { devices: [], seriali: [], u: 45, name: '', row: '' },
  // ⚠ `presenza` aggiunta dalla fase 2G: presenza FISICA, indipendente dallo stato
  // operativo. L'assenza canonicalizza a `presente` — l'inventario di prima non
  // registrava le rimozioni, quindi di quelle macchine si sa solo che nessuno ha
  // detto che sono state portate via. Vedi handoff/domain.js e BACKEND-PLAN.md §8.50.
  device: { stato: 'attivo', presenza: 'presente', h: 1, type: 'altro', model: '',
            ip: '', serial: '', owner: '', garanzia: '', supporto: '', note: '' },
  manual: { titolo: '', blocchi: [] },
};

// Applicati SOLO se l'oggetto esiste già: canonicalizzare non deve inventare
// `notifiche` o `smtp` in un documento che non li ha mai avuti. Nessun
// `password`: non vive nel documento (§8.7).
export const SETTINGS_DEFAULTS = {
  notifiche: { email: '', giorni: 30, attive: false },
  smtp: { host: '', porta: 587, utente: '', mittente: '', tls: true },
};

const applyDefaults = (obj, defaults) => {
  if (!obj || typeof obj !== 'object') return obj;
  const out = { ...obj };
  for (const [k, v] of Object.entries(defaults)) {
    if (out[k] === undefined || out[k] === null) {
      out[k] = Array.isArray(v) ? [...v] : (v && typeof v === 'object' ? { ...v } : v);
    }
  }
  return out;
};

/** Documento in forma canonica. Pura (non modifica l'input) e idempotente.
 *  NON inventa `_uid` né `schemaVersion`: quelli si rifiutano, non si indovinano. */
export function canonicalise(doc) {
  if (!doc || typeof doc !== 'object') return doc;
  const out = { ...doc };

  out.locations = (doc.locations || []).map((L) => {
    const loc = applyDefaults(L, ENTITY_DEFAULTS.location);
    loc.sale = (loc.sale || []).map((R) => {
      const room = applyDefaults(R, ENTITY_DEFAULTS.room);
      room.vani = (room.vani || []).map((v) => ({ ...v }));
      room.racks = (room.racks || []).map((K) => {
        const rack = applyDefaults(K, ENTITY_DEFAULTS.rack);
        rack.seriali = [...(rack.seriali || [])];
        rack.devices = (rack.devices || []).map((V) => applyDefaults(V, ENTITY_DEFAULTS.device));
        return rack;
      });
      return room;
    });
    return loc;
  });

  if (doc.manuale != null) {
    out.manuale = doc.manuale.map((M) => applyDefaults(M, ENTITY_DEFAULTS.manual));
  }
  for (const [k, defaults] of Object.entries(SETTINGS_DEFAULTS)) {
    if (doc[k] != null) out[k] = applyDefaults(doc[k], defaults);
  }
  return out;
}

/** Ordina ricorsivamente le chiavi: serve al calcolo di hash stabili. */
export function canonicalSort(v) {
  if (Array.isArray(v)) return v.map(canonicalSort);
  if (v && typeof v === 'object') {
    const out = {};
    for (const k of Object.keys(v).sort()) out[k] = canonicalSort(v[k]);
    return out;
  }
  return v;
}

/** Rimuove ricorsivamente gli `_uid`. */
export function stripUids(v) {
  if (Array.isArray(v)) return v.map(stripUids);
  if (v && typeof v === 'object') {
    const out = {};
    for (const [k, val] of Object.entries(v)) if (k !== '_uid') out[k] = stripUids(val);
    return out;
  }
  return v;
}

/** Le entità con identità propria. I `vani` NON sono qui: vedi in fondo. */
export const KINDS = ['location', 'room', 'rack', 'device', 'manual'];

export const isUid = (v) => typeof v === 'string' && UUID_RE.test(v);

/**
 * Genera un nuovo `_uid`.
 *
 * `crypto.randomUUID` esiste solo in secure context (HTTPS oppure localhost).
 * Se manca si solleva: un fallback su Math.random darebbe UUID debilí e
 * collidibili, e un'identità debole è peggio di un errore visibile.
 */
export function newUid() {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (!c || typeof c.randomUUID !== 'function') {
    throw new Error(
      'crypto.randomUUID() non disponibile: serve un secure context (HTTPS o localhost). ' +
      'Nessun fallback: un UUID debole comprometterebbe l\'identità dell\'inventario.'
    );
  }
  return c.randomUUID();
}

// ---------------------------------------------------------------- traversal

/**
 * Percorre l'albero e restituisce una riga per ogni entità con identità.
 * `code` è il codice di business, `parentUid` l'identità del contenitore.
 */
export function walkEntities(doc) {
  const out = [];
  const d = doc || {};
  for (const L of d.locations || []) {
    out.push({ kind: 'location', obj: L, uid: L._uid, code: L.id, parentUid: null,
               path: L.id || '(senza id)' });
    for (const R of L.sale || []) {
      out.push({ kind: 'room', obj: R, uid: R._uid, code: R.id, parentUid: L._uid,
                 path: `${L.id} / ${R.id}` });
      for (const K of R.racks || []) {
        out.push({ kind: 'rack', obj: K, uid: K._uid, code: K.id, parentUid: R._uid,
                   path: `${L.id} / ${R.id} / ${K.id}` });
        for (const V of K.devices || []) {
          out.push({ kind: 'device', obj: V, uid: V._uid, code: V.id, parentUid: K._uid,
                     path: `${L.id} / ${R.id} / ${K.id} / ${V.id}` });
        }
      }
    }
  }
  for (const M of d.manuale || []) {
    out.push({ kind: 'manual', obj: M, uid: M._uid, code: M.id, parentUid: null,
               path: `manuale / ${M.titolo || M.id}` });
  }
  return out;
}

export function indexByUid(doc) {
  const m = new Map();
  for (const e of walkEntities(doc)) if (e.uid != null) m.set(e.uid, e);
  return m;
}

const keyOf = (e) => `${e.kind} ${e.parentUid ?? ''} ${e.code ?? ''}`;

// ------------------------------------------------------- validazione interna

/**
 * Controlli che non richiedono un documento di confronto: ogni entità deve
 * avere un `_uid` presente, sintatticamente valido e univoco.
 *
 * Restituisce un array di errori (vuoto = documento valido).
 */
export function validateDocument(doc) {
  const errors = [];
  const seen = new Map();

  for (const e of walkEntities(doc)) {
    if (e.uid === undefined || e.uid === null || e.uid === '') {
      errors.push({ code: 'missing_uid', kind: e.kind, path: e.path,
                    message: `${e.kind} "${e.path}" senza _uid` });
      continue;
    }
    if (!isUid(e.uid)) {
      errors.push({ code: 'malformed_uid', kind: e.kind, path: e.path, uid: e.uid,
                    message: `${e.kind} "${e.path}" ha un _uid non conforme a UUID: ${e.uid}` });
      continue;
    }
    if (seen.has(e.uid)) {
      errors.push({ code: 'duplicate_uid', kind: e.kind, path: e.path, uid: e.uid,
                    message: `_uid duplicato ${e.uid}: "${seen.get(e.uid).path}" e "${e.path}"` });
      continue;
    }
    seen.set(e.uid, e);
  }
  return errors;
}

// -------------------------------------------------- validazione differenziale

/**
 * Confronta il documento in uscita con quello di partenza.
 *
 * Un `_uid` sconosciuto è ammesso SOLO se corrisponde a un add autentico.
 * Viene rifiutato quando:
 *   - sostituisce un'entità esistente (stesso codice, vecchio uid svanito)
 *   - riusa il codice di business di un'entità ancora presente
 *   - accompagna la sparizione inspiegata del vecchio uid
 *   - realizza una sostituzione di identità delete+add
 *
 * Tutti e quattro i casi collassano su un unico test verificabile: esiste
 * un'entità della base, con lo stesso codice, che è scomparsa o che è ancora
 * lì? Se sì, questo non è un add: è una sostituzione travestita.
 */
export function validateAgainstBase(baseDoc, nextDoc) {
  const errors = validateDocument(nextDoc);
  // Senza identità coerente nel nuovo documento il confronto non è affidabile:
  // meglio riportare quegli errori e fermarsi che accumulare rumore.
  if (errors.length) return errors;

  const baseEntities = walkEntities(baseDoc);
  const baseByUid = new Map();
  const baseByKey = new Map();
  const baseByKindCode = new Map();           // fallback: stesso tipo e codice altrove
  for (const e of baseEntities) {
    if (e.uid != null) baseByUid.set(e.uid, e);
    baseByKey.set(keyOf(e), e);
    const kc = `${e.kind} ${e.code ?? ''}`;
    if (!baseByKindCode.has(kc)) baseByKindCode.set(kc, []);
    baseByKindCode.get(kc).push(e);
  }

  const nextEntities = walkEntities(nextDoc);
  const nextUids = new Set(nextEntities.map((e) => e.uid));

  for (const e of nextEntities) {
    if (baseByUid.has(e.uid)) continue;       // entità già nota: non è un add

    // _uid sconosciuto: add autentico oppure sostituzione di identità?
    const sameKey = baseByKey.get(keyOf(e));
    if (sameKey) {
      const oldSurvives = nextUids.has(sameKey.uid);
      errors.push({
        code: oldSurvives ? 'business_key_reuse' : 'identity_replacement',
        kind: e.kind, path: e.path, uid: e.uid, replacedUid: sameKey.uid,
        message: oldSurvives
          ? `${e.kind} "${e.path}": _uid nuovo ${e.uid} su un codice già usato da ${sameKey.uid}`
          : `${e.kind} "${e.path}": sostituzione di identità — ${sameKey.uid} è scomparso e ` +
            `al suo posto c'è ${e.uid} con lo stesso codice. Una rinomina deve conservare l'_uid.`,
      });
      continue;
    }

    // Stesso codice sotto un altro genitore (spostamento) con il vecchio uid
    // svanito: è la stessa sostituzione, mascherata da move.
    const elsewhere = (baseByKindCode.get(`${e.kind} ${e.code ?? ''}`) || [])
      .filter((b) => !nextUids.has(b.uid));
    if (elsewhere.length === 1) {
      errors.push({
        code: 'identity_replacement', kind: e.kind, path: e.path, uid: e.uid,
        replacedUid: elsewhere[0].uid,
        message: `${e.kind} "${e.path}": _uid nuovo ${e.uid} mentre ${elsewhere[0].uid} ` +
                 `("${elsewhere[0].path}") con lo stesso codice è scomparso. ` +
                 `Uno spostamento deve conservare l'_uid.`,
      });
      continue;
    }
    if (elsewhere.length > 1) {
      errors.push({
        code: 'ambiguous_replacement', kind: e.kind, path: e.path, uid: e.uid,
        message: `${e.kind} "${e.path}": _uid nuovo con codice "${e.code}" che corrisponde a ` +
                 `${elsewhere.length} entità scomparse. Corrispondenza ambigua: rifiutata.`,
      });
    }
    // nessuna corrispondenza: add autentico, ammesso.
  }
  return errors;
}

// ------------------------------------------------------------------ helper UI

/**
 * Deriva l'oggetto da inviare da quello esistente, così i campi sconosciuti
 * e i metadati futuri sopravvivono. Per le entità nuove genera l'`_uid`.
 *
 * È la primitiva che i percorsi di salvataggio devono usare al posto di
 * costruire un oggetto letterale da zero.
 */
export function preserveIdentity(original, patch) {
  const base = original && typeof original === 'object' ? original : null;
  const out = { ...(base || {}), ...patch };
  if (!base || !out._uid) out._uid = newUid();
  return out;
}

/** Crea una nuova entità con identità. */
export function createEntity(fields) {
  return { _uid: newUid(), ...fields };
}

// ------------------------------------------------- identità per import foglio

/**
 * Trova il dispositivo esistente cui una riga di foglio si riferisce.
 *
 * `_uid` è l'unica identità. Né `id` né `nome` lo sono:
 *   - `id` deriva dal nome, è modificabile e NON è univoco a livello globale
 *     (due rack possono contenere due dispositivi con lo stesso `id`);
 *   - `nome` è a maggior ragione mutabile.
 *
 * Regole:
 *   1. `_uid` presente → identità certa, e uno spostamento fra rack è ammesso.
 *   2. Senza `_uid` → corrispondenza **limitata al rack di destinazione**, per
 *      `id` e poi per `nome`, e solo se risolve a UN candidato. Serve per
 *      aggiornare, mai per spostare: una riga legacy senza `_uid` che
 *      corrisponde a un dispositivo in un ALTRO rack viene rifiutata, perché
 *      accettarla significherebbe spostare un dispositivo su una base di
 *      identità che non è identità.
 *   3. Zero candidati nel rack e nessuno altrove → dispositivo nuovo.
 *      Candidati multipli, o zero nel rack ma presenti altrove → rifiuto.
 *
 * `targetRack` è l'oggetto rack di destinazione (quello risolto dalle colonne
 * location/sala/rack della riga). Senza di esso non si può fare altro che
 * pretendere l'`_uid`.
 *
 * Restituisce { match, ambiguous, isNew, reason }.
 */
export function matchDeviceForImport(doc, row, targetRack, claimed) {
  const devices = walkEntities(doc).filter((e) => e.kind === 'device');
  const wantUid = (row._uid || '').trim();
  const wantId = (row.id || '').trim();
  const wantName = (row.nome || '').trim();

  // Due righe non possono puntare allo stesso dispositivo: la seconda
  // sovrascriverebbe la prima e l'esito dipenderebbe dall'ordine delle righe.
  const claim = (m) => {
    if (m.match && claimed && claimed.has(m.match.uid)) {
      return { match: null, ambiguous: true, isNew: false,
               reason: `un'altra riga del foglio punta già a questo dispositivo `
                     + `(${m.match.path}): righe duplicate` };
    }
    return m;
  };

  // --- 1. identità esplicita ---
  if (wantUid) {
    if (!isUid(wantUid)) {
      return { match: null, ambiguous: true, isNew: false,
               reason: `_uid "${wantUid}" non conforme a UUID` };
    }
    const hits = devices.filter((d) => d.uid === wantUid);
    if (hits.length === 1) return claim({ match: hits[0], ambiguous: false, isNew: false, reason: 'per _uid' });
    if (hits.length === 0) {
      return { match: null, ambiguous: true, isNew: false,
               reason: `_uid ${wantUid} non corrisponde a nessun dispositivo esistente` };
    }
    return { match: null, ambiguous: true, isNew: false,
             reason: `_uid ${wantUid} corrisponde a ${hits.length} dispositivi: documento incoerente` };
  }

  // --- 2. riga legacy: solo dentro il rack di destinazione ---
  if (!targetRack) {
    return { match: null, ambiguous: true, isNew: false,
             reason: 'rack di destinazione non risolto: senza _uid non è possibile identificare la riga' };
  }
  const inRack = devices.filter((d) => d.parentUid === targetRack._uid);

  const pick = (candidates, how, needle) => {
    if (candidates.length === 1) {
      return { match: candidates[0], ambiguous: false, isNew: false, reason: `per ${how} nel rack` };
    }
    return { match: null, ambiguous: true, isNew: false,
             reason: `${how} "${needle}" corrisponde a ${candidates.length} dispositivi nel rack ` +
                     `${targetRack.id}: corrispondenza ambigua, serve la colonna _uid` };
  };

  if (!wantId && !wantName) {
    return { match: null, ambiguous: true, isNew: false, reason: 'riga senza _uid, id e nome' };
  }

  const byId = wantId ? inRack.filter((d) => d.code === wantId) : [];
  const byName = wantName ? inRack.filter((d) => (d.obj.name || '') === wantName) : [];

  // `id` e `nome` che indicano dispositivi DIVERSI: la riga è contraddittoria.
  // Scegliere una delle due chiavi significherebbe decidere quale informazione
  // dell'utente ignorare, e l'esito dipenderebbe dalla precedenza scelta.
  if (byId.length === 1 && byName.length === 1 && byId[0].uid !== byName[0].uid) {
    return { match: null, ambiguous: true, isNew: false,
             reason: `id "${wantId}" e nome "${wantName}" indicano due dispositivi diversi `
                   + `nel rack ${targetRack.id} (${byId[0].path} e ${byName[0].path}): `
                   + `riga contraddittoria` };
  }

  if (byId.length) return claim(pick(byId, 'id', wantId));
  if (byName.length) return claim(pick(byName, 'nome', wantName));

  // --- 3. niente nel rack: nuovo, oppure spostamento non consentito ---
  const elsewhere = devices.filter((d) =>
    d.parentUid !== targetRack._uid &&
    ((wantId && d.code === wantId) || (wantName && (d.obj.name || '') === wantName)));
  if (elsewhere.length) {
    return { match: null, ambiguous: true, isNew: false,
             reason: `"${wantName || wantId}" esiste in un altro rack ` +
                     `(${elsewhere.map((d) => d.path).join('; ')}). Una riga senza _uid non può ` +
                     `spostare un dispositivo: aggiungere la colonna _uid.` };
  }
  return { match: null, ambiguous: false, isNew: true, reason: 'nuovo' };
}

// ------------------------------------------- mappatura colonne e valori foglio
//
// L'export XLSX formattato usa intestazioni leggibili ("Altezza U") ed etichette
// ("In manutenzione"), mentre l'import cercava i nomi tecnici. Risultato: quel
// foglio si ri-importava perdendo altezze e stati. Le mappe qui sotto rendono i
// due formati compatibili, e sono qui per poter essere testate.

/** Alias di intestazione → nome canonico della colonna. */
export const HEADER_ALIASES = {
  'altezza u': 'h', 'altezza': 'h', 'altezza (u)': 'h', 'h (u)': 'h',
  'u di partenza': 'u', 'slot': 'u', 'slot u': 'u',
  'id location': 'location', 'location id': 'location', 'sito': 'location',
  'id sala': 'sala', 'sala id': 'sala',
  'id rack': 'rack', 'rack id': 'rack', 'armadio': 'rack',
  'nome dispositivo': 'nome', 'dispositivo': 'nome',
  'modello': 'modello', 'referente': 'referente',
  'numero di serie': 'seriale', 'serial': 'seriale', 'seriale/i': 'seriale',
  'indirizzo ip': 'ip',
  'uid': '_uid', 'id univoco': '_uid', 'identita': '_uid',
  'scadenza garanzia': 'garanzia', 'scadenza supporto': 'supporto',
  'stato del dispositivo': 'stato', 'tipo dispositivo': 'tipo',
  // fase 2G: presenza fisica. Gli alias coprono l'intestazione dell'export
  // formattato ("Presenza") e i modi in cui una persona la scrive a mano.
  'presenza fisica': 'presenza', 'presente': 'presenza', 'in rack': 'presenza',
};

/** Intestazioni normalizzate: minuscole, spazi compattati, alias risolti. */
export function normalizeHeaders(headers) {
  return (headers || []).map((h) => {
    const k = String(h == null ? '' : h).trim().toLowerCase().replace(/\s+/g, ' ');
    return HEADER_ALIASES[k] || k;
  });
}

export const TIPI = ['server', 'rete', 'storage', 'firewall', 'alimentazione', 'altro'];
export const STATI = ['attivo', 'manutenzione', 'dismissione', 'dismesso'];

/** Presenza FISICA (fase 2G). Vocabolario separato da `STATI`, perché le due
 *  domande sono indipendenti: vedi handoff/domain.js. */
export const PRESENZE = ['presente', 'rimosso'];

/** Etichette visualizzate → chiave. Le etichette degli stati NON coincidono con
 *  le chiavi ("In manutenzione" → manutenzione), quindi senza questa mappa un
 *  re-import riportava tutto ad "attivo". */
const STATO_LABELS = {
  'attivo': 'attivo',
  'in manutenzione': 'manutenzione', 'manutenzione': 'manutenzione',
  'in dismissione': 'dismissione', 'dismissione': 'dismissione',
  'dismesso': 'dismesso',
};

export function parseStato(v, fallback = 'attivo') {
  const k = String(v == null ? '' : v).trim().toLowerCase();
  if (!k) return fallback;
  return STATO_LABELS[k] || (STATI.includes(k) ? k : fallback);
}

export function parseTipo(v, fallback = 'altro') {
  const k = String(v == null ? '' : v).trim().toLowerCase();
  if (!k) return fallback;
  return TIPI.includes(k) ? k : fallback;
}

/** Etichette della presenza → chiave, per l'import da foglio.
 *
 *  ⚠ Comprende le forme che una persona scrive davvero in una colonna «presenza»:
 *  «sì»/«no», «in rack», «rimosso il 3/2». La regola è la stessa di `parseStato` —
 *  un valore che non si riconosce NON diventa il default in silenzio se il campo
 *  era valorizzato: diventa il fallback passato dal chiamante, che per una riga
 *  esistente è la presenza che il dispositivo ha già. Perdere una rimozione
 *  registrata a mano per una parola scritta male sarebbe peggio che non importarla. */
const PRESENZA_LABELS = {
  'presente': 'presente', 'si': 'presente', 'sì': 'presente', 'yes': 'presente',
  'in rack': 'presente', 'installato': 'presente',
  'rimosso': 'rimosso', 'no': 'rimosso', 'rimossa': 'rimosso',
  'asportato': 'rimosso', 'non presente': 'rimosso',
};

export function parsePresenza(v, fallback = 'presente') {
  const k = String(v == null ? '' : v).trim().toLowerCase();
  if (!k) return fallback;
  return PRESENZA_LABELS[k] || (PRESENZE.includes(k) ? k : fallback);
}

// --------------------------------------------------------------------- note
//
// `vani`: NON hanno `_uid`. Sono value object posseduti dalla sala — la
// geometria della stanza. L'applicazione non offre alcuna interfaccia per
// crearli, modificarli o eliminarli singolarmente: nascono con la sala e da lì
// vengono solo disegnati. Non hanno codice, nome, né identità che l'utente
// possa osservare, quindi nessuna domanda di audit su un vano è distinguibile
// da «è cambiata la geometria della sala». Assegnargli un'identità
// significherebbe obbligare il client a conservarla per oggetti che non ha modo
// di gestire, senza alcun beneficio.
//
// `manuale`: HANNO `_uid`. Gli admin le creano, modificano ed eliminano una per
// una, la fase 2 le normalizza in `manual_entries`, e `manSave` ricostruiva
// l'oggetto da zero — la stessa classe di bug di `saveDraft`.
//
// Conseguenza per la fase 2: la tabella `vani` non espone identità propria e
// viene riscritta in blocco quando cambia la geometria della sala. Vedi §2 e
// §8.4 del piano.
