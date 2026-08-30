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
| 5 | Own-feed screen recording | — | ⏳ **Harshit** |
| 6 | Government-feed screen recording | — | ⏳ **Harshit** — availability swings: 18 of 30 cameras on 2026-08-30, 25 of 30 on 2026-08-27. Camera 7 is the only one that has ever produced a legible plate; **check it is up before recording** |

Script for both, screen by screen with timings and what to say:
`docs/DEMO_RUNBOOK.md`.

## Analytics output

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 7 | Detected vehicles and plates with timestamps | `reports/detections-*.csv` and `.pdf`, `make detection-report` | ✅ 56 detections, 4 cameras, 32 grammar-valid, 8 distinct plates |
| 8 | Government-feed output report | `reports/evidence/gateway-output-report-2026-08-27.md` and `-2026-08-30.md` | ✅ **two dated runs.** 27 Aug: 25 of 30 cameras, 9,158 frames, 2 valid registrations. 30 Aug: 18 of 30, 5,055 frames, **0** valid. Both kept — the difference is the feed, not the pipeline |
| 9 | ANPR precision and recall | `data/seed/anpr_ground_truth.csv`, `reports/evidence/anpr-accuracy-*` | ✅ **measured: 29.6% precision, 29.6% recall, 26.9% CER** (was 0.0% before three defects were found and fixed). Annotations were made by reading each crop; **have a second person spot-check them** |

## Platform

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 10 | GitHub repository | `github.com/Shivansh0911/Gujrat_Hackathon` | ✅ |
| 11 | Screenshots, all screens, real data | `docs/screenshots/` | ✅ 11 images covering all eight screens |
| 12 | Screenshots of the deployed instance | `docs/screenshots/deployed/` | ✅ 8 images, captured against the container stack |
| 13 | Hosted URL + test credentials | **Console: https://setu-gujrat.netlify.app** · API: https://setu-api-ai7z.onrender.com | ✅ **live**, verified 10/10 by `verify_deployment.py`. Deployed on Render + Netlify, not Railway — see `docs/adr/0004` addendum. Credentials in `deploy-secrets.env` (gitignored) |
| 14 | Test accounts for the screening committee | `admin` (System Administrator) and `operator` (Control Room Operator) | ⚠️ live values are in the gitignored `deploy-secrets.env`; copy them into `DEMO_RUNBOOK.md` §1 before submitting |

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
- [ ] Replace the third-party own-feed clip with our own stationary footage, then
      `make demo-reset` and recapture screenshots
- [ ] Record both demonstration videos
- [ ] Deploy and record the live URL with credentials
- [ ] Send `docs/SUPPORT_QUERY.md`
- [ ] Re-run `gitleaks detect` over full history — **still outstanding**, the binary could not be downloaded on this connection and Docker was unavailable; last clean run was 2026-08-27
- [ ] Confirm the four limitations in `README.md` still read accurately

## Four limitations to state up front

Stating them before a judge finds them is what makes the rest credible.

1. The own-feed clip is third-party (CC BY 3.0, Karnataka) — plates read `KA…` not `GJ…`
2. The four `REPLAY-` cameras are a replay harness: real inference and real evidence
   photos, only the attributing camera is simulated
3. **ANPR plate-level accuracy is 29.6%** (26.9% character error rate), up from 0.0%
   once three measured defects were fixed. State the number and the story before a
   judge tests it — finding them by measurement is the strongest evidence of rigour
   in the submission. Resolution still bounds what is achievable
4. The gateway media plane was down for most of the build; it recovered on
   2026-08-27 with 25 of 30 cameras producing frames, and the estate publishes below
   the resolution at which plates are legible — 3 legible plates in 9,158 frames
