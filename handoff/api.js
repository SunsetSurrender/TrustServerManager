// ============================================================
// api.js — client HTTP dell'applicazione
//
// URL SEMPRE RELATIVE (`/api/...`): l'applicazione è servita dallo stesso
// nginx che fa da proxy all'API, quindi non esiste un host da configurare.
// Un URL assoluto sarebbe un valore da tenere allineato fra ambienti e, con
// `SameSite=strict`, farebbe anche cadere il cookie di sessione.
//
// `credentials: 'same-origin'` e non 'include': il cookie serve solo verso la
// nostra origine, e 'include' aprirebbe la porta a invii verso terzi.
//
// Riferimento: BACKEND-PLAN.md §8.33.
// ============================================================

/** Errore con il codice del server, per poter distinguere i casi senza leggere
 *  i messaggi. `status` è quello HTTP, `code` quello di dominio. */
export class ApiError extends Error {
  constructor(status, code, message, detail) {
    super(message || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || '';
    this.detail = detail || null;
  }
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * @param {object} [opts]
 *  - headers      intestazioni aggiuntive (es. `If-Match`)
 *  - withHeaders  restituisce `{ data, etag }` invece del solo corpo
 *  - raw          il corpo si invia COSÌ COM'È e senza `Content-Type` nostro.
 *                 Serve al `FormData` del caricamento delle foto: il confine del
 *                 multipart lo genera il browser, e imporre un `Content-Type`
 *                 senza `boundary` renderebbe la richiesta illeggibile al server.
 *  - signal       `AbortSignal`. Serve alle interrogazioni: una ricerca in volo
 *                 diventa inutile appena l'utente digita un altro carattere, e
 *                 lasciarla correre significa pagarla e poi buttarla.
 */
async function request(method, path, body, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: opts.signal,
      headers: (body === undefined || opts.raw)
        ? (opts.headers || undefined)
        : { ...JSON_HEADERS, ...(opts.headers || {}) },
      body: body === undefined
        ? undefined
        : (opts.raw ? body : JSON.stringify(body)),
    });
  } catch (err) {
    // ⚠ L'annullamento PRIMA della rete, e non è un dettaglio: `fetch` respinge un
    // abort con un `DOMException` che finirebbe in questo stesso `catch`. Tradurlo in
    // «Server non raggiungibile» significherebbe mostrare un errore di rete ogni volta
    // che qualcuno digita un carattere in più nella casella di ricerca.
    if (err && (err.name === 'AbortError' || (opts.signal && opts.signal.aborted))) {
      throw new ApiError(0, 'aborted', 'Richiesta annullata.', null);
    }
    // Rete assente, DNS, TLS: non è una risposta del server e va distinta.
    throw new ApiError(0, 'network_error',
      'Server non raggiungibile. Controlla la connessione.', null);
  }

  if (res.status === 204) return opts.withHeaders ? { data: null, etag: null } : null;

  let payload = null;
  try {
    payload = await res.json();
  } catch { /* corpo vuoto o non JSON */ }

  if (res.ok) {
    return opts.withHeaders
      ? { data: payload, etag: res.headers.get('ETag') }
      : payload;
  }

  // FastAPI annida sotto `detail` gli errori sollevati come HTTPException, e
  // lascia in radice quelli dei middleware: si accetta entrambe le forme.
  const d = (payload && payload.detail !== undefined) ? payload.detail : payload;
  const code = (d && d.code) || '';
  const message = (d && d.message) || '';
  throw new ApiError(res.status, code, message, d);
}

// ------------------------------------------------------------------ auth

export const getMe = () => request('GET', '/api/auth/me');
export const doLogin = (username, password) =>
  request('POST', '/api/auth/login', { username, password });
export const doLogout = () => request('POST', '/api/auth/logout');
export const changePassword = (currentPassword, newPassword) =>
  request('POST', '/api/auth/password', { currentPassword, newPassword });

