# Project SETU — High-Level Design

**Gujarat Police Innovation Challenge 2026 — CCTV Integration Hackathon, Category 1**

Version 1.0 · 2026-08-28 · generated from the repository, not from a plan

---

## How to read this document

Every performance and capability figure here was produced by running the system, and
each is traceable to a dated artefact in `reports/evidence/`. Where a measurement
contradicted an earlier claim, the measurement stands and the claim was changed — the
audit trail for that is [`HLD_RECONCILIATION.md`](HLD_RECONCILIATION.md), which is
deliberately preserved rather than tidied away.

Two things this document does not do: quote a number it cannot source, and describe a
capability that is not built. Where something is designed but not implemented, it is
labelled as such in §9.

---

## 1. The problem

Gujarat operates roughly **80,000 CCTV cameras across 26 government departments**.
They do not interoperate: different vendors, different VMS platforms, different
storage and retention, different ages and AMC periods. An investigator tracing a
vehicle today asks 26 organisations separately.

The defining capability SETU delivers: **an officer supplies a vehicle registration
number and sees that vehicle's route across the camera network on a map — with
timestamps, evidence photographs, and honest gaps where it was not seen.** Separately,
a watchlisted vehicle appearing on a live feed raises an alert within seconds.

---

## 2. Solution model — a deliberate hybrid

The challenge offered four reference models. SETU is **Model 1 + Model 3 + Model 2**,
with Model 4 documented as a migration path rather than a starting position.

| Model | Role in SETU | Why |
|---|---|---|
| **1 — Centralised registry + GIS** | The control plane. Mandatory, and built first. | Every camera carries one identity record: who owns it, where it is, what it can do, and what the platform is currently permitted to pull from it. |
| **3 — Federation middleware** | The structural core. | One `CameraSource` interface, many implementations. This is how the control plane reaches heterogeneous reality without an N-to-N integration burden. |
| **2 — Unified viewing** | The operator surface. | One console over all departments, scoped by role and department. |
| **4 — Central analytics + storage** | Analytics adopted; **storage monopoly rejected**. | Its requirement that all video flow to one datacentre is unaffordable at this resolution (§8.1). |

**The governing principle: video stays at the edge; metadata flows to the centre.**

Gujarat does not have a camera problem. It has 26 camera *ecosystems*. Any
architecture that begins by replacing them is unaffordable; any architecture that
connects directly to each creates an integration burden that grows with every AMC
renewal. Model 1 is therefore not a spreadsheet with a map — it is the control plane,
and Model 3's adapter layer is how it reaches the estate.

---

## 3. Overall architecture

### 3.1 Diagram A — layered architecture

Describe as five horizontal bands, top to bottom, with a vertical governance column
spanning all of them:

```
┌──────────────────────────────────────────────────────────┐  ┌───────────────┐
│ CONSOLE   Map · Journey · Alert Desk · Coverage ·        │  │  GOVERNANCE   │
│           Health · Watchlist · System                    │  │               │
├──────────────────────────────────────────────────────────┤  │ Hash-chained  │
│ API       FastAPI · OpenAPI 3.1 · JWT · department       │  │ audit ledger  │
│           scoping · signed media URLs                    │  │               │
├──────────────────────────────────────────────────────────┤  │ Row-level     │
│ ANALYTICS ANPR pipeline · watchlist matcher ·            │  │ security      │
│           journey reconstruction · vehicle counting      │  │               │
├──────────────────────────────────────────────────────────┤  │ Ed25519       │
│ INGEST    CameraSource protocol                          │  │ evidence      │
│           ├─ GatewaySource (HLS/RTSP)                    │  │ signing       │
│           ├─ FileSource (recorded)                       │  │               │
│           └─ future: ONVIF, vendor SDK, RTSP direct      │  │ SSRF guard    │
├──────────────────────────────────────────────────────────┤  │               │
│ REGISTRY  PostgreSQL 16 · PostGIS · TimescaleDB ·        │  │ Mandatory     │
│           pgvector · pg_trgm · pgcrypto                  │  │ purpose       │
└──────────────────────────────────────────────────────────┘  └───────────────┘
```

