# Deploying SETU — step by step

Two supported shapes. Pick one.

| | **A · Railway only** | **B · Railway + Netlify** |
|---|---|---|
| Console served by | nginx, in a Railway service | Netlify CDN |
| Origins | one (same-origin) | two (cross-origin) |
| CORS | not involved | load-bearing |
| WebSocket | proxied by nginx | direct to the API |
| Effort | lower | slightly higher |

**Take A unless you have a reason not to.** One origin means no CORS, no
preflight, and the live alert socket needs no special handling. B is here because
Netlify is a genuinely better static host — global CDN, instant rollbacks — and
because the submission may want a `netlify.app` URL.

Everything below has been exercised against the images in this repository. Where a
step exists because something failed without it, the reason is stated.

---

## Before you start

Generate the secrets once and keep them somewhere you can paste from:

```bash
python -c "import secrets; print('SETU_JWT_SECRET      =', secrets.token_urlsafe(48))"
python -c "import secrets; print('SETU_APP_DB_PASSWORD =', secrets.token_urlsafe(24))"
python -c "import secrets; print('SETU_ADMIN_PASSWORD  =', secrets.token_urlsafe(18))"
python -c "import secrets; print('SETU_OPERATOR_PASSWORD =', secrets.token_urlsafe(18))"
python -c "import secrets; print('SETU_EVIDENCE_SIGNING_KEY =', secrets.token_hex(32))"
```

Keep `SETU_ADMIN_PASSWORD` and `SETU_OPERATOR_PASSWORD`: they are the test
credentials the screening committee will use, and they go on the submission form.

---

# Path A · Railway only

## A1 · The database

Railway's own Postgres does **not** carry PostGIS, and PostGIS is not optional here —
camera positions are stored as `geography(Point,4326)`.

1. **New → Docker Image**
2. Image:
   ```
   timescale/timescaledb-ha:pg16@sha256:92809e70c72a5fd169aa3bb7d9c6b1974d6afdf8da786d890f12ae9e7616a67c
   ```
   The digest is the one the compose stack runs, so the deployed database is the
   version everything was tested against.
3. **Variables:**
   ```
   POSTGRES_DB=railway
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=<generate one>
   ```
4. **Settings → Volumes → mount at** `/home/postgres/pgdata/data`

> **If you use a Postgres that only has PostGIS**, the deploy still works. Migration
> `0001` treats `postgis`, `pgcrypto` and `pg_trgm` as required and `timescaledb` and
> `vector` as optional — the optional ones are skipped with a warning and `detection`
> stays a plain table. Correctness is unaffected. Verified against
> `postgis/postgis:16-3.4`.

## A2 · The backend

1. **New → GitHub Repo →** this repository
2. **Settings → Build:**

   | Setting | Value |
   |---|---|
   | Root Directory | `/` |
   | Dockerfile Path | `backend/Dockerfile` |

   The root directory must be `/`, not `backend/`. The image deliberately mirrors the
   repository layout so `paths.py` resolves the same in a container as on a laptop,
   and building from `backend/` alone cannot reach `data/`.

3. **Settings → Deploy:**

   | Setting | Value |
   |---|---|
   | Health Check Path | `/healthz` |
   | Health Check Timeout | `300` |

   **The timeout matters.** First boot runs migrations, creates the unprivileged role,
   seeds the registry, then runs plate inference over the bundled clip — about three
   minutes. A 30-second health check kills it mid-seed and reports a crash loop that
   reads like a code fault.

4. **Variables:**
   ```
   SETU_MIGRATION_DATABASE_URL=${{Postgres.DATABASE_URL}}
   SETU_APP_DB_PASSWORD=<generated>
   SETU_JWT_SECRET=<generated>
   SETU_ADMIN_PASSWORD=<generated>
   SETU_OPERATOR_PASSWORD=<generated>
   SETU_EVIDENCE_SIGNING_KEY=<generated hex>
   SETU_SEED_DEMO=1
   SETU_DEMO_FRAMES=900
   ```
   Replace `Postgres` in the reference with whatever you named the database service.

   > **Do not set `SETU_DATABASE_URL`.** The entrypoint derives it, substituting the
   > unprivileged `setu_app` role. That is what stops the API running on the superuser
   > URL — a superuser carries `rolbypassrls` and silently ignores every row-level
   > security policy, so departments would stop being isolated while every isolation
   > test still passed. Setting it yourself is supported, but then that guarantee is
   > yours to maintain.

5. **Settings → Volumes → mount at** `/srv/setu/data/evidence/crops`

   The image ships the demo crops so a fresh deploy is never empty, but anything
   written afterwards vanishes on the next deploy without this, and the Journey and
   Alert screens then render empty image boxes.

