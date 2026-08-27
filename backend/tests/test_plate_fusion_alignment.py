"""Fusion must choose its alignment, not assume one.

These lock in the three defects that together produced 0% plate-level accuracy:
a recogniser that could not emit a 10-character plate, a tracker that never
associated anything so fusion never ran, and fusion that right-aligned reads of
different lengths and voted character against unrelated character.
"""

from __future__ import annotations

from services.analytics.model_ids import RECOGNISER_MODEL
from services.analytics.plate_grammar import PlateAccumulator


def test_dropped_trailing_character_does_not_smear() -> None:
    """The regression that mattered most.

    Right-alignment is correct when the OCR loses a *leading* character and wrong
    when it loses a trailing one. Fusing `KA25AB1542` with `KA25AB154` by right
    alignment shifts every position by one, so three near-correct reads fused to
    `KA25A1154` -- worse than the best single read, and wrong in a way the evidence
    did not support.
    """
    acc = PlateAccumulator()
    acc.add("KA25AB1542", [0.9] * 10)
    acc.add("KA25AB154", [0.8] * 9)  # trailing '2' lost
    acc.add("KA25AB154", [0.8] * 9)

    fused = acc.fused()
    assert fused is not None
    assert fused.normalised == "KA25AB1542"


def test_dropped_leading_character_still_right_aligns() -> None:
    """The older case must keep working; the fix chooses, it does not flip."""
    acc = PlateAccumulator()
    for _ in range(4):
        acc.add("GJ01AB1234", [0.9] * 10)
    for _ in range(2):
        acc.add("J01AB1234", [0.6] * 9)  # leading 'G' lost

    fused = acc.fused()
    assert fused is not None
    assert fused.normalised == "GJ01AB1234"


def test_confident_full_length_read_outweighs_hesitant_short_ones() -> None:
    """Length is decided by confidence-weighted vote, not by a show of hands."""
    acc = PlateAccumulator()
    acc.add("GJ32K98701", [0.95] * 10)
    acc.add("GJ32K9870", [0.3] * 9)
    acc.add("GJ32K9870", [0.3] * 9)

    fused = acc.fused()
    assert fused is not None
    assert len(fused.normalised) == 10


def test_a_lone_weak_long_read_does_not_stretch_the_plate() -> None:
    """The other side of the length bias.

    Preferring the longer length must not let one hesitant read invent a character
    the evidence does not support, so a longer candidate has to carry a respectable
    share of the best length's support, not merely exist.
    """
    acc = PlateAccumulator()
    for _ in range(5):
        acc.add("GJ01AB123", [0.9] * 9)
    acc.add("GJ01AB1234", [0.15] * 10)  # one hesitant, over-long read

    fused = acc.fused()
    assert fused is not None
    assert len(fused.normalised) == 9


def test_a_majority_of_agreeing_reads_still_wins() -> None:
    """Voting must not be hostage to one loud outlier of the same length."""
    acc = PlateAccumulator()
    for _ in range(4):
        acc.add("GJ01AB1234", [0.7] * 10)
    acc.add("GJ01AB1299", [0.75] * 10)

    fused = acc.fused()
    assert fused is not None
    assert fused.normalised == "GJ01AB1234"


def test_recogniser_must_have_room_for_a_full_indian_plate() -> None:
    """A 9-slot model cannot emit a 10-character plate at all.

    `max_plate_slots` is the model's number of classification heads, so this is an
    arithmetic ceiling rather than an accuracy problem. The original default had 9,
    which meant every full-length Indian registration was wrong before inference
    began. This guards the choice rather than the download: it asserts we have not
    silently reverted to a model whose name marks it as the 9-slot generation.
    """
    assert RECOGNISER_MODEL.startswith("cct-"), RECOGNISER_MODEL
    assert "-v1-" not in RECOGNISER_MODEL, (
        f"{RECOGNISER_MODEL} is a v1 CCT model; those carry 9 plate slots and cannot "
        "represent a 10-character Indian registration. Check max_plate_slots in the "
        "model's config before changing this."
    )
