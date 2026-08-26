# SETU — build instruction

The working document. A session resumes from here.

---

## PART A — CONTEXT

### A1. What we are building

Gujarat Police operate ~80,000 CCTV cameras across 26 government departments that
cannot interoperate — different vendors, VMS platforms, storage, retention. SETU
federates them into one platform.

**The defining capability:** a judge supplies a vehicle registration number, and the
site shows that vehicle's route across the camera network on a map — with timestamps,
evidence photographs, and honest gaps where we did not see it. Separately, a
watchlisted vehicle on a live feed raises an alert within seconds.

If unsure whether to build something, ask whether its absence would be visible in that
demonstration.

### A2. How this is judged

Seven evaluation areas: successful test case on the government feed, solution
presentation, solution architecture, **maturity of the working platform**, video
analytics output, scalability and PoC readiness, submission completeness.

Mock-ups and simulated interfaces are explicitly not accepted. Two screen-recorded
demonstrations are required — one on our own footage, one on the government feed.

Submitted architecture is a hybrid: **Model 1** (registry + GIS, mandatory) +
**Model 3** (federation middleware) + **Model 2** (unified viewing), with Model 4 as a
documented migration path.

### A3. Known constraints

- **The gateway `live.corp8.cloud` has returned 502 on every media playlist since
  2026-08-25.** `/api/ingest` still returns 200. Every demo path must work with the
  media plane unreachable.
- **RTSP and WHEP are unreachable** — Cloudflare proxies 443/80 only, so 8554 and 8889
  never reach origin. HLS is the transport. The gateway gates on a `cookieCheck=1`
  query parameter that FFmpeg drops when following a master playlist; we resolve the
  master ourselves and hand FFmpeg the variant.
- **The own-feed clip is third-party** — CC BY 3.0 Wikimedia Hubli–Dharwad BRTS
  footage, attributed in `data/own_feed/SOURCE.md`. Shot from a moving bus, so scene
  cuts fire often and multi-frame fusion barely exercises. Being replaced; keep the
  pipeline agnostic to which file is present.

### A4. Rules that bind everything

1. **Timing from PTS only.** Never declared FPS, never arrival time. The CI guard
   permits exactly two reference-only reads and fails the build on a third.
2. **No mocked UI.** A screen without a real backing endpoint does not ship.
3. **Security ships with the endpoint** — auth, validation, scoped access and audit in
   the same commit as the route.
4. **Verify, don't assert.** If you state a measurement, produce it.
5. **Report what you did not do.**
6. **Licences** — Apache-2.0 / MIT / BSD preferred. Isolate AGPL behind our interface
   with an ADR.
7. **Never invent a coordinate.** Every row traces to the geocode cache or a named
   district centroid.
8. **On approaching a context limit, stop and commit** with an honest status and a
   resume note. Committed partial work beats lost complete work.
9. **Do not stop for approval between sections.**

---

## PART B — SCORED WORK  ✅ complete

| # | Item | State |
|---|---|---|
| 1 | Gap analysis (`GET /cameras/gap-analysis` + Coverage screen) | done |
| 2 | Signed evidence export (PDF + Ed25519 detached signature) | done |
| 3 | Row-level security (policies + unprivileged role + 9 tests) | done |
| 4 | Ground-truth harness | **built; awaits human annotation** |
| 5 | Benchmarks | done, measured |
| 6 | Detection output report (CSV + PDF) | done, from own-feed; gateway 502 |
| 7 | README as a submitted artefact | done |

---

## PART C — DEPLOYMENT AND HOSTING  ⬅ next

The submission form accepts a hosted platform URL with test credentials. Optional, but
it materially helps the "working platform" assessment because a judge can click rather
than watch.

### C1. Containerise both trees
- `backend/Dockerfile` — multi-stage, non-root, base pinned by digest, runtime deps
  only in the final layer, health check on `/healthz`.
- `frontend/Dockerfile` — build stage produces the Vite bundle; serve stage is nginx
  proxying `/api` to the backend with SPA fallback for client-side routes.
