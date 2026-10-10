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

: "${SETU_JWT_SECRET:?SETU_JWT_SECRET must be set}"

# ── Database URLs ───────────────────────────────────────────────────────────────
#
# Two roles, deliberately. Migrations connect as the schema owner; the API connects
# as `setu_app`, which is NOSUPERUSER/NOBYPASSRLS so that row-level security actually
# binds. Getting this wrong is the most consequential misconfiguration available
# here: a superuser carries `rolbypassrls` and silently ignores every policy, so
# departments stop being isolated while every isolation test still passes.
#
# Managed platforms hand out one superuser URL, usually as DATABASE_URL. Accept it
# for MIGRATIONS -- that is exactly the right role for them -- and then *derive* the
# application URL from it by substituting the unprivileged role and its password.
# Deriving rather than accepting means the API cannot end up on the superuser URL by
# someone pasting the platform variable into both fields.
if [ -z "${SETU_MIGRATION_DATABASE_URL:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
  log "SETU_MIGRATION_DATABASE_URL unset; using the platform's DATABASE_URL for migrations."
  export SETU_MIGRATION_DATABASE_URL="$DATABASE_URL"
fi

if [ -z "${SETU_DATABASE_URL:-}" ]; then
  if [ -n "${SETU_MIGRATION_DATABASE_URL:-}" ] && [ -n "${SETU_APP_DB_PASSWORD:-}" ]; then
    log "SETU_DATABASE_URL unset; deriving the application URL for role setu_app."
    SETU_DATABASE_URL="$(python -c "
import os, sys
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, '/srv/setu/backend')
from services.common.dburl import normalise_pg_url

parts = urlsplit(normalise_pg_url(os.environ['SETU_MIGRATION_DATABASE_URL']))
host = parts.hostname or 'localhost'
netloc = 'setu_app:' + quote(os.environ['SETU_APP_DB_PASSWORD'], safe='')
netloc += '@' + ('[' + host + ']' if ':' in host else host)
if parts.port:
    netloc += ':' + str(parts.port)
print(urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)))
")"
    export SETU_DATABASE_URL
  else
    log "FATAL: set SETU_DATABASE_URL, or provide SETU_MIGRATION_DATABASE_URL (or"
    log "       DATABASE_URL) together with SETU_APP_DB_PASSWORD so it can be derived."
    exit 1
  fi
fi

# Fall back to the app URL for migrations only in a single-role local run, and say so.
if [ -z "${SETU_MIGRATION_DATABASE_URL:-}" ]; then
  log "WARNING: SETU_MIGRATION_DATABASE_URL unset; using SETU_DATABASE_URL for migrations."
  log "         In production these should be different roles."
  export SETU_MIGRATION_DATABASE_URL="$SETU_DATABASE_URL"
fi

