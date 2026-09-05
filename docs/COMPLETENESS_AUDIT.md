# Completeness audit — 2026-08-30

Every claim in `README.md` and `docs/` checked against the deployed system and the
code as it stands today, rather than against what an earlier session believed when it
wrote the sentence. Same rule as `DISCOVERY.md`: a number here was produced by running
something, and where a document disagreed with the measurement, the document changed.

> **Note, 2026-09-05.** The console was renamed from `setu-gujrat.netlify.app` to
> `setu-gujarat.netlify.app`. The URLs in the dated entries below have been updated to
> the current one so they still resolve; the deployment they describe is the same one.

Deployment audited: **https://setu-gujarat.netlify.app** (console) against
**https://setu-api-ai7z.onrender.com** (API), Render Postgres in Singapore.

---

## What was found stale, and corrected

| # | Claim | Where it was wrong | Corrected to |
|---|---|---|---|
| 1 | Cameras producing frames | `BUILD_INSTRUCTION.md` still said **17 of 30**, from the first 2026-08-27 reading. Four other documents said 25 of 30 | All now state the range and its dates: 17 → 25 → **18 of 30**, with a complete 502 in between |
| 2 | "No hosted URL yet" | `README.md` limitation 6, written before the deployment existed | Replaced with the live URL and the fact that the deployed instance carries own-feed detections only |
| 3 | Hosted URL row | `SUBMISSION_CHECKLIST.md` row 13 still described a pending **Railway** push | Now the live Netlify + Render URLs, 10/10 verified, pointing at the ADR 0004 addendum for why the platform changed |
| 4 | Government-feed output report | Checklist row 8 cited one run and its two valid registrations as settled | Now cites **both** dated runs, including the one that produced zero |
| 5 | Test-account row | Referred to `.env.prod`, which is the local compose stack | Now names `deploy-secrets.env` and the two operational role labels |
| 6 | Gateway test case | `HLD_RECONCILIATION.md` described a single sweep | Both sweeps, with the observation that the pipeline was identical and the feed was not |

## What was checked and found already accurate

| Claim | Verified how |
|---|---|
| ANPR accuracy **29.6% precision / 29.6% recall, 26.9% CER** | `ground_truth.py score` re-run today against the committed annotations: 8 correct of 27 human-legible crops. Unchanged by this session's work |
| The same four numbers appear in every document | Grepped across `README.md` and all of `docs/`: `29.6` and `26.9` are consistent in README, BUILD_INSTRUCTION, DEMO_RUNBOOK, DISCOVERY and the HLD. No document disagrees with another |
| `setu_app` is unprivileged on the deployed database | Queried `pg_roles` directly: `rolsuper=false`, `rolbypassrls=false` |
| `detection` is a real TimescaleDB hypertable on Render | Queried `timescaledb_information.hypertables` |
| Ten deployment checks pass | `verify_deployment.py --api-origin …` → 10/10 |
| Authentication description | Two roles, environment-issued passwords, no consumer OAuth. README and HLD §8.4 both now state this and the OIDC upgrade path |

---

## The one number that changed for the worse

The 2026-08-30 sweep is a worse result than 2026-08-27, and both are kept:

| | 2026-08-27 | 2026-08-30 |
|---|---:|---:|
| Cameras producing frames | 25 of 30 | **18 of 30** |
| Frames decoded | 9,158 | 5,055 |
| Plate regions detected | 30 | 17 |
| **Grammar-valid registrations** | 2 | **0** |

The pipeline is unchanged between the two runs; only the feed differs. Quoting the
better number alone would misrepresent what the estate reliably delivers, so the
submission cites both and the range.

---

## Still outstanding, and honestly so

| Item | State |
|---|---|
| **Gateway detections in the deployed database** | **Still zero.** The ingest ran correctly against the deployed database on 2026-08-30 and inserted nothing, because no plate was legible in that window. The console now labels feed source explicitly, so this is visible rather than inferred |
| **`gitleaks` over full history** | **Not run this session.** The binary could not be downloaded on this connection and the Docker daemon was unavailable. Last clean run was 2026-08-27, over 18 commits. Outstanding |
| Own-feed and government-feed demo videos | Human tasks, unchanged |
| `docs/SUPPORT_QUERY.md` | Not sent. Human task |
| Deployed screenshots | Predate this session's labelling changes; not recaptured |

