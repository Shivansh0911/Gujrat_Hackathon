"""Find a vehicle across the estate when nobody ever read its plate.

The journey screen answers "where did registration X go". That question needs a
registration, and on this estate most vehicles never produce one -- the plate occupies
too few pixels for any recogniser to resolve. An investigator who saw a vehicle on one
camera and wants it found on the others has, until now, had nothing to type.

This is the other direction. The operator picks a vehicle *out of the footage* -- any
vehicle the detector recorded, plate or no plate -- and the estate is searched by what
it looks like. No registration is involved at any point.

Why this is not just the candidate journey with a different entrance
--------------------------------------------------------------------
`candidate_journey` anchors on a confirmed plate read and proposes where that known
vehicle also appeared. Everything it returns inherits credibility from that read. Here
there is no read, so nothing can inherit anything, and the ceiling on what the result
may claim is lower:

**Every match here is a Candidate, always.** Not sometimes, not when the score is low
-- always. `tier_for` would happily return Probable for a strong appearance-plus-colour
agreement, and that is correct when a plate anchors the chain and wrong when nothing
does. Two vehicles that look alike, with no registration linking either of them to
anything, are two vehicles that look alike. The downgrade is applied after scoring
rather than by weakening the scorer, so the score still means what it means elsewhere
and only the claim attached to it changes.

What is deliberately reused
---------------------------
All of it: the pgvector neighbour search, the 0.05 distance gate chosen from the
measured false-link rate, the physical-plausibility bound from `speed.py`, and the
five-component score. Re-deriving any of those here would create a second set of
thresholds that drift from the measured ones, and the measurement is the only reason
to trust either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.analytics.candidate_journey import (
    MAX_CANDIDATE_DISTANCE,
    MIN_SCORE,
    Candidate,
    Seed,
    expand_from_seed,
)
from services.registry.enums import EvidenceTier

#: Default page size when browsing. Large enough to scan, small enough that the crops
#: behind it do not make the first paint slow on a shared connection.
BROWSE_LIMIT = 60

CAVEAT = (
    "This search is anchored on an appearance, not on a registration, so every result "
    "is a Candidate and none of them can become anything else. The measured false-link "
    "rate at this threshold is 0.055 and precision is 0.356 on the project's own "
    "footage -- roughly two proposals in three are wrong. Both crops are shown so each "
    "can be rejected by eye. A match here is a lead to check, never an identification."
)


@dataclass
class SightingBrief:
    """One vehicle the detector recorded, as the operator browses them."""

    sighting_id: uuid.UUID
    observed_at_utc: datetime
    camera_id: uuid.UUID
    camera_ref: str
    camera_name: str
    vehicle_class: str
    dominant_colour_lab: dict[str, float] | None
    quality_score: float
    crop_path: str | None
    #: The registration, when a plate read happened to be linked to this vehicle. Most
    #: of the time it is None, which is the entire reason this screen exists.
    plate_read: str | None
    has_fingerprint: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "sighting_id": str(self.sighting_id),
            "observed_at_utc": self.observed_at_utc.isoformat(),
            "camera_id": str(self.camera_id),
            "camera_ref": self.camera_ref,
            "camera_name": self.camera_name,
            "vehicle_class": self.vehicle_class,
            "dominant_colour_lab": self.dominant_colour_lab,
            "quality_score": self.quality_score,
            "crop_path": self.crop_path,
            "plate_read": self.plate_read,
            "has_fingerprint": self.has_fingerprint,
        }


_BROWSE = text(
    """
    SELECT s.id,
           s.observed_at_utc,
           s.camera_id,
           c.camera_ref,
           c.name AS camera_name,
           s.vehicle_class,
           s.dominant_colour_lab,
           s.quality_score,
           s.crop_path,
           d.plate_normalised AS plate_read,
           (s.appearance_vector IS NOT NULL) AS has_fingerprint
    FROM vehicle_sighting s
    JOIN camera c ON c.id = s.camera_id
    LEFT JOIN detection d ON d.id = s.plate_detection_id
    WHERE s.observed_at_utc >= :from_ AND s.observed_at_utc < :to
      AND s.camera_id = ANY(CAST(:camera_ids AS uuid[]))
      -- Every optional parameter is cast. Postgres infers a parameter's type from
      -- how it is used, and `$4 IS NULL` constrains nothing, so a nullable filter
      -- used this way fails with "could not determine data type of parameter" before
      -- a single row is read. The same shape bit this project once before, in the
      -- journey SQL, and the fix is the same: say what the type is.
      AND (CAST(:vehicle_class AS text) IS NULL
           OR lower(s.vehicle_class) = lower(CAST(:vehicle_class AS text)))
      AND (CAST(:camera_ref AS text) IS NULL OR c.camera_ref = CAST(:camera_ref AS text))
      AND (NOT CAST(:fingerprinted_only AS boolean) OR s.appearance_vector IS NOT NULL)
    -- Best crops first. A browse screen whose top row is unreadable teaches the
    -- operator that the whole feature is unreliable, when in fact they were shown the
    -- worst of it.
    ORDER BY s.quality_score DESC, s.observed_at_utc DESC
    LIMIT :limit OFFSET :offset
    """
)

_ANCHOR = text(
    """
    SELECT s.id,
           s.observed_at_utc,
           s.camera_id,
           c.camera_ref,
           c.name AS camera_name,
           s.appearance_vector::text AS vector,
           s.appearance_kind,
           s.vehicle_class,
           s.dominant_colour_lab,
           s.quality_score,
           s.crop_path,
           d.plate_normalised AS plate_read
    FROM vehicle_sighting s
    JOIN camera c ON c.id = s.camera_id
    LEFT JOIN detection d ON d.id = s.plate_detection_id
    WHERE s.id = :sighting_id
      AND s.camera_id = ANY(CAST(:camera_ids AS uuid[]))
    """
)


def browse_sightings(
    session: Session,
    *,
    camera_ids: list[Any],
    from_: datetime,
    to: datetime,
    vehicle_class: str | None = None,
    camera_ref: str | None = None,
    fingerprinted_only: bool = True,
    limit: int = BROWSE_LIMIT,
    offset: int = 0,
) -> list[SightingBrief]:
    """Vehicles the detector recorded, for an operator to pick one out of.

    `fingerprinted_only` defaults to true because a sighting with no descriptor cannot
    be searched from, and offering it as a starting point produces an empty result the
    operator has no way to explain.
    """
    if not camera_ids:
        return []
    rows = (
        session.execute(
            _BROWSE,
            {
                "camera_ids": [str(c) for c in camera_ids],
                "from_": from_,
                "to": to,
                "vehicle_class": vehicle_class,
                "camera_ref": camera_ref,
                "fingerprinted_only": fingerprinted_only,
                "limit": limit,
                "offset": offset,
            },
        )
        .mappings()
        .all()
    )
    return [
        SightingBrief(
            sighting_id=row["id"],
            observed_at_utc=row["observed_at_utc"],
            camera_id=row["camera_id"],
            camera_ref=row["camera_ref"],
            camera_name=row["camera_name"],
            vehicle_class=row["vehicle_class"],
            dominant_colour_lab=row["dominant_colour_lab"],
            quality_score=float(row["quality_score"]),
            crop_path=row["crop_path"],
            plate_read=row["plate_read"],
            has_fingerprint=bool(row["has_fingerprint"]),
        )
        for row in rows
    ]


@dataclass
class SearchResult:
    anchor: SightingBrief
    matches: list[Candidate]
    appearance_kind: str
    max_distance: float
    min_score: float
    caveat: str = CAVEAT


class AnchorUnavailable(LookupError):
    """The sighting cannot start a search, with a reason fit to show an operator."""


def find_similar(
    session: Session,
    *,
    sighting_id: uuid.UUID,
    camera_ids: list[Any],
    from_: datetime,
    to: datetime,
    max_speed_urban_kmph: float,
    max_speed_highway_kmph: float,
    limit: int = 24,
    min_score: float = MIN_SCORE,
) -> SearchResult:
    """Find the same vehicle on other cameras, starting from one sighting.

    Raises `AnchorUnavailable` rather than returning an empty result when the chosen
    sighting cannot anchor a search. "Nothing matched" and "this one could never have
    matched anything" are different answers and an operator has to be able to tell them
    apart -- the same distinction `plate_linking` got wrong by reporting zero links
    without having looked.
    """
    if not camera_ids:
        raise AnchorUnavailable("no cameras are visible to this account")

    row = (
        session.execute(
            _ANCHOR,
            {"sighting_id": str(sighting_id), "camera_ids": [str(c) for c in camera_ids]},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise AnchorUnavailable("that sighting does not exist, or is not visible to this account")
    if row["vector"] is None:
        raise AnchorUnavailable(
            "that vehicle has no appearance fingerprint, so there is nothing to search "
            "with. Fingerprints are computed at ingest; a sighting recorded before "
            "appearance analytics were enabled will not have one."
        )

    anchor = SightingBrief(
        sighting_id=row["id"],
        observed_at_utc=row["observed_at_utc"],
        camera_id=row["camera_id"],
        camera_ref=row["camera_ref"],
        camera_name=row["camera_name"],
        vehicle_class=row["vehicle_class"],
        dominant_colour_lab=row["dominant_colour_lab"],
        quality_score=float(row["quality_score"]),
        crop_path=row["crop_path"],
        plate_read=row["plate_read"],
        has_fingerprint=True,
    )

    # A Seed carries a plate because the journey path always has one. Here there is no
    # registration, and `exact=False` keeps the structure honest: nothing about this
    # anchor is confirmed. The tier every match ends up with is forced below.
    seed = Seed(
        sighting_id=anchor.sighting_id,
        observed_at_utc=anchor.observed_at_utc,
        camera_id=anchor.camera_id,
        camera_ref=anchor.camera_ref,
        vector=row["vector"],
        vehicle_class=anchor.vehicle_class,
        dominant_colour_lab=anchor.dominant_colour_lab,
        quality_score=anchor.quality_score,
        crop_path=anchor.crop_path,
        plate_read=anchor.plate_read or "",
        exact=False,
    )

    found = expand_from_seed(
        session,
        seed,
        kind=str(row["appearance_kind"]),
        from_=from_,
        to=to,
        max_speed_urban_kmph=max_speed_urban_kmph,
        max_speed_highway_kmph=max_speed_highway_kmph,
        limit=limit,
        min_score=min_score,
    )

    # Downgrade after scoring, never by weakening the scorer. The number still means
    # what it means on the journey screen; only the claim attached to it changes,
    # because an appearance anchored on no registration cannot support a stronger one.
    matches = [c if c.tier is EvidenceTier.CANDIDATE else _as_candidate(c) for c in found]

    return SearchResult(
        anchor=anchor,
        matches=matches,
        appearance_kind=str(row["appearance_kind"]),
        max_distance=MAX_CANDIDATE_DISTANCE,
        min_score=min_score,
    )


def _as_candidate(candidate: Candidate) -> Candidate:
    """The same match, with its tier lowered to Candidate.

    `Candidate` is frozen, so this rebuilds rather than mutates -- which is the right
    shape anyway: the original is what the journey path would have returned, and this
    is what a plateless search is allowed to say.
    """
    from dataclasses import replace

    return replace(candidate, tier=EvidenceTier.CANDIDATE)


__all__ = [
    "BROWSE_LIMIT",
    "CAVEAT",
    "AnchorUnavailable",
    "SearchResult",
    "SightingBrief",
    "browse_sightings",
    "find_similar",
]
