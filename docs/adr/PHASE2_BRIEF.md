# SETU — Phase 2 Build Brief (Sandbox Round → Grand Finale)

You are continuing work on **SETU**, a deployed CCTV integration platform built for the Gujarat
Police Innovation Challenge 2026. **We have been shortlisted.** This brief covers what to build
to convert qualification into a win.

**Scope: everything in this document is in scope for this piece of work.** The sections are
ordered so that each builds on the one before it — follow them in order, because section 2
creates the data model that most later sections depend on. There is no "nice to have" tier here;
treat the whole document as the definition of done.

Read the entire brief before writing code. It contains a strategic diagnosis, not just a feature
list, and if you implement the features without understanding the diagnosis you will build the
wrong thing.

---

## 0. What already exists — do not rebuild it

Run the test suite first (`pytest`, `ruff`, `mypy --strict`) and read `README.md`,
`docs/SETU_High_Level_Design.md` and `docs/DISCOVERY.md` before changing anything. Current state:

- **Backend:** FastAPI, PostgreSQL 16 + PostGIS + TimescaleDB, 319 tests passing, `mypy --strict`
  clean, hash-chained append-only audit ledger, Postgres row-level security via an unprivileged
  app role, Ed25519-signed evidence export, SSRF guard with DNS-rebinding defence.
- **Ingest:** `CameraSource` protocol (`backend/services/ingest/source.py`) with `GatewaySource`,
  `FileSource`, `Deadlined` wrapper. RTSP forced over TCP. All timing from stream PTS, never
  declared FPS — there is a CI guard that fails the build if a second untrusted read of the
  frame-rate property appears. Keep that guard.
- **Analytics:** `anpr.py` (YOLOv9-t ONNX detector + CCT OCR, both MIT), `matcher.py`
  (confusion-aware fuzzy watchlist matching), `plate_grammar.py`, `zones.py` (intrusion polygons),
  `speed.py` (implied-speed flagging, correctly silent on insufficient data),
  `vehicle_counting.py`.
- **Frontend:** React 18 + TypeScript + Vite + Tailwind + MapLibre GL. 11 pages: Login, Map,
  Journey, Alerts, ControlRoom, Health, Gaps, Watchlist, Zones, System, Demo.
- **Deployed:** Netlify (console) + Render (API + Postgres). Live and clickable.

**Three things in the codebase matter enormously for what follows:**

1. `pgvector` is enabled in `backend/migrations/versions/0001_extensions.py` **and is used
   nowhere.** It is installed and idle.
2. `Detection.vehicle_bbox` (JSONB) already exists — the vehicle box is already being captured
   alongside the plate box.
3. `Detection.plate_raw` and `Detection.plate_normalised` are **`nullable=False`**. A detection
   cannot exist without a plate string.

Point 3 is the problem this brief exists to solve.

---

## 1. The strategic diagnosis — read this twice

### The wall every team hits

The government sandbox cameras **cannot read number plates.** This is measured, not suspected:

- SETU's own fullest sweep: **3,938 frames across 25 live cameras → 18 plate regions → 1
  grammar-valid registration** (`GJ09BM3641`, cam22, confidence 0.757).
- Four separate optimisation attempts were implemented, measured, and rejected: detector tiling,
  crop upscaling (identical output at 1×/2×/3×/4× — the library normalises internally), six
  preprocessing variants (nothing beat leaving the crop alone; histogram equalisation destroyed
  the read entirely), and confirming full-resolution frames already reach the detector.
- Two other shortlisted teams independently measured the same ceiling on the same estate. One
  found only 3 of 30 cameras can resolve a plate at all, with the median vehicle presenting
  27.4 px. Another found the best plate any government camera offered was 61 px against a 70 px
  threshold.

The constraint is **pixels on the plate**, set by camera mounting distance and angle. It is not
a model problem and no amount of model tuning fixes it.

### Why this is the whole competition

The hackathon-day test is explicit: *"participants will be provided with a designated vehicle
registration number. The solution must demonstrate its capability to identify, trace, and present
the movement of the corresponding vehicle across the integrated CCTV network as it appears at
different camera locations and times."*

So on evaluation day, every team is handed a plate string and asked to trace it — **on a feed
that cannot read plate strings.** Three outcomes are possible:

