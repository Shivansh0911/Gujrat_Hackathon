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
   SETU_GATEWAY_HOST=cctv.corp8.cloud
   SETU_GATEWAY_MEDIA_HOST=103.250.160.189
   SETU_CATALOGUE_PATH=/cameras.json
   SETU_HLS_PATH_TEMPLATE=/{id}/index.m3u8
   SETU_GATEWAY_ACCESS_CODE=<the code from the feed-access form>
   ```

   > **The gateway moved on 2 September 2026, and the shape of it changed.** Four of
   > those five variables are new, and a deployment carrying only the old
   > `SETU_GATEWAY_HOST=live.corp8.cloud` reaches a host that no longer serves the feed.
   >
   > | Variable | Why it exists |
   > |---|---|
   > | `SETU_GATEWAY_HOST` | The CDN host. Serves the catalogue and HLS, behind a login |
   > | `SETU_GATEWAY_MEDIA_HOST` | RTSP and WebRTC, on a bare public IP. A CDN cannot proxy either, so the estate publishes them separately — and RTSP is the transport ANPR should use |
   > | `SETU_CATALOGUE_PATH` | `/api/ingest` became `/cameras.json` |
   > | `SETU_HLS_PATH_TEMPLATE` | `/live/stream/{id}/index.m3u8` became `/{id}/index.m3u8` |
   > | `SETU_GATEWAY_ACCESS_CODE` | **Secret.** The catalogue and every playlist now sit behind a password. Without it, requests return the sign-in page with HTTP 200, and the client reports a format error rather than a missing credential |
   >
   > The access code is a credential: it belongs in Render's environment and in the
   > gitignored `deploy-secrets.env`, never in a committed file.

   > **`SETU_GATEWAY_HOST` is not optional, and omitting it fails in a way that does
   > not look like a missing variable.** It has no default on purpose -- a deployment
   > should never silently fall back to some other team's gateway. But without it,
   > pydantic raises the first time a request touches the feed configuration, FastAPI
   > returns a bare 500, and the console shows "Load failed" on **Compare with
   > gateway** and on live camera preview. Nothing on screen points at the cause. This
   > list originally omitted it, and that is exactly how the first deployment failed.
   >
   > The endpoints now answer usefully when it is unset rather than 500-ing, but they
   > still cannot reach a gateway that was never named.

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

### "Child process [N] died", over and over, and still no open port

```
INFO:  Waiting for child process [96]
INFO:  Child process [96] died
==>    Port scan timeout reached, no open ports detected.
INFO:  Waiting for child process [97]
INFO:  Child process [97] died
```

Different failure from the one below, and it means uvicorn *did* start — its worker
children are dying as fast as they are spawned, so the port never opens.

The cause was the demo seed running when it should not have been, taking memory the
workers needed. And the reason it was running is worth stating plainly, because it is
the same shape as several other defects in this project:

> **A row-level-security policy made the seed gate lie.** `detection` carries
> `FORCE ROW LEVEL SECURITY`, whose deliberate failure mode is *no rows* rather than an
> error — forgetting to set the session context must never expose the whole estate.
> `count_rows` opened a plain connection and set no context, so on a database holding
> twenty detections it counted **zero**, and the gate whose entire job is to skip
> seeding fired on every single boot. The security control was working exactly as
> designed. The check was wrong.

Fixed by having `count_rows` set `setu.is_admin` — the same session GUC every background
job elevates with — before counting. The default worker count also dropped from 2 to 1:
a shared 0.1-CPU instance gains nothing from a second worker and pays for it twice in
memory, which is the constraint that actually bites here.

**To unblock a running service immediately, without waiting for a rebuild**, set these
in the Render dashboard's Environment tab:

| Variable | Value | Effect |
|---|---|---|
| `SETU_SEED_DEMO` | `0` | Skip boot-time seeding entirely. Safe whenever the database already holds detections — check the Alert Desk |
| `SETU_WORKERS` | `1` | **Already the default** — `docker-entrypoint.sh` runs `--workers ${SETU_WORKERS:-1}`. Set it only if a previous deploy raised it; there is nothing to change on a fresh service |

### "No open ports detected" and the deploy is killed and retried

```
==> No open ports detected, continuing to scan...
==> Port scan timeout reached, no open ports detected. Bind your service to
    at least one port.
