"""Plate grammar, confusion correction and multi-frame fusion."""

from __future__ import annotations

import time

import pytest

from services.analytics.plate_grammar import (
    PlateAccumulator,
    confusion_aware_distance,
    is_confusable,
    normalise_plate,
)


# ------------------------------------------------------------------- clean reads


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("GJ01AB1234", "GJ01AB1234"),
        ("gj 01 ab 1234", "GJ01AB1234"),
        ("GJ-01-AB-1234", "GJ01AB1234"),
        ("  MH12DE1433  ", "MH12DE1433"),
        ("GJ1AB1234", "GJ1AB1234"),  # single-digit RTO
        ("GJ01A1234", "GJ01A1234"),  # single-letter series
    ],
)
def test_clean_plates_pass_through_uncorrected(raw, expected):
    result = normalise_plate(raw)
    assert result.normalised == expected
    assert result.valid is True
    assert result.is_clean is True, "a clean read must record no corrections"


def test_ind_prefix_is_stripped():
    assert normalise_plate("INDGJ01AB1234").normalised == "GJ01AB1234"


# --------------------------------------------------------- grammar-led correction


def test_digit_in_a_letter_position_is_corrected():
    # '0' cannot begin a registration; the state code is always two letters.
    result = normalise_plate("0J01AB1234", [0.4] + [0.9] * 9)
    assert result.normalised == "OJ01AB1234"
    assert result.valid is True
    assert result.is_clean is False
    assert len(result.corrections) == 1
    c = result.corrections[0]
    assert (c.position, c.raw, c.corrected) == (0, "0", "O")
    assert c.reason == "grammar_expects_letter"


def test_letter_in_a_digit_position_is_corrected():
    result = normalise_plate("GJO1AB1234", [0.9, 0.9, 0.35] + [0.9] * 7)
    assert result.normalised == "GJ01AB1234"
    assert [c.corrected for c in result.corrections] == ["0"]
    assert result.corrections[0].confidence == pytest.approx(0.35)


def test_correction_records_the_confidence_at_that_position():
    # The operator must be able to see that the corrected character was the weak one.
    result = normalise_plate("GJ01AB1Z34", [0.99] * 7 + [0.31, 0.99, 0.99])
    assert result.normalised == "GJ01AB1234"
    assert result.corrections[0].confidence == pytest.approx(0.31)


def test_multiple_corrections_are_all_recorded():
    result = normalise_plate("6J01A81234", [0.4, 0.9, 0.9, 0.9, 0.9, 0.4] + [0.9] * 4)
    assert result.normalised == "GJ01AB1234"
    assert len(result.corrections) == 2
    assert {c.position for c in result.corrections} == {0, 5}


def test_corrections_that_do_not_yield_a_legal_plate_are_discarded():
    """A correction that does not resolve the grammar is a guess, not a fix."""
    result = normalise_plate("!!!!", [0.1] * 4)
    assert result.valid is False
    assert result.corrections == []


# -------------------------------------------------------------- special patterns


def test_bh_series_is_not_forced_through_the_state_layout():
    # 22BH1234AA ends in letters; the standard layout would "correct" them to digits.
    result = normalise_plate("22BH1234AA")
    assert result.normalised == "22BH1234AA"
    assert result.valid is True
    assert result.pattern == "bh_series"
    assert result.is_clean is True


def test_unparseable_text_is_kept_not_discarded():
    # An unreadable plate is still evidence that a vehicle was present.
    result = normalise_plate("XY", [0.2, 0.2])
    assert result.valid is False
    assert result.normalised == "XY"


def test_ambiguous_eight_character_length_is_not_corrected():
    # Two layouts are plausible at 8 characters; guessing would fabricate structure.
    result = normalise_plate("GJ1AB123", [0.9] * 8)
    assert result.corrections == []


# ------------------------------------------------------------------ ReDoS guard


def test_pathological_input_cannot_hang_the_regex():
    """A public endpoint takes this input; catastrophic backtracking would be a DoS."""
    hostile = "A" * 5000 + "0" * 5000
    started = time.monotonic()
    normalise_plate(hostile)
    assert time.monotonic() - started < 0.5


# -------------------------------------------------------------- confusion scoring


def test_known_confusion_pairs_are_recognised():
    assert is_confusable("0", "O")
    assert is_confusable("8", "B")
    assert is_confusable("5", "S")
    assert not is_confusable("X", "K")


def test_confusion_aware_distance_separates_explained_from_unexplained():
    # One character differs and a confusion set explains it -> likely the same plate.
    total, explained = confusion_aware_distance("GJ01AB1234", "GJ01AB1Z34")
    assert (total, explained) == (1, 1)

    # One character differs and nothing explains it -> a different vehicle.
    total, explained = confusion_aware_distance("GJ01AB1234", "GJ01AB1934")
    assert (total, explained) == (1, 0)


