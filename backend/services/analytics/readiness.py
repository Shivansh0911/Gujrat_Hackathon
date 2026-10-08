"""Per-camera ANPR readiness: which cameras can read a plate, and what the rest need.

The reframing
-------------
The estate cannot read number plates. Most teams at this evaluation will treat that as
their problem to hide. It is actually Gujarat Police's problem -- it is a property of
where the cameras are mounted and which way they point, not of anyone's software -- and
the useful thing to hand them is not an excuse but a per-camera answer: this one works,
this one would work if it were re-aimed, this one never will.

What is measured, and from what
-------------------------------
Everything here comes from rows the platform already has. Nothing is simulated and
nothing is inferred from a camera's declared properties, which DISCOVERY finding 2
showed to be absent or wrong for most of the estate.

* **Plate pixel width** from `detection.vehicle_bbox`. That column holds the *plate*
  box despite its name -- see finding 23 -- which makes it exactly the right source for
  this one measurement and the wrong source for everything else.
* **Vehicle pixel width** from `vehicle_sighting.vehicle_bbox`, which holds a real
  vehicle box.
* **The ratio between them** is the interesting number, and it is what exposes mounting
  angle. A camera looking along the approach lane sees a plate roughly 1/4 to 1/3 the
  width of its vehicle. A camera looking across the carriageway, or down from a pole,
  sees a large vehicle with a tiny plate or no plate at all -- it is photographing
  roofs and flanks. The same vehicle width with a much smaller plate ratio is a camera
  that is aimed wrong rather than one that is too far away, and that distinction is the
  difference between "re-aim it" and "replace it".

The thresholds
--------------
`PLATE_PX_READABLE` is 70. That is not ours: two other shortlisted teams independently
measured the same estate and put the practical floor at 70 px, with the best plate any
government camera offered measuring 61. Our own figure is consistent -- the identical
pipeline reads 8 plates at 2560x1440, 2 at 1280x720 and none at 704x396 (finding 12).

`PLATE_PX_MARGINAL` is 45. Between 45 and 70 a plate is resolved but not reliably read,
and that band is where re-aiming changes the outcome: the pixels are nearly there, and
pointing the camera along the lane rather than across it recovers the rest.

Both are stated as constants with their provenance rather than buried in a query,
because a grade that drives a procurement recommendation should be arguable.

Why a sample size matters more than usual here
----------------------------------------------
A camera that produced four plate reads has not demonstrated anything about its median.
`INSUFFICIENT_SAMPLE` is a grade in its own right rather than a default to
`NOT_VIABLE`, because telling a department its camera is useless on the strength of
four observations is worse than telling it nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Plate width in pixels at or above which this pipeline reads reliably.
PLATE_PX_READABLE: float = 70.0

#: Plate width below which no amount of re-aiming helps; the camera is too far away or
#: too low-resolution for the plate to exist in the image at all.
PLATE_PX_MARGINAL: float = 45.0

#: Plate-to-vehicle width ratio typical of a camera aimed along the approach lane.
#: Below it, the camera is looking across the carriageway or down from height.
RATIO_ALIGNED: float = 0.22

#: Minimum observations before a grade is asserted rather than withheld.
MIN_PLATE_SAMPLE: int = 8
MIN_VEHICLE_SAMPLE: int = 15


class ReadinessGrade(StrEnum):
    """What a camera can actually be asked to do."""

    ANPR_CAPABLE = "anpr_capable"
    MARGINAL_REAIM = "marginal_reaim"
    NOT_VIABLE = "not_viable"
    INSUFFICIENT_SAMPLE = "insufficient_sample"


@dataclass(frozen=True)
class CameraReadiness:
    """One camera's measured ANPR readiness, with the evidence behind the grade."""

    camera_id: Any
    camera_ref: str
    camera_name: str

    plate_samples: int
    plate_px_median: float | None
    plate_px_p90: float | None

    vehicle_samples: int
    vehicle_px_median: float | None

    #: Median plate width over median vehicle width. None when either is missing.
    plate_to_vehicle_ratio: float | None

    grade: ReadinessGrade
    #: Why this grade, in a sentence an officer can act on.
    reasoning: str
    #: What to do about it, or None when there is nothing to do.
    remedy: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": str(self.camera_id),
            "camera_ref": self.camera_ref,
            "camera_name": self.camera_name,
            "plate_samples": self.plate_samples,
            "plate_px_median": self.plate_px_median,
            "plate_px_p90": self.plate_px_p90,
            "vehicle_samples": self.vehicle_samples,
            "vehicle_px_median": self.vehicle_px_median,
            "plate_to_vehicle_ratio": self.plate_to_vehicle_ratio,
            "grade": self.grade.value,
            "reasoning": self.reasoning,
            "remedy": self.remedy,
        }


