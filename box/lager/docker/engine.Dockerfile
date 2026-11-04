# syntax=docker/dockerfile:1.4
# The bench engine service: lager's bench reasoning (suitability, renderings,
# citations, the API reference) answering over HTTP for a host that stores
# bench manifests, with no box attached. See lager.mcp.engine_service.
#
# Build from box/lager, the same context as box.Dockerfile:
#   docker build -f docker/engine.Dockerfile -t lager-bench-engine box/lager
#
# Same pinned base as the box image, so one Dependabot bump moves both.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG LAGER_ENGINE_VERSION=dev
ARG LAGER_ENGINE_REVISION=
ARG LAGER_ENGINE_SOURCE=https://github.com/lagerdata/lager

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LAGER_ENGINE_HOST=0.0.0.0 \
    LAGER_ENGINE_PORT=8200 \
    LAGER_ENGINE_VERSION=${LAGER_ENGINE_VERSION} \
    LAGER_ENGINE_REVISION=${LAGER_ENGINE_REVISION}

LABEL org.opencontainers.image.title="lager-bench-engine" \
      org.opencontainers.image.version="${LAGER_ENGINE_VERSION}" \
      org.opencontainers.image.revision="${LAGER_ENGINE_REVISION}" \
      org.opencontainers.image.source="${LAGER_ENGINE_SOURCE}" \
      org.opencontainers.image.licenses="Apache-2.0"

COPY docker/requirements-engine.txt /tmp/requirements-engine.txt
RUN pip install --no-cache-dir -r /tmp/requirements-engine.txt && rm /tmp/requirements-engine.txt

# Only the MCP package, under an EMPTY lager/__init__.py: the real package
# __init__ imports every driver, and the engine needs none of them.
COPY mcp /app/lager/mcp
RUN touch /app/lager/__init__.py \
    && find /app/lager -name __pycache__ -prune -exec rm -rf {} + \
    && python -c "import lager.mcp.engine_service.app" \
    && useradd --system --no-create-home --uid 10001 engine

USER engine
EXPOSE 8200

# No curl in the slim image; the standard library is enough for a probe.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('LAGER_ENGINE_PORT','8200'), timeout=2)"]

CMD ["python", "-m", "lager.mcp.engine_service"]
