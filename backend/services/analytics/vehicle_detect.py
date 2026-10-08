"""Vehicle detection: the thing that has to exist before a vehicle can be a sighting.

Why a second detector at all
----------------------------
The ANPR pipeline's only detector is `yolo-v9-t-384-license-plate-end2end`, which finds
*plates*. `PlateTracker` associates plate boxes, `PlateDetectionRecord.bbox` is a plate
box, and `persistence.py` writes that box into a column named `detection.vehicle_bbox`.
The name is misleading and was taken at face value when §3 of the Phase 2 brief was
written: there is no vehicle box anywhere in the schema, and
`vehicle_counting.py`'s own docstring says so outright -- "a detection is a *plate read*,
not a vehicle".

The consequence is that today the platform only learns a vehicle existed when its plate
was legible, which DISCOVERY finding 22 measured at one grammar-valid read per ~3,900
frames. Appearance-based identity has nothing to fingerprint until something finds the
vehicles themselves. That is what this module is.

Model choice, and the licence constraint that drove it
------------------------------------------------------
Ultralytics YOLO is AGPL and is excluded from this project on licence grounds (ADR
0003). That rules out the obvious choice and most tutorial code.

Shipped: **YOLOX-tiny**, Apache-2.0, from Megvii's official release. 20 MB ONNX, CPU
inference through the `onnxruntime` already in the dependency set, 80 COCO classes of
which four are vehicles. Considered and not shipped:

* `ssd_mobilenet_v1` (ONNX Model Zoo, Apache-2.0) -- simpler output, no decode to write,
  but markedly weaker on small objects, and small objects are the entire problem here:
  the median government-feed vehicle presents ~27 px.
* `tiny-yolov3` (ONNX Model Zoo, MIT) -- older and weaker again.
* RT-DETR (Apache-2.0) -- stronger, but an order of magnitude more compute than a free
  Render instance has.

Whether YOLOX-tiny actually finds a 27 px vehicle is a measurement, not an assumption,
and it is recorded in DISCOVERY alongside the ANPR ceiling. A negative result there is
a publishable finding, not a reason to quietly claim the capability.

The decode is ours
------------------
Megvii's released ONNX is exported *without* `decode_in_inference`, so the single
[1, 3549, 85] output is raw per-anchor prediction: 3549 = 52^2 + 26^2 + 13^2 anchors over
strides 8/16/32, and 85 = 4 box + 1 objectness + 80 class scores. Box values are
grid-relative and log-scale, so they are meaningless until reconstructed against the
stride grid. Feeding them straight to a consumer yields boxes clustered near the origin,
which looks like a bad model rather than a missing step.

No normalisation is applied to the input. YOLOX's own `preproc` for these weights keeps
BGR order and does not divide by 255; adding either silently degrades every score.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from services.analytics.model_ids import (
    VEHICLE_DETECTOR_INPUT,
    VEHICLE_DETECTOR_MODEL,
    VEHICLE_DETECTOR_SHA256,
    VEHICLE_DETECTOR_URL,
)
from services.common.cv_env import cv2
from services.registry.enums import VehicleClass

log = logging.getLogger(__name__)

#: COCO class indices that are vehicles, mapped to our own taxonomy.
#:
#: `VehicleClass.AUTO_RICKSHAW` is deliberately absent: COCO has no such class, so a
#: three-wheeler is reported by this model as a car, a truck or a motorcycle depending on
#: angle. Writing `AUTO_RICKSHAW` from a COCO detector would be inventing a distinction
#: the model cannot make. The enum member exists and stays unwritten until a detector
#: that can tell them apart is measured -- see its docstring.
COCO_VEHICLE_CLASSES: dict[int, VehicleClass] = {
    2: VehicleClass.CAR,
    3: VehicleClass.MOTORCYCLE,
    5: VehicleClass.BUS,
    7: VehicleClass.TRUCK,
}

#: YOLOX's feature-pyramid strides. The anchor count follows from these and the input
#: size; a mismatch is a hard error rather than a reshape, because silently decoding
#: against the wrong grid produces plausible-looking boxes in the wrong places.
STRIDES: tuple[int, ...] = (8, 16, 32)

#: Letterbox padding value from YOLOX's own preproc. Not 0 and not 128.
PAD_VALUE: int = 114

#: Minimum score for a box to be emitted at all.
#:
#: Lower than the plate pipeline's floor on purpose. A false plate read sends an
#: investigator to the wrong vehicle; a false vehicle box produces one spurious sighting
#: that carries a quality score and a fingerprint nobody will match. The costs are not
#: comparable, so the thresholds should not be either.
MIN_SCORE: float = 0.30

#: IoU above which two boxes of the same class are the same vehicle.
NMS_IOU: float = 0.45


@dataclass(frozen=True)
class VehicleBox:
    """One detected vehicle in frame-pixel coordinates."""

    bbox: tuple[int, int, int, int]
    vehicle_class: VehicleClass
    confidence: float
    #: Raw COCO index, retained so a reviewer can see what the model actually said
    #: rather than only our mapping of it.
    coco_class: int

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]


class VehicleDetector(Protocol):
    """Mirrors `PlateDetector`'s shape so both can be faked the same way in tests."""

    def detect(self, image: NDArray[Any]) -> list[VehicleBox]: ...


