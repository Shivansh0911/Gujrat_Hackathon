"""Route reconstruction — the capability the live test case is scored on.

A judge supplies a registration number; this returns where that vehicle went, when,
with the evidence for each sighting and an honest account of what we could not see.

Three things distinguish this from `SELECT * FROM detection WHERE plate = ?`:

**Plausibility gating.** Consecutive sightings imply a speed. A vehicle cannot cross
400 km in four minutes, so a hop implying that is a different vehicle with a similar
plate, and including it would fabricate a journey. Rejected hops are reported, not
silently dropped -- an investigator needs to know a candidate was considered.

**Tolerance scaled by coordinate confidence.** Nine of our cameras are located only
to a district centroid, with a 5 km confidence radius. Comparing one of those against
a 300 m-precise neighbour using nominal distance produces false impossible-speed
rejections and deletes real hops. The permitted distance therefore widens by the sum
of both endpoints' radii.

**Coverage gaps.** Where the route passes a camera that saw nothing, that is stated
explicitly. "The vehicle was not there" and "we could not see" are different
findings, and only one of them is a coverage problem worth fixing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.analytics.model_ids import MODEL_VERSION
from services.api.media_signing import signed_media_url
from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.api.security import CurrentActor
from services.registry.enums import GeomSource

log = logging.getLogger(__name__)

router = APIRouter(tags=["journey"])
SessionDep = Annotated[Session, Depends(get_session)]


class JourneyHop(BaseModel):
    seq: int
    camera_id: uuid.UUID
    camera_ref: str
    camera_name: str
    location_text: str
    lat: float
    lon: float
    geom_source: str
    confidence_radius_m: float | None

    observed_at_utc: datetime
    pts_ms: float
    clock_confidence: float

    plate_read: str
    evidence_type: str  # anpr_exact | anpr_fuzzy
    corrections: list[dict[str, Any]]
    confidence: float
    crop_url: str | None

    distance_from_prev_m: float | None = None
    seconds_from_prev: float | None = None
    implied_speed_kmph: float | None = None
    # True when the implied speed is only plausible once coordinate uncertainty is
    # taken into account. Surfaced so a reviewer can see the allowance was needed.
    within_tolerance_only: bool = False


class CoverageGap(BaseModel):
    after_seq: int
    camera_id: uuid.UUID
    camera_ref: str
    camera_name: str
    lat: float
    lon: float
    reason: str


class RejectedHop(BaseModel):
    camera_ref: str
    observed_at_utc: datetime
    implied_speed_kmph: float | None
    reason: str


class JourneyResult(BaseModel):
    plate: str
    window_start: datetime
    window_end: datetime
    purpose: str
    requested_by: str

    hops: list[JourneyHop]
    coverage_gaps: list[CoverageGap]
    rejected: list[RejectedHop]

    total_distance_m: float
    duration_s: float
    confidence: float
    # Distinguishes "this plate has never been seen at all" from "seen, but not in
    # the requested window". Different findings for an investigator.
    plate_ever_seen: bool
    query_ms: float
    cameras_excluded_no_coordinate: int


@dataclass
class _Sighting:
    camera_id: uuid.UUID
    camera_ref: str
    camera_name: str
    location_text: str
    lat: float
    lon: float
    geom_source: str
    radius_m: float | None
    observed_at_utc: datetime
    pts_ms: float
    clock_confidence: float
    plate_read: str
    corrections: list[dict[str, Any]]
    confidence: float
    crop_path: str | None
    exact: bool


def _crop_url(path: str | None) -> str | None:
    """A signed, expiring link to one evidence crop.

    Signed because the media endpoint is reachable without a session -- a browser
    cannot put an Authorization header on an <img> -- and these are photographs of
    vehicles and plates rather than public assets. See api/media_signing.py.
    """
    if not path:
        return None
    return signed_media_url("/media/crops", path, get_api_settings().jwt_secret)


@router.get("/journey", response_model=JourneyResult)
def reconstruct_journey(
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    plate: str = Query(min_length=4, max_length=32),
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    purpose: str = Query(min_length=8, max_length=500),
    fuzzy: bool = Query(default=True, description="include confusion-explained matches"),
) -> JourneyResult:
    """Reconstruct a vehicle's route across the camera network.

    `purpose` is mandatory and written to the audit ledger **before** the query runs,
    so a journey query that returns nothing is logged exactly as one that returns a
    route. Logging only successful queries would leave the most sensitive case --
    someone searching for a plate they should not be -- invisible.
    """
    started = datetime.now(timezone.utc)
    normalised = plate.upper().replace(" ", "").replace("-", "")

    if to <= from_:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")

    audit.append(
        session,
        action="QUERY_PLATE",
        subject_type="plate",
        subject_id=normalised,
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=purpose,
        detail={
            "window_start": from_.isoformat(),
            "window_end": to.isoformat(),
            "fuzzy_enabled": fuzzy,
        },
    )
    session.flush()

    # Cameras without a coordinate cannot contribute a hop, but the operator must be
    # told how many were excluded rather than left to assume full coverage.
    excluded = session.execute(
        text("SELECT count(*) FROM camera WHERE geom_source = :unset"),
        {"unset": GeomSource.UNSET.value},
    ).scalar_one()

    ever = session.execute(
        text("SELECT EXISTS (SELECT 1 FROM detection WHERE plate_normalised = :p)"),
        {"p": normalised},
    ).scalar_one()

    # Exact matches, plus same-length candidates for confusion-aware comparison.
    # Fuzzy filtering happens in Python because the confusion sets are ours, not the
    # database's, and encoding them in SQL would duplicate the rule.
    rows = (
        session.execute(
            text(
                """
            SELECT d.plate_normalised, d.observed_at_utc, d.pts_ms, d.confidence,
                   d.corrections, d.crop_path, d.clock_confidence,
                   c.id AS camera_id, c.camera_ref, c.name AS camera_name,
                   c.location_text, c.geom_source, c.confidence_radius_m,
                   ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon
            FROM detection d
            JOIN camera c ON c.id = d.camera_id
            WHERE d.observed_at_utc >= :from_ AND d.observed_at_utc <= :to
              AND c.geom IS NOT NULL
              AND (d.plate_normalised = :plate
                   OR (:fuzzy AND length(d.plate_normalised) = length(:plate)))
            ORDER BY d.observed_at_utc ASC
            """
            ),
            {"from_": from_, "to": to, "plate": normalised, "fuzzy": fuzzy},
        )
        .mappings()
        .all()
    )

    from services.analytics.plate_grammar import confusion_aware_distance

    sightings: list[_Sighting] = []
    for row in rows:
        read = row["plate_normalised"]
        exact = read == normalised
        if not exact:
            total, explained = confusion_aware_distance(read, normalised)
            # Every difference must be explained by a known confusion pair, and at
            # most two. Anything else is a different registration.
            if total == 0 or total > 2 or explained != total:
                continue
        sightings.append(
            _Sighting(
                camera_id=row["camera_id"],
                camera_ref=row["camera_ref"],
                camera_name=row["camera_name"],
                location_text=row["location_text"] or "",
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                geom_source=row["geom_source"],
                radius_m=float(row["confidence_radius_m"]) if row["confidence_radius_m"] else None,
                observed_at_utc=row["observed_at_utc"],
                pts_ms=float(row["pts_ms"] or 0),
                clock_confidence=float(row["clock_confidence"] or 1.0),
                plate_read=read,
                corrections=row["corrections"] or [],
                confidence=float(row["confidence"] or 0),
                crop_path=row["crop_path"],
                exact=exact,
            )
        )

    hops: list[JourneyHop] = []
    rejected: list[RejectedHop] = []
    total_distance = 0.0

    for candidate in sightings:
        if not hops:
            hops.append(_to_hop(candidate, 1))
            continue

        previous = hops[-1]
        distance_m = session.execute(
            text(
                "SELECT ST_Distance("
                "  ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326)::geography,"
                "  ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)::geography)"
            ),
            {
                "lon1": previous.lon,
                "lat1": previous.lat,
                "lon2": candidate.lon,
                "lat2": candidate.lat,
            },
        ).scalar_one()
        road_distance = float(distance_m) * settings.detour_factor

        delta_s = (candidate.observed_at_utc - previous.observed_at_utc).total_seconds()
        if delta_s <= 0:
            # Same instant at a different camera is physically impossible; keep the
            # first and record why the second was refused.
            rejected.append(
                RejectedHop(
                    camera_ref=candidate.camera_ref,
                    observed_at_utc=candidate.observed_at_utc,
                    implied_speed_kmph=None,
                    reason="not after the previous sighting",
                )
            )
            continue

        # Coordinate uncertainty widens the permitted distance. Without this, a
        # 5 km-radius district centroid is falsely judged impossible against a
        # 300 m-precise neighbour.
        tolerance_m = (previous.confidence_radius_m or 0.0) + (candidate.radius_m or 0.0)
        nominal_speed = (road_distance / delta_s) * 3.6
        tolerant_speed = (max(0.0, road_distance - tolerance_m) / delta_s) * 3.6

        ceiling = (
            settings.max_speed_highway_kmph
            if road_distance > 5000
            else settings.max_speed_urban_kmph
        )

        if tolerant_speed > ceiling:
            rejected.append(
                RejectedHop(
                    camera_ref=candidate.camera_ref,
                    observed_at_utc=candidate.observed_at_utc,
                    implied_speed_kmph=round(nominal_speed, 1),
                    reason=(
                        f"implied speed {tolerant_speed:.0f} km/h exceeds the "
                        f"{ceiling:.0f} km/h ceiling even allowing {tolerance_m:.0f} m "
                        "of coordinate uncertainty"
                    ),
                )
            )
            continue

        hop = _to_hop(candidate, len(hops) + 1)
        hop.distance_from_prev_m = round(road_distance, 1)
        hop.seconds_from_prev = round(delta_s, 2)
        hop.implied_speed_kmph = round(nominal_speed, 1)
        hop.within_tolerance_only = nominal_speed > ceiling >= tolerant_speed
        hops.append(hop)
        total_distance += road_distance

    gaps = _coverage_gaps(session, hops)

    duration = (
        (hops[-1].observed_at_utc - hops[0].observed_at_utc).total_seconds()
        if len(hops) > 1
        else 0.0
    )
    # Journey confidence is the mean per-hop confidence discounted by the share of
    # hops that needed a correction: a route pinned by clean reads deserves more
    # trust than one assembled from corrected ones.
    if hops:
        mean_conf = sum(h.confidence for h in hops) / len(hops)
        corrected_share = sum(1 for h in hops if h.corrections) / len(hops)
        confidence = round(max(0.0, mean_conf * (1.0 - 0.3 * corrected_share)), 4)
    else:
        confidence = 0.0

    elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0

    return JourneyResult(
        plate=normalised,
        window_start=from_,
        window_end=to,
        purpose=purpose,
        requested_by=actor.subject,
        hops=hops,
        coverage_gaps=gaps,
        rejected=rejected,
        total_distance_m=round(total_distance, 1),
        duration_s=round(duration, 2),
        confidence=confidence,
        plate_ever_seen=bool(ever),
        query_ms=round(elapsed_ms, 2),
        cameras_excluded_no_coordinate=int(excluded),
    )


def _to_hop(s: _Sighting, seq: int) -> JourneyHop:
    return JourneyHop(
        seq=seq,
        camera_id=s.camera_id,
        camera_ref=s.camera_ref,
        camera_name=s.camera_name,
        location_text=s.location_text,
        lat=s.lat,
        lon=s.lon,
        geom_source=s.geom_source,
        confidence_radius_m=s.radius_m,
        observed_at_utc=s.observed_at_utc,
        pts_ms=s.pts_ms,
        clock_confidence=s.clock_confidence,
        plate_read=s.plate_read,
        evidence_type="anpr_exact" if s.exact else "anpr_fuzzy",
        corrections=s.corrections,
        confidence=s.confidence,
        crop_url=_crop_url(s.crop_path),
    )


def _coverage_gaps(session: Session, hops: list[JourneyHop]) -> list[CoverageGap]:
    """Cameras lying between consecutive hops that produced no detection.

    This is the honest part of the output. A camera on the route that saw nothing is
    either a missed read or a coverage problem, and either way the operator should be
    looking at it rather than assuming the route is complete.
    """
    gaps: list[CoverageGap] = []
    for i in range(len(hops) - 1):
        a, b = hops[i], hops[i + 1]
        rows = (
            session.execute(
                text(
                    """
                SELECT c.id, c.camera_ref, c.name,
                       ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon
                FROM camera c
                WHERE c.geom IS NOT NULL
                  AND c.id NOT IN (:a, :b)
                  AND ST_DWithin(
                        c.geom,
                        ST_MakeLine(
                          ST_SetSRID(ST_MakePoint(:lon1, :lat1), 4326),
                          ST_SetSRID(ST_MakePoint(:lon2, :lat2), 4326)
                        )::geography,
                        :corridor_m)
                """
                ),
                {
                    "a": a.camera_id,
                    "b": b.camera_id,
                    "lon1": a.lon,
                    "lat1": a.lat,
                    "lon2": b.lon,
                    "lat2": b.lat,
                    # Corridor half-width. Wide enough to catch cameras genuinely on the
                    # route, narrow enough not to sweep in the whole district.
                    "corridor_m": 2000.0,
                },
            )
            .mappings()
            .all()
        )

        for row in rows:
            seen = session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM detection d
                        WHERE d.camera_id = :cid
                          AND d.observed_at_utc BETWEEN :t1 AND :t2
                    )
                    """
                ),
                {"cid": row["id"], "t1": a.observed_at_utc, "t2": b.observed_at_utc},
            ).scalar_one()
            if seen:
                continue
            gaps.append(
                CoverageGap(
                    after_seq=a.seq,
                    camera_id=row["id"],
                    camera_ref=row["camera_ref"],
                    camera_name=row["name"],
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    reason=f"no detection at {row['camera_ref']} - coverage gap",
                )
            )
    return gaps


