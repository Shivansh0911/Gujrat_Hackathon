"""Join plate reads to vehicle sightings after both pipelines have run.

Why this is a separate pass rather than part of ingest
------------------------------------------------------
The two pipelines are independent by design. `AnprPipeline` samples frames, gates on
motion, detects plates and fuses reads across a plate track. `SightingPipeline` samples
frames, detects vehicles and fuses a pass across a vehicle track. They do not agree on
which frames they looked at, their trackers close at different moments, and either can
run without the other -- a camera graded unviable for ANPR (§4.2) runs only the second.

Forcing them into one pass would couple those lifecycles and make the plate pipeline's
behaviour depend on whether vehicle detection happened to fire. Correlating afterwards
keeps both independently testable and lets the link be recomputed when either side is
reprocessed.

How a plate is matched to a vehicle
-----------------------------------
Three conditions, all of which must hold:

1. **Same camera.** Obvious, and a WHERE clause rather than a filter.
2. **Overlapping in time.** The plate read's `pts_ms` must fall inside the vehicle
   pass's `[first, last]` stream interval, widened by `PTS_SLACK_MS`. Slack is needed
   because the two trackers close at different frames: a vehicle pass often ends a frame
   or two before or after the plate track it contains.
3. **The plate box sits inside the vehicle box.** This is the condition that does the
   real work. A plate is physically bolted to a vehicle, so its box is contained by the
   vehicle's. Containment is tested with tolerance, because the two boxes come from
   different frames of the same pass and the vehicle moved between them.

Where more than one vehicle qualifies, the smallest containing box wins: a plate inside
both a car and the bus behind it belongs to the car, and the car is the tighter box.

What a missing link means
-------------------------
Most sightings will never acquire one, and that is the expected state rather than a
failure. On the government estate the plate pipeline produces roughly one grammar-valid
read per 3,900 frames while the sighting pipeline produces a record for every vehicle
that passes. A sighting with no plate link is exactly the case this whole design exists
to serve.

The reverse -- a plate read with no containing vehicle sighting -- is also normal and is
counted separately. It happens when the vehicle detector missed a vehicle whose plate the
plate detector found, which is common at distance: a plate is high-contrast and
rectangular, and the thing it is attached to can be a grey smear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: How far outside a vehicle pass's stream interval a plate read may fall and still be
#: considered part of it. One analytic frame at 5 fps is 200 ms; two frames of slack
#: covers the trackers closing at different moments without reaching the next vehicle.
PTS_SLACK_MS: float = 400.0

#: Fraction of the plate box that may fall outside the vehicle box and still count as
#: contained. The two boxes come from different frames of the same pass, so the vehicle
#: has moved between them; requiring strict containment would reject most true pairs.
CONTAINMENT_TOLERANCE: float = 0.35


@dataclass
class LinkStats:
    plate_reads_considered: int = 0
    linked: int = 0
    #: Plate reads with no containing vehicle sighting. Normal at distance -- a plate is
    #: high-contrast and rectangular while the vehicle behind it can be a grey smear.
    no_containing_vehicle: int = 0
    already_linked: int = 0


_LINK = text(
    """
    WITH plate AS (
        SELECT d.id, d.observed_at_utc, d.camera_id, d.pts_ms,
               (d.vehicle_bbox->>'x1')::float AS px1,
               (d.vehicle_bbox->>'y1')::float AS py1,
               (d.vehicle_bbox->>'x2')::float AS px2,
               (d.vehicle_bbox->>'y2')::float AS py2
        FROM detection d
        WHERE d.camera_id = CAST(:camera_id AS uuid)
          AND d.vehicle_bbox IS NOT NULL
          AND d.observed_at_utc >= CAST(:from_ AS timestamptz)
          AND d.observed_at_utc <= CAST(:to AS timestamptz)
    ),
    pairs AS (
        SELECT p.id AS plate_id, p.observed_at_utc AS plate_at,
               s.id AS sighting_id, s.observed_at_utc AS sighting_at,
               (s.vehicle_bbox->>'x2')::float - (s.vehicle_bbox->>'x1')::float AS sw,
               (s.vehicle_bbox->>'y2')::float - (s.vehicle_bbox->>'y1')::float AS sh,
               -- Overlap of the plate box with the vehicle box, as a fraction of the
               -- plate box. 1.0 is full containment.
               GREATEST(0, LEAST(p.px2, (s.vehicle_bbox->>'x2')::float)
                         - GREATEST(p.px1, (s.vehicle_bbox->>'x1')::float))
             * GREATEST(0, LEAST(p.py2, (s.vehicle_bbox->>'y2')::float)
                         - GREATEST(p.py1, (s.vehicle_bbox->>'y1')::float))
             / NULLIF((p.px2 - p.px1) * (p.py2 - p.py1), 0) AS contained
        FROM plate p
        JOIN vehicle_sighting s
          ON s.camera_id = p.camera_id
         AND s.plate_detection_id IS NULL
         AND p.pts_ms >= s.pts_ms - CAST(:slack AS float)
         AND p.pts_ms <= s.pts_ms + CAST(:slack AS float)
    ),
    best AS (
        -- Smallest containing box wins: a plate inside both a car and the bus behind
        -- it belongs to the car, and the car is the tighter box.
        SELECT DISTINCT ON (plate_id)
               plate_id, plate_at, sighting_id, sighting_at
        FROM pairs
        WHERE contained >= CAST(:min_contained AS float)
        ORDER BY plate_id, sw * sh ASC
    )
    UPDATE vehicle_sighting s
       SET plate_detection_id = b.plate_id,
           plate_detection_at = b.plate_at
      FROM best b
     WHERE s.id = b.sighting_id
       AND s.observed_at_utc = b.sighting_at
    """
)


def link_plate_reads(
    session: Session,
    camera_id: Any,
    *,
    from_: Any,
    to: Any,
    slack_ms: float = PTS_SLACK_MS,
    tolerance: float = CONTAINMENT_TOLERANCE,
) -> LinkStats:
    """Attach plate reads to the vehicle sightings that contain them, for one camera.

    Scoped to one camera and one time window rather than run over everything: this is
    called after ingesting a camera's footage, and a full-table pass would grow with the
    estate while the work to do stays constant.

    Idempotent. Only sightings with `plate_detection_id IS NULL` are considered, so a
    second run neither duplicates nor overwrites. Re-linking after reprocessing requires
    clearing the column first, which is deliberate -- an existing link is evidence that
    something was already decided, and silently replacing it would be invisible.
    """
    stats = LinkStats()

    stats.plate_reads_considered = int(
        session.execute(
            text(
                "SELECT count(*) FROM detection WHERE camera_id = CAST(:camera_id AS uuid)"
                "  AND vehicle_bbox IS NOT NULL"
                "  AND observed_at_utc >= CAST(:from_ AS timestamptz)"
                "  AND observed_at_utc <= CAST(:to AS timestamptz)"
            ),
            {"camera_id": str(camera_id), "from_": from_, "to": to},
        ).scalar_one()
    )
    stats.already_linked = int(
        session.execute(
            text(
                "SELECT count(*) FROM vehicle_sighting"
                " WHERE camera_id = CAST(:camera_id AS uuid) AND plate_detection_id IS NOT NULL"
            ),
            {"camera_id": str(camera_id)},
        ).scalar_one()
    )

    result = cast(
        CursorResult[Any],
        session.execute(
            _LINK,
            {
                "camera_id": str(camera_id),
                "from_": from_,
                "to": to,
                "slack": slack_ms,
                "min_contained": 1.0 - tolerance,
            },
        ),
    )
    stats.linked = int(result.rowcount or 0)
    stats.no_containing_vehicle = max(0, stats.plate_reads_considered - stats.linked)
    session.flush()

    log.info(
        "plate linking camera=%s: %d reads considered, %d linked, %d with no containing vehicle",
        camera_id,
        stats.plate_reads_considered,
        stats.linked,
        stats.no_containing_vehicle,
    )
    return stats


__all__ = ["CONTAINMENT_TOLERANCE", "PTS_SLACK_MS", "LinkStats", "link_plate_reads"]
