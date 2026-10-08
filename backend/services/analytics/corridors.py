"""Corridor transit times: what the platform is worth on an ordinary day.

Why a corridor rather than a camera
-----------------------------------
Everything else this platform does is driven by an investigation: a plate arrives, a
route comes back. That is the scored test case and it is the right thing to be good at,
but it means the system is only useful on the days something has gone wrong.

A corridor -- an ordered sequence of cameras along a road -- turns the same detection
stream into something a traffic officer uses every shift. Median transit time between
two cameras, today against the baseline for this hour on this day of the week, and a
flag when it diverges. That is congestion detection with no new sensors, no new ingest
and no new model: the data is already there, and nobody is currently asking it this
question.

Why the median and not the mean
-------------------------------
One vehicle that stopped for lunch between two cameras adds an hour to a mean and
nothing to a median. Transit-time distributions on a road are long-tailed by
construction -- a vehicle can always take arbitrarily long and can never take less than
the free-flow time -- so the mean is dominated by behaviour that is not congestion. The
p85 is reported alongside because the tail is where a queue actually shows up before the
median moves.

Why the baseline is hour-of-week
--------------------------------
Comparing a Monday 09:00 transit against "the last 7 days" compares rush hour with
Sunday afternoon and reports congestion that is just a weekday. Hour-of-week is the
coarsest baseline that does not make that mistake. It needs several weeks of data to be
meaningful, which this deployment does not have, so `baseline_samples` is returned on
every result and a baseline computed from too little is reported as absent rather than
as a number.

What this cannot do
-------------------
A transit is a pair of sightings of the same vehicle at two cameras, so it needs
identity across cameras -- which means a plate read. On the government estate that is
rare, so a corridor built on those cameras will report very few transits. The count is
returned with every result for exactly that reason. Appearance matching is not used
here: a transit time averaged over links that are wrong two times in three (finding 25)
would be a confident number describing nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Longest plausible transit between two adjacent corridor cameras. Beyond this the
#: vehicle stopped, parked or left and came back, and the pair describes two journeys
#: rather than one leg.
MAX_TRANSIT = timedelta(minutes=45)

#: Shortest. Below it the two reads are the same moment and the pair is noise.
MIN_TRANSIT_S: float = 5.0

#: Transits needed before a median is reported rather than withheld. A median of three
#: observations is a number with the authority of a statistic and the reliability of an
#: anecdote.
MIN_TRANSITS: int = 5

#: Hour-of-week baseline samples needed before "versus normal" is claimed.
MIN_BASELINE_SAMPLES: int = 20

#: How far today's median may sit above the baseline before it is flagged.
CONGESTION_RATIO: float = 1.4

REPLAY_PREFIX = "REPLAY"


@dataclass(frozen=True)
class LegTransit:
    """Observed transit times for one ordered pair of cameras."""

    from_camera_ref: str
    to_camera_ref: str
    transits: int
    median_s: float | None
    p85_s: float | None
    distance_m: float | None

    baseline_median_s: float | None
    baseline_samples: int
    #: None when there is no baseline to compare against -- which is the usual state on
    #: a deployment this young, and is reported rather than silently treated as normal.
    ratio_to_baseline: float | None
    congested: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_camera_ref": self.from_camera_ref,
            "to_camera_ref": self.to_camera_ref,
            "transits": self.transits,
            "median_s": round(self.median_s, 1) if self.median_s is not None else None,
            "p85_s": round(self.p85_s, 1) if self.p85_s is not None else None,
            "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
            "baseline_median_s": (
                round(self.baseline_median_s, 1) if self.baseline_median_s is not None else None
            ),
            "baseline_samples": self.baseline_samples,
            "ratio_to_baseline": (
                round(self.ratio_to_baseline, 3) if self.ratio_to_baseline is not None else None
            ),
            "congested": self.congested,
        }


@dataclass(frozen=True)
class CorridorReport:
    name: str
    camera_refs: list[str]
    window_start: datetime
    window_end: datetime
    legs: list[LegTransit]
    total_transits: int
    #: Stated on every report. A corridor that reports two transits is not reporting
    #: light traffic, it is reporting that two plates were legible.
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "camera_refs": self.camera_refs,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "legs": [leg.as_dict() for leg in self.legs],
            "total_transits": self.total_transits,
            "note": self.note,
        }


_LEG = text(
    """
    WITH transit AS (
        SELECT EXTRACT(EPOCH FROM (b.observed_at_utc - a.observed_at_utc)) AS secs,
               a.observed_at_utc AS at
        FROM detection a
        JOIN detection b
          ON b.plate_normalised = a.plate_normalised
         AND b.camera_id = CAST(:to_id AS uuid)
         AND b.observed_at_utc > a.observed_at_utc
         AND b.observed_at_utc <= a.observed_at_utc + CAST(:max_transit AS interval)
        WHERE a.camera_id = CAST(:from_id AS uuid)
          AND a.observed_at_utc >= :from_ AND a.observed_at_utc <= :to
          AND EXTRACT(EPOCH FROM (b.observed_at_utc - a.observed_at_utc)) >= :min_s
    )
    SELECT count(*) AS n,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY secs) AS median_s,
           percentile_cont(0.85) WITHIN GROUP (ORDER BY secs) AS p85_s
    FROM transit
    """
)

#: The same measurement over the matching hour-of-week in the preceding weeks. Separate
#: query rather than one clever window function: this one is read far less often, and a
#: transit query that an operator waits on should not carry the cost of a baseline they
#: did not ask for.
_BASELINE = text(
    """
    WITH transit AS (
        SELECT EXTRACT(EPOCH FROM (b.observed_at_utc - a.observed_at_utc)) AS secs
        FROM detection a
        JOIN detection b
          ON b.plate_normalised = a.plate_normalised
         AND b.camera_id = CAST(:to_id AS uuid)
         AND b.observed_at_utc > a.observed_at_utc
         AND b.observed_at_utc <= a.observed_at_utc + CAST(:max_transit AS interval)
        WHERE a.camera_id = CAST(:from_id AS uuid)
          AND a.observed_at_utc >= :baseline_from
          AND a.observed_at_utc <  :from_
          AND EXTRACT(EPOCH FROM (b.observed_at_utc - a.observed_at_utc)) >= :min_s
          -- Same hour, same day of week. Comparing a Monday rush hour against a Sunday
          -- afternoon reports congestion that is just a weekday.
          AND EXTRACT(DOW  FROM a.observed_at_utc) = :dow
          AND EXTRACT(HOUR FROM a.observed_at_utc) = :hour
    )
    SELECT count(*) AS n,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY secs) AS median_s
    FROM transit
    """
)

_RESOLVE = text(
    """
    SELECT camera_ref, id, geom IS NOT NULL AS placed
    FROM camera
    WHERE camera_ref = ANY(CAST(:refs AS text[]))
    """
)

_DISTANCE = text(
    """
    SELECT ST_Distance(a.geom, b.geom)
    FROM camera a, camera b
    WHERE a.id = CAST(:from_id AS uuid) AND b.id = CAST(:to_id AS uuid)
      AND a.geom IS NOT NULL AND b.geom IS NOT NULL
    """
)


def corridor_report(
    session: Session,
    *,
    name: str,
    camera_refs: list[str],
    from_: datetime,
    to: datetime,
    baseline_weeks: int = 4,
) -> CorridorReport:
    """Transit times for each consecutive pair of cameras along a corridor.

    A corridor is an *ordered* list: the direction matters, because the transit from A
    to B and from B to A are different roads at different times of day. Reversing the
    list gives the other direction.
    """
    found = {
        row["camera_ref"]: row
        for row in session.execute(_RESOLVE, {"refs": camera_refs}).mappings()
    }
    missing = [ref for ref in camera_refs if ref not in found]
    if missing:
        raise ValueError(f"unknown cameras: {', '.join(missing)}")

    replay = [ref for ref in camera_refs if ref.startswith(REPLAY_PREFIX)]
    legs: list[LegTransit] = []

    for first, second in zip(camera_refs, camera_refs[1:], strict=False):
        from_id, to_id = found[first]["id"], found[second]["id"]
        params = {
            "from_id": str(from_id),
            "to_id": str(to_id),
            "from_": from_,
            "to": to,
            "max_transit": f"{int(MAX_TRANSIT.total_seconds())} seconds",
            "min_s": MIN_TRANSIT_S,
        }
        row = session.execute(_LEG, params).mappings().one()
        transits = int(row["n"] or 0)
        enough = transits >= MIN_TRANSITS

        base = (
            session.execute(
                _BASELINE,
                {
                    **params,
                    "baseline_from": from_ - timedelta(weeks=baseline_weeks),
                    "dow": from_.weekday() + 1 if from_.weekday() < 6 else 0,
                    "hour": from_.hour,
                },
            )
            .mappings()
            .one()
        )
        baseline_n = int(base["n"] or 0)
        baseline_median = (
            float(base["median_s"])
            if baseline_n >= MIN_BASELINE_SAMPLES and base["median_s"] is not None
            else None
        )

        median = float(row["median_s"]) if enough and row["median_s"] is not None else None
        # Written out rather than as a conditional expression: `not in (None, 0.0)`
        # reads fine and does not narrow the Optional, so mypy was right to object.
        ratio: float | None = None
        if median is not None and baseline_median is not None and baseline_median > 0.0:
            ratio = median / baseline_median

        distance = session.execute(_DISTANCE, params).scalar()

        legs.append(
            LegTransit(
                from_camera_ref=first,
                to_camera_ref=second,
                transits=transits,
                median_s=median,
                p85_s=float(row["p85_s"]) if enough and row["p85_s"] is not None else None,
                distance_m=float(distance) if distance is not None else None,
                baseline_median_s=baseline_median,
                baseline_samples=baseline_n,
                ratio_to_baseline=ratio,
                congested=ratio is not None and ratio >= CONGESTION_RATIO,
            )
        )

    total = sum(leg.transits for leg in legs)
    notes = [
        "A transit is one vehicle read at both cameras, so it needs a legible plate at "
        "each end. On cameras graded unviable for ANPR this will report very few "
        "transits, and a low count means few plates were readable rather than light "
        "traffic."
    ]
    if replay:
        notes.append(
            f"{', '.join(replay)} are harness positions replaying one recording, not "
            f"places. Transit times involving them describe the harness."
        )
    if all(leg.baseline_median_s is None for leg in legs):
        notes.append(
            "No hour-of-week baseline yet: this deployment has too little history for "
            "'versus normal' to mean anything, so no congestion flag is raised."
        )

    return CorridorReport(
        name=name,
        camera_refs=camera_refs,
        window_start=from_,
        window_end=to,
        legs=legs,
        total_transits=total,
        note=" ".join(notes),
    )


__all__ = [
    "CONGESTION_RATIO",
    "MAX_TRANSIT",
    "MIN_BASELINE_SAMPLES",
    "MIN_TRANSITS",
    "MIN_TRANSIT_S",
    "CorridorReport",
    "LegTransit",
    "corridor_report",
]