---

## Method

- Deployed database queried directly over the external connection string for camera
  counts, detection counts by source, role privileges and hypertable status.
- `verify_deployment.py` run against the live console with `--api-origin`, which is
  what a split-origin deployment requires.
- `ground_truth.py score` re-run against committed annotations.
- `ingest_gateway.py` run against the deployed database across all 30 catalogued
  cameras; `gateway_report.py` merged both dated passes.
- Numeric claims grepped across `README.md` and `docs/*.md` to confirm no two
  documents state different figures for the same measurement.


---

# Completeness audit — 2026-08-31

Second pass. The 2026-08-30 entry above stands unchanged; this appends what changed
today rather than rewriting it.

Deployment audited: **https://setu-gujarat.netlify.app** against
**https://setu-api-ai7z.onrender.com**.

## The gateway, measured again

| Date | Cameras producing frames | Valid registrations |
|---|---:|---:|
| 2026-08-27 | 25 of 30 | 2 |
| 2026-08-30 | 18 of 30 | 0 |
| **2026-08-31** | **0 of 30** — Cloudflare 502 on every endpoint | — |

Checked once at the start of the session and not looped on. The origin behind
Cloudflare is down; this is the organiser's infrastructure and there is nothing in
this repository to fix. `gateway-ingest` was therefore not run, and **the 2026-08-30
figures remain the most recent real measurement** everywhere they are quoted.

## What was built about it

The platform's *handling* of the outage changed; the outage did not.

A passive watcher polls the catalogue once a minute and records transitions, so the
console can answer "when did it stop" rather than only "is it up". `unreachable_since`
is set at the transition and cleared on recovery — a detail with its own test, because
overwriting it each poll would report a four-hour outage as "down for one minute",
every minute. Reachability is three-valued: `null` means not yet checked, and the card
says so rather than presenting an unknown as an outage.

The reconnect logic was **read and left alone**. A whole-domain 502 and a single
camera failing take the identical path — connect fails, jittered backoff, retry — and
the full jitter already exists so that ~50 workers do not become a thundering herd
against infrastructure we do not own. Nothing mishandled a full-domain outage, so
nothing was changed.

`DEMO_RUNBOOK.md` §3 now carries the three-day table and the wording to use if the
feed drops mid-demonstration.

## Found on the deployed API, still outstanding

**`SETU_GATEWAY_HOST` is not set on Render.** The live watcher reports
`no gateway configured (SETU_GATEWAY_HOST unset)` rather than a 502, which means the
deployed API does not know where the gateway is and has never attempted to reach it.
Last session made the endpoints report this instead of returning an opaque 500; the
variable itself still needs adding in the Render dashboard. Until it is, live camera
preview and **Compare with gateway** cannot work even when the organiser's feed
recovers.

## What was checked and corrected

| # | Item | Finding |
|---|---|---|
| 1 | Responsive layout | The first audit **passed every page and was wrong**. `main` carries `overflow-hidden`, so content is clipped rather than scrolled and `scrollWidth` never grows. At 375px the sidebar took 55% of the viewport and the GIS page rendered with **no map at all** |
| 2 | Audit method | Corrected twice: it could not see clipping, then it could not tell clipping from legitimate scrolling inside `overflow-x-auto`. It now checks geometry, walks up for a scrollable ancestor, and fails when navigation exceeds a third of a phone viewport |
| 3 | Navigation | Sidebar becomes a drawer below `md`, closing on route change, 44px targets, transition disabled under `prefers-reduced-motion` |
| 4 | Health table | Ten columns clipped at **every** width including 1024px. Now scrolls inside its own container |
| 5 | Screenshot scripts | `capture_screenshots.mjs` and `record_demo.mjs` still filled `input[autocomplete="username"]`, which the role-button login removed last session. They were broken and nothing had noticed |
| 6 | Department filter | Verified with a temporary fixture: two cameras moved to `HEALTH`, both codes offered, each filter returned the right set, then reverted. **The filter needs data, not code** — all 34 cameras sit in `HOME` because the catalogue has no departmental demarcation. No departments were fabricated |
| 7 | ANPR accuracy | Re-measured: **29.6% precision / 29.6% recall, 26.9% CER**, unchanged. Consistent across README, BUILD_INSTRUCTION, DEMO_RUNBOOK, DISCOVERY and the HLD |
| 8 | Secret scanning | Resolved after three sessions outstanding — see below |

