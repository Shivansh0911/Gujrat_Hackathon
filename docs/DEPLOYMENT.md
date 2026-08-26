# Deployment

The stack runs as three containers: Postgres, the backend API, and nginx serving the
console and proxying `/api`. It has been verified end to end from a clean state —
nine checks, all passing, listed in §5.

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

> **Status: not yet performed.** The stack is container-ready and verified locally,
> but pushing to Railway requires the team's account. The steps below are what to
> run; §5 is what to check afterwards before quoting the URL to anyone.

Railway is the recommendation: managed Postgres with PostGIS available, both
services on one platform, one set of secrets. See
`docs/adr/0004-deployment-platform.md` for the trade-offs against Render and Fly.

```bash
npm i -g @railway/cli
railway login
railway init                      # or: railway link  (existing project)

# 1. Database
railway add --database postgres
# In the Postgres service shell, enable the extensions the schema needs:
#   CREATE EXTENSION IF NOT EXISTS postgis;
#   CREATE EXTENSION IF NOT EXISTS timescaledb;
#   CREATE EXTENSION IF NOT EXISTS vector;
#   CREATE EXTENSION IF NOT EXISTS pg_trgm;
#   CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

**If TimescaleDB is unavailable** on the plan, migration `0003` fails. Either use a
Postgres image that carries it, or set `SETU_SKIP_HYPERTABLE=1` and accept that
`detection` stays a plain table — correctness is unaffected, only time-window query
performance at scale.

```bash
# 2. Backend service — root directory ".", Dockerfile "backend/Dockerfile"
railway variables set \
  SETU_DATABASE_URL='postgresql+psycopg://setu_app:<APP_PW>@<PGHOST>:<PGPORT>/<PGDB>' \
  SETU_MIGRATION_DATABASE_URL='postgresql+psycopg://<PGUSER>:<PGPW>@<PGHOST>:<PGPORT>/<PGDB>' \
  SETU_APP_DB_PASSWORD='<APP_PW>' \
  SETU_JWT_SECRET='<generated>' \
  SETU_ADMIN_PASSWORD='<generated>' \
  SETU_OPERATOR_PASSWORD='<generated>' \
  SETU_CORS_ORIGINS='https://<console-domain>' \
  SETU_EVIDENCE_SIGNING_KEY='<64 hex chars>' \
  SETU_SEED_DEMO=1 SETU_DEMO_FRAMES=900

# 3. Console service — root ".", Dockerfile "frontend/Dockerfile"
#    Build arg VITE_API_BASE_URL=https://<backend-domain>  (split origin)
#    or leave the default /api and put both behind one domain.
railway up
```

Migrations and seeding run from the entrypoint on every deploy — there is no manual
step, and nothing to forget.

**Evidence crops.** The image ships the demo crops, so a fresh deploy is never empty.
Crops written at runtime need a persistent volume mounted at
`/srv/setu/data/evidence/crops`, or they vanish on redeploy and the Journey and Alert
screens render empty image boxes.

---

## 5. Verification — do this before quoting the URL

A broken hosted URL is worse than none: a judge who clicks it and sees an error
carries that impression into everything else.

```bash
python /path/to/verify_deployed.py https://<console-domain>
```

The nine checks, all of which pass against the local production stack:

| # | Check | Result |
|---|---|---|
| 1 | Log in as `admin` and `operator` | PASS |
| 2 | Map renders pins, uncertainty circles and coordinate-missing entries | PASS — 34 cameras, 32 placed, 26 approximate, 2 missing |
| 3 | Journey returns a multi-hop route **with evidence photos** | PASS — 4 hops, 4 crops, images fetch |
| 4 | Signed PDF downloads with signature headers | PASS — 25 KB |
| 5 | Alerts list; WebSocket connects | PASS — 8 alerts |
| 6 | Coverage returns district findings | PASS — 10 districts |
| 7 | Health shows measured vs declared fps | PASS |
| 8 | **RLS live**: `setu_app` is `rolsuper=false`, `rolbypassrls=false` | PASS |
| 9 | Audit chain verifies | PASS |

Check 3 is the crop-storage check and check 8 the tenant-isolation check; those two
are the ones most likely to pass locally and fail in a deployment.

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
