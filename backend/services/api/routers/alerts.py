"""Alert desk endpoints and the live WebSocket feed."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import (
    Response,
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.analytics.plate_grammar import normalise_plate
from services.api import audit
from services.api.config import get_api_settings
from services.api.media_signing import signed_media_url
from services.api.db import get_session
from services.api.security import (
    Actor,
    AdminActor,
    CurrentActor,
    camera_scope,
    decode_token,
)
from services.registry.enums import AlertDisposition, AlertState, Role
from services.registry.models import Alert, Camera, WatchlistEntry

log = logging.getLogger(__name__)

router = APIRouter(tags=["alerts"])
SessionDep = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------- schemas


class AlertOut(BaseModel):
    id: uuid.UUID
    matched_value: str
    match_type: str
    match_score: float
    priority: float
    state: str
    disposition: str | None = None
    observation_count: int
    is_movement: bool

    camera_id: uuid.UUID
    camera_name: str | None = None
    #: `gateway` or `file` -- see JourneyHop.camera_source_type.
    camera_source_type: str | None = None
    camera_lat: float | None = None
    camera_lon: float | None = None

    observed_at_utc: datetime
    raised_at: datetime
    latency_ms: float | None = None

    detection_pts_ms: float | None = None
    detection_confidence: float | None = None
    corrections: list[dict[str, Any]] = []
    crop_url: str | None = None

    watchlist_name: str | None = None
    watchlist_authority: str | None = None
    watchlist_case_ref: str | None = None
    watchlist_priority: int | None = None

    corroboration: dict[str, Any] = {}
    sightings: list[dict[str, Any]] = []

    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None


class ResolveRequest(BaseModel):
    disposition: str = Field(description="true_positive | false_positive | unable_to_verify")
    note: str | None = Field(default=None, max_length=1000)


class WatchlistOut(BaseModel):
    id: uuid.UUID
    plate_normalised: str | None
    watchlist_name: str
    authority: str | None
    case_ref: str | None
    priority: int
    severity: str
    colour: str | None
    make: str | None
    model: str | None
    active: bool
    valid_from: datetime
    valid_to: datetime
    notes: str | None

    @property
    def expired(self) -> bool:
        return self.valid_to <= datetime.now(timezone.utc)


class WatchlistCreate(BaseModel):
    plate_normalised: str = Field(min_length=4, max_length=32)
    watchlist_name: str = Field(min_length=1, max_length=120)
    authority: str = Field(min_length=1, max_length=200)
    case_ref: str | None = Field(default=None, max_length=120)
    priority: int = Field(default=50, ge=0, le=100)
    severity: str = Field(default="medium")
    colour: str | None = None
    make: str | None = None
    model: str | None = None
    notes: str | None = Field(default=None, max_length=1000)
    # Required, with no default. An entry without an expiry becomes a permanent
    # shadow record on a citizen; making the caller state one is the only reliable
    # way to stop that happening by omission.
    valid_to: datetime


# ------------------------------------------------------------------ projection


def _project(session: Session, alert: Alert) -> AlertOut:
    camera = session.get(Camera, alert.camera_id)
    entry = (
        session.get(WatchlistEntry, alert.watchlist_entry_id) if alert.watchlist_entry_id else None
    )

    lat = lon = None
    if camera is not None and camera.geom is not None:
        # ST_X/ST_Y are geometry functions; a geography column must be cast first.
        row = session.execute(
            text(
                "SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
                "FROM camera WHERE id = :cid"
            ),
            {"cid": camera.id},
        ).one_or_none()
        if row is not None:
            lat, lon = float(row.lat), float(row.lon)

    first = alert.sightings[0] if alert.sightings else {}
    crop = first.get("crop_path")
    # Served by basename through the media route: the absolute path never reaches the
    # browser, which must not learn the server's filesystem layout.
    crop_url = (
        signed_media_url("/media/crops", crop, get_api_settings().jwt_secret) if crop else None
    )

    return AlertOut(
        id=alert.id,
        matched_value=alert.matched_value,
        match_type=alert.match_type,
        match_score=alert.match_score,
        priority=alert.priority,
        state=alert.state,
        disposition=alert.disposition,
        observation_count=alert.observation_count,
        is_movement=alert.is_movement,
        camera_id=alert.camera_id,
        camera_name=camera.name if camera else None,
        camera_source_type=camera.source_type if camera else None,
        camera_lat=lat,
        camera_lon=lon,
        observed_at_utc=alert.observed_at_utc,
        raised_at=alert.raised_at,
        latency_ms=alert.latency_ms,
        detection_pts_ms=first.get("pts_ms"),
        detection_confidence=first.get("confidence"),
        corrections=first.get("corrections", []),
        crop_url=crop_url,
        watchlist_name=entry.watchlist_name if entry else None,
        watchlist_authority=entry.authority if entry else None,
        watchlist_case_ref=entry.case_ref if entry else None,
        watchlist_priority=entry.priority if entry else None,
        corroboration=alert.corroboration or {},
        sightings=alert.sightings or [],
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
    )


# ------------------------------------------------------------------- endpoints


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    session: SessionDep,
    actor: CurrentActor,
    state: str | None = Query(default=None),
    min_priority: float | None = Query(default=None, ge=0, le=1),
    camera_id: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AlertOut]:
    """Alerts within the caller's camera scope, highest priority first."""
    # Scope through the camera accessor rather than querying alerts directly: an
    # alert is only visible if its camera is.
    visible = select(Camera.id).where(Camera.id.in_(select(camera_scope(actor).subquery().c.id)))
    stmt = select(Alert).where(Alert.camera_id.in_(visible))

    if state:
        stmt = stmt.where(Alert.state == state.upper())
    if min_priority is not None:
        stmt = stmt.where(Alert.priority >= min_priority)
    if camera_id is not None:
        stmt = stmt.where(Alert.camera_id == camera_id)
    if since is not None:
        stmt = stmt.where(Alert.raised_at >= since)

    alerts = (
        session.execute(stmt.order_by(Alert.priority.desc(), Alert.raised_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [_project(session, a) for a in alerts]


def _get_alert_in_scope(session: Session, actor: Actor, alert_id: uuid.UUID) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    # 404 rather than 403 for an out-of-scope alert, so ids cannot be enumerated.
    in_scope = session.execute(
        camera_scope(actor).where(Camera.id == alert.camera_id)
    ).scalar_one_or_none()
    if in_scope is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return alert


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def acknowledge_alert(alert_id: uuid.UUID, session: SessionDep, actor: CurrentActor) -> AlertOut:
    alert = _get_alert_in_scope(session, actor, alert_id)
    if alert.state == AlertState.RESOLVED.value:
        raise HTTPException(status_code=409, detail="alert is already resolved")

    alert.state = AlertState.ACKNOWLEDGED.value
    alert.acknowledged_by = actor.subject
    alert.acknowledged_at = datetime.now(timezone.utc)

    audit.append(
        session,
        action="ACK_ALERT",
        subject_type="alert",
        subject_id=str(alert.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={"matched_value": alert.matched_value, "match_type": alert.match_type},
    )
    session.flush()
    return _project(session, alert)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(
    alert_id: uuid.UUID, body: ResolveRequest, session: SessionDep, actor: CurrentActor
) -> AlertOut:
    """Close an alert with a disposition.

    The disposition is not bookkeeping: it feeds the per-camera false-positive rate
    on the Health screen, which is how the platform measures its own precision
    instead of asserting it.
    """
    valid = {d.value for d in AlertDisposition}
    if body.disposition not in valid:
        raise HTTPException(status_code=422, detail=f"disposition must be one of {sorted(valid)}")

    alert = _get_alert_in_scope(session, actor, alert_id)
    alert.state = AlertState.RESOLVED.value
    alert.disposition = body.disposition
    if alert.acknowledged_by is None:
        alert.acknowledged_by = actor.subject
        alert.acknowledged_at = datetime.now(timezone.utc)

    audit.append(
        session,
        action="RESOLVE_ALERT",
        subject_type="alert",
        subject_id=str(alert.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "matched_value": alert.matched_value,
            "disposition": body.disposition,
            "note": body.note,
        },
    )
    session.flush()
    return _project(session, alert)


@router.get("/watchlist", response_model=list[WatchlistOut])
def list_watchlist(
    session: SessionDep,
    actor: CurrentActor,
    include_expired: bool = Query(default=False),
) -> list[WatchlistOut]:
    stmt = select(WatchlistEntry)
    if not include_expired:
        stmt = stmt.where(WatchlistEntry.valid_to > datetime.now(timezone.utc))
    entries = session.execute(stmt.order_by(WatchlistEntry.priority.desc())).scalars().all()
    return [WatchlistOut.model_validate(e, from_attributes=True) for e in entries]


@router.post("/watchlist", response_model=WatchlistOut, status_code=201)
def create_watchlist_entry(
    body: WatchlistCreate, session: SessionDep, actor: CurrentActor
) -> WatchlistOut:
    if actor.role != Role.ADMIN.value:
        raise HTTPException(status_code=403, detail="adding a watchlist entry requires admin")
    if body.valid_to <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=422,
            detail=(
                "the expiry date must be in the future. An entry that has already "
                "expired would never match anything."
            ),
        )

    # Validate with the same grammar the recogniser's output is checked against, rather
    # than a second implementation. A watchlist plate that could never be produced by
    # the ANPR pipeline can never match one, so accepting it creates an entry that
    # looks active on the desk and is silently inert -- the worst kind of failure here,
    # because nobody finds out until the vehicle they were watching for goes unnoticed.
    candidate = body.plate_normalised.upper().replace(" ", "").replace("-", "")
    parsed = normalise_plate(candidate)
    if not parsed.valid:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{body.plate_normalised}' is not a valid Indian registration. "
                "Expected formats: GJ01AB1234 (state, district, series, number) or a "
                "BH-series plate such as 22BH1234A."
            ),
        )

    entry = WatchlistEntry(
        plate_normalised=parsed.normalised,
        entity_type="vehicle",
        watchlist_name=body.watchlist_name,
        source_system=body.authority,
        authority=body.authority,
        severity=body.severity,
        priority=body.priority,
        case_ref=body.case_ref,
        colour=body.colour,
        make=body.make,
        model=body.model,
        notes=body.notes,
        active=True,
        valid_from=datetime.now(timezone.utc),
        valid_to=body.valid_to,
    )
    session.add(entry)
    session.flush()

    audit.append(
        session,
        action="WATCHLIST_ENTRY_ADDED",
        subject_type="watchlist_entry",
        subject_id=str(entry.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "plate": entry.plate_normalised,
            "authority": entry.authority,
            "case_ref": entry.case_ref,
            "expires_at": entry.valid_to.isoformat(),
        },
    )
    return WatchlistOut.model_validate(entry, from_attributes=True)


@router.delete("/watchlist/{entry_id}", status_code=204)
def delete_watchlist_entry(
    entry_id: uuid.UUID, session: SessionDep, actor: CurrentActor
) -> Response:
    """Remove a watchlist entry.

    Deletion is audited *before* the row goes, and the audit entry carries the plate,
    authority and case reference. That ordering is the point: once the row is gone the
    ledger is the only remaining evidence that the vehicle was ever watched, and a
    surveillance authorisation that can be removed without trace is not an
    authorisation.

    Entries normally end by expiring -- `valid_to` is mandatory for exactly that
    reason. This is for the other cases: a plate entered wrongly, or an authority
    withdrawn before its expiry.

    Alerts already raised are untouched. They are evidence of what was observed, and
    they reference the entry rather than depending on it.
    """
    if actor.role != Role.ADMIN.value:
        raise HTTPException(status_code=403, detail="removing a watchlist entry requires admin")

    entry = session.get(WatchlistEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="watchlist entry not found")

    audit.append(
        session,
        action="WATCHLIST_ENTRY_REMOVED",
        subject_type="watchlist_entry",
        subject_id=str(entry.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "plate": entry.plate_normalised,
            "watchlist_name": entry.watchlist_name,
            "authority": entry.authority,
            "case_ref": entry.case_ref,
            "expires_at": entry.valid_to.isoformat(),
        },
    )
    session.flush()

    session.delete(entry)
    session.flush()
    return Response(status_code=204)


# ------------------------------------------------------------------- websocket


class AlertHub:
    """Fan-out to connected alert-desk clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload, default=str)
        async with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                # A dead socket must not stop the others receiving the alert.
                await self.disconnect(ws)


hub = AlertHub()


@router.websocket("/ws/alerts")
async def alert_stream(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    """Live alert feed.

    The token arrives as a query parameter because browsers cannot set headers on a
    WebSocket handshake. It is verified exactly as on any other route -- an
    unauthenticated socket is closed before it is accepted, so an anonymous client
    never receives a single alert.
    """
    settings = get_api_settings()
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        decode_token(token, settings)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await hub.connect(websocket)
    try:
        while True:
            # The client sends nothing; this keeps the connection open and detects
            # disconnection promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(websocket)
    except Exception:
        await hub.disconnect(websocket)


class RescanResult(BaseModel):
    detections_scanned: int
    watchlist_alerts: int
    zone_alerts: int
    speed_alerts: int
    deduplicated: int


@router.post("/alerts/rescan", response_model=RescanResult)
def rescan(
    session: SessionDep,
    actor: AdminActor,
    hours: int = Query(default=168, ge=1, le=24 * 90),
) -> RescanResult:
    """Re-evaluate detections already on record against the current configuration.

    Alerts are normally raised as detections arrive, which means a rule added afterwards
    only ever applies to the future. That is the wrong default for how this system is
    actually used: an officer adds a registration to the watchlist *because* of something
    that already happened, and draws an intrusion zone around a place that has already
    been driven through. Without this, the honest answer to "has this vehicle been seen?"
    is "it will be, from now on", and the evidence sitting in the database goes unasked.

    So it runs the same three classifiers over stored detections -- the watchlist matcher,
    the zone test and the speed check -- with no re-decoding of any video. Deduplication
    is unchanged, so re-running it does not multiply alerts: a detection that already
    produced one folds into it rather than raising a second.

    Admin-only and audited. Re-evaluating the estate against a newly drawn zone can put
    alerts in front of officers, and who asked for that should be recoverable.
    """
    from datetime import timedelta

    from services.analytics.matcher import scan_detections
    from services.api.config import get_api_settings as _api_settings

    settings = _api_settings()
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    stats = scan_detections(
        session,
        since=since,
        max_speed_highway_kmph=settings.max_speed_highway_kmph,
        max_speed_urban_kmph=settings.max_speed_urban_kmph,
    )

    audit.append(
        session,
        action="RESCAN_DETECTIONS",
        subject_type="estate",
        subject_id="alerts",
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose="Re-evaluate stored detections against current watchlist and zones",
        detail={
            "hours": hours,
            "detections_scanned": stats.detections_scanned,
            "watchlist_alerts": stats.alerts_created,
            "zone_alerts": stats.zone_alerts,
            "speed_alerts": stats.speed_alerts,
        },
    )
    session.flush()

    return RescanResult(
        detections_scanned=stats.detections_scanned,
        watchlist_alerts=stats.alerts_created,
        zone_alerts=stats.zone_alerts,
        speed_alerts=stats.speed_alerts,
        deduplicated=stats.deduplicated,
    )