// ------------------------------------------------------------------ utenze
//
// Solo /api/users. Le utenze NON stanno nel documento dell'inventario: il server
// rifiuterebbe un documento che le contenesse (§8.16).
//
// Non esiste una funzione di cancellazione, e non è una dimenticanza: l'audit
// referenzia le utenze, quindi si disattiva e non si elimina (§8.30). Il ruolo di
// runtime del database non ha nemmeno il privilegio.

export const listUsers = (includeDisabled = true) =>
  request('GET', `/api/users?includeDisabled=${includeDisabled ? 'true' : 'false'}`);

export const createUser = (payload) => request('POST', '/api/users', payload);

/** Ogni operazione va per UUID immutabile, mai per username o posizione:
 *  l'username è rinominabile e la posizione nell'elenco cambia a ogni ricarica. */
export const updateUser = (id, patch) => request('PATCH', `/api/users/${id}`, patch);
export const disableUser = (id) => request('POST', `/api/users/${id}/disable`, {});
export const enableUser = (id) => request('POST', `/api/users/${id}/enable`, {});
export const resetUserPassword = (id) =>
  request('POST', `/api/users/${id}/reset-password`, {});

// ------------------------------------------------------------------ audit
//
// Sola lettura, solo admin. Il registro NON passa dal documento dell'inventario
// e il client non ne ricostruisce voci per conto proprio (§8.9): quello che si
// vede è quello che il server ha registrato, o niente.

/** Una pagina di registro. `cursor` è OPACO: si rimanda quello ricevuto, senza
 *  interpretarlo — il formato è affare del server e può cambiare. */
export function getAudit({ cursor = null, pageSize = 50, from = null, to = null,
                           username = null, event = null, result = null } = {}) {
  const q = new URLSearchParams();
  if (cursor) q.set('cursor', cursor);
  if (pageSize) q.set('pageSize', String(pageSize));
  if (from) q.set('from', from);
  if (to) q.set('to', to);
  if (username) q.set('username', username);
  if (event) q.set('event', event);
  if (result) q.set('result', result);
  return request('GET', '/api/audit?' + q.toString());
}

// ------------------------------------------------------------ impostazioni
//
// Concorrenza ottimistica con `ETag`/`If-Match`. Non è cerimonia: due
// amministratori che hanno la stessa schermata aperta salverebbero l'uno sopra
// l'altro, e chi perde non se ne accorgerebbe — nessun errore, nessuna traccia,
// e il destinatario appena aggiunto semplicemente non c'è più.
//
// La password SMTP non compare né in andata né in ritorno: è gestita
// dall'operations con un secret montato. Il documento dice solo se è configurata.

/** Impostazioni correnti. Restituisce `{ data, etag }`: l'ETag va CONSERVATO e
 *  rimandato tale e quale nella PUT, senza interpretarlo. */
export const getSettings = () =>
  request('GET', '/api/settings', undefined, { withHeaders: true });

/**
 * Salva. `etag` è quello ricevuto dalla GET.
 *
 * Si invia il SOLO blocco `notifications`: `version`, `smtp` e `updatedAt` sono
 * di sola lettura e il server li rifiuta. Rimandare indietro il documento
 * ricevuto così com'è non funziona, ed è voluto — la concorrenza si gestisce con
 * l'intestazione, non con un campo nel corpo che si può dimenticare.
 */
export const putSettings = (etag, notifications) =>
  request('PUT', '/api/settings', { notifications },
          { headers: { 'If-Match': etag }, withHeaders: true });

// ------------------------------------------------------------- notifiche
//
// Nessun parametro, e non è una semplificazione: destinatari e testo vengono
// dalle impostazioni SALVATE e dal server. Un endpoint che accettasse
// destinatario, oggetto e corpo sarebbe un servizio di invio posta autenticato.

/** Invia un messaggio di prova ai destinatari configurati. */
export const testNotification = () => request('POST', '/api/notifications/test', {});

