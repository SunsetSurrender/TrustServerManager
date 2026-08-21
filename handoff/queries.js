// ============================================================
// queries.js — il client delle interrogazioni relazionali (fase 2H)
//
// Dalla fase 2H la ricerca, la capacità e le scadenze NON si calcolano più nel
// browser: si chiedono al server, che le calcola con la semantica finale della 2G.
// Questo modulo è il posto — uno solo — dove stanno le due regole che rendono
// sicuro chiedere una cosa a un server mentre l'utente continua a lavorare.
//
// REGOLA 1: una risposta vecchia non deve sovrascrivere una nuova.
//
//   L'utente digita `sr`, poi `srv`. Partono due richieste. Se quella di `sr`
//   torna DOPO, l'elenco mostrato risponde a una domanda che l'utente non sta più
//   facendo — e non c'è niente sullo schermo che lo dica. È un difetto che si
//   manifesta solo quando la rete è lenta, cioè quando nessuno lo sta cercando.
//
//   Si risolve con un contatore di generazione PIÙ l'annullamento: il contatore
//   decide chi ha diritto di scrivere il risultato, `AbortController` evita di
//   pagare una risposta che verrà scartata. Serve il contatore anche con l'abort,
//   perché fra l'`abort()` e il rifiuto della promessa passa del tempo.
//
// REGOLA 2: un risultato di una revisione non si mescola con un'altra revisione.
//
//   Ogni risposta porta `version` e `sha256` dell'inventario su cui è stata
//   calcolata. Il frontend ha in memoria un documento, con la sua revisione. Se le
//   due non coincidono, il risultato descrive un inventario diverso da quello sullo
//   schermo: una ricerca che rimanda a un rack che nel documento caricato non
//   esiste più, una capacità che non corrisponde ai rack disegnati.
//
//   ⚠ Il confronto è su ENTRAMBI i valori. Il numero di versione da solo non basta:
//   dopo un rollback esistono due revisioni con lo stesso numero e contenuto
//   diverso, ed è esattamente il caso in cui un client sbaglierebbe in silenzio.
//
//   Cosa si fa quando divergono: si RICARICA l'inventario per il cammino già
//   esistente e si riprova UNA volta. Non si mostra il risultato vecchio, e non si
//   riprova all'infinito: se dopo il ricaricamento la revisione ancora non
//   corrisponde, qualcuno sta salvando in continuazione e la risposta onesta è
//   dirlo, non entrare in un ciclo di richieste.
//
// ⚠ Nessun fallback locale, in nessun ramo (§12 del requisito). Un errore qui è uno
// stato dell'interfaccia, non un motivo per ricalcolare i numeri nel browser: il
// calcolo locale è ciò che questa fase esiste per togliere, e riportarlo come «rete
// di sicurezza» significherebbe far tacere in silenzio un guasto della proiezione.
//
// Riferimento: BACKEND-PLAN.md §8.51.
// ============================================================

/** Esiti di `run`. Sono quattro, e ognuno ha una risposta diversa nell'interfaccia. */
export const OK = 'ok';           // consuma il risultato
export const STALE = 'stale';     // superata da una richiesta più recente: scarta
export const MISMATCH = 'mismatch'; // revisione ancora diversa dopo il ricaricamento
export const ERROR = 'error';     // il server ha risposto male, o non ha risposto

/** La revisione di una risposta, o `null` se non ne porta una. */
export function revisionOf(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const { version, sha256 } = payload;
  if (typeof version !== 'number' || typeof sha256 !== 'string' || !sha256) return null;
  return { version, sha256 };
}

/**
 * Le due revisioni sono la stessa?
 *
 * ⚠ `sha256` ANCHE quando i numeri di versione coincidono. Dopo un rollback (§8.19)
 * il numero può ripetersi con un contenuto diverso: confrontare solo il numero
 * significherebbe accettare come «stesso stato» due stati diversi, che è il difetto
 * che questo confronto esiste per impedire.
 */
export function sameRevision(a, b) {
  if (!a || !b) return false;
  return a.version === b.version && a.sha256 === b.sha256;
}

/**
 * Una richiesta per volta, e vince l'ultima.
 *
 * Non è un dettaglio di implementazione della ricerca: qualunque interrogazione
 * ripetuta (l'utente riapre la vista Capacità mentre la precedente è in volo) ha lo
 * stesso problema.
 */
export class LatestOnly {
  constructor() {
    this._gen = 0;
    this._ctrl = null;
  }

  /** Annulla la richiesta in volo, se c'è. L'esito diventa `STALE` per chi l'attendeva. */
  cancel() {
    this._gen++;
    if (this._ctrl) { this._ctrl.abort(); this._ctrl = null; }
  }

  /** `true` se `gen` è ancora la generazione corrente. */
  isCurrent(gen) { return gen === this._gen; }

