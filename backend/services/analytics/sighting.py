"""Turning frames into vehicle sightings, with or without a readable plate.

This is the ingest half of §3. `vehicle_detect` finds vehicles in a frame; this module
associates them across frames into passes, picks the best crop of each pass, fingerprints
it, and emits one record per pass rather than one per frame.

One record per pass, not per frame
----------------------------------
A vehicle crossing a camera at 5 analytic fps appears in 10-30 frames. Writing a sighting
for each would make "how many vehicles passed" a question about frame rate, inflate every
nearest-neighbour search by an order of magnitude, and bury the one good crop among
dozens of poor ones. `vehicle_counting.py` already carries this warning about plate reads
-- "one car in view for two seconds is not two cars" -- and the same arithmetic applies
here with more force, because every vehicle is now seen rather than only the legible ones.

So the tracker groups frames into a pass, and the pass emits a single record built from
its **best** frame: largest, sharpest crop wins, because that is the frame whose
fingerprint is worth comparing. `frames_seen` is carried on the record so a reviewer can
tell a solid eight-frame pass from a one-frame glimpse.

Association is `PlateTracker`'s, for its reasons
------------------------------------------------
IoU alone associates almost nothing at 5 fps -- measured, see `PlateTracker`'s docstring:
22 plate detections produced 14 tracks, 13 of them one frame long, because in 200 ms a
box travels further than its own width. The same motion-tolerant fallback is used here,
with looser bounds: a vehicle box is far larger than a plate box, so the travel and scale
gates that suit a plate are unnecessarily tight for the car it is bolted to.

Scene cuts
----------
The own-feed clips are recordings replayed on a loop, and the government feed wraps the
same way. At the wrap the scene changes completely while the tracker's state says a
vehicle is mid-pass. Carrying a track across that boundary would fuse two different
vehicles into one pass and produce a sighting that never happened, so a discontinuity
flushes every open track before resetting -- evidence already gathered is kept, only the
association state is discarded. This mirrors `AnprPipeline` exactly.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np
from numpy.typing import NDArray

from services.analytics.appearance import AppearanceDescriptor, describe
from services.analytics.boxes import Box, centre, diagonal, iou
from services.analytics.vehicle_detect import VehicleBox, VehicleDetector
from services.common.cv_env import cv2
from services.registry.enums import VehicleClass

if TYPE_CHECKING:
    from services.ingest.source import CameraSource

log = logging.getLogger(__name__)


@dataclass
class VehicleTrack:
    """One vehicle's pass across one camera, accumulated over consecutive frames."""

    track_id: int
    bbox: Box
    vehicle_class: VehicleClass
    class_confidence: float
    first_pts_ms: float
    last_pts_ms: float
    session_id: str
    #: Wall-clock of the frame the track opened on, resolved by the source. Captured at
    #: open rather than recomputed at close: `CameraSource.observed_at` takes a `Frame`,
    #: and by the time a track closes that frame is long gone.
    observed_at_utc: datetime
    frames_seen: int = 1
    misses: int = 0
    #: The box the track opened on, kept so displacement across the pass can be
    #: measured at close without retaining every frame's box.
    first_bbox: Box = (0, 0, 0, 0)
    #: Best crop of the pass, and the score that won it. See `_crop_score`.
    best_crop: NDArray[Any] | None = None
    best_crop_score: float = 0.0
    best_bbox: Box = (0, 0, 0, 0)
    best_frame_size: tuple[int, int] = (0, 0)
    #: Class votes across the pass. A vehicle does not change type mid-pass, so the
    #: majority over the whole pass beats whatever the last frame happened to say.
    class_votes: dict[VehicleClass, float] = field(default_factory=dict)


