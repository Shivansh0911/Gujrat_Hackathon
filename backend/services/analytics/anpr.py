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
from numpy.typing import NDArray

from services.analytics.model_ids import DETECTOR_MODEL, RECOGNISER_MODEL
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
        self._prev: NDArray[Any] | None = None
        self.frames_seen = 0
        self.frames_passed = 0

    def reset(self) -> None:
        """Called on a scene discontinuity; the previous frame is no longer comparable."""
        self._prev = None

    def check(self, frame: NDArray[Any]) -> tuple[bool, float]:
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
    best_crop: NDArray[Any] | None = None
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


def _centre(b: tuple[int, int, int, int]) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _diag(b: tuple[int, int, int, int]) -> float:
    w, h = b[2] - b[0], b[3] - b[1]
    return float((w * w + h * h) ** 0.5) or 1.0


class PlateTracker:
    """Associates plate detections across frames, so reads can be fused.

    Association is what makes multi-frame fusion possible at all: without it, ten
    reads of one vehicle are ten unrelated detections rather than one observation
    with ten pieces of evidence.

    **Why this is not IoU alone.** It was, with a 0.25 threshold, and on real footage
    it associated almost nothing: 22 plate detections produced 14 tracks, 13 of them
    a single frame long. Sampling runs at 5 analytic fps, so consecutive looks at one
    vehicle are 200 ms apart, and in 200 ms a plate travels further than its own
    width -- especially in the own-feed clip, which is shot from a moving vehicle.
    Two boxes of the same plate then overlap by nothing at all and IoU is exactly
    zero. Multi-frame fusion was therefore dead code in practice, and every plate was
    decided by a single noisy read.

    So a second, motion-tolerant gate runs when IoU fails: match if the box centre
    moved less than `max_travel` box-diagonals, the box is a similar size, and the
    gap is short. Each condition is there to stop a different false merge -- distance
    bounds how far a vehicle can plausibly have gone, scale rejects a different plate
    at a different depth, and the time bound stops a track adopting an unrelated
    vehicle that happens to arrive later in the same part of the frame.

    Assignment is greedy over the best-scoring pairs rather than first-come per box,
    so two nearby plates cannot have their tracks swapped by iteration order.
    """

    def __init__(
        self,
        iou_threshold: float = 0.25,
        max_misses: int = 8,
        max_travel_diagonals: float = 2.5,
        max_scale_ratio: float = 2.5,
        max_gap_ms: float = 600.0,
    ) -> None:
        self._iou_threshold = iou_threshold
        self._max_misses = max_misses
        self._max_travel = max_travel_diagonals
        self._max_scale_ratio = max_scale_ratio
        self._max_gap_ms = max_gap_ms
        self._tracks: dict[int, PlateTrack] = {}
        self._next_id = 1

    def _affinity(self, track: PlateTrack, box: tuple[int, int, int, int], pts_ms: float) -> float:
        """How strongly `box` looks like the next sighting of `track`. 0 = no match."""
        iou = _iou(track.bbox, box)
        if iou >= self._iou_threshold:
            # Overlap is the strongest evidence; keep it ranked above any motion match.
            return 1.0 + iou

        gap = pts_ms - track.last_pts_ms
        if gap < 0 or gap > self._max_gap_ms:
            return 0.0

        tw, th = track.bbox[2] - track.bbox[0], track.bbox[3] - track.bbox[1]
        bw, bh = box[2] - box[0], box[3] - box[1]
        if tw <= 0 or th <= 0 or bw <= 0 or bh <= 0:
            return 0.0
        scale = max(bw / tw, tw / bw, bh / th, th / bh)
        if scale > self._max_scale_ratio:
            return 0.0

        (tx, ty), (bx, by) = _centre(track.bbox), _centre(box)
        travel = float(((tx - bx) ** 2 + (ty - by) ** 2) ** 0.5) / _diag(track.bbox)
        if travel > self._max_travel:
            return 0.0

        # Nearer is better, and never outranks a genuine IoU match.
        return 1.0 - (travel / self._max_travel)

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
        unmatched_boxes = set(range(len(boxes)))

        # Score every surviving pair, then take them best-first. Greedy over a
        # global ranking, not per box in arrival order, so the strongest evidence
        # wins a contested track instead of whichever box was considered first.
        candidates = [
            (self._affinity(self._tracks[tid], box, pts_ms), tid, bi)
            for bi, box in enumerate(boxes)
            for tid in self._tracks
        ]
        candidates = [c for c in candidates if c[0] > 0.0]
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

        assigned: dict[int, int] = {}  # box index -> track id
        for _score, cand_tid, cand_bi in candidates:
            if cand_tid in unmatched_tracks and cand_bi in unmatched_boxes:
                unmatched_tracks.discard(cand_tid)
                unmatched_boxes.discard(cand_bi)
                assigned[cand_bi] = cand_tid

        for bi, box in enumerate(boxes):
            matched_tid = assigned.get(bi)
            if matched_tid is not None:
                track = self._tracks[matched_tid]
                track.bbox = box
                track.last_pts_ms = pts_ms
                track.misses = 0
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
    def detect(self, image: NDArray[Any]) -> list[tuple[tuple[int, int, int, int], float]]: ...