// ------------------------------------------------------------------ foto
//
// Le foto NON stanno nel documento: nel documento c'è il loro UUID, e i byte
// vivono in una tabella a parte (§8.5). Il prototipo le teneva come `data:` URL
// dentro il JSON, e il server oggi rifiuta un documento che lo faccia — ogni
// versione ne duplicherebbe i byte, e il documento è versionato per sempre.
//
// Non esiste una funzione di cancellazione, e non è una dimenticanza: le versioni
// storiche referenziano le foto, quindi cancellarne i byte trasformerebbe un
// ripristino in un riquadro rotto. Togliere una foto da un rack significa salvare
// una versione nuova senza quel riferimento; i byte li libera la manutenzione
// lato server quando nessuna versione conservata li usa più.

/** URL da cui il browser carica una foto. Sempre relativa e sempre `/api/photos`. */
export const photoUrl = (id) => `/api/photos/${encodeURIComponent(id)}`;

/**
 * Carica un'immagine e restituisce `{ id, mime, sizeBytes, sha256, url }`.
 *
 * **Non aggancia la foto a niente.** L'aggancio è il normale `PUT` versionato
 * dell'inventario, e finché quello non conferma la modifica del rack NON è
 * salvata. È il motivo per cui questa funzione non tocca il documento: chi la
 * chiama deve mettere l'UUID nella bozza e passare dalla coda di scrittura.
 *
 * Il nome del file locale non si invia: al server non serve — l'identità della
 * foto è un UUID che genera lui — e un nome di file è testo che l'utente non ha
 * scelto di condividere (percorsi, cognomi, nomi di clienti).
 */
export function uploadPhoto(file) {
  const form = new FormData();
  form.append('file', file, 'foto');
  return request('POST', '/api/photos', form, { raw: true });
}

// ------------------------------------------------------------- inventario

export const getInventory = () => request('GET', '/api/inventory');

// ---------------------------------------------- interrogazioni relazionali (2H)
//
// Le tre domande che il frontend non calcola più da sé: ricerca, capacità,
// scadenze. Ognuna risponde con `version` e `sha256` — la revisione di inventario a
// cui la risposta appartiene — e sta al chiamante confrontarla con quella che ha
// sullo schermo. Il posto dove si confronta è uno solo: `queries.js`.
//
// ⚠ Nessuna di queste funzioni ha un fallback locale. Se il server risponde 503
// perché la proiezione non è attuale, la vista deve dirlo: calcolare la risposta nel
// browser nasconderebbe un difetto del backend e farebbe rinascere la duplicazione
// che la fase 2G ha chiuso.

/**
 * Query string, saltando i parametri non valorizzati.
 *
 * ⚠ `sempre` è l'elenco dei parametri che vanno mandati ANCHE se vuoti, ed esiste per
 * un difetto vero: `q` è obbligatoria per la rotta, e la vista Dismessi con la casella
 * di ricerca vuota la chiedeva senza — ricevendo 422 invece dell'elenco. Saltare i
 * vuoti è giusto per un filtro («nessun filtro») e sbagliato per una domanda
 * («cercami la stringa vuota fra i dismessi» è una domanda con una risposta).
 */
const _qs = (params, sempre = []) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (v === '' && !sempre.includes(k)) continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
};

/**
 * Ricerca globale. `q` è obbligatoria per il server; con `stato` o `presenza`
 * valorizzati una `q` vuota è legittima e restituisce l'elenco filtrato (estensione
 * della fase 2H, per la vista Dismessi).
 */
export const searchInventory = ({ q, stato, presenza, limit, cursor, signal } = {}) =>
  request('GET', '/api/inventory/search'
    + _qs({ q: q === undefined || q === null ? '' : q, stato, presenza, limit, cursor },
           ['q']),
    undefined, { signal });

/** Capacità di TUTTI i rack, in una richiesta. Non è paginata: vedi la rotta. */
export const getCapacity = ({ signal } = {}) =>
  request('GET', '/api/inventory/capacity', undefined, { signal });

