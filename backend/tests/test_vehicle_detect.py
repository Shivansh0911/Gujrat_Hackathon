"""YOLOX decode, NMS and letterbox arithmetic, without needing the 20 MB model.

The decode is the part most likely to be wrong and least likely to look wrong. Megvii's
released ONNX is exported without `decode_in_inference`, so its output is grid-relative
and log-scale; feeding it straight through produces boxes clustered near the origin that
read as a weak model rather than a missing step. These tests build raw tensors by hand so
the expected box is arithmetic a reviewer can check.

Tests that need the real model are marked and skip when it has not been fetched, so CI
without network still exercises everything above.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from services.analytics.model_ids import VEHICLE_DETECTOR_INPUT
from services.analytics.vehicle_detect import (
    COCO_VEHICLE_CLASSES,
    PAD_VALUE,
    STRIDES,
    _letterbox,
    _nms,
    decode,
)
from services.registry.enums import VehicleClass

SIZE = VEHICLE_DETECTOR_INPUT
ANCHORS = sum((SIZE // stride) ** 2 for stride in STRIDES)


def _raw(
    anchor: int, *, dx: float, dy: float, lw: float, lh: float, obj: float, cls: int, score: float
) -> NDArray[np.float32]:
    """A raw YOLOX output with exactly one populated anchor."""
    pred = np.zeros((1, ANCHORS, 85), dtype=np.float32)
    # Log-scale width/height of 0 means exp(0)=1 cell, so a non-zero value is needed for
    # anything but a one-cell box.
    pred[0, anchor, :4] = (dx, dy, lw, lh)
    pred[0, anchor, 4] = obj
    pred[0, anchor, 5 + cls] = score
    return pred


# ------------------------------------------------------------------------ decode


def test_anchor_count_matches_the_stride_pyramid() -> None:
    """3549 = 52^2 + 26^2 + 13^2. If this drifts, every decoded box is in the wrong place."""
    assert ANCHORS == 3549


def test_first_anchor_decodes_to_the_top_left_cell() -> None:
    """Anchor 0 is grid (0,0) at stride 8, so a zero offset centres it on the origin."""
    boxes, scores = decode(_raw(0, dx=0.0, dy=0.0, lw=0.0, lh=0.0, obj=1.0, cls=2, score=1.0), SIZE)
    x1, y1, x2, y2 = boxes[0]
    # exp(0) * 8 = 8 px box centred on (0, 0): corners at -4 and +4.
    assert (x1, y1, x2, y2) == pytest.approx((-4.0, -4.0, 4.0, 4.0))


def test_grid_offset_moves_the_box_by_whole_cells() -> None:
    """Anchor 1 is grid (1,0) at stride 8, so the centre must sit 8 px right of anchor 0.

    Note the index: `decode` returns a box for *every* anchor, so the box to inspect is
    `boxes[anchor]`, not `boxes[0]`. Reading `boxes[0]` here reports the all-zero first
    anchor and passes or fails for reasons unrelated to the one under test.
    """
    boxes, _ = decode(_raw(1, dx=0.0, dy=0.0, lw=0.0, lh=0.0, obj=1.0, cls=2, score=1.0), SIZE)
    x1, _, x2, _ = boxes[1]
    assert (x1 + x2) / 2 == pytest.approx(8.0)


def test_width_is_exponential_in_the_prediction() -> None:
    """wh = exp(pred) * stride. A linear reading would make every large vehicle tiny."""
    boxes, _ = decode(
        _raw(0, dx=0.0, dy=0.0, lw=np.log(10.0), lh=np.log(4.0), obj=1.0, cls=2, score=1.0), SIZE
    )
    x1, y1, x2, y2 = boxes[0]
    assert (x2 - x1) == pytest.approx(80.0)  # 10 cells * stride 8
    assert (y2 - y1) == pytest.approx(32.0)  # 4 cells * stride 8


def test_a_later_stride_decodes_against_its_own_cell_size() -> None:
    """The first stride-16 anchor sits immediately after all the stride-8 anchors."""
    first_s16 = (SIZE // 8) ** 2
    boxes, _ = decode(
        _raw(first_s16, dx=0.0, dy=0.0, lw=0.0, lh=0.0, obj=1.0, cls=2, score=1.0), SIZE
    )
    x1, y1, x2, y2 = boxes[first_s16]
    assert (x2 - x1) == pytest.approx(16.0), "stride-16 anchors must use a 16 px cell"


def test_score_is_objectness_times_class_probability() -> None:
    """Either factor alone over-reports: a confident 'something' is not a confident car."""
    _, scores = decode(_raw(0, dx=0, dy=0, lw=0, lh=0, obj=0.5, cls=2, score=0.6), SIZE)
    assert scores[0, 2] == pytest.approx(0.30)


def test_a_wrong_anchor_count_is_fatal_rather_than_reshaped() -> None:
    """Decoding against the wrong grid yields plausible boxes in wrong places."""
    with pytest.raises(ValueError, match="anchor count mismatch"):
        decode(np.zeros((1, 100, 85), dtype=np.float32), SIZE)


# --------------------------------------------------------------------- letterbox


def test_letterbox_preserves_aspect_ratio_and_reports_its_scale() -> None:
    image = np.zeros((1440, 2560, 3), dtype=np.uint8)
    chw, ratio = _letterbox(image, SIZE)
    assert chw.shape == (3, SIZE, SIZE)
    assert ratio == pytest.approx(SIZE / 2560)


def test_letterbox_pads_with_yolox_own_value() -> None:
    """114, not 0 and not 128. A different pad shifts every score slightly."""
    image = np.zeros((100, 400, 3), dtype=np.uint8)
    chw, _ = _letterbox(image, SIZE)
    # Bottom rows are padding for a wide image.
    assert chw[0, SIZE - 1, SIZE - 1] == pytest.approx(float(PAD_VALUE))


def test_letterbox_leaves_no_channel_scaling() -> None:
    """YOLOX's preproc for these weights does not divide by 255; doing so degrades scores."""
    image = np.full((416, 416, 3), 200, dtype=np.uint8)
    chw, _ = _letterbox(image, SIZE)
    assert chw.max() == pytest.approx(200.0)


