"""Camera registry endpoints — the Model 1 control plane surface."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.media_signing import signed_media_url
from services.api.db import get_session
from services.api.schemas import (
    CameraOut,
    GeomPatch,
    StreamUrlOut,
    SyncResult,
)
from services.api.security import AdminActor, CurrentActor, camera_scope, get_camera_or_404
from services.common.catalogue import fetch_catalogue
from services.common.config import get_settings as get_feed_settings
from services.registry.enums import CameraStatus, GeomSource, SourceType, assert_transition
from services.registry.enums import IllegalTransition
from services.registry.models import Camera, Department

router = APIRouter(prefix="/cameras", tags=["cameras"])

SessionDep = Annotated[Session, Depends(get_session)]


def _to_out(session: Session, camera: Camera) -> CameraOut:
    """Project a Camera row, extracting lat/lon from PostGIS geography."""
    lat = lon = None
    if camera.geom is not None:
        # ST_X/ST_Y are geometry functions; a geography column must be cast first.
        row = session.execute(
            text(
                "SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
                "FROM camera WHERE id = :cid"
            ),
            {"cid": camera.id},
        ).one()
        lat, lon = float(row.lat), float(row.lon)

    dept_code = session.execute(
        select(Department.code).where(Department.id == camera.department_id)
    ).scalar_one_or_none()

    return CameraOut(
        id=camera.id,
        camera_ref=camera.camera_ref,
        name=camera.name,
        location_text=camera.location_text,
        department_id=camera.department_id,
        department_code=dept_code,
        lat=lat,
        lon=lon,
        geom_source=camera.geom_source,
        confidence_radius_m=camera.confidence_radius_m,
        resolved_by=camera.resolved_by,
        resolved_at=camera.resolved_at,
        coordinate_missing=camera.geom_source == GeomSource.UNSET.value,
        status=camera.status,
        codec=camera.codec,
        resolution_w=camera.resolution_w,
        resolution_h=camera.resolution_h,
        declared_fps=camera.declared_fps,
        measured_fps=camera.measured_fps,
        transport=camera.transport,
        source_type=camera.source_type,
        last_seen_at=camera.last_seen_at,
    )


@router.get("", response_model=list[CameraOut])
def list_cameras(
    session: SessionDep,
    actor: CurrentActor,
    department: str | None = Query(default=None, description="department code"),
    status_filter: str | None = Query(default=None, alias="status"),
    has_geom: bool | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[CameraOut]:
    """List cameras within the caller's scope.

    `has_geom=false` is how the console finds `coordinate missing` cameras: they are
    filtered, never dropped, so an operator can see and fix them.
    """
    stmt = camera_scope(actor)

    if department:
        stmt = stmt.join(Department, Camera.department_id == Department.id).where(
            Department.code == department.upper()
        )
    if status_filter:
        stmt = stmt.where(Camera.status == status_filter.upper())
    if has_geom is not None:
        stmt = (
            stmt.where(Camera.geom_source != GeomSource.UNSET.value)
            if has_geom
            else stmt.where(Camera.geom_source == GeomSource.UNSET.value)
        )

    cameras = session.execute(stmt.order_by(Camera.camera_ref).limit(limit)).scalars().all()
    return [_to_out(session, c) for c in cameras]


@router.get("/{camera_id}", response_model=CameraOut)
def get_camera(camera_id: uuid.UUID, session: SessionDep, actor: CurrentActor) -> CameraOut:
    return _to_out(session, get_camera_or_404(session, actor, camera_id))


@router.patch("/{camera_id}/geom", response_model=CameraOut)
def patch_camera_geom(
    camera_id: uuid.UUID, patch: GeomPatch, session: SessionDep, actor: CurrentActor
) -> CameraOut:
    """Operator pin-drop. Records manual_survey provenance and an audit entry.

    This is what lets a wrong coordinate be corrected in seconds without a redeploy,
    and the audit entry is what makes the correction itself evidence.
    """
    camera = get_camera_or_404(session, actor, camera_id)

    before = {
        "geom_source": camera.geom_source,
        "confidence_radius_m": camera.confidence_radius_m,
        "resolved_by": camera.resolved_by,
    }

    camera.geom = func.ST_SetSRID(func.ST_MakePoint(patch.lon, patch.lat), 4326)
    camera.geom_source = GeomSource.MANUAL_SURVEY.value
    camera.confidence_radius_m = patch.confidence_radius_m
    camera.resolved_by = f"pin_editor:{actor.subject}"
    camera.resolved_at = datetime.now(timezone.utc)

    audit.append(
        session,
        action="CAMERA_GEOM_UPDATED",
        subject_type="camera",
        subject_id=str(camera.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "camera_ref": camera.camera_ref,
            "before": before,
            "after": {
                "lat": patch.lat,
                "lon": patch.lon,
                "geom_source": GeomSource.MANUAL_SURVEY.value,
                "confidence_radius_m": patch.confidence_radius_m,
            },
            "note": patch.note,
        },
    )
    session.flush()
    session.refresh(camera)
    return _to_out(session, camera)


@router.post("/sync-catalogue", response_model=SyncResult)
def sync_catalogue(session: SessionDep, actor: AdminActor) -> SyncResult:
    """Diff the gateway catalogue into the registry.

    A camera that disappears transitions to UNREACHABLE and is never deleted:
    detections reference cameras, and deleting one would orphan evidence. Camera ids
    and the camera set can change (§2.1), so the catalogue is the contract and the
    URL pattern is not.
    """
    from services.api.events import CameraEvent, event_bus

    feed_settings = get_feed_settings()
    try:
        descriptors = fetch_catalogue(feed_settings)
    except Exception as exc:  # noqa: BLE001 - third-party infrastructure
        # The catalogue being down is an operational fact, not a server error. The
        # registry keeps its existing rows rather than concluding every camera is gone.
        return SyncResult(
            catalogue_reachable=False,
            cameras_in_catalogue=0,
            note=f"catalogue unreachable, registry unchanged: {type(exc).__name__}",
        )

    existing = {
        c.camera_ref: c
        for c in session.execute(
            select(Camera).where(Camera.source_type == SourceType.GATEWAY.value)
        ).scalars()
    }
    seen: set[str] = set()
    added: list[str] = []
    changed: list[str] = []
    unchanged = 0

    default_dept = session.execute(
        select(Department).where(Department.code == "HOME")
    ).scalar_one_or_none()
    if default_dept is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="departments are not seeded; run `make seed` first",
        )

    for desc in descriptors:
        seen.add(desc.external_id)
        camera = existing.get(desc.external_id)
        if camera is None:
            camera = Camera(
                camera_ref=desc.external_id,
                name=desc.name,
                location_text=desc.location_text,
                department_id=default_dept.id,
                source_type=SourceType.GATEWAY.value,
                status=CameraStatus.DRAFT.value,
                codec=desc.declared_codec,
                resolution_w=desc.declared_width,
                resolution_h=desc.declared_height,
                declared_fps=desc.declared_fps,
            )
            session.add(camera)
            session.flush()
            added.append(desc.external_id)
            event_bus.publish(CameraEvent.ADDED, {"camera_ref": desc.external_id})
            audit.append(
                session,
                action="CAMERA_ADDED",
                subject_type="camera",
                subject_id=str(camera.id),
                actor_id=actor.subject,
                actor_role=actor.role,
                detail={"camera_ref": desc.external_id, "source": "catalogue_sync"},
            )
            continue

        diff: dict[str, list[object]] = {}
        for field, new in (
            ("location_text", desc.location_text),
            ("codec", desc.declared_codec),
            ("resolution_w", desc.declared_width),
            ("resolution_h", desc.declared_height),
            ("declared_fps", desc.declared_fps),
        ):
            old = getattr(camera, field)
            # The catalogue reports zeros for unknown properties, normalised to None
            # upstream. Do not overwrite a measured value with an absent declaration.
            if new is not None and old != new:
                diff[field] = [old, new]
                setattr(camera, field, new)

        if diff:
            changed.append(desc.external_id)
            event_bus.publish(
                CameraEvent.PROPERTIES_CHANGED, {"camera_ref": desc.external_id, "diff": diff}
            )
            audit.append(
                session,
                action="CAMERA_PROPERTIES_CHANGED",
                subject_type="camera",
                subject_id=str(camera.id),
                actor_id=actor.subject,
                actor_role=actor.role,
                detail={"camera_ref": desc.external_id, "diff": diff},
            )
        else:
            unchanged += 1

    removed: list[str] = []
    for ref, camera in existing.items():
        if ref in seen:
            continue
        try:
            assert_transition(CameraStatus(camera.status), CameraStatus.UNREACHABLE)
        except IllegalTransition:
            # e.g. already DECOMMISSIONED. Absence from the catalogue does not revive
            # or re-retire a camera; the lifecycle rules win.
            continue
        camera.status = CameraStatus.UNREACHABLE.value
        removed.append(ref)
        event_bus.publish(CameraEvent.REMOVED, {"camera_ref": ref})
        audit.append(
            session,
            action="CAMERA_REMOVED",
            subject_type="camera",
            subject_id=str(camera.id),
            actor_id=actor.subject,
            actor_role=actor.role,
            detail={
                "camera_ref": ref,
                "new_status": CameraStatus.UNREACHABLE.value,
                "note": "absent from catalogue; row retained, evidence references it",
            },
        )

    return SyncResult(
        catalogue_reachable=True,
        cameras_in_catalogue=len(descriptors),
        added=added,
        removed=removed,
        properties_changed=changed,
        unchanged=unchanged,
    )


@router.get("/{camera_id}/stream-url", response_model=StreamUrlOut)
def get_stream_url(
    camera_id: uuid.UUID,
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
) -> StreamUrlOut:
    """Playback URL for the console.

    The console asks for a camera by id; it never holds an upstream address of its
    own. Viewing is audited because watching live video of a public place is an act
    that should be attributable.
    """
    camera = get_camera_or_404(session, actor, camera_id)
    feed_settings = get_feed_settings()

    if camera.source_type == SourceType.GATEWAY.value:
        url = feed_settings.hls_url(camera.camera_ref)
        transport = "hls"
    elif camera.source_type == SourceType.FILE.value:
        # Own-feed (replay) cameras are backed by a bundled clip, not a gateway
        # stream. This used to return `/media/own-feed/<ref>/index.m3u8`, which
        # nothing served -- previewing a replay camera showed a player error. The
        # clip is an MP4, so serve it as one, signed like any other evidence media.
        from services.common.paths import OWN_FEED_DIR

        clip = next(
            (p for p in sorted(OWN_FEED_DIR.glob("*")) if p.name == "demo_clip.mp4"),
            None,
        ) or next(
            (p for p in sorted(OWN_FEED_DIR.glob("*")) if p.suffix.lower() == ".mp4"),
            None,
        )
        if clip is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no own-feed clip is bundled with this deployment",
            )
        url = signed_media_url("/media/own-feed", clip.name, settings.jwt_secret)
        transport = "file"
    else:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"no playback path for source type {camera.source_type}",
        )

    audit.append(
        session,
        action="VIEW_STREAM",
        subject_type="camera",
        subject_id=str(camera.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={"camera_ref": camera.camera_ref, "transport": transport},
    )
    return StreamUrlOut(
        camera_id=camera.id, camera_ref=camera.camera_ref, transport=transport, url=url
    )
