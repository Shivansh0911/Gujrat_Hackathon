#!/usr/bin/env python
"""Run vehicle-sighting detection over footage or the gateway, and persist the result.

This is the ingest half of the capability the Phase 2 brief calls the x-factor: it
records every vehicle the cameras see, rather than only the ones whose number plate the
OCR could resolve. On the government estate that is the difference between roughly one
usable observation per 3,900 frames and one per vehicle (DISCOVERY findings 22 and 23).

It deliberately mirrors `ingest_gateway.py` rather than extending it. The two pipelines
are independent by design -- one reads plates, the other finds vehicles -- and
`plate_linking.py` joins their output afterwards. Fusing them into a single run would
couple their lifecycles and make the plate pipeline's behaviour depend on whether
vehicle detection happened to fire.

Usage
-----
    # Own-feed footage against a registry camera
    python scripts/ingest_sightings.py --file ../data/own_feed/demo_clip.mp4 \
        --camera-ref REPLAY-01 --max-frames 900

    # The government feed, one pass over the catalogue
    python scripts/ingest_sightings.py --gateway --seconds 30

Nothing here writes a camera row. A sighting against a camera the registry does not know
is counted and dropped loudly rather than written against a fabricated camera, which is
the same rule `DetectionWriter` applies.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.analytics.plate_linking import link_plate_reads  # noqa: E402
from services.analytics.sighting import SightingPipeline  # noqa: E402
from services.analytics.sighting_persistence import SightingWriter  # noqa: E402
from services.analytics.vehicle_detect import YoloxVehicleDetector  # noqa: E402
from services.api.db import get_sessionmaker  # noqa: E402
from services.api.tenancy import set_admin_context  # noqa: E402
from services.common.paths import CROPS_DIR  # noqa: E402

#: Pause between cameras, so a sweep does not become a burst the estate refuses. Same
#: value and same reason as `ingest_gateway`.
_SETTLE_S = 1.5

log = logging.getLogger("ingest-sightings")


def _ingest_source(
    source: Any,
    pipeline: SightingPipeline,
    writer: SightingWriter | None,
    *,
    max_frames: int | None,
) -> dict[str, Any]:
    """Run one source to completion. Never raises: a failure is a recorded result."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "camera_ref": getattr(source, "camera_ref", "?"),
        "ok": False,
        "error": None,
        "records": 0,
        "fingerprinted": 0,
        "below_quality_gate": 0,
        "frames_decoded": 0,
        "frames_analysed": 0,
        "vehicles_detected": 0,
    }
    try:
        count = 0
        for record in pipeline.run(source, max_frames=max_frames):
            count += 1
            if writer is not None:
                writer.add(record)
        stats = pipeline.stats
        result.update(
            ok=True,
            records=count,
            fingerprinted=stats.fingerprinted,
            below_quality_gate=stats.below_quality_gate,
            frames_decoded=stats.frames_decoded,
            frames_analysed=stats.frames_analysed,
            vehicles_detected=stats.vehicles_detected,
        )
    except Exception as exc:  # noqa: BLE001 -- a camera failing is data, not a crash
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("source %s failed: %s", result["camera_ref"], result["error"])
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def _file_sources(paths: list[str], camera_refs: list[str]) -> list[Any]:
    from services.ingest.file_source import FileSource

    if len(camera_refs) not in (1, len(paths)):
        raise SystemExit("--camera-ref must be given once, or once per --file")
    refs = camera_refs * len(paths) if len(camera_refs) == 1 else camera_refs
    # Not looping and not realtime: this is a batch ingest of a finite recording, and
    # replaying it at wall-clock pace would spend thirty minutes to read thirty minutes.
    return [
        FileSource(path, camera_ref=ref, realtime=False, loop=False)
        for path, ref in zip(paths, refs, strict=True)
    ]


