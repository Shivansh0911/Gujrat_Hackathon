# Deploying SETU — Path C · Render + Netlify + UptimeRobot

**This is the primary path.** Railway's free tier now gives a one-time trial credit
and then requires a card, so the evaluation deployment moved to Render.
[`DEPLOY_STEP_BY_STEP.md`](DEPLOY_STEP_BY_STEP.md) stays as the Railway procedure and
is still correct — use it if the team's situation changes.

Everything below is free and needs no credit card.

| | |
|---|---|
| Backend + database | Render (free web service, free Postgres) |
| Console | Netlify |
| Keeping the backend warm | UptimeRobot (free HTTP monitor) |

All Render facts below were checked against Render's own documentation on
**2026-08-29**. Platform limits change; if something here does not match what you see,
the dashboard is right and this document is stale.

---

## Read this before you start: two dates that matter

**A free Render Postgres expires 30 days after you create it.** After that there is a
14-day grace period to upgrade to a paid plan, and then **Render deletes the
database** — data and all, with no recovery.

Creating it **today, 2026-08-29**, gives:

| Event | Date |
|---|---|
| Database created | **2026-08-29** |
| **Database expires** | **2026-09-28** (Monday 28 September) |
| Deleted if not upgraded | ~2026-10-12 |

Against this timeline:

- Submission — **7 September** ✅ three weeks of margin
- Grand finale — **10–11 September** ✅ seventeen days of margin

**So for this specific evaluation, no action is needed.** If the deployment has to
outlive ~28 September, the database must be upgraded to a paid instance *before* the
grace period ends. Nothing about keeping the web service awake changes this — see §6.

> If you create the database on a different day, recompute: creation date + 30 days.

---

## Before you start — generate the secrets

Same commands as the Railway guide. Generate once, keep them somewhere you can paste
from.

```bash
python -c "import secrets; print('SETU_JWT_SECRET      =', secrets.token_urlsafe(48))"
python -c "import secrets; print('SETU_APP_DB_PASSWORD =', secrets.token_urlsafe(24))"
python -c "import secrets; print('SETU_ADMIN_PASSWORD  =', secrets.token_urlsafe(18))"
python -c "import secrets; print('SETU_OPERATOR_PASSWORD =', secrets.token_urlsafe(18))"
python -c "import secrets; print('SETU_EVIDENCE_SIGNING_KEY =', secrets.token_hex(32))"
```