## Secret scanning, resolved

Both routes succeeded, not just one.

**CI `gitleaks`, full history** — the *Secret scan (full history)* step is green on
`42700d8`, run
[33355613476](https://github.com/Shivansh0911/Gujrat_Hackathon/actions/runs/33355613476).
This is the authoritative check: history is where a committed-then-deleted secret
hides, and a working-tree scan cannot see it.

**Local `detect-secrets`** — installs through pip where the gitleaks binary would not
download and Docker was unavailable. 18 findings over tracked files, each read and
confirmed a false positive: the `change-me-locally` placeholder the API refuses to
start on, the regex in `redact.py` that *detects* credentials, a test constant, and
fifteen `git_sha` provenance fields whose 40-character hashes read as high-entropy
hex. Committed as `.secrets.baseline`; documented in `SECURITY.md`.

No credential is committed. `.env`, `.env.prod` and `deploy-secrets.env` appear only
in an `--all-files` scan, never in the tracked-file one.

## Added this session

| Capability | Evidence |
|---|---|
| Passive gateway watch | `GET /health/gateway`, card on Health, 10 tests |
| Manual camera onboarding | `POST /cameras`, form on System, 9 tests run against the deployed database |
| Sample gap-analysis report | `reports/gap-analysis-2026-08-31T06-14-08Z.md` — 10 districts, 28 camera gaps, grouped by remedy. `make gap-report` |
| Control Room video wall | 1 / 2×2 / 2×3 tiles, capped at six concurrent streams |
| Reads-per-hour chart | Labelled axes, hover detail, caption |

## Not built, and why

**Speed-based flagging.** It needs genuine cross-camera timestamps. The only
multi-camera data available is the REPLAY harness, where camera attribution is
simulated — a speed alert computed from fake attribution is a fabricated capability
presented as a real one, which is worse than the feature's absence. The last gateway
run to produce any valid plate at all was 27 August, with two. It stays unbuilt.

## Method

Deployed database queried directly; `verify_deployment.py` with `--api-origin`;
`ground_truth.py score` re-run; `responsive_audit.mjs` at 375/390/768/1024 against the
live site; gateway probed once; `detect-secrets` over tracked files; CI job status read
from the Actions API.


---

# Close-out — 2026-08-31

Final engineering pass. The two entries above stand; this is the definitive statement
of what this submission is and is not, written to be read by someone who was not here.

## What SETU is

One web platform that federates a heterogeneous camera estate. A judge supplies a
registration number and sees that vehicle's route across the network on a map, with
timestamps, evidence photographs and honest gaps where it was not seen. A watchlisted
vehicle appearing on a feed raises an alert within seconds.

Live at **https://setu-gujarat.netlify.app** (Render API, Netlify console, Render
Postgres in Singapore), passing **10 of 10** deployment checks at close-out.

| | |
|---|---:|
| Backend tests | **209 passing**, 42 skipped without Postgres |
| `mypy --strict` | clean, 51 source files |
| `ruff` / `ruff format` | clean, 101 files |
| Console screens | **9**, all on real endpoints |
| Responsive | 9 pages × 375/390/768/1024 px, no overflow, clipping or nav dominance |
| Deployment checks | **10 / 10** |
| Secret scan | CI `gitleaks` full history green; `detect-secrets` baseline committed |

## What is genuinely real

Every detection is actual ONNX inference over real road footage — YOLOv9-t detection,
CCT recognition, both MIT. No fixtures, no seeded rows pretending to be output. The
evidence crops are the pixels the recogniser actually read, which is why some are
visibly wrong.

