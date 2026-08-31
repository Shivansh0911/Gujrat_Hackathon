# Submission checklist

Every required deliverable mapped to its artefact, so nothing is discovered missing
at upload time.

**Legend** — ✅ present · ⏳ outstanding · ⚠️ present with a caveat that must be stated

---

## Documents

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 1 | Solution Presentation | `SETU_Solution_Presentation.pdf` | ✅ complete — **do not regenerate** |
| 2 | High-Level Design | [`docs/SETU_High_Level_Design.md`](SETU_High_Level_Design.md) | ✅ generated in-repo from measured evidence. Both previously-amended claims are correct from the start; `HLD_RECONCILIATION.md` remains as the dated audit trail of why |
| 3 | Workflow / integration diagram | `SETU_Workflow_Integration_Diagram.png` | ✅ |
| 4 | Repository README | `README.md` | ✅ model choice, screenshots, quickstart, measured results |

## Demonstration videos

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 5 | Own-feed screen recording | — | ⏳ **Harshit** — record against the deployed console; the own-feed detections are the ones already in it |
| 6 | Government-feed screen recording | — | ⏳ **Harshit** — availability swings: 25 of 30 cameras on 27 Aug, 18 of 30 on 30 Aug, **a total 502 on 31 Aug and still down at close-out**. Camera 7 is the only one that has ever produced a legible plate. **Check the Health page's gateway card before recording**; if the feed is dark, record the outage handling instead — `DEMO_RUNBOOK.md` §3 has the wording |

Script for both, screen by screen with timings and what to say:
`docs/DEMO_RUNBOOK.md`.

## Analytics output

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 7a | **Sample gap-analysis report** (Model 1) | `reports/gap-analysis-2026-08-31T06-14-08Z.md` | ✅ **10 districts, 28 cameras with a gap, 28 investigation-derived.** Grouped by remedy rather than severity, because a pin drop and a procurement do not belong on one scale. `make gap-report` |
| 7 | Detected vehicles and plates with timestamps | `reports/detections-*.csv` and `.pdf`, `make detection-report` | ✅ 56 detections, 4 cameras, 32 grammar-valid, 8 distinct plates |
| 8 | Government-feed output report | `reports/evidence/gateway-output-report-2026-08-27.md` and `-2026-08-30.md` | ✅ **two dated runs.** 27 Aug: 25 of 30 cameras, 9,158 frames, 2 valid registrations. 30 Aug: 18 of 30, 5,055 frames, **0** valid. Both kept — the difference is the feed, not the pipeline |
| 9 | ANPR precision and recall | `data/seed/anpr_ground_truth.csv`, `reports/evidence/anpr-accuracy-*` | ✅ **measured: 29.6% precision, 29.6% recall, 26.9% CER** (was 0.0% before three defects were found and fixed). Annotations were made by reading each crop; **have a second person spot-check them** |

## Platform

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 10 | GitHub repository | `github.com/Shivansh0911/Gujrat_Hackathon` | ✅ |
| 10a | **Manual + bulk camera onboarding** (Model 1) | `POST /cameras` and `POST /cameras/bulk-import`, both on the System screen | ✅ both demonstrable, both audited |
| 10b | **Control Room video wall** (Model 2) | Console → Control Room | ✅ 1 / 2×2 / 2×3 tiles, capped at six concurrent streams |
| 11 | Screenshots, all screens, real data | `docs/screenshots/` | ✅ 11 images covering all eight screens |
| 12 | Screenshots of the deployed instance | `docs/screenshots/deployed/` | ✅ 8 images, captured against the container stack |
| 13 | Hosted URL + test credentials | **Console: https://setu-gujrat.netlify.app** · API: https://setu-api-ai7z.onrender.com | ✅ **live**, verified 10/10 by `verify_deployment.py`. Deployed on Render + Netlify, not Railway — see `docs/adr/0004` addendum. Credentials in `deploy-secrets.env` (gitignored) |
| 14 | Test accounts for the screening committee | `admin` (System Administrator) and `operator` (Control Room Operator) | ✅ **live and verified.** Values are in the gitignored `deploy-secrets.env` and go on the **submission form**. They are deliberately *not* written into `DEMO_RUNBOOK.md` — that file is committed, and a working credential in a public repository is a secret leak whatever the reason for putting it there. `DEMO_RUNBOOK.md` §0 says where to find them |

## Evidence

