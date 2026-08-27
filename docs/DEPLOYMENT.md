# Deployment

The stack runs as three containers: Postgres, the backend API, and nginx serving the
console and proxying `/api`. It has been verified end to end from a clean state —
ten checks, listed in §5.

---

## 1. Local production stack

This is the same stack a platform deploy runs; verify here first.

```bash
# 1. Generate secrets. Nothing has a default: the stack refuses to start rather
#    than silently running on a development value someone forgot to override.
python - <<'PY'
import secrets, pathlib
pw = lambda n=24: secrets.token_urlsafe(n)
pathlib.Path(".env.prod").write_text("\n".join([
    "POSTGRES_DB=setu", "POSTGRES_USER=setu",
    f"POSTGRES_PASSWORD={pw()}",
    f"SETU_APP_DB_PASSWORD={pw()}",
    f"SETU_JWT_SECRET={pw(48)}",
    "SETU_JWT_ISSUER=setu-prod",
    f"SETU_ADMIN_PASSWORD={pw(18)}",
    f"SETU_OPERATOR_PASSWORD={pw(18)}",
    "SETU_CORS_ORIGINS=http://localhost:8080",
    "SETU_PUBLIC_PORT=8080",
    "SETU_GATEWAY_HOST=live.corp8.cloud",
    "SETU_GATEWAY_SCHEME=https",
    "SETU_SEED_DEMO=1", "SETU_DEMO_FRAMES=900",
]) + "\n", encoding="utf-8")
print("wrote .env.prod")
PY

# 2. Build and run
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

First start takes about three minutes: migrations, then ANPR inference over the
bundled demo clip. Watch it with `docker logs -f setu-prod-backend-1`.

Console: **http://localhost:8080** · API docs: **http://localhost:8080/api/docs**

---

## 2. Environment variables, and what breaks without each

| Variable | Required | What happens if it is wrong or missing |
|---|---|---|
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | yes | Compose refuses to start |
| `SETU_APP_DB_PASSWORD` | yes | **The API connects as the Postgres superuser. A superuser carries `rolbypassrls` and ignores every row-level security policy, so departments are not isolated — while every isolation test still passes locally.** The entrypoint logs a warning, but nothing else surfaces it |
| `SETU_MIGRATION_DATABASE_URL` | yes | Alembic connects as the unprivileged role, which cannot create tables; migrations fail |
| `SETU_JWT_SECRET` | yes | The API refuses to start on a placeholder value. A shared or guessable secret means anyone can mint an admin token |
| `SETU_ADMIN_PASSWORD` / `SETU_OPERATOR_PASSWORD` | yes | `POST /auth/login` returns 503 — the API issues no tokens rather than falling back to a default credential |
| `SETU_CORS_ORIGINS` | yes | Cross-origin requests from the console are rejected. Never a wildcard: a wildcard with credentials is refused by browsers and prohibited in `SECURITY.md` |
| `VITE_API_BASE_URL` | build-time | Baked into the bundle by Vite and unchangeable at runtime. Defaults to `/api`, which the nginx proxy serves same-origin. Only set it for a split-origin deployment |
| `SETU_GATEWAY_HOST` | no | Defaults to the competition gateway. Live camera preview fails without it; everything else works |
| `SETU_SEED_DEMO` | no | `0` skips demo ingest and deploys an empty instance |
| `SETU_DEMO_FRAMES` | no | Below ~600 the bundled clip produces no detections and the journey view is empty |
| `SETU_EVIDENCE_SIGNING_KEY` | no | A development key is generated on first use and written to disk. **Set a real one in production**, or exports are signed by a key that changes when the volume does |

---

## 3. Startup order — and why it is what it is

The entrypoint runs these in sequence. The order was arrived at by getting it wrong:

1. **Wait for Postgres** (120 s budget, then fail).
2. **Migrations**, as the schema owner.
3. **`create_app_role.py`** — creates the unprivileged `setu_app` role and grants it
   table-scoped DML. It runs *after* migrations because its grants need the tables;
   an earlier version ran it first and the container crash-looped on
   `relation "department" does not exist`. It runs *before* uvicorn because the API
   must never connect as a superuser.
4. **Registry seed**, gated on the camera count.
5. **Demo ingest**, gated *independently* on the detection count. Gating both on the
   camera count meant a run that seeded cameras and then failed left the next start
   reporting "registry already populated" and skipping ingest entirely — producing
   an instance with cameras on the map and a permanently empty journey view.
6. **uvicorn.**

Every step is idempotent. A restart is a normal event, not a recovery scenario.

---

## 4. Deploying to Railway

> **Status: not yet performed** -- pushing to Railway needs the team's account.
> The procedure below is not guesswork, though: the images were driven the way
> Railway drives them and the failure modes were fixed rather than documented around.
> Verified by simulation, not by reading docs:
>
> * the backend booting with **only** a libpq `DATABASE_URL` and an app password,
>   deriving its own unprivileged URL and ending up connected as `setu_app`
>   (`rolsuper=false`, `rolbypassrls=false`) rather than as the superuser;
> * the same, against a Postgres carrying **PostGIS but neither TimescaleDB nor
>   pgvector** -- the optional extensions are skipped with a warning and the schema
>   builds anyway;
> * the console serving on an injected `PORT=7431`;
> * the console **starting and staying up while the backend does not exist**, then
>   picking it up when it appears.
>
> §5 is what to check against the real URL before quoting it to anyone.

Railway is the recommendation: managed Postgres with PostGIS available, both
services on one platform, one set of secrets. See
`docs/adr/0004-deployment-platform.md` for the trade-offs against Render and Fly.

Two services and a database, all in one Railway project. Both images are built from
this repository's Dockerfiles, so what deploys is what was verified locally.

### 1. The database

Railway's own Postgres image does **not** carry PostGIS, and PostGIS is not optional
here — the registry stores camera positions as `geography(Point,4326)`. Deploy the
database from a custom image instead:

> **New → Docker Image →**
> `timescale/timescaledb-ha:pg16@sha256:92809e70c72a5fd169aa3bb7d9c6b1974d6afdf8da786d890f12ae9e7616a67c`
>
> Variables: `POSTGRES_DB=railway`, `POSTGRES_USER=postgres`,
> `POSTGRES_PASSWORD=<generate one>`. Attach a volume at `/home/postgres/pgdata/data`.

That is the same digest the compose stack runs, and it carries all five extensions.

**If you use a Postgres that only has PostGIS**, the deploy still succeeds. Migration
`0001` treats `postgis`, `pgcrypto` and `pg_trgm` as required and `timescaledb` and
`vector` as optional: the optional ones are logged and skipped, and `detection` is
left as a plain table. Correctness is unaffected; only time-window query performance
at scale is. This was verified by deploying against `postgis/postgis:16-3.4`.

**What is *not* true:** there is no `SETU_SKIP_HYPERTABLE` variable. An earlier
version of this document promised one. The skip is automatic and needs no
configuration.

### 2. The backend service

**New → GitHub Repo →** this repository. Then in Settings:

| Setting | Value |
|---|---|
| Root Directory | `/` (the build context must be the repository root) |
| Dockerfile Path | `backend/Dockerfile` |
| Health Check Path | `/healthz` |
| Health Check Timeout | `300` seconds |

The health-check timeout matters. First boot runs migrations, creates the
unprivileged role, seeds the registry and then runs plate inference over the bundled
clip — around three minutes. A 30-second health check kills it mid-seed and reports a
crash loop that reads like a code fault.

Variables:

```bash
# Reference Railway's own database variable. The entrypoint accepts the libpq form
# and rewrites the scheme to name psycopg 3.
SETU_MIGRATION_DATABASE_URL=${{Postgres.DATABASE_URL}}

