"""A grade drives a procurement recommendation, so it has to be arguable.

These tests pin the decision table rather than the SQL. The measurement -- medians and
percentiles over boxes the platform already stores -- is Postgres's job and is exercised
by the integration path; what needs guarding is the reasoning that turns two numbers into
"replace this camera", because that sentence may end up in front of someone with a
budget.

The first test is a regression. The original ordering tested the sample size before
looking at the plates, and so graded a camera `not_viable` for having produced only five
reads -- while those five reads measured a median 87 px, comfortably readable. The
absence of plates is evidence about a camera only when the camera is demonstrably seeing
vehicles; the width of the plates it does resolve is evidence either way.
"""

from __future__ import annotations

import pytest

from services.analytics.readiness import (
    MIN_PLATE_SAMPLE,
    PLATE_PX_MARGINAL,
    PLATE_PX_READABLE,
    RATIO_ALIGNED,
    ReadinessGrade,
    _grade,
    analytics_for,
)


def _g(
    plate_n: int,
    plate_median: float | None,
    vehicle_n: int = 0,
    vehicle_median: float | None = None,
    ratio: float | None = None,
) -> ReadinessGrade:
    return _grade(plate_n, plate_median, vehicle_n, vehicle_median, ratio)[0]


def _reason(
    plate_n: int,
    plate_median: float | None,
    vehicle_n: int = 0,
    vehicle_median: float | None = None,
    ratio: float | None = None,
) -> str:
    return _grade(plate_n, plate_median, vehicle_n, vehicle_median, ratio)[1]


def _remedy(
    plate_n: int,
    plate_median: float | None,
    vehicle_n: int = 0,
    vehicle_median: float | None = None,
    ratio: float | None = None,
) -> str | None:
    return _grade(plate_n, plate_median, vehicle_n, vehicle_median, ratio)[2]


# --------------------------------------------------------------------- regression


def test_a_thin_sample_of_readable_plates_is_not_called_unviable() -> None:
    """Five readable plates is weak evidence of capability, not evidence against it.

    This is the exact case that was graded `not_viable` by the first implementation: a
    camera watching 39 vehicles that resolved five plates at a median 87 px.
    """
    grade = _g(5, 87.0, vehicle_n=39, vehicle_median=265.0, ratio=0.33)
    assert grade is ReadinessGrade.INSUFFICIENT_SAMPLE
    assert "readable" in _reason(5, 87.0, vehicle_n=39, vehicle_median=265.0, ratio=0.33)


# ------------------------------------------------------------------ capable band


def test_enough_readable_plates_grades_capable() -> None:
    assert _g(MIN_PLATE_SAMPLE, PLATE_PX_READABLE, vehicle_n=50, vehicle_median=250.0) is (
        ReadinessGrade.ANPR_CAPABLE
    )


def test_a_capable_camera_needs_no_remedy() -> None:
    assert _remedy(20, 120.0, vehicle_n=80, vehicle_median=300.0) is None


# ----------------------------------------------------------------- marginal band


def test_the_marginal_band_recommends_a_re_aim() -> None:
    grade, reason, remedy = _grade(20, 55.0, 60, 250.0, 0.22)
    assert grade is ReadinessGrade.MARGINAL_REAIM
    assert remedy is not None and "field of view" in remedy or "Re-aim" in (remedy or "")
    assert "55" in reason


def test_a_low_plate_to_vehicle_ratio_is_named_as_an_aiming_fault() -> None:
    """A large vehicle with a tiny plate is a camera photographing flanks, not distance.

    The distinction matters because it is the difference between "re-aim it" and
    "replace it", and only one of those costs money.
    """
    _, reason, remedy = _grade(20, 55.0, 60, 400.0, RATIO_ALIGNED - 0.05)
    assert "across the carriageway" in reason
    assert remedy is not None and "along the approach lane" in remedy


def test_a_well_aimed_marginal_camera_is_not_told_to_re_aim() -> None:
    _, reason, remedy = _grade(20, 55.0, 60, 200.0, RATIO_ALIGNED + 0.10)
    assert "across the carriageway" not in reason
    assert remedy is not None and "Narrow the field of view" in remedy


# --------------------------------------------------------------- not viable band


def test_plates_too_small_for_a_re_aim_to_recover() -> None:
    grade, _, remedy = _grade(20, PLATE_PX_MARGINAL - 5, 60, 250.0, 0.15)
    assert grade is ReadinessGrade.NOT_VIABLE
    assert remedy is not None and "counting" in remedy


def test_plenty_of_vehicles_and_almost_no_plates_is_the_headline_finding() -> None:
    """The camera sees traffic and the plates do not survive its image.

    This is the sentence worth handing to a department, so it is asserted literally.
    """
    grade, reason, remedy = _grade(2, 30.0, 200, 27.0, 0.11)
    assert grade is ReadinessGrade.NOT_VIABLE
    assert "200 vehicles observed" in reason
    assert "not resolvable" in reason
    assert remedy is not None


def test_no_plates_at_all_with_plenty_of_vehicles_is_also_not_viable() -> None:
    assert _g(0, None, vehicle_n=200, vehicle_median=27.0) is ReadinessGrade.NOT_VIABLE


# ------------------------------------------------------------ withholding a grade


def test_a_camera_with_nothing_recorded_is_not_graded() -> None:
    """Telling a department its camera is useless on no evidence is worse than silence."""
    assert _g(0, None, vehicle_n=0) is ReadinessGrade.INSUFFICIENT_SAMPLE


def test_a_few_vehicles_and_no_plates_is_not_yet_a_verdict() -> None:
    """Below the vehicle sample floor, absence of plates is not evidence."""
    assert _g(0, None, vehicle_n=5, vehicle_median=200.0) is ReadinessGrade.INSUFFICIENT_SAMPLE


def test_an_ungraded_camera_is_never_given_a_remedy() -> None:
    """A recommendation without a measurement behind it is a guess with authority."""
    assert _remedy(0, None, vehicle_n=0) is None


# -------------------------------------------------------- analytics assignment


def test_an_unviable_camera_is_not_asked_to_run_anpr() -> None:
    """Asking a camera for something it cannot deliver wastes the compute the capable
    ones need, and produces an empty result that reads as a software fault."""
    assert "anpr" not in analytics_for(ReadinessGrade.NOT_VIABLE)


def test_an_unviable_camera_still_runs_everything_that_works_at_27_px() -> None:
    assigned = analytics_for(ReadinessGrade.NOT_VIABLE)
    for analytic in (
        "vehicle_counting",
        "flow_direction",
        "intrusion_zones",
        "appearance",
        "dwell",
    ):
        assert analytic in assigned


def test_a_marginal_camera_keeps_anpr() -> None:
    """Switching it off would remove the measurement that justifies the re-aim advice."""
    assert "anpr" in analytics_for(ReadinessGrade.MARGINAL_REAIM)


def test_an_ungraded_camera_keeps_anpr() -> None:
    """Otherwise it can never gather the plate observations needed to grade it."""
    assert "anpr" in analytics_for(ReadinessGrade.INSUFFICIENT_SAMPLE)


@pytest.mark.parametrize("grade", list(ReadinessGrade))
def test_every_grade_has_an_analytics_assignment(grade: ReadinessGrade) -> None:
    """A new grade must be assigned deliberately rather than defaulting to nothing."""
    assert analytics_for(grade)
