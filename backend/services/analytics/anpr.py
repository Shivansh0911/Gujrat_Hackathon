"""ANPR pipeline: decode -> motion gate -> plate detect -> OCR -> fuse -> persist.

Ordering is a throughput decision, not a style one. Across 30 cameras the detector is
the expensive stage, so the cheapest possible filter runs first: a downscaled frame
difference that costs microseconds and rejects the majority of frames on a road that
is empty most of the time. Everything downstream only ever sees frames where
something moved.

Timing is taken from `Frame.pts_ms` throughout. Nothing here reads a declared frame
rate or a frame arrival time -- the CI guard enforces that, and the reason is that the
gateway replays a buffered GOP on connect, so arrival-time reasoning produces
impossible velocities in the first seconds of every session.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Protocol, cast

import numpy as np

from services.analytics.plate_grammar import NormalisedPlate, PlateAccumulator
from services.common.cv_env import cv2
from services.common.stream_client import Frame

if TYPE_CHECKING:  # import cycle at runtime: ingest builds on analytics
    from services.ingest.source import CameraSource

log = logging.getLogger(__name__)


# ------------------------------------------------------------------- motion gate


class MotionGate:
    """Cheap frame-difference gate. The largest single throughput win at 30 cameras.

    Deliberately not a background subtractor: MOG2 keeps per-pixel state and costs
    far more than a downscaled absdiff, and at 2-5 analytic fps the extra precision
    buys nothing the detector will not resolve anyway.
    """

    def __init__(self, threshold: float = 2.5, work_size: tuple[int, int] = (160, 90)) -> None:
        self._threshold = threshold
        self._size = work_size
        self._prev: np.ndarray | None = None
        self.frames_seen = 0
        self.frames_passed = 0

    def reset(self) -> None:
        """Called on a scene discontinuity; the previous frame is no longer comparable."""
        self._prev = None

    def check(self, frame: np.ndarray) -> tuple[bool, float]:
        self.frames_seen += 1
        small = cv2.resize(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame,
            self._size,
            interpolation=cv2.INTER_AREA,
        )
        if self._prev is None:
            self._prev = small
            # The first frame of a segment always passes: we have nothing to compare
            # it against, and dropping it would lose the vehicle already in shot.
            self.frames_passed += 1
            return True, 0.0

        score = float(np.mean(cv2.absdiff(self._prev, small)))
        self._prev = small
        passed = score >= self._threshold
        if passed:
            self.frames_passed += 1
        return passed, score

    @property
    def pass_rate(self) -> float:
        """The parameter GPU sizing depends on. Reported per camera."""
        return self.frames_passed / self.frames_seen if self.frames_seen else 0.0


# ----------------------------------------------------------------------- tracking


@dataclass
class PlateTrack:
    """One vehicle's plate, observed across consecutive frames."""

    track_id: int
    bbox: tuple[int, int, int, int]
    accumulator: PlateAccumulator = field(default_factory=PlateAccumulator)
    first_pts_ms: float = 0.0
    last_pts_ms: float = 0.0
    misses: int = 0
    best_crop: np.ndarray | None = None
    best_crop_score: float = 0.0
    session_id: str = ""


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / float(area_a + area_b - inter)


class PlateTracker:
    """Associates plate detections across frames by IoU, so reads can be fused.

    Association is what makes multi-frame fusion possible at all: without it, ten
    reads of one vehicle are ten unrelated detections rather than one observation
    with ten pieces of evidence.
    """

    def __init__(self, iou_threshold: float = 0.25, max_misses: int = 8) -> None:
        self._iou_threshold = iou_threshold
        self._max_misses = max_misses
        self._tracks: dict[int, PlateTrack] = {}
        self._next_id = 1

    def reset(self) -> str:
        """Drop all tracks. Called on a scene discontinuity.

        Returns nothing useful, but the caller must flush closed tracks first: after a
        hard cut the vehicle in frame is a different vehicle, and carrying a track
        across the cut would merge two of them into one journey hop that never happened.
        """
        self._tracks.clear()
        return ""

    def update(
        self, boxes: list[tuple[int, int, int, int]], pts_ms: float, session_id: str
    ) -> list[tuple[PlateTrack, tuple[int, int, int, int]]]:
        """Match boxes to tracks. Returns (track, box) pairs for this frame."""
        matched: list[tuple[PlateTrack, tuple[int, int, int, int]]] = []
        unmatched_tracks = set(self._tracks)

        for box in boxes:
            best_id, best_iou = None, 0.0
            for tid in unmatched_tracks:
                score = _iou(self._tracks[tid].bbox, box)
                if score > best_iou:
                    best_id, best_iou = tid, score

            if best_id is not None and best_iou >= self._iou_threshold:
                track = self._tracks[best_id]
                track.bbox = box
                track.last_pts_ms = pts_ms
                track.misses = 0
                unmatched_tracks.discard(best_id)
            else:
                track = PlateTrack(
                    track_id=self._next_id,
                    bbox=box,
                    first_pts_ms=pts_ms,
                    last_pts_ms=pts_ms,
                    session_id=session_id,
                )
                self._tracks[self._next_id] = track
                self._next_id += 1
            matched.append((track, box))

        for tid in unmatched_tracks:
            self._tracks[tid].misses += 1
        return matched

    def collect_closed(self) -> list[PlateTrack]:
        """Remove and return tracks that have not been seen for `max_misses` frames."""
        closed = [t for t in self._tracks.values() if t.misses > self._max_misses]
        for track in closed:
            self._tracks.pop(track.track_id, None)
        return closed

    def flush(self) -> list[PlateTrack]:
        closed = list(self._tracks.values())
        self._tracks.clear()
        return closed


