#!/usr/bin/env python
"""Run the ANPR pipeline over a video file and report measured performance.

Reports only what can be established objectively. Note carefully what "recall" means
here: strict recall requires ground truth -- a human counting every plate legible in
the footage -- which this script cannot produce on its own. It therefore reports the
rates it can measure, and writes every evidence crop to disk so that a ground-truth
pass can be run against real output rather than against an estimate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

# backend/ on the path so `services.*` imports resolve however this is launched.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import PROJECT_ROOT as REPO_ROOT  # noqa: E402

from services.analytics.anpr import (  # noqa: E402
    AnprPipeline,
    FastPlateRecogniser,
    OpenImagePlateDetector,
)
from services.common import evidence, redact  # noqa: E402
from services.ingest.file_source import FileSource  # noqa: E402

log = logging.getLogger("anpr")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--camera-ref", default=None)
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--analytic-fps", type=float, default=5.0)
    ap.add_argument("--motion-threshold", type=float, default=2.5)
    ap.add_argument("--crop-dir", type=Path, default=REPO_ROOT / "data" / "evidence" / "crops")
    ap.add_argument("--emit-evidence", action="store_true")
    ap.add_argument("--persist", action="store_true",
                    help="write detections to Postgres (idempotent on re-ingest)")
    args = ap.parse_args()

    redact.install(level=logging.INFO)

    source = FileSource(args.video, camera_ref=args.camera_ref, realtime=False, loop=False)
    caps = source.probe()
    print(f"\nsource : {args.video.name}")
    print(f"  {caps.width}x{caps.height} codec={caps.codec} "
          f"measured_fps={caps.measured_fps} duration={caps.duration_s:.0f}s")

    print("loading models...")
    t0 = time.monotonic()
    detector = OpenImagePlateDetector()
    recogniser = FastPlateRecogniser()
    print(f"  models ready in {time.monotonic() - t0:.1f}s")

    pipeline = AnprPipeline(
        detector, recogniser,
        crop_dir=args.crop_dir,
        analytic_fps=args.analytic_fps,
        motion_threshold=args.motion_threshold,
    )

    writer = None
    session = None
    if args.persist:
        from services.analytics.persistence import DetectionWriter
        from services.api.db import get_sessionmaker

        session = get_sessionmaker()()
        writer = DetectionWriter(session)

    records = []
    started = time.monotonic()
    for rec in pipeline.run(source, max_frames=args.max_frames):
        if writer is not None:
            writer.add(rec)
        records.append(rec)
        flag = "VALID   " if rec.plate.valid else "UNPARSED"
        corr = f" [{len(rec.plate.corrections)} corrected]" if rec.plate.corrections else ""
        print(f"  {flag} {rec.plate.normalised:<12} conf={rec.plate.confidence:.2f} "
              f"frames={rec.frames_fused} pts={rec.first_pts_ms:.0f}ms{corr}")
    elapsed = time.monotonic() - started
    source.close()

    if writer is not None and session is not None:
        writer.flush()
        session.commit()
        session.close()
        ps = writer.stats
        dropped = (
            f", {ps.unknown_camera} dropped (no registry camera)"
            if ps.unknown_camera else ""
        )
        print(
            "\npersisted: "
            f"{ps.inserted} inserted, {ps.duplicates} already present{dropped}"
        )

    st = pipeline.stats
    valid = [r for r in records if r.plate.valid]
    corrected = [r for r in valid if r.plate.corrections]
    distinct = Counter(r.plate.normalised for r in valid)

    print("\n" + "=" * 66)
    print("MEASURED PIPELINE PERFORMANCE")
    print("=" * 66)
    print(f"  frames decoded          : {st.frames_decoded}")
    print(f"  frames passing motion   : {st.frames_gated_in}  "
          f"({st.gate_pass_rate * 100:.1f}% gate pass rate)")
    print(f"  detector invocations    : {st.detector_runs}")
    print(f"  plate boxes detected    : {st.plates_detected}")
    print(f"  OCR invocations         : {st.ocr_runs}")
    print(f"  fused plate records     : {len(records)}")
    print(f"  grammar-valid plates    : {len(valid)}")
    print(f"  of which corrected      : {len(corrected)}")
    print(f"  distinct valid plates   : {len(distinct)}")
    print(f"  scene discontinuities   : {st.discontinuities}")
    print(f"  wall time               : {elapsed:.1f}s "
          f"({st.frames_decoded / elapsed:.1f} decode fps)")
    if distinct:
        print("\n  most frequent plates:")
        for plate, n in distinct.most_common(10):
            print(f"    {plate:<12} x{n}")
    print("=" * 66 + "\n")

    caveat = (
        "Strict recall requires ground truth: a human counting every plate legible in "
        "the footage. This run reports measured pipeline rates only. Evidence crops "
        "are written to disk so a ground-truth pass can be done against real output "
        "rather than an estimate."
    )

    payload = {
        "video": str(args.video),
        "source_properties": {
            "width": caps.width, "height": caps.height, "codec": caps.codec,
            "measured_fps": caps.measured_fps, "declared_fps": caps.declared_fps,
            "duration_s": caps.duration_s,
        },
        "settings": {
            "analytic_fps": args.analytic_fps,
            "motion_threshold": args.motion_threshold,
            "detector_model": detector.model_name,
            "recogniser_model": recogniser.model_name,
        },
        "measurements": {
            "frames_decoded": st.frames_decoded,
            "frames_gated_in": st.frames_gated_in,
            "gate_pass_rate": round(st.gate_pass_rate, 4),
            "detector_runs": st.detector_runs,
            "plates_detected": st.plates_detected,
            "ocr_runs": st.ocr_runs,
            "fused_records": len(records),
            "grammar_valid": len(valid),
            "corrected": len(corrected),
            "distinct_plates": len(distinct),
            "wall_seconds": round(elapsed, 2),
            "decode_fps": round(st.frames_decoded / elapsed, 2) if elapsed else 0,
        },
        "plates": [
            {
                "normalised": r.plate.normalised,
                "raw_fused": r.plate.raw,
                "valid": r.plate.valid,
                "pattern": r.plate.pattern,
                "confidence": round(r.plate.confidence, 4),
                "frames_fused": r.frames_fused,
                "first_pts_ms": r.first_pts_ms,
                "observed_at_utc": r.observed_at_utc.isoformat(),
                "clock_confidence": r.clock_confidence,
                "corrections": [c.to_dict() for c in r.plate.corrections],
                "crop_path": r.crop_path,
            }
            for r in records
        ],
        "recall_caveat": caveat,
    }
    out = REPO_ROOT / "reports" / "anpr_run.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"report: {out}")

    if args.emit_evidence:
        md = [
            "# ANPR pipeline - measured performance", "",
            f"- **Footage:** `{args.video.name}` "
            f"({caps.width}x{caps.height}, {caps.codec}, {caps.duration_s:.0f}s)",
            f"- **Detector:** {detector.model_name} (MIT)",
            f"- **Recogniser:** {recogniser.model_name} (MIT)",
            f"- **Analytic rate:** {args.analytic_fps} fps, sampled on PTS",
            "",
            "| measurement | value |", "|---|---:|",
            f"| frames decoded | {st.frames_decoded} |",
            f"| frames passing motion gate | {st.frames_gated_in} |",
            f"| **gate pass rate** | **{st.gate_pass_rate * 100:.1f}%** |",
            f"| detector invocations | {st.detector_runs} |",
            f"| plate boxes detected | {st.plates_detected} |",
            f"| fused plate records | {len(records)} |",
            f"| grammar-valid plates | {len(valid)} |",
            f"| of which corrected | {len(corrected)} |",
            f"| distinct valid plates | {len(distinct)} |",
            f"| decode throughput | {st.frames_decoded / elapsed:.1f} fps |",
            "", "## Caveat on recall", "", caveat,
        ]
        j, m = evidence.write("anpr-run", payload, "\n".join(md) + "\n")
        print(f"evidence: {j.name}, {m.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
