"""Health and audit endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.analytics.readiness import (
    MIN_PLATE_SAMPLE,
    MIN_VEHICLE_SAMPLE,
    PLATE_PX_MARGINAL,
    PLATE_PX_READABLE,
    RATIO_ALIGNED,
    ReadinessGrade,
    analytics_for,
    grade_cameras,
)
from services.api import audit as audit_mod
from services.api.buildinfo import build_info, readiness
from services.api.versioning import version_info
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


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def liveness() -> dict[str, str]:
    """Liveness, answered for HEAD as well as GET.

    FastAPI does not add HEAD to a GET route, and uptime monitors send HEAD by
    default -- UptimeRobot did, and read the resulting 405 as an outage for four
    days while the service was healthy the whole time. A liveness probe that only
    half the monitoring world can speak is not doing its job.
    """
    return {"status": "ok"}


class ReadinessOut(BaseModel):
    """One camera's measured ANPR readiness, with the evidence behind the grade.

    The evidence travels with the verdict. A grade of `not_viable` that arrives without
    the numbers behind it is an opinion, and this one may end up in front of someone
    deciding whether to spend money on a camera.
    """

    camera_id: str
    camera_ref: str
    camera_name: str

    plate_samples: int
    plate_px_median: float | None
    plate_px_p90: float | None
    vehicle_samples: int
    vehicle_px_median: float | None
    plate_to_vehicle_ratio: float | None

    grade: str
    reasoning: str
    remedy: str | None
    #: Which analytics this camera should be asked to run, from its grade (§4.2).
    analytics: list[str]


class ReadinessSummaryOut(BaseModel):
    counts_by_grade: dict[str, int]
    #: The fraction of graded cameras that can actually read a plate. This is the input
    #: the Scale Planner needs and the number the estate's owner does not currently have.
    anpr_capable_fraction: float | None
    graded: int
    ungraded: int
    thresholds: dict[str, float]
    cameras: list[ReadinessOut]


@router.get("/health/readiness", response_model=ReadinessSummaryOut)
def camera_readiness(session: SessionDep, actor: CurrentActor) -> ReadinessSummaryOut:
    """Grade every camera for ANPR readiness, and say what the rest should run instead.

    Recomputed on each call rather than cached. It is cheap -- two grouped aggregates
    over indexed columns -- and a stale grade is worse than none when it drives a
    re-aiming or replacement decision.
    """
    rows = grade_cameras(session)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.grade.value] = counts.get(row.grade.value, 0) + 1

    graded = sum(
        count
        for grade, count in counts.items()
        if grade != ReadinessGrade.INSUFFICIENT_SAMPLE.value
    )
    capable = counts.get(ReadinessGrade.ANPR_CAPABLE.value, 0)

    return ReadinessSummaryOut(
        counts_by_grade=counts,
        # None rather than 0.0 when nothing is graded: "no cameras can read plates" and
        # "we have not measured any camera yet" are different findings, and a Scale
        # Planner fed the first when the second is true produces a confident wrong answer.
        anpr_capable_fraction=round(capable / graded, 4) if graded else None,
        graded=graded,
        ungraded=counts.get(ReadinessGrade.INSUFFICIENT_SAMPLE.value, 0),
        thresholds={
            "plate_px_readable": PLATE_PX_READABLE,
            "plate_px_marginal": PLATE_PX_MARGINAL,
            "ratio_aligned": RATIO_ALIGNED,
            "min_plate_sample": float(MIN_PLATE_SAMPLE),
            "min_vehicle_sample": float(MIN_VEHICLE_SAMPLE),
        },
        cameras=[
            ReadinessOut(**row.as_dict(), analytics=list(analytics_for(row.grade))) for row in rows
        ],
    )


class DependencyOut(BaseModel):
    name: str
    ok: bool
    detail: str
    latency_ms: float | None


class ReadyOut(BaseModel):
    ready: bool
    checks: list[DependencyOut]


@router.api_route("/readyz", methods=["GET", "HEAD"], include_in_schema=True)
def readiness_probe(session: SessionDep, response: Response) -> ReadyOut:
    """Should this instance receive traffic?

    Distinct from `/healthz`, which answers only "is this process alive". An
    orchestrator reading liveness as failed *restarts* the container, so a liveness
    probe that touched the database would turn a recoverable blip into a restart loop.
    This one touches dependencies and returns 503 when they are unhappy, which is the
    right signal for a load balancer to stop sending traffic here.

    Deliberately does **not** check the government gateway. It is third-party
    infrastructure that is down for hours at a time, its state is already on
    `/health/gateway`, and failing readiness on it would remove the whole platform from
    the load balancer while journeys, alerts and exports all work perfectly.
    """
    ready, checks = readiness(session)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyOut(ready=ready, checks=[DependencyOut(**c.as_dict()) for c in checks])


@router.get("/build-info")
def build_information() -> dict[str, Any]:
    """Version, commit, expected migration head and analytics versions.

    What an ops team monitors, and what a disputed result is tied back to: an evidence
    export names its analytics versions, and this says which build produced them.
    """
    return build_info()


@router.get("/api-version")
def api_version() -> dict[str, Any]:
    """The versioning and deprecation policy, served rather than only documented."""
    return version_info()