/** Scadenze: `warningDays` è la soglia del livello «entro N giorni» (90 nella vista). */
export const getExpiries = ({ warningDays, stato, presenza, limit, cursor,
                              signal } = {}) =>
  request('GET', '/api/inventory/expiries'
    + _qs({ warningDays, stato, presenza, limit, cursor }), undefined, { signal });

/**
 * Coda di scrittura dell'inventario: UNA sola PUT in volo, UNA sola in attesa.
 *
 * `persist()` viene chiamata anche durante drag e resize, decine di volte al
 * secondo, e la versione è una sequenza stretta: due PUT in parallelo dallo
 * stesso client fanno 409 contro sé stesse. Il documento è completo, quindi
 * "coalescere" vuol dire semplicemente scartare l'intermedio — le posizioni
 * intermedie di un trascinamento non hanno valore storico.
 *
 * Vedi BACKEND-PLAN.md §8.2.
 */
export class InventoryWriter {
  /**
   * @param {object} hooks
   *  - onSaved(version, sha256)      salvataggio confermato dal server
   *  - onConflict(currentVersion)    409: il chiamante DEVE ricaricare
   *  - onError(apiError)             qualunque altro esito
   *  - onPendingChange(hasPending)   per l'indicatore "salvataggio in corso"
   */
  constructor(hooks = {}) {
    this.hooks = hooks;
    this.baseVersion = null;
    this.inFlight = null;      // Promise della PUT in corso
    this.pending = null;       // { doc, action } — sovrascritto, non accodato
    this.stopped = false;      // dopo un 409 non si scrive più finché non si ricarica
  }

  /** Versione da cui ripartire, dopo un GET o un ricaricamento. */
  reset(version) {
    this.baseVersion = version;
    this.pending = null;
    this.stopped = false;
    this._notify();
  }

  hasWorkOutstanding() {
    return !!(this.inFlight || this.pending);
  }

  _notify() {
    if (this.hooks.onPendingChange) this.hooks.onPendingChange(this.hasWorkOutstanding());
  }

  /** Accoda un salvataggio. Non attende: l'interfaccia resta reattiva. */
  queue(doc, action) {
    if (this.stopped) return;
    // Copia profonda: il documento in stato React continua a cambiare sotto,
    // e si deve inviare quello di adesso, non quello di quando parte la fetch.
    this.pending = { doc: JSON.parse(JSON.stringify(doc)), action: action || null };
    this._notify();
    if (!this.inFlight) this._drain();
  }

  async _drain() {
    while (this.pending && !this.stopped) {
      const { doc, action } = this.pending;
      this.pending = null;
      this._notify();

      this.inFlight = request('PUT', '/api/inventory', {
        baseVersion: this.baseVersion, doc, action,
      });
      try {
        const out = await this.inFlight;
        // La versione da cui ripartire è quella che il server dichiara, sempre:
        // anche per un no-op (`changed: false`), dove è quella già in testa.
        this.baseVersion = out.version;
        if (this.hooks.onSaved) this.hooks.onSaved(out.version, out.sha256, out.changed);
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          // Conflitto: qualcun altro ha scritto. Si SMETTE di scrivere e si
          // chiede un ricaricamento esplicito. Continuare significherebbe
          // sovrascrivere il lavoro di un'altra persona.
          this.stopped = true;
          this.pending = null;
          if (this.hooks.onConflict) {
            this.hooks.onConflict(err.detail && err.detail.currentVersion);
          }
        } else if (this.hooks.onError) {
          this.hooks.onError(err);
        }
        break;
      } finally {
        this.inFlight = null;
        this._notify();
      }
    }
  }
}

// --------------------------------------------------- messaggi per l'utente

/**
 * Traduce un ApiError in { titolo, testo, azione } per l'interfaccia.
 *
 * Ogni stato ha un significato diverso e merita un messaggio diverso: un
 * "errore" generico costringe l'utente a indovinare se ha sbagliato lui, se
 * deve riprovare, o se deve chiamare qualcuno.
 */
