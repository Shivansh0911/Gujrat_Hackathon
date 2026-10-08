"""Sightings must be one per pass, must survive a scene cut, and must never be invented.

The property under test throughout is **a pass is one sighting**. A vehicle crossing a
camera at 5 analytic fps appears in ten to thirty frames; writing one row per frame would
make "how many vehicles passed" a question about frame rate and would bury the one good
crop among dozens of poor ones. `vehicle_counting.py` carries the same warning about
plate reads -- "one car in view for two seconds is not two cars".

A fake detector is used rather than the 20 MB ONNX, so these run without network and
without inference: the tracker and the pass-assembly logic are what is being tested, and
they are the parts that decide what reaches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import numpy as np
from numpy.typing import NDArray

from services.analytics.sighting import (
    SightingPipeline,
    VehicleSightingRecord,
    VehicleTracker,
)
from services.analytics.vehicle_detect import VehicleBox
from services.registry.enums import VehicleClass

EPOCH = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Frame:
    """Matches the fields `SightingPipeline` reads off a real `Frame`."""

    image: NDArray[Any]
    pts_ms: float
    session_id: str


class _ScriptedSource:
    """A CameraSource whose frames and detections are both scripted.

    The detector is driven off the frame index rather than the pixels, so a test can
    describe a vehicle's path directly instead of rendering one and hoping the detector
    agrees about it.
    """

    camera_ref = "TEST-01"
    clock_confidence = 1.0

    def __init__(self, frames: list[_Frame]) -> None:
        self._frames = frames

    def open(self) -> Iterator[_Frame]:
        yield from self._frames

    def observed_at(self, frame: Any) -> datetime:
        return EPOCH

    def probe(self) -> Any:  # pragma: no cover -- not exercised by the pipeline
        raise NotImplementedError

    def health(self) -> Any:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        return None


class _ScriptedDetector:
    """Returns a prepared list of boxes per call, in order."""

    def __init__(self, script: list[list[VehicleBox]]) -> None:
        self._script = script
        self._calls = 0

    def detect(self, image: NDArray[Any]) -> list[VehicleBox]:
        out = self._script[self._calls] if self._calls < len(self._script) else []
        self._calls += 1
        return out


def _textured(width: int = 420, height: int = 200, seed: int = 5) -> NDArray[np.uint8]:
    """A frame with enough texture that a crop of it passes the blur gate."""
    rng = np.random.default_rng(seed)
    base = np.full((height, width, 3), 150, dtype=np.uint8)
    noise = rng.integers(-30, 31, size=base.shape, dtype=np.int16)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _box(
    x: int,
    *,
    y: int = 10,
    w: int = 60,
    h: int = 50,
    cls: VehicleClass = VehicleClass.CAR,
    conf: float = 0.9,
) -> VehicleBox:
    return VehicleBox(bbox=(x, y, x + w, y + h), vehicle_class=cls, confidence=conf, coco_class=2)


def _run(
    script: list[list[VehicleBox]], *, session: str = "s1", **kwargs: Any
) -> list[VehicleSightingRecord]:
    frames = [
        _Frame(image=_textured(), pts_ms=float(i * 200), session_id=session)
        for i in range(len(script))
    ]
    pipeline = SightingPipeline(_ScriptedDetector(script), analytic_fps=5.0, **kwargs)
    return list(pipeline.run(_ScriptedSource(frames)))


# ------------------------------------------------------------- one pass, one sighting


def test_a_vehicle_crossing_the_frame_is_one_sighting_not_one_per_frame() -> None:
    """Eight frames of one car moving left to right must produce exactly one row."""
    script = [[_box(10 + i * 8)] for i in range(8)]
    records = _run(script)
    assert len(records) == 1
    assert records[0].frames_seen == 8


def test_two_vehicles_in_frame_produce_two_sightings() -> None:
    script = [[_box(10 + i * 6), _box(200 + i * 6, y=70)] for i in range(6)]
    records = _run(script)
    assert len(records) == 2


def test_a_single_frame_blink_is_not_recorded() -> None:
    """One frame is as likely to be a detector blink as a vehicle.

    Recording it would put a row in the database for something that may not have been
    there, which is the one thing this platform must not do.
    """
    script: list[list[VehicleBox]] = [[_box(10)]] + [[] for _ in range(8)]
    assert _run(script) == []


def test_the_minimum_track_length_is_configurable() -> None:
    script: list[list[VehicleBox]] = [[_box(10)]] + [[] for _ in range(8)]
    assert len(_run(script, min_track_frames=1)) == 1


# ------------------------------------------------------------------- scene cuts


def test_a_scene_cut_does_not_fuse_two_vehicles_into_one_pass() -> None:
    """The recording loops; at the wrap the vehicle in frame is a different vehicle.

    Carrying a track across the cut would emit one sighting spanning both, which is a
    journey hop that never happened.
    """
    frames = [
        _Frame(image=_textured(), pts_ms=float(i * 200), session_id="before") for i in range(4)
    ] + [_Frame(image=_textured(), pts_ms=float(i * 200), session_id="after") for i in range(4)]
    # The same box position on both sides of the cut: without session handling these
    # would associate perfectly into a single track.
    script = [[_box(50)] for _ in range(8)]
    pipeline = SightingPipeline(_ScriptedDetector(script), analytic_fps=5.0)
    records = list(pipeline.run(_ScriptedSource(frames)))

    assert len(records) == 2, "a cut must close the open pass and start a new one"
    assert pipeline.stats.discontinuities == 1
    assert records[0].track_id.startswith("before:")
    assert records[1].track_id.startswith("after:")


def test_track_ids_are_namespaced_by_session() -> None:
    """A bare integer would collide across the wrap of a looping recording."""
    records = _run([[_box(10 + i * 5)] for i in range(4)], session="abc123")
    assert records[0].track_id.startswith("abc123:")


# --------------------------------------------------------------- class and framing


def test_class_is_the_confidence_weighted_majority_of_the_pass() -> None:
    """A single frame's opinion is noisy at CCTV angles; the pass as a whole is not."""
    script = [
        [_box(10, cls=VehicleClass.CAR, conf=0.9)],
        [_box(18, cls=VehicleClass.CAR, conf=0.9)],
        [_box(26, cls=VehicleClass.TRUCK, conf=0.4)],
        [_box(34, cls=VehicleClass.CAR, conf=0.9)],
    ]
    records = _run(script)
    assert len(records) == 1
    assert records[0].vehicle_class is VehicleClass.CAR
    assert 0.0 < records[0].class_confidence <= 1.0


