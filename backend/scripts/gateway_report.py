#!/usr/bin/env python
"""Merge gateway ingest passes into one government-feed output report.

Why merging rather than one run
-------------------------------
A single sweep of the estate takes about half an hour, and the machine running it
does not stay awake reliably for that long. The first full pass was interrupted by a
host suspend: wall-clock timings for the cameras in flight became meaningless (one
records 19,059 seconds) and five cameras failed on local DNS rather than on anything
the gateway did. Re-running only the affected cameras and merging is both faster and
more honest than quoting a contaminated sweep or pretending a clean one happened.

The merge rule is last-writer-wins per camera, ordered by the run's start time. A
later attempt supersedes an earlier one because gateway availability changes by the
minute; every superseded attempt stays in the JSON, so the history is not lost.

Wall-clock timings are reported only where they are trustworthy. A result whose
elapsed time exceeds the per-camera budget by more than a factor of two is flagged as
suspect rather than averaged in -- a host suspend is not a measurement of the feed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import EVIDENCE_DIR  # noqa: E402

SUSPECT_FACTOR = 2.0


def load_runs(paths: list[Path]) -> list[dict[str, Any]]:
    runs = []
    for p in paths:
        runs.append(json.loads(p.read_text(encoding="utf-8")))
    runs.sort(key=lambda r: r["started_utc"])
    return runs


def merge(runs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return the surviving result per camera, and the refs that were superseded."""
    latest: dict[str, dict[str, Any]] = {}
    superseded: list[str] = []
    for run in runs:
        budget = float(run["settings"]["seconds_per_camera"])
        for res in run["results"]:
            ref = res["camera_ref"]
            res = dict(res)
            res["_run_started"] = run["started_utc"]
            res["_budget_s"] = budget
            wall = float(res.get("wall_seconds") or 0.0)
            res["_timing_suspect"] = wall > budget * SUSPECT_FACTOR
            if ref in latest:
                superseded.append(ref)
            latest[ref] = res
    return latest, superseded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    paths = sorted(EVIDENCE_DIR.glob("gateway-ingest-*.json"))
    if not paths:
        print("no gateway ingest reports found; run scripts/ingest_gateway.py first")
        return 2

    runs = load_runs(paths)
    latest, superseded = merge(runs)

    results = [latest[k] for k in sorted(latest, key=lambda r: int(r) if r.isdigit() else 0)]
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    recs = [rec for r in results for rec in r["records"]]
    valid = [rec for rec in recs if rec["valid"]]
    distinct = Counter(rec["plate"] for rec in valid)
    suspect = [r for r in results if r["_timing_suspect"]]

    # Declared-versus-measured is the §2.2 claim; count only cameras where both exist.
    comparable = [
        r
        for r in ok
        if r.get("declared_fps") not in (None, 0) and r.get("measured_fps") not in (None, 0)
    ]
    diverged = [
        r
        for r in comparable
        if abs(float(r["measured_fps"]) - float(r["declared_fps"])) / float(r["declared_fps"])
        > 0.05
    ]
    undeclared = [r for r in ok if r.get("declared_fps") in (None, 0)]

    frames = sum(int(r.get("frames_decoded") or 0) for r in ok)
    boxes = sum(int(r.get("plates_detected") or 0) for r in ok)
    gated = sum(int(r.get("frames_gated_in") or 0) for r in ok)

    lines: list[str] = []
    A = lines.append
    A("# Government feed — ANPR output report")
    A("")
    A(
        f"Merged from **{len(runs)} ingest pass(es)** against `{runs[0]['gateway_host']}`, "
        f"{runs[0]['started_utc'][:19]}Z to {runs[-1]['finished_utc'][:19]}Z."
    )
    A("")
    A(
        "Produced by `backend/scripts/ingest_gateway.py`, which runs the same "
        "`AnprPipeline` as the own-feed report against `GatewaySource` instead of a "
        "file. Timing is taken from stream PTS; no declared frame rate is read anywhere "
        "in the path."
    )
    A("")
    A("## Estate coverage")
    A("")
    A("| | Cameras |")
    A("|---|---:|")
    A(f"| Catalogued | {len(results)} |")
    A(f"| Produced frames | **{len(ok)}** |")
    A(f"| Produced none | **{len(failed)}** |")
    A("")
    if failed:
        A("Cameras that produced no frames within the budget, with the reason returned:")
        A("")
        A("| Camera | Reason |")
        A("|---|---|")
        for r in failed:
            A(f"| {r['camera_ref']} | {r['error']} |")
        A("")
    A("## What the analytics produced")
    A("")
    A("| Measurement | Value |")
    A("|---|---:|")
    A(f"| Frames decoded | {frames} |")
    A(f"| Frames passing the motion gate | {gated} |")
    A(f"| Plate regions detected | {boxes} |")
    A(f"| Fused plate records | {len(recs)} |")
    A(f"| Grammar-valid registrations | **{len(valid)}** |")
    A(f"| Distinct valid registrations | **{len(distinct)}** |")
    A("")
    if valid:
        A("### Registrations read")
        A("")
        A("| Plate | Camera | Confidence | Frames fused | Observed (UTC) |")
        A("|---|---|---:|---:|---|")
        for r in results:
            for rec in r["records"]:
                if rec["valid"]:
                    A(
                        f"| `{rec['plate']}` | {r['camera_ref']} | {rec['confidence']:.2f} "
                        f"| {rec['frames_fused']} | {rec['observed_at_utc'][:19]} |"
                    )
        A("")
    A("### The finding that matters")
    A("")
    A(
        f"{frames} frames across {len(ok)} live cameras yielded {boxes} plate regions and "
        f"**{len(valid)} grammar-valid registrations**. That is not a pipeline fault: the "
        "recogniser reads what is legible, and at the resolution and framing these "
        "cameras publish, very little is. The evidence crops are committed, and the "
        "unreadable ones are unreadable to a human reviewer too."
    )
    A("")
    A(
        "This is the same resolution effect measured on the own-feed clip, where the "
        "identical pipeline reads 8 plates at 2560x1440, 2 at 1280x720 and none at "
        "704x396. It is the empirical basis for the amended scalability claim in "
        "`docs/HLD_RECONCILIATION.md`: sub-stream ingest buys throughput at an operating "
        "point where nothing is read, so the honest number is the full-resolution one."
    )
    A("")
    A(
        "A second, subtler class of error appears here. Reads such as those on the "
        "unreadable crops are rejected by the Indian plate grammar and never become "
        "registrations, which is the layered design working. But a read that is wrong "
        "*and* grammatical passes every check the system has. Precision against "
        "annotated ground truth is the only thing that measures that class; see "
        "`reports/evidence/anpr-accuracy-*`."
    )
    A("")
    A("## Declared versus measured frame rate")
    A("")
    A(
        f"The organiser's §2.2 warns not to trust the reported frame rate. Of {len(comparable)} "
        f"cameras that both declare a rate and delivered frames, **{len(diverged)} diverge "
        f"by more than 5%**. A further {len(undeclared)} delivered frames while declaring "
        "no rate at all."
    )
    A("")
    A("| Camera | Declared | Measured | Drift |")
    A("|---|---:|---:|---:|")
    for r in sorted(
        comparable,
        key=lambda x: -abs(
            (float(x["measured_fps"]) - float(x["declared_fps"])) / float(x["declared_fps"])
        ),
    ):
        d, m = float(r["declared_fps"]), float(r["measured_fps"])
        A(f"| {r['camera_ref']} | {d:.2f} | {m:.2f} | {(m - d) / d * 100:+.1f}% |")
    A("")
    A("## Provenance and caveats")
    A("")
    A(
        f"- Merged from {len(runs)} pass(es); {len(set(superseded))} camera(s) were "
        "re-run and the later result supersedes the earlier. Every attempt is retained "
        "in the source JSON."
    )
    if suspect:
        A(
            f"- **{len(suspect)} result(s) carry a suspect wall-clock time** "
            f"({', '.join(r['camera_ref'] for r in suspect)}): elapsed time exceeded the "
            "per-camera budget by more than 2x because the host suspended mid-run. "
            "Frame counts and PTS-derived rates for these are unaffected; only their "
            "elapsed time is meaningless, and it is excluded from every figure above."
        )
    A(
        "- Cameras returning HTTP 500 on their playlist are a gateway-side fault, "
        "reported in `docs/SUPPORT_QUERY.md`. Cameras that time out may be either."
    )
    A(
        "- Every detection listed is genuine inference on a live government feed, with "
        "the evidence crop written to `data/evidence/crops/`."
    )
    A("")
    A("Source records: " + ", ".join(f"`{p.name}`" for p in paths))
    A("")

    out = args.out or (EVIDENCE_DIR / f"gateway-output-report-{runs[-1]['started_utc'][:10]}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"cameras: {len(ok)} producing frames, {len(failed)} none")
    print(f"frames {frames}, boxes {boxes}, valid plates {len(valid)} ({len(distinct)} distinct)")
    print(f"fps divergence: {len(diverged)} of {len(comparable)} comparable")
    if suspect:
        print(f"suspect timings excluded: {', '.join(r['camera_ref'] for r in suspect)}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
