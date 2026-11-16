# Base images pinned by digest (multi-arch index), so a rebuild of the same commit uses the same bases; bump tag and
# digest together (Dependabot docker, or `docker buildx imagetools inspect <tag>`) and requalify (checklist §1).
ARG NODE_IMAGE=node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
ARG PYTHON_IMAGE=python:3.13.18-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f
FROM ${NODE_IMAGE} AS web
WORKDIR /build
COPY package.json package-lock.json ./
ARG CI_NPM_REGISTRY=""
RUN if [ -n "$CI_NPM_REGISTRY" ]; then export npm_config_registry="$CI_NPM_REGISTRY"; fi \
    && npm ci --ignore-scripts
COPY apps/web ./apps/web
RUN ./node_modules/.bin/tsc -p apps/web/tsconfig.json && cp -R apps/web/public/. apps/web/dist/
FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements ./requirements
# Debian security fixes the official image has not picked up yet (e.g. OpenSSL); the release scan checks the result.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
ARG CI_PIP_INDEX_URL=""
ARG CI_PIP_TRUSTED_HOST=""
RUN if [ -n "$CI_PIP_INDEX_URL" ]; then \
      export PIP_INDEX_URL="$CI_PIP_INDEX_URL" PIP_TRUSTED_HOST="$CI_PIP_TRUSTED_HOST"; \
    fi \
    && python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements/postgres.lock \
    && groupadd --gid 10001 rota && useradd --uid 10001 --gid 10001 --no-create-home rota
COPY apps/process_memory.py ./apps/process_memory.py
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/__init__.py ./apps/__init__.py
COPY packages ./packages
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY scripts/manage.py scripts/preflight.py scripts/group_rules.py scripts/preload_web.py ./scripts/
COPY scripts/initial_seed.py scripts/system_command.py scripts/warm_read_projections.py scripts/generate_vapid_key.py ./scripts/
COPY --from=web /build/apps/web/dist ./apps/web/dist
# The installable app's icon falls back to the approved emblem until a department uploads one (services/pwa.py).
COPY apps/web/public/assets/brand/emblem.png ./apps/web/public/assets/brand/emblem.png
# About shows what this image was built from (apps/api/version.py build()); the web bundle is stamped with the same
# commit (preload_web.py). Declared late, so only the cheap preload step below reruns per commit.
ARG ROTA_BUILD_COMMIT=""
ARG ROTA_BUILD_DATE=""
ENV ROTA_BUILD_COMMIT=${ROTA_BUILD_COMMIT} ROTA_BUILD_DATE=${ROTA_BUILD_DATE}
RUN python scripts/preload_web.py apps/web/dist
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
