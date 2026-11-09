# syntax=docker/dockerfile:1
# Packages Tracker: the API and the UI in one image (phase 5).
#
#   docker build -t packages-tracker .
#
# It builds on amd64 and arm64 (Oracle's A1 VMs) alike: the base images and
# every Python wheel used exist for both. deploy/compose.cloud.yml builds it
# on the VM itself, so no registry is involved.

# ---- the UI ---------------------------------------------------------------------
FROM node:24-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- the app --------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git clones the repos being scanned; tini reaps the processes scans start
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The dependencies first, so a code change doesn't reinstall them. They come
# from the lock (exact versions, the ones CI tests); the app itself runs from
# /app and isn't pip-installed.
COPY backend/requirements.lock ./
RUN pip install -r requirements.lock

COPY backend/ ./
COPY --from=ui /ui/dist ./static

# a user of its own; /data (uploads, caches) is a volume it owns
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown app:app /data

ENV APP_ENV=prod \
    STATIC_DIR=/app/static \
    UPLOAD_DIR=/data/uploads \
    APT_INDEX_CACHE_DIR=/data/cache/apt-index \
    EOL_DATA_DIR=/data/legacy

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

ENTRYPOINT ["tini", "--"]
# One process: the scheduler, the scan queue and the rate limits live in its
# memory, so never add --workers. --proxy-headers takes the visitor's address
# from Caddy (the only thing that can reach this port) for the rate limits.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
