"""Speed flagging between two sightings of one plate on two genuinely different cameras.

What makes this defensible, and what would not
----------------------------------------------
A speed-violation alert is a materially stronger claim than anything else this platform
raises. A journey hop says "this vehicle was probably seen here"; a speed alert says
"this vehicle was travelling at 150 km/h", and somebody may act on it. Three constraints
follow, and each is enforced rather than documented:

**Both cameras must be real.** The four `REPLAY-` cameras replay one clip through
several registry positions, so the distance between them is a property of the harness
and not of any vehicle's movement. The journey view already displays REPLAY attribution
and labels it to the viewer, which is honest because the viewer can see the label. A
computed speed carries no such label into whatever it is quoted in. Deriving one from
simulated attribution would be fabricating a capability, so REPLAY cameras are excluded
from ever being an input -- as a WHERE clause, not a convention.

**Both cameras must have a real position.** A camera placed at a district centroid is
5 km from where it actually is, and the "distance travelled" between two such cameras is
mostly the error bars.

**The speed must exceed the ceiling even after allowing for that error.** This reuses
the journey reconstruction's tolerance exactly: subtract the summed coordinate
uncertainty from the distance before dividing. A vehicle is flagged only when it must
have been speeding, not when it might have been.

If the deployed dataset contains no pair of real cameras that saw the same plate, this
produces no alerts. That is stated in the limitations rather than worked around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.registry.enums import AlertState
from services.registry.models import Alert, Detection

log = logging.getLogger(__name__)

#: How far back to look for the previous sighting of the same plate.
#:
#: Two hours matches the watchlist matcher's MOVEMENT_WINDOW. Beyond it, a pair of
#: sightings is two separate journeys rather than one leg, and the implied speed
#: describes nothing.
SPEED_WINDOW = timedelta(hours=2)

#: Below this, timestamp error dominates. Two sightings nine seconds apart across
#: cameras whose clock confidence is an estimate cannot support a speed claim.
MIN_SEPARATION_S = 10.0

#: Distance above which the highway ceiling applies rather than the urban one. Same
#: threshold the journey reconstruction uses, so the two cannot disagree about what
#: counts as an open road.
HIGHWAY_DISTANCE_M = 5000.0

#: A camera whose ref starts with this is a replay harness position, not a place.
REPLAY_PREFIX = "REPLAY"

MATCH_TYPE = "speed_violation"


@dataclass(frozen=True)
class SpeedFinding:
    plate: str
    from_camera_ref: str
    to_camera_ref: str
    distance_m: float
    seconds: float
    nominal_kmph: float
    tolerant_kmph: float
    ceiling_kmph: float
    tolerance_m: float


def _previous_real_sighting(session: Session, detection: Detection) -> dict[str, Any] | None:
    """The most recent sighting of this plate on a *different, real, placed* camera.

    Every condition here is a WHERE clause on purpose. Filtering in Python would leave
    the REPLAY exclusion one refactor away from being dropped by accident, and it is the
    condition that keeps this feature honest.
    """
    plate = detection.plate_normalised
    if not plate:
        return None

    row = (
        session.execute(
            text(
                """
            SELECT d.id,
                   d.observed_at_utc,
                   c.camera_ref,
                   c.confidence_radius_m,
                   ST_Distance(c.geom, target.geom) AS distance_m,
                   target.camera_ref AS to_ref,
                   target.confidence_radius_m AS to_radius
            FROM detection d
            JOIN camera c      ON c.id = d.camera_id
            JOIN camera target ON target.id = :camera_id
            WHERE d.plate_normalised = :plate
              AND d.camera_id <> :camera_id
              AND d.observed_at_utc <  :observed
              AND d.observed_at_utc >= :since
              -- Both ends must be real places, not harness positions.
              AND c.camera_ref      NOT LIKE :replay
              AND target.camera_ref NOT LIKE :replay
              -- and both must actually have a position to measure between.
              AND c.geom IS NOT NULL
              AND target.geom IS NOT NULL
            ORDER BY d.observed_at_utc DESC
            LIMIT 1
            """
            ),
            {
                "plate": plate,
                "camera_id": str(detection.camera_id),
                "observed": detection.observed_at_utc,
                "since": detection.observed_at_utc - SPEED_WINDOW,
                "replay": f"{REPLAY_PREFIX}%",
            },
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def evaluate_speed(
    session: Session,
    detection: Detection,
    *,
    max_speed_highway_kmph: float,
    max_speed_urban_kmph: float,
) -> SpeedFinding | None:
    """A speeding finding for this detection, or None.

    None covers every ordinary case: no previous sighting, only a harness camera to
    pair with, no position on one end, too little time between them, or a speed that is
    within the ceiling once uncertainty is allowed for.
    """
    prev = _previous_real_sighting(session, detection)
    if prev is None:
        return None

    seconds = (detection.observed_at_utc - prev["observed_at_utc"]).total_seconds()
    if seconds < MIN_SEPARATION_S:
        return None

    distance_m = float(prev["distance_m"] or 0.0)
    tolerance_m = float(prev["confidence_radius_m"] or 0.0) + float(prev["to_radius"] or 0.0)

    nominal = (distance_m / seconds) * 3.6
    # The honest speed: the slowest the vehicle could have been going given how badly
    # we know where the two cameras are.
    tolerant = (max(0.0, distance_m - tolerance_m) / seconds) * 3.6

    ceiling = max_speed_highway_kmph if distance_m > HIGHWAY_DISTANCE_M else max_speed_urban_kmph
    if tolerant <= ceiling:
        return None

    return SpeedFinding(
        plate=detection.plate_normalised or "",
        from_camera_ref=str(prev["camera_ref"]),
        to_camera_ref=str(prev["to_ref"]),
        distance_m=round(distance_m, 1),
        seconds=round(seconds, 2),
        nominal_kmph=round(nominal, 1),
        tolerant_kmph=round(tolerant, 1),
        ceiling_kmph=ceiling,
        tolerance_m=round(tolerance_m, 1),
    )


def raise_speed_alert(
    session: Session, detection: Detection, finding: SpeedFinding
) -> tuple[Alert, str]:
    """Create a speed alert, or fold into the open one for this plate and leg."""
    observed = detection.observed_at_utc

    existing = session.execute(
        select(Alert)
        .where(
            Alert.camera_id == detection.camera_id,
            Alert.matched_value == finding.plate,
            Alert.match_type == MATCH_TYPE,
            Alert.state != AlertState.RESOLVED.value,
            Alert.dedup_window_start > observed - SPEED_WINDOW,
        )
        .order_by(Alert.raised_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    sighting = {
        "camera_ref": finding.to_camera_ref,
        "observed_at_utc": observed.isoformat(),
        "from_camera_ref": finding.from_camera_ref,
        "distance_m": finding.distance_m,
        "seconds": finding.seconds,
        "implied_speed_kmph": finding.nominal_kmph,
        # The number the flag actually rests on, kept beside the headline one so a
        # reviewer can see the claim is the conservative reading, not the dramatic one.
        "speed_after_uncertainty_kmph": finding.tolerant_kmph,
        "coordinate_tolerance_m": finding.tolerance_m,
        "ceiling_kmph": finding.ceiling_kmph,
        "crop_path": detection.crop_path,
    }

    if existing is not None:
        existing.observation_count += 1
        existing.sightings = [*existing.sightings, sighting]
        return existing, "deduplicated"

    alert = Alert(
        watchlist_entry_id=None,
        camera_id=detection.camera_id,
        detection_id=detection.id,
        matched_value=finding.plate,
        match_type=MATCH_TYPE,
        # How far past the ceiling it went, capped -- a stronger overspeed is a
        # stronger claim, and 2x the ceiling is as strong as this gets.
        match_score=min(1.0, finding.tolerant_kmph / (finding.ceiling_kmph * 2.0)),
        priority=0.8,
        observed_at_utc=observed,
        dedup_window_start=observed,
        observation_count=1,
        state=AlertState.RAISED.value,
        sightings=[sighting],
    )
    session.add(alert)
    return alert, "created"
