"""Health and audit endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from services.api import audit as audit_mod
from services.api.db import get_session
from services.api.schemas import AuditVerifyOut, CameraHealthOut, GatewayStatusOut
from services.api.security import CurrentActor, camera_scope
from services.registry.enums import GeomSource
from services.registry.models import Camera

router = APIRouter(tags=["system"])

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/health/cameras", response_model=list[CameraHealthOut])
def camera_health(session: SessionDep, actor: CurrentActor) -> list[CameraHealthOut]:
    """Per-camera health, including the declared-vs-measured FPS divergence.

    The drift column exists because the organiser's integration guide warns that the
    reported frame rate cannot be trusted. Showing the gap turns their warning into a
    visible platform feature instead of an assumption buried in our pipeline.
    """
    cameras = session.execute(camera_scope(actor).order_by(Camera.camera_ref)).scalars().all()

    out: list[CameraHealthOut] = []
    for camera in cameras:
        drift = None
        if camera.measured_fps is not None and camera.declared_fps:
            drift = round(
                (camera.measured_fps - camera.declared_fps) / camera.declared_fps * 100, 1
            )
        out.append(
            CameraHealthOut(
                camera_id=camera.id,
                camera_ref=camera.camera_ref,
                name=camera.name,
                status=camera.status,
                transport=camera.transport,
                declared_fps=camera.declared_fps,
                measured_fps=camera.measured_fps,
                fps_drift_pct=drift,
                last_seen_at=camera.last_seen_at,
                coordinate_missing=camera.geom_source == GeomSource.UNSET.value,
            )
        )
    return out


@router.get("/health/gateway", response_model=GatewayStatusOut)
def gateway_status(actor: CurrentActor) -> GatewayStatusOut:
    """Current reachability of the government gateway, from the passive watcher.

    Cheap on purpose: this returns the last recorded observation rather than probing,
    so the console can poll it without turning a page refresh into load on somebody
    else's infrastructure. The watcher does the probing, once a minute.
    """
    from services.api import gateway_watch

    return GatewayStatusOut(**gateway_watch.current_status().as_dict())  # type: ignore[arg-type]


@router.get("/audit/verify", response_model=AuditVerifyOut)
def verify_audit_chain(session: SessionDep, actor: CurrentActor) -> AuditVerifyOut:
    """Recompute the audit hash chain and report any break.

    Deliberately available to any authenticated actor, not just admins: the value of
    a tamper-evident ledger is that anyone with standing can check it, and restricting
    verification to the same role that can alter records defeats the purpose.
    """
    result = audit_mod.verify_chain(session)
    return AuditVerifyOut(**result)


@router.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}
