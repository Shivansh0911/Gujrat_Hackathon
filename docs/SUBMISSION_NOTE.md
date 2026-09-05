# SETU — supplementary submission note

**Team SETU** · Category 1 (student team) · Gujarat Police Innovation Challenge 2026 — CCTV Integration

This document exists so that nothing a reviewer needs is more than one page away. It
carries the access details, a five-minute path through the platform, a map from each
evaluation area to where it is demonstrated, and — deliberately — the list of things
this system does **not** do. Every figure below was read from the running deployment on
2026-09-04, not estimated.

---

## 1. Reviewer access

| | |
|---|---|
| **Console** | https://setu-gujarat.netlify.app |
| **API + interactive docs** | https://setu-api-ai7z.onrender.com/docs |
| **Repository** | https://github.com/Shivansh0911/Gujrat_Hackathon |

| Role | Username | Password |
|---|---|---|
| System Administrator | `admin` | *(supplied on the submission form)* |
| Control Room Operator | `operator` | *(supplied on the submission form)* |

> **Please allow up to a minute for the first page to load.** The platform is hosted on
> a free tier that suspends the container when idle; the first request wakes it. Every
> request after that is normal speed. This is a hosting-cost decision, not a performance
> characteristic of the system.

**Please try the operator account as well as the administrator one.** The difference is
the point: an operator can view cameras, trace vehicles and work alerts, but cannot add
a watchlist entry or create a zone. The API refuses it — `403 adding a watchlist entry
requires admin` — rather than the interface merely hiding a button.

---

## 2. Five minutes, in order

1. **GIS Map** — the camera registry. Note that a camera we can only place to a district
   is drawn as a *circle*, not a pin. A false pin produces an authoritative-looking route
   that is wrong, which is exactly what a forensic reviewer looks for.
2. **Journey** — enter `KA25AB1542`, widen the dates to cover August–September, type any
   purpose, and trace. You should get **4 hops, 413 km**. Read the two lines the page
   prints without being asked: how many cameras were excluded for having no coordinate,
   and how many produced no detection. The second is labelled *a coverage gap, not an
   absence of the vehicle*.
3. **Export signed evidence (PDF)** — the same reconstruction, re-run server-side, signed,
   with the evidence crops embedded.
4. **Alert Desk** — watchlist matches and zone intrusions together, each carrying the crop,
   camera, time, confidence, and the listing that authorised the match.
5. **Zones** — select `cam22`. The polygon is drawn on that camera's own live view, with
   real past detections plotted on it. This is the analytic that works on the government
   estate today, because it needs a vehicle box rather than a readable plate.
6. **System** — press verify on the audit chain. Every vehicle search in this deployment,
   including the one you just ran, is in it with the purpose you typed.

---

## 3. Where each evaluation area is demonstrated

### Common evaluation areas

| Area | Where | Evidence |
|---|---|---|
| **1. Successful test case** | Government feed onboarded, viewed and analysed | `reports/evidence/gateway-output-report-2026-09-03.md`; feed-contract compliance **8/8** against §2.4 in `reports/evidence/preflight-*.json` |
| **2. Solution presentation** | Submitted separately | — |
| **3. Solution architecture** | `docs/SETU_High_Level_Design.md` | Hybrid: Model 1 control plane + Model 3 adapter layer + Model 2 operator surface, with Model 4 as a documented migration path |
| **4. Working platform** | The live URL above | 16/16 end-to-end checks, and a 17-step browser walk-through covering every user flow |
| **5. Video analytics output** | Alert Desk, Journey, Demo | Detected plates with timestamps in `reports/detections-*.csv`; ANPR scored at **29.6% precision / 29.6% recall / 26.9% CER** against `data/seed/anpr_ground_truth.csv` |
| **6. Scalability and PoC readiness** | `docs/SETU_High_Level_Design.md`, `docs/EDGE_OPTIMISATION.md` | Throughput measured per camera and extrapolated with the assumptions stated; the edge-processing argument is derived from the measured resolution effect rather than asserted |
| **7. Submission completeness** | This document and `docs/SUBMISSION_CHECKLIST.md` | — |

### Bonus consideration

| Bonus item | How it is met |
|---|---|
| Innovative hybrid architecture | Model 1 + 3 + 2 by deliberate design, with the reasoning and the rejected alternatives recorded |
| Cross-camera vehicle tracking | Route reconstruction with per-hop uncertainty, plausibility gating and declared gaps |
| Additional reliable analytics beyond ANPR | **Intrusion-zone detection**, running on a live government camera; **implied-speed flagging**, built, tested, and correctly silent on data that cannot support it |
| Edge processing / bandwidth optimisation | `docs/EDGE_OPTIMISATION.md` — argued from the measured finding that sub-stream ingest buys throughput at an operating point where nothing is legible |
| Cybersecurity, privacy, auditability, RBAC | Hash-chained tamper-evident ledger; PostgreSQL row-level security; Ed25519-signed evidence; HMAC-signed short-lived media URLs; SSRF guard; rate-limited login; secret scanning in CI |
| Operational dashboards, alerts, health, APIs | Health screen with declared-versus-measured frame rate and a one-click §2.5 fault report; OpenAPI 3.1 with generated client types |