@dataclass(frozen=True)
class VehicleSightingRecord:
    """One pass of one vehicle, ready to persist."""

    camera_ref: str
    track_id: str
    vehicle_class: VehicleClass
    class_confidence: float
    bbox: Box
    frame_width: int
    frame_height: int
    first_pts_ms: float
    last_pts_ms: float
    observed_at_utc: datetime
    clock_confidence: float
    frames_seen: int
    crop: NDArray[Any] | None
    crop_path: str | None
    appearance: AppearanceDescriptor | None
    #: Distance the box centre travelled across the pass, in frame pixels. A long
    #: pass with near-zero displacement is a vehicle that stayed put.
    displacement_px: float = 0.0

    @property
    def rel_area(self) -> float:
        frame = self.frame_width * self.frame_height
        if frame <= 0:
            return 0.0
        w, h = self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]
        return min(1.0, (w * h) / frame)

    @property
    def rel_cx(self) -> float:
        return centre(self.bbox)[0] / self.frame_width if self.frame_width else 0.0

    @property
    def rel_cy(self) -> float:
        return centre(self.bbox)[1] / self.frame_height if self.frame_height else 0.0


@dataclass
class SightingStats:
    frames_decoded: int = 0
    frames_analysed: int = 0
    vehicles_detected: int = 0
    tracks_opened: int = 0
    records_emitted: int = 0
    fingerprinted: int = 0
    #: Passes whose best crop was still too small or too blurred to fingerprint. Counted
    #: rather than hidden: on the government estate this is expected to be most of them,
    #: and the ratio is the honest headline number for appearance coverage.
    below_quality_gate: int = 0
    discontinuities: int = 0
    wall_seconds: float = 0.0


def _crop_score(bbox: Box, sharpness: float) -> float:
    """Which frame of a pass carries the fingerprint worth storing.

    Area times sharpness. Area because a larger crop has more colour samples per
    histogram bin; sharpness because a large smeared crop is worse than a smaller clean
    one. Multiplied rather than summed so neither can mask the other -- the same
    reasoning as `appearance._quality`, which then re-derives both independently.
    """
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return float(max(0, w) * max(0, h)) * max(sharpness, 1e-6)


