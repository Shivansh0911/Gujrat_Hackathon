"""Three findings that fall out of data the platform already has.

Each answers a question a police officer actually asks, and each is deliberately built
on the strongest evidence available rather than the most impressive.

Cloned plates: splitting a band `speed.py` currently conflates
-------------------------------------------------------------
`speed.py` raises a speed violation whenever a pair of sightings implies a speed above
the ceiling, even after allowing for coordinate uncertainty. That band contains two
completely different findings:

* 95 km/h in a 60 zone is **speeding**. A traffic offence, one vehicle, one plate.
* 735 km/h is **not speeding**. No road vehicle does that, so the plate was in two
  places at once -- which means either the same registration is on two vehicles, or one
  of the two reads is wrong.

Those call for different responses, and conflating them means a cloned plate is filed as
a traffic ticket. `IMPOSSIBLE_KMPH` splits the band. Both hypotheses are returned
together with the evidence for each, because the platform cannot tell them apart and
pretending otherwise would be the fabrication this project refuses: a cloned plate and a
misread plate look identical from here, and only a human looking at both crops can say
which it is.

Convoy: only on plates, and the reason matters
----------------------------------------------
A convoy claim is "these two specific vehicles travelled together", which requires
knowing that the vehicle at camera A is the same one at camera B -- twice over, for two
vehicles. Identity across cameras is either a plate read or an appearance match, and
DISCOVERY finding 25 measured appearance matching at 0.356 precision on this project's
own footage.

Building a convoy claim on two independent appearance matches compounds that: the
probability both links are right is the product, so roughly 0.13. A finding that is wrong
seven times in eight is not a finding. So convoy detection runs on plate reads only, and
where the estate yields no plates it yields no convoys. That is a documented limit, not
an omission, and it is better than a feature that would have looked impressive in a demo
and misled an investigation.

Dwell: the one that needs no cross-camera identity at all
----------------------------------------------------------
"This vehicle stopped here and stayed" is a single-camera question. It needs the extent
of one tracked pass and the distance its box moved -- both recorded directly by the
tracker since migration 0008 -- and no identity matching whatsoever. It is therefore the
most reliable of the three, works at 27 px, and is exactly what the godown, gate and
toll-yard scenarios the zones feature targets actually need.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# --- Cloned plates ------------------------------------------------------------------

#: Implied speed above which no road vehicle explains the pair, so the plate was in two
#: places at once.
#:
#: 200 km/h, not a precise physical limit. Indian expressway traffic tops out well below
#: it; a performance car on an empty road could approach it. Set high deliberately: the
#: cost of calling a genuine speeding event a cloned plate is an investigation opened
#: against an innocent registration, which is worse than filing a clone as a fast car and
#: having a human notice.
IMPOSSIBLE_KMPH: float = 200.0

#: Matches `speed.SPEED_WINDOW`. Beyond it a pair of sightings is two journeys.
CLONE_WINDOW = timedelta(hours=2)

#: Below this separation the timestamps themselves cannot support the claim. Same value
#: and same reason as `speed.MIN_SEPARATION_S`.
MIN_SEPARATION_S: float = 10.0

REPLAY_PREFIX = "REPLAY"


@dataclass(frozen=True)
class ClonedPlateFinding:
    plate: str
    from_camera_ref: str
    to_camera_ref: str
    from_observed_at: datetime
    to_observed_at: datetime
    distance_m: float
    seconds: float
    implied_kmph: float
    tolerance_m: float
    #: Both readings of the same evidence, returned together because the platform
    #: cannot distinguish them and a human looking at the crops can.
    hypotheses: tuple[str, str]
    from_crop_path: str | None
    to_crop_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plate": self.plate,
            "from_camera_ref": self.from_camera_ref,
            "to_camera_ref": self.to_camera_ref,
            "from_observed_at": self.from_observed_at.isoformat(),
            "to_observed_at": self.to_observed_at.isoformat(),
            "distance_m": round(self.distance_m, 1),
            "seconds": round(self.seconds, 1),
            "implied_kmph": round(self.implied_kmph, 1),
            "tolerance_m": round(self.tolerance_m, 1),
            "hypotheses": list(self.hypotheses),
            "from_crop_path": self.from_crop_path,
            "to_crop_path": self.to_crop_path,
        }


_CLONE_PAIRS = text(
    """
    SELECT a.plate_normalised AS plate,
           ca.camera_ref AS from_ref, cb.camera_ref AS to_ref,
           a.observed_at_utc AS from_at, b.observed_at_utc AS to_at,
           a.crop_path AS from_crop, b.crop_path AS to_crop,
           ST_Distance(ca.geom, cb.geom) AS metres,
           COALESCE(ca.confidence_radius_m, 0) + COALESCE(cb.confidence_radius_m, 0) AS tol,
           EXTRACT(EPOCH FROM (b.observed_at_utc - a.observed_at_utc)) AS secs
    FROM detection a
    JOIN detection b
      ON b.plate_normalised = a.plate_normalised
     AND b.observed_at_utc > a.observed_at_utc
     AND b.observed_at_utc <= a.observed_at_utc + CAST(:window AS interval)
     AND b.camera_id <> a.camera_id
    JOIN camera ca ON ca.id = a.camera_id
    JOIN camera cb ON cb.id = b.camera_id
    WHERE a.observed_at_utc >= :from_ AND a.observed_at_utc <= :to
      -- Both ends must be real places, not harness positions. The REPLAY cameras
      -- replay one clip through several registry coordinates, so the distance between
      -- them is a property of the harness and not of any vehicle's movement.
      AND ca.camera_ref NOT LIKE :replay
      AND cb.camera_ref NOT LIKE :replay
      AND ca.geom IS NOT NULL AND cb.geom IS NOT NULL
    ORDER BY a.observed_at_utc
    """
)


def find_cloned_plates(
    session: Session,
    *,
    from_: datetime,
    to: datetime,
    detour_factor: float = 1.3,
    impossible_kmph: float = IMPOSSIBLE_KMPH,
) -> list[ClonedPlateFinding]:
    """Plates observed at two cameras no vehicle could have travelled between.

    The tolerance subtraction is the same one the journey reconstruction and `speed.py`
    apply: a camera placed at a district centroid is 5 km from where it actually is, and
    judging it against a 300 m-precise neighbour on nominal distance invents impossible
    hops out of coordinate error. A pair is flagged only when it is impossible *even
    after* the uncertainty is handed to it.
    """
    rows = (
        session.execute(
            _CLONE_PAIRS,
            {
                "window": f"{int(CLONE_WINDOW.total_seconds())} seconds",
                "from_": from_,
                "to": to,
                "replay": f"{REPLAY_PREFIX}%",
            },
        )
        .mappings()
        .all()
    )

    out: list[ClonedPlateFinding] = []
    for row in rows:
        seconds = float(row["secs"] or 0.0)
        if seconds < MIN_SEPARATION_S:
            continue
        metres = float(row["metres"] or 0.0)
        tolerance = float(row["tol"] or 0.0)
        road_m = max(0.0, metres - tolerance) * detour_factor
        implied = (road_m / seconds) * 3.6
        if implied <= impossible_kmph:
            continue

        out.append(
            ClonedPlateFinding(
                plate=row["plate"],
                from_camera_ref=row["from_ref"],
                to_camera_ref=row["to_ref"],
                from_observed_at=row["from_at"],
                to_observed_at=row["to_at"],
                distance_m=road_m,
                seconds=seconds,
                implied_kmph=implied,
                tolerance_m=tolerance,
                hypotheses=(
                    f"the registration {row['plate']} is on two different vehicles",
                    "one of the two reads is wrong; compare the crops",
                ),
                from_crop_path=row["from_crop"],
                to_crop_path=row["to_crop"],
            )
        )
    return out


# --- Convoys ------------------------------------------------------------------------

#: How close in time two vehicles must be at one camera to count as travelling together.
CONVOY_GAP_S: float = 20.0

#: How many separate cameras must show the pair before it is a convoy rather than
#: coincidence.
#:
#: Three, not two. Two vehicles arriving at one camera within 20 s of each other is
#: ordinary traffic -- on a busy arterial it happens hundreds of times an hour. Two
#: cameras is a shared route, which most traffic on a corridor also has. Three separate
#: cameras in sequence is a pattern that ordinary traffic does not produce often, and it
#: is the lowest threshold at which the claim is worth making. Stated as a constant with
#: this reasoning because it is the parameter a reviewer should push back on.
CONVOY_MIN_CAMERAS: int = 3


@dataclass(frozen=True)
class ConvoyFinding:
    plate_a: str
    plate_b: str
    cameras: int
    first_seen: datetime
    last_seen: datetime
    #: Every co-sighting behind the claim, so it can be checked rather than believed.
    co_sightings: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "plate_a": self.plate_a,
            "plate_b": self.plate_b,
            "cameras": self.cameras,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "co_sightings": self.co_sightings,
        }


_CONVOY = text(
    """
    WITH pairs AS (
        SELECT LEAST(a.plate_normalised, b.plate_normalised)    AS plate_a,
               GREATEST(a.plate_normalised, b.plate_normalised) AS plate_b,
               a.camera_id, c.camera_ref,
               a.observed_at_utc AS at_a, b.observed_at_utc AS at_b
        FROM detection a
        JOIN detection b
          ON b.camera_id = a.camera_id
         AND b.plate_normalised <> a.plate_normalised
         AND b.observed_at_utc BETWEEN a.observed_at_utc
                                   AND a.observed_at_utc + CAST(:gap AS interval)
        JOIN camera c ON c.id = a.camera_id
        WHERE a.observed_at_utc >= :from_ AND a.observed_at_utc <= :to
          AND c.camera_ref NOT LIKE :replay
    )
    SELECT plate_a, plate_b,
           count(DISTINCT camera_id) AS cameras,
           min(at_a) AS first_seen,
           max(at_b) AS last_seen,
           json_agg(json_build_object(
               'camera_ref', camera_ref,
               'at_a', at_a,
               'at_b', at_b
           ) ORDER BY at_a) AS co_sightings
    FROM pairs
    GROUP BY plate_a, plate_b
    HAVING count(DISTINCT camera_id) >= :min_cameras
    ORDER BY count(DISTINCT camera_id) DESC, min(at_a)
    """
)


def find_convoys(
    session: Session,
    *,
    from_: datetime,
    to: datetime,
    gap_s: float = CONVOY_GAP_S,
    min_cameras: int = CONVOY_MIN_CAMERAS,
) -> list[ConvoyFinding]:
    """Pairs of registrations repeatedly seen together across several cameras.

    Plate reads only. See the module docstring for why appearance-based convoy
    detection is not offered: two independent appearance links at 0.356 precision
    compound to roughly 0.13, and a finding wrong seven times in eight is not a finding.
    """
    rows = (
        session.execute(
            _CONVOY,
            {
                "gap": f"{int(gap_s)} seconds",
                "from_": from_,
                "to": to,
                "min_cameras": min_cameras,
                "replay": f"{REPLAY_PREFIX}%",
            },
        )
        .mappings()
        .all()
    )
    return [
        ConvoyFinding(
            plate_a=row["plate_a"],
            plate_b=row["plate_b"],
            cameras=int(row["cameras"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            co_sightings=list(row["co_sightings"] or []),
        )
        for row in rows
    ]


# --- Dwell and abandonment ----------------------------------------------------------

#: How long a pass must last before it is dwelling rather than passing, in stream
#: milliseconds. 30 s is long enough to exclude a queue at a signal on most cycles and
#: short enough to catch a vehicle stopping at a gate.
DWELL_MS: float = 30_000.0

#: How far the box centre may travel, as a fraction of the box's own width, and still
#: count as stationary. Expressed relative to the box rather than in absolute pixels
#: because the same movement means different things for a vehicle filling the frame and
#: one at the far end of it -- and because no calibration is available to convert either
#: into metres.
DWELL_MAX_DRIFT_RATIO: float = 0.5


@dataclass(frozen=True)
class DwellFinding:
    sighting_id: Any
    camera_ref: str
    camera_name: str
    observed_at_utc: datetime
    vehicle_class: str
    dwell_seconds: float
    displacement_px: float
    box_width_px: float
    frames_seen: int
    crop_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sighting_id": str(self.sighting_id),
            "camera_ref": self.camera_ref,
            "camera_name": self.camera_name,
            "observed_at_utc": self.observed_at_utc.isoformat(),
            "vehicle_class": self.vehicle_class,
            "dwell_seconds": round(self.dwell_seconds, 1),
            "displacement_px": round(self.displacement_px, 1),
            "box_width_px": round(self.box_width_px, 1),
            "frames_seen": self.frames_seen,
            "crop_path": self.crop_path,
        }


_DWELL = text(
    """
    SELECT s.id, s.observed_at_utc, s.vehicle_class, s.crop_path, s.frames_seen,
           s.track_displacement_px,
           (s.last_pts_ms - s.pts_ms) / 1000.0 AS dwell_seconds,
           (s.vehicle_bbox->>'x2')::float - (s.vehicle_bbox->>'x1')::float AS box_w,
           c.camera_ref, c.name AS camera_name
    FROM vehicle_sighting s
    JOIN camera c ON c.id = s.camera_id
    WHERE s.observed_at_utc >= :from_ AND s.observed_at_utc <= :to
      AND s.last_pts_ms - s.pts_ms >= :dwell_ms
      AND (s.vehicle_bbox->>'x2')::float > (s.vehicle_bbox->>'x1')::float
      AND s.track_displacement_px
          <= :drift_ratio * ((s.vehicle_bbox->>'x2')::float - (s.vehicle_bbox->>'x1')::float)
    ORDER BY (s.last_pts_ms - s.pts_ms) DESC
    """
)


def find_dwells(
    session: Session,
    *,
    from_: datetime,
    to: datetime,
    dwell_ms: float = DWELL_MS,
    drift_ratio: float = DWELL_MAX_DRIFT_RATIO,
) -> list[DwellFinding]:
    """Vehicles that stayed in one camera's view without going anywhere.

    No cross-camera identity is involved, so this is the most reliable of the three
    findings and the only one that works unchanged on a camera that cannot read plates.
    """
    rows = (
        session.execute(
            _DWELL,
            {"from_": from_, "to": to, "dwell_ms": dwell_ms, "drift_ratio": drift_ratio},
        )
        .mappings()
        .all()
    )
    return [
        DwellFinding(
            sighting_id=row["id"],
            camera_ref=row["camera_ref"],
            camera_name=row["camera_name"],
            observed_at_utc=row["observed_at_utc"],
            vehicle_class=row["vehicle_class"],
            dwell_seconds=float(row["dwell_seconds"]),
            displacement_px=float(row["track_displacement_px"]),
            box_width_px=float(row["box_w"]),
            frames_seen=int(row["frames_seen"]),
            crop_path=row["crop_path"],
        )
        for row in rows
    ]


__all__ = [
    "CONVOY_GAP_S",
    "CONVOY_MIN_CAMERAS",
    "DWELL_MAX_DRIFT_RATIO",
    "DWELL_MS",
    "IMPOSSIBLE_KMPH",
    "ClonedPlateFinding",
    "ConvoyFinding",
    "DwellFinding",
    "find_cloned_plates",
    "find_convoys",
    "find_dwells",
]
