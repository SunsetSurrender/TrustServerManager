# Immagine dell'API. Base pinnata per digest: in rete chiusa un build non riproducibile
# significa non poter ricostruire la versione che funzionava (BACKEND-PLAN.md §6).
# python:3.13-slim
ARG PYTHON_IMAGE=python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# ---------- build: le wheel si compilano qui, non nell'immagine finale ----------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ---------- runtime ----------
FROM ${PYTHON_IMAGE}

# PYTHONDONTWRITEBYTECODE: obbligatorio, non estetico. Il container gira con
# read_only: true e Python non potrebbe scrivere __pycache__.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:${PATH}"

# Utente non-root con uid/gid fissi: servono per poter dare al file del secret sull'host
# un owner corrispondente (vedi backend/README.md).
RUN groupadd --gid 10001 tsm \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tsm

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=root:root app ./app
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root alembic.ini ./alembic.ini

# Il codice resta di root e in sola lettura per l'utente che esegue: il processo non
# può riscrivere se stesso.
USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
