#!/usr/bin/env python
"""Export a small, real set of vehicle sightings so a deployment can have them without inference.

Why this exists
---------------
`ingest_sightings.py` produces sightings by running vehicle detection over footage.
That is the right thing on a machine with a processor. The hosted instance has a
fraction of one and 512 MB of memory shared with the API, and the seed that runs there
was killed before it finished on every attempt -- leaving the appearance search working
and empty, which is the one state worse than not shipping it.

So the detection happens once, here, on a machine that can do it, and its output is
committed: the crops the detector cut, and the descriptors it computed from them. A
deployment loads those rows instead of recomputing them.

What this is and is not
-----------------------
**The data is real.** Every crop is a frame region the vehicle detector selected, and
every descriptor is `appearance.py`'s output over that crop. Nothing is synthesised,
interpolated or hand-written. What moves is *where the arithmetic ran*, not what it
produced -- the same relationship `data/seed/` already has with the catalogue it holds.

**It is not a substitute for the pipeline.** A deployment with the capacity to run
`ingest_sightings.py` should: the fixture is four cameras of one clip, and the real
thing is whatever the estate is actually showing. The entrypoint prefers the live
ingest and falls back to this only when that produced nothing.

Selection
---------
Sightings are taken at matching stream positions across the four replay cameras, so the
*same* vehicles appear on all of them -- which is what makes a cross-camera search
return anything at all. Crops above `MAX_CROP_BYTES` are skipped: a 591 KB crop in the
repository buys nothing a 40 KB one does not, and the fixture has to stay small enough
that committing it is reasonable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

#: Per-crop ceiling. The median crop is 24 KB; the tail runs to 591 KB because a crop is
#: cut at the resolution it was found in, and the largest add size without adding
#: evidence.
MAX_CROP_BYTES = 60 * 1024

#: Sightings per camera. Enough to browse, and enough that a search between cameras has
#: several plausible pairs to rank.
PER_CAMERA = 10

REPLAY_REFS = ("REPLAY-01", "REPLAY-02", "REPLAY-03", "REPLAY-04")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=BACKEND_ROOT.parent / "data" / "seed" / "sighting_fixture.json",
    )
    ap.add_argument(
        "--crop-dir",
        type=Path,
        default=BACKEND_ROOT.parent / "data" / "seed" / "sighting_crops",
    )
    ap.add_argument("--per-camera", type=int, default=PER_CAMERA)
    args = ap.parse_args()

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from services.api.tenancy import set_admin_context
    from services.common.dburl import normalise_pg_url
    from services.common.paths import CROPS_DIR

    url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ.get("SETU_DATABASE_URL")
    if not url:
        raise SystemExit("set SETU_MIGRATION_DATABASE_URL or SETU_DATABASE_URL")

    session = sessionmaker(bind=create_engine(normalise_pg_url(url), future=True), future=True)()
    set_admin_context(session)

    args.crop_dir.mkdir(parents=True, exist_ok=True)
    for stale in args.crop_dir.glob("*.jpg"):
        stale.unlink()

    records: list[dict[str, Any]] = []
    for ref in REPLAY_REFS:
        rows = session.execute(
            text(
                """
                SELECT s.crop_path, s.pts_ms, s.last_pts_ms, s.vehicle_class,
                       s.vehicle_class_confidence, s.vehicle_bbox, s.rel_area, s.rel_cx,
                       s.rel_cy, s.aspect_ratio, s.quality_score, s.frames_seen,
                       s.track_displacement_px, s.dominant_colour_lab,
                       s.appearance_vector::text AS vector, s.appearance_kind
                FROM vehicle_sighting s
                JOIN camera c ON c.id = s.camera_id
                WHERE c.camera_ref = :ref
                  AND s.appearance_vector IS NOT NULL
                  AND s.crop_path IS NOT NULL
                -- Ordered by position in the clip, not by quality: the four cameras
                -- replay the same footage, so matching positions mean matching
                -- vehicles, and a cross-camera search needs those to exist.
                ORDER BY s.pts_ms
                """
            ),
            {"ref": ref},
        ).mappings()

        kept = 0
        for row in rows:
            if kept >= args.per_camera:
                break
            source = CROPS_DIR / Path(row["crop_path"]).name
            if not source.exists() or source.stat().st_size > MAX_CROP_BYTES:
                continue
            shutil.copy2(source, args.crop_dir / source.name)
            records.append(
                {
                    "camera_ref": ref,
                    "crop_file": source.name,
                    "pts_ms": float(row["pts_ms"]),
                    "last_pts_ms": float(row["last_pts_ms"]),
                    "vehicle_class": row["vehicle_class"],
                    "vehicle_class_confidence": float(row["vehicle_class_confidence"]),
                    "vehicle_bbox": row["vehicle_bbox"],
                    "rel_area": float(row["rel_area"]),
                    "rel_cx": float(row["rel_cx"]),
                    "rel_cy": float(row["rel_cy"]),
                    "aspect_ratio": float(row["aspect_ratio"]),
                    "quality_score": float(row["quality_score"]),
                    "frames_seen": int(row["frames_seen"]),
                    "track_displacement_px": float(row["track_displacement_px"]),
                    "dominant_colour_lab": row["dominant_colour_lab"],
                    "appearance_vector": row["vector"],
                    "appearance_kind": row["appearance_kind"],
                }
            )
            kept += 1
        print(f"  {ref}: {kept} sightings")

    args.out.write_text(json.dumps(records, indent=1), encoding="utf-8")
    total_kb = sum(f.stat().st_size for f in args.crop_dir.glob("*.jpg")) // 1024
    print(f"\nwrote {len(records)} sightings to {args.out}")
    print(
        f"copied {len(list(args.crop_dir.glob('*.jpg')))} crops ({total_kb} KB) to {args.crop_dir}"
    )
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
