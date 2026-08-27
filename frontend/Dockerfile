# Project SETU — console image.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, matching the backend:
#     docker build -f frontend/Dockerfile -t setu-frontend .
#
# Base images pinned by digest so a rebuild months from now produces the bytes we
# tested rather than whatever the tag points at by then.

# ── build stage ─────────────────────────────────────────────────────────────────
FROM node@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

WORKDIR /build

# Dependencies first, so a source-only change does not reinstall the tree.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./

# Vite inlines env vars at build time, so the API base URL is baked into the bundle
# and cannot be changed by restarting the container. Default is same-origin, which
# is what the nginx proxy below serves; override only when the API lives elsewhere.
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

# ── serve stage ─────────────────────────────────────────────────────────────────
#
# nginx-unprivileged, NOT the stock nginx image. The stock image renders
# /etc/nginx/templates/*.template in an entrypoint step that requires root. Adding
# `USER nginx` to it makes that step silently do nothing: the container starts,
# prints healthy-looking worker logs, serves the default nginx page on port 80, and
# never applies our configuration at all. This variant is built to run unprivileged
# on 8080 and still processes templates.
FROM nginxinc/nginx-unprivileged@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0 AS serve

# The upstream is templated so one image serves both compose (service name) and a
# platform deployment (internal hostname) without a rebuild.
# PORT is templated into the `listen` directive. Platforms that assign a port at
# runtime (Railway, Cloud Run, Heroku) route to $PORT and nothing else, so a
# hard-coded 8080 makes the console unreachable there while the logs look healthy.
# The default keeps compose and local runs on 8080 exactly as before.
#
# It must be a real environment variable, not only a compose setting: the image's
# envsubst step substitutes only variables that are actually set, and leaves anything
# else in the config verbatim -- which nginx then rejects with
# `host not found in "${PORT}" of the "listen" directive`.
ENV SETU_API_HOST=backend \
    SETU_API_PORT=8000 \
    PORT=8080

# Root only long enough to place files the unprivileged user must read.
USER root
COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /build/dist /usr/share/nginx/html
RUN chown -R nginx:nginx /usr/share/nginx/html /etc/nginx/conf.d

USER nginx
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 || exit 1