The governance column is drawn spanning every band deliberately: these are not a layer
that can be bypassed by going around it.

### 3.2 Diagram B — the federation boundary

Draw as three vertical zones left to right:

```
   EDGE / DEPARTMENT              FEDERATION              CENTRE
 ┌────────────────────┐      ┌──────────────────┐   ┌──────────────────┐
 │ Dept A VMS ────────┼──┐   │                  │   │                  │
 │ Dept B NVR ────────┼──┼──▶│  CameraSource    │──▶│  Registry (GIS)  │
 │ Dept C RTSP ───────┼──┘   │  adapters        │   │  Detections      │
 │ ...26 departments  │      │                  │   │  Alerts          │
 │                    │      │  decode → gate → │   │  Audit ledger    │
 │  VIDEO STAYS HERE  │      │  detect → OCR    │   │  METADATA ONLY   │
 └────────────────────┘      └──────────────────┘   └──────────────────┘
        │                            │                       │
        └── full resolution ─────────┘                       │
            frames, never leave                              │
                                     └── ~hundreds of bytes ─┘
                                         per detection
```

The arrow crossing into the centre carries detection records, not frames. That single
property is what makes the cost model in §8.1 work.

### 3.3 Diagram C — the journey query

Draw as a left-to-right sequence with a branch:

```
Officer enters plate + PURPOSE
        │
        ▼
  Audit ledger  ◀── written BEFORE the query runs, not after
        │
        ▼
  Confusion-aware candidate search (0/O, 1/I, 8/B, 5/S, 2/Z …)
        │
        ▼
  Sightings, ordered by observed_at_utc (from PTS)
        │
        ├──▶ plausible hop  ──▶ route polyline segment
        │      (implied speed within limits)
        │
        └──▶ implausible   ──▶ REJECTED, shown with reason
                                 (never silently dropped)
        │
        ▼
  Coverage gaps rendered as dashed segments:
  "no detection at <camera> — coverage gap"
```

The rejected branch is drawn as a first-class output. A route that silently omits an
implausible hop is a route an investigator cannot check.

---

## 4. Integrating heterogeneous cameras and VMS platforms

### 4.1 The adapter contract

Every camera reaches the platform through one Python protocol:

```python
class CameraSource(Protocol):
    def probe(self) -> CameraCapabilities: ...
    def open(self) -> Iterator[Frame]: ...
    def health(self) -> HealthReport: ...
    def observed_at(self, frame: Frame) -> datetime: ...
    @property
    def clock_confidence(self) -> float: ...
    def close(self) -> None: ...
```

Two implementations ship: `GatewaySource` (the challenge gateway, HLS with RTSP
preferred where reachable) and `FileSource` (recorded footage). Adding ONVIF, a vendor
SDK, or direct RTSP is a new class, not a change to anything above it.

**Onboarding is a platform capability, not a script.** `POST /cameras/bulk-import`
accepts a departmental CSV and validates row by row, so a spreadsheet with two bad
lines imports the rest and reports those two with line numbers. The seed script and
the endpoint share one validation implementation (`services/registry/camera_import.py`)
so they cannot drift into disagreeing about what a valid camera is.

### 4.2 What the estate actually returns

Measured against the live gateway, recorded in [`DISCOVERY.md`](DISCOVERY.md):

| Finding | Consequence for the design |
|---|---|
| 19 of 30 cameras declare **no codec or resolution at all** | Capabilities are probed, never trusted from the catalogue |
| **12 of 16** cameras that declare a frame rate diverge from it; one by **+87%** | All timing is derived from PTS; declared FPS is never used |
| RTSP:8554 and WHEP:8889 unreachable (Cloudflare proxies 443/80 only) | HLS is the working transport; TCP is still forced where RTSP is reachable |
| HLS is a relative path, and the gateway gates on a `cookieCheck` parameter FFmpeg drops | The master playlist is resolved by us and the variant handed to FFmpeg |
| The catalogue's `live` flag is a **claim, not a health signal** | Liveness is established by probing before any check relies on it |