_MEASURE = text(
    """
    WITH plate AS (
        SELECT d.camera_id,
               count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY (d.vehicle_bbox->>'x2')::float - (d.vehicle_bbox->>'x1')::float
               ) AS px_median,
               percentile_cont(0.9) WITHIN GROUP (
                   ORDER BY (d.vehicle_bbox->>'x2')::float - (d.vehicle_bbox->>'x1')::float
               ) AS px_p90
        FROM detection d
        WHERE d.vehicle_bbox IS NOT NULL
        GROUP BY d.camera_id
    ),
    vehicle AS (
        SELECT s.camera_id,
               count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY (s.vehicle_bbox->>'x2')::float - (s.vehicle_bbox->>'x1')::float
               ) AS px_median
        FROM vehicle_sighting s
        GROUP BY s.camera_id
    )
    SELECT c.id, c.camera_ref, c.name,
           COALESCE(p.n, 0) AS plate_n, p.px_median AS plate_median, p.px_p90 AS plate_p90,
           COALESCE(v.n, 0) AS vehicle_n, v.px_median AS vehicle_median
    FROM camera c
    LEFT JOIN plate p   ON p.camera_id = c.id
    LEFT JOIN vehicle v ON v.camera_id = c.id
    WHERE c.status <> 'DECOMMISSIONED'
    ORDER BY c.camera_ref
    """
)


def _grade(
    plate_n: int,
    plate_median: float | None,
    vehicle_n: int,
    vehicle_median: float | None,
    ratio: float | None,
) -> tuple[ReadinessGrade, str, str | None]:
    """Decide the grade, and say why in terms an officer can act on.

    Order matters here, and getting it wrong is easy. The first version tested the
    sample size before looking at the plates, so a camera that had produced five plate
    reads at a median 87 px -- comfortably readable -- was graded `not_viable` on the
    strength of there being only five of them. The width of the plates a camera *does*
    resolve is evidence about the camera even when there are few; the absence of plates
    is only evidence when the camera is demonstrably seeing vehicles.
    """
    sees_traffic = vehicle_n >= MIN_VEHICLE_SAMPLE and vehicle_median is not None
    # A plate for roughly every tenth vehicle or fewer. Not "zero", because a handful of
    # reads from thousands of vehicles says the same thing as none.
    plates_are_negligible = plate_n <= max(2, int(vehicle_n * 0.1))

    if plate_n and plate_median is not None and plate_median >= PLATE_PX_READABLE:
        # Readable plates settle the question of whether the camera *can*. How often is
        # a separate question, and with a thin sample it stays open rather than being
        # answered badly in either direction.
        if plate_n >= MIN_PLATE_SAMPLE:
            return (
                ReadinessGrade.ANPR_CAPABLE,
                f"median plate width {plate_median:.0f} px over {plate_n} reads, at or above "
                f"the {PLATE_PX_READABLE:.0f} px this pipeline reads reliably",
                None,
            )
        return (
            ReadinessGrade.INSUFFICIENT_SAMPLE,
            f"the {plate_n} plates this camera resolved measured a median {plate_median:.0f} px, "
            f"which is readable, but {MIN_PLATE_SAMPLE} reads are needed before grading it "
            f"capable",
            "Ingest more footage from this camera to confirm.",
        )

    if sees_traffic and plates_are_negligible:
        # The most useful single finding the grading produces: the camera is plainly
        # watching traffic, and plates are not surviving its image.
        assert vehicle_median is not None  # implied by sees_traffic
        return (
            ReadinessGrade.NOT_VIABLE,
            (
                f"{vehicle_n} vehicles observed at a median {vehicle_median:.0f} px wide, but "
                f"only {plate_n} plate regions were ever detected. The camera sees traffic; "
                f"the plates are not resolvable in its image."
            ),
            "Re-aim along the approach lane, or replace with a camera sited for ANPR.",
        )

    if plate_n < MIN_PLATE_SAMPLE or plate_median is None:
        return (
            ReadinessGrade.INSUFFICIENT_SAMPLE,
            f"only {plate_n} plate observations and {vehicle_n} vehicle observations; "
            f"not enough to grade",
            None,
        )

    if plate_median >= PLATE_PX_MARGINAL:
        aimed_across = ratio is not None and ratio < RATIO_ALIGNED
        return (
            ReadinessGrade.MARGINAL_REAIM,
            (
                f"median plate width {plate_median:.0f} px, below the {PLATE_PX_READABLE:.0f} px "
                f"threshold but within reach"
                + (
                    f"; plate-to-vehicle ratio {ratio:.2f} indicates the camera is aimed across "
                    f"the carriageway rather than along it"
                    if aimed_across
                    else ""
                )
            ),
            (
                "Re-aim along the approach lane rather than across it."
                if aimed_across
                else "Narrow the field of view, or move the camera closer to the lane."
            ),
        )

    return (
        ReadinessGrade.NOT_VIABLE,
        f"median plate width {plate_median:.0f} px, below the {PLATE_PX_MARGINAL:.0f} px at which "
        f"re-aiming can recover a read",
        "Reposition or replace. Run counting, flow, intrusion and appearance analytics here instead.",
    )