def test_different_lengths_are_maximally_distant():
    total, explained = confusion_aware_distance("GJ01AB1234", "GJ01AB123")
    assert total == 10 and explained == 0


# ------------------------------------------------------------ multi-frame fusion


def test_fusion_prefers_the_confident_minority_over_the_hesitant_majority():
    """The core claim: N frames are one observation with a distribution, not N votes."""
    acc = PlateAccumulator()
    # Eight hesitant frames read position 2 as 'O'; two confident frames read '0'.
    for _ in range(8):
        acc.add("GJO1AB1234", [0.95, 0.95, 0.30] + [0.95] * 7)
    for _ in range(2):
        acc.add("GJ01AB1234", [0.99, 0.99, 0.99] + [0.99] * 7)

    fused = acc.fused()
    assert fused is not None
    assert fused.normalised == "GJ01AB1234"
    assert acc.frame_count == 10


def test_fusion_recovers_from_a_dropped_leading_character():
    """OCR dropping a character must not smear every following position.

    Observed in practice: our first model probe read a 10-character plate as 9.
    Right-alignment keeps the numeric group in register.
    """
    acc = PlateAccumulator()
    for _ in range(6):
        acc.add("GJ01AB1234", [0.9] * 10)
    for _ in range(2):
        acc.add("J01AB1234", [0.6] * 9)  # leading 'G' lost

    fused = acc.fused()
    assert fused is not None
    assert fused.normalised == "GJ01AB1234"


def test_fusion_of_a_single_frame_matches_a_direct_read():
    acc = PlateAccumulator()
    acc.add("GJ01AB1234", [0.9] * 10)
    fused = acc.fused()
    assert fused is not None and fused.normalised == "GJ01AB1234"


def test_empty_accumulator_returns_none():
    assert PlateAccumulator().fused() is None


def test_fusion_confidence_reflects_agreement_across_frames():
    unanimous = PlateAccumulator()
    for _ in range(6):
        unanimous.add("GJ01AB1234", [0.9] * 10)

    split = PlateAccumulator()
    for _ in range(3):
        split.add("GJ01AB1234", [0.9] * 10)
    for _ in range(3):
        split.add("GJ01AB1299", [0.9] * 10)

    assert unanimous.fused().confidence > split.fused().confidence


# ------------------------------------------- regressions found on real footage


def test_excessive_corrections_are_rejected_as_fabrication():
    """Found on real footage: noise was being rewritten into legal-looking plates.

    A 9-character read needing four substitutions produced 'AA25II166' at confidence
    1.00. The grammar was not disambiguating a near-miss, it was manufacturing one.
    """
    # Needs three substitutions to become legal: 0->O, 8->B, Z->2.
    result = normalise_plate("0J01A81Z34", [0.3] * 10)
    assert result.valid is False
    assert result.pattern == "too_many_corrections"
    # The raw reading survives: it is still evidence a vehicle was present.
    assert result.normalised == "0J01A81Z34"


def test_two_corrections_are_still_accepted():
    # The cap must not reject genuine near-misses, which is the whole point.
    result = normalise_plate("GJO1AB1Z34", [0.9, 0.9, 0.3] + [0.9] * 4 + [0.3, 0.9, 0.9])
    assert result.valid is True
    assert len(result.corrections) == 2


def test_single_frame_read_is_not_maximally_confident():
    """Found on real footage: every one-frame read reported confidence 1.00.

    Fusion confidence measured cross-frame agreement, which is trivially total when
    there is only one frame. One hesitant glance is the weakest evidence we have and
    must not outrank ten corroborating reads.
    """
    single = PlateAccumulator()
    single.add("GJ01AB1234", [0.55] * 10)
    fused = single.fused()
    assert fused is not None
    assert fused.confidence < 0.7, "a lone low-confidence read must not report ~1.0"


def test_corroborated_reads_outrank_a_single_hesitant_one():
    lone = PlateAccumulator()
    lone.add("GJ01AB1234", [0.5] * 10)

    many = PlateAccumulator()
    for _ in range(10):
        many.add("GJ01AB1234", [0.9] * 10)

    assert many.fused().confidence > lone.fused().confidence


def test_confidence_reflects_ocr_strength_not_just_agreement():
    weak = PlateAccumulator()
    strong = PlateAccumulator()
    for _ in range(5):
        weak.add("GJ01AB1234", [0.4] * 10)
        strong.add("GJ01AB1234", [0.95] * 10)
    # Both are unanimous; only OCR strength separates them.
    assert strong.fused().confidence > weak.fused().confidence
