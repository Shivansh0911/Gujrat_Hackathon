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
return anything at all.

Within that, the ones the plate linker matched come first. This was a correction: taking
the earliest ten per camera gave forty sightings clustered in the first three seconds of
the clip, while every plate read on that footage falls after the fifth second. Loaded into
a deployment, that fixture produced no plate links at all -- so the one screen that shows
why appearance search exists had no vehicle with a plate to contrast against the ones
without. The linked sightings sit at identical stream positions on all four cameras, so
preferring them costs nothing in cross-camera coverage.

The link itself is never exported. It is recomputed in the deployment by the same linker,
against that deployment's own plate reads, from bbox containment and stream position -- a
fixture row claims no relationship it has not earned there.

Crops have to stay small enough that committing them is reasonable, and the tail of the
distribution runs to 591 KB because a crop is cut at whatever resolution it was found in.
A crop over `MAX_CROP_BYTES` is therefore re-encoded at a lower JPEG quality until it
fits, and skipped only if it still does not. Re-encoding, not downscaling: the pixel
dimensions are left exactly as the detector cut them, so the crop still shows the region
it showed, at the size it showed it. The fixture records the quality used, and the
descriptor in the row beside it was computed from the original -- a recompressed crop is
display evidence, never the input to a measurement.
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

#: Per-crop ceiling. The median crop is 24 KB and the tail runs to 591 KB, because a crop
#: is cut at whatever resolution the vehicle occupied. 150 KB rather than the 60 KB this
#: started at: a 1478x912 crop -- a vehicle close enough for its plate to be read -- does
#: not reach 60 KB at any quality worth looking at, and forty crops at this ceiling is
#: about 1.5 MB, which is a reasonable thing to commit.
MAX_CROP_BYTES = 150 * 1024

#: JPEG qualities tried, in order, for a crop over the ceiling. Stops at the first that
#: fits. 35 is the floor: below it the blocking is visible enough to argue with, and a
#: crop nobody trusts is not worth the bytes.
RECOMPRESS_QUALITIES = (75, 60, 50, 40, 35)


def _place_crop(source: Path, destination: Path) -> int | None:
    """Copy a crop into the fixture, re-encoding it only if it is over the ceiling.

    Returns the JPEG quality used, or None when the file was copied untouched. Raises
    nothing and returns -1 when even the lowest quality does not fit, which the caller
    treats as a skip.

    The ceiling used to be a filter rather than a ceiling, and the sightings it dropped
    were not a random sample: a crop is large when the vehicle filled the frame, and a
    vehicle that fills the frame is the one whose plate was readable. Every plate-linked
    sighting on this footage was over 60 KB, so the fixture that reached a deployment had
    none of them.
    """
    if source.stat().st_size <= MAX_CROP_BYTES:
        shutil.copy2(source, destination)
        return None

    from PIL import Image

    with Image.open(source) as image:
        frame = image.convert("RGB")
        for quality in RECOMPRESS_QUALITIES:
            # `optimize` costs a second pass over the Huffman tables and buys a few
            # percent, which at this ceiling is the difference between two quality steps.
            frame.save(destination, "JPEG", quality=quality, optimize=True)
            if destination.stat().st_size <= MAX_CROP_BYTES:
                return quality

    destination.unlink(missing_ok=True)
    return -1


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

    # The deployment's own configuration first, the raw URL second. Same order as
    # `seed_sighting_fixture.py`: this runs from a developer checkout, where the database
    # is in `.env` and pydantic-settings is the only thing that reads it, and against an
    # arbitrary instance, where it arrives in the environment.
    url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ.get("SETU_DATABASE_URL")
    normalised = normalise_pg_url(url)
    if normalised:
        session = sessionmaker(bind=create_engine(normalised, future=True), future=True)()
    else:
        from services.api.db import get_sessionmaker

        session = get_sessionmaker()()
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
                -- Sightings the plate linker matched first, then by position in the
                -- clip -- not by quality. The four cameras replay the same footage, so
                -- matching positions mean matching vehicles, and a cross-camera search
                -- needs those to exist. The linked ones lead because every plate read on
                -- this footage falls after the fifth second: ordering by position alone
                -- returned only the opening seconds and no linkable vehicle at all.
                ORDER BY (s.plate_detection_id IS NULL), s.pts_ms
                """
            ),
            {"ref": ref},
        ).mappings()

        kept = 0
        for row in rows:
            if kept >= args.per_camera:
                break
            source = CROPS_DIR / Path(row["crop_path"]).name
            if not source.exists():
                continue
            quality = _place_crop(source, args.crop_dir / source.name)
            if quality == -1:
                continue
            records.append(
                {
                    "camera_ref": ref,
                    "crop_file": source.name,
                    # Null when the crop is byte-for-byte what the detector wrote.
                    "crop_jpeg_quality": quality,
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