That last row is why the feed-contract preflight discovers which cameras actually
deliver frames before testing anything against them.

### 4.3 Feed-contract compliance — 8/8

The organiser's §2.4 checklist, verified empirically rather than asserted:

| # | Requirement | Result |
|---|---|---|
| 1 | RTSP forced over TCP, HLS fallback when 8554 blocked | PASS |
| 2 | No timing logic depends on declared FPS | PASS — exactly 2 reference-only reads, CI-enforced |
| 3 | Inter-frame gaps do not stall the pipeline | PASS — 109 intervals, median 40 ms |
| 4 | Reconnect with backoff, resumes frames | PASS |
| 5 | Decoder warnings on join are logged, not fatal | PASS |
| 6 | Camera list read from `/api/ingest` | PASS — 30 cameras |
| 7 | Mixed H.264/H.265 and mixed resolutions | PASS — 4 distinct resolutions |
| 8 | Sane across a scene discontinuity | PASS — 0 false cuts, real cut detected |

`reports/evidence/preflight-*.json`. A check the gateway cannot supply data for
reports **NOT EXERCISED** — neither a pass nor a failure — because "our pipeline
mishandled this" and "the feed gave us nothing to handle" are different findings.

---

## 5. Ingesting and processing dispersed live streams

### 5.1 The pipeline

```
StreamSession (per camera)
   │  forced TCP where reachable · HLS variant re-resolved on rejoin
   │  jittered exponential backoff · non-fatal join warnings
   ▼
Frame (image, pts_ms, session_id)
   │
   ▼
PTS-interval sampler ──▶ 5 analytic fps        [83.2% of frames removed]
   │
   ▼
MotionGate (downscaled absdiff) ──────────────  [32.7% of survivors removed]
   │
   ▼
Plate detector (YOLOv9-t ONNX, MIT)
   │
   ▼
PlateTracker ──▶ multi-frame fusion ──▶ Indian plate grammar
   │
   ▼
Detection record ──▶ Postgres (TimescaleDB hypertable)
   │
   ▼
Watchlist matcher ──▶ Alert ──▶ WebSocket to the console
```

Scene discontinuities (the recording loop point, or a camera switch) raise an event
that resets tracker association and issues a new session id, while evidence already
written is preserved.

### 5.2 Timing

All observation times derive from **presentation timestamps**, never from arrival time
and never from a declared frame rate. A live stream's PTS is relative to when we
joined, so `GatewaySource` anchors the timeline at the first frame and carries an
explicit `clock_confidence` recorded per detection — the mapping is an estimate and is
labelled as one.

This is enforced, not merely intended: a CI job asserts the codebase contains exactly
two reference-only reads of `CAP_PROP_FPS` and fails the build on a third.

### 5.3 Compute reduction — measured 2026-08-28

Over 901 frames of 2560×1440 footage, CPU only:

| Analytic rate | Motion gate | Frames reaching detector | Wall time |
|---|---|---:|---:|
| 30 fps | off | 901 — 100% | 41.9 s |
| 30 fps | on | 606 — 67.3% | 36.6 s |
| 5 fps | off | 151 — 16.8% | 19.8 s |
| **5 fps (production)** | **on** | **123 — 13.7%** | **19.0 s** |

**86.3% of decoded frames never reach the detector**, and wall time falls 54.7%.
Sampling is the dominant contributor (83.2%); the motion gate adds 32.7% of what
survives. Enabling the gate changes the output not at all — 22 plate regions and 2
grammar-valid registrations either way — so it is a free saving rather than a quality
trade. Full method: [`EDGE_OPTIMISATION.md`](EDGE_OPTIMISATION.md).

---

## 6. Correlating feeds with watchlists, and generating alerts

### 6.1 Matching

Matching is **confusion-aware**, because OCR errors are not random: `0/O/D/Q`,
`1/I/L`, `8/B`, `5/S`, `2/Z`, `6/G`, `7/T`, `4/A`, `9/P`. A listed `GJ01AB1234` is
still found in a read of `GJO1AB1Z34`, and the alert names **which characters were
treated as equivalent** so an officer can judge the match rather than trust it.

