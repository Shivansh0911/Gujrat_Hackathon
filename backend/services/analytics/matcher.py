"""Watchlist matching and alert generation.

Three decisions shape this module.

**Fuzzy matching is confusion-aware, not blind edit distance.** A one-character
difference at a position where the OCR confuses `8` for `B` is very likely the same
vehicle; the same distance between `K` and `X` is a different vehicle entirely.
Scoring by explained-versus-unexplained substitutions is what recovers matches an
exact-only system misses without flooding the operator with noise.

**Deduplication is a product decision, not an optimisation.** A vehicle parked in
view of one camera produces a detection every few seconds. Raising an alert for each
teaches the operator to dismiss alerts, which is the failure mode that matters.
Same plate, same camera, inside a window is one alert carrying a count.

**Successive sightings across cameras are one movement alert.** A vehicle crossing
the network is a single developing event. Grouping it is what makes the alert desk
readable when the subject is actually moving -- which is exactly when it matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.analytics import speed as speed_module
from services.analytics import zones as zones_module
from services.analytics.plate_grammar import confusion_aware_distance
from services.registry.enums import AlertState
from services.registry.models import Alert, Camera, Detection, WatchlistEntry

log = logging.getLogger(__name__)

# Same plate, same camera, inside this window is one alert with a count.
DEDUP_WINDOW = timedelta(minutes=5)

# A sighting at a different camera within this window extends the existing alert into
# a movement alert rather than raising a new one.
#
# Two hours, not thirty minutes. The window has to cover a realistic inter-city leg:
# Junagadh to Rajkot is roughly 100 km and takes over an hour, so a thirty-minute
# window classifies a vehicle genuinely crossing the state as a series of unrelated
# sightings -- which is precisely the case where grouping matters most to an operator.
MOVEMENT_WINDOW = timedelta(hours=2)

# An exact match is certainty about the read, not about the vehicle -- the OCR could
# still be wrong. Fuzzy scores are deliberately well below it.
SCORE_EXACT = 1.0
SCORE_FUZZY_ONE_EXPLAINED = 0.72
SCORE_FUZZY_TWO_EXPLAINED = 0.55

# Below this, a candidate is not surfaced at all. Set so that two explained
# substitutions still reach an operator but one unexplained difference does not.
MIN_MATCH_SCORE = 0.5


@dataclass
class MatchResult:
    entry: WatchlistEntry
    match_type: str  # 'exact' | 'fuzzy_1' | 'fuzzy_2'
    score: float
    corroboration: dict[str, Any] = field(default_factory=dict)

    @property
    def priority(self) -> float:
        """Combine watchlist priority, match confidence and corroboration.

        Watchlist priority dominates -- a stolen vehicle on a weak match still
        outranks a routine entry on a perfect one -- but corroboration can lift a
        fuzzy match into the range an operator will act on.
        """
        base = (self.entry.priority or 50) / 100.0
        agreed = sum(1 for v in self.corroboration.values() if v is True)
        conflicted = sum(1 for v in self.corroboration.values() if v is False)
        bonus = 0.05 * agreed - 0.10 * conflicted
        return round(max(0.0, min(1.0, base * 0.6 + self.score * 0.4 + bonus)), 4)


def _active_entries(session: Session, at: datetime) -> list[WatchlistEntry]:
    """Entries in force at `at`. An expired entry must never raise an alert."""
    return list(
        session.execute(
            select(WatchlistEntry).where(
                WatchlistEntry.active.is_(True),
                WatchlistEntry.valid_from <= at,
                WatchlistEntry.valid_to > at,
            )
        ).scalars()
    )


def _corroborate(entry: WatchlistEntry, detection: Detection) -> dict[str, Any]:
    """Compare vehicle attributes where both sides have them.

    Absence is recorded as None rather than as agreement: not knowing the colour is
    not the same as the colour matching, and collapsing the two would inflate
    confidence in exactly the cases where we know least.
    """
    attributes = (
        (detection.vehicle_bbox or {}).get("attributes", {}) if detection.vehicle_bbox else {}
    )
    out: dict[str, Any] = {}
    for field_name in ("colour", "make", "model"):
        expected = getattr(entry, field_name, None)
        observed = attributes.get(field_name)
        if not expected or not observed:
            out[field_name] = None
            continue
        out[field_name] = str(expected).lower() == str(observed).lower()
    return out


def match_detection(session: Session, detection: Detection) -> MatchResult | None:
    """Best watchlist match for one detection, or None."""
    plate = (detection.plate_normalised or "").strip().upper()
    if not plate:
        return None

    candidates: list[MatchResult] = []
    for entry in _active_entries(session, detection.observed_at_utc):
        target = (entry.plate_normalised or "").strip().upper()
        if not target:
            continue

        if target == plate:
            candidates.append(
                MatchResult(entry, "exact", SCORE_EXACT, _corroborate(entry, detection))
            )
            continue

        total, explained = confusion_aware_distance(plate, target)
        if total == 0 or total > 2:
            continue
        # Every differing character must be explained by a known confusion pair.
        # One unexplained substitution means a different registration, not a misread.
        if explained != total:
            continue

        score = SCORE_FUZZY_ONE_EXPLAINED if total == 1 else SCORE_FUZZY_TWO_EXPLAINED
        # A low-confidence OCR read makes a fuzzy match weaker still: the character
        # that differs is more likely to be the one we misread.
        score *= max(0.5, float(detection.confidence or 0.5))
        if score < MIN_MATCH_SCORE:
            continue
        candidates.append(
            MatchResult(entry, f"fuzzy_{total}", round(score, 4), _corroborate(entry, detection))
        )

    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.score, c.priority))


def _sighting(detection: Detection, camera: Camera | None) -> dict[str, Any]:
    return {
        "detection_id": str(detection.id),
        "camera_id": str(detection.camera_id),
        "camera_name": camera.name if camera else None,
        "plate": detection.plate_normalised,
        "observed_at_utc": detection.observed_at_utc.isoformat(),
        "pts_ms": detection.pts_ms,
        "confidence": detection.confidence,
        "crop_path": detection.crop_path,
    }


def raise_or_update_alert(
    session: Session, detection: Detection, match: MatchResult
) -> tuple[Alert, str]:
    """Create an alert, or fold this sighting into an existing one.

    Returns (alert, action) where action is 'created', 'deduplicated' or 'movement'.
    """
    camera = session.get(Camera, detection.camera_id)
    observed = detection.observed_at_utc

    # 1. Same plate, same camera, inside the dedup window -> one alert with a count.
    existing = session.execute(
        select(Alert)
        .where(
            Alert.matched_value == match.entry.plate_normalised,
            Alert.camera_id == detection.camera_id,
            Alert.state != AlertState.RESOLVED.value,
            Alert.dedup_window_start > observed - DEDUP_WINDOW,
        )
        .order_by(Alert.raised_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        existing.observation_count += 1
        existing.sightings = [*existing.sightings, _sighting(detection, camera)]
        # Keep the strongest evidence: a later, clearer read should improve the alert.
        if match.score > existing.match_score:
            existing.match_score = match.score
            existing.match_type = match.match_type
            existing.detection_id = detection.id
        return existing, "deduplicated"

    # 2. Same plate at a DIFFERENT camera recently -> the vehicle is moving.
    moving = session.execute(
        select(Alert)
        .where(
            Alert.matched_value == match.entry.plate_normalised,
            Alert.camera_id != detection.camera_id,
            Alert.state != AlertState.RESOLVED.value,
            Alert.dedup_window_start > observed - MOVEMENT_WINDOW,
        )
        .order_by(Alert.raised_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if moving is not None:
        moving.is_movement = True
        moving.observation_count += 1
        moving.sightings = [*moving.sightings, _sighting(detection, camera)]
        moving.observed_at_utc = observed
        # A subject confirmed to be moving is more urgent than a static sighting.
        moving.priority = min(1.0, moving.priority + 0.05)
        return moving, "movement"

    alert = Alert(
        watchlist_entry_id=match.entry.id,
        camera_id=detection.camera_id,
        detection_id=detection.id,
        matched_value=match.entry.plate_normalised or detection.plate_normalised,
        match_type=match.match_type,
        match_score=match.score,
        priority=match.priority,
        observed_at_utc=observed,
        dedup_window_start=observed,
        observation_count=1,
        state=AlertState.RAISED.value,
        sightings=[_sighting(detection, camera)],
        corroboration=match.corroboration,
        # Decode-to-alert latency: the detection carries when the frame was decoded,
        # so this is the real end-to-end number rather than a benchmark of the matcher.
        latency_ms=max(
            0.0,
            (datetime.now(timezone.utc) - detection.ingested_at_utc).total_seconds() * 1000.0,
        )
        if detection.ingested_at_utc
        else None,
    )
    session.add(alert)
    session.flush()
    return alert, "created"


@dataclass
class ScanStats:
    detections_scanned: int = 0
    matched: int = 0
    alerts_created: int = 0
    deduplicated: int = 0
    movement: int = 0
    zone_alerts: int = 0
    speed_alerts: int = 0


def scan_detections(
    session: Session,
    *,
    since: datetime | None = None,
    limit: int = 5000,
    on_alert: Callable[[Alert, str], None] | None = None,
    # Speed ceilings are passed in rather than imported, so this analytics module stays
    # free of the API's settings object and both callers -- the API and the seeding
    # script -- name the same numbers the journey reconstruction uses.
    max_speed_highway_kmph: float = 140.0,
    max_speed_urban_kmph: float = 90.0,
) -> ScanStats:
    """Match unprocessed detections against the watchlist and raise alerts.

    `on_alert(alert, action)` is called for each, so the API can push over WebSocket
    without this module knowing anything about transport.
    """
    stats = ScanStats()
    stmt = select(Detection).order_by(Detection.observed_at_utc).limit(limit)
    if since is not None:
        stmt = stmt.where(Detection.observed_at_utc >= since)

    for detection in session.execute(stmt).scalars():
        stats.detections_scanned += 1

        # Two further classifiers over the same detection. They are deliberately not
        # gated on a watchlist match: an unlisted vehicle inside a restricted zone, or
        # one travelling at 150 km/h, is exactly the case the watchlist cannot cover.
        for alert, action in zones_module.evaluate_zones(session, detection):
            if action == "created":
                stats.zone_alerts += 1
            if on_alert is not None:
                on_alert(alert, action)

        finding = speed_module.evaluate_speed(
            session,
            detection,
            max_speed_highway_kmph=max_speed_highway_kmph,
            max_speed_urban_kmph=max_speed_urban_kmph,
        )
        if finding is not None:
            alert, action = speed_module.raise_speed_alert(session, detection, finding)
            if action == "created":
                stats.speed_alerts += 1
            if on_alert is not None:
                on_alert(alert, action)

        match = match_detection(session, detection)
        if match is None:
            continue
        stats.matched += 1
        alert, action = raise_or_update_alert(session, detection, match)
        if action == "created":
            stats.alerts_created += 1
        elif action == "deduplicated":
            stats.deduplicated += 1
        else:
            stats.movement += 1
        if on_alert is not None:
            on_alert(alert, action)

    session.flush()
    return stats
