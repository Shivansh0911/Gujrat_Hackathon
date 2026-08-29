"""Vehicle counting per camera per time window.

An independent classifier over the detection stream the ANPR pipeline already
produces. It adds no ingest path, opens no camera, and does not touch `anpr.py`: it
is a query, which is the whole point -- the expensive work of decoding frames and
reading plates has already happened, and counting is what you get for free once the
metadata exists.

What this counts, precisely
---------------------------
The temptation is to report "N vehicles crossed this camera", and that would be
wrong in two directions at once:

* **Upwards.** A detection is a *plate read*, not a vehicle. One car in view for two
  seconds at a 5 fps analytic rate can produce several rows. Counting rows would
  inflate a quiet junction into a busy one.
* **Downwards.** A vehicle whose plate the recogniser could not read produces no row
  at all. On this footage that is most of them -- 9,158 frames across 25 government
  cameras yielded 30 plate regions. Any count here is a count of *identified*
  vehicles, and is a floor on traffic, never an estimate of it.

So three numbers are reported and named for what they are: `reads` (rows),
`distinct_plates` (unique registrations, the closest honest proxy for vehicles), and
`cameras_reporting`. A caller that wants "traffic volume" is asking a question this
data cannot answer, and the field names are chosen so that is obvious rather than
inferred.

No new persistence
------------------
There is no table and no migration behind this. Everything here is derived from rows
that already exist, and storing a rollup would create a second copy of the truth that
can disagree with the first -- after a re-ingest, a corrected read, or a deleted
camera. The counts are cheap enough to compute on demand at this estate size, and
`detection` is already a TimescaleDB hypertable partitioned on exactly the column
these queries group by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.registry.models import Camera, Detection

#: Buckets a caller may ask for. Anything finer than a minute is noise at a 5 fps
#: analytic rate; anything coarser than a day is a report, not a dashboard.
BUCKETS: dict[str, timedelta] = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}


@dataclass(frozen=True)
class WindowCount:
    """Counts for one bucket. `distinct_plates` is the honest vehicle proxy."""

    bucket_start_utc: datetime
    reads: int
    distinct_plates: int
    cameras_reporting: int


def count_windows(
    session: Session,
    *,
    since: datetime,
    until: datetime,
    bucket: str = "hour",
    camera_ids: list[str] | None = None,
) -> list[WindowCount]:
    """Counts per bucket between `since` and `until`.

    Buckets are produced by `date_trunc` on the observation time, not on ingest time.
    A detection's `observed_at_utc` is derived from stream PTS; ingest time is when it
    happened to reach us, which on a reconnecting feed can be minutes later and would
    move a vehicle into the wrong hour.

    Empty buckets are not fabricated. A gap in the result is a period in which nothing
    was identified, which is a different statement from "zero vehicles passed", and
    the caller is left to render that distinction rather than having it flattened here.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(BUCKETS)}, not {bucket!r}")

    trunc = func.date_trunc(bucket, Detection.observed_at_utc)
    stmt = (
        select(
            trunc.label("bucket_start"),
            func.count().label("reads"),
            func.count(func.distinct(Detection.plate_normalised)).label("distinct_plates"),
            func.count(func.distinct(Detection.camera_id)).label("cameras_reporting"),
        )
        .where(Detection.observed_at_utc >= since)
        .where(Detection.observed_at_utc < until)
        .group_by(trunc)
        .order_by(trunc)
    )
    if camera_ids:
        stmt = stmt.where(Detection.camera_id.in_(camera_ids))

    return [
        WindowCount(
            bucket_start_utc=row.bucket_start,
            reads=int(row.reads),
            distinct_plates=int(row.distinct_plates),
            cameras_reporting=int(row.cameras_reporting),
        )
        for row in session.execute(stmt)
    ]


@dataclass(frozen=True)
class CameraCount:
    """Totals for one camera over the whole requested period."""

    camera_id: str
    camera_ref: str
    camera_name: str
    reads: int
    distinct_plates: int
    first_seen_utc: datetime | None
    last_seen_utc: datetime | None


def count_by_camera(
    session: Session,
    *,
    since: datetime,
    until: datetime,
    camera_ids: list[str] | None = None,
    limit: int = 50,
) -> list[CameraCount]:
    """Per-camera totals, busiest first.

    Joined to `camera` rather than returning bare ids, because a count attributed to a
    UUID is not something an operator can act on. Cameras with no detections in the
    period are absent rather than present with a zero: the registry already knows which
    cameras exist, and the Health screen is where "reporting nothing" belongs.
    """
    stmt = (
        select(
            Detection.camera_id.label("camera_id"),
            Camera.camera_ref.label("camera_ref"),
            Camera.name.label("camera_name"),
            func.count().label("reads"),
            func.count(func.distinct(Detection.plate_normalised)).label("distinct_plates"),
            func.min(Detection.observed_at_utc).label("first_seen"),
            func.max(Detection.observed_at_utc).label("last_seen"),
        )
        .join(Camera, Camera.id == Detection.camera_id)
        .where(Detection.observed_at_utc >= since)
        .where(Detection.observed_at_utc < until)
        .group_by(Detection.camera_id, Camera.camera_ref, Camera.name)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if camera_ids:
        stmt = stmt.where(Detection.camera_id.in_(camera_ids))

    return [
        CameraCount(
            camera_id=str(row.camera_id),
            camera_ref=row.camera_ref,
            camera_name=row.camera_name,
            reads=int(row.reads),
            distinct_plates=int(row.distinct_plates),
            first_seen_utc=row.first_seen,
            last_seen_utc=row.last_seen,
        )
        for row in session.execute(stmt)
    ]


__all__ = ["BUCKETS", "WindowCount", "CameraCount", "count_windows", "count_by_camera"]
