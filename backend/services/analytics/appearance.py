"""Appearance fingerprinting for a vehicle crop (§3.2).

Why this exists
---------------
`Detection` cannot exist without a plate string -- `plate_raw` and `plate_normalised`
are both `nullable=False`. On the government estate that means the database only ever
learns about a vehicle when its registration happens to be legible, which DISCOVERY
finding 22 measured at one grammar-valid read per ~3,900 frames. Every other vehicle
the cameras saw is discarded.

A plate is unreadable at 27 px. The vehicle it is bolted to is still plainly a white
hatchback. So identity becomes a scored, evidenced claim built from what *does* survive
at that resolution, and the plate becomes one signal among several rather than the
primary key.

What is in the vector, and what is deliberately not
---------------------------------------------------
The vector is compared across cameras with cosine distance, so it may contain only
properties of the *vehicle*. Properties of the *framing* -- where in the image the box
sat, how large it was -- are camera-specific: the same car at the near edge of camera 4
and the far edge of camera 9 would be pushed apart by exactly the dimensions that say
nothing about whether it is the same car.

The brief lists bbox area and vertical position alongside the colour histogram. They are
kept, but as their own columns feeding the scorer, not as vector components. That is a
deliberate departure and the reason is above.

So the vector is:

    [ 0: 64]  chroma histogram over (a, b), 8x8, L1-normalised, weight 1.0
    [64: 68]  lightness histogram over L, 4 bins, L1-normalised, weight 0.30
    [68: 76]  edge-orientation histogram, 8 bins, L1-normalised, weight 0.50

and `aspect_ratio`, `rel_area`, `rel_cx`, `rel_cy` ride alongside as scalars.

Why CIELAB and not RGB, and why the channels are split
------------------------------------------------------
The brief requires Lab and the requirement is sound. RGB distance does not correspond to
perceived colour difference: a white car under sodium streetlight and the same car under
daylight are far apart in RGB and close in Lab, because Lab separates lightness from
chroma and is approximately perceptually uniform. Two cameras on the same corridor at
different times of day are exactly that case.

But *using* Lab is not enough -- it has to be used in a way that collects the benefit.
The first version of this module binned all three channels jointly into a 4x4x4 cube,
which looked like the obvious reading of "CIELAB colour histogram" and measured
backwards: on a synthetic pair, the same vehicle under dimmer light scored a cosine
distance of 0.333 while a plainly different vehicle scored 0.119. Lightness occupies a
third of a joint cube, so dimming a crop migrates most of its mass to different bins and
the distance explodes, while two unrelated vehicles that happen to share a brightness
profile land close together. The joint cube reintroduces exactly the illumination
sensitivity Lab was chosen to remove.

Splitting the axes fixes it, because it makes the weighting expressible. Chroma -- the
(a, b) plane -- is what "white", "red", "dark blue" actually means and is the most
illumination-stable thing in the crop, so it carries full weight and gets the finer
8x8 binning. Lightness is genuinely informative (a black car is not a white one) but it
is the channel a streetlight moves, so it is binned coarsely and weighted 0.30: enough
to separate black from white, not enough to separate a car from itself at dusk. Edge
orientation sits between the two at 0.50.

The weights are applied before L1-ing the blocks together, so cosine distance over the
concatenation reproduces them. They are provisional in the same sense as the quality
thresholds below -- set from this reasoning, then re-derived against labelled pairs in
§3.5 -- and the measured outcome, including if it contradicts the reasoning, goes in
DISCOVERY.

Quality gating
--------------
A fingerprint from a 30x20 crop is close to noise and must not be weighted like one from
300x200. `quality_score` is computed here and every downstream match multiplies by it.
Crops below `MIN_CROP_AREA_PX` are refused outright -- `describe` returns `None` -- so a
sighting can exist with no fingerprint at all rather than with a meaningless one.

Occlusion is **not** measured. Detecting that a vehicle is half-hidden behind another
needs instance masks, which the detector does not produce. The brief asks for occlusion
gating; this does not provide it, and the omission is recorded rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from services.analytics.model_ids import APPEARANCE_DIM, APPEARANCE_KIND
from services.common.cv_env import cv2

# --- Vector layout ----------------------------------------------------------------

#: Bins per chroma axis. 8x8=64 over the (a, b) plane. Fine enough to separate "white",
#: "silver", "red" and "dark blue"; coarse enough that a one-bin lighting shift does not
#: move a vehicle out of its own neighbourhood.
CHROMA_BINS: int = 8

#: Bins over L. Four, deliberately coarse: this axis is the one a streetlight moves, so
#: it is here to separate a black vehicle from a white one and nothing finer.
LIGHTNESS_BINS: int = 4

#: Orientation bins for the edge histogram. Eight gives 22.5-degree resolution, which
#: distinguishes a boxy truck tail from a sloped hatchback without encoding the
#: vehicle's heading relative to the camera finely enough to penalise the same vehicle
#: seen at a different angle.
EDGE_BINS: int = 8

#: Per-block weights, applied before concatenation. See the module docstring for why
#: lightness is held down rather than dropped.
CHROMA_WEIGHT: float = 1.00
LIGHTNESS_WEIGHT: float = 0.30
EDGE_WEIGHT: float = 0.50

CHROMA_DIM: int = CHROMA_BINS**2

#: Re-exported from `model_ids`, which is the single source of truth because
#: `services.registry.models` needs the same number to size the pgvector column and
#: must not import this module (it would drag OpenCV into the API process).
#:
#: Asserted rather than computed so that editing a binning constant above without
#: updating the column width fails at import rather than at the first write.
DESCRIPTOR_DIM: int = APPEARANCE_DIM
if CHROMA_DIM + LIGHTNESS_BINS + EDGE_BINS != DESCRIPTOR_DIM:  # pragma: no cover
    raise AssertionError(
        f"descriptor layout is {CHROMA_DIM + LIGHTNESS_BINS + EDGE_BINS} wide but "
        f"model_ids.APPEARANCE_DIM says {DESCRIPTOR_DIM}. Changing the layout needs a "
        f"migration for vehicle_sighting.appearance_vector and a new APPEARANCE_KIND."
    )

#: Stamped on every stored vector. Vectors of different kinds are **never** compared:
#: cosine distance between two different feature layouts is a number with no meaning.
#: Bump it in `model_ids` whenever the layout, any binning constant or any weight above
#: changes, so old rows stop being eligible for comparison instead of silently
#: degrading match quality.
DESCRIPTOR_KIND: str = APPEARANCE_KIND

# --- Quality gating ---------------------------------------------------------------

#: Below this crop area a fingerprint is refused. 24x24=576 px is the floor at which a
#: 4x4x4 Lab histogram still has enough samples per bin to be stable; below it the
#: histogram is dominated by a handful of pixels and two unrelated vehicles routinely
#: land closer than two views of the same one.
#:
#: Provisional: chosen from the binning arithmetic, not yet from labelled pairs. It is
#: re-derived against ground truth in §3.5 and this constant is updated with the
#: measured value -- see DISCOVERY.
MIN_CROP_AREA_PX: int = 576

#: Crop area at which quality saturates to 1.0. A 128x128 crop carries as much colour
#: and edge information as this descriptor can represent, so more pixels do not make
#: the fingerprint better.
FULL_QUALITY_AREA_PX: int = 16384

#: Variance of the Laplacian below which a crop is treated as motion-blurred. Blur
#: destroys the edge histogram first and the colour histogram much later, so a blurred
#: crop is down-weighted rather than refused.
#:
#: Provisional, same caveat as MIN_CROP_AREA_PX.
BLUR_VARIANCE_FLOOR: float = 12.0

#: Laplacian variance at which sharpness stops contributing.
BLUR_VARIANCE_FULL: float = 120.0


@dataclass(frozen=True)
class AppearanceDescriptor:
    """One vehicle crop reduced to what survives at CCTV resolution."""

    vector: list[float]
    kind: str
    #: CIELAB of the crop's dominant colour cluster, as stored in
    #: `vehicle_sighting.dominant_colour_lab`. Human-readable, unlike the histogram.
    dominant_colour_lab: dict[str, float]
    quality_score: float
    aspect_ratio: float
    #: Diagnostics, carried so a low score can be explained rather than just asserted.
    crop_area_px: int
    blur_variance: float


def _l1(hist: NDArray[Any]) -> NDArray[np.float64]:
    """Normalise a histogram to sum 1, leaving an all-zero histogram alone."""
    flat: NDArray[np.float64] = np.asarray(hist, dtype=np.float64).reshape(-1)
    total = float(flat.sum())
    return flat if total <= 0.0 else flat / total


def _chroma_histogram(lab: NDArray[Any]) -> NDArray[np.float64]:
    """L1-normalised 8x8 histogram over the (a, b) chroma plane.

    Ranges are OpenCV's 8-bit Lab convention -- all three channels 0..255, with a and b
    centred on 128 -- not the 0..100 / -128..127 convention. Using the mathematical
    ranges here would put every real sample in a corner of the plane.
    """
    return _l1(cv2.calcHist([lab], [1, 2], None, [CHROMA_BINS, CHROMA_BINS], [0, 256, 0, 256]))


def _lightness_histogram(lab: NDArray[Any]) -> NDArray[np.float64]:
    """L1-normalised 4-bin histogram over L."""
    return _l1(cv2.calcHist([lab], [0], None, [LIGHTNESS_BINS], [0, 256]))


def _edge_orientation_histogram(grey: NDArray[Any]) -> NDArray[np.float64]:
    """L1-normalised histogram of gradient orientation, weighted by gradient magnitude.

    Orientation is taken modulo 180 degrees, not 360: an edge is a line, and a vehicle's
    roofline produces opposite gradient directions on its two sides. Treating those as
    different orientations would make the histogram depend on which side of the vehicle
    is brighter, which is a lighting property rather than a shape one.
    """
    gx = cv2.Sobel(grey, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(grey, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = np.sqrt(
        np.asarray(gx, dtype=np.float64) ** 2 + np.asarray(gy, dtype=np.float64) ** 2
    )
    # arctan2 gives (-pi, pi]; fold to [0, pi) so opposite gradients share a bin.
    angle = np.mod(
        np.arctan2(np.asarray(gy, dtype=np.float64), np.asarray(gx, dtype=np.float64)), np.pi
    )

    bin_width = np.pi / EDGE_BINS
    indices = np.minimum((angle / bin_width).astype(np.int64), EDGE_BINS - 1)

    hist: NDArray[np.float64] = np.zeros(EDGE_BINS, dtype=np.float64)
    # Magnitude weighting: a long straight roofline should count for more than a few
    # noisy pixels of tarmac texture at the same orientation.
    np.add.at(hist, indices.reshape(-1), magnitude.reshape(-1))
    return _l1(hist)


def _quality(crop_area_px: int, blur_variance: float) -> float:
    """Confidence that this crop's fingerprint means anything, in [0, 1].

    Two factors, multiplied rather than averaged: a crop that is large but smeared is
    as useless as one that is sharp but tiny, and averaging would let either factor
    hide the other.
    """
    size_term = (crop_area_px - MIN_CROP_AREA_PX) / (FULL_QUALITY_AREA_PX - MIN_CROP_AREA_PX)
    size_term = min(1.0, max(0.0, size_term))

    blur_term = (blur_variance - BLUR_VARIANCE_FLOOR) / (BLUR_VARIANCE_FULL - BLUR_VARIANCE_FLOOR)
    blur_term = min(1.0, max(0.0, blur_term))

    # Floor the product away from exactly zero: a crop that passed the area gate did
    # carry *some* signal, and a hard zero would make it algebraically invisible to a
    # scorer that multiplies by quality.
    return max(0.05, size_term * blur_term)


def describe(crop: NDArray[Any]) -> AppearanceDescriptor | None:
    """Fingerprint a vehicle crop, or return None if it is too small to be meaningful.

    Returning None rather than a low-quality descriptor is the point: a sighting with
    no fingerprint is still a sighting, still placed and timestamped, and still counts
    towards flow and dwell analytics. A sighting carrying a meaningless 72-float vector
    would instead pollute every nearest-neighbour search run against it.
    """
    if crop.ndim != 3 or crop.shape[2] != 3:
        return None

    height, width = int(crop.shape[0]), int(crop.shape[1])
    area = height * width
    if area < MIN_CROP_AREA_PX or height <= 0 or width <= 0:
        return None

    bgr = np.ascontiguousarray(crop, dtype=np.uint8)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    chroma_hist = _chroma_histogram(lab)
    lightness_hist = _lightness_histogram(lab)
    edge_hist = _edge_orientation_histogram(grey)
    vector = np.concatenate(
        [
            chroma_hist * CHROMA_WEIGHT,
            lightness_hist * LIGHTNESS_WEIGHT,
            edge_hist * EDGE_WEIGHT,
        ]
    )

    blur_variance = float(np.asarray(cv2.Laplacian(grey, cv2.CV_64F), dtype=np.float64).var())

    # The dominant colour is the mean Lab of the pixels in the most populated *chroma*
    # bin, not the mean of the whole crop: a crop always contains some road and some
    # shadow, and averaging those in turns every vehicle grey.
    #
    # Chroma rather than the full cube, and strongest-bin by mass rather than by count,
    # because an earlier version tie-broke on a joint Lab cube with `argmax` and
    # reported the *darker* half of an evenly split bright/dark crop as its dominant
    # colour -- argmax returns the first maximum, and on a tie that is whichever bin
    # happens to be indexed lower.
    lab_flat = np.asarray(lab, dtype=np.float64).reshape(-1, 3)
    a_bin = (lab_flat[:, 1] * CHROMA_BINS / 256).astype(np.int64).clip(0, CHROMA_BINS - 1)
    b_bin = (lab_flat[:, 2] * CHROMA_BINS / 256).astype(np.int64).clip(0, CHROMA_BINS - 1)
    bin_of_pixel = a_bin * CHROMA_BINS + b_bin

    # Ties are broken towards the brighter bin: a vehicle is the thing lit in the crop,
    # and its shadow is not a second vehicle.
    counts = np.bincount(bin_of_pixel, minlength=CHROMA_DIM).astype(np.float64)
    brightness = np.bincount(
        bin_of_pixel, weights=lab_flat[:, 0], minlength=CHROMA_DIM
    ) / np.maximum(counts, 1.0)
    dominant_bin = int(np.lexsort((brightness, counts))[-1])

    members = lab_flat[bin_of_pixel == dominant_bin]
    if members.size == 0:  # pragma: no cover -- the bin was chosen for having members
        members = lab_flat
    mean_lab = members.mean(axis=0)

    return AppearanceDescriptor(
        vector=[float(v) for v in vector],
        kind=DESCRIPTOR_KIND,
        # Converted to the conventional ranges for storage, so a stored value can be
        # read by a human and compared with a published colour reference.
        dominant_colour_lab={
            "L": round(float(mean_lab[0]) * 100.0 / 255.0, 2),
            "a": round(float(mean_lab[1]) - 128.0, 2),
            "b": round(float(mean_lab[2]) - 128.0, 2),
        },
        quality_score=round(_quality(area, blur_variance), 4),
        aspect_ratio=round(width / height, 4),
        crop_area_px=area,
        blur_variance=round(blur_variance, 3),
    )


def cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance in [0, 2], matching pgvector's `<=>` operator exactly.

    Exists so the Python scorer and a SQL nearest-neighbour query cannot disagree about
    what "distance" means. Tested against pgvector's own output.
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        # pgvector returns NaN for a zero vector; the platform treats an undefined
        # distance as "no evidence of similarity" rather than propagating NaN into a
        # confidence score.
        return 1.0
    return 1.0 - float(np.dot(va, vb) / (na * nb))


__all__ = [
    "AppearanceDescriptor",
    "CHROMA_BINS",
    "DESCRIPTOR_DIM",
    "DESCRIPTOR_KIND",
    "EDGE_BINS",
    "LIGHTNESS_BINS",
    "MIN_CROP_AREA_PX",
    "cosine_distance",
    "describe",
]