def _gateway_sources(seconds: float) -> list[Any]:
    from services.common.catalogue import fetch_catalogue
    from services.common.config import get_settings
    from services.ingest.deadlined import Deadlined
    from services.ingest.gateway_source import GatewaySource

    settings = get_settings()
    descriptors = fetch_catalogue(settings)
    log.info("catalogue returned %d cameras", len(descriptors))
    # A live stream has no end, so each camera gets a wall-clock budget enforced by
    # wrapping the source rather than by changing the pipeline -- the pipeline must
    # behave identically on live and recorded input, or this run stops being evidence
    # about the same system every other report describes.
    return [Deadlined(GatewaySource(d, settings), seconds=seconds) for d in descriptors]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", action="append", default=[], help="footage to ingest")
    ap.add_argument(
        "--camera-ref",
        action="append",
        default=[],
        help="registry camera_ref for the footage; once, or once per --file",
    )
    ap.add_argument("--gateway", action="store_true", help="ingest the government catalogue")
    ap.add_argument("--seconds", type=float, default=30.0, help="wall-clock budget per camera")
    ap.add_argument("--max-frames", type=int, default=900)
    ap.add_argument("--analytic-fps", type=float, default=5.0)
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.30,
        help="detector confidence floor; see vehicle_detect.MIN_SCORE for why it is "
        "lower than the plate pipeline's",
    )
    ap.add_argument(
        "--no-persist",
        action="store_true",
        help="run the pipeline and report, writing nothing",
    )
    ap.add_argument(
        "--no-link",
        action="store_true",
        help="skip joining plate reads to sightings afterwards",
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)sZ %(levelname)-7s %(name)s: %(message)s"
    )

    if not args.file and not args.gateway:
        raise SystemExit("give --file (one or more) or --gateway")

    detector = YoloxVehicleDetector(min_score=args.min_score)
    pipeline = SightingPipeline(detector, crop_dir=CROPS_DIR, analytic_fps=args.analytic_fps)

    sources = (
        _gateway_sources(args.seconds)
        if args.gateway
        else _file_sources(args.file, args.camera_ref)
    )

    started = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    session = None if args.no_persist else get_sessionmaker()()
    if session is not None:
        # Without this the registry lookup returns nothing and every sighting is
        # dropped as "no registry camera", because row-level security fails closed for
        # a connection carrying no identity. An ingest worker legitimately spans
        # departments -- it writes for whichever camera it is reading -- which is what
        # `set_admin_context` exists for. Session-scoped, not transaction-scoped: this
        # job commits more than once.
        set_admin_context(session)
    writer = SightingWriter(session) if session is not None else None
    try:
        for index, source in enumerate(sources):
            if index:
                time.sleep(_SETTLE_S)
            log.info("ingesting %s", getattr(source, "camera_ref", "?"))
            results.append(_ingest_source(source, pipeline, writer, max_frames=args.max_frames))

        if writer is not None and session is not None:
            writer.flush()
            session.commit()
            log.info(
                "persisted: %d inserted, %d duplicates, %d without a fingerprint, "
                "%d unknown camera",
                writer.stats.inserted,
                writer.stats.duplicates,
                writer.stats.without_fingerprint,
                writer.stats.unknown_camera,
            )

            if not args.no_link:
                # Linking is scoped to the cameras just ingested and to a window around
                # this run, so it does not grow with the estate.
                window_start = started - timedelta(hours=1)
                window_end = datetime.now(timezone.utc) + timedelta(hours=1)
                from sqlalchemy import text

                for result in results:
                    if not result["ok"]:
                        continue
                    camera_id = session.execute(
                        text("SELECT id FROM camera WHERE camera_ref = :ref"),
                        {"ref": result["camera_ref"]},
                    ).scalar_one_or_none()
                    if camera_id is None:
                        continue
                    stats = link_plate_reads(session, camera_id, from_=window_start, to=window_end)
                    result["linked_plate_reads"] = stats.linked
                session.commit()
    finally:
        if session is not None:
            session.close()

    total_records = sum(r["records"] for r in results)
    total_frames = sum(r["frames_decoded"] for r in results)
    total_vehicles = sum(r["vehicles_detected"] for r in results)
    ok = sum(1 for r in results if r["ok"])

    print()
    print(f"sources           : {len(results)} ({ok} ok, {len(results) - ok} failed)")
    print(f"frames decoded    : {total_frames}")
    print(f"vehicles detected : {total_vehicles}")
    print(f"sightings emitted : {total_records}")
    if writer is not None:
        print(f"rows inserted     : {writer.stats.inserted}")
        print(f"without fingerprint: {writer.stats.without_fingerprint}")
    for result in results:
        status = "ok " if result["ok"] else "ERR"
        print(
            f"  [{status}] {result['camera_ref']:14s} "
            f"frames={result['frames_decoded']:5d} "
            f"vehicles={result['vehicles_detected']:4d} "
            f"sightings={result['records']:3d} "
            f"linked={result.get('linked_plate_reads', '-')}"
            + (f"  {result['error']}" if result["error"] else "")
        )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "started_utc": started.isoformat(),
                    "finished_utc": datetime.now(timezone.utc).isoformat(),
                    "results": results,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nreport: {args.json}")

    # A run where every source failed is a failed run, even though each individual
    # failure was handled. A zero exit there would make a broken sweep look healthy in
    # CI or in a cron log.
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
