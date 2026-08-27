# Project SETU — backend image.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not backend/:
#     docker build -f backend/Dockerfile -t setu-backend .
#
# That is not incidental. `services/common/paths.py` resolves DATA_DIR and
# REPORTS_DIR relative to the *parent* of the backend directory, because footage,
# seeds and evidence are project artefacts shared with the frontend rather than
# Python package data. Building from backend/ alone cannot reach them, and flattening
# the layout would mean the image and the repository disagree about where things live.
#
# Base images are pinned by digest, not tag. A jury re-running this months after
# submission must get the bytes we tested; `python:3.12-slim` will not be the same
# image by then. `make pin-digests` refreshes these deliberately.

# ── build stage ─────────────────────────────────────────────────────────────────
FROM python@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-only toolchain. None of it reaches the runtime image, which is the point of
# separating the stages: a compiler in a production container is an exploitation
# primitive that serves no purpose once the wheels are built.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt

# ── runtime stage ───────────────────────────────────────────────────────────────
FROM python@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    HOME=/home/setu

# Runtime libraries only. libGL and libglib are what OpenCV links against; without
# them `import cv2` fails at container start with an error that reads as a Python
# problem rather than a missing shared object.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpq5 \
        libgl1 \
        libglib2.0-0 \
        curl \
        bash \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

# Non-root. The application has no reason to write outside its own data directory,
# and running as root turns a code-execution bug into a host problem.
RUN groupadd --system --gid 10001 setu \
    && useradd --system --uid 10001 --gid setu --create-home --shell /usr/sbin/nologin setu

# Mirrors the repository layout so paths.py resolves identically in the container
# and on a developer machine.
#   /srv/setu/backend   ← BACKEND_ROOT
#   /srv/setu/data      ← DATA_DIR
#   /srv/setu/reports   ← REPORTS_DIR
WORKDIR /srv/setu/backend

COPY --chown=setu:setu backend/ /srv/setu/backend/
COPY --chown=setu:setu data/ /srv/setu/data/

# Evidence crops are baked in. Without them the Journey and Alert screens render
# empty image boxes on a fresh deploy, which reads to a judge as broken software
# rather than as a storage decision still to be made.
RUN mkdir -p /srv/setu/reports/evidence /srv/setu/data/evidence/crops \
    && chown -R setu:setu /srv/setu \
    && chmod +x /srv/setu/backend/docker-entrypoint.sh

USER setu

# Model weights download on first use. Fetching them at build time means a cold
# container serves its first request immediately instead of stalling for ~15 MB
# while a judge waits.
#
# Retried, and each model reported separately. An earlier version wrapped the whole
# prefetch in `|| echo skipped`, so when the detector cached and the OCR model hit a
# transient SSL error the build still printed "models cached" and the image shipped
# with half a cache. The runtime download then failed inside the demo ingest, which
# surfaced as "demo ingest failed" with an SSL traceback several layers away.
# The script's exit code is now respected. `|| true` used to discard it, so when the
# recogniser prefetch broke outright -- ModuleNotFoundError, not a network blip -- the
# build printed its warning, returned 1, and the image shipped anyway with only the
# detector cached. Nothing downstream noticed until the container was inspected by
# hand. A build step whose result is thrown away is not a build step.
#
# Offline or flaky-network builds can still proceed explicitly:
#   docker build --build-arg REQUIRE_MODEL_CACHE=0 ...
# which accepts an image that downloads its models on first inference.
ARG REQUIRE_MODEL_CACHE=1
RUN if python scripts/prefetch_models.py; then \
      echo "model cache complete"; \
    elif [ "$REQUIRE_MODEL_CACHE" = "0" ]; then \
      echo "WARNING: shipping without a complete model cache (REQUIRE_MODEL_CACHE=0)"; \
    else \
      echo "ERROR: model prefetch failed and REQUIRE_MODEL_CACHE=1."; \
      echo "       Rebuild with --build-arg REQUIRE_MODEL_CACHE=0 to accept a runtime download."; \
      exit 1; \
    fi

EXPOSE 8000

# Hits the liveness endpoint rather than checking the process exists: a uvicorn
# worker that has lost its database connection is running and useless.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["bash", "/srv/setu/backend/docker-entrypoint.sh"]
