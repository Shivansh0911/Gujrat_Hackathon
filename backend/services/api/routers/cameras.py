"""Camera registry endpoints — the Model 1 control plane surface."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.media_signing import signed_media_url
from services.api.db import get_session
from services.api.schemas import (
    BulkImportRejection,
    BulkImportResult,
    CameraCreate,
    CameraOut,
    GeomPatch,
    StreamUrlOut,
    SyncResult,
)
from services.api.security import AdminActor, CurrentActor, camera_scope, get_camera_or_404
from services.common.catalogue import fetch_catalogue
from services.common.config import Settings as FeedSettings
from services.common.config import get_settings as get_feed_settings
from services.registry.camera_import import validate_row
from services.registry.enums import CameraStatus, GeomSource, SourceType, assert_transition
from services.registry.enums import IllegalTransition
from services.registry.models import Camera, Department, Detection

#: Bulk import is an operator pasting a departmental spreadsheet, not a data feed.
#: The cap keeps a mistaken upload from becoming a memory event; the real seed file
#: for this estate is a few kilobytes.
MAX_IMPORT_BYTES = 2 * 1024 * 1024

router = APIRouter(prefix="/cameras", tags=["cameras"])

SessionDep = Annotated[Session, Depends(get_session)]


def _feed_settings_or_none() -> "FeedSettings | None":
    """The gateway configuration, or None when it is not configured.

    `SETU_GATEWAY_HOST` has no default: a platform deployment that forgets it should
    not silently fall back to some other team's gateway. But the resulting pydantic
    ValidationError surfaced as a bare HTTP 500, which told the operator nothing --
    the console showed "Load failed" and the cause was an unset environment variable
    on the API, three layers away from anything visible.

    Returning None lets each caller answer usefully instead: this instance has no feed
    configured, which is a different statement from "the feed is down" and a different
    one again from "the server broke".
    """
    try:
        return get_feed_settings()
    except Exception:  # noqa: BLE001 -- any config failure means "not configured"
        return None


def _detection_counts(session: Session, camera_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Detections per camera, in one query rather than one per camera."""
    if not camera_ids:
        return {}
    rows = session.execute(
        select(Detection.camera_id, func.count(Detection.id))
        .where(Detection.camera_id.in_(camera_ids))
        .group_by(Detection.camera_id)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


def _to_out(session: Session, camera: Camera, detection_count: int | None = None) -> CameraOut:
    """Project a Camera row, extracting lat/lon from PostGIS geography.

    `detection_count` is passed in by list endpoints that already counted every camera
    in one grouped query; a single-camera read counts its own.
    """
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
        detection_count=(
            detection_count
            if detection_count is not None
            else _detection_counts(session, [camera.id]).get(camera.id, 0)
        ),
    )


