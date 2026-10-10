#!/usr/bin/env python
"""Load the committed vehicle sightings, for a deployment that cannot run detection.

`ingest_sightings.py` is the real path and stays the first choice. This one exists for
the hosted instance, which has a fraction of a CPU and 512 MB shared with the API: the
detection job there was killed before finishing on every attempt, leaving the appearance
search deployed and empty, which is worse than not shipping it.

The rows loaded here are genuine output of that same pipeline, computed once on a
machine that could do it (`export_sighting_fixture.py`). The crops are frame regions the
detector selected; the descriptors are `appearance.py`'s output over those crops. What
moved is where the arithmetic ran, not what it produced.

Timeline
--------
The fixture stores each sighting's position *within the clip* (`pts_ms`), not a
wall-clock time, so this places them the same way `seed_demo.py` places its plate
reads: an epoch per camera, offset by the real travel time between those four cameras.
Anchored to the plate reads already in the database when there are any, so the two
halves describe one journey and plate linking has something to join.

Idempotent on `(camera, track position)`: running it twice adds nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

#: Minutes past the first camera's epoch, from the real distances between them. The
#: same numbers `seed_demo.py` uses, so the two seeds describe one vehicle's movement.
OFFSETS_MIN = {"REPLAY-01": 0, "REPLAY-02": 100, "REPLAY-03": 300, "REPLAY-04": 340}

_INSERT = """
INSERT INTO vehicle_sighting (
    id, camera_id, observed_at_utc, vehicle_class, vehicle_class_confidence,
    vehicle_bbox, rel_area, rel_cx, rel_cy, aspect_ratio, quality_score,
    frames_seen, track_id, pts_ms, last_pts_ms, clock_confidence, ingested_at_utc,
    track_displacement_px, appearance_vector, appearance_kind, dominant_colour_lab,
    crop_path
) VALUES (
    :id, :camera_id, :observed_at, :cls, :cls_conf,
    CAST(:bbox AS jsonb), :rel_area, :rel_cx, :rel_cy, :aspect, :quality,
    :frames, :track, :pts, :last_pts, 1.0, now(),
    :displacement, CAST(:vector AS vector), :kind, CAST(:colour AS jsonb),
    :crop
)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fixture",
        type=Path,
        default=BACKEND_ROOT.parent / "data" / "seed" / "sighting_fixture.json",
    )
    ap.add_argument(
        "--crop-source",
        type=Path,
        default=BACKEND_ROOT.parent / "data" / "seed" / "sighting_crops",
    )
    ap.add_argument(
        "--hours-ago",
        type=float,
        default=7.0,
        help="where to place the first camera's timeline when there are no plate reads "
        "to anchor to",
    )
    args = ap.parse_args()

    if not args.fixture.exists():
        print(f"no fixture at {args.fixture}; nothing to load", file=sys.stderr)
        return 0

    import shutil

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from services.api.db import get_sessionmaker
    from services.api.tenancy import set_admin_context
    from services.common.paths import CROPS_DIR

    try:
        session = get_sessionmaker()()
    except Exception:  # noqa: BLE001 -- fall back to the raw URL outside the API process
        import os

        from services.common.dburl import normalise_pg_url

        url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ["SETU_DATABASE_URL"]
        session = sessionmaker(
            bind=create_engine(normalise_pg_url(url), future=True), future=True
        )()
    set_admin_context(session)

    records: list[dict[str, Any]] = json.loads(args.fixture.read_text(encoding="utf-8"))
    cameras = {
        row.camera_ref: row.id
        for row in session.execute(
            text("SELECT id, camera_ref FROM camera WHERE camera_ref = ANY(:refs)"),
            {"refs": sorted(OFFSETS_MIN)},
        )
    }
    missing = sorted(set(OFFSETS_MIN) - set(cameras))
    if missing:
        # Not an error: the replay cameras are created by the demo seed, and a registry
        # without them simply has nothing for these rows to attach to.
        print(f"replay cameras absent ({', '.join(missing)}); nothing to load", file=sys.stderr)
        return 0

    # Anchor to the plate reads when there are any, for the same reason
    # `ingest_sightings.py --align-to-reads` does: two timelines computed independently
    # end up as far apart as the gap between the runs that computed them.
    anchor = session.execute(
        text(
            "SELECT min(d.observed_at_utc) FROM detection d JOIN camera c ON c.id = d.camera_id"
            " WHERE c.camera_ref = 'REPLAY-01'"
        )
    ).scalar()
    base = anchor or (datetime.now(timezone.utc) - timedelta(hours=args.hours_ago))
    print(f"placing the timeline from {base.isoformat()}")

    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    inserted = skipped = 0
    for record in records:
        ref = record["camera_ref"]
        camera_id = cameras[ref]
        epoch = base + timedelta(minutes=OFFSETS_MIN[ref])
        observed = epoch + timedelta(milliseconds=record["pts_ms"])

        # Idempotent on where the vehicle was in the clip, which is what identifies a
        # fixture row: re-running must not multiply the demonstration data.
        exists = session.execute(
            text(
                "SELECT 1 FROM vehicle_sighting"
                " WHERE camera_id = :cam AND pts_ms = :pts AND crop_path = :crop"
            ),
            {"cam": camera_id, "pts": record["pts_ms"], "crop": record["crop_file"]},
        ).first()
        if exists:
            skipped += 1
            continue

        source = args.crop_source / record["crop_file"]
        if source.exists():
            shutil.copy2(source, CROPS_DIR / record["crop_file"])

        session.execute(
            text(_INSERT),
            {
                "id": uuid.uuid4(),
                "camera_id": camera_id,
                "observed_at": observed,
                "cls": record["vehicle_class"],
                "cls_conf": record["vehicle_class_confidence"],
                "bbox": json.dumps(record["vehicle_bbox"]),
                "rel_area": record["rel_area"],
                "rel_cx": record["rel_cx"],
                "rel_cy": record["rel_cy"],
                "aspect": record["aspect_ratio"],
                "quality": record["quality_score"],
                "frames": record["frames_seen"],
                "track": uuid.uuid4().hex[:12],
                "pts": record["pts_ms"],
                "last_pts": record["last_pts_ms"],
                "displacement": record["track_displacement_px"],
                "vector": record["appearance_vector"],
                "kind": record["appearance_kind"],
                "colour": json.dumps(record["dominant_colour_lab"]),
                "crop": record["crop_file"],
            },
        )
        inserted += 1

    session.commit()
    print(f"inserted {inserted}, already present {skipped}")

    # Join the plate reads to the vehicles that contain them, exactly as the live ingest
    # does -- without it the appearance search has rows to search and no seed to search
    # from.
    if inserted:
        from services.analytics.plate_linking import link_plate_reads

        span = session.execute(
            text("SELECT min(observed_at_utc), max(observed_at_utc) FROM vehicle_sighting")
        ).first()
        if span and span[0]:
            linked = 0
            for ref, camera_id in cameras.items():
                stats = link_plate_reads(
                    session,
                    camera_id,
                    from_=span[0] - timedelta(hours=1),
                    to=span[1] + timedelta(hours=1),
                )
                linked += stats.linked
            session.commit()
            print(f"linked {linked} plate read(s) to the vehicles containing them")

    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
