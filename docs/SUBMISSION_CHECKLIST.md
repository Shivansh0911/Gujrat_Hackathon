# Submission checklist

Every required deliverable mapped to its artefact, so nothing is discovered missing
at upload time.

**Legend** — ✅ present · ⏳ outstanding · ⚠️ present with a caveat that must be stated

---

## Documents

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 1 | Solution Presentation | `SETU_Solution_Presentation.pdf` | ✅ complete — **do not regenerate** |
| 2 | High-Level Design | `SETU_High_Level_Design.pdf` | ⚠️ complete, but **two claims need amending** — see `HLD_RECONCILIATION.md` §Claims that need amending |
| 3 | Workflow / integration diagram | `SETU_Workflow_Integration_Diagram.png` | ✅ |
| 4 | Repository README | `README.md` | ✅ model choice, screenshots, quickstart, measured results |

## Demonstration videos

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 5 | Own-feed screen recording | — | ⏳ **Harshit** |
| 6 | Government-feed screen recording | — | ⏳ **Harshit** — gateway recovered 2026-08-27, 17 of 30 cameras produce frames |

Script for both, screen by screen with timings and what to say:
`docs/DEMO_RUNBOOK.md`.

## Analytics output

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 7 | Detected vehicles and plates with timestamps | `reports/detections-*.csv` and `.pdf`, `make detection-report` | ✅ 56 detections, 4 cameras, 32 grammar-valid, 8 distinct plates |
| 8 | Government-feed output report | — | ⏳ run the report against gateway ingest now that the feed is back |
| 9 | ANPR precision and recall | `make ground-truth` → annotate → `make accuracy` | ⏳ **Harshit — 56 crops awaiting annotation.** The single highest-value outstanding item |

## Platform

| # | Deliverable | Artefact | State |
|---|---|---|---|
| 10 | GitHub repository | `github.com/Shivansh0911/Gujrat_Hackathon` | ✅ |
| 11 | Screenshots, all screens, real data | `docs/screenshots/` | ✅ 8 images |
| 12 | Screenshots of the deployed instance | `docs/screenshots/deployed/` | ✅ 8 images, captured against the container stack |
| 13 | Hosted URL + test credentials | — | ⏳ **containers verified 9/9 locally; the Railway push needs the team account.** Steps in `docs/DEPLOYMENT.md` §4 |
| 14 | Test accounts for the screening committee | `admin` and `operator`, credentials in `.env.prod` | ⚠️ generated per deployment; record the live values in `DEMO_RUNBOOK.md` §1 once hosted |

## Evidence

| # | Artefact | State |
|---|---|---|
| 15 | Feed-contract compliance, 8/8 | `reports/preflight.json` | ✅ |
| 16 | Catalogue probe, declared vs measured | `reports/evidence/catalogue-probe-*` | ✅ two dated runs, including one against the recovered gateway |
| 17 | Performance benchmarks | `reports/evidence/benchmarks-*` | ✅ |
| 18 | Gateway outage support query | `docs/SUPPORT_QUERY.md` | ⏳ **Harshit — send it.** It is the evidence that we reported the fault |
| 19 | Discovery record | `docs/DISCOVERY.md` | ✅ nine dated findings with measurements |
| 20 | ADRs | `docs/adr/` | ✅ |
| 21 | HLD reconciliation | `docs/HLD_RECONCILIATION.md` | ✅ |

---

## Before upload

- [ ] Amend the two HLD claims (`HLD_RECONCILIATION.md`)
- [ ] Annotate the 56 crops and run `make accuracy`
- [ ] Replace the third-party own-feed clip with our own stationary footage, then
      `make demo-reset` and recapture screenshots
- [ ] Record both demonstration videos
- [ ] Deploy and record the live URL with credentials
- [ ] Send `docs/SUPPORT_QUERY.md`
- [ ] Re-run `gitleaks detect` over full history
- [ ] Confirm the four limitations in `README.md` still read accurately

## Four limitations to state up front

Stating them before a judge finds them is what makes the rest credible.

1. The own-feed clip is third-party (CC BY 3.0, Karnataka) — plates read `KA…` not `GJ…`
2. The four `REPLAY-` cameras are a replay harness: real inference and real evidence
   photos, only the attributing camera is simulated
3. ANPR accuracy is unmeasured and reads are visibly imperfect
4. The gateway media plane was down for most of the build; it recovered on
   2026-08-27 with 17 of 30 cameras producing frames