Row-level security is enforced in Postgres, not just in the application: nine tests
issue raw SQL that bypasses the application entirely, and `setu_app` is verified
`rolsuper=false, rolbypassrls=false` **on the deployed database**. The audit ledger is
hash-chained and append-only — the application holds no UPDATE grant on it. Evidence
exports carry Ed25519 detached signatures verifiable without SETU. Evidence images are
served only through short-lived signed URLs.

`detection` is a real TimescaleDB hypertable on the deployed instance.

## What is not

**The government gateway is down and has been all day.** Cloudflare 502 on every
endpoint — the organiser's origin, not their edge. Measured across four days:

| Date | Cameras producing frames | Valid registrations |
|---|---:|---:|
| 2026-08-27 | 25 of 30 | 2 |
| 2026-08-30 | 18 of 30 | 0 |
| 2026-08-31 | **0 of 30** | — |

`SETU_GATEWAY_HOST` **is** now set on Render — confirmed today, the watcher's error
changed from "no gateway configured" to a real 502, which means the deployed API is
actively reaching for the feed and being refused. The configuration problem is fixed;
the dependency is not, and cannot be from here.

**The deployed database therefore holds own-feed detections only.** Twenty detections,
all from the bundled clip via the four REPLAY cameras. Zero from the gateway. The
console labels the source of every hop, alert and camera, and draws cameras with no
detections hollow, so this is visible rather than inferred.

**ANPR accuracy is 29.6%** precision and recall, 26.9% character error rate, over 27
hand-annotated crops. It was 0.0% until three defects were found by measurement: a
recogniser with nine classification heads against ten-character Indian plates, a
tracker that never associated anything so multi-frame fusion never ran, and fusion
voting misaligned characters against each other. The ceiling is what the estate
publishes — 9,158 frames across 25 live cameras yielded three human-legible plates.

**Four REPLAY cameras are a replay harness.** Real inference, real evidence photos;
only *which camera saw it* is simulated. The prefix is visible in the console.

**Speed-based flagging is not built.** It needs genuine cross-camera timestamps, and
the only multi-camera data available is that harness. A speed alert computed from
simulated attribution would be a fabricated capability presented as a real one.

**The own-feed clip is third-party** — CC BY 3.0 Wikimedia, Hubli–Dharwad, so plates
read `KA…` not `GJ…`. Decided today: it stays. Replacing it seven days out would
invalidate the 27-crop annotation set the 29.6% figure is scored against, trading a
measured number for an unmeasured one. The confusion it caused was never about the
clip's origin but about nothing on screen naming the feed, and that is fixed.

## Human tasks, genuinely outstanding

Neither is startable by tooling, and neither is ticked.

- **Both demonstration videos.** `DEMO_RUNBOOK.md` has the screen-by-screen script;
  §3 covers what to say if the gateway is dark, which today it would be.
- **Send `docs/SUPPORT_QUERY.md`.** More warranted than when it was written.

Also worth a human: spot-check the ANPR annotations, since one person read all 27
crops.

## The thing this project got right

Every headline number here was produced by running something, and three of them got
*worse* when measured properly. The sub-stream throughput figure was withdrawn because
the recogniser reads nothing at that resolution. The 8/8 feed-contract pass was found
to be conditional on which camera the harness happened to pick. A responsive audit
written this week passed every page while the GIS screen had no map on it at all.

Each of those was caught by measurement and each is written down, here and in
`DISCOVERY.md` and `HLD_RECONCILIATION.md`, with the date and the number. A reviewer
who goes looking for the weak points will find them already documented — which is the
only reason to trust the parts that are strong.


---

# Partner test round — 2026-08-31 (evening)

Avani tested the deployed console on a phone and ran the feed-contract preflight on
her own machine. Five findings, all real, all fixed. This entry records them because
three of them were things our own checks had passed.

## What she found

| # | Finding | Status |
|---|---|---|
| 1 | Gap analysis opens in a tab but cannot be taken away as a document | Fixed — signed PDF export |
| 2 | Coverage and Journey are unusable on a phone | Fixed — both stack below `lg` |
| 3 | Content runs off the right edge with no way to scroll to it | Fixed — same root cause as 2 |
| 4 | Too many floating cards on Journey; the map is not visible | Fixed — cards move inline below `lg` |
| 5 | `preflight_check.py` dies on a traceback | Fixed — catalogue failure is caught and reported |