export function describeError(err) {
  if (!(err instanceof ApiError)) {
    return { titolo: 'Errore inatteso', testo: String(err && err.message || err),
             azione: null };
  }
  switch (err.status) {
    case 0:
      // L'annullamento non è un errore da mostrare: è una richiesta che non
      // interessava più. Chi la riceve la scarta; se finisse in un avviso, digitare
      // in fretta nella casella di ricerca produrrebbe una fila di messaggi.
      if (err.code === 'aborted') {
        return { titolo: 'Richiesta annullata', testo: '', azione: null };
      }
      return { titolo: 'Server non raggiungibile',
               testo: 'Le modifiche non sono state salvate. Controlla la connessione.',
               azione: 'riprova' };
    case 401:
      return { titolo: 'Sessione scaduta',
               testo: 'Accedi di nuovo per continuare.', azione: 'login' };
    case 403:
      // Due 403 diversi, e vanno distinti: uno è una password da cambiare, che
      // l'utente può risolvere subito; l'altro è un permesso che non ha, e per
      // cui deve chiedere a un amministratore.
      if (err.code === 'password_change_required') {
        return { titolo: 'Password da cambiare',
                 testo: 'Imposta una password personale per continuare.',
                 azione: 'cambia-password' };
      }
      if (err.code === 'origin_not_allowed') {
        return { titolo: 'Richiesta bloccata',
                 testo: 'La richiesta non proviene da un\'origine consentita. '
                      + 'Ricarica la pagina dall\'indirizzo ufficiale.',
                 azione: 'ricarica' };
      }
      return { titolo: 'Permesso negato',
               testo: (err.detail && err.detail.requiredRole)
                 ? `Questa operazione richiede il ruolo "${err.detail.requiredRole}".`
                 : 'Il tuo ruolo non consente questa operazione.',
               azione: null };
    case 409:
      // 409 non è un caso solo: lo stesso stato copre il conflitto di versione
      // dell'inventario, l'utenza già esistente e la protezione dell'ultimo
      // amministratore. Un testo unico direbbe alla persona una cosa falsa —
      // «qualcun altro ha salvato» quando in realtà ha scelto un nome già in
      // uso. Si distingue per codice.
      if (err.code === 'username_taken') {
        return { titolo: 'Utenza già esistente',
                 testo: err.message || 'Esiste già un\'utenza con questo nome.',
                 azione: null };
      }
      if (err.code === 'last_admin_protected') {
        return { titolo: 'Ultimo amministratore',
                 testo: err.message
                   || 'Non si può togliere l\'ultimo amministratore attivo: '
                    + 'nominane un altro prima di procedere.',
                 azione: null };
      }
      if (err.code === 'settings_version_conflict') {
        // Si RICARICA, non si riprova con gli stessi dati: il conflitto esiste
        // perché quei dati sono vecchi, e rimandarli sovrascriverebbe il lavoro
        // di un'altra persona — che è esattamente ciò che l'ETag impedisce.
        return { titolo: 'Impostazioni modificate altrove',
                 testo: 'Un altro amministratore ha salvato prima di te. '
                      + 'Ricarico la versione del server: rivedi le modifiche '
                      + 'e salva di nuovo.',
                 azione: 'ricarica' };
      }
      return { titolo: 'Modificato da un\'altra sessione',
               testo: 'Un\'altra persona ha salvato prima di te. '
                    + 'Ricarico i dati aggiornati: le tue modifiche non salvate '
                    + 'andranno riapplicate.',
               azione: 'ricarica' };
    case 404:
      if (err.code === 'photo_not_found') {
        return { titolo: 'Foto non disponibile',
                 testo: 'L\'immagine non è più presente sul server.', azione: null };
      }
      return { titolo: 'Non trovato',
               testo: err.message || 'La risorsa richiesta non esiste.', azione: null };
    case 413:
      if (err.code === 'photo_too_large') {
        return { titolo: 'Immagine troppo grande',
                 testo: 'Il limite è 10 MB. Riducila o salvala come JPEG.',
                 azione: null };
      }
      return { titolo: 'Dati troppo grandi',
               testo: 'Il documento supera il limite consentito. '
                    + 'Le foto vanno caricate separatamente.', azione: null };
    case 422:
      // Le foto hanno codici propri: dire PERCHÉ un'immagine è stata rifiutata è
      // la differenza fra un messaggio utile e «file non valido», che lascia la
      // persona a riprovare con lo stesso file.
      if (_PHOTO_ERRORS[err.code]) {
        return { titolo: 'Immagine non accettata',
                 testo: _PHOTO_ERRORS[err.code], azione: null };
      }
      // Le impostazioni hanno codici propri e un campo: dire QUALE campo è
      // sbagliato è la differenza fra un messaggio utile e «dati non accettati».
      if (_SETTINGS_ERRORS[err.code]) {
        return { titolo: 'Impostazioni non accettate',
                 testo: _SETTINGS_ERRORS[err.code]
                      + (err.detail && err.detail.field
                          ? ` (campo: ${err.detail.field})` : ''),
                 azione: null };
      }
      return { titolo: 'Dati non accettati',
               testo: _problemsText(err) || 'Il server ha rifiutato il documento.',
               azione: null };
    case 429:
      if (err.code === 'notification_test_rate_limited') {
        return { titolo: 'Troppi invii di prova',
                 testo: 'Il numero di messaggi di prova è limitato di proposito. '
                      + 'Attendi prima di riprovare.', azione: null };
      }
      return { titolo: 'Troppi tentativi',
               testo: 'Attendi qualche minuto prima di riprovare.', azione: null };
    case 503:
      if (_SMTP_ERRORS[err.code]) {
        return { titolo: 'Invio non riuscito',
                 testo: _SMTP_ERRORS[err.code](err), azione: null };
      }
      // ⚠ I due stati della proiezione relazionale, distinti (§12 della fase 2H).
      // Sono l'unico caso in cui una VISTA non può rispondere, e il rimedio è
      // un'operazione di sistemista: dirlo è l'unica risposta utile, e calcolare i
      // numeri nel browser sarebbe nascondere il guasto proprio a chi lo può riparare.
      if (_PROJECTION_ERRORS[err.code]) {
        return { titolo: 'Dati di ricerca non disponibili',
                 testo: _PROJECTION_ERRORS[err.code], azione: 'riprova' };
      }
      return { titolo: 'Servizio non disponibile',
               testo: 'Il server non è pronto. Le modifiche non sono state salvate; '
                    + 'riprova fra poco.', azione: 'riprova' };
    default:
      return { titolo: `Errore ${err.status}`,
               testo: err.message || 'Operazione non riuscita.', azione: null };
  }
}

