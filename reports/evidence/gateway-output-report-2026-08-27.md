# Government feed — ANPR output report

Merged from **8 ingest pass(es)** against `live.corp8.cloud`, 2026-08-26T22:55:59Z to 2026-08-27T08:58:23Z.

Produced by `backend/scripts/ingest_gateway.py`, which runs the same `AnprPipeline` as the own-feed report against `GatewaySource` instead of a file. Timing is taken from stream PTS; no declared frame rate is read anywhere in the path.

## Estate coverage

| | Cameras |
|---|---:|
| Catalogued | 30 |
| Produced frames | **20** |
| Produced none | **10** |

Cameras that produced no frames within the budget, with the reason returned:

| Camera | Reason |
|---|---|
| 4 | no frames within budget |
| 5 | no frames within budget |
| 6 | no frames within budget |
| 9 | no frames within budget |
| 15 | no frames within budget |
| 17 | no frames within budget |
| 18 | no frames within budget |
| 20 | no frames within budget |
| 22 | no frames within budget |
| 23 | no frames within budget |

## What the analytics produced

| Measurement | Value |
|---|---:|
| Frames decoded | 9713 |
| Frames passing the motion gate | 555 |
| Plate regions detected | 51 |
| Fused plate records | 7 |
| Grammar-valid registrations | **4** |
| Distinct valid registrations | **4** |

### Registrations read

| Plate | Camera | Confidence | Frames fused | Observed (UTC) |
|---|---|---:|---:|---|
| `GJ32AA3900` | 7 | 0.70 | 3 | 2026-08-27T08:56:34 |
| `GJ32AG1111` | 7 | 1.00 | 1 | 2026-08-27T08:56:38 |
| `GJ32AG3028` | 7 | 0.83 | 6 | 2026-08-27T08:56:49 |
| `GJ32K2007` | 7 | 0.82 | 3 | 2026-08-27T08:56:51 |

### The finding that matters

9713 frames across 20 live cameras yielded 51 plate regions and **4 grammar-valid registrations**. That is not a pipeline fault: the recogniser reads what is legible, and at the resolution and framing these cameras publish, very little is. The evidence crops are committed, and the unreadable ones are unreadable to a human reviewer too.

This is the same resolution effect measured on the own-feed clip, where the identical pipeline reads 8 plates at 2560x1440, 2 at 1280x720 and none at 704x396. It is the empirical basis for the amended scalability claim in `docs/HLD_RECONCILIATION.md`: sub-stream ingest buys throughput at an operating point where nothing is read, so the honest number is the full-resolution one.

A second, subtler class of error appears here. Reads such as those on the unreadable crops are rejected by the Indian plate grammar and never become registrations, which is the layered design working. But a read that is wrong *and* grammatical passes every check the system has. Precision against annotated ground truth is the only thing that measures that class; see `reports/evidence/anpr-accuracy-*`.

## Declared versus measured frame rate

The organiser's §2.2 warns not to trust the reported frame rate. Of 6 cameras that both declare a rate and delivered frames, **4 diverge by more than 5%**. A further 14 delivered frames while declaring no rate at all.

| Camera | Declared | Measured | Drift |
|---|---:|---:|---:|
| 13 | 12.50 | 9.89 | -20.9% |
| 14 | 12.50 | 9.93 | -20.6% |
| 16 | 12.50 | 10.03 | -19.7% |
| 26 | 13.35 | 12.64 | -5.3% |
| 27 | 24.86 | 24.58 | -1.1% |
| 29 | 24.78 | 25.00 | +0.9% |

## Provenance and caveats

- Merged from 8 pass(es); 30 camera(s) were re-run and the later result supersedes the earlier. Every attempt is retained in the source JSON.
- **Passes span more than one recogniser** (cct-s-v1-global-model, cct-s-v2-global-model). Estate coverage and frame-rate figures are unaffected -- they do not depend on the recogniser -- but plate counts should be read per pass rather than summed as though one model produced them all.
- Cameras returning HTTP 500 on their playlist are a gateway-side fault, reported in `docs/SUPPORT_QUERY.md`. Cameras that time out may be either.
- Every detection listed is genuine inference on a live government feed, with the evidence crop written to `data/evidence/crops/`.

Source records: `gateway-ingest-2026-08-26T22-55-59Z.json`, `gateway-ingest-2026-08-27T04-38-16Z.json`, `gateway-ingest-2026-08-27T07-23-42Z.json`, `gateway-ingest-2026-08-27T07-31-00Z.json`, `gateway-ingest-2026-08-27T07-37-29Z.json`, `gateway-ingest-2026-08-27T07-50-49Z.json`, `gateway-ingest-2026-08-27T08-05-00Z.json`, `gateway-ingest-2026-08-27T08-56-18Z.json`
