# Note di licenza per i componenti di terze parti

Questa cartella redistribuisce software di terze parti in forma binaria/minificata.
Sotto: attribuzione e testo integrale delle licenze, come richiesto dalla licenza MIT
(«The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software»).

## Componenti redistribuiti

| Componente | Versione | Licenza | File in questa cartella |
|---|---|---|---|
| React | 18.3.1 | MIT | `react.production.min.js` |
| React DOM | 18.3.1 | MIT | `react-dom.production.min.js` |
| scheduler | 0.23.2 | MIT | *incluso dentro* `react-dom.production.min.js` |

`scheduler` non è un file separato: il bundle UMD di `react-dom` lo incorpora. Va comunque
attribuito, perché viene redistribuito insieme al resto.

Titolare del copyright per tutti e tre: Meta Platforms, Inc. e affiliate
(il testo delle licenze riporta la denominazione storica «Facebook, Inc. and its affiliates»).

Progetto upstream: <https://github.com/facebook/react>

Le tre licenze sono **byte-identiche** (SHA-256 `52412d7b…`), quindi il testo è riportato
una volta sola.

## MIT License — React, React DOM, scheduler

```
MIT License

Copyright (c) Facebook, Inc. and its affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Obbligo in fase di distribuzione

Questi file vanno nell'immagine `tsm-web` insieme ai `.js` che coprono
(vedi l'allowlist in `BACKEND-PLAN.md` §6): l'immagine è la forma in cui il software
viene distribuito, quindi le note di licenza devono viaggiare con essa.

## Altri componenti del pacchetto (non di terze parti in questa cartella)

- `handoff/fonts/` — Public Sans e Roboto Mono: font con licenza propria (OFL/Apache-2.0
  a monte). **Da verificare e documentare separatamente**: non sono coperti da questo file.
- `handoff/support.js`, `handoff/xlsx.js` — prodotti nel contesto del progetto, non
  redistribuzione di terze parti.