# The API's own, unprivileged role. Supply only the password: the entrypoint derives
# the URL from the migration URL by substituting role and password, which is what
# stops the API ever running on the superuser URL by mistake.
SETU_APP_DB_PASSWORD=<generate>

SETU_JWT_SECRET=<generate, 48+ chars>
SETU_ADMIN_PASSWORD=<generate>
SETU_OPERATOR_PASSWORD=<generate>
SETU_EVIDENCE_SIGNING_KEY=<64 hex chars>

# The console's public origin, once you know it. Never a wildcard.
SETU_CORS_ORIGINS=https://<console-domain>

SETU_SEED_DEMO=1
SETU_DEMO_FRAMES=900
```

Generate the secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_hex(32))"   # signing key
```

**Do not set `SETU_DATABASE_URL` yourself.** Deriving it is what guarantees the API
connects as `setu_app` (NOSUPERUSER, NOBYPASSRLS) rather than as the superuser. A
superuser carries `rolbypassrls` and silently ignores every row-level security policy,
so departments stop being isolated while every isolation test still passes. If you do
set it, it is used as given and that guarantee is yours to maintain.

Attach a volume at `/srv/setu/data/evidence/crops`. The image ships the demo crops so
a fresh deploy is never empty, but anything written after deploy vanishes on the next
one without it, and the Journey and Alert screens then render empty image boxes.

### 3. The console service

**New → GitHub Repo →** the same repository, with:

| Setting | Value |
|---|---|
| Root Directory | `/` |
| Dockerfile Path | `frontend/Dockerfile` |
| Health Check Path | `/healthz` |

Variables:

```bash
SETU_API_HOST=<backend service name>.railway.internal
SETU_API_PORT=8000
```

Leave `VITE_API_BASE_URL` unset. The console then calls `/api` on its own origin and
nginx proxies it to the backend over the private network — one public URL, no CORS
preflight, and the WebSocket works without further configuration.

Both images honour Railway's injected `$PORT`; nothing needs setting for that.

