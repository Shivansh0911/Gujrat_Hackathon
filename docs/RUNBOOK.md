# SETU operational runbook

For whoever is on call. Each entry is a failure you will actually see, how to tell it
apart from the failures it resembles, and what to do. The diagnosis step is not padding:
three of the four entries below have a look-alike that the obvious fix makes worse.

Two conventions throughout. `$API` is the API origin (`https://setu-api-8m2h.onrender.com`
in the current deployment). Every command here is read-only unless the entry says
otherwise.

## First: which half is broken?

```bash
curl -s $API/readyz | jq
```

`/readyz` checks the database, the migration head and model availability. `/healthz` is
a liveness probe and deliberately touches nothing — it answers "is the process alive",
not "is it working", because a liveness probe that queries the database turns a database
blip into a restart loop.

| `/readyz` | `/health/gateway` | Where the fault is |
|---|---|---|
| green | green | Not the platform. Check the browser console and `VITE_API_ORIGIN`. |
| green | red | Upstream video. See **Gateway down**. |
| red | — | The platform. `jq '.checks[] \| select(.ok==false)'` names the failing check. |

---

## Gateway down

**Looks like:** every camera UNREACHABLE at once; `/health/gateway` failing; Control Room
tiles black.

**The gateway is the organiser's server, not ours.** Nothing in this repository can fix
it, and the runbook's job is to establish that quickly and prove it rather than to
attempt a repair.

**Diagnose:**

```bash
curl -s $API/health/gateway | jq
python backend/scripts/gateway_report.py          # last successful fetch per camera
```

Separate the three causes, because they look identical from the console and have
completely different responses:

1. **Watch-time quota exhausted.** The quota is per-account and recovers on a cooldown.
   `gateway_report.py` shows the quota response verbatim. *Wait.* Do not retry in a
   loop — retries consume the quota that the cooldown is restoring, and a tight retry
   loop is how the quota is lost for the rest of the day.
2. **Access code rotated.** The organiser issues a new code periodically. Symptom is an
   auth rejection rather than a quota message. Update `SETU_GATEWAY_ACCESS_CODE` in the
   Render environment and redeploy.
3. **Gateway genuinely down.** Neither of the above, and no camera responds. Nothing to
   do but record it.

**Do not** widen timeouts or add retries to work around a quota. That converts a
recoverable cooldown into a sustained one.

The platform is designed to stay useful through this: every screen except Control Room
reads from the database, so Journey, Coverage, Dashboard and the Alert Desk keep working
on already-ingested evidence while the gateway is unreachable.

---

## Camera flapping

**Looks like:** one camera alternating between OK and UNREACHABLE; alert noise from that
camera only.

**Diagnose first, because the two causes need opposite responses:**

```bash
curl -s $API/health/cameras | jq '.[] | select(.camera_ref=="<ref>")'
```

- **`measured_fps` well below `declared_fps`** — the camera is reachable and delivering,
  just slower than it advertises. This is the divergence the integration guide warns
  about and the Dashboard flags. It is **not** a fault; the camera is under-delivering
  and the platform already records measured rather than declared rate. Leave it.
- **`measured_fps` null, status alternating** — genuine flap: the stream is being
  established and dropped.

For a genuine flap, prefer RTSP over TCP to HLS; most flapping on this estate is a UDP
transport problem rather than a camera problem. If it persists, mark the camera out of
service rather than letting it generate alerts — a flapping camera produces detections
with unreliable timestamps, and an unreliable timestamp on a journey is worse than a
missing hop, because the operator cannot see that it is wrong.

**Never** raise the alert threshold to quieten a flapping camera. That suppresses real
alerts estate-wide to fix one camera.

---

## Database failover or unavailability

**Looks like:** `/readyz` red with a database reason; API returning 503; the Render
dashboard showing a database event.

**Diagnose:**

