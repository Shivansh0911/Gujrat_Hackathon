# Government feed — ANPR output report

Merged from **10 ingest pass(es)** against `cctv.corp8.cloud`, 2026-09-01T23:54:25Z to 2026-09-02T14:19:41Z.

Produced by `backend/scripts/ingest_gateway.py`, which runs the same `AnprPipeline` as the own-feed report against `GatewaySource` instead of a file. Timing is taken from stream PTS; no declared frame rate is read anywhere in the path.

## Estate coverage

| | Cameras |
|---|---:|
| Catalogued | 30 |
| Produced frames | **22** |
| Produced none | **8** |

Cameras that produced no frames within the budget, with the reason returned:

| Camera | Reason |
|---|---|
| cam07 | no frames within budget |
| cam08 | no frames within budget |
| cam09 | no frames within budget |
| cam10 | no frames within budget |
| cam11 | no frames within budget |
| cam21 | no frames within budget |
| cam24 | no frames within budget |
| cam25 | no frames within budget |

## What the analytics produced

| Measurement | Value |
|---|---:|
| Frames decoded | 2150 |
| Frames passing the motion gate | 312 |
| Plate regions detected | 9 |
| Fused plate records | 1 |
| Grammar-valid registrations | **0** |
| Distinct valid registrations | **0** |

### The finding that matters

2150 frames across 22 live cameras yielded 9 plate regions and **0 grammar-valid registrations**. That is not a pipeline fault: the recogniser reads what is legible, and at the resolution and framing these cameras publish, very little is. The evidence crops are committed, and the unreadable ones are unreadable to a human reviewer too.

This is the same resolution effect measured on the own-feed clip, where the identical pipeline reads 8 plates at 2560x1440, 2 at 1280x720 and none at 704x396. It is the empirical basis for the amended scalability claim in `docs/HLD_RECONCILIATION.md`: sub-stream ingest buys throughput at an operating point where nothing is read, so the honest number is the full-resolution one.

A second, subtler class of error appears here. Reads such as those on the unreadable crops are rejected by the Indian plate grammar and never become registrations, which is the layered design working. But a read that is wrong *and* grammatical passes every check the system has. Precision against annotated ground truth is the only thing that measures that class; see `reports/evidence/anpr-accuracy-*`.

## Declared versus measured frame rate

The organiser's §2.2 warns not to trust the reported frame rate. Of 0 cameras that both declare a rate and delivered frames, **0 diverge by more than 5%**. A further 22 delivered frames while declaring no rate at all.

| Camera | Declared | Measured | Drift |
|---|---:|---:|---:|

## Provenance and caveats

- Merged from 10 pass(es); 30 camera(s) were re-run and the later result supersedes the earlier. Every attempt is retained in the source JSON.
- Recogniser: `cct-s-v2-global-model`.
- Cameras returning HTTP 500 on their playlist are a gateway-side fault, reported in `docs/SUPPORT_QUERY.md`. Cameras that time out may be either.
- Every detection listed is genuine inference on a live government feed, with the evidence crop written to `data/evidence/crops/`.

Source records: `gateway-ingest-2026-09-01T23-54-25Z.json`, `gateway-ingest-2026-09-02T00-07-48Z.json`, `gateway-ingest-2026-09-02T00-55-29Z.json`, `gateway-ingest-2026-09-02T01-00-11Z.json`, `gateway-ingest-2026-09-02T13-33-51Z.json`, `gateway-ingest-2026-09-02T13-37-31Z.json`, `gateway-ingest-2026-09-02T13-41-18Z.json`, `gateway-ingest-2026-09-02T13-45-31Z.json`, `gateway-ingest-2026-09-02T13-50-03Z.json`, `gateway-ingest-2026-09-02T14-15-36Z.json`
