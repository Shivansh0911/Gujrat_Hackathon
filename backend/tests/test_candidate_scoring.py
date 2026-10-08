"""An appearance match must never be able to promote itself into a plate read.

The scorer decides what the console shows and what an officer acts on, so the tests that
matter most here are the ones about *restraint*: that a very close fingerprint alone
stays CANDIDATE, that a weak pair is dropped rather than listed, and that a poor crop
cannot carry a match on similarity alone.

Everything in this file is pure arithmetic over the scorer's inputs -- no database, no
model, no fixtures. That is deliberate: the weighting is the part a reviewer will argue
with, and it should be readable as a table of cases.
"""

from __future__ import annotations

import pytest

from services.analytics.candidate_journey import (
    MAX_APPEARANCE_DISTANCE,
    MAX_CANDIDATE_DISTANCE,
    MIN_SCORE,
    PROMOTE_SCORE,
    _colour_distance,
    score_candidate,
    tier_for,
)
from services.registry.enums import EvidenceTier, VehicleClass

WHITE = {"L": 85.0, "a": 0.5, "b": -1.0}
WHITE_DIMMER = {"L": 62.0, "a": 1.0, "b": -1.5}
RED = {"L": 40.0, "a": 55.0, "b": 38.0}


def _score(**over: object) -> object:
    base: dict[str, object] = {
        "appearance_distance": 0.05,
        "seed_quality": 0.9,
        "candidate_quality": 0.9,
        "seed_class": VehicleClass.CAR.value,
        "candidate_class": VehicleClass.CAR.value,
        "seed_colour": WHITE,
        "candidate_colour": WHITE,
        "implied_speed_kmph": 30.0,
        "ceiling_kmph": 80.0,
    }
    base.update(over)
    return score_candidate(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------- restraint


def test_a_perfect_fingerprint_alone_is_still_only_a_candidate() -> None:
    """The failure mode of appearance matching is two vehicles that genuinely look alike.

    A closer fingerprint does not make that less likely -- it makes it more so. So
    promotion requires corroboration that is not appearance, checked explicitly.
    """
    s = _score(appearance_distance=0.0)
    assert s.total >= PROMOTE_SCORE  # type: ignore[attr-defined]
    assert tier_for(s, class_agrees=False, colour_agrees=False) is EvidenceTier.CANDIDATE  # type: ignore[arg-type]
    assert tier_for(s, class_agrees=True, colour_agrees=False) is EvidenceTier.CANDIDATE  # type: ignore[arg-type]
    assert tier_for(s, class_agrees=False, colour_agrees=True) is EvidenceTier.CANDIDATE  # type: ignore[arg-type]


def test_promotion_needs_score_and_both_corroborations() -> None:
    s = _score(appearance_distance=0.0)
    assert tier_for(s, class_agrees=True, colour_agrees=True) is EvidenceTier.PROBABLE  # type: ignore[arg-type]


def test_a_high_score_below_the_promotion_bar_stays_a_candidate() -> None:
    """A pair that passes the gate but scores modestly must not be promoted.

    The distance is kept inside `MAX_CANDIDATE_DISTANCE` on purpose: a test that used a
    gated-out distance would assert CANDIDATE against a total of zero and pass for a
    reason unrelated to what it claims to check.
    """
    s = _score(appearance_distance=0.045, candidate_quality=0.15, seed_quality=0.2)
    assert s.total > 0.0, "the pair must pass the gate for this test to mean anything"  # type: ignore[attr-defined]
    assert s.total < PROMOTE_SCORE  # type: ignore[attr-defined]
    assert tier_for(s, class_agrees=True, colour_agrees=True) is EvidenceTier.CANDIDATE  # type: ignore[arg-type]


# ------------------------------------------------------------------------ quality


def test_one_poor_crop_drags_the_pair_down() -> None:
    """Geometric mean, not arithmetic: a good crop must not average away a bad one.

    A fingerprint from a 30x20 crop is close to noise. If quality were averaged, a
    pristine seed would hide that and a lucky noise match would outrank a solid one.
    """
    both_good = _score(seed_quality=0.9, candidate_quality=0.9)
    one_bad = _score(seed_quality=0.9, candidate_quality=0.1)
    arithmetic_equivalent = _score(seed_quality=0.5, candidate_quality=0.5)

    assert one_bad.total < both_good.total  # type: ignore[attr-defined]
    # Geometric mean of (0.9, 0.1) is 0.3, below the 0.5 an average would give.
    assert one_bad.quality < arithmetic_equivalent.quality  # type: ignore[attr-defined]


def test_corroboration_alone_cannot_produce_a_candidate() -> None:
    """Two white cars that do not look alike are not a candidate, however much agrees.

    This is the false-link generator the gate exists to stop. Everything else here is
    maximal -- both crops pristine, same class, same colour, a leisurely hop -- and the
    fingerprints contribute nothing. Without the gate this summed to 0.52, above the
    reporting floor, so any white car would have been proposed for any other white car.
    """
    s = _score(
        appearance_distance=MAX_APPEARANCE_DISTANCE,
        seed_quality=1.0,
        candidate_quality=1.0,
        implied_speed_kmph=5.0,
    )
    assert s.appearance == pytest.approx(0.0)  # type: ignore[attr-defined]
    # The components are still reported, so the missing signal is visible.
    assert s.quality == pytest.approx(1.0)  # type: ignore[attr-defined]
    assert s.vehicle_class == pytest.approx(1.0)  # type: ignore[attr-defined]
    assert s.total == 0.0  # type: ignore[attr-defined]


def test_the_appearance_gate_sits_at_the_measured_operating_point() -> None:
    """Just inside the gate scores; just outside is zero, not merely lower.

    The constant is pinned by measurement, not taste: at 0.05 the measured false-link
    rate on the own-feed footage is 0.055, while the value reasoning originally produced
    (0.39) measured 0.834. A change to this line changes what the platform is willing to
    tell an investigator, so it should be hard to make by accident.
    """
    assert MAX_CANDIDATE_DISTANCE == 0.05
    inside = _score(appearance_distance=MAX_CANDIDATE_DISTANCE * 0.98)
    outside = _score(appearance_distance=MAX_CANDIDATE_DISTANCE * 1.02)
    assert inside.total > 0.0  # type: ignore[attr-defined]
    assert outside.total == 0.0  # type: ignore[attr-defined]


def test_the_old_reasoned_gate_would_now_produce_nothing() -> None:
    """A regression guard on the specific mistake, not just on the constant.

    0.39 was the distance the original weighting admitted, and it linked five of every
    six pairs to the wrong vehicle. If a future change reopens that band, this fails.
    """
    assert _score(appearance_distance=0.39).total == 0.0  # type: ignore[attr-defined]


# -------------------------------------------------------------------------- class


def test_a_car_and_a_bus_are_penalised_but_not_vetoed() -> None:
    """The classifier is wrong at CCTV angles often enough that a veto loses true links."""
    same = _score(candidate_class=VehicleClass.CAR.value)
    different = _score(candidate_class=VehicleClass.BUS.value)
    assert different.total < same.total  # type: ignore[attr-defined]
    assert different.vehicle_class > 0.0, "a hard veto would discard true links"  # type: ignore[attr-defined]


def test_an_unknown_class_is_absence_of_evidence_not_evidence_of_difference() -> None:
    unknown = _score(candidate_class=VehicleClass.UNKNOWN.value)
    different = _score(candidate_class=VehicleClass.BUS.value)
    same = _score(candidate_class=VehicleClass.CAR.value)
    assert different.vehicle_class < unknown.vehicle_class < same.vehicle_class  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- colour


def test_the_same_colour_under_different_light_stays_close() -> None:
    """Lightness is halved for the same reason the descriptor down-weights it."""
    gap = _colour_distance(WHITE, WHITE_DIMMER)
    assert gap is not None and gap < 12.0


def test_two_different_colours_are_far_apart() -> None:
    gap = _colour_distance(WHITE, RED)
    assert gap is not None and gap > 40.0


def test_a_missing_colour_scores_neutral_rather_than_zero() -> None:
    """No colour recorded is not evidence against a match."""
    s = _score(candidate_colour=None)
    assert s.colour == pytest.approx(0.5)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ plausibility


def test_a_comfortable_hop_outscores_a_barely_possible_one() -> None:
    """Plausibility is a preference here; the hard bound already ran before the scorer."""
    easy = _score(implied_speed_kmph=20.0, ceiling_kmph=80.0)
    strained = _score(implied_speed_kmph=79.0, ceiling_kmph=80.0)
    assert easy.total > strained.total  # type: ignore[attr-defined]


def test_plausibility_never_goes_negative() -> None:
    s = _score(implied_speed_kmph=500.0, ceiling_kmph=80.0)
    assert s.plausibility == pytest.approx(0.0)  # type: ignore[attr-defined]


# ------------------------------------------------------------------------- ranges


def test_every_component_and_the_total_stay_in_unit_range() -> None:
    for distance in (0.0, 0.2, 0.45, MAX_APPEARANCE_DISTANCE, 1.5):
        for quality in (0.0, 0.5, 1.0):
            s = _score(
                appearance_distance=distance, seed_quality=quality, candidate_quality=quality
            )
            parts = s.as_dict()  # type: ignore[attr-defined]
            for name, value in parts.items():
                assert 0.0 <= value <= 1.0, f"{name} out of range at d={distance} q={quality}"


def test_the_breakdown_is_returned_not_just_the_total() -> None:
    """A score without its components is an assertion; with them it is an argument."""
    parts = _score().as_dict()  # type: ignore[attr-defined]
    assert set(parts) == {"appearance", "quality", "class", "colour", "plausibility", "total"}


def test_a_weak_pair_falls_below_the_reporting_floor() -> None:
    """Silence beats a list of weak guesses an investigator may act on anyway.

    The distance is inside the gate, so this tests the reporting floor rather than the
    gate -- a pair that looks similar enough to consider, but whose every corroborating
    signal disagrees, is still not worth putting in front of anyone.
    """
    s = _score(
        appearance_distance=0.048,
        seed_quality=0.1,
        candidate_quality=0.1,
        candidate_class=VehicleClass.BUS.value,
        candidate_colour=RED,
        implied_speed_kmph=78.0,
        ceiling_kmph=80.0,
    )
    assert s.total > 0.0, "the pair must pass the gate for this test to mean anything"  # type: ignore[attr-defined]
    assert s.total < MIN_SCORE  # type: ignore[attr-defined]
