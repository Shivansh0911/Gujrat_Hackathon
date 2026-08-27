#!/usr/bin/env python
"""Run the ANPR pipeline against the government gateway, camera by camera.

This is the government-feed test case. It differs from `run_anpr.py` in three ways
that matter, all of them consequences of the feed being live rather than a file:

1.  **A live stream has no end.** Every camera gets a wall-clock budget as well as a
    frame budget, enforced by wrapping the source rather than by changing the
    pipeline -- the pipeline must behave identically on live and recorded input, or
    this run stops being evidence about the same system the other reports describe.
2.  **Cameras fail, and that is data.** A camera that yields nothing is recorded as a
    result carrying its reason, not skipped. That 13 of 30 cameras return no frames
    is one of the findings; swallowing it would be discarding the measurement.
3.  **Timing is anchored, not assumed.** GatewaySource anchors the PTS timeline at
    the first frame and carries an explicit clock confidence. Nothing here reads a
    declared frame rate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import CROPS_DIR, EVIDENCE_DIR  # noqa: E402

from services.analytics.anpr import (  # noqa: E402
    AnprPipeline,
    FastPlateRecogniser,
    OpenImagePlateDetector,
)
from services.common import redact  # noqa: E402
from services.common.catalogue import CameraDescriptor, fetch_catalogue  # noqa: E402
from services.common.config import get_settings  # noqa: E402
from services.ingest.deadlined import Deadlined  # noqa: E402
from services.ingest.gateway_source import GatewaySource  # noqa: E402

log = logging.getLogger("ingest-gateway")


def ingest_one(
    descriptor: CameraDescriptor,
    pipeline: AnprPipeline,
    *,
    seconds: float,
    max_frames: int,
    writer=None,
) -> dict:
    """Ingest a single camera. Never raises: a failure is a recorded result."""
    started = time.monotonic()
    result: dict = {
        "camera_ref": descriptor.external_id,
        "name": descriptor.name,
        "declared_fps": descriptor.declared_fps,
        "ok": False,
        "error": None,
        "transport": None,
        "records": [],
    }

    source = None
    try:
        source = Deadlined(GatewaySource(descriptor), seconds)
        result["transport"] = source.transport
        for rec in pipeline.run(source, max_frames=max_frames):
            if writer is not None:
                writer.add(rec)
            result["records"].append(
                {
                    "plate": rec.plate.normalised,
                    "raw_fused": rec.plate.raw,
                    "valid": rec.plate.valid,
                    "confidence": round(rec.plate.confidence, 4),
                    "frames_fused": rec.frames_fused,
                    "first_pts_ms": rec.first_pts_ms,
                    "observed_at_utc": rec.observed_at_utc.isoformat(),
                    "clock_confidence": rec.clock_confidence,
                    "corrections": [c.to_dict() for c in rec.plate.corrections],
                    "crop_path": rec.crop_path,
                }
            )
        st = pipeline.stats
        health = source.health()
        result.update(
            {
                "ok": st.frames_decoded > 0,
                "frames_decoded": st.frames_decoded,
                "frames_gated_in": st.frames_gated_in,
                "gate_pass_rate": round(st.gate_pass_rate, 4),
                "detector_runs": st.detector_runs,
                "plates_detected": st.plates_detected,
                "ocr_runs": st.ocr_runs,
                "discontinuities": st.discontinuities,
                "measured_fps": getattr(health, "measured_fps", None),
                "fps_drift": getattr(health, "fps_drift", None),
                "reconnects": getattr(health, "reconnects", None),
                "clock_confidence": source.clock_confidence,
            }
        )
        result["timed_out"] = source.timed_out
        if st.frames_decoded == 0:
            result["error"] = "no frames within budget"
    except Exception as exc:  # a live feed fails in many ways; every one is a result
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

    result["wall_seconds"] = round(time.monotonic() - started, 2)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30.0, help="wall-clock budget per camera")
    ap.add_argument("--max-frames", type=int, default=900)
    ap.add_argument("--analytic-fps", type=float, default=5.0)
    ap.add_argument("--motion-threshold", type=float, default=2.5)
    ap.add_argument(
        "--cameras",
        default=None,
        help="comma-separated camera refs; default is every catalogued camera",
    )
    ap.add_argument(
        "--persist",
        action="store_true",
        help="write detections to Postgres (idempotent on re-ingest)",
    )
    args = ap.parse_args()

    redact.install(level=logging.WARNING)
    settings = get_settings()

    descriptors = fetch_catalogue(settings)
    if args.cameras:
        wanted = {c.strip() for c in args.cameras.split(",") if c.strip()}
        descriptors = [d for d in descriptors if d.external_id in wanted]
    print(f"catalogue: {len(descriptors)} cameras, {args.seconds:.0f}s budget each")

    print("loading models...")
    t0 = time.monotonic()
    detector = OpenImagePlateDetector()
    recogniser = FastPlateRecogniser()
    print(f"  models ready in {time.monotonic() - t0:.1f}s")

    pipeline = AnprPipeline(
        detector,
        recogniser,
        crop_dir=CROPS_DIR,
        analytic_fps=args.analytic_fps,
        motion_threshold=args.motion_threshold,
    )

    writer = None
    session = None
    if args.persist:
        from services.analytics.persistence import DetectionWriter
        from services.api.db import get_sessionmaker
        from services.api.tenancy import set_admin_context

        session = get_sessionmaker()()
        set_admin_context(session)
        writer = DetectionWriter(session)

    started = datetime.now(timezone.utc)
    results = []
    for i, d in enumerate(descriptors, 1):
        print(f"\n[{i}/{len(descriptors)}] camera {d.external_id} ({d.name})")
        r = ingest_one(d, pipeline, seconds=args.seconds, max_frames=args.max_frames, writer=writer)
        results.append(r)
        if r["error"]:
            print(f"  FAILED  {r['error']}  ({r['wall_seconds']}s)")
        else:
            print(
                f"  {r['frames_decoded']} frames, {r['plates_detected']} plate boxes, "
                f"{len(r['records'])} fused, measured_fps={r.get('measured_fps')} "
                f"({r['wall_seconds']}s)"
            )
        for rec in r["records"]:
            flag = "VALID   " if rec["valid"] else "UNPARSED"
            print(
                f"    {flag} {rec['plate']:<12} conf={rec['confidence']:.2f} "
                f"frames={rec['frames_fused']}"
            )

    if writer is not None and session is not None:
        writer.flush()
        session.commit()
        ps = writer.stats
        print(
            f"\npersisted: {ps.inserted} inserted, {ps.duplicates} already present, "
            f"{ps.unknown_camera} dropped (no registry camera)"
        )
        session.close()

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    all_recs = [rec for r in results for rec in r["records"]]
    valid = [rec for rec in all_recs if rec["valid"]]
    distinct = Counter(rec["plate"] for rec in valid)
    drift = [r for r in ok if r.get("fps_drift") not in (None, 0)]

    print("\n" + "=" * 70)
    print("GOVERNMENT FEED INGEST - MEASURED")
    print("=" * 70)
    print(f"  cameras attempted        : {len(results)}")
    print(f"  cameras producing frames : {len(ok)}")
    print(f"  cameras producing none   : {len(failed)}")
    print(f"  frames decoded           : {sum(r.get('frames_decoded', 0) for r in ok)}")
    print(f"  plate boxes detected     : {sum(r.get('plates_detected', 0) for r in ok)}")
    print(f"  fused plate records      : {len(all_recs)}")
    print(f"  grammar-valid plates     : {len(valid)}")
    print(f"  distinct valid plates    : {len(distinct)}")
    print(f"  cameras with fps drift   : {len(drift)} of {len(ok)}")
    print("=" * 70 + "\n")

    stamp = started.strftime("%Y-%m-%dT%H-%M-%SZ")
    payload = {
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "gateway_host": settings.gateway_host,
        "settings": {
            "seconds_per_camera": args.seconds,
            "max_frames": args.max_frames,
            "analytic_fps": args.analytic_fps,
            "motion_threshold": args.motion_threshold,
            "detector_model": detector.model_name,
            "recogniser_model": recogniser.model_name,
        },
        "summary": {
            "cameras_attempted": len(results),
            "cameras_producing_frames": len(ok),
            "cameras_producing_none": len(failed),
            "frames_decoded": sum(r.get("frames_decoded", 0) for r in ok),
            "plates_detected": sum(r.get("plates_detected", 0) for r in ok),
            "fused_records": len(all_recs),
            "grammar_valid": len(valid),
            "distinct_plates": len(distinct),
            "cameras_with_fps_drift": len(drift),
        },
        "results": results,
    }
    out = EVIDENCE_DIR / f"gateway-ingest-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
