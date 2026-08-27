# ADR 0004 — Railway for the evaluation deployment; the container stack is the artefact

- **Status:** Accepted (deployment not yet performed — see Consequences)
- **Date:** 2026-08-27
- **Deciders:** SETU engineering
- **Referenced by:** `docs/DEPLOYMENT.md` §4

## Context

The submission form accepts a hosted URL with test credentials. It is optional, but a
screening committee that can click through the platform forms a different impression
from one watching a recording, so it materially affects the "maturity of the working
platform" assessment.

The requirement is unusual in one respect: this deployment exists to be *evaluated*,
not to serve traffic. It runs for a few weeks, is visited by a handful of judges, and
then stops mattering. Optimising it for production scale would be misdirected effort;
optimising it for **the demo never being empty or broken when a stranger opens it** is
the actual goal.

Three constraints narrow the field:

1. **PostGIS, and preferably TimescaleDB.** The schema uses `geography` columns and a
   hypertable on `detection`. A managed Postgres without PostGIS is disqualifying;
   without TimescaleDB it is survivable (`SETU_SKIP_HYPERTABLE=1` leaves `detection` a
   plain table — correctness is unaffected, only time-window performance at scale).
2. **First start does real work.** Migrations, role creation, registry seed, then ANPR
   inference over the bundled clip — roughly three minutes. A platform with a short
   fixed startup-health deadline will kill the container mid-seed and then report a
   crash loop that looks like a code fault.
3. **A student team's budget**, with no institutional card.

## Options

| Option | PostGIS | Verdict |
|---|---|---|
| **Railway** | Yes, on the standard Postgres image; TimescaleDB via a custom image | **Chosen** |
| Render | Yes; free-tier Postgres expires after 90 days and the free web service cold-starts | Rejected — a judge clicking a cold-started free service waits ~50 s and concludes the platform is slow |
| Fly.io | Yes, but Postgres is an unmanaged app you operate yourself | Rejected — we would be running our own database during submission week |
| Vercel + Neon | Frontend excellent; backend is a long-lived container with ONNX models, not a serverless function | Rejected — the backend does not fit the execution model |

## Decision

**Railway**, both services on one platform, one set of secrets.

The deciding factors were the absence of cold starts (Render's free tier disqualified
itself on constraint 3 meeting constraint 1), managed Postgres with PostGIS on the
standard image, and native Dockerfile deploys — meaning **the deployed artefact is the
same image verified locally**, not a platform-specific build.

That last point is the one that matters architecturally. `docker-compose.prod.yml`
is the source of truth; the platform is a place to run it. The nine post-deployment
checks in `DEPLOYMENT.md` §5 pass against the local container stack, and they are the
same checks to run against the hosted URL. If Railway becomes unsuitable, the
migration cost is the environment variables and nothing else.

## Consequences

**The deployment has not been performed.** It requires the team's Railway account, and
account creation and billing are not ours to do. The stack is container-ready and
verified 9/9 from a clean state; §4 of `DEPLOYMENT.md` is the procedure and §5 is what
to check before quoting the URL to anyone. **A broken hosted URL is worse than no
hosted URL** — a judge who clicks it and sees an error carries that impression into
every other part of the submission — so the URL goes on the submission form only after
those nine checks pass against it.

**Two things will break on a first deploy if they are forgotten**, both documented:

- **TimescaleDB may be unavailable** on the plan, which fails migration `0003`. Either
  use a Postgres image carrying it or set `SETU_SKIP_HYPERTABLE=1`.
- **Evidence crops written at runtime need a persistent volume** at
  `/srv/setu/data/evidence/crops`. The image ships the demo crops so a fresh deploy is
  never empty, but without the volume anything generated after deploy vanishes on
  redeploy and the Journey and Alert screens render empty image boxes — which is
  precisely the failure a judge would notice first.

**Not decided here:** where a real Gujarat Police deployment would run. That is a
government-cloud or on-premise question involving data-residency rules that no
commercial PaaS satisfies, and the federation architecture is deliberately agnostic to
it — video stays at the edge, and only metadata reaches whatever the centre turns out
to be.