@router.post("", response_model=CameraOut, status_code=201)
def create_camera(body: CameraCreate, session: SessionDep, actor: AdminActor) -> CameraOut:
    """Onboard a single camera by hand.

    Model 1 asks for manual *and* bulk onboarding to be demonstrable. Bulk import
    handles a departmental spreadsheet; this handles the one camera someone is
    standing in front of, and the two share their coordinate rules rather than
    drifting apart.

    Rejects a duplicate `camera_ref` with 409 rather than silently updating: a
    registry that quietly overwrites an existing camera because two people chose the
    same reference is how one camera's evidence ends up filed under another's.

    Created as DRAFT, never ACTIVE. Nothing has been probed yet, and a camera that
    claims to be active before anything has connected to it is a claim the registry
    cannot support.
    """
    ref = body.camera_ref.strip()

    existing = session.execute(select(Camera).where(Camera.camera_ref == ref)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"camera_ref '{ref}' already exists. Use the map's pin-drop to correct "
                "its position, or choose a different reference."
            ),
        )

    # Coordinates are all-or-nothing. A latitude with no longitude is a typo, and
    # storing half a position would place the camera on the prime meridian.
    if (body.lat is None) != (body.lon is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="supply both lat and lon, or neither",
        )
    if body.lat is not None and body.confidence_radius_m is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "confidence_radius_m is required with coordinates. A position with no "
                "stated uncertainty reads as survey-grade."
            ),
        )

    department = None
    if body.department_code:
        department = session.execute(
            select(Department).where(Department.code == body.department_code.upper())
        ).scalar_one_or_none()
        if department is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown department code '{body.department_code}'",
            )
    if department is None:
        department = session.execute(select(Department).order_by(Department.code)).scalars().first()
    if department is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no departments exist in the registry; run the department seed first",
        )

    camera = Camera(
        camera_ref=ref,
        name=body.name.strip(),
        location_text=(body.location_text or "").strip(),
        department_id=department.id,
        source_type=SourceType.GATEWAY.value,
        status=CameraStatus.DRAFT.value,
        geom_source=GeomSource.UNSET.value,
    )

    if body.lat is not None and body.lon is not None:
        camera.geom = func.ST_SetSRID(func.ST_MakePoint(body.lon, body.lat), 4326)
        # manual_survey, because a person supplied it. That provenance is what the map
        # renders as a precise pin rather than an uncertainty circle.
        camera.geom_source = GeomSource.MANUAL_SURVEY.value
        camera.confidence_radius_m = body.confidence_radius_m
        camera.resolved_by = f"manual entry by {actor.subject}"
        camera.resolved_at = datetime.now(timezone.utc)

    session.add(camera)
    session.flush()

    audit.append(
        session,
        action="CAMERA_CREATED",
        subject_type="camera",
        subject_id=str(camera.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "camera_ref": camera.camera_ref,
            "name": camera.name,
            "department": department.code,
            "geom_source": camera.geom_source,
            "note": body.note,
        },
    )
    session.flush()

    return _to_out(session, camera, 0)


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
    counts = _detection_counts(session, [c.id for c in cameras])
    return [_to_out(session, c, counts.get(c.id, 0)) for c in cameras]


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

    feed_settings = _feed_settings_or_none()
    if feed_settings is None:
        return SyncResult(
            catalogue_reachable=False,
            cameras_in_catalogue=0,
            note=(
                "no camera gateway is configured on this deployment: set "
                "SETU_GATEWAY_HOST (and SETU_GATEWAY_SCHEME if not https) and redeploy. "
                "The registry is unchanged."
            ),
        )

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

        # Absence means two different things depending on where the camera got to.
        #
        # A camera that was working and has gone is UNREACHABLE -- it may come back, and
        # the lifecycle allows it to. A camera still in DRAFT was never onboarded at all,
        # so "unreachable" is the wrong word for it: it is not a working camera that
        # stopped, it is a candidate that no longer exists. DECOMMISSIONED is the right
        # terminal state and, unlike UNREACHABLE, is a legal move from DRAFT.
        #
        # This mattered the moment the estate renamed every camera: thirty DRAFT rows
        # for cameras that had ceased to exist failed the UNREACHABLE transition, hit
        # the `continue` below, and stayed DRAFT indefinitely -- so the registry went on
        # claiming an estate twice the size of the real one, silently, because the only
        # signal was an exception being swallowed.
        current = CameraStatus(camera.status)
        target = (
            CameraStatus.DECOMMISSIONED
            if current is CameraStatus.DRAFT
            else CameraStatus.UNREACHABLE
        )
        try:
            assert_transition(current, target)
        except IllegalTransition:
            # e.g. already DECOMMISSIONED. Absence from the catalogue does not revive
            # or re-retire a camera; the lifecycle rules win.
            continue
        camera.status = target.value
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
                "new_status": target.value,
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


