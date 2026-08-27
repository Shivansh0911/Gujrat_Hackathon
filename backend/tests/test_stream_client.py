"""Unit tests for the parts of StreamSession that must not depend on a live feed."""

import numpy as np
import pytest

from services.common.stream_client import (
    PTS_DISCONTINUITY_MS,
    Frame,
    StreamSession,
)


def _session(**kw) -> StreamSession:
    return StreamSession("rtsp://unused.test/stream/1", "1", detect_scene_cuts=False, **kw)


def test_backoff_stays_within_envelope_and_is_jittered():
    s = _session(backoff_min_s=2.0, backoff_max_s=30.0)
    samples = [s._backoff_delay(a) for a in range(1, 12) for _ in range(20)]
    assert all(2.0 <= d <= 30.0 for d in samples)
    # Full jitter, not fixed steps: ~50 workers reconnecting in lockstep after a
    # gateway restart is a self-inflicted thundering herd.
    assert len(set(round(d, 4) for d in samples)) > 50


def test_backoff_ceiling_grows_then_caps():
    s = _session(backoff_min_s=2.0, backoff_max_s=30.0)
    assert max(s._backoff_delay(1) for _ in range(200)) <= 4.0
    assert max(s._backoff_delay(20) for _ in range(200)) <= 30.0


def test_measured_fps_comes_from_pts_deltas_not_frame_count():
    s = _session()
    # 12.5 fps stream => 80ms PTS steps.
    for i in range(40):
        assert s._record_pts(i * 80.0) is False
    assert s.stats.measured_fps == 12.5
    assert s.stats.declared_fps is None  # never populated without a real capture


def test_ordinary_gap_is_not_a_discontinuity_but_is_recorded():
    s = _session()
    s._record_pts(0.0)
    # A 2s stall is a network hiccup, not a loop point: keep reading.
    assert s._record_pts(2000.0) is False
    assert s.stats.max_interframe_gap_ms == 2000.0


def test_pts_going_backwards_is_a_discontinuity():
    s = _session()
    s._record_pts(500_000.0)
    assert s._record_pts(0.0) is True  # recording wrapped to the start


def test_large_forward_pts_jump_is_a_discontinuity():
    s = _session()
    s._record_pts(1000.0)
    assert s._record_pts(1000.0 + PTS_DISCONTINUITY_MS + 1) is True


def test_discontinuity_issues_a_new_session_id_and_preserves_counters():
    s = _session()
    s._session_id = "old"
    s.stats.frames = 123
    s._raise_discontinuity("pts_jump")
    # New id so downstream trackers cannot merge two vehicles across a hard cut...
    assert s._session_id != "old"
    # ...while evidence already written stays counted.
    assert s.stats.frames == 123
    assert s.stats.discontinuities == 1


def test_discontinuity_callback_receives_reason_and_stats():
    seen = []
    s = _session()
    s._on_discontinuity = lambda reason, stats: seen.append((reason, stats.external_id))
    s._raise_discontinuity("scene_change")
    assert seen == [("scene_change", "1")]


def test_frame_carries_pts_and_session_scope():
    f = Frame(
        image=np.zeros((2, 2, 3), np.uint8), pts_ms=80.0, seq=1, session_id="abc", wall_recv_ts=1.0
    )
    # wall_recv_ts exists for latency telemetry only; nothing may correlate on it.
    assert f.pts_ms == 80.0 and f.session_id == "abc"


# --- transport-agnostic URL provision -----------------------------------------


def test_url_provider_is_called_per_open_not_cached():
    calls = []

    def provider() -> str:
        calls.append(1)
        return f"https://host/live/stream/1/v.m3u8?session={len(calls)}"

    s = StreamSession(provider, "1", transport="hls", detect_scene_cuts=False)
    # HLS variant URLs carry a session UUID that expires within seconds; a cached URL
    # would reconnect onto a dead session and present as a flapping camera.
    s._open()
    s._open()
    assert len(calls) == 2


def test_resolution_failure_is_counted_and_does_not_raise():
    def provider() -> str:
        raise RuntimeError("master playlist 502")

    s = StreamSession(provider, "1", transport="hls", detect_scene_cuts=False)
    assert s._open() is False
    assert s.stats.resolve_failures == 1  # surfaces on the health dashboard


def test_bare_string_url_is_accepted_for_rtsp():
    s = StreamSession("rtsp://host:8554/stream/9", "9", detect_scene_cuts=False)
    assert s._url_provider() == "rtsp://host:8554/stream/9"


def test_stop_is_idempotent_and_close_survives_no_capture():
    s = _session()
    s.stop()
    s.stop()
    s.close()  # must not raise when nothing was ever opened
    with pytest.raises(StopIteration):
        next(iter(s.frames()))