# Accept the libpq form (`postgres://`, `postgresql://`) that every managed Postgres
# emits, and rewrite it to name psycopg 3. Without this the first connection fails
# with ModuleNotFoundError: psycopg2, long after the container reports healthy.
eval "$(python -c "
import os, sys
sys.path.insert(0, '/srv/setu/backend')
from services.common.dburl import normalise_pg_url
for key in ('SETU_DATABASE_URL', 'SETU_MIGRATION_DATABASE_URL'):
    value = normalise_pg_url(os.environ.get(key))
    if value:
        print(\"export %s='%s'\" % (key, value.replace(\"'\", \"'\\\\''\")))
")"

# Counting a table that is under row-level security requires saying who you are.
#
# `detection` and `alert` carry FORCE ROW LEVEL SECURITY, and its failure mode is
# deliberately "no rows" rather than an error -- forgetting the context must never
# expose the whole estate. This counted with no context set, so on a database holding
# twenty detections it returned zero, and the gate whose entire job is to skip seeding
# fired on every boot instead. The security control was working; the check was lying.
#
# `setu.is_admin` is the same session GUC every background job elevates with, set
# SESSION-scoped so it survives the statement.
count_rows() {
  python -c "
import os, sys
from sqlalchemy import create_engine, text
engine = create_engine(os.environ['SETU_MIGRATION_DATABASE_URL'])
with engine.connect() as conn:
    conn.execute(text(\"SELECT set_config('setu.is_admin', 'on', false)\"))
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

# ── 4b. Demo detections, in the BACKGROUND ──────────────────────────────────────
# This must never delay the port bind, and it used to.
#
# A platform decides a web service is dead if it has not bound its port within a scan
# window. Running ANPR over the bundled clip takes minutes on a shared CPU, and it sat
# here in the foreground ahead of `exec uvicorn` -- so the moment the clip was actually
# shipped in the image, the branch that had always been skipped started running, the
# port was never bound, the platform killed the container, and the restart seeded from
# the beginning again. A crash loop whose visible symptom was "No open ports detected".
#
# Migrations and the application role stay in the foreground above: they are fast, and
# they are correctness and security preconditions for serving any request at all.
# Seeding demonstration data is neither. It is opportunistic, it is idempotent, and it
# belongs behind the port bind.
seed_demo_data() {
  DETECTION_COUNT="$(count_rows detection || echo unknown)"
  if [ "${SETU_SEED_DEMO:-1}" != "1" ]; then
    log "demo seeding disabled; skipping ingest"
    return 0
  fi
  if [ "$DETECTION_COUNT" != "0" ]; then
    log "detections present (${DETECTION_COUNT}); skipping ingest"
    return 0
  fi
  if ! compgen -G "/srv/setu/data/own_feed/*.mp4" > /dev/null; then
    log "no own-feed footage in the image; skipping demo ingest"
    return 0
  fi

  log "no detections yet; running ANPR over the bundled demo clip in the background..."
  # Retried: the first attempt can fail on a transient model download if the build-time
  # cache is incomplete, and a deployed instance with an empty journey view is the worst
  # possible outcome for a judge clicking the hosted URL.
  for attempt in 1 2 3; do
    if python scripts/seed_demo.py --frames "${SETU_DEMO_FRAMES:-900}"; then
      break
    fi
    log "demo ingest attempt ${attempt} failed; retrying in 10s"
    sleep 10
  done
  python scripts/seed_watchlist.py --reset || log "watchlist seed failed; continuing"
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
  log "background demo seeding finished"
}

# Vehicle sightings, which are what the appearance search runs on.
#
# Gated on its own table rather than on `detection`, because the two are seeded
# independently and an instance that already has plate reads would otherwise never get
# sightings -- which is exactly the state the first deployment of the search screen
# landed in: the page worked and had nothing to search.
#
# The offsets are the real travel times between these four replay cameras, the same
# ones `seed_demo.py` uses for its plate reads, and `--align-to-reads` anchors this
# timeline to those reads rather than to the clock. Without that the two halves sit
# hours apart, plate linking finds nothing and the search has no seed.
#
# This has to run in the container rather than from a developer machine: the crops it
# writes are served from local disk, so sightings inserted remotely would reference
# images this instance does not have.
#
# 80 frames per camera, down from 600 and then 150. Four cameras at 600 takes ten minutes on a developer
# laptop; the hosted instance has a fraction of a CPU, which puts the same job in the
# hours and spends every one of them competing with the API for the processor a
# reviewer is waiting on. 150 still yields enough vehicles per camera to browse and to
# search between, which is what the screen needs, and SETU_SIGHTING_FRAMES raises it
# wherever there is capacity to spare. Lowered twice because the hosted instance has
# 512 MB and is already running the API: onnxruntime, OpenCV's decode buffers and the
# detector's session have to fit in what is left, and a job killed for memory leaves
# the same zero behind as a job that never ran.
seed_sightings() {
  if [ "${SETU_SEED_DEMO:-1}" != "1" ]; then
    return 0
  fi
  SIGHTING_COUNT="$(count_rows vehicle_sighting || echo unknown)"
  if [ "$SIGHTING_COUNT" != "0" ]; then
    log "vehicle sightings present (${SIGHTING_COUNT}); skipping sighting ingest"
    return 0
  fi
  CLIP=/srv/setu/data/own_feed/demo_clip.mp4
  if [ ! -f "$CLIP" ]; then
    log "no demo clip in the image; skipping sighting ingest"
    return 0
  fi

  # The fixture first, the detector second, and that order is the whole lesson.
  #
  # It used to be the other way round: run detection, fall back to the fixture if it
  # produced nothing. On this instance detection never produced anything -- a fraction
  # of a CPU and 512 MB shared with the API -- and because the job was killed rather
  # than returning, it took the shell that would have run the fallback with it. The
  # fallback never executed, through four deployments, and the only symptom available
  # from outside was a sighting count of zero.
  #
  # So the cheap, deterministic thing runs first and the screen works everywhere. A
  # deployment with capacity sets SETU_SIGHTING_INGEST=1 and gets detection over its
  # own footage on top; the fixture is idempotent, so the two do not fight.
  log "loading the committed sighting fixture..."
  python scripts/seed_sighting_fixture.py >> /tmp/sighting-ingest.log 2>&1 \
    || log "fixture load failed; the appearance search will have nothing to search"

  if [ "${SETU_SIGHTING_INGEST:-0}" = "1" ]; then
    log "SETU_SIGHTING_INGEST=1; also running vehicle detection over the demo clip..."
    python scripts/ingest_sightings.py \
      --file "$CLIP" --camera-ref REPLAY-01 --offset-min 0 \
      --file "$CLIP" --camera-ref REPLAY-02 --offset-min 100 \
      --file "$CLIP" --camera-ref REPLAY-03 --offset-min 300 \
      --file "$CLIP" --camera-ref REPLAY-04 --offset-min 340 \
      --align-to-reads \
      --max-frames "${SETU_SIGHTING_FRAMES:-80}" \
      --analytic-fps 5 \
      >> /tmp/sighting-ingest.log 2>&1 || true
  fi

  log "sighting ingest finished"
}

# Keep the demonstration inside the windows the console opens on.
#
# `seed_demo.py` places the replay timeline seven hours before the moment it runs, and
# the sighting seed anchors to that. Both are relative to their run, and a seed is only
# written once -- so a deployment seeded six weeks ago serves a demonstration dated six
# weeks ago, while the dashboard opens on 24 hours and "Find a vehicle" on two days.
# Every row is present and every query is correct, and the console shows zeroes, which
# on screen is indistinguishable from a platform that does not work.
#
# So this runs on every start, not only on a first seed: it re-anchors the rows the seed
# itself placed and no others, and it is a no-op when they are already in place.
rebase_demo_timeline() {
  if [ "${SETU_SEED_DEMO:-1}" != "1" ]; then
    log "demo seeding disabled; leaving the timeline where it is"
    return 0
  fi
  log "re-anchoring the demonstration timeline"
  python scripts/rebase_demo_timeline.py >> /tmp/rebase-timeline.log 2>&1     || log "timeline rebase failed; see /tmp/rebase-timeline.log"
}

# `set -e` does not apply inside a backgrounded job, so a seeding failure degrades the
# demonstration data without ever taking down a service that is already serving.
# Sequenced, not parallel: the sighting timeline anchors itself to the plate reads, so
# it has to run after they exist, and the rebase has to run after both.
{ seed_demo_data; seed_sightings; rebase_demo_timeline; } &

log "starting API on port ${PORT:-8000}"
exec uvicorn services.api.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --workers "${SETU_WORKERS:-1}"