def _letterbox(image: NDArray[Any], size: int) -> tuple[NDArray[np.float32], float]:
    """Resize preserving aspect ratio onto a padded square, returning the scale used.

    The scale is returned rather than recomputed by the caller because undoing it is
    the step that puts boxes back in frame coordinates, and a caller that recomputes it
    from the output shape gets it subtly wrong on odd-sized frames.
    """
    height, width = int(image.shape[0]), int(image.shape[1])
    ratio = min(size / height, size / width)
    new_h, new_w = int(round(height * ratio)), int(round(width * ratio))

    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    if new_h > 0 and new_w > 0:
        canvas[:new_h, :new_w] = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    # CHW float32, BGR order, no /255: see the module docstring.
    chw = np.ascontiguousarray(canvas.transpose(2, 0, 1), dtype=np.float32)
    return chw, ratio


def _grid_and_strides(size: int) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Anchor centres and their strides, in the order YOLOX emits them."""
    grids: list[NDArray[np.float32]] = []
    strides: list[NDArray[np.float32]] = []
    for stride in STRIDES:
        cells = size // stride
        ys, xs = np.meshgrid(np.arange(cells), np.arange(cells), indexing="ij")
        grid = np.stack((xs, ys), axis=2).reshape(-1, 2).astype(np.float32)
        grids.append(grid)
        strides.append(np.full((grid.shape[0], 1), stride, dtype=np.float32))
    return np.concatenate(grids, axis=0), np.concatenate(strides, axis=0)


def decode(raw: NDArray[Any], size: int) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Turn YOLOX's raw output into xyxy boxes and per-anchor class scores.

    Separated from the session so it can be tested without the 20 MB model: given a
    hand-built raw tensor, the boxes it produces are arithmetic and checkable.
    """
    pred = np.asarray(raw, dtype=np.float32).reshape(raw.shape[-2], raw.shape[-1])
    grid, strides = _grid_and_strides(size)
    if grid.shape[0] != pred.shape[0]:
        raise ValueError(
            f"anchor count mismatch: model emitted {pred.shape[0]} anchors, strides "
            f"{STRIDES} at input {size} imply {grid.shape[0]}. Decoding against the "
            f"wrong grid yields plausible boxes in wrong places, so this is fatal."
        )

    centres = (pred[:, 0:2] + grid) * strides
    sizes = np.exp(pred[:, 2:4]) * strides
    half = sizes / 2.0
    boxes = np.concatenate([centres - half, centres + half], axis=1)

    objectness = pred[:, 4:5]
    class_scores = pred[:, 5:]
    return boxes, objectness * class_scores