@router.get("/journey/export", response_class=Response, tags=["journey"])
def export_journey_pdf(
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    plate: str = Query(min_length=4, max_length=32),
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    purpose: str = Query(min_length=8, max_length=500),
    fuzzy: bool = Query(default=True),
) -> Response:
    """Signed PDF evidence export for a journey.

    Runs the same reconstruction as GET /journey rather than accepting a client-
    supplied result: a document signed over whatever the caller posted would attest
    to nothing. The export is itself audited, separately from the query, because
    producing a distributable evidence document is a more consequential act than
    looking at a route on screen.
    """
    from services.api.evidence_export import export_journey

    result = reconstruct_journey(
        session=session,
        actor=actor,
        settings=settings,
        plate=plate,
        from_=from_,
        to=to,
        purpose=purpose,
        fuzzy=fuzzy,
    )

    entry = audit.append(
        session,
        action="EXPORT_EVIDENCE",
        subject_type="plate",
        subject_id=result.plate,
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=purpose,
        detail={
            "hops": len(result.hops),
            "coverage_gaps": len(result.coverage_gaps),
            "model_version": MODEL_VERSION,
            "window_start": from_.isoformat(),
            "window_end": to.isoformat(),
        },
    )
    session.flush()

    payload = result.model_dump(mode="json")
    pdf, manifest, signature, public_key = export_journey(
        payload, audit_seq=entry.seq, model_version=MODEL_VERSION
    )

    filename = f"setu-evidence-{result.plate}-{entry.seq}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The manifest and signature travel in headers so a recipient can verify
            # the document without a second request that might return a different
            # reconstruction.
            "X-SETU-Manifest-SHA256": __import__("hashlib").sha256(manifest).hexdigest(),
            "X-SETU-Signature": signature,
            "X-SETU-Public-Key": public_key,
            "X-SETU-Audit-Seq": str(entry.seq),
        },
    )
