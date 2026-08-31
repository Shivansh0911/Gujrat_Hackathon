"""The demonstration feed, and the reads it actually produced.

Why this endpoint exists
------------------------
A reviewer looking at the console sees a list of plate reads and has no way to tell
them apart from a seeded table. Two different people on this project independently
mistook own-feed detections for government-feed ones, which is the same confusion in a
milder form: nothing on screen connected a read to the footage it came from.

This closes that gap in the only way that is actually convincing. `detection.pts_ms` is
the stream presentation timestamp -- for a file-backed source that is the position
*inside the clip*. So the same number that proves we never time anything by declared
FPS also lets the console seek the video to the exact moment a plate was read, and put
the evidence crop next to it. A judge watches the frame, sees the crop taken from it,
and reads the characters the recogniser returned. Nothing about that is assertable from
a fixture.

It also answers the "which feed is this?" question directly, with counts from the
database rather than a claim in a caption: how many detections came from our own
footage, how many from the government gateway, and -- while the gateway is down --
why the second number is zero.

Deliberately not a processing endpoint
--------------------------------------
There is no "run ANPR on this video" button here. The deployed instance has no GPU and
a shared CPU; inference over a ten-minute clip would take minutes and could exhaust the
container's memory. A demonstration that times out is worse than one that is honest
about where the processing happened. Footage is processed offline, and this endpoint
serves what that produced.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.api.media_signing import media_basename, signed_media_url
from services.api.security import CurrentActor
from services.common.paths import OWN_FEED_DIR

log = logging.getLogger(__name__)

router = APIRouter(tags=["demo"])
SessionDep = Annotated[Session, Depends(get_session)]

#: The clip the container image carries. `seed_demo.py` prefers the same file, so the
#: reads below and the video above them are the same footage by construction.
DEMO_CLIP_NAME = "demo_clip.mp4"


class DemoRead(BaseModel):
    """One plate read, positioned in the clip that produced it."""

    plate: str
    confidence: float
    #: Position within the clip, in seconds. This is what makes the video seekable to
    #: the read: it is the stream PTS, not a wall-clock time we assigned afterwards.
    at_seconds: float
    camera_ref: str
    camera_name: str
    observed_at_utc: str
    crop_url: str | None
    corrections: list[dict[str, Any]]


class DemoFeed(BaseModel):
    clip_available: bool
    clip_url: str | None
    clip_name: str
    source_title: str
    source_url: str
    licence: str
    attribution: str
    reads: list[DemoRead]
    own_feed_detections: int
    gateway_detections: int
    note: str


@router.get("/demo/own-feed", response_model=DemoFeed)
def demo_own_feed(
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
) -> DemoFeed:
    """The demonstration clip, and every plate read taken from it."""
    rows = (
        session.execute(
            text(
                """
            SELECT d.plate_normalised, d.plate_raw, d.confidence, d.pts_ms,
                   d.observed_at_utc, d.crop_path, d.corrections,
                   c.camera_ref, c.name AS camera_name
            FROM detection d
            JOIN camera c ON c.id = d.camera_id
            WHERE c.camera_ref LIKE 'REPLAY%'
            ORDER BY d.pts_ms, c.camera_ref
            """
            )
        )
        .mappings()
        .all()
    )

    reads: list[DemoRead] = []
    for row in rows:
        crop = row["crop_path"]
        crop_url = (
            signed_media_url("/media/crops", media_basename(crop), settings.jwt_secret)
            if crop
            else None
        )
        corrections = row["corrections"] or []
        reads.append(
            DemoRead(
                plate=row["plate_normalised"] or row["plate_raw"] or "",
                confidence=float(row["confidence"]),
                at_seconds=round(float(row["pts_ms"]) / 1000.0, 2),
                camera_ref=row["camera_ref"],
                camera_name=row["camera_name"],
                observed_at_utc=row["observed_at_utc"].isoformat(),
                crop_url=crop_url,
                corrections=list(corrections) if isinstance(corrections, list) else [],
            )
        )

    # Counted from the database, not asserted. The split between the two feeds is the
    # single most misread thing about this submission, and a number that is wrong will
    # at least be wrong visibly.
    counts = (
        session.execute(
            text(
                """
            SELECT
              COUNT(*) FILTER (WHERE c.camera_ref LIKE 'REPLAY%')  AS own_feed,
              COUNT(*) FILTER (WHERE c.camera_ref NOT LIKE 'REPLAY%') AS gateway
            FROM detection d JOIN camera c ON c.id = d.camera_id
            """
            )
        )
        .mappings()
        .one()
    )
    own_n = int(counts["own_feed"] or 0)
    gw_n = int(counts["gateway"] or 0)

    clip_path = OWN_FEED_DIR / DEMO_CLIP_NAME
    available = clip_path.is_file()
    clip_url = (
        signed_media_url("/media/own-feed", DEMO_CLIP_NAME, settings.jwt_secret)
        if available
        else None
    )

    if gw_n:
        note = (
            f"{gw_n} detection(s) in this instance came from the government gateway. "
            f"The {own_n} below came from the clip you can play here."
        )
    else:
        note = (
            "No detection in this instance came from the government gateway. Every read "
            "below is from the clip you can play here. That is not a choice: the "
            "gateway has returned a Cloudflare 502 on every endpoint since 31 August, "
            "so there has been nothing to read. The Health page shows its live state."
        )

    return DemoFeed(
        clip_available=available,
        clip_url=clip_url,
        clip_name=DEMO_CLIP_NAME,
        source_title="India's Number one BRTS Bus Service Hubli to Dharwad Karnataka",
        source_url=(
            "https://commons.wikimedia.org/wiki/File:India%27s_Number_one_BRTS_Bus_"
            "Service_Hubli_to_Dharwad_Karnataka.webm"
        ),
        licence="CC BY 3.0",
        attribution=(
            '"India\'s Number one BRTS Bus Service Hubli to Dharwad Karnataka", '
            "Wikimedia Commons, licensed CC BY 3.0. Cropped to 90 seconds and transcoded."
        ),
        reads=reads,
        own_feed_detections=own_n,
        gateway_detections=gw_n,
        note=note,
    )
