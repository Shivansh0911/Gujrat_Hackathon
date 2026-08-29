"""Analytics beyond ANPR, derived from the detection stream ANPR already produces.

Scoped like every other read endpoint here: an operator sees their own department's
cameras, an admin sees the estate. The scoping is applied by filtering to the camera
ids the actor may see rather than by trusting the query, because an aggregate is
exactly the shape of leak that is easy to miss -- a count is still information about
a camera you are not allowed to look at.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.analytics.vehicle_counting import BUCKETS, count_by_camera, count_windows
from services.api.db import get_session
from services.api.security import CurrentActor, camera_scope

router = APIRouter(prefix="/analytics", tags=["analytics"])

SessionDep = Annotated[Session, Depends(get_session)]

#: The longest period a single request may aggregate. A dashboard asking for a year
#: at minute resolution is a mistake, not a requirement.
MAX_WINDOW = timedelta(days=31)


class VehicleCountWindow(BaseModel):
    bucket_start_utc: datetime
    reads: int
    distinct_plates: int
    cameras_reporting: int


class VehicleCountByCamera(BaseModel):
    camera_id: str
    camera_ref: str
    camera_name: str
    reads: int
    distinct_plates: int
    first_seen_utc: datetime | None
    last_seen_utc: datetime | None


class VehicleCountResult(BaseModel):
    """Counts of *identified* vehicles, with the caveat carried in the payload.

    `caveat` is part of the response rather than only the documentation because this
    number will be read off a screen and quoted. A count of plate reads is a floor on
    traffic, not a measure of it, and anything consuming this should have to see that.
    """

    since_utc: datetime
    until_utc: datetime
    bucket: str
    total_reads: int
    total_distinct_plates: int
    windows: list[VehicleCountWindow]
    by_camera: list[VehicleCountByCamera]
    caveat: str


COUNT_CAVEAT = (
    "Counts identified vehicles only. A detection is a plate read, so a vehicle whose "
    "plate could not be read does not appear at all, and one vehicle can produce "
    "several reads. distinct_plates is the closest honest proxy for vehicles; treat "
    "every figure as a floor on traffic, never an estimate of it."
)


@router.get("/vehicle-counts", response_model=VehicleCountResult)
def vehicle_counts(
    session: SessionDep,
    actor: CurrentActor,
    hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
    bucket: Annotated[str, Query(pattern="^(minute|hour|day)$")] = "hour",
    camera_id: Annotated[str | None, Query()] = None,
) -> VehicleCountResult:
    """Vehicle counts per time bucket and per camera, over the last `hours`.

    This is an independent classifier over the shared detection stream: it opens no
    camera and runs no inference of its own. The costly work happened once, in the
    ANPR pipeline, and this is what that metadata buys afterwards.
    """
    if bucket not in BUCKETS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"bucket must be one of {sorted(BUCKETS)}",
        )

    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    if until - since > MAX_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"window exceeds {MAX_WINDOW.days} days",
        )

    # Resolve the actor's visible cameras once and filter on the ids. An aggregate
    # over cameras the actor cannot see would leak their activity without ever
    # returning a row that names them.
    visible = [str(c.id) for c in session.execute(camera_scope(actor)).scalars()]
    if camera_id is not None:
        if camera_id not in visible:
            # 404 rather than 403: whether that camera exists is itself scoped.
            raise HTTPException(status_code=404, detail="camera not found")
        visible = [camera_id]

    if not visible:
        return VehicleCountResult(
            since_utc=since,
            until_utc=until,
            bucket=bucket,
            total_reads=0,
            total_distinct_plates=0,
            windows=[],
            by_camera=[],
            caveat=COUNT_CAVEAT,
        )

    windows = count_windows(session, since=since, until=until, bucket=bucket, camera_ids=visible)
    by_camera = count_by_camera(session, since=since, until=until, camera_ids=visible)

    return VehicleCountResult(
        since_utc=since,
        until_utc=until,
        bucket=bucket,
        total_reads=sum(w.reads for w in windows),
        # Summing per-bucket distinct counts would double-count a vehicle seen in two
        # buckets, so the estate total is taken across cameras instead -- still an
        # overcount if one vehicle passes two cameras, and deliberately not presented
        # as a vehicle total.
        total_distinct_plates=sum(c.distinct_plates for c in by_camera),
        windows=[
            VehicleCountWindow(
                bucket_start_utc=w.bucket_start_utc,
                reads=w.reads,
                distinct_plates=w.distinct_plates,
                cameras_reporting=w.cameras_reporting,
            )
            for w in windows
        ],
        by_camera=[
            VehicleCountByCamera(
                camera_id=c.camera_id,
                camera_ref=c.camera_ref,
                camera_name=c.camera_name,
                reads=c.reads,
                distinct_plates=c.distinct_plates,
                first_seen_utc=c.first_seen_utc,
                last_seen_utc=c.last_seen_utc,
            )
            for c in by_camera
        ],
        caveat=COUNT_CAVEAT,
    )