| # | Artefact | State |
|---|---|---|
| 15 | Feed-contract compliance, 8/8 | `reports/preflight.json` | ✅ |
| 16 | Catalogue probe, declared vs measured | `reports/evidence/catalogue-probe-*` | ✅ two dated runs, including one against the recovered gateway |
| 17 | Performance benchmarks | `reports/evidence/benchmarks-*` | ✅ |
| 18 | Gateway outage support query | `docs/SUPPORT_QUERY.md` | ⏳ **Harshit — send it.** It is the evidence that we reported the fault |
| 19 | Discovery record | `docs/DISCOVERY.md` | ✅ nine dated findings with measurements |
| 20 | ADRs | `docs/adr/` | ✅ four — event bus, HLS transport, ANPR model licensing, deployment platform |
| 21 | HLD reconciliation | `docs/HLD_RECONCILIATION.md` | ✅ |

---

## Before upload

- [x] ~~Annotate the crops and run `make accuracy`~~ — done; **spot-check the annotations**
- [x] **Own-feed clip — decided 2026-08-31: keep it.** See the decision below
- [ ] **Record both demonstration videos** — human task, not startable by tooling
- [x] ~~Deploy and record the live URL with credentials~~ — live at
      https://setu-gujrat.netlify.app, verified 10/10. Credentials go on the submission
      form, never into a committed file
- [ ] **Send `docs/SUPPORT_QUERY.md`** — human task. More warranted than ever: the
      gateway has been 502 on every endpoint since 31 Aug
- [x] Secret scan — **done 2026-08-31**. CI's `gitleaks` full-history step is green on `42700d8`; `detect-secrets` also run locally over tracked files with all 18 findings verified as false positives and committed as `.secrets.baseline`. See `SECURITY.md`
- [x] ~~Confirm the limitations in `README.md` still read accurately~~ — re-checked
      2026-08-31 against this session's measurements; see below

---

## Decision — the own-feed clip stays (2026-08-31)

This has been left open across several sessions. Closing it: **the Hubli–Dharwad
Wikimedia clip is what ships**, unless someone films replacement footage before
upload, in which case reopen this deliberately rather than by drift.

**Why keep it.** It is CC BY 3.0 and correctly attributed in
`data/own_feed/SOURCE.md`, so the licensing is clean. The confusion it caused — a
reviewer reading own-feed detections as government-feed ones — was never really about
the clip's origin; it was that nothing on screen said which feed a detection came
from. That is now fixed at the source: every journey hop, alert card and camera panel
carries a "Government feed" or "Own feed" badge, and cameras with no detections behind
them are drawn hollow on the map.

**What replacing it would cost, seven days out.** The 29.6% accuracy figure is scored
against 27 hand-annotated crops from *this* clip. New footage invalidates that
annotation set, so the headline accuracy number would have to be withdrawn and
re-measured, or quoted against footage no longer in the repository. Trading a measured
number for an unmeasured one this close to submission is the wrong direction.

**What is honestly lost by keeping it.** Plates read `KA…`/`KL…` rather than `GJ…`,
which a judge will notice immediately. That is limitation 1 in the README and should
be said out loud in the demonstration rather than waited for.

**If footage is filmed anyway:** `make demo-reset`, re-run `make ground-truth`,
re-annotate, `make accuracy`, recapture screenshots — and update the accuracy figure
everywhere it appears. Budget an afternoon, not an hour.

## Limitations to state up front

Six, not four — the list grew as measurement found more. They match `README.md`
exactly; a judge who reads both and sees two different numbers trusts neither.
Re-checked **2026-08-31**.

1. **The own-feed clip is third-party** (CC BY 3.0 Wikimedia, Karnataka) — plates read
   `KA…`/`KL…`, not `GJ…`. Kept deliberately; see the decision above
2. **The four `REPLAY-` cameras are a replay harness** — real inference and real
   evidence photos, only the attributing camera is simulated, and the prefix is visible
   in the console
3. **ANPR plate-level accuracy is 29.6%** precision and recall, 26.9% character error
   rate, over 27 annotated crops. It was 0.0% until three defects were found *by
   measurement*. Say the number and the story before a judge tests it
4. **The government estate publishes below the resolution ANPR needs** — 9,158 frames
   across 25 live cameras yielded three human-legible plates. This bounds what any
   recogniser could achieve here, and is the empirical case for processing at the edge
5. **The gateway is intermittent, and getting worse.** 25 of 30 cameras on 27 Aug,
   18 of 30 on 30 Aug, **a Cloudflare 502 on every endpoint on 31 Aug and still down**.
   Valid registrations across those sweeps: 2, then 0. The pipeline was identical; the
   feed was not. The Health page now shows a live gateway card with the outage start
   time, so this is visible rather than mistaken for our own failure
6. **The deployed instance carries own-feed detections only.** Live, 10/10 verified,
   but every detection in it came from our own footage — the gateway has never yielded
   a legible plate during a run against the deployed database. The console labels the
   source of every hop and alert, so this is stated rather than left to be assumed
