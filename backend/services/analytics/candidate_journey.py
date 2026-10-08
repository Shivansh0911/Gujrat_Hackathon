"""Expanding a journey by appearance when the plate is only legible once.

The problem this solves
-----------------------
The scored test case hands us a registration and asks where that vehicle went. On the
government estate the plate is legible roughly once per 3,900 frames (DISCOVERY finding
22), so a plate-equality journey returns one hop, or none. One hop is not a route.

But one hop is enough to *start* from. A single legible read anchors the vehicle's
appearance; from there the question becomes "where else did something that looks like
this go, within the bounds of what a vehicle can physically do". That is a search over
`vehicle_sighting`, which has a row for every vehicle the cameras saw rather than only
the ones whose plate resolved.

What this is not
----------------
It is not a claim that the candidate is the vehicle. Every result carries
`EvidenceTier.CANDIDATE` unless corroborated, every result carries its score and the
reasons behind it, and the console renders candidates in a visually distinct style that
can never be mistaken for a plate read. A judge must be able to disagree with the system
by looking at what it looked at, which means both crops, the distance, and the elapsed
time are returned alongside the number.

The scoring
-----------
A composite, not a single distance. Appearance alone is the weakest of the signals and
is deliberately not allowed to carry a candidate on its own:

  appearance   (0.45)  cosine distance between fingerprints, as pgvector computes it
  quality      (0.20)  geometric mean of both crops' quality scores
  class        (0.15)  same vehicle class, or a penalty
  colour       (0.12)  CIELAB distance between dominant colours, chroma-weighted
  plausibility (0.08)  how far inside the physical speed bound the hop sits

Quality is weighted heavily on purpose. A fingerprint from a 30x20 crop is close to
noise, and a scorer that ignored that would rank a lucky noise match above a solid one.
Weighting by the *geometric* mean rather than the arithmetic one means one bad crop
drags the pair down rather than being averaged away by a good one.

Plausibility is a factor, not a gate, only because the gate has already run: a candidate
that fails `speed.py`'s bound never reaches the scorer. What this term adds is a
preference for the comfortable hop over the barely-possible one.

These weights are reasoned, not yet fitted. §3.5 measures precision, recall and
false-link rate against hand-labelled pairs and the measured outcome -- including if it
contradicts the reasoning -- goes in DISCOVERY.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.registry.enums import EvidenceTier, EvidenceType, VehicleClass

log = logging.getLogger(__name__)

# --- Scoring weights (see the module docstring) ------------------------------------

W_APPEARANCE = 0.45
W_QUALITY = 0.20
W_CLASS = 0.15
W_COLOUR = 0.12
W_PLAUSIBILITY = 0.08

#: The scale the appearance term is normalised against, **not** the gate -- see
#: `MAX_CANDIDATE_DISTANCE` below for the gate. A cosine distance of 0.60 maps to an
#: appearance score of 0, which puts the measured different-vehicle median (0.19) at
#: roughly 0.68 and the same-vehicle median (0.016) at roughly 0.97.
MAX_APPEARANCE_DISTANCE = 0.60

#: Cosine distance above which there is no candidate at all, whatever else agrees.
#:
#: **This is the single most consequential constant in the module, and it is measured.**
#:
#: It is a gate rather than a weight for two reasons. First, the weighted sum alone does
#: not hold the line: with appearance contributing exactly zero, two sharp crops of two
#: white cars on a comfortable hop still summed to 0.52, above the reporting floor, so
#: any white car would have been proposed for any other white car purely on
#: corroboration. Second, and worse, the first value chosen for it by reasoning was
#: wrong by an order of magnitude.
#:
#: Measured on the own-feed footage, using the tracker's own association as ground truth
#: -- views within one track are the same vehicle, views in different tracks are
#: different vehicles. 1,070 same-vehicle pairs against 24,581 different-vehicle pairs:
#:
#:     distance   recall   false-link   precision
#:        0.05     0.704       0.055        0.356
#:        0.10     0.861       0.213        0.149
#:        0.20     0.980       0.526        0.075
#:        0.39     0.993       0.834        0.049     <- the value reasoning produced
#:
#: The reasoned value would have linked five of every six pairs it proposed to the wrong
#: vehicle. 0.05 is chosen on the same principle as the ANPR publish floor: reporting a
#: wrong vehicle to an investigator is worse than reporting nothing, because nothing
#: prompts them to look again while a wrong answer sends them somewhere else entirely.
#:
#: Even at 0.05 the precision on this footage is 0.356. That number belongs in front of
#: a jury unprompted, and it is the reason an appearance match is never rendered as a
#: plate read. Two caveats, both of which cut against flattering the figure:
#:
#:   * The footage is a BRTS bus depot -- a fleet in one livery -- so the
#:     different-vehicle set is adversarially similar. That is also a realistic policing
#:     case (a road of white hatchbacks), so it is not discounted.
#:   * The deployed search additionally requires a *different camera* and physical
#:     reachability, neither of which this measurement models, so the operational
#:     false-link rate should be lower. It has not been measured, so it is not claimed.
#:
#: Quality gating was measured here too and did **not** help: raising the minimum crop
#: quality from 0.0 to 0.6 moved the false-link rate at 0.20 from 0.526 to 0.556, in the
#: wrong direction. See DISCOVERY.
MAX_CANDIDATE_DISTANCE = 0.05

#: CIELAB distance at which colour agreement contributes nothing. ~25 is roughly the
#: separation between two colours a person would name differently.
MAX_COLOUR_DISTANCE = 25.0

#: Below this composite score a candidate is not returned at all. Silence beats a list
#: of weak guesses: an investigator shown ten candidates at 0.2 learns nothing and may
#: act on the first one anyway.
#:
#: Raised from 0.45 once `MAX_CANDIDATE_DISTANCE` was measured. The two interact, and at
#: 0.45 they stopped interacting usefully: a pair that passes a 0.05 distance gate has
#: an appearance term of at least 0.917, contributing 0.413 of the 0.45 floor on its
#: own, so every gated pair arrived needing almost no corroboration and the floor did no
#: work. 0.55 restores it. A true pair with poor crops -- weak quality, class unknown,
#: colour merely close -- still scores around 0.61, while a pair whose every
#: corroborating signal disagrees lands near 0.46 and is dropped.
MIN_SCORE = 0.55

#: A candidate scoring at least this, *and* agreeing on class and colour, is promoted
#: from CANDIDATE to PROBABLE. Appearance alone never promotes -- the corroboration is
#: the point, and it is checked explicitly rather than folded into the score.
PROMOTE_SCORE = 0.78

#: How far either side of a seed to search. Matches `speed.SPEED_WINDOW`: beyond two
#: hours a pair of sightings is two journeys rather than one leg.
SEARCH_WINDOW = timedelta(hours=2)


@dataclass(frozen=True)
class Seed:
    """A sighting the journey is anchored on, because its plate was read."""

    sighting_id: uuid.UUID
    observed_at_utc: datetime
    camera_id: uuid.UUID
    camera_ref: str
    vector: str
    vehicle_class: str
    dominant_colour_lab: dict[str, float] | None
    quality_score: float
    crop_path: str | None
    plate_read: str
    exact: bool

    @property
    def tier(self) -> EvidenceTier:
        return EvidenceTier.CONFIRMED if self.exact else EvidenceTier.PROBABLE


@dataclass(frozen=True)
class ScoreBreakdown:
    """Why a candidate scored what it did. Returned, not just logged.

    A score without its components is an assertion. With them, a reviewer can see that a
    0.71 came from a strong colour match and a mediocre fingerprint, and decide for
    themselves whether that is worth acting on.
    """

    appearance: float
    quality: float
    vehicle_class: float
    colour: float
    plausibility: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "appearance": round(self.appearance, 4),
            "quality": round(self.quality, 4),
            "class": round(self.vehicle_class, 4),
            "colour": round(self.colour, 4),
            "plausibility": round(self.plausibility, 4),
            "total": round(self.total, 4),
        }


@dataclass(frozen=True)
class Candidate:
    """One sighting proposed as the same vehicle as a seed."""

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
    crop_path: str | None

    seed_sighting_id: uuid.UUID
    seed_crop_path: str | None
    appearance_distance: float
    distance_m: float
    seconds_from_seed: float
    implied_speed_kmph: float
    score: ScoreBreakdown
    tier: EvidenceTier
    evidence_type: EvidenceType = EvidenceType.APPEARANCE_REID


def _colour_distance(a: dict[str, float] | None, b: dict[str, float] | None) -> float | None:
    """CIELAB distance, chroma-weighted. None when either colour is missing.

    Lightness is halved for the same reason the descriptor down-weights it: it is the
    channel a streetlight moves, and two cameras on one corridor are lit differently.
    """
    if not a or not b:
        return None
    dl = (float(a.get("L", 0.0)) - float(b.get("L", 0.0))) * 0.5
    da = float(a.get("a", 0.0)) - float(b.get("a", 0.0))
    db = float(a.get("b", 0.0)) - float(b.get("b", 0.0))
    return float((dl * dl + da * da + db * db) ** 0.5)


def score_candidate(
    *,
    appearance_distance: float,
    seed_quality: float,
    candidate_quality: float,
    seed_class: str,
    candidate_class: str,
    seed_colour: dict[str, float] | None,
    candidate_colour: dict[str, float] | None,
    implied_speed_kmph: float,
    ceiling_kmph: float,
) -> ScoreBreakdown:
    """Combine the signals into one calibrated confidence in [0, 1]."""
    appearance = max(0.0, 1.0 - appearance_distance / MAX_APPEARANCE_DISTANCE)

    # Geometric mean: one poor crop drags the pair down instead of being averaged away.
    quality = float((max(0.0, seed_quality) * max(0.0, candidate_quality)) ** 0.5)

    if seed_class == candidate_class:
        class_term = 1.0
    elif VehicleClass.UNKNOWN.value in (seed_class, candidate_class):
        # An unknown class is an absence of evidence, not evidence of difference.
        class_term = 0.5
    else:
        # A car is not a bus. Not zero, because the classifier is wrong at CCTV angles
        # often enough that a hard veto would discard true links.
        class_term = 0.15

    colour_gap = _colour_distance(seed_colour, candidate_colour)
    colour = 0.5 if colour_gap is None else max(0.0, 1.0 - colour_gap / MAX_COLOUR_DISTANCE)

    plausibility = max(0.0, 1.0 - implied_speed_kmph / ceiling_kmph) if ceiling_kmph > 0 else 0.0

    total = (
        W_APPEARANCE * appearance
        + W_QUALITY * quality
        + W_CLASS * class_term
        + W_COLOUR * colour
        + W_PLAUSIBILITY * plausibility
    )

    # The gate. Corroboration does not substitute for similarity: without this, two
    # sharp crops of two white cars on a comfortable hop sum to 0.52 with the
    # fingerprints contributing nothing at all, and every white car becomes a candidate
    # for every other white car. The components are still returned so a reviewer can
    # see exactly which signal was missing.
    if appearance_distance > MAX_CANDIDATE_DISTANCE:
        total = 0.0

    return ScoreBreakdown(
        appearance=appearance,
        quality=quality,
        vehicle_class=class_term,
        colour=colour,
        plausibility=plausibility,
        total=min(1.0, total),
    )


def tier_for(score: ScoreBreakdown, *, class_agrees: bool, colour_agrees: bool) -> EvidenceTier:
    """CANDIDATE unless corroborated by something other than appearance.

    Promotion needs the score *and* independent agreement on class and colour. A very
    close fingerprint alone stays CANDIDATE however close it is, because the failure mode
    of appearance matching is two different vehicles that genuinely look alike, and a
    closer fingerprint does not make that less likely -- it makes it more so.
    """
    if score.total >= PROMOTE_SCORE and class_agrees and colour_agrees:
        return EvidenceTier.PROBABLE
    return EvidenceTier.CANDIDATE


_SEEDS = text(
    """
    SELECT s.id, s.observed_at_utc, s.camera_id, c.camera_ref,
           s.appearance_vector::text AS vector, s.vehicle_class,
           s.dominant_colour_lab, s.quality_score, s.crop_path,
           d.plate_normalised, d.confidence AS plate_confidence
    FROM vehicle_sighting s
    JOIN camera c    ON c.id = s.camera_id
    JOIN detection d ON d.id = s.plate_detection_id
                    AND d.observed_at_utc = s.plate_detection_at
    WHERE d.plate_normalised = :plate
      AND s.appearance_vector IS NOT NULL
      AND s.appearance_kind = :kind
      AND s.observed_at_utc >= :from_ AND s.observed_at_utc <= :to
    ORDER BY s.quality_score DESC
    """
)

#: Nearest neighbours of one seed, bounded by time and by having a comparable vector.
#:
#: The ORDER BY is on `<=>` so the HNSW index is used; adding a WHERE on the distance
#: instead would force a sequential scan. The distance bound is applied as a LIMIT plus
#: a Python-side cut, which is how pgvector is meant to be queried.
_NEIGHBOURS = text(
    """
    SELECT s.id, s.observed_at_utc, s.camera_id, c.camera_ref, c.name AS camera_name,
           s.vehicle_class, s.dominant_colour_lab, s.quality_score, s.crop_path,
           ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
           s.appearance_vector <=> CAST(:vector AS vector) AS distance,
           ST_Distance(c.geom, seed_cam.geom) AS metres
    FROM vehicle_sighting s
    JOIN camera c        ON c.id = s.camera_id
    JOIN camera seed_cam ON seed_cam.id = CAST(:seed_camera AS uuid)
    WHERE s.appearance_vector IS NOT NULL
      AND s.appearance_kind = :kind
      AND s.camera_id <> CAST(:seed_camera AS uuid)
      AND c.geom IS NOT NULL
      AND s.observed_at_utc >= :from_ AND s.observed_at_utc <= :to
    ORDER BY s.appearance_vector <=> CAST(:vector AS vector)
    LIMIT :limit
    """
)


def find_seeds(
    session: Session,
    *,
    plate: str,
    kind: str,
    from_: datetime,
    to: datetime,
) -> list[Seed]:
    """Sightings whose linked plate read matches, best crop first.

    Exact matches only here. Confusion-aware fuzzy seeding is the caller's job, because
    the confusion sets live in `plate_grammar` and encoding them in SQL would duplicate
    the rule -- the same reasoning `journey.py` already applies.
    """
    rows = (
        session.execute(_SEEDS, {"plate": plate, "kind": kind, "from_": from_, "to": to})
        .mappings()
        .all()
    )
    return [
        Seed(
            sighting_id=row["id"],
            observed_at_utc=row["observed_at_utc"],
            camera_id=row["camera_id"],
            camera_ref=row["camera_ref"],
            vector=row["vector"],
            vehicle_class=row["vehicle_class"],
            dominant_colour_lab=row["dominant_colour_lab"],
            quality_score=float(row["quality_score"]),
            crop_path=row["crop_path"],
            plate_read=row["plate_normalised"],
            exact=True,
        )
        for row in rows
    ]


def expand_from_seed(
    session: Session,
    seed: Seed,
    *,
    kind: str,
    from_: datetime,
    to: datetime,
    max_speed_urban_kmph: float,
    max_speed_highway_kmph: float,
    highway_distance_m: float = 5000.0,
    detour_factor: float = 1.3,
    limit: int = 50,
    min_score: float = MIN_SCORE,
) -> list[Candidate]:
    """Nearest neighbours of one seed, gated for physical plausibility and scored.

    The gate runs before the score, not after: a hop that implies 400 km/h is a different
    vehicle regardless of how similar it looks, and scoring it first would put an
    impossible hop at the top of the list on appearance alone.
    """
    window_start = max(from_, seed.observed_at_utc - SEARCH_WINDOW)
    window_end = min(to, seed.observed_at_utc + SEARCH_WINDOW)

    rows = (
        session.execute(
            _NEIGHBOURS,
            {
                "vector": seed.vector,
                "kind": kind,
                "seed_camera": str(seed.camera_id),
                "from_": window_start,
                "to": window_end,
                "limit": limit,
            },
        )
        .mappings()
        .all()
    )

    out: list[Candidate] = []
    for row in rows:
        distance = float(row["distance"])
        if distance > MAX_CANDIDATE_DISTANCE:
            # The results are distance-ordered, so everything after this is further and
            # would score zero at the gate anyway. Breaking here also means the measured
            # operating point bounds the work, not just the output.
            break

        delta_s = abs((row["observed_at_utc"] - seed.observed_at_utc).total_seconds())
        if delta_s <= 0:
            # The same vehicle cannot be at two cameras at one instant. Not a candidate,
            # and not an error either -- it is the clock-skew case, and §7.2's
            # cloned-plate detection is where it belongs.
            continue

        road_m = float(row["metres"] or 0.0) * detour_factor
        implied = (road_m / delta_s) * 3.6
        ceiling = max_speed_highway_kmph if road_m > highway_distance_m else max_speed_urban_kmph
        if implied > ceiling:
            continue

        colour_gap = _colour_distance(seed.dominant_colour_lab, row["dominant_colour_lab"])
        breakdown = score_candidate(
            appearance_distance=distance,
            seed_quality=seed.quality_score,
            candidate_quality=float(row["quality_score"]),
            seed_class=seed.vehicle_class,
            candidate_class=row["vehicle_class"],
            seed_colour=seed.dominant_colour_lab,
            candidate_colour=row["dominant_colour_lab"],
            implied_speed_kmph=implied,
            ceiling_kmph=ceiling,
        )
        if breakdown.total < min_score:
            continue

        out.append(
            Candidate(
                sighting_id=row["id"],
                observed_at_utc=row["observed_at_utc"],
                camera_id=row["camera_id"],
                camera_ref=row["camera_ref"],
                camera_name=row["camera_name"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                vehicle_class=row["vehicle_class"],
                dominant_colour_lab=row["dominant_colour_lab"],
                quality_score=float(row["quality_score"]),
                crop_path=row["crop_path"],
                seed_sighting_id=seed.sighting_id,
                seed_crop_path=seed.crop_path,
                appearance_distance=round(distance, 4),
                distance_m=round(road_m, 1),
                seconds_from_seed=round(delta_s, 2),
                implied_speed_kmph=round(implied, 1),
                score=breakdown,
                tier=tier_for(
                    breakdown,
                    class_agrees=seed.vehicle_class == row["vehicle_class"],
                    colour_agrees=colour_gap is not None and colour_gap <= MAX_COLOUR_DISTANCE / 2,
                ),
            )
        )

    out.sort(key=lambda c: c.score.total, reverse=True)
    return out


__all__ = [
    "MAX_APPEARANCE_DISTANCE",
    "MAX_CANDIDATE_DISTANCE",
    "MIN_SCORE",
    "PROMOTE_SCORE",
    "SEARCH_WINDOW",
    "Candidate",
    "ScoreBreakdown",
    "Seed",
    "expand_from_seed",
    "find_seeds",
    "score_candidate",
    "tier_for",
]