1. Teams that keyed everything on the plate string produce nothing. Most teams.
2. Teams that fake it get caught. The rules say mock-ups and simulated interfaces will not be
   considered, and the jury includes NFSU and DA-IICT.
3. **A team that can trace a vehicle without needing the plate to be legible wins the room.**

That third option is the x-factor, and it is architecturally within reach because
`vehicle_bbox` is already captured and `pgvector` is already installed.

### The reframe

**Stop making the plate string the primary key of vehicle identity.**

A number plate is one signal among several. A vehicle also has a colour, a body type, a size, a
shape, a position, a direction of travel, and a time. Those survive at resolutions where OCR
fails completely. A 27-pixel plate is unreadable; the white hatchback it is bolted to is still
obviously a white hatchback.

So: record **every vehicle sighting**, with or without a readable plate, carry an appearance
fingerprint on each one, and make identity a **ranked, evidenced claim** rather than a string
equality test.

This single change:
- Converts the 99.97% of frames currently discarded into usable evidence.
- Makes the scored test case survivable on a feed where ANPR fails.
- Delivers bonus criteria #2 (advanced cross-camera correlation) and #3 (additional reliable
  analytics beyond mandatory ANPR) as a direct consequence, not as bolted-on extras.
- Is defensible to a forensic jury, because every link is scored and shows its evidence rather
  than asserting a match.

---

## 2. Non-negotiable constraints

These are why SETU qualified. Breaking any of them to add a feature is a net loss.

1. **Never fabricate.** No simulated data presented as real, no invented plate reads, no
   interpolated route segments drawn as if observed. The existing rule stands: show cameras where
   the vehicle was actually seen; render gaps as gaps.
2. **Every new claim carries its evidence.** A fingerprint match shows the two crops side by
   side. A convoy claim shows the co-sightings. A judge must be able to disagree with the system
   by looking at what it looked at.
3. **Labels stay honest.** `REPLAY-` cameras keep saying `REPLAY-`. Appearance matches are never
   rendered in the same visual style as a confirmed plate match.
4. **Tests, `ruff`, `mypy --strict` stay green.** Every new feature ships with tests. The PTS/FPS
   CI guard stays.
5. **Migrations have working downgrades.** Every one, as now.
6. **No new AGPL dependencies.** Ultralytics YOLO is excluded on licence grounds; that decision
   stands and is documented. New models must be MIT/Apache/BSD.
7. **No external AI APIs.** Nothing leaves the deployment. This is a privacy property of a
   surveillance system, not a cost decision.
8. **Keep the existing honesty documents current.** `DISCOVERY.md` gets new findings as they are
   measured, including the ones that fail.

---

## 3. Vehicle identity that survives an unreadable plate

Everything downstream depends on this section. Build it first.

### 3.1 Decouple sighting from plate

Add a migration creating a **`vehicle_sighting`** table (TimescaleDB hypertable on
`observed_at_utc`, same pattern as `detection`):

| Column | Notes |
|---|---|
| `id`, `observed_at_utc` | composite PK, as `detection` does |
| `camera_id` | FK |
| `vehicle_bbox` | JSONB |
| `appearance_vector` | `vector(N)` — pgvector, N set by the chosen model |
| `dominant_colour_lab` | JSONB, CIELAB not RGB — see 3.2 |
| `vehicle_class` | car / truck / bus / motorcycle / auto-rickshaw |
| `track_id` | per-camera tracker ID, groups frames of one pass |
| `plate_detection_id` | **nullable** FK to `detection` — set only when a plate was read |
| `crop_path`, `pts_ms`, `clock_confidence`, `quality_score` | as `detection` |

`plate_detection_id` nullable is the entire point: a sighting exists whether or not OCR produced
anything. Keep `detection` as-is for plate reads so nothing downstream breaks; link the two.

Apply the same RLS policy as `detection` (`FORCE ROW LEVEL SECURITY`, department isolation
through `camera`). Index the vector column (HNSW, cosine).

Backfill from existing footage so the feature has data the moment it ships.

### 3.2 Appearance fingerprinting

Extract a fingerprint per sighting from the vehicle crop:

- **Model:** a small MIT/Apache-licensed vehicle re-identification or general embedding model,
  ONNX, CPU-capable. Evaluate candidates and **record the comparison in `DISCOVERY.md`** — which
  you tried, what each scored, which you shipped and why. If no re-ID model performs acceptably
  at this resolution, say so and fall back to the handcrafted descriptor below. A measured
  negative result is a legitimate outcome and belongs in the log.
- **Handcrafted descriptor** (ship this regardless, it is cheap and interpretable): colour
  histogram in **CIELAB** (perceptually uniform, so "same colour under different streetlight" is
  a small distance — RGB is not), aspect ratio, bbox area relative to frame, vertical position,
  coarse edge-orientation histogram.
- **Quality gating:** do not fingerprint a crop below a minimum pixel area, or heavily occluded,
  or motion-blurred beyond a threshold. Store `quality_score` and **weight every downstream match
  by it.** A fingerprint from a 30×20 crop is close to noise and must not carry the same weight as
  one from 300×200. Determine the threshold by measurement and record it.

### 3.3 Candidate-based journey reconstruction

This replaces plate-string-equality as the Journey engine. Given a plate from the judge:

1. **Seed.** Find any sighting whose plate read matches (exact, or confusion-aware fuzzy via
   existing `matcher.py`). This may be a single sighting on a single camera — that is enough.
   If there is no plate read at all, allow seeding from an operator-selected crop instead.
2. **Expand by appearance.** pgvector nearest-neighbour search across all sightings, bounded by
   a time window and by spatial plausibility (existing speed/distance logic in `speed.py` — a
   candidate is only reachable if the implied speed is physically possible given both cameras'
   `confidence_radius_m`).
3. **Score each candidate** on a composite: appearance distance, plate agreement where a plate
   exists, vehicle class agreement, colour agreement, spatio-temporal plausibility, crop quality.
   Emit a **calibrated confidence per hop**, not a binary.
4. **Present as a ranked, branching hypothesis** — not a single asserted route. The operator sees
   the most likely path, alternates where they exist, and can accept or reject each hop. Record
   every accept/reject in the audit ledger: this is both an accountability control and a labelled
   dataset for improving the scorer.

### 3.4 The three-tier evidence model

Every hop in a journey, and every alert, carries exactly one tier. Render each in a visually
distinct way, and make the legend permanent and unmissable in the UI:

| Tier | Basis | Visual treatment |
|---|---|---|
| **Confirmed** | Grammar-valid plate read above confidence floor | Solid, full-strength |
| **Probable** | Fuzzy plate match, or strong appearance match with corroborating attributes | Distinct secondary treatment |
| **Candidate** | Appearance match alone | Clearly provisional — dashed, lower contrast, explicitly labelled |

**A Candidate must never be rendered in the Confirmed style.** This is the single most important
UI rule in the system. The credibility of the whole approach rests on the operator never
mistaking an appearance guess for a plate read.

### 3.5 Honest validation

Build the capability **and** measure it, in the same pass:

- Hand-label a ground-truth set of vehicles appearing on two or more cameras in the own-feed
  footage. Report precision/recall at several confidence thresholds.
- Report **false-link rate** explicitly — how often does the system link two genuinely different
  vehicles? That number is the one a forensic juror will ask for, and quoting it unprompted is
  worth more than a flattering accuracy figure.
- If re-ID performs poorly at government-feed resolution, **publish that**, exactly as the ANPR
  ceiling was published. A documented limit is an asset.

---

## 4. Turn the resolution ceiling into a deliverable

The estate cannot read plates. Most teams will treat this as their problem to hide. **It is
actually Gujarat Police's problem, and SETU is positioned to be the only team that hands them the
answer.**

### 4.1 Per-camera ANPR readiness grading

For every camera, measure and store:

- Median and p90 **plate pixel width** of vehicles observed.
- Median **vehicle pixel width**, and the plate-to-vehicle ratio (this exposes mounting angle:
  a large vehicle with a tiny plate means the camera sees roofs and flanks, not plates).
- Observed **viewing angle** and approximate mounting height where derivable.
- A **grade**: `ANPR-capable` / `marginal — would pass with re-aim` / `not viable for ANPR` /
  `insufficient sample`.
- For marginal cameras, a concrete remedy: re-aim along the approach lane rather than across it,
  reposition, or replace.

Surface this on the Health page and as an exportable report. Grade any newly onboarded camera
automatically.