## Findings 2, 3 and 4 were one defect

Coverage and Journey each put a fixed-width side panel next to a `flex-1` map. At
375 px the panel alone (27 rem / 432 px) is wider than the viewport, so the map
resolved to **zero width** and the panel's right-hand third was clipped by `main`'s
`overflow-hidden` with nothing to scroll. The floating cards then appeared to hover
over nothing, because there was nothing.

This is the *third* time this exact defect has been found, after the GIS page on
30 Aug. The responsive audit did not catch it either time, and the reason is
specific: its clipping rule ignores an element hanging less than a quarter of its own
width past the edge, and 432 px in a 375 px viewport hangs 13%. The audit measured
the panel and never the map.

**The audit now measures the map directly**, as a fraction of `main` rather than of
the viewport — a healthy desktop layout legitimately gives the map only half the
screen, so a viewport-relative threshold would flag the good case. Validated against
a reconstruction of both layouts before being trusted:

| Layout | 375 px | 768 px | 1024 px |
|---|---|---|---|
| Old | map at **0%** → FAIL | **23%** → FAIL | — |
| New | 100% → pass | 100% → pass | 47% → pass |

The 768 px column is why the breakpoint moved from `md` to `lg`: on a tablet the old
layout left **128 px of map** beside a 432 px panel, and nobody had ever looked,
because nothing measured it.

## The preflight traceback

`preflight_check.py` called `fetch_catalogue` unguarded. With the gateway returning
502 the script died before check 1, producing a bare `requests.exceptions.HTTPError`.
Two things were wrong with that. It reads as "SETU is broken" when the finding is
"the feed did not answer", and it aborted the two checks that need no network at all.

Now: the fetch is caught, the static checks still run, every live check reports NOT
EXERCISED with the reason, and **exit code 3** distinguishes an upstream outage from
both a pass (0) and a pipeline failure (1). Two further places reported an outage as
a defect and were fixed with it — check 4 dereferenced the `None` camera its own
signature allows, and check 6 called an empty catalogue a sourcing violation.

Measured against the live gateway, 31 Aug:

```
1/8 checks passed, 0 failed, 7 not exercised     exit 3
HTTPError: 502 Server Error: Bad Gateway for url: https://live.corp8.cloud/api/ingest
```

Before this change the same command produced a traceback and no report at all.

**`SUBMISSION_CHECKLIST.md` and `HLD_RECONCILIATION.md` were corrected**: both cited
"8/8" against `reports/preflight.json`, a scratch file that this run overwrote. They
now cite the dated evidence record from 27 Aug, the last day the checklist could be
exercised, and state the 31 Aug result beside it.

## Gap analysis as a document

`GET /cameras/gap-analysis/export` returns the analysis as a PDF carrying the same
Ed25519 detached signature and canonical manifest as the evidence export, audited as
`EXPORT_GAP_ANALYSIS` before the document is produced. A planning document naming
specific cameras as defective gets forwarded to people who were not in the room; they
should be able to check the figures were not edited on the way. Four tests, including
one that requires an altered manifest to fail verification.

## Verified on the deployed site — 2026-09-01

Everything above is now confirmed against the live deployment, not a reconstruction.

| Check | Result |
|---|---|
| CI on `73d8d5b` | success — run 33419686501, all three jobs |
| Responsive audit vs `setu-gujarat.netlify.app` | **32/32 pass**, 8 pages × 375/390/768/1024 px |
| Coverage @ 375 px, screenshot read | map renders with gap circles, panel below it full-width, stat cards in two columns, nothing clipped |
| Journey @ 375 px, screenshot read | form stacks, all five purpose presets fit, map fully visible, no overlapping cards |
| Gap-analysis PDF, live end to end | HTTP 200, `application/pdf`, 3 pages, 8,093 bytes, 34 cameras / 10 districts / 28 gaps matching the console |
| Its signature and audit trail | 128-hex Ed25519 signature, 64-hex public key, **audit ledger entry 107** |

