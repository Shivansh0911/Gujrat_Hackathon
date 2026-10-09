"""Candidate journey expansion — tracing a vehicle whose plate is legible once.

`/journey` answers "where was this plate read?". On the government estate the answer is
usually one camera or none, because the plate resolves roughly once per 3,900 frames
(DISCOVERY finding 22). This endpoint answers the question the test case actually asks:
given that one read, where else did that vehicle go?

It is deliberately a separate endpoint rather than an extension of `/journey`. A journey
is a sequence of places a registration was *read*; this returns a ranked set of
*proposals*, each of which a human accepts or rejects. Merging them into one response
would put two different kinds of claim in one list, and the first thing a tired operator
does with one list is read it as one kind of claim.

Everything here carries its evidence. Each candidate returns both crops, the appearance
distance, the metres and seconds between the two cameras, the implied speed, and the
score broken into its five components -- so a reviewer can disagree with the system by
looking at what it looked at, rather than by trusting a number.

Accept and reject are audited. That is both an accountability control -- someone decided
this, and the ledger says who and why -- and, over time, a labelled dataset for improving
the scorer against real operator judgement rather than against the tracker's own
association.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.analytics.candidate_journey import (
    MAX_CANDIDATE_DISTANCE,
    MIN_SCORE,
    Candidate,
    expand_from_seed,
    find_seeds,
)
from services.analytics.model_ids import APPEARANCE_KIND, SIGHTING_VERSION
from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.api.media_signing import signed_media_url
from services.api.security import CurrentActor
from services.registry.enums import EvidenceTier

log = logging.getLogger(__name__)

router = APIRouter(tags=["candidates"])
SessionDep = Annotated[Session, Depends(get_session)]


class ScoreOut(BaseModel):
    """The five components, never just the total.

    A score without its parts is an assertion. With them a reviewer can see that a 0.71
    came from a strong colour match and a mediocre fingerprint, and decide for
    themselves whether that is worth acting on.
    """

    appearance: float
    quality: float
    vehicle_class: float = Field(alias="class")
    colour: float
    plausibility: float
    total: float

    model_config = {"populate_by_name": True}


class CandidateOut(BaseModel):
    sighting_id: uuid.UUID
    observed_at_utc: datetime
    camera_id: uuid.UUID
    camera_ref: str
    camera_name: str
    lat: float
    lon: float

    vehicle_class: str
    dominant_colour_lab: dict[str, float] | None
    quality_score: float

    #: Both crops, so the claim can be checked by eye. This is the non-negotiable part
    #: of the design: an appearance match that cannot be looked at is an assertion.
    crop_url: str | None
    seed_crop_url: str | None

    seed_sighting_id: uuid.UUID
    appearance_distance: float
    distance_m: float
    seconds_from_seed: float
    implied_speed_kmph: float
    score: ScoreOut

    #: Always `candidate` unless corroborated by class *and* colour, in which case
    #: `probable`. Never `confirmed` -- only a plate read confirms.
    tier: str
    evidence_type: str


class SeedOut(BaseModel):
    sighting_id: uuid.UUID
    observed_at_utc: datetime
    camera_ref: str
    camera_name: str
    plate_read: str
    quality_score: float
    crop_url: str | None
    tier: str


class CandidateJourneyResult(BaseModel):
    plate: str
    window_start: datetime
    window_end: datetime
    purpose: str
    requested_by: str

    seeds: list[SeedOut]
    candidates: list[CandidateOut]

    #: Stated in the response rather than left to documentation. An operator reading a
    #: short list should be able to see whether it is short because little matched or
    #: short because little was searched.
    sightings_searched: int
    appearance_kind: str
    max_candidate_distance: float
    min_score: float
    analytics_version: str
    query_ms: float

    #: Plain-language statement of what this result is and is not. Returned with every
    #: response so it travels wherever the result does -- including into a screenshot,
    #: which is where a caveat kept only in the UI chrome gets lost.
    caveat: str


_CAVEAT = (
    "Candidates are appearance matches, not identifications. On the project's own "
    "footage the measured false-link rate at this threshold is 0.055 and precision is "
    "0.356 -- roughly two of three proposals are wrong. Each is shown with both crops "
    "so it can be checked by eye. Only a plate read confirms a vehicle."
)


#: Appended when a result is built from the replay harness. Without it the figures
#: flatter the method badly: all four REPLAY cameras play the same clip, so two
#: "different cameras" can hold byte-identical frames and the cosine distance between
#: their descriptors comes out at exactly 0.0000. On footage from genuinely different
#: cameras it never does. A reviewer who sees a perfect score and only later learns the
#: inputs were the same file has been misled, whether or not anyone intended it.
_REPLAY_CAVEAT = (
    " Note: this result involves REPLAY- cameras, which replay the same recorded clip "
    "at different places on the map. Two of them can therefore hold identical frames, "
    "which is why an appearance distance of 0.0000 appears here and would not on "
    "footage from genuinely different cameras."
)


def _involves_replay(seeds: Sequence[object], candidates: Sequence[Candidate]) -> bool:
    """Whether any camera in this result is part of the replay harness.

    `getattr` on the seeds because `find_seeds` returns rows rather than a declared
    type; the candidates are typed, so they are read directly.
    """
    refs = [str(getattr(seed, "camera_ref", "") or "") for seed in seeds]
    refs += [candidate.camera_ref or "" for candidate in candidates]
    return any(ref.startswith("REPLAY-") for ref in refs)


def _crop_url(path: str | None) -> str | None:
    if not path:
        return None
    return signed_media_url("/media/crops", path, get_api_settings().jwt_secret)


def _to_out(candidate: Candidate) -> CandidateOut:
    return CandidateOut(
        sighting_id=candidate.sighting_id,
        observed_at_utc=candidate.observed_at_utc,
        camera_id=candidate.camera_id,
        camera_ref=candidate.camera_ref,
        camera_name=candidate.camera_name,
        lat=candidate.lat,
        lon=candidate.lon,
        vehicle_class=candidate.vehicle_class,
        dominant_colour_lab=candidate.dominant_colour_lab,
        quality_score=candidate.quality_score,
        crop_url=_crop_url(candidate.crop_path),
        seed_crop_url=_crop_url(candidate.seed_crop_path),
        seed_sighting_id=candidate.seed_sighting_id,
        appearance_distance=candidate.appearance_distance,
        distance_m=candidate.distance_m,
        seconds_from_seed=candidate.seconds_from_seed,
        implied_speed_kmph=candidate.implied_speed_kmph,
        score=ScoreOut(**candidate.score.as_dict()),
        tier=candidate.tier.value,
        evidence_type=candidate.evidence_type.value,
    )


@router.get("/journey/candidates", response_model=CandidateJourneyResult)
def candidate_journey(
    session: SessionDep,
    actor: CurrentActor,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    plate: str = Query(min_length=4, max_length=32),
    from_: datetime = Query(alias="from"),
    to: datetime = Query(),
    purpose: str = Query(min_length=8, max_length=500),
    limit_per_seed: int = Query(default=25, ge=1, le=100),
) -> CandidateJourneyResult:
    """Expand a plate read into appearance candidates across the camera network.

    `purpose` is mandatory and audited before the search runs, for the same reason
    `/journey` does it: a query that returns nothing must be logged exactly like one
    that returns results, or the most sensitive case -- someone searching for a plate
    they should not be -- is the one that leaves no trace.
    """
    started = datetime.now(timezone.utc)
    normalised = plate.upper().replace(" ", "").replace("-", "")

    if to <= from_:
        raise HTTPException(status_code=422, detail="'to' must be after 'from'")

    audit.append(
        session,
        action="QUERY_CANDIDATES",
        subject_type="plate",
        subject_id=normalised,
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=purpose,
        detail={
            "window_start": from_.isoformat(),
            "window_end": to.isoformat(),
            "appearance_kind": APPEARANCE_KIND,
            "max_candidate_distance": MAX_CANDIDATE_DISTANCE,
        },
    )
    session.flush()

    searched = int(
        session.execute(
            text(
                "SELECT count(*) FROM vehicle_sighting"
                " WHERE appearance_vector IS NOT NULL AND appearance_kind = :kind"
                "   AND observed_at_utc >= :from_ AND observed_at_utc <= :to"
            ),
            {"kind": APPEARANCE_KIND, "from_": from_, "to": to},
        ).scalar_one()
    )

    seeds = find_seeds(session, plate=normalised, kind=APPEARANCE_KIND, from_=from_, to=to)

    # One candidate per sighting, keeping the best-scoring seed that proposed it. Two
    # seeds of the same vehicle will often propose the same third sighting, and listing
    # it twice would make a single piece of evidence look like two.
    best: dict[uuid.UUID, Candidate] = {}
    for seed in seeds:
        for candidate in expand_from_seed(
            session,
            seed,
            kind=APPEARANCE_KIND,
            from_=from_,
            to=to,
            max_speed_urban_kmph=settings.max_speed_urban_kmph,
            max_speed_highway_kmph=settings.max_speed_highway_kmph,
            detour_factor=settings.detour_factor,
            limit=limit_per_seed,
        ):
            existing = best.get(candidate.sighting_id)
            if existing is None or candidate.score.total > existing.score.total:
                best[candidate.sighting_id] = candidate

    candidates = sorted(best.values(), key=lambda c: c.score.total, reverse=True)

    seed_rows = [
        SeedOut(
            sighting_id=s.sighting_id,
            observed_at_utc=s.observed_at_utc,
            camera_ref=s.camera_ref,
            camera_name=s.camera_ref,
            plate_read=s.plate_read,
            quality_score=s.quality_score,
            crop_url=_crop_url(s.crop_path),
            tier=s.tier.value,
        )
        for s in seeds
    ]

    elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
    return CandidateJourneyResult(
        plate=normalised,
        window_start=from_,
        window_end=to,
        purpose=purpose,
        requested_by=actor.subject,
        seeds=seed_rows,
        candidates=[_to_out(c) for c in candidates],
        sightings_searched=searched,
        appearance_kind=APPEARANCE_KIND,
        max_candidate_distance=MAX_CANDIDATE_DISTANCE,
        min_score=MIN_SCORE,
        analytics_version=SIGHTING_VERSION,
        query_ms=round(elapsed_ms, 2),
        caveat=_CAVEAT + (_REPLAY_CAVEAT if _involves_replay(seeds, candidates) else ""),
    )


class AdjudicationIn(BaseModel):
    sighting_id: uuid.UUID
    seed_sighting_id: uuid.UUID
    plate: str = Field(min_length=4, max_length=32)
    accepted: bool
    purpose: str = Field(min_length=8, max_length=500)
    #: What the operator saw that the scorer did not. Optional, and worth more than the
    #: boolean when present.
    note: str | None = Field(default=None, max_length=1000)


class AdjudicationOut(BaseModel):
    recorded: bool
    audit_seq: int | None


@router.post("/journey/candidates/adjudicate", response_model=AdjudicationOut)
def adjudicate_candidate(
    session: SessionDep,
    actor: CurrentActor,
    body: AdjudicationIn,
) -> AdjudicationOut:
    """Record an operator accepting or rejecting one candidate.

    Written to the hash-chained ledger rather than to a mutable column, because the
    value of the record is that it cannot be quietly revised afterwards. A decision that
    sent an investigation one way should still be visible if the investigation later
    went the other way.

    Nothing about the candidate changes in the database. An accepted candidate is not
    promoted to a confirmed hop, and this endpoint deliberately offers no way to do
    that: only a plate read confirms a vehicle, and an operator agreeing with a
    fingerprint is not a plate read.
    """
    normalised = body.plate.upper().replace(" ", "").replace("-", "")

    exists = session.execute(
        text("SELECT 1 FROM vehicle_sighting WHERE id = :id LIMIT 1"),
        {"id": body.sighting_id},
    ).scalar_one_or_none()
    if exists is None:
        # Also the RLS answer: a sighting in another department is simply not there.
        raise HTTPException(status_code=404, detail="no such sighting")

    entry = audit.append(
        session,
        action="ADJUDICATE_CANDIDATE",
        subject_type="vehicle_sighting",
        subject_id=str(body.sighting_id),
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=body.purpose,
        detail={
            "plate": normalised,
            "seed_sighting_id": str(body.seed_sighting_id),
            "accepted": body.accepted,
            "note": body.note,
            "tier_offered": EvidenceTier.CANDIDATE.value,
            "analytics_version": SIGHTING_VERSION,
        },
    )
    session.flush()

    return AdjudicationOut(recorded=True, audit_seq=entry.seq)


__all__: list[str] = ["router"]
