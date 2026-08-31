# Completeness audit — 2026-08-30

Every claim in `README.md` and `docs/` checked against the deployed system and the
code as it stands today, rather than against what an earlier session believed when it
wrote the sentence. Same rule as `DISCOVERY.md`: a number here was produced by running
something, and where a document disagreed with the measurement, the document changed.

Deployment audited: **https://setu-gujrat.netlify.app** (console) against
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

Deployment audited: **https://setu-gujrat.netlify.app** against
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