def test_framing_scalars_are_relative_to_the_frame() -> None:
    """They feed the scorer, so they must be comparable across cameras of any size."""
    records = _run([[_box(10 + i * 5)] for i in range(4)])
    r = records[0]
    assert 0.0 <= r.rel_cx <= 1.0
    assert 0.0 <= r.rel_cy <= 1.0
    assert 0.0 < r.rel_area <= 1.0


def test_the_best_crop_of_the_pass_is_the_one_fingerprinted() -> None:
    """Largest and sharpest wins: that is the frame whose fingerprint is worth storing.

    The sizes step gently on purpose. A vehicle whose box quadruples between two frames
    200 ms apart is not a vehicle approaching, it is a different detection, and the
    tracker's scale gate correctly refuses to associate it -- so a test written with a
    4.5x jump measures the gate rather than the crop choice.
    """
    script = [
        [_box(10, w=40, h=32)],
        [_box(55, w=64, h=50)],  # nearer: the best view of the pass
        [_box(105, w=48, h=38)],
    ]
    records = _run(script)
    assert len(records) == 1
    # The stored box is the best one, not the first or the last.
    assert records[0].bbox[2] - records[0].bbox[0] == 64


# ------------------------------------------------------------------------ tracker


def test_a_track_that_misses_too_many_frames_is_closed() -> None:
    """This, not the gap bound, is what stops a later vehicle inheriting an old track.

    The gap bound guards the *motion-tolerant* branch only. A box that still overlaps
    strongly is treated as the same vehicle however much time has passed, and that is
    deliberate: a parked or queuing vehicle is exactly a high-overlap box across a long
    gap, and §7.2's dwell detection depends on it staying one sighting rather than
    fragmenting into one per traffic-light cycle.

    What bounds an abandoned track is `max_misses`: once the vehicle stops being
    detected, the track closes and a later arrival opens a new one.
    """
    tracker = VehicleTracker(max_misses=2)
    tracker.update([_box(50)], 0.0, "s", EPOCH)
    for i in range(3):
        tracker.update([], float(200 * (i + 1)), "s", EPOCH)

    closed = tracker.closed()
    assert len(closed) == 1 and closed[0].track_id == 1
    assert tracker.open_tracks == 0

    pairs = tracker.update([_box(50)], 2000.0, "s", EPOCH)
    assert pairs[0][0].track_id == 2, "a new arrival must not reuse the closed track"


def test_a_stationary_vehicle_stays_one_track_across_a_long_gap() -> None:
    """The other half of the rule above, asserted directly so it is not lost later."""
    tracker = VehicleTracker(max_gap_ms=800.0)
    tracker.update([_box(50)], 0.0, "s", EPOCH)
    pairs = tracker.update([_box(50)], 5000.0, "s", EPOCH)
    assert pairs[0][0].track_id == 1
    assert pairs[0][0].frames_seen == 2


def test_the_tracker_associates_a_moving_box_that_no_longer_overlaps() -> None:
    """IoU alone associates almost nothing at 5 fps -- see PlateTracker's measurement."""
    tracker = VehicleTracker()
    tracker.update([_box(10, w=60, h=50)], 0.0, "s", EPOCH)
    # Moved most of its own width, so IoU is 0, but it is plainly the same vehicle.
    pairs = tracker.update([_box(75, w=60, h=50)], 200.0, "s", EPOCH)
    assert pairs[0][0].track_id == 1
    assert pairs[0][0].frames_seen == 2


def test_the_tracker_refuses_an_implausible_size_jump() -> None:
    """A different vehicle at a different depth must not inherit the track."""
    tracker = VehicleTracker(max_scale_ratio=1.8)
    tracker.update([_box(10, w=60, h=50)], 0.0, "s", EPOCH)
    pairs = tracker.update([_box(12, w=200, h=180)], 200.0, "s", EPOCH)
    assert pairs[0][0].track_id == 2


def test_flush_closes_every_open_track() -> None:
    tracker = VehicleTracker()
    tracker.update([_box(10), _box(200, y=70)], 0.0, "s", EPOCH)
    assert len(tracker.flush()) == 2
    assert tracker.open_tracks == 0


# -------------------------------------------------------------------------- stats


def test_stats_separate_detections_from_recorded_passes() -> None:
    """The ratio is the honest headline: 74 detections became 11 sightings on real
    footage, and a reader needs both numbers to see that."""
    script = [[_box(10 + i * 7)] for i in range(6)]
    pipeline = SightingPipeline(_ScriptedDetector(script), analytic_fps=5.0)
    list(
        pipeline.run(
            _ScriptedSource(
                [_Frame(image=_textured(), pts_ms=float(i * 200), session_id="s") for i in range(6)]
            )
        )
    )
    assert pipeline.stats.vehicles_detected == 6
    assert pipeline.stats.records_emitted == 1
    assert pipeline.stats.frames_analysed == 6
