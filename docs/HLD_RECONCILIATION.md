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
| Government-feed test case | **Ran across the estate.** 25 of 30 cameras produced frames, 9,158 frames decoded, 30 plate regions, 2 grammar-valid registrations. Cameras 17 and 18 return HTTP 500; 22, 23 and 30 time out | `gateway-output-report-2026-08-27.md` |
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

**What is now measured.** Every evidence crop was annotated by eye and scored with
`backend/scripts/ground_truth.py`. The result is worse than "unmeasured" implied:

| Measure | Result |
|---|---:|
| Crops annotated | 80 (17 distinct images; the four REPLAY cameras share frames) |
| Legible to a reviewer | 59 rows / 17 distinct |
| **Plate-level precision** | **0.0%** |
| **Plate-level recall** | **0.0%** |
| Character error rate | **39.8%** |
| Reads asserted on an illegible crop | 21 |

**Not one registration was read correctly.** The character error rate says why: the
recogniser gets roughly six characters in ten right and the whole plate wrong every
time. `KA25AB1542` is read as `KA25AB144`, `KA25SB512`, `KA25SB542` and `0ADA811` on
four frames of one vehicle. The single grammar-valid plate from the government feed,
`GJ14AK533` at confidence 0.94, is a misread of `GJ14AK5333` -- a dropped digit, on
the one plate in the entire estate sweep that a human can read.

**This is the most important number in the submission and it must not be softened.**
A confidence of 0.94 on a wrong registration is worse than a low-confidence one,
because an investigator would act on it.

**Amend the HLD to** state plate-level accuracy as measured -- precision 0.0% on a
17-image annotated sample, character error rate 39.8% -- and to describe the ANPR
pipeline as demonstrating the architecture end to end rather than as an operationally
accurate recogniser. The full replacement prose is in the amendment section below.

**What the measurement does not say.** The sample is small (17 distinct legible
images) and comes from two sources, neither ideal: a third-party Karnataka clip shot
from a moving bus, and a government estate that publishes at a resolution where three
crops in the entire sweep contain a legible plate. It measures this recogniser on this
footage. It is not a claim about ANPR generally, and a fair reading of it is "the
platform is sound and the recogniser needs work", not "the platform does not work".

**What is already known to help.** Scoring the candidate recognisers against the same
annotations (`backend/scripts/compare_recognisers.py`,
`reports/evidence/recogniser-comparison-*.json`) shows `cct-s-v2-global-model` ahead of
the `cct-s-v1-global-model` currently in use -- 1 exact match against 0, and character
error rate 31.2% against 39.4% on the distinct-image sample. That is a measured
improvement rather than a guess, and swapping the default is the first thing to do
next. It is not yet done here because changing the recogniser invalidates every
committed crop filename and therefore the annotation sheet keyed to it, and shipping a
re-ingest without re-annotating would replace a measured number with an unmeasured one.

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
> **Measured plate-level accuracy is 0.0% precision and 0.0% recall, with a character
> error rate of 39.8%**, scored against a by-eye annotation of all 80 evidence crops
> (17 distinct images) in `data/seed/anpr_ground_truth.csv`. The recogniser resolves
> roughly six characters in ten and has not yet returned a complete registration
> correctly on this footage. One read, `GJ14AK533` at confidence 0.94, is a
> single-digit misread of `GJ14AK5333`; a high confidence attached to a wrong
> registration is the most dangerous failure mode this system has, and it is why the
> platform records provenance, the evidence crop and the corrections applied against
> every read rather than presenting a plate as fact.
>
> Two measured factors explain most of it. **Resolution:** the same pipeline reads 8
> grammar-valid plates at 2560x1440, 2 at 1280x720 and none at 704x396, and the
> government estate publishes below the threshold where plates survive -- 9,158 frames
> across 25 live cameras contained three human-legible plates. **Model fit:** scoring
> the candidate recognisers against the same annotations shows `cct-s-v2-global-model`
> ahead of the model currently configured, at 31.2% character error rate against
> 39.4%.
>
> The accuracy figure therefore measures this recogniser on this footage, and it is
> stated because the annotation harness exists to produce it. What the platform
> demonstrates is the pipeline, the evidence chain and the federation architecture end
> to end on live government feeds; the recogniser is the replaceable component behind
> the interface described in ADR 0003, and improving it is bounded, measurable work
> rather than an architectural change.

---

## Reconciliation status

- **7 claims hold**, each with a dated measurement.
- **2 claims need amendment** before submission; the replacement prose is above.
- **1 claim is now testable and was tested** -- the government-feed test case ran
  across the estate; see `reports/evidence/gateway-output-report-*.md`.
- **2 claims remain documented as untestable** with the reason stated.

Last reconciled 2026-08-27.