# ------------------------------------------------------------------ model wiring


class PlateDetector(Protocol):
    def detect(self, image: np.ndarray) -> list[tuple[tuple[int, int, int, int], float]]: ...


class PlateRecogniser(Protocol):
    def read(self, crop: np.ndarray) -> tuple[str, list[float]]: ...


class OpenImagePlateDetector:
    """open-image-models YOLOv9 ONNX plate detector (MIT)."""

    def __init__(self, model: str = "yolo-v9-t-384-license-plate-end2end") -> None:
        from open_image_models import LicensePlateDetector

        # The package types this parameter as a Literal of its bundled model names.
        # We keep `str` in our own signature so a caller can select a model from
        # configuration, and validate by letting the constructor raise.
        self._impl = LicensePlateDetector(detection_model=cast(Any, model))
        self.model_name = model

    def detect(self, image: np.ndarray) -> list[tuple[tuple[int, int, int, int], float]]:
        out = []
        for det in self._impl.predict(image):
            bb = det.bounding_box
            out.append(((int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)), float(det.confidence)))
        return out


class FastPlateRecogniser:
    """fast-plate-ocr CCT ONNX recogniser (MIT). Returns per-character confidences."""

    def __init__(self, model: str = "cct-s-v1-global-model") -> None:
        from fast_plate_ocr import LicensePlateRecognizer

        self._impl = LicensePlateRecognizer(cast(Any, model))  # Literal, as above
        self.model_name = model

    def read(self, crop: np.ndarray) -> tuple[str, list[float]]:
        preds = cast(Any, self._impl.run(crop, return_confidence=True))
        if not preds:
            return "", []
        pred = preds[0]
        text = str(pred.plate or "")
        probs = [float(p) for p in (pred.char_probs if pred.char_probs is not None else [])]
        # The model emits a padding symbol for short plates; drop it but keep the
        # confidences aligned to the characters that remain.
        keep = [(c, p) for c, p in zip(text, probs + [0.0] * len(text)) if c not in "_ "]
        if keep:
            text = "".join(c for c, _ in keep)
            probs = [p for _, p in keep]
        return text, probs


# --------------------------------------------------------------------- pipeline


@dataclass
class PlateDetectionRecord:
    """One fused plate observation, ready to persist."""

    camera_ref: str
    plate: NormalisedPlate
    first_pts_ms: float
    last_pts_ms: float
    observed_at_utc: datetime
    clock_confidence: float
    frames_fused: int
    detector_confidence: float
    crop: np.ndarray | None
    crop_path: str | None = None
    bbox: tuple[int, int, int, int] | None = None


@dataclass
class PipelineStats:
    frames_decoded: int = 0
    frames_gated_in: int = 0
    detector_runs: int = 0
    plates_detected: int = 0
    ocr_runs: int = 0
    records_emitted: int = 0
    discontinuities: int = 0
    wall_seconds: float = 0.0

    @property
    def gate_pass_rate(self) -> float:
        return self.frames_gated_in / self.frames_decoded if self.frames_decoded else 0.0

    @property
    def decode_fps(self) -> float:
        return self.frames_decoded / self.wall_seconds if self.wall_seconds else 0.0


