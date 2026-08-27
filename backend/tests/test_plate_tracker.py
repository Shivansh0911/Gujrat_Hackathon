"""Track association across frames, and the limits that stop it over-merging.

Association is what makes multi-frame fusion possible. It was IoU-only with a 0.25
threshold, and on real footage that associated almost nothing: 22 plate detections
became 14 tracks, 13 of them one frame long. Sampling is 5 analytic fps, so
consecutive looks at a vehicle are 200 ms apart, by which time the plate has moved
further than its own width and the boxes do not overlap at all. Fusion was dead code
and every plate was decided by a single noisy read.
"""

from __future__ import annotations

from services.analytics.anpr import PlateTracker

SESSION = "s1"


def _box(x: int, y: int, w: int = 60, h: int = 20) -> tuple[int, int, int, int]:
    return (x, y, x + w, y + h)


def test_a_plate_moving_between_frames_stays_one_track() -> None:
    """The regression: no overlap, same vehicle, must still associate."""
    tracker = PlateTracker()
    pairs = []
    for i in range(4):
        # Travels one and a half box widths per sample -- no IoU whatsoever.
        boxes = [_box(100 + i * 90, 200)]
        pairs.append(tracker.update(boxes, pts_ms=i * 200.0, session_id=SESSION)[0][0])

    assert len({t.track_id for t in pairs}) == 1, "one vehicle became several tracks"


def test_overlapping_boxes_still_associate() -> None:
    tracker = PlateTracker()
    a = tracker.update([_box(100, 200)], pts_ms=0.0, session_id=SESSION)[0][0]
    b = tracker.update([_box(105, 202)], pts_ms=200.0, session_id=SESSION)[0][0]
    assert a.track_id == b.track_id


def test_a_distant_plate_is_a_different_vehicle() -> None:
    """Distance is bounded, or a track adopts whatever appears next."""
    tracker = PlateTracker()
    a = tracker.update([_box(100, 200)], pts_ms=0.0, session_id=SESSION)[0][0]
    b = tracker.update([_box(1400, 900)], pts_ms=200.0, session_id=SESSION)[0][0]
    assert a.track_id != b.track_id


def test_a_very_different_size_is_a_different_vehicle() -> None:
    """Scale rejects a different plate at a different depth in the same place."""
    tracker = PlateTracker()
    a = tracker.update([_box(100, 200, w=60, h=20)], pts_ms=0.0, session_id=SESSION)[0][0]
    b = tracker.update([_box(105, 205, w=400, h=130)], pts_ms=200.0, session_id=SESSION)[0][0]
    assert a.track_id != b.track_id


def test_a_long_gap_starts_a_new_track() -> None:
    """Otherwise a track adopts an unrelated vehicle arriving much later."""
    tracker = PlateTracker()
    a = tracker.update([_box(100, 200)], pts_ms=0.0, session_id=SESSION)[0][0]
    b = tracker.update([_box(130, 205)], pts_ms=60_000.0, session_id=SESSION)[0][0]
    assert a.track_id != b.track_id


def test_two_plates_do_not_swap_tracks() -> None:
    """Assignment is greedy over a global ranking, not per box in arrival order."""
    tracker = PlateTracker()
    first = tracker.update([_box(100, 200), _box(400, 200)], pts_ms=0.0, session_id=SESSION)
    left_id = first[0][0].track_id
    right_id = first[1][0].track_id
    assert left_id != right_id

    # Both advance slightly, and the boxes are presented in the opposite order.
    second = tracker.update([_box(410, 202), _box(110, 202)], pts_ms=200.0, session_id=SESSION)
    assert second[0][0].track_id == right_id
    assert second[1][0].track_id == left_id


def test_reset_drops_every_track() -> None:
    """A hard cut means the vehicle in frame is a different vehicle."""
    tracker = PlateTracker()
    a = tracker.update([_box(100, 200)], pts_ms=0.0, session_id=SESSION)[0][0]
    tracker.reset()
    b = tracker.update([_box(100, 200)], pts_ms=200.0, session_id="s2")[0][0]
    assert a.track_id != b.track_id