### 4.2 Analytics assignment follows capability

Do not run ANPR on cameras that cannot support it. Assign per camera, automatically, from its
grade:

- ANPR-capable → full plate pipeline.
- Not viable for ANPR → vehicle counting, flow direction, intrusion zones, appearance
  fingerprinting, dwell detection. **All of which work fine at 27 px.**

This is the practical answer to *"the solution must enable centralised monitoring and AI-powered
video analytics"* across a heterogeneous estate: every camera contributes something useful; no
camera is asked for something it physically cannot deliver.

### 4.3 The Scale Planner

The existing HLD scale figure is honest but bleak: **0.81 cameras per CPU worker at the
resolution ANPR needs → ~98,500 workers for 80,000 cameras.**

Capability grading changes the question. If only a measured fraction of the estate can support
ANPR at all, the state does not need 98,500 ANPR workers — it needs them for the ANPR-capable
subset, and far cheaper analytics everywhere else.

Build a **Scale Planner** page that makes this interactive and defensible:

- Inputs: total cameras, measured ANPR-capable fraction, per-worker throughput (measured, cite
  it), GPU vs CPU, retention period, edge vs central split.
- Outputs: worker count, GPU count, bandwidth at the edge and at the centre, storage by tier
  (hot/warm/cold), and an indicative cost envelope.
- **Every constant traceable** to a measurement in the repo. Show the source of each number
  inline. No invented figures.
- Model both architectures side by side: full-central vs edge-first, so the bandwidth argument
  (video stays at the edge, only ~100-byte detection records travel) is something a judge can
  manipulate rather than take on faith.

This directly answers Step 6 "Plan for Scale": central/regional/edge compute, GPU requirements,
bandwidth and low-bandwidth strategy, hot/warm/cold storage by retention, load balancing and
health checks, HA/backup/DR, and estimated implementation and operational cost.

---

## 5. 3D visualisation that is a forensic tool, not decoration

Two 3D features. Both must earn their place by showing something a 2D view genuinely cannot.
Reject any 3D that is merely a rotating globe.

Use **MapLibre GL's native 3D** (already a dependency): terrain, `fill-extrusion`, sky layer,
pitch/bearing control. Add `deck.gl` only if a specific layer demands it — justify it if you do.

### 5.1 Coverage cones — "where can the state actually see?"

`Camera.heading_deg` and `Camera.confidence_radius_m` already exist and are currently unused in
the map. Render each camera as a **3D viewshed cone**: origin at mounting position, oriented by
heading, length by effective range, width by field of view.

- Colour each cone by its **ANPR readiness grade** from §4.1. The map instantly answers "which
  parts of Gujarat can actually read a plate" versus "which parts we can merely watch."
- Cameras with uncertain position render as a **translucent dome of radius
  `confidence_radius_m`** rather than a cone — uncertainty stays visible in 3D exactly as it is
  in 2D today.
- **Blind spots become the negative space between cones.** This is the thing a flat pin map
  physically cannot show, and it turns the existing Gap Analysis from a table into something a
  senior officer understands in one glance.
- Toggle coverage by analytic (ANPR / counting / intrusion), so "where can we read plates" and
  "where can we merely count" are separate layers.

### 5.2 Time-as-altitude journey plot — "the shape of a movement"

Plot a journey in 3D where **X/Y is geography and Z is time.** A vehicle's route becomes a
polyline rising through time. This is a real forensic instrument:

- **Loitering** reads as a vertical segment — same place, time passing.
- **Convoy** reads as two polylines running parallel at the same altitude.
- **Impossible travel** reads as a near-horizontal jump — large distance, almost no time — which
  is the visual signature of either a misread plate or a cloned plate.
- Segment colour by evidence tier (§3.4), so the operator sees at a glance which parts of a route
  are plate-confirmed and which are appearance-inferred.
- Coverage gaps render as explicit breaks in the line, consistent with current behaviour.
- Scrub time to replay the movement; a judge should be able to drag through the trace.

Keep a 2D fallback and respect `prefers-reduced-motion`. 3D is an additional view, never the only
way to read a route.

---

## 6. Onboarding and integration — production-grade

The problem statement's core is 26 departments with heterogeneous systems. Onboarding currently
works but is invisible on stage and incomplete as a platform capability. This section makes it
both demonstrable and genuinely production-ready.

