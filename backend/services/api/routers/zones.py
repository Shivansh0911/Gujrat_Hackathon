"""Intrusion zone configuration.

Defining a zone is an administrative act with operational consequences: it decides that
vehicles entering a region of one camera's view will raise alerts an officer must
triage. So it is admin-only and written to the audit ledger, on the same reasoning as
camera onboarding -- and deletion is audited *before* the row goes, because afterwards
the ledger is the only remaining record that the zone ever existed.

The polygon arrives as a list of points in the camera's frame pixels, together with the
frame size it was drawn against. See migration 0006 for why this geometry is not
geographic.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from services.api import audit
from services.api.db import get_session
from services.analytics.zones import bbox_centroid
from services.api.security import AdminActor, CurrentActor, get_camera_or_404
from services.registry.models import Camera, CameraZone

router = APIRouter(tags=["zones"])
SessionDep = Annotated[Session, Depends(get_session)]

#: Three points is the smallest thing that encloses an area. Fewer is a line, and a line
#: contains nothing, so a zone built from one would silently never fire.
MIN_POINTS = 3

#: An upper bound so a pasted GeoJSON coastline cannot become a per-detection query.
MAX_POINTS = 64


class ZoneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: Polygon vertices as [x, y] pairs in frame pixels, in order. The ring is closed
    #: for the caller -- requiring a repeated last point is a rule people get wrong.
    points: list[tuple[float, float]] = Field(min_length=MIN_POINTS, max_length=MAX_POINTS)
    reference_width: int = Field(gt=0, le=16384)
    reference_height: int = Field(gt=0, le=16384)
    active: bool = True

    @field_validator("points")
    @classmethod
    def _within_frame(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for x, y in v:
            if x < 0 or y < 0:
                raise ValueError("zone points must be non-negative frame coordinates")
        return v


class ZoneOut(BaseModel):
    id: uuid.UUID
    camera_id: uuid.UUID
    camera_ref: str
    name: str
    points: list[tuple[float, float]]
    reference_width: int
    reference_height: int
    active: bool
    created_by: str | None


def _points_of(session: Session, zone_id: uuid.UUID) -> list[tuple[float, float]]:
    """Read the ring back as points, dropping the closing duplicate vertex."""
    wkt = session.execute(
        text("SELECT ST_AsText(polygon) FROM camera_zone WHERE id = :id"),
        {"id": str(zone_id)},
    ).scalar_one()
    inner = wkt[wkt.index("((") + 2 : wkt.index("))")]
    pairs = [p.strip().split() for p in inner.split(",")]
    ring = [(float(a), float(b)) for a, b in pairs]
    return ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring


def _to_out(session: Session, zone: CameraZone, camera: Camera) -> ZoneOut:
    return ZoneOut(
        id=zone.id,
        camera_id=zone.camera_id,
        camera_ref=camera.camera_ref,
        name=zone.name,
        points=_points_of(session, zone.id),
        reference_width=zone.reference_width,
        reference_height=zone.reference_height,
        active=zone.active,
        created_by=zone.created_by,
    )


@router.get("/cameras/{camera_id}/zones", response_model=list[ZoneOut])
def list_zones(camera_id: uuid.UUID, session: SessionDep, actor: CurrentActor) -> list[ZoneOut]:
    """Zones on one camera. Readable by any signed-in operator."""
    camera = get_camera_or_404(session, actor, camera_id)
    zones = (
        session.execute(
            select(CameraZone).where(CameraZone.camera_id == camera.id).order_by(CameraZone.name)
        )
        .scalars()
        .all()
    )
    return [_to_out(session, z, camera) for z in zones]


@router.post("/cameras/{camera_id}/zones", response_model=ZoneOut, status_code=201)
def create_zone(
    camera_id: uuid.UUID, body: ZoneCreate, session: SessionDep, actor: AdminActor
) -> ZoneOut:
    """Define a zone on a camera, or replace the one with this name.

    Replacing rather than adding is deliberate: re-drawing "the gate" should move the
    gate, not leave the old polygon in place to alert alongside the new one.
    """
    camera = get_camera_or_404(session, actor, camera_id)

    ring = [*body.points, body.points[0]]
    wkt = "POLYGON((" + ", ".join(f"{x} {y}" for x, y in ring) + "))"

    existing = session.execute(
        select(CameraZone).where(
            CameraZone.camera_id == camera.id, CameraZone.name == body.name.strip()
        )
    ).scalar_one_or_none()

    # Build the geometry as a PostGIS expression and let the database parse it. The
    # first version inserted a placeholder and rewrote the column immediately after,
    # which never worked for a single request: `polygon` is NOT NULL *and* typed, so
    # the placeholder had to be valid geometry, and an empty string is not -- PostGIS
    # answers "parse error - invalid geometry" and the endpoint 500s before it reaches
    # the update. Passing the expression makes an invalid ring the database's refusal
    # rather than something stored and discovered later by a containment test.
    geometry = func.ST_SetSRID(func.ST_GeomFromText(wkt), 0)

    if existing is not None:
        zone = existing
        zone.polygon = geometry
        zone.reference_width = body.reference_width
        zone.reference_height = body.reference_height
        zone.active = body.active
        action = "UPDATE_ZONE"
    else:
        zone = CameraZone(
            camera_id=camera.id,
            name=body.name.strip(),
            polygon=geometry,
            reference_width=body.reference_width,
            reference_height=body.reference_height,
            active=body.active,
            created_by=actor.subject,
        )
        session.add(zone)
        action = "CREATE_ZONE"

    session.flush()

    audit.append(
        session,
        action=action,
        subject_type="camera_zone",
        subject_id=str(zone.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose="Intrusion zone configuration",
        detail={
            "camera_ref": camera.camera_ref,
            "zone": zone.name,
            "points": len(body.points),
            "reference_frame": [body.reference_width, body.reference_height],
            "active": body.active,
        },
    )
    session.flush()
    return _to_out(session, zone, camera)


@router.delete("/cameras/{camera_id}/zones/{zone_id}", status_code=204)
def delete_zone(
    camera_id: uuid.UUID, zone_id: uuid.UUID, session: SessionDep, actor: AdminActor
) -> None:
    """Remove a zone. Audited before the row goes."""
    camera = get_camera_or_404(session, actor, camera_id)
    zone = session.execute(
        select(CameraZone).where(CameraZone.id == zone_id, CameraZone.camera_id == camera.id)
    ).scalar_one_or_none()
    if zone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="zone not found")

    # Before the delete: afterwards there is nothing left to describe, and the ledger
    # becomes the only evidence the zone was ever configured.
    audit.append(
        session,
        action="DELETE_ZONE",
        subject_type="camera_zone",
        subject_id=str(zone.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose="Intrusion zone configuration",
        detail={"camera_ref": camera.camera_ref, "zone": zone.name},
    )
    session.delete(zone)
    session.flush()


class DetectionPoint(BaseModel):
    """Where one vehicle was, in this camera's frame."""

    x: float
    y: float
    plate: str | None
    observed_at_utc: str