class VehicleTracker:
    """Associates vehicle boxes across frames. `PlateTracker`'s algorithm, looser gates.

    The gate values differ from `PlateTracker`'s because the thing being tracked is
    ~20x larger. A plate moving two of its own diagonals is a plausible 200 ms of
    travel; a bus moving two of *its* diagonals has crossed the frame. Travel is
    therefore allowed to be a smaller multiple of a much bigger box, and the scale gate
    is tightened, because a vehicle's apparent size changes smoothly as it approaches
    while a plate's can jump as it is resolved.

    **`max_gap_ms` bounds the motion branch only, not the overlap branch.** A box that
    still overlaps strongly is treated as the same vehicle however much time has passed.
    That is deliberate rather than an oversight: a parked, queuing or broken-down
    vehicle is precisely a high-overlap box across a long gap, and §7.2's dwell and
    abandonment detection needs it to stay one sighting instead of fragmenting into one
    per traffic-light cycle. What bounds an abandoned track is `max_misses` -- once the
    vehicle stops being detected the track closes, and a later arrival in the same part
    of the frame opens a new one.
    """

    def __init__(
        self,
        iou_threshold: float = 0.20,
        max_misses: int = 5,
        max_travel_diagonals: float = 1.2,
        max_scale_ratio: float = 1.8,
        max_gap_ms: float = 800.0,
    ) -> None:
        self._iou_threshold = iou_threshold
        self._max_misses = max_misses
        self._max_travel = max_travel_diagonals
        self._max_scale_ratio = max_scale_ratio
        self._max_gap_ms = max_gap_ms
        self._tracks: dict[int, VehicleTrack] = {}
        self._next_id = 1

    def _affinity(self, track: VehicleTrack, box: Box, pts_ms: float) -> float:
        overlap = iou(track.bbox, box)
        if overlap >= self._iou_threshold:
            return 1.0 + overlap

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

        (tx, ty), (bx, by) = centre(track.bbox), centre(box)
        travel = float(((tx - bx) ** 2 + (ty - by) ** 2) ** 0.5) / diagonal(track.bbox)
        if travel > self._max_travel:
            return 0.0
        return 1.0 - (travel / self._max_travel)

    def update(
        self,
        detections: list[VehicleBox],
        pts_ms: float,
        session_id: str,
        observed_at_utc: datetime,
    ) -> list[tuple[VehicleTrack, VehicleBox]]:
        """Match this frame's boxes to open tracks, opening new ones for the rest."""
        matched: list[tuple[VehicleTrack, VehicleBox]] = []
        unmatched_tracks = set(self._tracks)
        unmatched = set(range(len(detections)))

        # Greedy over a global ranking rather than per box in arrival order, so two
        # vehicles close together cannot have their tracks swapped by iteration order.
        scored: list[tuple[float, int, int]] = []
        for track_id in unmatched_tracks:
            for index in unmatched:
                affinity = self._affinity(self._tracks[track_id], detections[index].bbox, pts_ms)
                if affinity > 0.0:
                    scored.append((affinity, track_id, index))
        scored.sort(reverse=True)

        for _, track_id, index in scored:
            if track_id not in unmatched_tracks or index not in unmatched:
                continue
            unmatched_tracks.discard(track_id)
            unmatched.discard(index)
            track = self._tracks[track_id]
            detection = detections[index]
            track.bbox = detection.bbox
            track.last_pts_ms = pts_ms
            track.frames_seen += 1
            track.misses = 0
            track.class_votes[detection.vehicle_class] = (
                track.class_votes.get(detection.vehicle_class, 0.0) + detection.confidence
            )
            matched.append((track, detection))

        for index in sorted(unmatched):
            detection = detections[index]
            track = VehicleTrack(
                track_id=self._next_id,
                bbox=detection.bbox,
                vehicle_class=detection.vehicle_class,
                class_confidence=detection.confidence,
                first_pts_ms=pts_ms,
                last_pts_ms=pts_ms,
                session_id=session_id,
                observed_at_utc=observed_at_utc,
                first_bbox=detection.bbox,
                class_votes={detection.vehicle_class: detection.confidence},
            )
            self._tracks[self._next_id] = track
            self._next_id += 1
            matched.append((track, detection))

        for track_id in unmatched_tracks:
            self._tracks[track_id].misses += 1

        return matched

    def closed(self) -> list[VehicleTrack]:
        """Tracks that have missed too many frames. Removed from the tracker."""
        done = [t for t in self._tracks.values() if t.misses > self._max_misses]
        for track in done:
            del self._tracks[track.track_id]
        return done

    def flush(self) -> list[VehicleTrack]:
        """Close every open track. Called at a scene cut and at end of stream."""
        done = list(self._tracks.values())
        self._tracks.clear()
        return done

    @property
    def open_tracks(self) -> int:
        return len(self._tracks)