6. Deploy, and watch the logs for:
   ```
   [entrypoint] SETU_DATABASE_URL unset; deriving the application URL for role setu_app.
   [entrypoint] applying migrations...
   [entrypoint] ensuring the unprivileged application role...
   ```

## A3 · The console

1. **New → GitHub Repo →** the same repository
2. **Settings → Build:**

   | Setting | Value |
   |---|---|
   | Root Directory | `/` |
   | Dockerfile Path | `frontend/Dockerfile` |

3. **Settings → Deploy → Health Check Path:** `/healthz`
4. **Variables:**
   ```
   SETU_API_HOST=<backend service name>.railway.internal
   SETU_API_PORT=8000
   ```
   Leave `VITE_API_ORIGIN` and `VITE_API_BASE_URL` unset — the console then calls
   `/api` on its own origin and nginx proxies it privately.

5. **Settings → Networking → Generate Domain**

> **Deploy order does not matter.** nginx resolves the API host at request time
> through a runtime resolver, so the console starts and serves the app even while
> `*.railway.internal` does not resolve yet, and picks the backend up when it appears.
> API calls return 502 in the meantime. Verified by pointing it at a host that does
> not exist: the container stayed up with 0 restarts.

## A4 · Close the loop

1. Copy the console's public domain.
2. On the **backend**, set `SETU_CORS_ORIGINS=https://<that domain>` and redeploy.
3. Verify before quoting the URL to anyone:
   ```bash
   SETU_ADMIN_PASSWORD=... SETU_OPERATOR_PASSWORD=... \
     python backend/scripts/verify_deployment.py https://<console-domain>
   ```
   **Expect 9 of 10.** The tenth is row-level security, which reports `NOT VERIFIED`
   without database access rather than claiming a pass it cannot demonstrate. To
   settle it, run the query it prints against the Railway Postgres — both `rolsuper`
   and `rolbypassrls` must be `false`.

---

# Path B · Railway backend + Netlify console

Do **A1** and **A2** exactly as above. Then:

## B1 · Give the backend a public domain

Railway → backend service → **Settings → Networking → Generate Domain**.

Note it, e.g. `https://setu-api.up.railway.app`. In path A the API stayed private;
here the browser talks to it directly, so it must be reachable.

## B2 · Netlify

1. **Add new site → Import an existing project →** this repository.
2. Netlify reads `frontend/netlify.toml`, so base, publish directory and build
   command are already set. Confirm it shows:

   | Setting | Value |
   |---|---|
   | Base directory | `frontend` |
   | Build command | `npm ci && npm run build` |
   | Publish directory | `dist` |

3. **Site settings → Environment variables:**
   ```
   VITE_API_ORIGIN=https://setu-api.up.railway.app
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

## B3 · Close the loop

On the **backend** in Railway:

```
SETU_CORS_ORIGINS=https://<your-site>.netlify.app
```

Redeploy. This is not optional in path B: the console is now a different origin, it
sends an `Authorization` header, and the browser will block every call without it.
Never use `*` — a wildcard origin with credentials is refused by browsers anyway and
is prohibited in `SECURITY.md`.

Then verify exactly as in **A4**, against the Netlify URL.

---

## Troubleshooting, by symptom

| Symptom | Cause | Fix |
|---|---|---|
| Console loads, every API call fails | `SETU_API_HOST` wrong, or backend still deploying | Check the internal hostname; API calls 502 until the backend is up, which is expected |
| Console loads, alerts list works, **status dot never turns green** | The WebSocket is not reaching the API | Path B: set `VITE_API_ORIGIN` and redeploy Netlify. Netlify cannot proxy `wss://` |
| Every request blocked in the browser console with a CORS message | `SETU_CORS_ORIGINS` does not exactly match the console's origin | Include the scheme, no trailing slash, then redeploy the backend |
| Build fails: `model prefetch failed` | The third-party model host is unreachable | Retry — it retries six times over ~8 minutes already. If it persists, build with `--build-arg REQUIRE_MODEL_CACHE=0` and the models download on first inference instead |
| Deploy fails during migrations, `invalid interpolation syntax` | A `%` in the database password | Already fixed; make sure you are on `main` |
| Deploy crash-loops, `relation "department" does not exist` | Role creation ran before migrations | Already fixed; the entrypoint orders them correctly |
| Journey and Alert screens show empty image boxes | No volume on the crops path | Mount `/srv/setu/data/evidence/crops` |
| Health check kills the container during first boot | Timeout too short | Raise it to 300 seconds |

## After deploying

- Put the console URL and both credentials on the submission form.
- Re-run `make gateway-ingest` shortly before any demonstration so the estate figures
  and any live plate reads are current — the government feed's availability changes
  hour to hour.
- Nothing in this repository needs editing to deploy. Every value above is an
  environment variable.