@router.get("/cameras/{camera_id}/detection-points", response_model=list[DetectionPoint])
def detection_points(
    camera_id: uuid.UUID, session: SessionDep, actor: CurrentActor, limit: int = 300
) -> list[DetectionPoint]:
    """Recent vehicle-box centroids on this camera, for drawing a zone against.

    Drawing a polygon on an empty rectangle is guesswork: an operator has no way to
    know which part of the frame vehicles actually pass through, and a zone drawn over
    the sky alerts on nothing while looking perfectly reasonable. Plotting where
    detections have genuinely landed turns zone drawing into something answerable from
    evidence -- the same principle as the coverage report deriving gaps from queries
    that really ran rather than from a model.

    These are the same pixel coordinates the containment test uses, so what an operator
    draws around is exactly what will be tested against.
    """
    camera = get_camera_or_404(session, actor, camera_id)
    rows = session.execute(
        text(
            """
            SELECT vehicle_bbox, plate_normalised, observed_at_utc
            FROM detection
            WHERE camera_id = :camera_id
              AND vehicle_bbox IS NOT NULL
            ORDER BY observed_at_utc DESC
            LIMIT :limit
            """
        ),
        {"camera_id": str(camera.id), "limit": max(1, min(limit, 2000))},
    ).mappings()

    out: list[DetectionPoint] = []
    for r in rows:
        centre = bbox_centroid(r["vehicle_bbox"])
        if centre is None:
            continue
        out.append(
            DetectionPoint(
                x=round(centre[0], 1),
                y=round(centre[1], 1),
                plate=r["plate_normalised"],
                observed_at_utc=r["observed_at_utc"].isoformat(),
            )
        )
    return out