/**
 * I due stati della proiezione relazionale → cosa dire a chi guarda.
 *
 * `projection_not_current`: la proiezione è più vecchia della testa. Succede dopo un
 * aggiornamento che cambia la versione della mappa, e si ripara con
 * `project.py --rebuild`.
 *
 * `projection_inconsistent`: la proiezione non corrisponde al documento che dichiara
 * di rappresentare. È più grave, e la differenza va detta: la prima è un passo di
 * manutenzione dimenticato, la seconda è un dato da guardare.
 */
const _PROJECTION_ERRORS = {
  projection_not_current:
    'Le viste Ricerca, Capacità e Scadenze leggono una copia dei dati che il server '
    + 'sta ancora aggiornando. L\'inventario resta consultabile e modificabile. '
    + 'Se persiste, serve una ricostruzione lato server.',
  projection_inconsistent:
    'La copia dei dati usata da Ricerca, Capacità e Scadenze non corrisponde '
    + 'all\'inventario. Le viste sono sospese di proposito: mostrare numeri sbagliati '
    + 'sarebbe peggio. Segnala l\'anomalia ai sistemisti.',
};

/** Codici di validazione delle impostazioni → cosa deve fare la persona. */
const _SETTINGS_ERRORS = {
  invalid_recipient: 'Un indirizzo email non è valido. Uno per riga.',
  duplicate_recipient: 'Lo stesso destinatario è indicato due volte.',
  too_many_recipients: 'Troppi destinatari.',
  invalid_warning_day: 'I giorni di preavviso devono essere numeri interi positivi.',
  too_many_warning_days: 'Troppe finestre di preavviso.',
  invalid_timezone: 'Fuso orario non riconosciuto (atteso un nome IANA, es. Europe/Rome).',
  invalid_schedule: 'Orario di invio non valido.',
  unknown_field: 'Il server non riconosce uno dei campi inviati.',
  missing_field: 'Manca un campo obbligatorio.',
  read_only_field: 'Uno dei campi inviati è di sola lettura.',
  secret_field_rejected: 'Le impostazioni non possono contenere password o segreti: '
                       + 'la password SMTP è gestita dai sistemisti.',
  invalid_type: 'Un valore ha un tipo non ammesso.',
  document_too_complex: 'La richiesta è troppo complessa.',
  if_match_required: 'Ricarico le impostazioni: riprova a salvare.',
  if_match_malformed: 'Ricarico le impostazioni: riprova a salvare.',
  invalid_body: 'Il server non ha potuto leggere la richiesta.',
  unexpected_fields: 'Questa operazione non accetta parametri.',
};

