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
| Government-feed test case | **Ran across the estate, twice.** 2026-08-27: 25 of 30 cameras produced frames, 9,158 frames, 30 plate regions, **2** grammar-valid registrations. 2026-08-30: 18 of 30 cameras, 5,055 frames, 17 plate regions, **0** grammar-valid registrations. The pipeline behaved identically; the feed did not | `gateway-output-report-2026-08-27.md`, `-2026-08-30.md` |
| Never trust the declared frame rate (re-measured live) | **5 of 8** cameras that declare a rate and delivered frames diverge by more than 5%; camera 15 declares 12.5 and delivers 5.38 (-57%). A further 17 delivered frames while declaring nothing | same |

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

**What is measured.** Every evidence crop is annotated by eye and scored with
`backend/scripts/ground_truth.py`; the sheet is committed as
`data/seed/anpr_ground_truth.csv`.

| Measure | First measurement | Current |
|---|---:|---:|
| Plate-level precision | 0.0% | **29.6%** |
| Plate-level recall | 0.0% | **29.6%** |
| Character error rate | 39.8% | **26.9%** |
| Reads asserted on an illegible crop | 21 | **0** |

The first measurement was zero — not one registration read correctly. Three defects
were responsible, none of them visible without measuring:

1. **A nine-slot recogniser.** `max_plate_slots` is the model's number of
   classification heads, so `cct-s-v1-global-model` could not emit a ten-character
   Indian plate under any circumstances. Every read was exactly nine characters long.
2. **Association by IoU alone**, at 5 analytic fps, associated almost nothing — a
   plate moves further than its own width in 200 ms. Multi-frame fusion never ran.
3. **Fusion always right-aligned** reads of differing length, so a dropped *trailing*
   character shifted every position and manufactured disagreement.

All three are fixed and covered by regression tests, and a confidence floor now
suppresses reads no human could confirm.

**Amend the HLD to** state plate-level accuracy as measured — 29.6% precision and
recall, 26.9% character error rate — rather than implying a working recogniser, and
to note that resolution bounds it. The replacement prose is in the amendment section
below.

**What the measurement does not say.** 27 annotated rows from two sources, neither
ideal: a third-party Karnataka clip shot from a moving bus, and a government estate
that publishes below the resolution at which plates survive. It measures this
recogniser on this footage.

---

## Claims not yet testable

| Claim | Why not | What would settle it |
|---|---|---|
| 80,000-camera operation | Extrapolated from a single-worker measurement on one laptop CPU with no GPU | A multi-worker run on representative hardware |
| Edge-processing bandwidth saving | Architecture documented, not implemented | An edge deployment |

---

## Amendment text — ready to paste into the HLD

The two amendments above are described in terms of *why*. This section is the
replacement prose itself, so amending the PDF is a copy-and-paste rather than a
rewrite. Nothing else in the document needs to change.

### Replace the scalability paragraph with:

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

### Replace any statement of ANPR accuracy with:

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

---

## Reconciliation status

- **7 claims hold**, each with a dated measurement.
- **2 claims need amendment** before submission; the replacement prose is above.
- **1 claim is now testable and was tested** -- the government-feed test case ran
  across the estate; see `reports/evidence/gateway-output-report-*.md`.
- **2 claims remain documented as untestable** with the reason stated.

Last reconciled 2026-08-27.
