# Government feed — ANPR output report

Merged from **2 ingest pass(es)** against `live.corp8.cloud`, 2026-08-26T22:55:59Z to 2026-08-27T04:44:20Z.

Produced by `backend/scripts/ingest_gateway.py`, which runs the same `AnprPipeline` as the own-feed report against `GatewaySource` instead of a file. Timing is taken from stream PTS; no declared frame rate is read anywhere in the path.

## Estate coverage

| | Cameras |
|---|---:|
| Catalogued | 30 |
| Produced frames | **25** |
| Produced none | **5** |

Cameras that produced no frames within the budget, with the reason returned:

| Camera | Reason |
|---|---|
| 17 | no frames within budget |
| 18 | no frames within budget |
| 22 | no frames within budget |
| 23 | no frames within budget |
| 30 | no frames within budget |

## What the analytics produced

| Measurement | Value |
|---|---:|
| Frames decoded | 9158 |
| Frames passing the motion gate | 643 |
| Plate regions detected | 30 |
| Fused plate records | 24 |
| Grammar-valid registrations | **2** |
| Distinct valid registrations | **2** |

### Registrations read

| Plate | Camera | Confidence | Frames fused | Observed (UTC) |
|---|---|---:|---:|---|
| `GJ14AK533` | 7 | 0.94 | 1 | 2026-08-26T23:01:08 |
| `EE3E1` | 12 | 0.39 | 3 | 2026-08-26T23:04:47 |

### The finding that matters

9158 frames across 25 live cameras yielded 30 plate regions and **2 grammar-valid registrations**. That is not a pipeline fault: the recogniser reads what is legible, and at the resolution and framing these cameras publish, very little is. The evidence crops are committed, and the unreadable ones are unreadable to a human reviewer too.

This is the same resolution effect measured on the own-feed clip, where the identical pipeline reads 8 plates at 2560x1440, 2 at 1280x720 and none at 704x396. It is the empirical basis for the amended scalability claim in `docs/HLD_RECONCILIATION.md`: sub-stream ingest buys throughput at an operating point where nothing is read, so the honest number is the full-resolution one.

A second, subtler class of error appears here. Reads such as those on the unreadable crops are rejected by the Indian plate grammar and never become registrations, which is the layered design working. But a read that is wrong *and* grammatical passes every check the system has. Precision against annotated ground truth is the only thing that measures that class; see `reports/evidence/anpr-accuracy-*`.

## Declared versus measured frame rate

The organiser's §2.2 warns not to trust the reported frame rate. Of 8 cameras that both declare a rate and delivered frames, **5 diverge by more than 5%**. A further 17 delivered frames while declaring no rate at all.

| Camera | Declared | Measured | Drift |
|---|---:|---:|---:|
| 15 | 12.50 | 5.38 | -56.9% |
| 13 | 12.50 | 6.55 | -47.6% |
| 14 | 12.50 | 8.13 | -35.0% |
| 16 | 12.50 | 9.96 | -20.3% |
| 26 | 13.35 | 12.10 | -9.4% |
| 29 | 24.78 | 24.58 | -0.8% |
| 27 | 24.86 | 25.00 | +0.6% |
| 6 | 25.00 | 25.00 | +0.0% |

## Provenance and caveats

- Merged from 2 pass(es); 8 camera(s) were re-run and the later result supersedes the earlier. Every attempt is retained in the source JSON.
- **1 result(s) carry a suspect wall-clock time** (28): elapsed time exceeded the per-camera budget by more than 2x because the host suspended mid-run. Frame counts and PTS-derived rates for these are unaffected; only their elapsed time is meaningless, and it is excluded from every figure above.
- Cameras returning HTTP 500 on their playlist are a gateway-side fault, reported in `docs/SUPPORT_QUERY.md`. Cameras that time out may be either.
- Every detection listed is genuine inference on a live government feed, with the evidence crop written to `data/evidence/crops/`.

Source records: `gateway-ingest-2026-08-26T22-55-59Z.json`, `gateway-ingest-2026-08-27T04-38-16Z.json`
