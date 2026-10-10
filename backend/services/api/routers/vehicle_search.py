"""Finding a vehicle with no registration to search by.

Every other search surface on this platform starts from a plate. This one starts from
a picture, because on this estate most vehicles never produce a plate the recogniser
can resolve, and "we could not read it" has so far meant "we cannot help you".

Two endpoints, and the split matters. Browsing is cheap and read-only and happens
constantly as an operator scans footage. Searching from one of those sightings runs a
vector query over the estate and is audited, because it is an investigative act
performed against a person's vehicle and the ledger is what makes that accountable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.analytics.candidate_journey import Candidate
from services.analytics.model_ids import APPEARANCE_KIND
from services.analytics.vehicle_search import (
    AnchorUnavailable,
    browse_sightings,
    find_similar,
)
from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.api.media_signing import signed_media_url
from services.api.security import CurrentActor, camera_scope

router = APIRouter(prefix="/vehicle-search", tags=["vehicle-search"])
SessionDep = Annotated[Session, Depends(get_session)]

#: Browsing is bounded the same way every other window on this API is. The vector
#: search inside a result is bounded separately by `SEARCH_WINDOW`.
MAX_WINDOW = timedelta(days=31)


class SightingOut(BaseModel):
    sighting_id: str
    observed_at_utc: datetime
    camera_id: str
    camera_ref: str
    camera_name: str
    vehicle_class: str
    dominant_colour_lab: dict[str, float] | None
    quality_score: float
    crop_url: str | None
    #: Usually null. That is the point: these are vehicles no registration identifies.
    plate_read: str | None
    has_fingerprint: bool


class MatchOut(BaseModel):
    sighting_id: str
    observed_at_utc: datetime
    camera_id: str
    camera_ref: str
    camera_name: str
    lat: float | None
    lon: float | None
    vehicle_class: str
    dominant_colour_lab: dict[str, float] | None
    quality_score: float
    crop_url: str | None
    appearance_distance: float
    distance_m: float
    seconds_from_seed: float
    implied_speed_kmph: float
    score: dict[str, float]
    #: Always "candidate" from this endpoint. Kept in the payload rather than implied,
    #: so an integrator reading the JSON sees the ceiling without reading the prose.
    tier: str


class BrowseResult(BaseModel):
    from_utc: datetime
    to_utc: datetime
    total_returned: int
    sightings: list[SightingOut]
    note: str


class SearchResultOut(BaseModel):
    anchor: SightingOut
    matches: list[MatchOut]
    appearance_kind: str
    max_distance: float
    min_score: float
    query_ms: float
    caveat: str


def _crop_url(path: str | None) -> str | None:
    if not path:
        return None
    return signed_media_url("/media/crops", path, get_api_settings().jwt_secret)


def _sighting_out(brief: Any) -> SightingOut:
    return SightingOut(
        sighting_id=str(brief.sighting_id),
        observed_at_utc=brief.observed_at_utc,
        camera_id=str(brief.camera_id),
        camera_ref=brief.camera_ref,
        camera_name=brief.camera_name,
        vehicle_class=brief.vehicle_class,
        dominant_colour_lab=brief.dominant_colour_lab,
        quality_score=brief.quality_score,
        crop_url=_crop_url(brief.crop_path),
        plate_read=brief.plate_read,
        has_fingerprint=brief.has_fingerprint,
    )


def _match_out(candidate: Candidate) -> MatchOut:
    return MatchOut(
        sighting_id=str(candidate.sighting_id),
        observed_at_utc=candidate.observed_at_utc,
        camera_id=str(candidate.camera_id),
        camera_ref=candidate.camera_ref,
        camera_name=candidate.camera_name,
        lat=candidate.lat,
        lon=candidate.lon,
        vehicle_class=candidate.vehicle_class,
        dominant_colour_lab=candidate.dominant_colour_lab,
        quality_score=candidate.quality_score,
        crop_url=_crop_url(candidate.crop_path),
        appearance_distance=candidate.appearance_distance,
        distance_m=candidate.distance_m,
        seconds_from_seed=candidate.seconds_from_seed,
        implied_speed_kmph=candidate.implied_speed_kmph,
        score={
            "total": candidate.score.total,
            "appearance": candidate.score.appearance,
            "quality": candidate.score.quality,
            "class": candidate.score.vehicle_class,
            "colour": candidate.score.colour,
            "plausibility": candidate.score.plausibility,
        },
        tier=candidate.tier.value,
    )


@router.get("/sightings", response_model=BrowseResult)
def browse(
    session: SessionDep,
    actor: CurrentActor,
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
    vehicle_class: Annotated[str | None, Query()] = None,
    camera_ref: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BrowseResult:
    """Vehicles the detector recorded, for an operator to pick one out of.

    Not audited. Looking at footage the platform already holds is the ordinary work of
    a control room, and an entry per scroll would bury the entries that matter -- the
    searches, which are audited, and the exports, which are audited more heavily still.
    """
    if to <= from_:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="`to` must be after `from`",
        )
    if to - from_ > MAX_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"window exceeds the {MAX_WINDOW.days}-day maximum",
        )

    visible = [c.id for c in session.execute(camera_scope(actor)).scalars()]
    rows = browse_sightings(
        session,
        camera_ids=visible,
        from_=from_,
        to=to,
        vehicle_class=vehicle_class,
        camera_ref=camera_ref,
        limit=limit,
        offset=offset,
    )
    with_plate = sum(1 for r in rows if r.plate_read)
    return BrowseResult(
        from_utc=from_,
        to_utc=to,
        total_returned=len(rows),
        sightings=[_sighting_out(r) for r in rows],
        note=(
            f"{len(rows)} vehicles, of which {with_plate} also produced a readable "
            "registration. The rest are invisible to any plate-based search, which is "
            "what this screen is for. Best crops first."
        ),
    )


class SearchRequest(BaseModel):
    """Why this search is being run. Recorded before it produces anything."""

    purpose: str = Field(min_length=8, max_length=500)


@router.post("/sightings/{sighting_id}/similar", response_model=SearchResultOut)
def similar(
    sighting_id: uuid.UUID,
    body: SearchRequest,
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
) -> SearchResultOut:
    """Find the same vehicle on other cameras, starting from one sighting.

    POST rather than GET, and with a required purpose, for the same reason the journey
    search has both: this is an investigative act against a particular vehicle, the
    ledger records why it happened before it happens, and a side effect belongs behind
    a method that is not supposed to be cacheable or prefetchable.

    Every result is a Candidate. Nothing returned here can be promoted, because no
    registration anchors it -- two vehicles that look alike are two vehicles that look
    alike.
    """
    started = datetime.now(timezone.utc)
    window_to = to or started
    window_from = from_ or (window_to - timedelta(days=2))
    if window_to <= window_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="`to` must be after `from`",
        )

    visible = [c.id for c in session.execute(camera_scope(actor)).scalars()]
    try:
        result = find_similar(
            session,
            sighting_id=sighting_id,
            camera_ids=visible,
            from_=window_from,
            to=window_to,
            max_speed_urban_kmph=settings.max_speed_urban_kmph,
            max_speed_highway_kmph=settings.max_speed_highway_kmph,
            limit=limit,
        )
    except AnchorUnavailable as exc:
        # 404 and not 422: whether that sighting exists is itself scoped, and an error
        # that distinguishes "no such row" from "not yours" is an enumeration oracle.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    audit.append(
        session,
        action="VEHICLE_APPEARANCE_SEARCHED",
        subject_type="vehicle_sighting",
        subject_id=str(sighting_id),
        actor_id=actor.subject,
        actor_role=actor.role,
        detail={
            "purpose": body.purpose,
            "anchor_camera": result.anchor.camera_ref,
            "anchor_has_plate": bool(result.anchor.plate_read),
            "matches": len(result.matches),
            "window_start": window_from.isoformat(),
            "window_end": window_to.isoformat(),
            "appearance_kind": result.appearance_kind,
        },
    )
    session.flush()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
    return SearchResultOut(
        anchor=_sighting_out(result.anchor),
        matches=[_match_out(m) for m in result.matches],
        appearance_kind=result.appearance_kind or APPEARANCE_KIND,
        max_distance=result.max_distance,
        min_score=result.min_score,
        query_ms=elapsed,
        caveat=result.caveat,
    )