class SightingPipeline:
    """Runs vehicle detection and fingerprinting over any `CameraSource`."""

    def __init__(
        self,
        detector: VehicleDetector,
        *,
        crop_dir: Path | None = None,
        analytic_fps: float = 5.0,
        min_track_frames: int = 2,
    ) -> None:
        self._detector = detector
        self._crop_dir = crop_dir
        # A minimum PTS interval, never "every Nth frame": frame cadence is not uniform,
        # so an interval is the only stable meaning. Same reasoning as AnprPipeline.
        self._min_interval_ms = 1000.0 / analytic_fps if analytic_fps > 0 else 0.0
        # A one-frame track is as likely to be a detector blink as a vehicle. Two frames
        # is the weakest evidence worth recording; it is a parameter because the right
        # value depends on analytic fps and on how fast traffic crosses the frame.
        self._min_track_frames = min_track_frames
        self.stats = SightingStats()

    def run(
        self, source: CameraSource, max_frames: int | None = None
    ) -> Iterator[VehicleSightingRecord]:
        tracker = VehicleTracker()
        stats = SightingStats()
        self.stats = stats

        last_analysed_pts = -1e18
        last_session = ""
        started = time.monotonic()

        for frame in source.open():
            stats.frames_decoded += 1

            if frame.session_id != last_session:
                if last_session:
                    stats.discontinuities += 1
                    for track in tracker.flush():
                        record = self._finalise(track, source)
                        if record is not None:
                            stats.records_emitted += 1
                            yield record
                last_session = frame.session_id
                last_analysed_pts = -1e18

            if frame.pts_ms - last_analysed_pts < self._min_interval_ms:
                continue
            last_analysed_pts = frame.pts_ms
            stats.frames_analysed += 1

            detections = self._detector.detect(frame.image)
            stats.vehicles_detected += len(detections)

            before = tracker.open_tracks
            observed = source.observed_at(frame)
            for track, detection in tracker.update(
                detections, frame.pts_ms, frame.session_id, observed
            ):
                self._consider_crop(track, detection, frame.image)
            stats.tracks_opened += max(0, tracker.open_tracks - before)

            for track in tracker.closed():
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

    def _consider_crop(
        self, track: VehicleTrack, detection: VehicleBox, image: NDArray[Any]
    ) -> None:
        """Keep this frame's crop if it is the best view of the pass so far."""
        x1, y1, x2, y2 = detection.bbox
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return

        grey = cv2.cvtColor(np.ascontiguousarray(crop, dtype=np.uint8), cv2.COLOR_BGR2GRAY)
        sharpness = float(np.asarray(cv2.Laplacian(grey, cv2.CV_64F), dtype=np.float64).var())
        score = _crop_score(detection.bbox, sharpness)
        if score > track.best_crop_score:
            track.best_crop_score = score
            track.best_crop = crop.copy()
            track.best_bbox = detection.bbox
            track.best_frame_size = (int(image.shape[1]), int(image.shape[0]))

    def _finalise(self, track: VehicleTrack, source: CameraSource) -> VehicleSightingRecord | None:
        """Build a record from a closed track, or drop it as too thin to be evidence."""
        if track.frames_seen < self._min_track_frames or track.best_crop is None:
            return None

        # Majority class over the whole pass, weighted by confidence. A single frame's
        # opinion is noisy at the angles CCTV gives; the pass as a whole is not.
        winner = max(track.class_votes.items(), key=lambda kv: kv[1])
        total = sum(track.class_votes.values()) or 1.0
        vehicle_class, class_confidence = winner[0], min(1.0, winner[1] / total)

        appearance = describe(track.best_crop)
        if appearance is None:
            self.stats.below_quality_gate += 1
        else:
            self.stats.fingerprinted += 1

        crop_path: str | None = None
        if self._crop_dir is not None:
            self._crop_dir.mkdir(parents=True, exist_ok=True)
            name = f"sighting_{uuid.uuid4().hex[:12]}.jpg"
            cv2.imwrite(str(self._crop_dir / name), track.best_crop)
            crop_path = name

        # Displacement between the first and last box of the pass. Measured centre to
        # centre: a vehicle approaching the camera grows without moving, and comparing
        # corners would read that growth as travel.
        (fx, fy), (lx, ly) = centre(track.first_bbox), centre(track.bbox)
        displacement = float(((fx - lx) ** 2 + (fy - ly) ** 2) ** 0.5)

        return VehicleSightingRecord(
            camera_ref=source.camera_ref,
            # Namespaced by session: tracker ids restart after a scene cut, and a bare
            # integer would silently collide across the wrap of a looping recording.
            track_id=f"{track.session_id or 's'}:{track.track_id}"[:64],
            vehicle_class=vehicle_class,
            class_confidence=class_confidence,
            bbox=track.best_bbox,
            frame_width=track.best_frame_size[0],
            frame_height=track.best_frame_size[1],
            first_pts_ms=track.first_pts_ms,
            last_pts_ms=track.last_pts_ms,
            observed_at_utc=track.observed_at_utc,
            clock_confidence=source.clock_confidence,
            frames_seen=track.frames_seen,
            crop=track.best_crop,
            crop_path=crop_path,
            appearance=appearance,
            displacement_px=round(displacement, 2),
        )


__all__ = [
    "SightingPipeline",
    "SightingStats",
    "VehicleSightingRecord",
    "VehicleTrack",
    "VehicleTracker",
]