---

## 4. What is real, and what is constructed

Stated plainly, because a reviewer who discovers this unaided is right to discount
everything else in the submission.

**Real.** Every detection in this system is inference output with an evidence crop you
can open and judge for yourself — 25 detections, 25 crops, 25 confidence values, none
inserted by hand. The camera registry comes from the estate's own catalogue. Codec,
resolution and frame rate were measured by decoding the streams, because the catalogue
publishes neither. Alerts are produced by classifiers running over stored detections.
The audit chain records actions that actually happened.

**Constructed, and labelled as such inside the product.**

1. **The four `REPLAY-…` cameras.** Route reconstruction needs one vehicle seen at
   several cameras, and this estate has not yet produced that. Our own footage is
   therefore replayed through four registry positions, running the full pipeline
   separately for each. Every detection is genuine inference with a real crop; **only
   which camera saw it is simulated**, and the `REPLAY` prefix is visible in the
   interface and on every route hop.
2. **The watchlist.** Ten representative entries, which the rules expressly permit. These
   are the only rows a human typed. One of them lists the plate that appears in our own
   footage, so that matching can be demonstrated at all.
3. **The own-feed clip.** Third-party CC BY 3.0 Wikimedia footage of the Hubli–Dharwad
   BRTS route, attributed in `data/own_feed/SOURCE.md`. It is Karnataka footage, which is
   why its plates read `KA…`.
4. **Camera coordinates.** Geocoded from each camera's own name against Nominatim, with
   the provenance and a confidence radius stored per camera. Six cameras that resolve to
   nothing are left `unset` and excluded from spatial queries rather than placed at a
   guess.

---

## 5. Measured results, as at 2026-09-04

### The government estate

| | |
|---|---:|
| Cameras catalogued | 30 |
| Producing frames | 25 |
| Frames decoded | 3,938 |
| Plate regions detected | 18 |
| Grammar-valid registrations | **1** — `GJ09BM3641` |
| Feed-contract compliance (§2.4) | **8/8** |

The estate is genuinely heterogeneous, and the registry proves it rather than asserting
it — **24 × H.264 and 6 × H.265** across five distinct resolutions, from 2560×1440 down
to 960×576. Every one of those values was measured from the stream, because the
catalogue contains only an id and a name.

### The platform

| | |
|---|---:|
| Cameras in the registry | 65 |
| Government cameras placed on the map | 24 of 30 — 17 geocoded, 7 district centroid, 6 honestly unset |
| Districts covered by gap analysis | 10, with 34 identified gaps |
| Watchlist entries | 10 |
| Alerts raised | 19 — 6 watchlist matches, 13 zone intrusions |
| Audit ledger | **917 entries, chain verified valid** |

---

## 6. Technology, and why each piece was chosen

Nothing here is load-bearing by accident. Where a choice was forced by a measurement
rather than a preference, the measurement is named.

### AI models

| Model | Role | Why this one |
|---|---|---|
| **YOLOv9-t**, 384×384, ONNX | Licence-plate detection | Small enough to run on a shared CPU with no GPU, which is the operating point a statewide rollout has to survive at the edge. Full-resolution frames are handed to it, never downscaled copies — verified, because a downscaled frame is the single easiest way to lose a plate silently |
| **CCT-S-v2-global** via `fast-plate-ocr`, ONNX | Plate character recognition | Emits ten character slots with per-character confidences, which is what makes multi-frame fusion possible. An earlier recogniser could not emit a ten-character plate at all, and that defect alone held accuracy at 0% |

Both run locally through **ONNX Runtime**. **No external AI API is called** — no
frames, crops or registrations leave the deployment. For a system processing public
surveillance footage that is a privacy property, not a cost decision.

Everything before the models matters as much as the models. A **motion gate** drops
frames with no scene change so the detector is not run on an empty road; an **Indian
registration grammar filter** rejects reads that cannot be a plate; and **multi-frame
fusion** votes character-by-character across a track, aligned per observation rather
than assumed right-aligned — a fix made after fusing three reads of one plate produced
something worse than the best single read.

### Platform