```bash
curl -s $API/readyz | jq .checks
python backend/scripts/verify_deployment.py       # schema, RLS, role, migration head
```

**Order matters on recovery.** Check these in sequence; each one makes the next
meaningful:

1. **Connectivity.** If the database is simply unreachable the API recovers on its own
   once it returns; no action.
2. **Migration head.** `/readyz` reports the head it is at. A failover to a replica that
   has not caught up reports an older head. Do not run `alembic upgrade` against a
   replica that is still catching up.
3. **The `setu_app` role.** A restore from a plain dump recreates tables but not
   necessarily the unprivileged role or its grants. Run
   `python backend/scripts/create_app_role.py` — it is idempotent.
4. **RLS, in two parts — both matter and the scripts cover only the first.**

   `verify_deployment.py` asserts the *role* cannot bypass RLS (`rolsuper` and
   `rolbypassrls` both false on `current_user`). It does **not** check the *tables*.
   After any restore, check those too, because a plain dump can recreate a table
   without `FORCE ROW LEVEL SECURITY` and the owner then reads every department's rows:

   ```sql
   SELECT relname, relrowsecurity, relforcerowsecurity
     FROM pg_class
    WHERE relname IN ('camera','detection','vehicle_sighting','alert','audit_entry');
   ```

   Both flags must be true on every row. This is the single most consequential thing to
   confirm after a restore: a false here is a silent cross-department data leak, not an
   error anyone will see.
5. **The audit chain.** `curl -s $API/audit/verify | jq`.

A gap in the audit `seq` is **not** data loss — a Postgres sequence does not reclaim
numbers from rolled-back transactions. Broken hash links are data loss. The drill checks
both separately and `docs/DISCOVERY.md` finding 26 records why.

### Restoring from backup

```bash
python backend/scripts/backup_restore_test.py --docker-service postgres --report out.json
```

This is the drill, run against a scratch database — it never touches the live one. It is
the rehearsal; run it before you need it. Last recorded result:
`docs/evidence/backup_restore_drill.json`.

---

## Disk pressure

**Looks like:** writes failing; ingest stalling; Postgres refusing connections.

**What grows, in the order it grows:**

| Path | Growth | Safe to delete |
|---|---|---|
| `data/evidence/crops/` | Fastest — one JPEG per vehicle sighting | Yes, except crops cited by a report in `reports/evidence/` |
| `backups/` | Per drill run | Yes, once a newer drill has passed |
| `detection` / `vehicle_sighting` tables | Steady with ingest | **No — see below** |
| Container logs | Slow | Yes, via Docker log rotation |

```bash
du -sh data/evidence/crops backups 2>/dev/null
df -h
```

**Free the crops first.** They are regenerated by re-running ingest and are the largest
and least valuable thing on disk.

**Never** delete rows from `audit_entry` to reclaim space. The ledger is append-only and
hash-chained; deleting entries breaks the chain irreparably and destroys the property
that makes every other record defensible. If the ledger is genuinely the problem, archive
the database and start a new one — do not prune.

Detection and sighting rows should be aged out by policy, not by an emergency `DELETE`.
An emergency deletion under disk pressure is how a journey loses the hop that mattered.

---

## What to capture before escalating

Every API response carries a correlation ID, and logs are structured JSON. With the ID
from the failing response:

```bash
curl -s $API/build-info | jq        # commit and build the deployment is running

# /metrics is authenticated, unlike a conventional Prometheus endpoint: its series
# include alert volumes and watchlist size, which are facts about policing activity
# rather than neutral infrastructure counters. Scrape it with a service-account key.
# A service account presents its key through the same Authorization: Bearer header a
# person's session uses; the setu_sk_ prefix is what tells them apart.
curl -s -H "Authorization: Bearer $SETU_METRICS_KEY" $API/metrics | head -40
```

Attach the correlation ID, the `/readyz` body and the `/build-info` commit. Those three
answer "which build, which request, which check" without a second round trip.