**Keep `SETU_ADMIN_PASSWORD` and `SETU_OPERATOR_PASSWORD`.** They are the test
credentials the screening committee will use, they go on the submission form, and
they belong in [`DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md) §1 once the site is live.

`make deploy-secrets` writes all five to a gitignored `deploy-secrets.env` if you
would rather not copy from a terminal.

---

## C1 · The database

1. Render Dashboard → **New +** → **Postgres**.
2. Fill in:

   | Field | Value |
   |---|---|
   | Name | `setu-db` |
   | Database | `setu` |
   | User | `setu` |
   | Region | pick one, and **use the same region for the web service** |
   | PostgreSQL Version | **16** (any 13+ works; extensions need 13+) |
   | Instance Type | **Free** |

   The region matters: the internal connection string only resolves between services
   in the same region. Different regions means falling back to the external URL,
   which goes out over the internet and is slower for every query.

3. Create it, and wait for status **Available**.

4. Open the database's **Info** page and copy the **PSQL Command**. Run it, then
   enable the extensions:

   ```sql
   CREATE EXTENSION IF NOT EXISTS postgis;
   CREATE EXTENSION IF NOT EXISTS pgcrypto;
   CREATE EXTENSION IF NOT EXISTS pg_trgm;
   ```

   These three are **required**. Migration `0001` treats them as such and fails with a
   message naming the missing one — camera positions are `geography(Point,4326)`, the
   `id` defaults use `gen_random_uuid()`, and fuzzy plate search uses a trigram index.

5. Then try the two optional ones:

   ```sql
   CREATE EXTENSION IF NOT EXISTS timescaledb;
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

   **Render does support `timescaledb`** on PostgreSQL 13+, with the caveat that
   TimescaleDB's *community* features are unavailable and the database must have been
   created after 12 January 2023. If it installs, `detection` becomes a hypertable as
   designed. If it errors, that is fine — **do nothing about it**, and read on.

> ### There is no `SETU_SKIP_HYPERTABLE` variable
>
> Do not set one. It does not exist, and setting it would give false confidence.
>
> Migration `0003` checks `pg_extension` at run time and skips `create_hypertable`
> when TimescaleDB is absent, logging a warning. Migration `0001` wraps each optional
> extension in a `SAVEPOINT`, so a failed `CREATE EXTENSION` cannot abort Alembic's
> own transaction. Both paths are exercised: the stack is verified against
> `postgis/postgis:16-3.4`, which has neither TimescaleDB nor pgvector.
>
> **What it costs without TimescaleDB:** `detection` stays an ordinary table.
> Correctness is completely unaffected — every query returns the same rows. What is
> lost is chunk exclusion on time-window queries, which matters at an estate far
> larger than the 30–50 cameras this deployment serves. At this size it is not
> measurable.

6. From the **Info** page, note both connection strings. You want the **Internal
   Database URL** for the backend.

---

## C2 · The backend

1. Render Dashboard → **New +** → **Web Service** → **Build and deploy from a Git
   repository** → this repository.

2. Configure:

   | Field | Value |
   |---|---|
   | Name | `setu-api` |
   | Region | **the same region as the database** |
   | Branch | `main` |
   | Root Directory | *(leave empty — this means the repo root)* |
   | Language / Runtime | **Docker** |
   | Dockerfile Path | `backend/Dockerfile` |
   | Instance Type | **Free** |

   **Root Directory must stay empty, i.e. the repo root — not `backend/`.** The image
   deliberately mirrors the repository layout so `paths.py` resolves identically in a
   container and on a laptop. A build whose context is `backend/` alone cannot reach
   `data/`, and the demo clip and seed files live there.

3. **Environment variables** — Advanced → Add Environment Variable:

   ```
   SETU_MIGRATION_DATABASE_URL=<the Internal Database URL from C1 step 6>
   SETU_APP_DB_PASSWORD=<generated>
   SETU_JWT_SECRET=<generated>
   SETU_ADMIN_PASSWORD=<generated>
   SETU_OPERATOR_PASSWORD=<generated>
   SETU_EVIDENCE_SIGNING_KEY=<generated hex>
   SETU_SEED_DEMO=1
   SETU_DEMO_FRAMES=900
   ```

   Render hands out a `postgresql://` URL. The entrypoint rewrites the scheme to
   `postgresql+psycopg://` itself, so paste it exactly as given.

   > **Do not set `SETU_DATABASE_URL`.** The entrypoint derives it from the migration
   > URL, substituting the unprivileged `setu_app` role and `SETU_APP_DB_PASSWORD`.
   > That derivation is what stops the API running on the superuser URL — a superuser
   > carries `rolbypassrls` and silently ignores every row-level security policy, so
   > departments would stop being isolated while every isolation test still passed.
   > Setting it by hand is supported, but then that guarantee is yours to maintain.

4. **Health Check Path** — Settings → **Health Checks** → set to `/healthz`.

   **There is nothing else to configure here, and that is worth knowing.** Render has
   no health-check timeout or startup-grace-period field. It applies a fixed 5-second
   response window per check, and gives a deploy **15 minutes** to become healthy
   before cancelling it.

   First boot does real work — migrations, unprivileged role creation, registry seed,
   then plate inference over the bundled clip — and takes about three minutes. That is
   comfortably inside Render's 15-minute window, so unlike the Railway path there is
   no timeout to raise. Do not go looking for the field; it is not there.

5. **Create Web Service.** Watch the log. You are looking for, in order:

   ```
   [entrypoint] applying migrations...
   [entrypoint] ensuring the unprivileged application role...
   [entrypoint] no detections yet; running ANPR over the bundled demo clip...
   ```

6. Note the public URL Render assigns, e.g. `https://setu-api.onrender.com`. Render
   generates this automatically for a web service; there is no "generate domain"
   step as on Railway.

> ### The 750-hour budget, precisely
>
> Render gives each **workspace** 750 free instance-hours per calendar month. Exceed
> them and Render **suspends every free web service in the workspace** until the start
> of the next month.
>
> One always-on service costs 24 × 31 = **744 hours** in a 31-day month, or 720 in a
> 30-day one. So a single always-on service fits — but with about six hours of
> headroom in a long month, and **a second free web service in the same workspace
> would not fit**. If you need one, put it in a different workspace.

---

## C3 · The console

Identical to the Railway path's Netlify section; the backend origin is the only
difference.

1. Netlify → **Add new site** → **Import an existing project** → this repository.
2. Netlify reads `frontend/netlify.toml`, so the build settings come from the repo.
   Confirm it shows:

   | Setting | Value |
   |---|---|
   | Base directory | `frontend` |
   | Build command | `npm ci && npm run build` |
   | Publish directory | `dist` |

3. **Site settings → Environment variables:**

   ```
   VITE_API_ORIGIN=https://setu-api.onrender.com
   ```

   No trailing slash, no `/api` suffix.

   > **Why not proxy `/api` through Netlify instead?** Netlify's `status = 200`
   > redirects proxy HTTP happily, and it is tempting. But **they do not proxy
   > WebSockets**, and the alert desk's live feed is one. You would get a console
   > where the alert list still polls — so the page looks alive — and the only symptom
   > is a status dot that never turns green. Pointing the console straight at the API
   > avoids the split entirely.
   >
   > `VITE_API_ORIGIN` is read at **build time** by Vite, so changing it needs a
   > redeploy, not just a restart.

4. Deploy, and note the site URL.

---

## C4 · Close the loop

On the **Render backend**, add:

```
SETU_CORS_ORIGINS=https://<your-site>.netlify.app
```

The exact origin, no trailing slash. Save — Render redeploys automatically.

This is not optional. The console is now a different origin, it sends an
`Authorization` header, and the browser blocks every call without this. **Never use
`*`**: a wildcard origin with credentials is refused by browsers anyway and is
prohibited in [`SECURITY.md`](../SECURITY.md).

---

## C5 · Keep the service warm

A free Render web service **spins down after 15 minutes without inbound traffic**, and
takes roughly a minute to come back. A judge clicking a cold link waits that minute
looking at nothing.

No API key was available when this was written, so these are manual steps.

1. Create a free account at [uptimerobot.com](https://uptimerobot.com) — no card.
2. **+ New monitor**:

   | Field | Value |
   |---|---|
   | Monitor Type | **HTTP(s)** |
   | Friendly Name | `SETU API` |
   | URL | `https://setu-api.onrender.com/healthz` |
   | Monitoring Interval | **5 minutes** (the shortest on the free plan) |

3. Create it, and confirm it goes green.

`/healthz` is the right target: it is unauthenticated, it is cheap, and it is the
same endpoint Render's own health check uses.

> ### ⚠️ This keeps the service awake. It does **not** extend the database's life.
>
> These are two unrelated limits, and conflating them is the mistake that loses the
> deployment:
>
> - **Web service spin-down** is about *traffic*. Pinging every 5 minutes prevents it
>   entirely.
> - **Postgres expiry** is a *hard calendar limit on the database instance*. It fires
>   on **2026-09-28** whether the service has been pinged a thousand times a day or
>   never. No amount of monitoring changes it, and after the 14-day grace period the
>   database is **deleted**.
>
> For this evaluation that is fine — 28 September is well past the 10–11 September
> finale. Beyond that date, upgrade the database or lose it.

---

## C6 · Verify

```bash
SETU_ADMIN_PASSWORD=... SETU_OPERATOR_PASSWORD=... \
  python backend/scripts/verify_deployment.py https://<your-site>.netlify.app
```

**Expect 9 of 10.** The tenth is row-level security declining to certify itself
without database access — correct behaviour, not a failure. Settle it directly, using
the PSQL Command from the database's Info page:

```sql
SELECT rolname, rolsuper, rolbypassrls
FROM pg_roles WHERE rolname = 'setu_app';
```

Both flags must be `false`. If `rolsuper` is `true`, the API is running as the
superuser and RLS is inert — you set `SETU_DATABASE_URL` by hand somewhere, or the
role creation step did not run.

---

## Troubleshooting, by symptom

| Symptom | Cause | Fix |
|---|---|---|
| First click after a quiet period takes ~1 minute | The free service spun down after 15 minutes idle | Expected. C5 prevents it — but the **very first** request, before UptimeRobot has run once, can still be slow. Open the link yourself once after setting the monitor up |
| Console loads, every API call fails, browser console says CORS | `SETU_CORS_ORIGINS` does not exactly match the Netlify origin | Copy the origin from the browser address bar. No trailing slash. Redeploy the backend |
| Console loads, alerts list works, **status dot never turns green** | The WebSocket is not reaching the API | `VITE_API_ORIGIN` is unset or wrong. It is read at build time, so redeploy Netlify after changing it. Netlify cannot proxy `wss://` |
| Deploy cancelled after 15 minutes | The service never became healthy | Check the Health Check Path is `/healthz`. If migrations are still running, look for the real error above them in the log |
| `the required PostgreSQL extension 'postgis' is unavailable` | C1 step 4 was skipped | Run the three `CREATE EXTENSION` statements and redeploy |
| Log says `timescaledb is not installed; leaving 'detection' as a plain table` | TimescaleDB was not enabled | **Not an error.** Correctness is unaffected; see C1 |
| Deploy fails during migrations, `invalid interpolation syntax` | A `%` in the database password | Already fixed; make sure you are on `main` |
| Journey and Alert screens show **empty image boxes** | Evidence crops written at runtime were lost | See below — this is expected on Render's free tier |
| All free services suddenly suspended | The workspace exceeded 750 instance-hours | Wait for the next calendar month, or move a second service to another workspace |
| `POST /auth/login` returns 503 | `SETU_ADMIN_PASSWORD` / `SETU_OPERATOR_PASSWORD` unset | The API refuses to issue tokens rather than fall back to a default credential. Set them |

### Evidence crops and the free tier — stated plainly

**Render's free web services cannot attach a persistent disk.** Render's own
documentation says so directly: persistent disks are a paid-service feature. The
container filesystem is ephemeral, so **any evidence crop written after deployment is
lost on the next redeploy**, and on any restart Render performs.

What this does and does not break:

- **The demo is never empty.** The image *ships* the evidence crops produced from the
  bundled clip, so a fresh deploy has its journey hops, alert cards and evidence
  photographs from the first second. Everything a judge sees on the scripted demo path
  survives a redeploy, because it is baked into the image rather than written at run
  time.
- **Crops from a live ingest run performed against the deployed instance will not
  survive a redeploy.** If you run `gateway-ingest` against the deployed backend, its
  new crops live until the next deploy and then the Journey and Alert screens render
  empty boxes for those specific detections — the rows remain, the images do not.

The fix, if it ever matters, is a paid instance with a disk mounted at
`/srv/setu/data/evidence/crops`. For an evaluation deployment it does not: run
ingest locally, commit the evidence, and let the image carry it.