Scores are deliberately separated:

| Match | Score |
|---|---:|
| Exact | 1.00 |
| One explained substitution | 0.72 |
| Two explained substitutions | 0.55 |

An exact match is certainty about the *read*, not about the vehicle — the OCR can
still be wrong — so fuzzy scores sit well below it and a floor suppresses the rest.

### 6.2 Alert lifecycle

```
Detection ──▶ matcher
                │
                ├── same plate, same camera, within 5 min ──▶ existing alert, count++
                │      (DEDUP_WINDOW)
                │
                ├── same plate, different camera, within 2 h ──▶ MOVEMENT alert
                │      (MOVEMENT_WINDOW — sized for a real inter-city leg:
                │       Junagadh→Rajkot is ~100 km and over an hour)
                │
                └── otherwise ──▶ NEW alert
                       │
                       ▼
             WebSocket push ──▶ Alert Desk
                       │
        ACKNOWLEDGE ──▶ RESOLVE (disposition: confirmed / false positive / …)
                       │
                       ▼
              Audit ledger + false-positive rate on the Health screen
```

Every card carries the evidence crop, three timestamps (`observed_at_utc`, stream PTS,
ingest), match type and score, the corrections applied, and the watchlist authority
and case reference. Operator dispositions feed a measured false-positive rate per
camera — the platform measuring its own precision rather than asserting it.

### 6.3 Watchlist governance

`valid_to` is **NOT NULL**. An entry without an expiry is a permanent shadow record on
a citizen created by omission, so the API refuses one and the console defaults the
field to 30 days. Adding an entry requires admin, is audited before it takes effect,
and expiry stops matching automatically.

---

## 7. AI-powered analytics

### 7.1 Models, and why these

| Role | Model | Licence |
|---|---|---|
| Plate detection | YOLOv9-t 384px end-to-end ONNX (`open-image-models`) | **MIT** |
| Plate recognition | CCT ONNX, 10 slots (`fast-plate-ocr`, `cct-s-v2-global-model`) | **MIT** |

Ultralytics YOLO is **not a dependency at any tier**. It is AGPL-3.0, and the network
clause applies squarely to a platform used over a network by police officers —
committing a state procurement to a per-deployment licence negotiation with a single
vendor is a worse outcome than choosing a different model. Both models run on
`onnxruntime` with no GPU requirement, which is what makes the CPU figures in §5.3 and
§8.1 meaningful. See [`adr/0003-anpr-model-selection.md`](adr/0003-anpr-model-selection.md).

### 7.2 Accuracy — measured against annotated ground truth

> Plate recognition uses `open-image-models` (YOLOv9-t, ONNX) for detection and
> `fast-plate-ocr` (CCT, ONNX) for recognition, both MIT-licensed and both CPU-only
> (see ADR 0003).
>
> **Measured plate-level accuracy is 29.6% precision and 29.6% recall, with a
> character error rate of 26.9%**, scored against a by-eye annotation of every
> evidence crop in `data/seed/anpr_ground_truth.csv`. On the government feed, four of
> the seven evidence crops from the one camera that frames plates at a readable size
> are read exactly right.
>
> That figure was **0.0%** when it was first measured, and the gap between the two is
> the substance of the engineering. Three defects were found, none of which was
> visible without measuring against annotated ground truth:
>
> - The configured recogniser had **nine** classification heads. Indian registrations
>   have up to **ten** characters, so a full-length plate could not be represented at
>   all — every read was exactly nine characters long, and every full-length plate was
>   wrong before inference began.
> - Detections were associated across frames by bounding-box overlap alone. At the 5
>   fps analytic rate a plate moves further than its own width between samples, so the
>   boxes did not overlap and multi-frame fusion never ran. Every plate was decided by
>   a single noisy read.
> - Fusion combined reads of differing length by right-aligning them, which is correct
>   for a dropped leading character and wrong for a dropped trailing one. When wrong,
>   it shifted every position and voted unrelated characters against each other.
>
> A read whose fused confidence falls below 0.5 is now not published at all. Of the
> crops a reviewer found illegible, every pipeline read scored 0.46 or below, while
> the correct reads scored 0.79 and 0.94. Reporting a wrong registration to an
> investigator is worse than reporting nothing, and a wrong registration carrying a
> high confidence is worse still, because it will be acted upon.
>
> Accuracy remains bounded by what the estate publishes. The same pipeline reads 8
> grammar-valid plates at 2560×1440, 2 at 1280×720 and none at 704×396, and across
> 9,713 frames from 20 live government cameras only one camera framed plates at a
> readable size. This is the empirical case for processing at the edge, where full
> resolution still exists, and for treating plate legibility as a camera-placement and
> procurement requirement rather than an analytics problem.

