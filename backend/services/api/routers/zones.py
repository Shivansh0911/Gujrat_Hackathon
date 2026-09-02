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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.api import audit
from services.api.db import get_session
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

    if existing is not None:
        zone = existing
        zone.reference_width = body.reference_width
        zone.reference_height = body.reference_height
        zone.active = body.active
        action = "UPDATE_ZONE"
    else:
        zone = CameraZone(
            camera_id=camera.id,
            name=body.name.strip(),
            polygon=text("''"),  # replaced below; the column is NOT NULL
            reference_width=body.reference_width,
            reference_height=body.reference_height,
            active=body.active,
            created_by=actor.subject,
        )
        session.add(zone)
        action = "CREATE_ZONE"

    session.flush()
    # Set the geometry through PostGIS rather than binding a WKT string to the column,
    # so an invalid ring is refused by the database instead of stored and failing every
    # containment test later.
    session.execute(
        text(
            "UPDATE camera_zone SET polygon = ST_SetSRID(ST_GeomFromText(:wkt), 0) WHERE id = :id"
        ),
        {"wkt": wkt, "id": str(zone.id)},
    )

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