| Layer | Choice | Why |
|---|---|---|
| **Backend** | Python 3.12, **FastAPI**, Uvicorn | One language from stream decode to HTTP response; OpenAPI 3.1 is generated from the code, and the frontend's types are generated from that, so an API change that breaks the console fails at compile time |
| **Database** | **PostgreSQL 16** with **PostGIS**, TimescaleDB, pgvector, pg_trgm | PostGIS does the spatial work route reconstruction depends on — distance, containment, uncertainty radii — in the database rather than in application code. Row-level security lives here too, which is why department scoping survives an application bug |
| **ORM / migrations** | SQLAlchemy 2, **Alembic** | Every migration has a working downgrade, because a schema you cannot reverse is a schema you cannot deploy carefully |
| **Video** | OpenCV + FFmpeg, RTSP forced over TCP, **hls.js** in the browser | RTSP/TCP for inference per the integration guide's first rule; HLS for viewing, through our own authenticated proxy so the estate credential never reaches a browser |
| **Frontend** | **React 18**, TypeScript, Vite, Tailwind, **MapLibre GL** | MapLibre renders the registry and routes from real geometry; no map-provider account is required, so the console runs in an air-gapped deployment |
| **Evidence** | ReportLab, **Ed25519** signatures, HMAC-signed media URLs | An exported route becomes a document in a case file, so it is signed, and the manifest commits to the photographs as well as the text |
| **Hosting** | Render (API + PostgreSQL), Netlify (console), Docker Compose for on-premise | The same containers run on a laptop, on a free tier, or inside a government data centre. Nothing in the code knows which |

### Engineering discipline

`pytest` (319 tests), `Ruff`, `mypy --strict`, `gitleaks` secret scanning and a
declared-FPS guard all run in GitHub Actions on every commit. The FPS guard is worth
naming: §2.2 of the integration guide says never to trust a camera's reported frame
rate, so a CI step counts the reads of that property and fails if a second one appears.
It is a rule enforced by the build rather than by a code review.

---

## 7. What this system does not do

1. **It cannot yet trace a vehicle across the government cameras.** 3,938 frames across
   25 live cameras have produced one grammar-valid registration. Route reconstruction
   needs the same plate at two or more placed cameras, so it is demonstrated on own-feed
   material. The limit is pixels on the plate rather than the pipeline: **four
   optimisations were implemented, measured and rejected** — detector tiling, crop
   upscaling, six preprocessing variants, and a confirmation that full-resolution frames
   already reach the detector. Each is recorded in `docs/DISCOVERY.md` with the numbers
   that rejected it.
2. **ANPR accuracy is 29.6%, and we report it rather than rounding it.** It was 0% until
   three defects were found by measurement. Resolution still bounds it.
3. **Department attribution is almost entirely unavailable.** All five departments are
   seeded and row-level security enforces the scoping, but the estate's catalogue
   publishes no department — checked three ways: `/api/ingest`, the estate's own
   `cameras.json`, and its dashboard HTML all carry an id and a name and nothing else.
   One camera states its department in its own label and is filed accordingly
   (`cam19` → Panchayat); the other twenty-nine sit at the default. Assigning them would
   mean fabricating the one field a department-scoped access-control model exists to
   protect.
4. **Speed flagging raises nothing today, by design.** It requires one plate at two
   genuinely different, positioned, non-replay cameras. The exclusion is a `WHERE` clause
   with a test that fails if it is removed.
5. **Evidence images written at runtime do not survive a redeploy** on this hosting tier.

---

## 8. Evidence you can check without trusting us

- `reports/evidence/` — dated ingest records and output reports, one estate per report
- `data/seed/anpr_ground_truth.csv` — the annotations the accuracy figure is scored against
- `data/evidence/crops/` — every evidence crop, including the ones that turned out not to
  be number plates
- `docs/DISCOVERY.md` — 22 findings, including the ones where a measurement contradicted
  us and the optimisations that were rejected
- `GET /audit/verify` on the live API — chain verification is open to any authenticated
  actor rather than to administrators only, because a tamper-evidence control that only
  the role able to alter records may check is not a control
- `docs/SUPPORT_QUERY.md` — two measured issues raised with the organisers: that
  `/api/ingest` returns only `id` and `name` where the integration guide documents
  location, codec, live status and stream properties, and that the HLS plane delivers
  about 5 KB/s per connection

---

## 9. Repository map

```
backend/services/analytics/    ANPR pipeline, zone and speed classifiers, matcher
backend/services/api/          FastAPI application, auth, RLS tenancy, audit ledger
backend/services/ingest/       Camera sources — gateway (RTSP/HLS) and file
backend/services/registry/     Camera model, lifecycle, import, observation write-back
backend/migrations/            Alembic migrations, each with a working downgrade
frontend/src/pages/            The ten console screens
docs/                          HLD, discovery log, deployment, runbook, this note
reports/evidence/              Dated measurement records
```

---

**Contact** · Team SETU · issues at https://github.com/Shivansh0911/Gujrat_Hackathon/issues