### 7.3 Tracking and multi-frame fusion

`PlateTracker` associates detections across frames within a session and fuses their
reads. Fusion is right-aligned because OCR drops leading characters more often than
trailing ones, and confidence is `agreement × strength` — agreement across frames
multiplied by per-character OCR confidence — so a single-frame read cannot reach the
confidence of a corroborated one. A scene discontinuity flushes and resets tracks.

### 7.4 Analytics beyond ANPR

**Vehicle counting** runs as an independent classifier over the detection stream ANPR
already produces: no second ingest path, no additional camera load, no change to
`anpr.py`. `GET /analytics/vehicle-counts` reports per-bucket and per-camera counts,
surfaced on the Health screen.

It reports `reads`, `distinct_plates` and `cameras_reporting` as separate numbers, and
ships a caveat **in the response body**, because "N vehicles crossed" would be wrong in
two directions: one vehicle can produce several reads, and a vehicle whose plate is
unreadable produces none. Every figure is a floor on traffic, never an estimate of it.

### 7.5 Deliberately not built

**No face recognition.** It stays unbuilt until four governance controls fit: off by
default per camera, recorded authorisation naming an officer with an expiry,
gallery scoped to an authorised case, and separately auditable. An ungoverned biometric
capability is worse than none.

---

## 8. Scalability, interoperability, security, performance

### 8.1 Scalability to ~80,000 cameras

> Throughput was measured on a single CPU worker with no GPU. At the resolution the
> cameras publish (2560x1440) one worker sustains 0.81 cameras, which extrapolates to
> approximately 98,500 workers for an 80,000-camera estate processed centrally.
> Ingesting a lower-resolution sub-stream raises that to 3.82 cameras per worker at
> 704x396, but **accuracy collapses before the saving is realised**: the same footage
> yields 8 grammar-valid plates at 2560x1440, 2 at 1280x720 and none at all at
> 704x396. The sub-stream figure is therefore a throughput at an operating point where
> the recogniser reads nothing, and is not quoted as a scaling result.
>
> The defensible conclusion is the full-resolution one, and it is the stronger
> argument: a centralised design costs on the order of 98,500 CPU workers at the
> resolution ANPR actually requires. This is why SETU pushes analytics to the edge and
> moves metadata rather than video. Establishing the lowest resolution that preserves
> plate legibility is identified as the single highest-value optimisation outstanding.

**The scaling path**, in order of what each step buys:

1. **Edge inference.** Each site processes its own cameras at full resolution and
   emits detection records of a few hundred bytes. The compute reductions in §5.3 are
   what make an edge node affordable.
2. **GPU where density demands it.** All figures above are CPU-only; a GPU changes the
   worker count, not the architecture.
3. **Durable event log.** Domain events already flow through an interface whose shape
   is Kafka's, with an in-process backend at 30 cameras. Swapping the backend is a
   deployment choice, not a rewrite — see [`adr/0001-event-bus-abstraction.md`](adr/0001-event-bus-abstraction.md).
4. **Time-partitioned storage.** `detection` is already a TimescaleDB hypertable
   partitioned on `observed_at_utc`, the column every time-window query groups by.

### 8.2 Performance — measured