@router.post("/bulk-import", response_model=BulkImportResult)
async def bulk_import_cameras(
    session: SessionDep,
    actor: AdminActor,
    file: Annotated[UploadFile, File(description="CSV with the camera_geo.csv columns")],
) -> BulkImportResult:
    """Onboard many cameras from a CSV, row by row.

    Model 1 requires bulk onboarding as a platform capability, not only as a script an
    engineer runs on the server. This is that capability, and it deliberately shares
    its validation with the seed script (`services.registry.camera_import`) so the two
    cannot drift into disagreeing about what a valid camera row is.

    **Partial success is the normal outcome.** A departmental spreadsheet usually has a
    couple of bad rows, and the useful answer is "28 landed, 2 did not, here is why"
    rather than a single rejection an operator cannot act on. Good rows are applied;
    bad rows are reported with their line number and reason.

    **A rejected row is never partially applied.** Validation for a row completes
    before anything is written for it, so a row cannot leave a camera half-updated.

    Admin-only, and audited before the transaction commits -- the same treatment as
    every other mutating endpoint here. Onboarding a camera is an assertion about
    where surveillance exists, and it should be attributable.
    """
    raw = await file.read()
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {MAX_IMPORT_BYTES // 1024} KiB",
        )
    try:
        text_body = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="file is not UTF-8 text",
        ) from None

    reader = csv.DictReader(io.StringIO(text_body))
    if reader.fieldnames is None or "camera_ref" not in reader.fieldnames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="CSV has no camera_ref column; expected the camera_geo.csv header",
        )
    rows = list(reader)

    departments = {d.code: d for d in session.execute(select(Department)).scalars()}
    if not departments:
        return BulkImportResult(
            rows_read=len(rows),
            accepted=0,
            rejected=len(rows),
            created=0,
            updated=0,
            unset_coordinates=0,
            note="no departments in the registry; run the department seed first",
        )
    default_department = departments.get("HOME") or next(iter(departments.values()))

    rejections: list[BulkImportRejection] = []
    accepted_rows: list[dict[str, str]] = []

    for line_no, row in enumerate(rows, start=2):  # the header is line 1
        problems = validate_row(row, line_no)
        if problems:
            rejections.append(
                BulkImportRejection(
                    line=line_no,
                    camera_ref=(row.get("camera_ref") or "").strip() or None,
                    reasons=problems,
                )
            )
            continue
        accepted_rows.append(row)

    created = updated = unset = 0
    unknown_departments: set[str] = set()
    for row in accepted_rows:
        ref = row["camera_ref"].strip()
        camera = session.execute(
            select(Camera).where(Camera.camera_ref == ref)
        ).scalar_one_or_none()

        # The row may say which department owns the camera, and until now this endpoint
        # ignored it and filed everything under the default -- in an import whose whole
        # premise is a departmental spreadsheet. An unknown code is not a reason to
        # reject an otherwise good row, so it falls back to the default and is reported.
        code = (row.get("department_code") or "").strip().upper()
        department = departments.get(code) if code else None
        if code and department is None:
            unknown_departments.add(code)

        if camera is None:
            camera = Camera(
                camera_ref=ref,
                name=(row.get("name") or f"Camera {ref}").strip(),
                department_id=(department or default_department).id,
                source_type=SourceType.GATEWAY.value,
                # DRAFT, not ACTIVE: onboarded, nothing verified yet. A camera becomes
                # ACTIVE once it has actually been probed.
                status=CameraStatus.DRAFT.value,
                geom_source=GeomSource.UNSET.value,
            )
            session.add(camera)
            created += 1
        else:
            updated += 1
            # Only on an explicit code: an import that omits the column must not
            # silently move every existing camera into the default department.
            if department is not None:
                camera.department_id = department.id

        location = (row.get("location_text") or "").strip()
        if location:
            camera.location_text = location

        # A manually surveyed coordinate outranks an imported one. Someone stood at
        # that camera; a spreadsheet did not.
        if camera.geom_source == GeomSource.MANUAL_SURVEY.value:
            continue

        lat = (row.get("lat") or "").strip()
        lon = (row.get("lon") or "").strip()
        source = (row.get("geom_source") or GeomSource.UNSET.value).strip()

        if lat and lon and source != GeomSource.UNSET.value:
            camera.geom = func.ST_SetSRID(func.ST_MakePoint(float(lon), float(lat)), 4326)
            camera.geom_source = source
            radius = (row.get("confidence_radius_m") or "").strip()
            camera.confidence_radius_m = float(radius) if radius else None
            camera.resolved_by = row.get("resolved_by") or None
            resolved_at = (row.get("resolved_at") or "").strip()
            camera.resolved_at = (
                datetime.fromisoformat(resolved_at) if resolved_at else datetime.now(timezone.utc)
            )
        else:
            # Explicitly NULL rather than zero or a nearby guess. The table's CHECK
            # constraint requires this to pair with geom_source='unset'.
            camera.geom = None
            camera.geom_source = GeomSource.UNSET.value
            camera.confidence_radius_m = None
            camera.resolved_by = None
            camera.resolved_at = None
            unset += 1

    session.flush()

    audit.append(
        session,
        action="CAMERA_BULK_IMPORT",
        subject_type="camera",
        subject_id=file.filename or "upload.csv",
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "rows_read": len(rows),
            "accepted": len(accepted_rows),
            "rejected": len(rejections),
            "created": created,
            "updated": updated,
            "unset_coordinates": unset,
        },
    )
    session.flush()

    return BulkImportResult(
        rows_read=len(rows),
        accepted=len(accepted_rows),
        rejected=len(rejections),
        created=created,
        updated=updated,
        unset_coordinates=unset,
        rejections=rejections,
        note=(
            "unknown department code(s) ignored, cameras filed under the default: "
            + ", ".join(sorted(unknown_departments))
            if unknown_departments
            else None
        ),
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
    feed_settings = _feed_settings_or_none()

    if camera.source_type == SourceType.GATEWAY.value and feed_settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "no camera gateway is configured on this deployment, so there is no "
                "upstream to play. Set SETU_GATEWAY_HOST and redeploy."
            ),
        )

    if camera.source_type == SourceType.GATEWAY.value and feed_settings is not None:
        # Through our own proxy, not the upstream URL directly. The estate serves
        # playlists, segments and the decryption key behind a login, and a browser has
        # no session for that third-party origin -- it receives the sign-in page with
        # HTTP 200 and hls.js reports `manifestLoadError` on a camera that is fine.
        # The API already holds the access code for ingest, so it fronts the stream and
        # the credential stays server-side. See services/api/gateway_proxy.py.
        from services.api.gateway_proxy import signed_proxy_url

        url = signed_proxy_url(camera.camera_ref, "index.m3u8", settings.jwt_secret)
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