The screenshots were opened and read, not merely produced. That step is not optional
here: on 30 Aug this same audit reported every page green while the GIS screen had no
map on it, and the only thing that caught it was looking at the picture.

Backend at the same commit: 213 tests pass, `ruff` clean, `mypy --strict` clean under
the pinned numpy.

One correction to the record: during this session the Render API was read as down for
~28 minutes. It was not. `/health` is not a route — the health endpoints are
`/health/cameras` and `/health/gateway` — so a 404 was misread as a continuing 502
after a genuine cold-start window. The service recovered on its own and the deployed
OpenAPI carries `/cameras/gap-analysis/export`.


---

# The estate moved — 2026-09-02

The entries above stand as written. Two statements in them are now superseded and are
called out here rather than edited, because a dated record that quietly changes is worth
less than one that accumulates:

* **"Speed-based flagging is not built"** — it is, as of today. See below.
* **"The gateway is down"** — it moved. The old host is gone; a new one answers.

## What changed on the organiser's side

The feed is no longer `live.corp8.cloud/api/ingest`. It is `cctv.corp8.cloud` behind a
login, with the media plane on a bare public IP because a CDN cannot proxy RTSP.

| | Before | Now |
|---|---|---|
| Catalogue | `live.corp8.cloud/api/ingest`, open | `cctv.corp8.cloud/cameras.json`, **behind a password** |
| Catalogue contents | per-camera URLs, codec, resolution, fps, `live` flag | **`id` and `name`. Nothing else** |
| RTSP | 8554 **unreachable** — Cloudflare proxies 443/80 only | `103.250.160.189:8554`, **open** |
| HLS | `/live/stream/{id}/index.m3u8` | `/{id}/index.m3u8`, authenticated, VOD playlist |

**The single most consequential line in that table is RTSP.** ADR 0002 chose HLS because
8554 could not be reached; it can now, and the pipeline uses it.

## The first honest 8/8

`reports/evidence/preflight-2026-09-01T23-35-14Z` — **8 passed, 0 failed, 0 not
exercised**, every live check over RTSP/TCP.

Check 1 had never been demonstrated in this project. "Every client forces RTSP over TCP"
could only be proved by reading the source, because there was no reachable RTSP port to
prove it against. It now decodes frames over TCP directly, join 0.12 s.

Getting there meant fixing the harness, and one of those fixes matters more than the
number it produced.

### The preflight could report 8/8 with a failing check inside it

`Check.status` defaulted to `"pass"`, and almost no check set it. The summary counts
`status`, not `passed` — so **every check that computed `False` and returned without an
explicit status printed as PASS**. The first 8/8 of the day was wrong: check 7's own
detail line said "1 distinct resolution" while its criterion required more than one, and
it rendered green anyway. With the default derived from `passed`, check 5 then showed
itself as a genuine failure.

That is the fourth time in this project a green result has been the bug rather than the
finding, and it is the worst of them: this one was in the tool whose entire purpose is
to report honestly.

### Two more checks that could not have been right

* **Check 5** took `cameras[0]`. A camera that happened to be down that second was
  reported as "decoder warnings are fatal" — the mirror image of the defect
  `discover_live_cameras` exists to prevent. It now tries the estate and reports NOT
  EXERCISED when nothing answers, because no frame decoded is the absence of a
  demonstration, not evidence of a defect.
* **Check 7** read codecs from the catalogue. The new catalogue declares none, so it
  reported the estate as lacking mixed codecs while that estate plainly carries both.
  It now probes, which is the rule this codebase already follows for exactly this
  reason (DISCOVERY finding 1).

### We were causing an outage we then measured

Probing cameras individually succeeded while a sweep opening all thirty in rapid
succession reported **every one of them dead** — port open throughout, same cameras
answering moments later. That was our load pattern, not the estate's health. The
organiser's integration guide says "pace your load" in as many words, and discovery was
the one place this codebase did not. Both the preflight sweep and `ingest_gateway.py`
are now paced and bounded.

## ANPR against the new grid: the same answer, from a new estate

Ran the production pipeline over the live feed and, separately, the detector alone over
one still from every camera that answered.

