# HLD reconciliation

Every performance and capability claim in the High-Level Design, checked against what
the system actually does. Measurements are in `reports/evidence/`.

**Where a measurement contradicts the document, the measurement stands and the
document is what changes.** Two claims below need amending.

> **Scope limitation.** `SETU_High_Level_Design.pdf` is not in the repository, so
> this reconciles against the claims referenced throughout the build instructions
> and the code. Before submission, read the PDF against this table and add any claim
> it makes that is not listed here — an unreconciled claim is exactly the kind of
> thing a technical jury finds.

---

## Claims that hold

| Claim | Measured | Evidence |
|---|---|---|
| Journey query under 3 s | **p95 60 ms**, median 38 ms, over a 12-hour window | `benchmarks-2026-08-26T13-56-28Z` |
| Decode to alert under 2 s | **p95 1486 ms**, median 14 ms | same |
| Feed-contract compliance | **8/8** on the organiser's own checklist, exercised empirically against the live gateway | `reports/preflight.json` |
| Three-level tenant isolation | Gateway policy, scoped accessors, **and Postgres RLS**. 9 tests issue raw SQL bypassing the application; `setu_app` verified `rolsuper=false`, `rolbypassrls=false` in the deployed container | `test_row_level_security.py`, deployment check 8 |
| Tamper-evident audit chain | 8 tests prove modification, actor rewriting and deletion are all detected. The ledger is append-only at the database level — the application holds no UPDATE grant | `test_audit_chain.py` |
| Never trust the declared frame rate | **12 of 16 cameras that produced frames diverge.** Camera 26 declares 13.35 fps and delivers 25.0 (+87%); cameras 13 and 16 declare 12.5 and deliver 9.96 (−20%); 9 declare nothing at all | `catalogue-probe-2026-08-26T21-07-28Z` |
| Signed evidence verifiable without SETU | Ed25519 detached signature over a canonical manifest. Verified: valid passes, tampered manifest fails, altered signature fails, wrong key fails, manifest byte-deterministic | Part B verification |
| Coordinates never invented | 18 geocoded, 10 district-centroid approximate, 2 unset. Every row traces to a cached geocoder response | `data/seed/geocode_cache.json` |

---

## Claims that need amending

### 1. Sub-stream ingest as the scalability answer

**What the benchmark says:** 3.82 cameras per worker at 704×396 versus 0.81 at the
published 2560×1440 — roughly 21,000 workers for 80,000 cameras instead of 98,500.

**What is also true, and was not measured when that figure was produced:** the same
clip yields

| Resolution | Valid plates read |
|---|---:|
| 2560×1440 (as published) | 8 |
| 1280×720 | 2 |
| 704×396 | 0 |

**So the 3.82 figure is a throughput at which this recogniser reads almost nothing.**
Quoting it as the scaling answer without that caveat would be misleading, and a jury
that asked "what is your accuracy at that resolution?" would expose it.

**Amend the HLD to:** sub-stream ingest is a real lever, but the operating point must
be chosen against measured accuracy, not throughput alone. On this footage the
recogniser needs full resolution. The honest statement is that a *centralised* design
costs on the order of 98,500 CPU workers at the resolution ANPR actually needs — which
is a stronger argument for edge processing than the sub-stream number was, not a
weaker one. Determining the lowest resolution that preserves plate legibility is
outstanding work, and is the single most valuable optimisation available.

### 2. ANPR accuracy

**What the HLD implies:** a working ANPR capability.

**What is measured:** pipeline *rates* only — 13.7% motion-gate pass, 22 plate boxes,
8 grammar-valid plates, confidences 0.61–0.80. **Precision and recall are not
measured.** No claim of accuracy is currently supported by evidence.

Worse, there is direct evidence of error: three crops of visibly the same vehicle
(`KA-25 AB-1542`) produced three different registrations, because scene cuts reset
tracks and multi-frame fusion never grouped them.

**Amend the HLD to** state that accuracy is pending ground-truth annotation, and cite
`backend/scripts/ground_truth.py`. An acknowledged gap reads as rigour; a silent one
reads as an oversight. Run:

```bash
make ground-truth   # writes the annotation sheet for 56 crops
make accuracy       # produces precision, recall, character error rate
```

---

## Claims not yet testable

| Claim | Why not | What would settle it |
|---|---|---|
| Government-feed test case | The gateway media plane was unreachable for most of the build. It recovered on 2026-08-27, and **17 of 30 cameras produced frames**; 13 returned none | Ingest across the working cameras and publish the output report |
| 80,000-camera operation | Extrapolated from a single-worker measurement on one laptop CPU with no GPU | A multi-worker run on representative hardware |
| Edge-processing bandwidth saving | Architecture documented, not implemented | An edge deployment |

---

## Reconciliation status

- **7 claims hold**, each with a dated measurement.
- **2 claims need amendment** before submission.
- **3 claims are documented as untestable** with the reason stated.

Last reconciled 2026-08-27 against `c7639f8`.
