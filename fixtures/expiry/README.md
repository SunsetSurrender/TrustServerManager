# Fixture delle scadenze

`build.py` genera un inventario con date **relative a una data di riferimento**,
perché un file con date fisse smette di provare quello che dice il giorno dopo:
`2026-09-15` è «fra 30 giorni» solo per una settimana.

Il seed di produzione **non contiene nessuna data di scadenza** — verificato — ed
è la ragione per cui queste fixture esistono. Un worker che gira sul seed reale e
non manda niente non sta dimostrando di funzionare: sta dimostrando che non ci
sono dati (§8.41).

## Cosa contiene

| Dispositivo | Garanzia | Supporto | Perché c'è |
|---|---|---|---|
| `srv-oggi` | oggi (0 giorni) | — | confine inferiore: `days_remaining == 0` è dentro la finestra |
| `srv-7` | +7 | — | soglia esatta |
| `srv-6` | +6 | — | dentro la finestra da 7 **senza** essere il giorno esatto: è il caso del recupero |
| `srv-30` | +30 | +30 | soglia esatta, entrambi i tipi sullo stesso dispositivo |
| `srv-29` | +29 | — | recupero della soglia 30 |
| `srv-90` | +90 | — | soglia più larga |
| `srv-91` | +91 | — | **fuori** da ogni finestra: non deve comparire |
| `srv-scaduto` | −10 | — | già scaduto: escluso dallo scheduler, resta nella vista Scadenze |
| `srv-scaduto-ieri` | −1 | — | confine: `days_remaining == -1` è fuori |
| `srv-senza-date` | — | — | campi vuoti |
| `srv-data-rotta` | `in attesa` | `2026-13-45` | testo non interpretabile: si ignora, non fa cadere il giro |
| `dup-a`, `dup-b` | +7 / +30 | — | **stesso `id` di business, `_uid` diversi**: due promemoria distinti |
| `srv-<iniezione>` | +7 | — | nome con HTML e intestazioni: deve restare testo del corpo |

I due dispositivi con lo stesso `id` sono deliberati: l'identità di un promemoria
è l'`_uid`, non il codice scritto dall'utente. Se il worker raggruppasse per `id`,
uno dei due non riceverebbe l'avviso — e con inventari importati da fogli di
calcolo gli `id` ripetuti sono la norma, non l'eccezione.

## Uso

```bash
python fixtures/expiry/build.py --days-from 2026-08-10 > /tmp/inv.json
```

Nei test si chiama direttamente `build_inventory(reference_date)`.
