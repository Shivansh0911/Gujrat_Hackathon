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