### 6.1 Onboarding Studio — the live demonstration

1. Operator pastes an RTSP / HLS / ONVIF URL, or drops a CSV, or points at a catalogue endpoint.
2. The platform **probes it live, with every step visible**: TCP reachability → transport
   negotiation (RTSP preferred, HLS fallback, showing which was chosen and why) → codec,
   resolution, measured FPS **versus declared FPS** → first frame decoded and displayed.
3. Auto-grade ANPR readiness (§4.1) from the first frames, showing the grade with its reasoning.
4. Geocode, place on the map with provenance and confidence radius, assign a department.
5. Assign the analytics the camera can actually support (§4.2).
6. Write the whole sequence to the audit ledger.

Show the **declared-versus-measured divergence prominently** — the integration guide warns not to
trust declared FPS, and demonstrating live that a camera claims 25 and delivers 15 proves you took
the warning seriously in a way no slide can.

Support a **bulk mode** that onboards a whole departmental CSV with a live progress view, per-row
accept/reject with reasons, and a summary. "Here is a new department joining the platform" should
take under a minute on stage.

### 6.2 Build the ONVIF adapter

ONVIF has been designed-but-not-built since the beginning and is the single most credible gap in
the federation story. Build it now as a real `CameraSource` implementation:

- `probe()` via ONVIF `GetProfiles` → stream URI, codec, resolution.
- `open()` resolves RTSP from `GetStreamUri`, then reuses the **existing** `StreamSession` —
  transport, backoff and reconnect are already written and contract-tested.
- `observed_at()` / `clock_confidence` from `GetSystemDateAndTime` where the device exposes a
  real clock. This is **better** than the join-time estimate `GatewaySource` is forced into, so
  `clock_confidence` rises rather than falls — say so in the docs.
- `health()` from `GetServiceCapabilities` plus existing frame-level signals.
- **Per-camera credential store**, encrypted at rest, never returned to any client, never logged.
  This is the one genuinely new piece of work and it must not be skipped.
- Contract tests against a mock ONVIF device, in the same style as the existing source tests.

Once this lands, the federation claim is three live adapters plus a vendor-SDK path, not two live
adapters and a promise.

### 6.3 Integration-ready APIs

Bonus criterion #6 names "integration-ready APIs" explicitly. Make the API something another
department's system could genuinely consume:

- **OpenAPI 3.1** (already generated) kept accurate, with every endpoint carrying a description,
  example request and example response.
- **Service accounts and scoped API keys**, distinct from human JWT sessions. Keys are hashed at
  rest, revocable, scoped to a department and a permission set, and every use is audited. A
  departmental integration must never need a human's credentials.
- **Webhooks** for alerts and detections: subscriber registration, HMAC-signed payloads so the
  receiver can verify origin, at-least-once delivery with exponential backoff, a dead-letter view,
  and manual replay. This is how another department's control room receives a SETU alert without
  polling.
- **Idempotency keys** on every mutating endpoint, so a retried onboarding or a retried detection
  submission cannot double-write.
- **Consistent pagination, filtering and sorting** across all list endpoints — cursor-based, not
  offset, on the hypertables.
- **A documented error taxonomy**: stable machine-readable error codes, human-readable messages,
  and a correlation ID on every response that appears in the logs and the audit ledger.
- **Rate limiting** per key with standard headers, and documented retry/backoff semantics.
- **Explicit API versioning** with a stated deprecation policy.
- **`/healthz`, `/readyz` and a build-info endpoint** reporting version, commit, migration head
  and dependency health — what a government ops team will actually monitor.
- **Bulk endpoints** for camera import and watchlist import, with per-row results rather than
  all-or-nothing failure.
- **Generated TypeScript client types** from the OpenAPI document, as now, so console and API
  cannot silently drift.

### 6.4 Operational readiness

- **Structured JSON logging** with correlation IDs end to end.
- **Prometheus-style metrics** endpoint: ingest rate, detection rate, queue depths, inference
  latency percentiles, alert volumes, camera health counts.
- **Graceful shutdown and restart safety** on every worker — a terminated ingest must not corrupt
  or lose the audit chain.
- **Backup and restore documented and actually tested**, including audit-chain verification after
  a restore. Record the test and its result.
