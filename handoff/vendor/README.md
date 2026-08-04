# vendor/ — dipendenze locali per la rete chiusa

`support.js` carica React da unpkg.com a runtime
([support.js:1143-1146](../support.js#L1143)). In rete chiusa quel fetch fallisce e
l'applicazione **non parte** (`[dc] failed to load React or boot`).

Questi due file eliminano la dipendenza. Vengono caricati prima di `support.js`
nel `<head>` di `Sala Server v2.dc.html`; il runtime controlla

```js
if (w.React && w.ReactDOM) return Promise.resolve();   // support.js:1840
```

e quindi **salta la CDN da solo**. Nessuna modifica a `support.js`.

`@babel/standalone` non serve: è richiesto solo da `x-import` per i moduli `.jsx`
(`support.js:1176-1191`), e l'applicazione non usa `x-import` in nessun punto.

## Provenienza

| File | Origine | Byte |
|---|---|---|
| `react.production.min.js` | `https://unpkg.com/react@18.3.1/umd/react.production.min.js` | 10 751 |
| `react-dom.production.min.js` | `https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js` | 131 835 |

Scaricati il 2026-08-03.

## SHA-256

```
d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd  react.production.min.js
35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d  react-dom.production.min.js
```

Verifica:

```bash
sha256sum -c SHA256SUMS
```

```powershell
Get-FileHash .\react.production.min.js     -Algorithm SHA256   # d949f1c3...
Get-FileHash .\react-dom.production.min.js -Algorithm SHA256   # 35f4f974...
```

## SHA-384 / SRI — corrispondenza con gli hash attesi dal runtime

`support.js` incorpora gli hash SRI dei file che avrebbe scaricato. I file in questa
cartella corrispondono **esattamente** a quei digest, quindi sono byte-identici a
quello che il runtime avrebbe caricato dalla CDN:

| File | SHA-384 (base64) | Atteso da `support.js` |
|---|---|---|
| `react.production.min.js` | `DGyLxAyjq0f9SPpVevD6IgztCFlnMF6oW/XQGmfe+IsZ8TqEiDrcHkMLKI6fiB/Z` | riga 1144 ✔ |
| `react-dom.production.min.js` | `gTGxhz21lVGYNMcdJOyq01Edg0jhn/c22nsx0kyqP0TxaV5WVdsSH1fSDUf5YJj1` | riga 1146 ✔ |

Questo è il controllo di provenienza che conta. Da notare che `react-dom@18.3.1`
dichiara internamente `ReactDOM.version === "18.3.1-next-f1338f8080-20240426"`:
è il valore che porta l'artefatto 18.3.1 ufficiale, non un errore di versione.

## Aggiornamento

Se si aggiorna React, i digest SRI dentro `support.js` **non** vanno toccati: sono
usati solo sul percorso CDN, che qui non viene mai imboccato. Va invece rigenerato
questo file e riverificato l'avvio offline (`tools/offline-boot-test.py`).
