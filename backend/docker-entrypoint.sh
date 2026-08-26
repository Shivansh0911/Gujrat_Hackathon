#!/usr/bin/env bash
#
# Backend container entrypoint.
#
# The ORDER here is load-bearing, and two decisions in it are the difference between
# a deployment that works and one that only looks like it does:
#
#   1. `create_app_role.py` runs AFTER migrations and BEFORE the API starts.
#      Postgres creates POSTGRES_USER as a superuser, and a superuser carries
#      rolbypassrls -- it ignores every row-level security policy however the policy
#      is written. So the API must never connect as that role, and the unprivileged
#      one must exist before uvicorn does. It cannot run *before* migrations, because
#      its table grants need the tables: an earlier version of this file did exactly
#      that and the container crash-looped on `relation "department" does not exist`.
#      The RLS policies themselves reference session settings, not roles, so nothing
#      in the migrations depends on the role existing first.
#
#   2. Seeding is gated SEPARATELY for the registry and for demo detections. An
#      earlier version gated both on the camera count, so a run that seeded cameras
#      and then failed left the next start reporting "registry already populated" and
#      skipping demo ingest entirely -- producing a deployed instance with cameras on
#      the map and a permanently empty journey view.
#
# Every step is safe to re-run. A restart is a normal event, not a recovery scenario.

set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*" >&2; }

: "${SETU_DATABASE_URL:?SETU_DATABASE_URL must be set}"
: "${SETU_JWT_SECRET:?SETU_JWT_SECRET must be set}"

# Migrations connect as the schema owner; the API connects as the unprivileged role.
# Fall back to the app URL only for a single-role local run, and say so loudly.
if [ -z "${SETU_MIGRATION_DATABASE_URL:-}" ]; then
  log "WARNING: SETU_MIGRATION_DATABASE_URL unset; using SETU_DATABASE_URL for migrations."
  log "         In production these should be different roles."
  export SETU_MIGRATION_DATABASE_URL="$SETU_DATABASE_URL"
fi

count_rows() {
  python -c "
import os, sys
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['SETU_MIGRATION_DATABASE_URL'])
with engine.connect() as conn:
    print(conn.execute(text('SELECT count(*) FROM ' + sys.argv[1])).scalar_one())
engine.dispose()
" "$1"
}

# ── 1. Wait for Postgres ────────────────────────────────────────────────────────
log "waiting for the database..."
python -c "
import os, sys, time
from sqlalchemy import create_engine, text

url = os.environ['SETU_MIGRATION_DATABASE_URL']
deadline = time.monotonic() + 120
attempt = 0
while True:
    attempt += 1
    try:
        engine = create_engine(url, connect_args={'connect_timeout': 5})
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        engine.dispose()
        print(f'[entrypoint] database reachable after {attempt} attempt(s)', file=sys.stderr)
        break
    except Exception as exc:
        if time.monotonic() > deadline:
            print(f'[entrypoint] database unreachable after 120s: {exc}', file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
"

# ── 2. Migrations ───────────────────────────────────────────────────────────────
# Run as the schema owner. The unprivileged role deliberately cannot create tables.
log "applying migrations..."
alembic upgrade head

# ── 3. Unprivileged application role ────────────────────────────────────────────
# After migrations (its grants need the tables) and before uvicorn (the API must
# never connect as a superuser). See the header.
if [ -n "${SETU_APP_DB_PASSWORD:-}" ]; then
  log "ensuring the unprivileged application role..."
  python scripts/create_app_role.py
else
  log "WARNING: SETU_APP_DB_PASSWORD unset. The API will connect with whatever role"
  log "         SETU_DATABASE_URL names. If that role is a superuser, ROW-LEVEL"
  log "         SECURITY IS INERT and departments are not isolated."
fi

# ── 4a. Registry, gated on the camera count ─────────────────────────────────────
CAMERA_COUNT="$(count_rows camera)"
if [ "$CAMERA_COUNT" = "0" ]; then
  log "registry is empty; seeding departments and camera coordinates..."
  python -m services.registry.seed
else
  log "registry already holds ${CAMERA_COUNT} camera(s); skipping registry seed"
fi

# ── 4b. Demo detections, gated independently ────────────────────────────────────
DETECTION_COUNT="$(count_rows detection)"
if [ "${SETU_SEED_DEMO:-1}" = "1" ] && [ "$DETECTION_COUNT" = "0" ]; then
  if compgen -G "/srv/setu/data/own_feed/*.mp4" > /dev/null; then
    log "no detections yet; running ANPR over the bundled demo clip..."
    # Retried: the first attempt can fail on a transient model download if the
    # build-time cache is incomplete, and a deployed instance with an empty journey
    # view is the worst possible outcome for a judge clicking the hosted URL.
    for attempt in 1 2 3; do
      if python scripts/seed_demo.py --frames "${SETU_DEMO_FRAMES:-900}"; then
        break
      fi
      log "demo ingest attempt ${attempt} failed; retrying in 10s"
      sleep 10
    done
    python scripts/seed_watchlist.py --reset \
      || log "watchlist seed failed; continuing"
    python -c "
import sys
sys.path.insert(0, '.')
from services.analytics.matcher import scan_detections
from services.api.db import get_sessionmaker
from services.api.tenancy import set_admin_context

s = get_sessionmaker()()
set_admin_context(s)
stats = scan_detections(s)
s.commit()
s.close()
print(f'[entrypoint] {stats.alerts_created} alert(s) raised', file=sys.stderr)
" || true
  else
    log "no own-feed footage in the image; skipping demo ingest"
  fi
else
  log "detections present (${DETECTION_COUNT}) or demo seeding disabled; skipping ingest"
fi

log "starting API on port ${PORT:-8000}"
exec uvicorn services.api.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --workers "${SETU_WORKERS:-2}"