- **Clear runbook entries** for the common failures: gateway down, camera flapping, database
  failover, disk pressure.

---

## 7. Analytics beyond ANPR

All of these work at resolutions where plate reading fails, and all use the sighting stream from
§3.1 rather than requiring new ingest.

### 7.1 Operational dashboard

A real control-room dashboard: vehicles/hour per camera and per corridor, class mix (car / truck /
bus / two-wheeler / auto), directional flow, hour-of-day and day-of-week profiles, live versus
7-day-average with deviation highlighted, camera health rollup, alert volumes by tier.

Follow the `dataviz` skill for every chart. Tabular numerals everywhere so figures do not jitter
on update. This is bonus criterion #6 and it is also what "centralised monitoring" means in
practice.

### 7.2 Anomaly detections nobody else will have

Three detections, all derived from existing data, all genuinely useful to police:

- **Cloned-plate detection.** The same plate observed at two cameras separated by a distance that
  cannot be covered in the elapsed time, accounting for both cameras' position uncertainty. The
  existing plausibility logic in `speed.py` already computes the hard part. Either the plate is
  cloned or one read is wrong — surface both hypotheses with their evidence. **No other team will
  have this**, and it is a real, named crime pattern.
- **Convoy detection.** Two or more vehicles repeatedly co-sighted across multiple cameras within
  a short window. Requires a repeat threshold to avoid flagging ordinary traffic — tune it against
  the data and record the threshold and its justification.
- **Dwell / abandonment.** A vehicle stationary in a zone beyond a threshold. Pure sighting data,
  no plate needed, and directly useful for the godown/gate/toll scenario the zones feature targets.

### 7.3 Corridor analytics

Define a corridor as an ordered camera sequence. Report median transit time, today versus
baseline, and flag congestion or unusual flow. This is the kind of output that makes the platform
useful on an ordinary day, not only during an investigation — which is exactly what "operational
value" in bonus criterion #1 means.

---

## 8. Visual identity and operator experience

### 8.1 The problem, stated plainly

A large number of competing teams built with the same AI assistant and have converged on the same
aesthetic: dark navy background, amber or cyan accent, Inter, card grids, glassmorphism. Several
submissions are visually near-identical. **Looking like everyone else is a scoring risk**, because
"Solution Presentation" and "Working Platform and Demonstration" are both explicitly scored and
both are judged by eye in a room where the jury has just seen several similar screens.

Read the `frontend-design` skill before touching the UI.

### 8.2 Direction

**Default to a light, high-density instrument aesthetic.** Think flight-operations console or a
professional terminal, not a consumer dashboard. The rationale is worth saying aloud to a judge:
this is software an officer stares at for an eight-hour shift in a lit control room, and it should
look like it was designed for that, not for a product screenshot.

### 8.3 Theme system

- **Light theme as the default.** Dark as a genuine first-class alternative for night shift, not
  an afterthought.
- Both themes fully tested at the four widths already in the responsive audit (375 / 390 / 768 /
  1024).
- Theme choice persisted per user.
- **Every chart, map layer, video tile, badge and 3D surface** must be verified in both themes.
  The map recolour bug that was fixed earlier is exactly the class of thing to re-check.

### 8.4 Typography

- A **deliberate typeface pairing**, not system defaults — one for interface text, one for
  headings or data display, chosen and justified.
- **Tabular (monospaced) numerals on every metric**, so figures do not shift horizontally as they
  update. This is the single clearest signal of software built for reading numbers.
- A real **monospace for technical strings**: plate numbers, hashes, camera refs, timestamps,
  coordinates, correlation IDs.
- **Gujarati and Devanagari must render correctly** — this is Gujarat Police, and a platform that
  cannot set its own state's script in a camera name looks unfinished. Verify with real strings.
- A defined type scale with consistent line-height and measure. No ad-hoc font sizes.

### 8.5 Colour system

- Build the palette around **meaning, not fashion**: the three evidence tiers (§3.4), alert
  severity, camera health states, and ANPR readiness grades each get a defined, consistent colour
  role.
- Colour must never be the *only* carrier of meaning — pair with shape, icon, label or line style
  for accessibility.
- Verify **WCAG AA contrast** in both themes for every text and UI colour pairing.
- Keep the chart palette consistent with the UI palette via the `dataviz` skill, not invented
  per chart.