- `docker-compose.prod.yml` — backend, frontend, Postgres (PostGIS image), no exposed
  database port, secrets from environment, restart policies.

Verify both images build clean and the stack comes up on a machine with no prior state.

### C2. Deploy
Railway or Render for backend + Postgres; same platform or Vercel/Netlify for the
frontend bundle. Record the choice in an ADR.

Required:
- CORS for the deployed frontend origin — never a wildcard with credentials
- Environment configuration for database URL, JWT secret, gateway host, API base URL
- **Migrations run on deploy**, and `create_app_role.py` before them, or RLS is inert
- **Seed data loaded** — an empty deployed site is worse than no deployed site
- **Evidence crops served** — bake the demo crops into the image or mount a volume
- **Two test accounts** seeded, credentials recorded in `docs/DEMO_RUNBOOK.md`

Document in `docs/DEPLOYMENT.md`: exact steps, environment variables, how to redeploy.

### C3. Post-deployment verification
Do not report the URL as working until, against the deployed instance: logged in with
both accounts, map renders cameras, a journey query returns hops, alert desk loads,
gap analysis loads. Screenshot into `docs/screenshots/deployed/`.

---

## PART D — ONLY IF EVERYTHING ABOVE IS DONE

- Keycloak / OIDC replacing JWT, MFA on live video, journey queries, evidence export
- Extra analytics as independent classifiers on the shared detection stream: vehicle
  counting, intrusion, abandoned object, loitering by PTS dwell, wrong-way, tamper
- Vehicle re-identification — 512-d embedding, pgvector HNSW, used only to bridge gaps
  between confirmed plate reads, always shown as lower-grade evidence
- Observability — Prometheus, structured logging with formatter-level redaction,
  OpenTelemetry from decode to alert delivery
- Image digest pinning — `make pin-digests` so the supply-chain CI job passes

**Do not build face recognition** unless everything above is complete and all four
governance controls fit: off by default per camera, recorded authorisation with a
named officer and expiry, gallery-scoped to an authorised case, separately auditable.
An ungoverned biometric feature is worse than none in front of this jury.

---

## PART E — NOT OURS TO BUILD

Recording the two demonstration videos. Replacing the third-party own-feed clip with
our own stationary footage. Sending `docs/SUPPORT_QUERY.md` to the organisers. The
Solution Presentation and High-Level Design documents — **both complete, do not
regenerate.**

---

## MEASURED RESULTS

| Measurement | Result | HLD claim | Verdict |
|---|---:|---|---|
| Feed-contract checklist | **8/8** | — | passes |
| Journey query, 12-hour window | median 38 ms, p95 60 ms | under 3 s | meets |
| Decode to alert | median 14 ms, p95 1486 ms | under 2 s | meets |
| Motion gate pass rate | 13.7% | — | — |
| Throughput, published 2560×1440 | 0.81 cameras/worker | — | ~98,500 workers for 80k |
| Throughput, 704×396 sub-stream | 3.82 cameras/worker | — | ~21,000 workers for 80k |
| ANPR precision / recall | **not yet measured** | — | harness built |

Throughput is CPU-only with no GPU. ~21,000 workers is the honest floor for a
centralised design and is the reason the architecture pushes analytics to the edge.

---

## PROGRESS LOG

- `2026-08-25` — M0 closed: feed-contract 8/8, infrastructure, CI, evidence records.
- `2026-08-25` — Data layer, source abstraction, SSRF guard (44 tests).
- `2026-08-26` — API foundation, audit chain, geocoded coordinates; ANPR measured on real footage.
- `2026-08-26` — Persistence, matcher, five console screens, demo path, backend/frontend split.
- `2026-08-26` — Part B complete: gap analysis + Coverage screen, signed evidence export
  (Ed25519, tamper-tested), row-level security (unprivileged role, 9 isolation tests,
  append-only audit ledger), ground-truth harness, measured benchmarks, detection output
  report, README rewritten. 135 tests. **Next session: Part C — containerise and deploy.**