| Measurement | Result | Requirement | Verdict |
|---|---:|---|---|
| Journey query, 12-hour window | median **38 ms**, p95 **60 ms** | under 3 s | meets |
| Decode to alert | median **14 ms**, p95 **1486 ms** | under 2 s | meets |
| Frames reaching the detector | **13.7%** | — | 86.3% filtered |
| Throughput, 2560×1440, CPU-only | **0.81 cameras/worker** | — | §8.1 |

### 8.3 Interoperability

- **OpenAPI 3.1**, with the console's TypeScript types generated from it — the client
  cannot drift from the contract.
- **`CameraSource`** for ingest; a new camera type is a new class.
- **Standard formats**: HLS and RTSP in, GeoJSON on the map, CSV and signed PDF out.
- **PostGIS geography** in EPSG:4326, so positions are interoperable with any GIS.
- **Evidence verifiable without SETU**: an Ed25519 detached signature over a canonical
  JSON manifest, checkable with any Ed25519 implementation.

### 8.4 Security

| Control | Implementation |
|---|---|
| **Three-level tenant isolation** | Gateway policy, scoped accessors, **and Postgres row-level security**. Nine tests issue raw SQL bypassing the application entirely |
| **Unprivileged database role** | `setu_app` is NOSUPERUSER/NOBYPASSRLS with table-scoped grants. A superuser carries `rolbypassrls` and would ignore every policy while all isolation tests still passed |
| **Append-only audit ledger** | `entry_hash = SHA256(prev_hash ‖ canonical_json(entry))`. The application holds no UPDATE grant on the table |
| **Mandatory purpose** | Written to the ledger *before* a journey query executes |
| **Signed evidence** | Ed25519 detached signature over a canonical manifest |
| **Signed media URLs** | Evidence crops are served only via short-lived HMAC-signed URLs bound to a single filename |
| **SSRF defence** | Scheme and port allowlists, DNS checks, connect-time re-verification against rebinding, redirects refused, size cap — 44 adversarial tests |
| **`alg=none` rejection** | Explicitly, before verification, proven with a forged token |
| **Credential redaction** | At the logging formatter, so a credential cannot reach a sink even if interpolated |
| **Rate-limited login** | Failed authentications are audited in an independent transaction so a 401 rollback cannot erase them |
| **Watchlist expiry** | `NOT NULL` — §6.3 |

#### Authentication, and the department-federated upgrade path

Access is a **closed, two-role system**: `admin` (System Administrator) and
`operator` (Control Room Operator), with passwords issued from the deployment
environment. There is no self-registration, no default credential, and no
consumer identity provider. `POST /auth/login` returns 503 rather than falling back
to a known password if the environment does not supply one. Passwords are bcrypt
hashed, login is rate-limited, and failed attempts are audited in an independent
transaction so that a 401's rollback cannot erase them.

**No third-party consumer login, deliberately.** Adding Google sign-in was considered
and rejected on threat-model grounds rather than effort. This is a law-enforcement
system with a closed user population; unscoped consumer OAuth would let anyone
holding a Gmail account reach the authenticated boundary and attempt access, which
enlarges the attack surface without serving any real user. It would only be an
improvement if restricted to a government Workspace domain, and no such domain exists
for this deployment.

**What department-wise access actually requires, and how far it is built.** The RBAC
model already scopes every read to a department: `camera_scope()` filters at the
application layer, and Postgres row-level security enforces the same boundary against
raw SQL that bypasses the application entirely (§8.4). What is missing is not
enforcement but *identity* — a way for an officer to authenticate as a member of the
Traffic department rather than being handed an operator password.

The production answer is **OIDC federation against each department's own directory**,
with Keycloak as the broker. `docker-compose.yml` already provisions a Keycloak
service and a realm import, behind a `planned` profile so it is not started by
default; the enforcement side it would feed is complete and tested. What remains is
the token exchange and a department claim mapped onto the existing scope.

This was deliberately **not** implemented during the hackathon timeline. Replacing
token issuance is an architecture change whose failure mode is losing access to the
platform entirely, and the current scheme is correct for a closed evaluation
deployment. The gap is one of convenience and directory integration, not of
enforcement.