/** Rifiuti di un'immagine → cosa deve fare la persona. */
const _PHOTO_ERRORS = {
  photo_format_not_allowed: 'Sono ammessi solo JPEG, PNG e WebP. '
                          + 'I disegni vettoriali (SVG) non sono accettati.',
  photo_type_mismatch: 'Il contenuto del file non corrisponde al suo tipo. '
                     + 'Rinominare un file non ne cambia il formato: '
                     + 'riesporta l\'immagine.',
  photo_malformed: 'L\'immagine è danneggiata o incompleta.',
  photo_too_many_pixels: 'L\'immagine ha troppi pixel: ridimensionala '
                       + '(massimo circa 40 megapixel).',
  photo_empty: 'Il file è vuoto.',
  photo_id_malformed: 'Riferimento a un\'immagine non valido.',
  photo_not_found: 'L\'immagine non è più sul server: ricaricala.',
};

/** Esiti di un invio SMTP → una spiegazione, senza dettagli del server di posta. */
const _SMTP_ERRORS = {
  smtp_not_configured: () =>
    'L\'invio email non è configurato sul server. Serve l\'intervento dei sistemisti.',
  no_recipients_configured: () =>
    'Non c\'è nessun destinatario configurato: aggiungine uno e salva prima di provare.',
  smtp_send_failed: (err) => {
    const reason = (err.detail && err.detail.reason) || '';
    return {
      timeout: 'Il server di posta non ha risposto entro il tempo massimo.',
      connection_failed: 'Server di posta non raggiungibile.',
      auth_failed: 'Il server di posta ha rifiutato le credenziali.',
      recipients_refused: 'Il server di posta ha rifiutato i destinatari.',
      sender_refused: 'Il server di posta ha rifiutato il mittente.',
      tls_failed: 'Negoziazione TLS con il server di posta non riuscita.',
      protocol_error: 'Il server di posta ha risposto con un errore.',
    }[reason] || 'Invio non riuscito.';
  },
  notification_test_unavailable: () =>
    'Non è possibile verificare il limite degli invii: prova rifiutata per prudenza.',
  settings_unavailable: () =>
    'Le impostazioni non sono inizializzate sul server.',
};

function _problemsText(err) {
  const list = (err.detail && (err.detail.problems || err.detail.violations)) || [];
  if (!list.length) return '';
  const codes = [...new Set(list.map((p) => p.code).filter(Boolean))];
  return 'Problemi segnalati: ' + codes.join(', ') + '.';
}