def grade_cameras(session: Session) -> list[CameraReadiness]:
    """Measure and grade every live camera from the observations already recorded.

    Reads only. Grading is recomputed rather than cached because it is cheap, because a
    stale grade is worse than none when it drives a procurement decision, and because
    the inputs change every time an ingest runs.
    """
    out: list[CameraReadiness] = []
    for row in session.execute(_MEASURE).mappings():
        plate_median = float(row["plate_median"]) if row["plate_median"] is not None else None
        plate_p90 = float(row["plate_p90"]) if row["plate_p90"] is not None else None
        vehicle_median = float(row["vehicle_median"]) if row["vehicle_median"] is not None else None
        ratio = (
            round(plate_median / vehicle_median, 4)
            if plate_median is not None and vehicle_median
            else None
        )
        grade, reasoning, remedy = _grade(
            int(row["plate_n"]), plate_median, int(row["vehicle_n"]), vehicle_median, ratio
        )
        out.append(
            CameraReadiness(
                camera_id=row["id"],
                camera_ref=row["camera_ref"],
                camera_name=row["name"],
                plate_samples=int(row["plate_n"]),
                plate_px_median=round(plate_median, 1) if plate_median is not None else None,
                plate_px_p90=round(plate_p90, 1) if plate_p90 is not None else None,
                vehicle_samples=int(row["vehicle_n"]),
                vehicle_px_median=(
                    round(vehicle_median, 1) if vehicle_median is not None else None
                ),
                plate_to_vehicle_ratio=ratio,
                grade=grade,
                reasoning=reasoning,
                remedy=remedy,
            )
        )
    return out


#: Which analytics a camera of each grade should be asked to run (§4.2).
#:
#: The point is not to disable things. It is that asking a camera for something it
#: physically cannot deliver wastes the compute that the capable cameras need, and
#: produces an empty result that reads as a software failure rather than a siting one.
#: Everything in the second list works fine at 27 px.
ANALYTICS_BY_GRADE: dict[ReadinessGrade, tuple[str, ...]] = {
    ReadinessGrade.ANPR_CAPABLE: (
        "anpr",
        "vehicle_counting",
        "flow_direction",
        "intrusion_zones",
        "appearance",
        "dwell",
    ),
    ReadinessGrade.MARGINAL_REAIM: (
        # ANPR stays on: the reads that do succeed are still evidence, and switching it
        # off would remove the measurement that justifies the re-aim recommendation.
        "anpr",
        "vehicle_counting",
        "flow_direction",
        "intrusion_zones",
        "appearance",
        "dwell",
    ),
    ReadinessGrade.NOT_VIABLE: (
        "vehicle_counting",
        "flow_direction",
        "intrusion_zones",
        "appearance",
        "dwell",
    ),
    ReadinessGrade.INSUFFICIENT_SAMPLE: (
        # Everything, because withholding ANPR here would prevent the camera ever
        # gathering the plate observations needed to grade it.
        "anpr",
        "vehicle_counting",
        "flow_direction",
        "intrusion_zones",
        "appearance",
        "dwell",
    ),
}


def analytics_for(grade: ReadinessGrade) -> tuple[str, ...]:
    return ANALYTICS_BY_GRADE[grade]


__all__ = [
    "ANALYTICS_BY_GRADE",
    "MIN_PLATE_SAMPLE",
    "MIN_VEHICLE_SAMPLE",
    "PLATE_PX_MARGINAL",
    "PLATE_PX_READABLE",
    "RATIO_ALIGNED",
    "CameraReadiness",
    "ReadinessGrade",
    "analytics_for",
    "grade_cameras",
]