**Why the console does not need the backend to exist first.** nginx resolves upstream
hostnames through a runtime resolver rather than at startup, so it boots and serves
the app even while `*.railway.internal` does not resolve yet, and picks the backend up
as soon as it appears. API calls return 502 in the meantime. Deploy order does not
matter, and a backend redeploy does not require restarting the console.

### 4. Afterwards

Generate a public domain for the console, set `SETU_CORS_ORIGINS` on the backend to
that origin, and redeploy the backend. Then verify before quoting the URL to anyone:

```bash
SETU_ADMIN_PASSWORD=... SETU_OPERATOR_PASSWORD=... \
  python backend/scripts/verify_deployment.py https://<console-domain>
```

---

## 4a. Pinning a digest against an existing volume

Worth knowing before it costs an evening. `docker-compose.prod.yml` previously ran the
floating tag `timescale/timescaledb-ha:pg16`. Pinning it to the digest already used by
the development stack (`pg16.4-ts2.17.2-oss`) **downgraded TimescaleDB from 2.29.2 to
2.17.2 underneath a data directory that had been initialised by the newer image.**

The failure is quiet and very easy to misread:

```
ERROR:  could not access file "$libdir/timescaledb-2.29.2": No such file or directory
STATEMENT:  BEGIN
```

Every *new* session fails. The running API does not, because its connection pool was
already established, so the container reports healthy, the console works, and the
deployment checks pass — while `psql` and any restarted process cannot open a
transaction at all. A restart of the backend would have surfaced it in front of
whoever happened to be watching.

**The rule:** a Postgres image's extension version is part of the on-disk format. When
you change the pinned digest, either match the extension version the volume was built
with, or recreate the volume:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod down -v
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

Recreating is safe here and takes about three minutes: migrations, seeding and demo
ingest all run from the entrypoint, and the stack carries no data that is not
regenerated. On a real deployment with real evidence it is not safe, and the extension
version must be matched instead.

---

## 5. Verification — do this before quoting the URL

A broken hosted URL is worse than none: a judge who clicks it and sees an error
carries that impression into everything else.

```bash
python backend/scripts/verify_deployment.py https://<console-domain>
```

Credentials come from `SETU_ADMIN_PASSWORD` and `SETU_OPERATOR_PASSWORD` in the
environment, or `--admin-password` / `--operator-password`. Check 8 needs database
access: set `SETU_DATABASE_URL` to reach the deployed database, or run the query it
prints by hand. **It reports `NOT VERIFIED` rather than `PASS` when it cannot reach
one** — an unverifiable security control must never read as a green tick. Results are
written to `reports/deployment-verification.json`.

The ten checks. Nine pass against the local production stack; check 8, row-level
security, reports NOT VERIFIED there because that stack deliberately publishes no
database port -- run the query it prints, inside the stack, to confirm it:

| # | Check | Result |
|---|---|---|
| 1 | Log in as `admin` and `operator` | PASS |
| 2 | Map renders pins, uncertainty circles and coordinate-missing entries | PASS — 34 cameras, 32 placed, 32 approximate, 2 missing |
| 3 | Journey returns a multi-hop route **with evidence photos** | PASS — `KA25AB1542`, 4 hops, 4/4 crops fetch |
| 4 | Signed PDF downloads with signature headers | PASS — ~30 KB |
| 5 | Alerts list; WebSocket connects | PASS — 6 alerts, socket connected |
| 6 | Coverage returns district findings | PASS — 10 districts |
| 7 | Health shows measured vs declared fps | PASS — 34 rows |
| 8 | **RLS live**: `setu_app` is `rolsuper=false`, `rolbypassrls=false` | NOT VERIFIED without database access — confirmed by hand inside the stack |
| 9 | Audit chain verifies | PASS |
| 10 | Watchlist populated, every entry bounded by an expiry | PASS — 9 entries, 9 with an expiry |

Check 3 is the crop-storage check and check 8 the tenant-isolation check; those two
are the ones most likely to pass locally and fail in a deployment.

**Both images honour `$PORT`.** Railway, Cloud Run and Heroku assign a port at
runtime and route to that alone. The console's nginx `listen` directive is templated
from it, defaulting to 8080 so compose is unchanged — verified by running the image
with `PORT=7431` and fetching the console on that port.

---

## 6. Operations

**Redeploy** — `railway up`, or `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build`. Migrations reapply; seeding is skipped because the gates find existing rows.

**Roll back** — Railway keeps previous deployments; redeploy the earlier one. Locally, rebuild from the previous commit. Migrations are reversible (`alembic downgrade -1`), and the downgrade path is tested, but a rollback across `0003` rewrites the `detection` table.

**Reset demo data** —
```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend \
  python scripts/seed_demo.py --reset --frames 900
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend \
  python scripts/seed_watchlist.py --reset
```

**Full reset** — `docker compose -f docker-compose.prod.yml --env-file .env.prod down -v` destroys the volumes; the next start reseeds from scratch.

**Logs** — `docker logs -f setu-prod-backend-1`. Credentials are redacted at the logging formatter, so a connection string cannot reach a log sink even if interpolated.
