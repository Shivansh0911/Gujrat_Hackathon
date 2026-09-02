"""Intrusion detection: a vehicle centring inside a zone an operator drew.

This is a classifier over the detection stream that already exists, not a second
pipeline. Every detection the ANPR path writes is already the product of decoding,
gating, detection and recognition; asking "and was it inside the gate zone?" costs one
indexed geometry test and no additional frames.

The test is deliberately the centroid of the vehicle box rather than any overlap with
it. A box that clips the corner of a zone is a vehicle passing the zone, and alerting on
that fills the desk with events an operator learns to dismiss -- which is worse than not
alerting at all, because it trains them to dismiss the real ones too. The centre of the
vehicle being inside the region is the claim worth making.

Coordinates are frame pixels, matching how the zone is stored; see migration 0006 for
why that is not a geographic geometry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.registry.enums import AlertState
from services.registry.models import Alert, Camera, Detection

log = logging.getLogger(__name__)

#: How long one vehicle sitting in a zone stays a single alert.
#:
#: Same shape and the same reasoning as the watchlist matcher's DEDUP_WINDOW: a vehicle
#: parked inside a zone produces a detection every few seconds, and one alert per
#: detection is a denial-of-service on the operator's attention. The alert instead
#: carries an observation count, so "still there, 47 sightings" is one row that grows.
ZONE_COOLDOWN = timedelta(minutes=5)

#: Deterministic geometric containment; there is no partial credit to express.
ZONE_MATCH_SCORE = 1.0

MATCH_TYPE = "zone_intrusion"


@dataclass(frozen=True)
class ZoneHit:
    zone_id: Any
    zone_name: str
    centroid_x: float
    centroid_y: float


def bbox_centroid(bbox: dict[str, Any] | None) -> tuple[float, float] | None:
    """Centre of a detection's vehicle box, or None when there is no box.

    A detection without a box is not an error: the plate was read, but the vehicle it
    belongs to was not localised. It simply cannot be placed in a zone, and inventing a
    position for it would be the kind of guess this project refuses elsewhere.
    """
    if not bbox:
        return None
    try:
        x1, y1 = float(bbox["x1"]), float(bbox["y1"])
        x2, y2 = float(bbox["x2"]), float(bbox["y2"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def zones_containing(session: Session, detection: Detection) -> list[ZoneHit]:
    """Active zones on this detection's camera that contain the vehicle centroid."""
    centre = bbox_centroid(detection.vehicle_bbox)
    if centre is None:
        return []
    x, y = centre

    rows = session.execute(
        text(
            """
            SELECT id, name
            FROM camera_zone
            WHERE camera_id = :camera_id
              AND active
              AND ST_Contains(polygon, ST_SetSRID(ST_Point(:x, :y), 0))
            ORDER BY name
            """
        ),
        {"camera_id": str(detection.camera_id), "x": x, "y": y},
    ).mappings()

    return [ZoneHit(zone_id=r["id"], zone_name=r["name"], centroid_x=x, centroid_y=y) for r in rows]


def raise_or_update_zone_alert(
    session: Session, detection: Detection, hit: ZoneHit
) -> tuple[Alert, str]:
    """Create a zone alert, or fold this sighting into the open one.

    Returns (alert, action) with action 'created' or 'deduplicated', matching the
    watchlist matcher's contract so the API can push both over the same socket.
    """
    observed = detection.observed_at_utc
    plate = detection.plate_normalised or detection.plate_raw or "UNKNOWN"

    existing = session.execute(
        select(Alert)
        .where(
            Alert.camera_id == detection.camera_id,
            Alert.matched_value == plate,
            Alert.match_type == MATCH_TYPE,
            Alert.state != AlertState.RESOLVED.value,
            Alert.dedup_window_start > observed - ZONE_COOLDOWN,
        )
        .order_by(Alert.raised_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    camera = session.get(Camera, detection.camera_id)
    sighting = {
        "camera_ref": camera.camera_ref if camera else None,
        "camera_name": camera.name if camera else None,
        "observed_at_utc": observed.isoformat(),
        "zone": hit.zone_name,
        "centroid": [round(hit.centroid_x, 1), round(hit.centroid_y, 1)],
        "crop_path": detection.crop_path,
    }

    if existing is not None:
        existing.observation_count += 1
        existing.sightings = [*existing.sightings, sighting]
        return existing, "deduplicated"

    alert = Alert(
        watchlist_entry_id=None,
        camera_id=detection.camera_id,
        detection_id=detection.id,
        matched_value=plate,
        match_type=MATCH_TYPE,
        match_score=ZONE_MATCH_SCORE,
        # Above a fuzzy plate match and below a confirmed stolen-vehicle hit: an
        # intrusion is a place-based fact, not an identity-based one.
        priority=0.7,
        observed_at_utc=observed,
        dedup_window_start=observed,
        observation_count=1,
        state=AlertState.RAISED.value,
        sightings=[sighting],
    )
    session.add(alert)
    return alert, "created"


def evaluate_zones(session: Session, detection: Detection) -> list[tuple[Alert, str]]:
    """Every zone this detection fell inside, as alerts."""
    out: list[tuple[Alert, str]] = []
    for hit in zones_containing(session, detection):
        out.append(raise_or_update_zone_alert(session, detection, hit))
    return out