class AnprPipeline:
    """Runs ANPR over any CameraSource."""

    def __init__(
        self,
        detector: PlateDetector,
        recogniser: PlateRecogniser,
        *,
        crop_dir: Path | None = None,
        motion_threshold: float = 2.5,
        analytic_fps: float = 5.0,
        min_detector_confidence: float = 0.35,
        min_crop_height: int = 16,
    ) -> None:
        self._detector = detector
        self._recogniser = recogniser
        self._crop_dir = crop_dir
        self._motion_threshold = motion_threshold
        # Sampling is expressed as a minimum PTS interval, never as "every Nth frame":
        # frame cadence is not uniform, so an interval is the only stable meaning.
        self._min_interval_ms = 1000.0 / analytic_fps if analytic_fps > 0 else 0.0
        self._min_detector_confidence = min_detector_confidence
        self._min_crop_height = min_crop_height

    def run(
        self, source: CameraSource, max_frames: int | None = None
    ) -> Iterator[PlateDetectionRecord]:
        """Process frames from a CameraSource, yielding fused plate records."""
        gate = MotionGate(threshold=self._motion_threshold)
        tracker = PlateTracker()
        stats = PipelineStats()
        self.stats = stats

        last_analysed_pts = -1e18
        last_session = ""
        started = time.monotonic()

        for frame in source.open():
            stats.frames_decoded += 1

            if frame.session_id != last_session:
                if last_session:
                    # A discontinuity: flush what we have as evidence, then reset.
                    # Evidence already gathered is preserved; only the association
                    # state is discarded.
                    stats.discontinuities += 1
                    for track in tracker.flush():
                        record = self._finalise(track, source)
                        if record is not None:
                            stats.records_emitted += 1
                            yield record
                    gate.reset()
                last_session = frame.session_id

            # Adaptive sampling on PTS, so an uneven cadence does not change the rate.
            if frame.pts_ms - last_analysed_pts < self._min_interval_ms:
                continue
            last_analysed_pts = frame.pts_ms

            moved, _score = gate.check(frame.image)
            if not moved:
                continue
            stats.frames_gated_in += 1

            stats.detector_runs += 1
            detections = [
                (box, conf)
                for box, conf in self._detector.detect(frame.image)
                if conf >= self._min_detector_confidence
            ]
            stats.plates_detected += len(detections)

            boxes = [box for box, _ in detections]
            confs = {box: conf for box, conf in detections}

            for track, box in tracker.update(boxes, frame.pts_ms, frame.session_id):
                crop = self._crop(frame.image, box)
                if crop is None:
                    continue
                text, char_confs = self._recogniser.read(crop)
                stats.ocr_runs += 1
                if not text:
                    continue
                track.accumulator.add(text, char_confs, frame.pts_ms)

                # Keep the sharpest crop as the evidence image, not the last one: the
                # frame a reviewer sees should be the best available, and sharpness is
                # a good proxy for legibility.
                sharpness = float(
                    cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                )
                score = sharpness * confs.get(box, 0.0)
                if score > track.best_crop_score:
                    track.best_crop_score = score
                    track.best_crop = crop.copy()
                    track.bbox = box

            for track in tracker.collect_closed():
                record = self._finalise(track, source)
                if record is not None:
                    stats.records_emitted += 1
                    yield record

            if max_frames is not None and stats.frames_decoded >= max_frames:
                break

        for track in tracker.flush():
            record = self._finalise(track, source)
            if record is not None:
                stats.records_emitted += 1
                yield record

        stats.wall_seconds = time.monotonic() - started

    # ------------------------------------------------------------------ helpers

    def _crop(self, image: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray | None:
        x1, y1, x2, y2 = box
        h, w = image.shape[:2]
        # A small margin: plate detectors crop tight, and the recogniser does better
        # with a little surrounding context than with clipped glyph edges.
        mx, my = int((x2 - x1) * 0.06), int((y2 - y1) * 0.18)
        x1, y1 = max(0, x1 - mx), max(0, y1 - my)
        x2, y2 = min(w, x2 + mx), min(h, y2 + my)
        if x2 - x1 < 8 or y2 - y1 < self._min_crop_height:
            # Below this the glyphs carry too few pixels to read; attempting it wastes
            # OCR budget and produces confident nonsense.
            return None
        return image[y1:y2, x1:x2]

    def _finalise(self, track: PlateTrack, source: CameraSource) -> PlateDetectionRecord | None:
        fused = track.accumulator.fused()
        if fused is None or not fused.normalised:
            return None

        crop_path = None
        if self._crop_dir is not None and track.best_crop is not None:
            self._crop_dir.mkdir(parents=True, exist_ok=True)
            name = f"{source.camera_ref}_{int(track.first_pts_ms)}_{fused.normalised}.jpg"
            path = self._crop_dir / name
            cv2.imwrite(str(path), track.best_crop)
            crop_path = (
                str(path.relative_to(self._crop_dir.parent.parent))
                if self._crop_dir.is_absolute() is False
                else str(path)
            )

        # Timestamp from the FIRST observation of the track: that is when the vehicle
        # was at this camera. Using the last would bias every sighting later by the
        # duration the vehicle stayed in frame.
        anchor = Frame(
            image=np.empty((0, 0)),
            pts_ms=track.first_pts_ms,
            seq=0,
            session_id=track.session_id,
            wall_recv_ts=0.0,
        )
        return PlateDetectionRecord(
            camera_ref=source.camera_ref,
            plate=fused,
            first_pts_ms=track.first_pts_ms,
            last_pts_ms=track.last_pts_ms,
            observed_at_utc=source.observed_at(anchor),
            clock_confidence=source.clock_confidence,
            frames_fused=track.accumulator.frame_count,
            detector_confidence=track.best_crop_score,
            crop=track.best_crop,
            crop_path=crop_path,
            bbox=track.bbox,
        )