[app] services.ingest.file_source: SCENE_DISCONTINUITY camera=REPLAY-01
```

Those two lines together are the whole diagnosis: the container is busy running the
ANPR demo seed, and has not reached `exec uvicorn` yet. Render decides a web service
that has not bound its port within the scan window is dead, kills it, and restarts —
and the restart begins seeding again from the top. A loop that never serves a request.

**Fixed in the entrypoint**: demo seeding now runs in the background, after the port is
bound. Migrations and the unprivileged application role still run in the foreground —
they are fast, and they are preconditions for serving any request safely. Seeding
demonstration data is neither, so it belongs behind the bind.

If you are running an older image, the workaround is `SETU_SEED_DEMO=0` and seeding the
database from a laptop instead.

**Two knobs worth knowing on a free instance** (0.1 CPU, 512 MB):

| Variable | Why you might change it |
|---|---|
| `SETU_DEMO_FRAMES` | Lower than 900 makes background seeding finish sooner. Fewer frames means fewer detections, so do not go so low the journey demo has nothing to show |
| `SETU_WORKERS` | Leave unset: it already defaults to `1`. Two uvicorn workers plus two ONNX models in 512 MB is tight and a shared 0.1 CPU gains nothing from a second worker, so raise it only on a larger instance. A second worker also halves the benefit of the gateway playlist cache, which is per process |


| Symptom | Cause | Fix |
|---|---|---|
| First click after a quiet period takes ~1 minute | The free service spun down after 15 minutes idle | Expected. C5 prevents it — but the **very first** request, before UptimeRobot has run once, can still be slow. Open the link yourself once after setting the monitor up |
| **Compare with gateway** or live preview says "Load failed", other screens fine | `SETU_GATEWAY_HOST` is unset on the API | Set it to `cctv.corp8.cloud`, along with the four companion variables above, and redeploy. Everything not touching the feed works without it, which is why this looks like a one-screen bug |
| Console loads, every API call fails, browser console says CORS | `SETU_CORS_ORIGINS` does not exactly match the Netlify origin | Copy the origin from the browser address bar. No trailing slash. Redeploy the backend |
| Console loads, alerts list works, **status dot never turns green** | The WebSocket is not reaching the API | `VITE_API_ORIGIN` is unset or wrong. It is read at build time, so redeploy Netlify after changing it. Netlify cannot proxy `wss://` |
| Deploy cancelled after 15 minutes | The service never became healthy | Check the Health Check Path is `/healthz`. If migrations are still running, look for the real error above them in the log |
| `the required PostgreSQL extension 'postgis' is unavailable` | C1 step 4 was skipped | Run the three `CREATE EXTENSION` statements and redeploy |
| Log says `timescaledb is not installed; leaving 'detection' as a plain table` | TimescaleDB was not enabled | **Not an error.** Correctness is unaffected; see C1 |
| Deploy fails during migrations, `invalid interpolation syntax` | A `%` in the database password | Already fixed; make sure you are on `main` |
| Journey and Alert screens show **empty image boxes** | Evidence crops written at runtime were lost | See below — this is expected on Render's free tier |
| All free services suddenly suspended | The workspace exceeded 750 instance-hours | Wait for the next calendar month, or move a second service to another workspace |
| **UptimeRobot reports a continuous outage, `405 Method Not Allowed`, while the site works** | The monitor probes with `HEAD`, and FastAPI does not add `HEAD` to a route declared with `.get()` | Fixed on `main`: `/` and `/healthz` now answer both. On an older build, set the monitor's method to `GET` instead. Note the 405 still reached the server, so the keep-alive was working the whole time — only the reporting was wrong |
| **Every government tile in Control Room says `Live feed unavailable, upstream returned HTTP 502`, while gateway ingest works** | The estate refuses its media plane to non-browser clients (`403 browser required`), and our proxy sent a library user-agent. RTSP is a different host with no such gate, which is why ingest was unaffected and the fault looked like an outage | Fixed on `main`. If it recurs, confirm `SETU_HLS_PATH_TEMPLATE` is `/{id}/index.m3u8` — the older `/live/stream/{id}/...` returns 404, which surfaces as the same 502 |
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