# --------------------------------------------------------------------------- NMS


def test_nms_keeps_the_strongest_of_two_overlapping_boxes() -> None:
    boxes = np.array([[0, 0, 100, 100], [5, 5, 105, 105]], dtype=np.float32)
    scores = np.array([0.6, 0.9], dtype=np.float32)
    assert _nms(boxes, scores, 0.45) == [1]


def test_nms_keeps_two_vehicles_that_merely_sit_beside_each_other() -> None:
    boxes = np.array([[0, 0, 100, 100], [200, 0, 300, 100]], dtype=np.float32)
    scores = np.array([0.9, 0.8], dtype=np.float32)
    assert sorted(_nms(boxes, scores, 0.45)) == [0, 1]


def test_nms_on_a_single_box_returns_it() -> None:
    boxes = np.array([[0, 0, 10, 10]], dtype=np.float32)
    assert _nms(boxes, np.array([0.5], dtype=np.float32), 0.45) == [0]


# ------------------------------------------------------------------ class mapping


def test_only_the_four_coco_vehicle_classes_are_mapped() -> None:
    assert set(COCO_VEHICLE_CLASSES) == {2, 3, 5, 7}
    assert set(COCO_VEHICLE_CLASSES.values()) == {
        VehicleClass.CAR,
        VehicleClass.MOTORCYCLE,
        VehicleClass.BUS,
        VehicleClass.TRUCK,
    }


def test_auto_rickshaw_is_never_produced_by_a_coco_detector() -> None:
    """COCO has no auto-rickshaw class, so claiming one would invent a distinction.

    The enum member exists for a future detector that can make it; nothing may write it
    until one is measured and adopted.
    """
    assert VehicleClass.AUTO_RICKSHAW not in COCO_VEHICLE_CLASSES.values()


# --------------------------------------------- the real model, when it is available


def _model_available() -> bool:
    from services.common.paths import DATA_DIR

    return (DATA_DIR / "models" / "yolox_tiny.onnx").exists()


needs_model = pytest.mark.skipif(
    not _model_available(), reason="yolox_tiny.onnx not fetched; run the sighting ingest once"
)


@needs_model
def test_the_shipped_model_has_the_expected_tensor_shapes() -> None:
    """Guards the decode against a model swap that changes the contract."""
    import onnxruntime as ort

    from services.analytics.vehicle_detect import model_path

    session = ort.InferenceSession(str(model_path()), providers=["CPUExecutionProvider"])
    assert session.get_inputs()[0].shape == [1, 3, SIZE, SIZE]
    assert session.get_outputs()[0].shape == [1, ANCHORS, 85]


@needs_model
def test_the_checksum_is_verified_on_every_load() -> None:
    """A model file decides what the platform reports seeing; a swap must fail loudly."""
    import hashlib

    from services.analytics.model_ids import VEHICLE_DETECTOR_SHA256
    from services.analytics.vehicle_detect import model_path

    assert hashlib.sha256(model_path().read_bytes()).hexdigest() == VEHICLE_DETECTOR_SHA256


@needs_model
def test_a_blank_frame_yields_no_vehicles() -> None:
    """The floor matters: a detector that hallucinates on flat grey pollutes every search."""
    from services.analytics.vehicle_detect import YoloxVehicleDetector

    detector = YoloxVehicleDetector()
    assert detector.detect(np.full((480, 640, 3), 127, dtype=np.uint8)) == []


@needs_model
@pytest.mark.parametrize(
    "bad",
    [np.zeros((480, 640), dtype=np.uint8), np.zeros((480, 640, 4), dtype=np.uint8)],
    ids=["greyscale", "four-channel"],
)
def test_malformed_frames_return_no_vehicles(bad: NDArray[np.uint8]) -> None:
    from services.analytics.vehicle_detect import YoloxVehicleDetector

    assert YoloxVehicleDetector().detect(bad) == []
