# Immagine web: SOLO i file in allowlist (§6 del piano).
#
# Il contenuto si copia file per file, non con `COPY handoff/ .`: quello che non è
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
# nginx:1.27-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine
FROM ${NGINX_IMAGE}

# Il contesto di build è la radice del repository (serve handoff/), quindi i
# percorsi sono relativi a essa, non alla cartella del Dockerfile.
COPY web/nginx.conf /etc/nginx/nginx.conf

WORKDIR /srv/app
# Forma JSON di COPY: il nome del file contiene spazi, e l'escape con backslash
# non è supportato dalla forma shell.
COPY ["handoff/Sala Server v2.dc.html", "./index.html"]
COPY ["handoff/support.js", "./support.js"]
COPY ["handoff/xlsx.js", "./xlsx.js"]
COPY ["handoff/identity.js", "./identity.js"]
# Fase 2G: il modello semantico condiviso col backend (BACKEND-PLAN.md §8.50).
COPY ["handoff/domain.js", "./domain.js"]
COPY ["handoff/api.js", "./api.js"]
# Fase 2H: il client delle interrogazioni relazionali (BACKEND-PLAN.md §8.51).
# ⚠ Un modulo dimenticato qui non è un difetto sottile: il browser riceve un 404
# sull'import e l'applicazione non parte. È già successo con domain.js.
COPY ["handoff/queries.js", "./queries.js"]
COPY ["handoff/trust-logo.png", "./trust-logo.png"]
COPY ["handoff/trust-logo-dark.png", "./trust-logo-dark.png"]
COPY ["handoff/fonts/", "./fonts/"]
COPY ["handoff/vendor/react.production.min.js", "./vendor/react.production.min.js"]
COPY ["handoff/vendor/react-dom.production.min.js", "./vendor/react-dom.production.min.js"]
# La licenza MIT richiede che l'avviso accompagni le copie del software, e
# l'immagine È la copia distribuita.
COPY ["handoff/vendor/THIRD-PARTY-NOTICES.md", "./vendor/THIRD-PARTY-NOTICES.md"]

# nginx alpine ha già l'utente `nginx` (uid 101). Su porta 8080 perché un
# processo non-root non può aprire la 80.
USER 101:101
EXPOSE 8080
