#!/usr/bin/env python
"""Populate a demonstrable dataset end to end, with the gateway unreachable.

What this does and does not claim
---------------------------------
The government gateway's media plane has been returning 502 on every playlist, so
there is no multi-camera live feed to ingest. Route reconstruction needs the same
vehicle seen at more than one camera, so this script **replays our own-feed clip
through several registry cameras**, running the full ANPR pipeline separately for
each one.

Every detection produced here is a genuine inference result on real footage, with a
real evidence crop -- nothing is copied between cameras and no plate is invented.
What is simulated is only *which camera saw it*: the geography is assigned so the
cross-camera correlation, plausibility gating and coverage-gap logic can be
demonstrated at all.

Those cameras are named with a `REPLAY` prefix so an operator, or a jury, can see
immediately which sightings came from the replay harness and which would come from a
live feed. `docs/DEMO_RUNBOOK.md` states the same thing.

Usage:
    python scripts/seed_demo.py [--reset] [--frames 900]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# backend/ on the path so `services.*` imports resolve however this is launched.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import PROJECT_ROOT as REPO_ROOT  # noqa: E402

from sqlalchemy import delete, func, select, text  # noqa: E402

from services.analytics.anpr import (  # noqa: E402
    AnprPipeline,
    FastPlateRecogniser,
    OpenImagePlateDetector,
)
from services.analytics.matcher import scan_detections  # noqa: E402
from services.analytics.persistence import DetectionWriter  # noqa: E402
from services.api.db import get_sessionmaker  # noqa: E402
from services.api.tenancy import set_admin_context  # noqa: E402
from services.common import redact  # noqa: E402
from services.ingest.file_source import FileSource  # noqa: E402
from services.registry.enums import CameraStatus, SourceType  # noqa: E402
from services.registry.models import Alert, Camera, Detection  # noqa: E402

log = logging.getLogger("demo")

OWN_FEED_DIR = REPO_ROOT / "data" / "own_feed"

# Replay positions, borrowed from real registry cameras so the coordinates and their
# confidence radii are genuine. Ordered south to north-west along a plausible corridor
# and spaced in time so implied speeds sit inside the plausibility ceiling.
#
# The minutes offset is chosen per leg from the real distance between these cameras:
# too short and the plausibility gate correctly rejects the hop, which would make the
# demo look broken when it is in fact working.
# Offsets are real driving times for these legs, not round numbers. Junagadh to
# Rajkot is about 100 km, Rajkot to Ahmedabad about 215 km, Ahmedabad to Gandhinagar
# about 25 km. An earlier version used 55/150/185 minutes, which implied 158 km/h on
# the Rajkot-Ahmedabad leg; the plausibility gate correctly rejected that hop, and a
# demo in which the system discards a legitimate sighting reads as a defect even when
# the gate is doing exactly its job.
REPLAY_CAMERAS = [
    ("REPLAY-01", "11", 0),  # Dolatpara, Junagadh   (1.5 km radius)
    ("REPLAY-02", "17", 100),  # Rajkot Bus Port       (4 km radius)   ~60 km/h
    ("REPLAY-03", "4", 300),  # Paldi, Ahmedabad      (5 km radius)   ~65 km/h
    ("REPLAY-04", "12", 340),  # Adalaj, Gandhinagar   (5 km radius)   ~38 km/h
]


def _pick_clip() -> Path:
    clips = sorted(
        p
        for p in OWN_FEED_DIR.glob("*")
        if p.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov", ".webm"}
    )
    if not clips:
        raise SystemExit(
            f"no video in {OWN_FEED_DIR}. Drop a clip there, or see SOURCE.md for the "
            "openly-licensed fallback used during development."
        )
    return clips[0]


def ensure_replay_cameras(session) -> list[tuple[Camera, int]]:
    """Create the replay cameras, copying geometry from real registry entries."""
    out: list[tuple[Camera, int]] = []
    for ref, source_ref, offset_min in REPLAY_CAMERAS:
        origin = session.execute(
            select(Camera).where(Camera.camera_ref == source_ref)
        ).scalar_one_or_none()
        if origin is None or origin.geom is None:
            log.warning("source camera %s has no geometry; skipping %s", source_ref, ref)
            continue

        camera = session.execute(
            select(Camera).where(Camera.camera_ref == ref)
        ).scalar_one_or_none()
        if camera is None:
            camera = Camera(
                camera_ref=ref,
                name=f"REPLAY · {origin.location_text or origin.name}",
                location_text=origin.location_text,
                department_id=origin.department_id,
                source_type=SourceType.FILE.value,
                status=CameraStatus.ACTIVE.value,
                transport="file",
            )
            session.add(camera)
            session.flush()

        # Copy the real coordinate and its real uncertainty, so plausibility gating
        # and the map render exactly as they would for the source camera.
        session.execute(
            text(
                "UPDATE camera SET geom = (SELECT geom FROM camera WHERE id = :src), "
                "geom_source = (SELECT geom_source FROM camera WHERE id = :src), "
                "confidence_radius_m = (SELECT confidence_radius_m FROM camera WHERE id = :src), "
                "resolved_by = (SELECT resolved_by FROM camera WHERE id = :src) "
                "WHERE id = :dst"
            ),
            {"src": origin.id, "dst": camera.id},
        )
        out.append((camera, offset_min))
    session.flush()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="clear detections and alerts first")
    ap.add_argument("--frames", type=int, default=900, help="frames to process per camera")
    args = ap.parse_args()

    redact.install(level=logging.INFO)
    clip = _pick_clip()
    session = get_sessionmaker()()
    # Background job: no human actor, and it legitimately spans departments.
    set_admin_context(session)

    try:
        if args.reset:
            session.execute(delete(Alert))
            session.execute(delete(Detection))
            session.commit()
            log.info("cleared existing detections and alerts")

        cameras = ensure_replay_cameras(session)
        session.commit()
        if not cameras:
            log.error("no replay cameras could be created; run `make seed` first")
            return 2

        print(f"\nDemo ingest from {clip.name}")
        print(f"  {len(cameras)} replay camera(s), {args.frames} frames each\n")

        detector = OpenImagePlateDetector()
        recogniser = FastPlateRecogniser()
        crop_dir = REPO_ROOT / "data" / "evidence" / "crops"

        # Anchor the timeline far enough back that the whole replayed route -- now
        # 340 minutes end to end -- falls inside the journey view's default window.
        base_epoch = datetime.now(timezone.utc) - timedelta(hours=7)
        total_inserted = 0

        for camera, offset_min in cameras:
            epoch = base_epoch + timedelta(minutes=offset_min)
            source = FileSource(
                clip, camera_ref=camera.camera_ref, realtime=False, loop=False, epoch=epoch
            )
            pipeline = AnprPipeline(detector, recogniser, crop_dir=crop_dir, analytic_fps=5.0)

            writer = DetectionWriter(session)
            for record in pipeline.run(source, max_frames=args.frames):
                writer.add(record)
            writer.flush()
            session.commit()
            source.close()

            # Record what the source actually delivered. The Health screen's
            # declared-versus-measured column is only meaningful if the measured
            # side is populated from a real decode rather than left null.
            caps = source.probe()
            camera.codec = caps.codec
            camera.resolution_w, camera.resolution_h = caps.width, caps.height
            camera.declared_fps = caps.declared_fps
            camera.measured_fps = caps.measured_fps
            camera.transport = caps.transport
            camera.last_seen_at = datetime.now(timezone.utc)
            session.commit()

            st = pipeline.stats
            print(
                f"  {camera.camera_ref}  T+{offset_min:>3}min  "
                f"{writer.stats.inserted:>3} inserted, {writer.stats.duplicates:>3} duplicate  "
                f"(gate {st.gate_pass_rate * 100:.0f}%, {st.frames_decoded} frames)"
            )
            total_inserted += writer.stats.inserted

        # Match against the watchlist and raise alerts.
        raised: list[str] = []
        stats = scan_detections(
            session, on_alert=lambda a, action: raised.append(f"{a.matched_value} ({action})")
        )
        session.commit()

        detections = session.execute(select(func.count()).select_from(Detection)).scalar_one()
        alerts = session.execute(select(func.count()).select_from(Alert)).scalar_one()

        multi = (
            session.execute(
                text(
                    # Only plates that parse as Indian registrations: an unparsed read
                    # is still evidence a vehicle passed, but it is useless as something
                    # to type into the journey box during a demonstration.
                    "SELECT d.plate_normalised, count(DISTINCT d.camera_id) AS cams, "
                    "       count(*) AS n, max(d.confidence) AS best "
                    "FROM detection d "
                    "WHERE d.plate_normalised ~ '^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$' "
                    "GROUP BY d.plate_normalised "
                    "HAVING count(DISTINCT d.camera_id) > 1 "
                    "ORDER BY cams DESC, best DESC LIMIT 5"
                )
            )
            .mappings()
            .all()
        )

        print(f"\n  detections in database : {detections}")
        print(
            f"  alerts raised          : {alerts} "
            f"(matched {stats.matched}, dedup {stats.deduplicated}, movement {stats.movement})"
        )
        print("\n  plates seen at more than one camera (use these for the journey demo):")
        for row in multi:
            print(
                f"    {row['plate_normalised']:<12} {row['cams']} cameras, "
                f"{row['n']} sightings, best confidence {row['best']:.2f}"
            )
        if not multi:
            print("    none - the journey demo will show a single hop")
        print()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