| | |
|---|---:|
| Cameras with a still captured | 25 |
| **Plate boxes found across all of them** | **1** |
| Size of that box | **66 × 21 px** |
| Its read | `AAA1649`, character confidence **0.28** |

The crop was opened and looked at: illegible to a human. Most feeds are night footage on
a loop — the OSD clock reads `14-06-2026 05:04` — and only cam11 and cam21 are daylight.

This is Finding 12 and Finding 16 reproduced a third time, now on a **different estate,
different cameras, different resolutions**. It strengthens the scaling argument rather
than weakening it: camera placement and resolution bound this problem far more tightly
than model choice does.

## Availability still swings, and now the fallback does not save us

At 05:00 the preflight found three cameras delivering and passed 8/8. At 06:00, with
pacing in place and cameras approached one at a time, **nothing answered** — RTSP and
HLS both, with port 8554 still open. A full `gateway-ingest` sweep in that window
recorded 0 of 30 producing frames.

One finding to carry into the demonstration: **HLS is not a usable ingest fallback on
this estate.** The playlist sits behind the login, so `requests` can fetch it with a
session cookie and FFmpeg — which is handed only a URL — cannot. RTSP needs no
authentication and is the working transport. Passing the cookie through to FFmpeg is
plausible but was not built, because the media plane was down and it could not have been
verified; building it blind is the thing this project does not do.

## Two bonus capabilities, built as classifiers over the existing stream

Neither is a new ingest path. Both are additional passes over detections the ANPR
pipeline already produced.

**Intrusion zones.** A polygon per camera via migration `0006`, with a working downgrade
and the same `FORCE ROW LEVEL SECURITY` that `detection` and `alert` carry. Admin-only
configuration, audited, with deletion audited *before* the row goes. Alerts arrive on the
existing Alert Desk with the same cooldown shape the movement logic uses.

The geometry is deliberately **not** geographic. Every other geometry in this schema
answers a question about the world; this one answers a question about a picture. Turning
a monocular CCTV frame into ground coordinates needs calibration this estate does not
publish, so storing the zone in EPSG:4326 would be a coordinate system chosen for
consistency rather than meaning — and would invite comparing it against real positions.
Containment is on the vehicle box *centroid*: a box grazing the boundary is a vehicle
passing, and alerting on that is how a desk fills with events an operator learns to
dismiss.

**Speed flagging.** An implied speed only between two sightings of one plate on two
genuinely different real cameras with known positions. The `REPLAY-` harness cameras are
excluded as a `WHERE` clause rather than a convention, with a test written so that
removing the clause fails the suite. It also reuses the journey reconstruction's
uncertainty tolerance exactly: the speed must exceed the ceiling *after* subtracting both
cameras' coordinate error, so a vehicle is flagged when it must have been speeding rather
than when it might have been.

**It currently raises nothing.** Seven detections come from non-`REPLAY` cameras, and no
plate has been seen at two real placed cameras. Stated in `README.md` beside the other
limitations rather than worked around. The data is missing, not the code.

## A CI failure worth recording

CI went red on Tests while every local run passed, and the difference was a file
developers have and CI does not: `.env`.

`resolve_hls_variant` had been given a `get_settings()` call to find the access code.
`SETU_GATEWAY_HOST` has no default on purpose, so on a machine with no configuration,
merely resolving a playlist URL raised. The regression test asserts the **absence** of
that call — by making `get_settings` explode — rather than the success of the function,
because the function succeeds either way on a developer machine. A test written the
obvious way would have passed on both the broken and the fixed code.

## Method and state

| | |
|---|---|
| Tests | **273 passed, 0 skipped** with Postgres up; 250 passed / 24 skipped the way CI runs it |
| New tests this session | 16 — 7 zone, 8 speed, 1 transport regression |
| `ruff`, `ruff format` | clean, 105 files |
| `mypy --strict` | clean but for the 19 pre-existing numpy-drift errors in `anpr.py`, `scene_cut.py`, `stream_client.py` |
| CI | green on `8360d16` |
| Deployed API | gateway `reachable: true`, 30 cameras in catalogue, audit chain valid |