### 8.6 Density and layout

- **Information density over whitespace.** More legible data per screen than the current build.
- Hairline rules and clear alignment instead of heavy cards, large radii and drop shadows.
- A consistent spacing scale, consistently applied.
- Tables are a first-class component: sticky headers, sortable columns, column alignment by data
  type (numerals right-aligned), zebra or hairline separation, row density toggle, and a genuinely
  usable empty state.
- Consistent page scaffolding — every page has the same header, breadcrumb, action and status
  grammar.

### 8.7 Motion — functional only

Every animation must encode information. Delete anything decorative.

- **Alert pulse rate tied to severity** — a critical alert visibly pulses faster than a medium one.
- **Map flies to the subject of a new alert**, rather than the operator hunting for it.
- **Counters transition rather than snap**, so a changing number draws the eye to the change.
- **Route segments draw in sequence** during journey replay, in chronological order.
- **Camera health states breathe subtly** — a reconnecting camera looks different from a dead one
  without needing to read the label.
- **Skeleton loaders** that match the shape of the content they are replacing, not spinners.
- **Page and view transitions** that preserve spatial continuity, so the operator never loses
  their place.
- **`prefers-reduced-motion` honoured throughout**, with a tested reduced-motion path. Be ready to
  say in the room that you did this, because it is an accessibility obligation for government
  software.

### 8.8 Guided demonstration and self-explanation

- **Guided Tour:** a dismissible, resumable overlay walking through the three jobs — trace a
  vehicle, watch a location, plan and audit — with real data, real queries, and a one-line
  explanation of what each screen proves.
- **Judge Mode:** pre-fills the demo plate and time window so the scored path is one click from
  the login screen.
- **"What am I looking at?"** affordance on every page: one sentence on what this screen is for,
  what data sits behind it, and what its known limits are. This is the honesty posture expressed
  as interface rather than as a document.
- **Permanent evidence-tier legend** wherever tiers are rendered (§3.4). Never hidden behind a
  tooltip.

### 8.9 Interaction and accessibility

- **Command palette** (`Cmd/Ctrl-K`) for fast navigation and search — jump to a camera, a plate, a
  page, an alert.
- **Keyboard shortcuts** for the control-room actions an operator repeats, with a discoverable
  shortcut reference.
- **Visible focus rings** and full keyboard navigability on every interactive element.
- **Semantic HTML and ARIA labels**, especially on the map, charts and video tiles.
- **Designed empty, loading and error states** for every view — never a blank panel, never a raw
  error string. Each error state says what happened and what the operator can do next.

### 8.10 Perceived performance

- Skeleton states, optimistic UI, and prefetch on the likely next step.
- Keep the **cold-start warning** already in the submission note.
- A platform that feels fast is read as a platform that works. Every interaction a judge performs
  must feel instant.

### 8.11 One signature view

Every project needs one screen a judge remembers. **The 3D coverage map (§5.1) is it.** Make it
the login destination and give it the most design attention of anything in this section.

---

## 9. Full evaluation criteria — build against these explicitly

### A. Common evaluation areas (all submissions assessed on these)

1. **Successful Test Case** — successful onboarding and operation of the solution on the
   Government-provided CCTV feed, including live or recorded viewing and the required analytics
   output.
2. **Solution Presentation** — clarity and completeness of the submitted PPT or PDF, including
   problem understanding, proposed model, justification, solution overview, and key features.
3. **Solution Architecture** — technical soundness, feasibility, security, interoperability, and
   clarity of the proposed High-Level Design and architecture diagrams.
4. **Working Platform and Demonstration** — maturity of the actual working platform demonstrated
   through the participant's own feed and the Government-provided feed.
5. **Video Analytics Output** — quality and usefulness of ANPR, vehicle or person detection,
   intrusion detection, object detection, timestamps, and output reports.
6. **Scalability and PoC Readiness** — readiness to scale toward approximately 80,000 cameras and
   preparedness for the on-site proof-of-concept.
7. **Submission Completeness** — completeness, accessibility, and consistency of all required
   documents, videos, reports, links, credentials, and supporting technical information.

### B. Bonus consideration

*Bonus consideration may be given for additional capabilities that are relevant, functional, and
demonstrated as part of the working solution. Bonus features will not compensate for failure to
meet any mandatory submission or test requirement.*

