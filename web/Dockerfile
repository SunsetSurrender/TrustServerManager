# Immagine web: SOLO i file in allowlist (§6 del piano).
#
# Il contenuto si copia file per file, non con `COPY frontend/ .`: quello che non è
# nell'immagine non può essere servito per sbaglio da una regola nginx scritta
# male in futuro. È la difesa che regge nel tempo; il default-deny in nginx.conf è
# il secondo livello.
#
# NON copiati, di proposito:
#   - Sale Server Pomezia (standalone).html  → contiene l'inventario inline e
#     scavalcherebbe auth e ruoli; inoltre è solo-sviluppo (§5)
#   - inventario.js  → seed in chiaro senza autenticazione. Il runtime prende i
#     dati da GET /api/inventory.
#   - README.md, vendor/README.md, vendor/SHA256SUMS → documentazione interna
#
# Base pinnata per DIGEST, come backend/Dockerfile e come il servizio `db` in
# compose.yaml. Questa era l'unica delle tre a essere pinnata per TAG, e la
# differenza non e' formale: `1.27-alpine` e' un tag MOBILE, che Docker Hub
# ripubblica a ogni 1.27.x e a ogni ricostruzione di alpine.
#
# ⚠ Non e' un pericolo teorico: la qualificazione della 2J ha fatto `docker pull
# nginx:1.27-alpine` su una macchina che aveva gia' costruito tsm-web, e Docker ha
# risposto «Downloaded newer image». Cioe' lo stesso commit di git costruiva due
# immagini web diverse in due giorni diversi, e l'identita' del rilascio non era
# ricostruibile dal repository — che e' esattamente la cosa che il commento di
# backend/Dockerfile dichiara di non voler permettere («in rete chiusa un build non
# riproducibile significa non poter ricostruire la versione che funzionava», §6).
#
# Aggiornare questo digest e' un atto deliberato e visibile in revisione, come per
# le altre due basi. Per leggerlo:
#     docker pull nginx:1.27-alpine
#     docker image inspect nginx:1.27-alpine --format '{{index .RepoDigests 0}}'
#
# nginx:1.27-alpine
ARG NGINX_IMAGE=nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
FROM ${NGINX_IMAGE}

# Il contesto di build è la radice del repository (serve frontend/), quindi i
# percorsi sono relativi a essa, non alla cartella del Dockerfile.
COPY web/nginx.conf /etc/nginx/nginx.conf

WORKDIR /srv/app
# Forma JSON di COPY: il nome del file contiene spazi, e l'escape con backslash
# non è supportato dalla forma shell.
COPY ["frontend/Sala Server v2.dc.html", "./index.html"]
COPY ["frontend/support.js", "./support.js"]
COPY ["frontend/xlsx.js", "./xlsx.js"]
COPY ["frontend/identity.js", "./identity.js"]
# Fase 2G: il modello semantico condiviso col backend (BACKEND-PLAN.md §8.50).
COPY ["frontend/domain.js", "./domain.js"]
COPY ["frontend/api.js", "./api.js"]
# Fase 2H: il client delle interrogazioni relazionali (BACKEND-PLAN.md §8.51).
# ⚠ Un modulo dimenticato qui non è un difetto sottile: il browser riceve un 404
# sull'import e l'applicazione non parte. È già successo con domain.js.
COPY ["frontend/queries.js", "./queries.js"]
COPY ["frontend/trust-logo.png", "./trust-logo.png"]
COPY ["frontend/trust-logo-dark.png", "./trust-logo-dark.png"]
COPY ["frontend/fonts/", "./fonts/"]
COPY ["frontend/vendor/react.production.min.js", "./vendor/react.production.min.js"]
COPY ["frontend/vendor/react-dom.production.min.js", "./vendor/react-dom.production.min.js"]
# La licenza MIT richiede che l'avviso accompagni le copie del software, e
# l'immagine È la copia distribuita.
COPY ["frontend/vendor/THIRD-PARTY-NOTICES.md", "./vendor/THIRD-PARTY-NOTICES.md"]

# nginx alpine ha già l'utente `nginx` (uid 101). Su porta 8080 perché un
# processo non-root non può aprire la 80.
USER 101:101
EXPOSE 8080