class PlateRecogniser(Protocol):
    def read(self, crop: NDArray[Any]) -> tuple[str, list[float]]: ...


class OpenImagePlateDetector:
    """open-image-models YOLOv9 ONNX plate detector (MIT)."""

    def __init__(self, model: str = DETECTOR_MODEL) -> None:
        from open_image_models import LicensePlateDetector

        # The package types this parameter as a Literal of its bundled model names.
        # We keep `str` in our own signature so a caller can select a model from
        # configuration, and validate by letting the constructor raise.
        self._impl = LicensePlateDetector(detection_model=cast(Any, model))
        self.model_name = model

    def detect(self, image: NDArray[Any]) -> list[tuple[tuple[int, int, int, int], float]]:
        out = []
        for det in self._impl.predict(image):
            bb = det.bounding_box
            out.append(((int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)), float(det.confidence)))
        return out


class FastPlateRecogniser:
    """fast-plate-ocr CCT ONNX recogniser (MIT). Returns per-character confidences."""

    #: Indian registrations are up to TEN characters (XX00XX0000). A model's
    #: `max_plate_slots` is the number of classification heads it has, so a 9-slot
    #: model cannot emit a 10-character plate at all -- it is not a matter of
    #: accuracy, it is arithmetically impossible. `cct-s-v1-global-model` has 9
    #: slots and was the original default; every full-length Indian plate it ever
    #: read was wrong before inference began. `cct-s-v2-global-model` has 10.
    DEFAULT_MODEL = RECOGNISER_MODEL

    @classmethod
    def default_model(cls) -> str:
        """So the prefetch script cannot drift from what inference actually loads."""
        return cls.DEFAULT_MODEL

    def __init__(self, model: str | None = None) -> None:
        model = model or self.DEFAULT_MODEL
        from fast_plate_ocr import LicensePlateRecognizer

        self._impl = LicensePlateRecognizer(cast(Any, model))  # Literal, as above
        self.model_name = model

    def read(self, crop: NDArray[Any]) -> tuple[str, list[float]]:
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
    crop: NDArray[Any] | None
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
        crop_margin_x: float = 0.12,
        crop_margin_y: float = 0.20,
        min_publish_confidence: float = 0.5,
    ) -> None:
        self._detector = detector
        self._recogniser = recogniser
        self._crop_dir = crop_dir
        self._motion_threshold = motion_threshold
        # Sampling is expressed as a minimum PTS interval, never as "every Nth frame":
        # frame cadence is not uniform, so an interval is the only stable meaning.
        self._min_interval_ms = 1000.0 / analytic_fps if analytic_fps > 0 else 0.0
        self._min_detector_confidence = min_detector_confidence
        # Crop padding around the detector box, as a fraction of box size. Tunable
        # because it is not a free parameter: too tight clips the outer glyphs and
        # the recogniser silently returns a shorter plate, too loose feeds it
        # background that the fixed 128x64 input then squeezes the plate out of.
        # Swept against annotated ground truth on the own-feed clip: 0.06 yielded 6
        # grammar-valid reads, 0.12 yielded 8 and the only full-length read of the
        # 10-character plate, 0.20 fell back to 4. Measured, not guessed.
        self._crop_margin_x = crop_margin_x
        self._crop_margin_y = crop_margin_y
        # Below this fused confidence a read is not emitted at all.
        #
        # This is a deliberate bias towards silence. Reporting a wrong registration to
        # an investigator is worse than reporting nothing: nothing prompts them to look
        # again, a wrong plate sends them somewhere else entirely, and a wrong plate
        # carrying a high confidence is worse still because it will be believed.
        #
        # The value comes from the annotated crops rather than taste. Of the crops a
        # reviewer found illegible, every single pipeline read scored 0.46 or below,
        # while both reads that were exactly right scored 0.79 and 0.94. A cut at 0.5
        # removed all eleven false reads on illegible crops and kept both correct ones.
        #
        # Stated honestly: that is a small sample, and 0.5 is chosen as the round
        # number inside a wide gap rather than as an optimum. It is a parameter, and
        # it should be re-derived whenever the recogniser or the footage changes.
        self._min_publish_confidence = min_publish_confidence
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

    def _crop(self, image: NDArray[Any], box: tuple[int, int, int, int]) -> NDArray[Any] | None:
        x1, y1, x2, y2 = box
        h, w = image.shape[:2]
        # A small margin: plate detectors crop tight, and the recogniser does better
        # with a little surrounding context than with clipped glyph edges.
        mx, my = int((x2 - x1) * self._crop_margin_x), int((y2 - y1) * self._crop_margin_y)
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
        if fused.confidence < self._min_publish_confidence:
            # Not evidence of a vehicle, and saying so is the whole point. The crop is
            # not written either: an evidence image implies there is something to see.
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
