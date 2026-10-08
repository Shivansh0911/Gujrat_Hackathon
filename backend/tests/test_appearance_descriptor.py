"""The appearance fingerprint must survive a lighting change and still separate vehicles.

That sentence is the whole contract, and the first implementation failed it in a way no
unit test would have caught by accident: it binned all three CIELAB channels into one
4x4x4 cube, which measured the same vehicle under dimmer light as *further apart* than
two plainly different vehicles. The ordering assertion below is therefore the primary
test in this file rather than an afterthought -- a descriptor that passes every shape
and range check and fails this one is useless.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from services.analytics.appearance import (
    DESCRIPTOR_DIM,
    DESCRIPTOR_KIND,
    MIN_CROP_AREA_PX,
    cosine_distance,
    describe,
)


def _vehicleish(
    *,
    bgr: tuple[int, int, int],
    height: int = 80,
    width: int = 120,
    texture: int = 18,
    seed: int = 11,
) -> NDArray[np.uint8]:
    """A crop with a body colour, a darker lower band and some texture.

    Texture matters: a perfectly flat colour block has almost no Laplacian variance, so
    the blur gate scores it near the floor. Real crops are not flat, and a fixture that
    is would be testing the gate rather than the descriptor.
    """
    rng = np.random.default_rng(seed)
    crop = np.zeros((height, width, 3), dtype=np.uint8)
    crop[:, :] = bgr
    # Lower third darker: shadow and wheels, present in any real vehicle crop.
    crop[int(height * 0.66) :, :] = tuple(int(c * 0.35) for c in bgr)
    noise = rng.integers(-texture, texture + 1, size=crop.shape, dtype=np.int16)
    return np.clip(crop.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _relight(crop: NDArray[np.uint8], factor: float) -> NDArray[np.uint8]:
    """Scale luminance, as a different time of day or a different exposure would."""
    return np.clip(crop.astype(np.float32) * factor, 0, 255).astype(np.uint8)


# --------------------------------------------------------------- the primary contract


def test_lighting_change_is_closer_than_a_different_vehicle() -> None:
    """A relit crop of one vehicle must be nearer to itself than any other vehicle is.

    This is the property that justifies using CIELAB at all. If it does not hold, the
    descriptor cannot support a cross-camera claim, because the two cameras in any real
    pair are lit differently.
    """
    silver = describe(_vehicleish(bgr=(200, 200, 198), seed=1))
    assert silver is not None

    # The same vehicle at 70% and 135% exposure.
    dimmer = describe(_relight(_vehicleish(bgr=(200, 200, 198), seed=1), 0.70))
    brighter = describe(_relight(_vehicleish(bgr=(200, 200, 198), seed=1), 1.35))
    assert dimmer is not None and brighter is not None

    red = describe(_vehicleish(bgr=(40, 40, 170), seed=2))
    blue = describe(_vehicleish(bgr=(170, 60, 40), seed=3))
    assert red is not None and blue is not None

    self_dim = cosine_distance(silver.vector, dimmer.vector)
    self_bright = cosine_distance(silver.vector, brighter.vector)
    worst_self = max(self_dim, self_bright)

    best_other = min(
        cosine_distance(silver.vector, red.vector),
        cosine_distance(silver.vector, blue.vector),
    )

    assert worst_self < best_other, (
        f"relit self ({worst_self:.4f}) must be nearer than the nearest different "
        f"vehicle ({best_other:.4f})"
    )
    # Not merely ordered -- separated by a margin a threshold can be placed in.
    assert (
        best_other > worst_self * 3
    ), f"margin too thin to threshold: self={worst_self:.4f} other={best_other:.4f}"


def test_distinct_colours_are_mutually_distant() -> None:
    """Three different body colours must not collapse into one another."""
    crops = {
        "silver": describe(_vehicleish(bgr=(200, 200, 198), seed=1)),
        "red": describe(_vehicleish(bgr=(40, 40, 170), seed=2)),
        "blue": describe(_vehicleish(bgr=(170, 60, 40), seed=3)),
    }
    assert all(v is not None for v in crops.values())

    names = sorted(crops)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            da, db = crops[a], crops[b]
            assert da is not None and db is not None
            assert cosine_distance(da.vector, db.vector) > 0.15, f"{a} vs {b} too close"


# ------------------------------------------------------------------- shape and ranges


def test_vector_has_the_declared_dimension_and_kind() -> None:
    """The migration hard-codes `vector(DESCRIPTOR_DIM)`; a drift here is a write error."""
    d = describe(_vehicleish(bgr=(120, 120, 120)))
    assert d is not None
    assert len(d.vector) == DESCRIPTOR_DIM
    assert d.kind == DESCRIPTOR_KIND
    assert all(np.isfinite(v) for v in d.vector)
    assert all(v >= 0.0 for v in d.vector), "histogram components are never negative"


def test_dominant_colour_is_in_conventional_lab_ranges() -> None:
    """Stored Lab must be the published convention, not OpenCV's 8-bit encoding.

    A reviewer comparing a stored value against a colour reference will assume
    L in 0..100 and a/b roughly -128..127. Storing OpenCV's 0..255 would make every
    recorded colour wrong by a factor and an offset.
    """
    d = describe(_vehicleish(bgr=(40, 40, 170), seed=2))
    assert d is not None
    lab = d.dominant_colour_lab
    assert 0.0 <= lab["L"] <= 100.0
    assert -128.0 <= lab["a"] <= 127.0
    assert -128.0 <= lab["b"] <= 127.0


def test_dominant_colour_prefers_the_lit_body_over_its_shadow() -> None:
    """On an evenly split bright/dark crop the vehicle is the lit part, not the shadow.

    The first implementation tie-broke with `argmax`, which returns the first maximum,
    and so reported the darker half of a 50/50 crop as its dominant colour.
    """
    crop = np.full((80, 120, 3), 230, dtype=np.uint8)
    crop[40:, :, :] = 55  # exactly half dark
    d = describe(crop)
    assert d is not None
    # Lab L of 230/255 is ~90; of 55/255 is ~21. Mid-scale separates them unambiguously.
    assert d.dominant_colour_lab["L"] > 50.0


# ---------------------------------------------------------------------- quality gating


def test_a_crop_below_the_area_floor_is_refused_outright() -> None:
    """No fingerprint is better than a meaningless one.

    A sighting with `appearance_vector IS NULL` still counts for flow and dwell
    analytics; a sighting carrying noise would pollute every neighbour search.
    """
    side = int(MIN_CROP_AREA_PX**0.5) - 2
    assert describe(np.full((side, side, 3), 200, dtype=np.uint8)) is None


def test_quality_rises_with_crop_area() -> None:
    small = describe(_vehicleish(bgr=(150, 150, 150), height=30, width=40))
    large = describe(_vehicleish(bgr=(150, 150, 150), height=160, width=200))
    assert small is not None and large is not None
    assert large.quality_score > small.quality_score


def test_quality_falls_with_blur() -> None:
    """Blur destroys the edge histogram, so a smeared crop must be down-weighted."""
    from services.common.cv_env import cv2

    sharp = _vehicleish(bgr=(150, 150, 150), height=120, width=160)
    smeared = cv2.GaussianBlur(sharp, (21, 21), 0)

    d_sharp = describe(sharp)
    d_smeared = describe(smeared)
    assert d_sharp is not None and d_smeared is not None
    assert d_smeared.quality_score < d_sharp.quality_score


def test_quality_never_reaches_exactly_zero() -> None:
    """A scorer multiplies by quality; a hard zero would make a real sighting invisible."""
    from services.common.cv_env import cv2

    flat = np.full((40, 40, 3), 128, dtype=np.uint8)
    d = describe(cv2.GaussianBlur(flat, (21, 21), 0))
    assert d is not None
    assert d.quality_score > 0.0


# ------------------------------------------------------------------- malformed input


@pytest.mark.parametrize(
    "bad",
    [
        np.full((80, 80), 200, dtype=np.uint8),  # greyscale, no channel axis
        np.full((80, 80, 4), 200, dtype=np.uint8),  # BGRA
        np.zeros((0, 0, 3), dtype=np.uint8),  # empty
    ],
    ids=["greyscale", "four-channel", "empty"],
)
def test_malformed_crops_return_none(bad: NDArray[np.uint8]) -> None:
    assert describe(bad) is None


# ----------------------------------------------------------------- distance semantics


def test_cosine_distance_of_a_vector_with_itself_is_zero() -> None:
    d = describe(_vehicleish(bgr=(90, 140, 90)))
    assert d is not None
    assert cosine_distance(d.vector, d.vector) == pytest.approx(0.0, abs=1e-12)


def test_cosine_distance_rejects_a_length_mismatch() -> None:
    """Comparing different layouts yields a number with no meaning; refuse instead."""
    with pytest.raises(ValueError, match="length mismatch"):
        cosine_distance([1.0, 0.0], [1.0, 0.0, 0.0])


def test_a_zero_vector_reads_as_no_similarity_rather_than_nan() -> None:
    """pgvector returns NaN here; a NaN propagating into a confidence score is worse."""
    zeros = [0.0] * DESCRIPTOR_DIM
    ones = [1.0] * DESCRIPTOR_DIM
    assert cosine_distance(zeros, ones) == 1.0
    assert cosine_distance(zeros, zeros) == 1.0
