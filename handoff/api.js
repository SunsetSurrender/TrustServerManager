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

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: body === undefined ? undefined : JSON_HEADERS,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // Rete assente, DNS, TLS: non è una risposta del server e va distinta.
    throw new ApiError(0, 'network_error',
      'Server non raggiungibile. Controlla la connessione.', null);
  }

  if (res.status === 204) return null;

  let payload = null;
  try {
    payload = await res.json();
  } catch { /* corpo vuoto o non JSON */ }

  if (res.ok) return payload;

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

// ------------------------------------------------------------- inventario

export const getInventory = () => request('GET', '/api/inventory');

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
      return { titolo: 'Modificato da un\'altra sessione',
               testo: 'Un\'altra persona ha salvato prima di te. '
                    + 'Ricarico i dati aggiornati: le tue modifiche non salvate '
                    + 'andranno riapplicate.',
               azione: 'ricarica' };
    case 413:
      return { titolo: 'Dati troppo grandi',
               testo: 'Il documento supera il limite consentito. '
                    + 'Le foto vanno caricate separatamente.', azione: null };
    case 422:
      return { titolo: 'Dati non accettati',
               testo: _problemsText(err) || 'Il server ha rifiutato il documento.',
               azione: null };
    case 429:
      return { titolo: 'Troppi tentativi',
               testo: 'Attendi qualche minuto prima di riprovare.', azione: null };
    case 503:
      return { titolo: 'Servizio non disponibile',
               testo: 'Il server non è pronto. Le modifiche non sono state salvate; '
                    + 'riprova fra poco.', azione: 'riprova' };
    default:
      return { titolo: `Errore ${err.status}`,
               testo: err.message || 'Operazione non riuscita.', azione: null };
  }
}

function _problemsText(err) {
  const list = (err.detail && (err.detail.problems || err.detail.violations)) || [];
  if (!list.length) return '';
  const codes = [...new Set(list.map((p) => p.code).filter(Boolean))];
  return 'Problemi segnalati: ' + codes.join(', ') + '.';
}