  /**
   * Esegue `fn(signal)`. Restituisce `{ status, payload | error, gen }`.
   *
   * `fn` riceve un `AbortSignal` e deve passarlo alla richiesta: senza, l'annullamento
   * eviterebbe di USARE la risposta ma non di aspettarla.
   */
  async run(fn) {
    if (this._ctrl) this._ctrl.abort();
    const ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
    this._ctrl = ctrl;
    const gen = ++this._gen;
    try {
      const payload = await fn(ctrl ? ctrl.signal : undefined);
      if (gen !== this._gen) return { status: STALE, gen };
      this._ctrl = null;
      return { status: OK, payload, gen };
    } catch (error) {
      if (gen !== this._gen) return { status: STALE, gen };
      this._ctrl = null;
      return { status: ERROR, error, gen };
    }
  }
}

/**
 * Il client: le due regole applicate insieme, in un posto solo.
 *
 * @param {object} hooks
 *  - getLoadedRevision()  `{version, sha256}` dell'inventario che il frontend mostra,
 *                         oppure `null` se non ne ha ancora uno.
 *  - reloadInventory()    ricarica l'inventario col cammino già esistente. Deve
 *                         risolvere DOPO che `getLoadedRevision()` è aggiornata.
 *  - maxReloads           quante volte riconciliare prima di arrendersi. Uno: due
 *                         tentativi consecutivi che divergono non sono una corsa
 *                         sfortunata, sono qualcuno che salva senza sosta.
 */
export class QueryClient {
  constructor({ getLoadedRevision, reloadInventory, maxReloads = 1 } = {}) {
    this._loaded = getLoadedRevision || (() => null);
    this._reload = reloadInventory || (async () => {});
    this._maxReloads = maxReloads;
    this._slots = new Map();
    //: Contatori, per poter MISURARE il comportamento invece di descriverlo. Il test
    //: del browser (§16) legge `stats.requests` per contare le richieste per battuta.
    this.stats = { requests: 0, stale: 0, reloads: 0, mismatches: 0, errors: 0 };
  }

  /** Lo slot di un'interrogazione logica. Chiavi diverse non si annullano fra loro. */
  slot(name) {
    if (!this._slots.has(name)) this._slots.set(name, new LatestOnly());
    return this._slots.get(name);
  }

  /** Annulla ciò che è in volo per quel nome (la vista si chiude, per esempio). */
  cancel(name) {
    const s = this._slots.get(name);
    if (s) s.cancel();
  }

  /**
   * Esegue l'interrogazione `name` con `fetcher(signal)` e ne applica le due regole.
   *
   * Restituisce `{ status, payload?, error?, revision? }`.
   */
  async run(name, fetcher) {
    const slot = this.slot(name);
    for (let tentativo = 0; ; tentativo++) {
      this.stats.requests++;
      const esito = await slot.run(fetcher);

      if (esito.status === STALE) { this.stats.stale++; return { status: STALE }; }
      if (esito.status === ERROR) {
        this.stats.errors++;
        return { status: ERROR, error: esito.error };
      }

      const revisione = revisionOf(esito.payload);
      const caricata = this._loaded();
      if (sameRevision(revisione, caricata)) {
        return { status: OK, payload: esito.payload, revision: revisione };
      }

      // ⚠ Da qui in poi il risultato NON si mostra. Anche quando «sembra giusto»: la
      // differenza fra le due revisioni può essere un solo dispositivo, e sarebbe
      // invisibile guardando lo schermo.
      if (tentativo >= this._maxReloads) {
        this.stats.mismatches++;
        return { status: MISMATCH, revision: revisione, loaded: caricata };
      }
      this.stats.reloads++;
      await this._reload();
      // Se un'altra richiesta è partita mentre si ricaricava, questa non ha più
      // diritto di scrivere: si dichiara superata invece di riprovare.
      if (!slot.isCurrent(esito.gen)) { this.stats.stale++; return { status: STALE }; }
    }
  }
}

/**
 * Ritardo per la ricerca a mano che digita.
 *
 * Non è un'ottimizzazione: senza, ogni carattere è una richiesta, e su «srv-web-01»
 * sono undici richieste per una risposta che interessa. `flush` serve al tasto Invio —
 * chi lo premette ha finito di digitare e non deve aspettare il ritardo — e `cancel` a
 * chi chiude la vista.
 */
export function debounce(fn, ms) {
  let t = null;
  let ultimi = null;
  const annulla = () => { if (t !== null) { clearTimeout(t); t = null; } };
  const chiamata = (...args) => {
    ultimi = args;
    annulla();
    t = setTimeout(() => { t = null; fn(...ultimi); }, ms);
  };
  chiamata.cancel = annulla;
  chiamata.flush = () => { if (t !== null) { annulla(); fn(...ultimi); } };
  chiamata.pending = () => t !== null;
  return chiamata;
}