Dependency posture: `pip-audit --strict` on every push, with exactly one suppression
(`PYSEC-2026-1325` in `ecdsa`, unreachable here and unfixable upstream), documented in
`SECURITY.md`. Container images are digest-pinned, and CI fails on a floating tag.

### 8.5 Quality gates

207 automated tests · `mypy --strict` clean · `ruff` clean · declared-FPS guard ·
gitleaks over full history · CycloneDX SBOM.

---

## 9. Prerequisites, assumptions, and what is needed from departments

### 9.1 Needed from each participating department

1. **A camera inventory** — reference, location description, and coordinates where
   held. `POST /cameras/bulk-import` accepts this as CSV; missing coordinates are
   represented as `unset` rather than guessed.
2. **A stream endpoint and its transport** — RTSP URL, HLS playlist, ONVIF endpoint or
   VMS API, with credentials issued to the platform.
3. **Network reachability** — which ports are open from where. RTSP on 8554 was
   unreachable throughout this build; that must be established per department rather
   than assumed.
4. **Retention policy per camera** — how long the department's own recording holds,
   which bounds how far back a journey query can be corroborated.
5. **A named data-protection owner** and the lawful basis for processing.
6. **Watchlist authority** — who may add an entry, and the case-reference convention.

### 9.2 Assumptions

- Cameras are fixed, not PTZ. A moving camera invalidates the position a detection is
  attributed to.
- Clocks are not assumed synchronised; PTS anchoring plus an explicit
  `clock_confidence` is how that is handled rather than requiring NTP everywhere.
- The estate is IPv4/IPv6 reachable from a federation node placed inside the
  department's network.
- Plate legibility is a **camera placement and procurement** property, not something
  analytics can recover (§7.2).

### 9.3 Built but not deployed, and not built

| Item | State |
|---|---|
| Edge deployment | Architecture documented and the compute case measured; **no edge node deployed** |
| Bandwidth saving | Follows from moving metadata not video; **no figure measured** |
| Kafka event backend | Interface in place, in-process backend active; Kafka backend not written |
| Keycloak / OIDC | Interim JWT in use; migration path documented |
| Vehicle re-identification | `pgvector` present; embeddings not built |
| Intrusion / zone detection | Not built |
| Adaptive analytic rate | Fixed at 5 fps; load-adaptive gating not built |
| Face recognition | **Deliberately not built** — §7.5 |

### 9.4 Known limitations, stated up front

1. **The own-feed clip is third-party** — CC BY 3.0 Wikimedia footage from Karnataka,
   attributed in `data/own_feed/SOURCE.md`, so plates read `KA…` not `GJ…`.
2. **The four `REPLAY-…` cameras are a replay harness**, not live feeds. Every
   detection is genuine inference with a real crop; only *which camera saw it* is
   simulated, and the `REPLAY` prefix is visible in the UI.
3. **ANPR accuracy is 29.6%**, and the ceiling is what the estate publishes (§7.2).
4. **The gateway media plane is intermittent.** Cameras producing frames measured 17,
   then 25, then 18 of 30 across 27–30 August, with a total 502 in between. The
   2026-08-30 sweep decoded 5,055 frames for **zero** grammar-valid registrations
   against two on 27 August — same pipeline, different feed.

---

## 10. Evidence index

| Claim | Artefact |
|---|---|
| Feed-contract 8/8 | `reports/preflight.json`, `reports/evidence/preflight-*` |
| Declared vs measured FPS | `reports/evidence/catalogue-probe-*` |
| Journey and alert latency, throughput | `reports/evidence/benchmarks-*` |
| ANPR accuracy | `reports/evidence/anpr-accuracy-*`, `data/seed/anpr_ground_truth.csv` |
| Government-feed output | `reports/evidence/gateway-output-report-*`, `gateway-ingest-*` |
| Compute reduction | [`EDGE_OPTIMISATION.md`](EDGE_OPTIMISATION.md) |
| Gateway behaviour findings | [`DISCOVERY.md`](DISCOVERY.md) |
| Claim-by-claim audit trail | [`HLD_RECONCILIATION.md`](HLD_RECONCILIATION.md) |
| Architecture decisions | [`adr/`](adr/) |
