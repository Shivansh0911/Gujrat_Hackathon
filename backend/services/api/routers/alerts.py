"""Alert desk endpoints and the live WebSocket feed."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.api import audit
from services.api.config import get_api_settings
from services.api.db import get_session
from services.api.security import CurrentActor, camera_scope, decode_token
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
        session.get(WatchlistEntry, alert.watchlist_entry_id)
        if alert.watchlist_entry_id
        else None
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
    crop_url = f"/media/crops/{Path(crop).name}" if crop else None

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
    visible = select(Camera.id).where(
        Camera.id.in_(select(camera_scope(actor).subquery().c.id))
    )
    stmt = select(Alert).where(Alert.camera_id.in_(visible))

    if state:
        stmt = stmt.where(Alert.state == state.upper())
    if min_priority is not None:
        stmt = stmt.where(Alert.priority >= min_priority)
    if camera_id is not None:
        stmt = stmt.where(Alert.camera_id == camera_id)
    if since is not None:
        stmt = stmt.where(Alert.raised_at >= since)

    alerts = session.execute(
        stmt.order_by(Alert.priority.desc(), Alert.raised_at.desc()).limit(limit)
    ).scalars().all()
    return [_project(session, a) for a in alerts]


def _get_alert_in_scope(session: Session, actor, alert_id: uuid.UUID) -> Alert:
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
def acknowledge_alert(
    alert_id: uuid.UUID, session: SessionDep, actor: CurrentActor
) -> AlertOut:
    alert = _get_alert_in_scope(session, actor, alert_id)
    if alert.state == AlertState.RESOLVED.value:
        raise HTTPException(status_code=409, detail="alert is already resolved")

    alert.state = AlertState.ACKNOWLEDGED.value
    alert.acknowledged_by = actor.subject
    alert.acknowledged_at = datetime.now(timezone.utc)

    audit.append(
        session, action="ACK_ALERT", subject_type="alert", subject_id=str(alert.id),
        actor_id=actor.subject, actor_role=actor.role,
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
        raise HTTPException(
            status_code=422, detail=f"disposition must be one of {sorted(valid)}"
        )

    alert = _get_alert_in_scope(session, actor, alert_id)
    alert.state = AlertState.RESOLVED.value
    alert.disposition = body.disposition
    if alert.acknowledged_by is None:
        alert.acknowledged_by = actor.subject
        alert.acknowledged_at = datetime.now(timezone.utc)

    audit.append(
        session, action="RESOLVE_ALERT", subject_type="alert", subject_id=str(alert.id),
        actor_id=actor.subject, actor_role=actor.role,
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
        raise HTTPException(status_code=422, detail="valid_to must be in the future")

    entry = WatchlistEntry(
        plate_normalised=body.plate_normalised.upper().replace(" ", ""),
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
        session, action="WATCHLIST_ENTRY_ADDED", subject_type="watchlist_entry",
        subject_id=str(entry.id), actor_id=actor.subject, actor_role=actor.role,
        detail={
            "plate": entry.plate_normalised,
            "authority": entry.authority,
            "case_ref": entry.case_ref,
            "expires_at": entry.valid_to.isoformat(),
        },
    )
    return WatchlistOut.model_validate(entry, from_attributes=True)


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
