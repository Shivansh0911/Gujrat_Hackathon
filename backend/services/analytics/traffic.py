"""Traffic counted from vehicles seen, not from registrations read.

`vehicle_counting.py` counts plate reads, and its own docstring is blunt about what
that costs: a vehicle whose plate the recogniser could not resolve produces no row at
all, and on this estate that is most of them. Every figure it reports is a floor on
traffic rather than a measurement of it.

That limitation is a property of the *input*, not of the idea. Phase 2 records a
`vehicle_sighting` for every vehicle a camera sees, whether or not its plate was ever
legible, which is the row this module counts instead. The difference is not cosmetic:
on the same footage the plate pipeline resolved five registrations where the vehicle
detector recorded two hundred and thirty-five passes.

So this is the honest traffic number, and it is the one a control room actually wants:
how many vehicles went past, what mix of classes, how that varies by hour and by
camera.

What a row means here
---------------------
One row is **one vehicle pass** -- a track that entered the frame, was followed, and
left -- not one frame and not one detection. `SightingPipeline` closes a track before
it writes, so a car in view for two seconds at 5 fps is one row, not ten.

What it still cannot tell you
-----------------------------
It counts passes, not distinct vehicles. The same lorry going back and forth past one
camera four times is four passes, correctly, because that is four events on that road
-- but it is one lorry, and nothing here can tell those cases apart without identity.
A pass at two cameras is two passes for the same reason. The caveat says so in the
response body rather than in a tooltip, because a number that travels into a
screenshot needs its caveat to travel with it.

It also counts only what the detector saw. A camera too dark or too oblique to resolve
a vehicle shape contributes nothing, and `readiness.py` is where that is reported per
camera rather than being absorbed into this total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Accepted bucket widths, mapped to the `date_trunc` unit that produces them.
BUCKETS: dict[str, str] = {"minute": "minute", "hour": "hour", "day": "day"}

CAVEAT = (
    "Counts vehicle passes seen by the detector, not registrations read -- a vehicle "
    "whose plate was never legible is counted here and is invisible to "
    "/analytics/vehicle-counts. One row is one pass of one camera, so a vehicle that "
    "passes two cameras counts twice and one that returns counts again. These are "
    "traffic events, not distinct vehicles; only a plate read or an appearance link "
    "can tell those apart."
)


@dataclass
class ClassMix:
    car: int = 0
    motorcycle: int = 0
    bus: int = 0
    truck: int = 0
    unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "car": self.car,
            "motorcycle": self.motorcycle,
            "bus": self.bus,
            "truck": self.truck,
            "unknown": self.unknown,
        }


@dataclass
class TrafficWindow:
    bucket_start_utc: datetime
    vehicles: int
    cameras_reporting: int


@dataclass
class CameraTraffic:
    camera_id: str
    camera_ref: str
    camera_name: str
    vehicles: int
    #: Mean passes per hour over the hours this camera actually reported, not over the
    #: whole requested window. A camera watched for one hour out of twenty-four is not
    #: quiet; it is barely watched, and dividing by 24 would report it as the former.
    per_hour: float
    classes: dict[str, int]
    #: Mean fraction of frame area a vehicle occupied. A proxy for how close traffic
    #: passes, and the quantity `readiness.py` grades plate legibility from.
    mean_rel_area: float | None


@dataclass
class TrafficReport:
    from_utc: datetime
    to_utc: datetime
    bucket: str
    total_vehicles: int
    cameras_reporting: int
    #: Mean passes per hour across the window, over cameras that reported at all.
    mean_per_hour: float
    #: The busiest single bucket, which is what a shift supervisor actually asks for.
    peak_bucket_start_utc: datetime | None
    peak_vehicles: int
    classes: dict[str, int]
    windows: list[TrafficWindow] = field(default_factory=list)
    by_camera: list[CameraTraffic] = field(default_factory=list)
    caveat: str = CAVEAT

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_utc": self.from_utc.isoformat(),
            "to_utc": self.to_utc.isoformat(),
            "bucket": self.bucket,
            "total_vehicles": self.total_vehicles,
            "cameras_reporting": self.cameras_reporting,
            "mean_per_hour": self.mean_per_hour,
            "peak_bucket_start_utc": (
                self.peak_bucket_start_utc.isoformat() if self.peak_bucket_start_utc else None
            ),
            "peak_vehicles": self.peak_vehicles,
            "classes": self.classes,
            "windows": [
                {
                    "bucket_start_utc": w.bucket_start_utc.isoformat(),
                    "vehicles": w.vehicles,
                    "cameras_reporting": w.cameras_reporting,
                }
                for w in self.windows
            ],
            "by_camera": [
                {
                    "camera_id": c.camera_id,
                    "camera_ref": c.camera_ref,
                    "camera_name": c.camera_name,
                    "vehicles": c.vehicles,
                    "per_hour": c.per_hour,
                    "classes": c.classes,
                    "mean_rel_area": c.mean_rel_area,
                }
                for c in self.by_camera
            ],
            "caveat": self.caveat,
        }


_WINDOWS = text(
    """
    SELECT date_trunc(:unit, s.observed_at_utc) AS bucket_start,
           count(*) AS vehicles,
           count(DISTINCT s.camera_id) AS cameras
    FROM vehicle_sighting s
    WHERE s.observed_at_utc >= :from_ AND s.observed_at_utc < :to
      AND s.camera_id = ANY(CAST(:camera_ids AS uuid[]))
    GROUP BY 1
    ORDER BY 1
    """
)

_BY_CAMERA = text(
    """
    SELECT c.id,
           c.camera_ref,
           c.name,
           count(*) AS vehicles,
           -- Compared lower-cased. `VehicleClass` is a StrEnum whose members are
           -- lower case, and the column stores the member value, so matching on
           -- 'CAR' silently classified all 489 passes as unknown -- a breakdown that
           -- is entirely one bucket looks like a detector failure rather than a
           -- string mismatch. Folding the case means it cannot break again if the
           -- enum's spelling ever changes.
           count(*) FILTER (WHERE lower(s.vehicle_class) = 'car')        AS car,
           count(*) FILTER (WHERE lower(s.vehicle_class) = 'motorcycle') AS motorcycle,
           count(*) FILTER (WHERE lower(s.vehicle_class) = 'bus')        AS bus,
           count(*) FILTER (WHERE lower(s.vehicle_class) = 'truck')      AS truck,
           count(*) FILTER (
               WHERE s.vehicle_class IS NULL
                  OR lower(s.vehicle_class) NOT IN ('car', 'motorcycle', 'bus', 'truck')
           ) AS unknown,
           avg(s.rel_area) AS mean_rel_area,
           -- Hours this camera actually produced sightings in, so the per-hour mean
           -- describes the period it was watched rather than the period we asked about.
           count(DISTINCT date_trunc('hour', s.observed_at_utc)) AS active_hours
    FROM vehicle_sighting s
    JOIN camera c ON c.id = s.camera_id
    WHERE s.observed_at_utc >= :from_ AND s.observed_at_utc < :to
      AND s.camera_id = ANY(CAST(:camera_ids AS uuid[]))
    GROUP BY c.id, c.camera_ref, c.name
    ORDER BY vehicles DESC
    """
)


def traffic_report(
    session: Session,
    *,
    from_: datetime,
    to: datetime,
    bucket: str,
    camera_ids: list[Any],
) -> TrafficReport:
    """Count vehicle passes over a window, bucketed and broken down by camera.

    `camera_ids` is resolved by the caller from the actor's scope. An aggregate over
    cameras the actor cannot see would leak their activity without ever naming them,
    which is the quiet way a tenancy boundary fails.
    """
    unit = BUCKETS[bucket]
    ids = [str(cid) for cid in camera_ids]
    params = {"unit": unit, "from_": from_, "to": to, "camera_ids": ids}

    if not ids:
        return TrafficReport(
            from_utc=from_,
            to_utc=to,
            bucket=bucket,
            total_vehicles=0,
            cameras_reporting=0,
            mean_per_hour=0.0,
            peak_bucket_start_utc=None,
            peak_vehicles=0,
            classes=ClassMix().as_dict(),
        )

    windows = [
        TrafficWindow(
            bucket_start_utc=row.bucket_start,
            vehicles=int(row.vehicles),
            cameras_reporting=int(row.cameras),
        )
        for row in session.execute(_WINDOWS, params)
    ]

    per_camera: list[CameraTraffic] = []
    mix = ClassMix()
    for row in session.execute(_BY_CAMERA, params):
        classes = {
            "car": int(row.car),
            "motorcycle": int(row.motorcycle),
            "bus": int(row.bus),
            "truck": int(row.truck),
            "unknown": int(row.unknown),
        }
        mix.car += classes["car"]
        mix.motorcycle += classes["motorcycle"]
        mix.bus += classes["bus"]
        mix.truck += classes["truck"]
        mix.unknown += classes["unknown"]
        active = max(int(row.active_hours), 1)
        per_camera.append(
            CameraTraffic(
                camera_id=str(row.id),
                camera_ref=row.camera_ref,
                camera_name=row.name,
                vehicles=int(row.vehicles),
                per_hour=round(int(row.vehicles) / active, 2),
                classes=classes,
                mean_rel_area=(
                    round(float(row.mean_rel_area), 5) if row.mean_rel_area is not None else None
                ),
            )
        )

    total = sum(w.vehicles for w in windows)
    peak = max(windows, key=lambda w: w.vehicles, default=None)
    # Averaged over the hours any camera reported, for the same reason the per-camera
    # figure is: an estate watched for one hour has not had a quiet day.
    active_hours = max(len({w.bucket_start_utc.replace(minute=0, second=0) for w in windows}), 1)

    return TrafficReport(
        from_utc=from_,
        to_utc=to,
        bucket=bucket,
        total_vehicles=total,
        cameras_reporting=len(per_camera),
        mean_per_hour=round(total / active_hours, 2),
        peak_bucket_start_utc=peak.bucket_start_utc if peak else None,
        peak_vehicles=peak.vehicles if peak else 0,
        classes=mix.as_dict(),
        windows=windows,
        by_camera=per_camera,
    )


__all__ = ["BUCKETS", "CAVEAT", "CameraTraffic", "TrafficReport", "TrafficWindow", "traffic_report"]