| Bonus criterion | Where this brief delivers it |
|---|---|
| Innovative hybrid or customised architecture with clear operational value | Existing Model 1+2+3 hybrid, plus capability-driven analytics assignment (§4.2) and corridor analytics (§7.3) |
| Advanced cross-camera vehicle movement tracking or multi-camera correlation | Appearance-based identity and candidate journeys (§3), convoy detection (§7.2) |
| Additional reliable analytics beyond the mandatory ANPR requirement | Counting, flow, dwell, intrusion zones, cloned-plate and convoy detection (§7) |
| Strong edge-processing, bandwidth-optimisation, or low-connectivity operation | Measured 86.3% frame filtering, edge-first scale model, Scale Planner (§4.3) |
| Enhanced cybersecurity, privacy protection, auditability, or role-based access controls | Existing RLS, hash-chained ledger, Ed25519 evidence, SSRF guard, plus scoped API keys and credential store (§6.3, §6.2) — extend the ledger to cover every new accept/reject decision (§3.3) |
| Operational dashboards, automated alerts, health monitoring, or integration-ready APIs | Operational dashboard (§7.1), readiness grading on Health (§4.1), webhooks and integration APIs (§6.3), metrics and runbooks (§6.4) |

### C. Test scenario, verbatim on the points that constrain design

- Cameras are deployed across different departments and use various technologies, formats, VMS
  platforms, and storage mechanisms.
- Teams must onboard the available cameras onto one integrated platform.
- The solution must enable centralised monitoring and AI-powered video analytics.
- Participants will be provided with a **designated vehicle registration number**. The solution
  must demonstrate its capability to identify, trace, and present the movement of the
  corresponding vehicle across the integrated CCTV network as it appears at different camera
  locations and times.
- The solution must demonstrate continuous cross-referencing of live CCTV feeds with a
  representative watchlist database and generate automated real-time alerts upon detecting a
  match. Participants may use their own representative watchlist database.

**Expected output:** demonstration of identifying and tracing the designated vehicle using the
registration number provided during evaluation; complete route traversed, including timestamped
and location-wise movement history; a working watchlist database with continuous cross-referencing
and automated real-time alert generation; and evidence of successful CCTV integration, AI-powered
video analytics, interoperability, scalability and end-to-end system performance.

**Demonstrations must show working software. Mock-ups, animations, simulated interfaces, or
concept videos without an operational backend will not be considered.**

### D. Plan for Scale — what must be explained

- Central, regional, and edge-compute requirements.
- GPU or accelerator requirements for video analytics.
- Expected network bandwidth and low-bandwidth strategies.
- Hot, warm, and cold storage assumptions based on retention periods.
- Load balancing, horizontal scaling, monitoring, logging, and health checks.
- High availability, backup, disaster recovery, and cybersecurity controls.
- Estimated implementation and operational costs.

---

## 10. What not to do

- Do not add face recognition. It was deliberately excluded pending four governance controls
  (per-camera opt-in, authorised case scoping, recorded per-officer authorisation with expiry,
  separate audit trail). That decision is documented and defensible; reversing it quietly would
  undermine the whole privacy posture.
- Do not fabricate data to make a demo smoother. Not one row.
- Do not remove or soften existing limitation disclosures. They are an asset. Update them.
- Do not quote the sub-stream throughput figure as a scaling result — it describes an operating
  point at which nothing is legible, and the HLD already says so.
- Do not let appearance matching be presented with the confidence of a plate read, anywhere, ever.
- Do not add 3D that does not show something 2D cannot.
- Do not break the PTS/FPS CI guard, the RLS policies, the audit chain, or the test suite.

---

## 11. Definition of done

- Every section above implemented, tested, deployed, and green on `pytest`, `ruff` and
  `mypy --strict`.
- `DISCOVERY.md` extended with every new measurement, **including the failures** — the re-ID model
  comparison, the quality thresholds and how they were chosen, the convoy threshold, the
  false-link rate, the backup/restore test result.
- README and HLD updated to match the built system, with no stale numbers anywhere.
- A refreshed demonstration script walking the three jobs plus the new capabilities.
- A one-page summary of what changed since Phase 1 and what each change is worth against the
  evaluation criteria in §9.