def _nms(
    boxes: NDArray[np.float32], scores: NDArray[np.float32], iou_threshold: float
) -> list[int]:
    """Greedy NMS. Written out rather than taken from cv2.dnn so the behaviour is ours.

    `cv2.dnn.NMSBoxes` would do, but it has changed its return shape between OpenCV
    minor versions, and this codebase pins OpenCV for the FFmpeg backend rather than
    for its DNN module.
    """
    order = np.argsort(-scores)
    keep: list[int] = []
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])

    while order.size > 0:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        rest = order[1:]

        x1 = np.maximum(boxes[best, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[best, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[best, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[best, 3], boxes[rest, 3])
        inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        union = areas[best] + areas[rest] - inter
        iou = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)

        order = rest[iou <= iou_threshold]
    return keep


def model_path() -> Path:
    """Where the ONNX lives, downloading it on first use.

    Downloaded rather than committed: 20 MB of binary in git for a file that is
    byte-identical to an upstream release is a poor trade, and the two models already in
    use (`open-image-models`, `fast-plate-ocr`) fetch themselves the same way.

    The checksum is verified every time, not just on download. A model file is executable
    content in every sense that matters -- it decides what the platform reports having
    seen -- so a silently corrupted or substituted one must fail loudly.
    """
    from services.common.paths import DATA_DIR

    cache = DATA_DIR / "models"
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / f"{VEHICLE_DETECTOR_MODEL}.onnx"

    if not dst.exists():
        import requests

        log.info("fetching %s (%s)", VEHICLE_DETECTOR_MODEL, VEHICLE_DETECTOR_URL)
        tmp = dst.with_suffix(".onnx.part")
        with requests.get(
            VEHICLE_DETECTOR_URL,
            stream=True,
            timeout=120,
            headers={"User-Agent": "setu-platform"},
        ) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(1 << 16):
                    handle.write(chunk)
        # Rename only after a complete write, so an interrupted download cannot leave a
        # truncated file that looks cached.
        tmp.replace(dst)

    import hashlib

    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
    if digest != VEHICLE_DETECTOR_SHA256:
        raise RuntimeError(
            f"{dst} has sha256 {digest}, expected {VEHICLE_DETECTOR_SHA256}. Refusing to "
            f"load a vehicle detector that is not the reviewed one. Delete the file to "
            f"re-fetch it."
        )
    return dst


class YoloxVehicleDetector:
    """YOLOX-tiny ONNX vehicle detector (Apache-2.0)."""

    def __init__(
        self,
        *,
        min_score: float = MIN_SCORE,
        nms_iou: float = NMS_IOU,
        input_size: int = VEHICLE_DETECTOR_INPUT,
    ) -> None:
        import onnxruntime as ort

        self.model_name = VEHICLE_DETECTOR_MODEL
        self._size = input_size
        self._min_score = min_score
        self._nms_iou = nms_iou
        # One intra-op thread. A free Render instance is 0.1 CPU; letting onnxruntime
        # spawn a thread per core there costs context switching and buys nothing. The
        # same reasoning set SETU_WORKERS=1.
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path()), options, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        # onnxruntime sessions are documented as thread-safe for Run, but the ingest
        # pipeline shares one detector across camera workers and a lock here is
        # cheaper than reasoning about provider-specific exceptions later.
        self._lock = threading.Lock()

    def detect(self, image: NDArray[Any]) -> list[VehicleBox]:
        if image.ndim != 3 or image.shape[2] != 3:
            return []

        chw, ratio = _letterbox(image, self._size)
        with self._lock:
            outputs = self._session.run(None, {self._input_name: chw[None, ...]})

        boxes, scores = decode(outputs[0], self._size)

        # Vehicle classes only. Filtering before NMS rather than after keeps a bus from
        # suppressing the car beside it through a shared box.
        results: list[VehicleBox] = []
        height, width = int(image.shape[0]), int(image.shape[1])
        for coco_index, mapped in COCO_VEHICLE_CLASSES.items():
            class_score = scores[:, coco_index]
            hits = np.nonzero(class_score >= self._min_score)[0]
            if hits.size == 0:
                continue
            kept_boxes = boxes[hits]
            kept_scores = class_score[hits]
            for keep in _nms(kept_boxes, kept_scores, self._nms_iou):
                # Undo the letterbox scale, then clamp into the frame. Clamping rather
                # than discarding: a vehicle entering frame is genuinely half outside it.
                x1, y1, x2, y2 = (kept_boxes[keep] / ratio).tolist()
                bbox = (
                    max(0, min(width - 1, int(round(x1)))),
                    max(0, min(height - 1, int(round(y1)))),
                    max(0, min(width, int(round(x2)))),
                    max(0, min(height, int(round(y2)))),
                )
                if bbox[2] - bbox[0] < 2 or bbox[3] - bbox[1] < 2:
                    continue
                results.append(
                    VehicleBox(
                        bbox=bbox,
                        vehicle_class=mapped,
                        confidence=float(kept_scores[keep]),
                        coco_class=coco_index,
                    )
                )

        results.sort(key=lambda box: box.confidence, reverse=True)
        return results


__all__ = [
    "COCO_VEHICLE_CLASSES",
    "MIN_SCORE",
    "NMS_IOU",
    "STRIDES",
    "VehicleBox",
    "VehicleDetector",
    "YoloxVehicleDetector",
    "decode",
    "model_path",
]
