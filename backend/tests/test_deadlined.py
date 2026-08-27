"""The ingest deadline, including the failure mode that motivated it.

`StreamSession` reconnects forever, so a batch caller needs a bound that does not
depend on frames arriving. The regression these tests protect is precise: a camera
that never yields a frame must still be abandoned, because the first implementation
checked the clock only after a successful yield and therefore hung indefinitely on
exactly the cameras that were broken.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Iterator

import pytest

from services.common.stream_client import Frame
from services.ingest.deadlined import Deadlined
from services.ingest.source import CameraCapabilities, HealthReport


class FakeSource:
    """A CameraSource whose behaviour each test dictates."""

    def __init__(self, *, yields: bool) -> None:
        self.camera_ref = "TEST-1"
        self.transport = "hls"
        self._yields = yields
        self._stop = threading.Event()
        self.closed = False
        self.stop_calls = 0

    def stop(self) -> None:
        """The lever the deadline pulls. Sets an Event; touches nothing else."""
        self.stop_calls += 1
        self._stop.set()

    def open(self) -> Iterator[Frame]:
        pts = 0.0
        while not self._stop.is_set():
            if self._yields:
                pts += 40.0
                yield Frame(
                    image=None,  # type: ignore[arg-type]
                    pts_ms=pts,
                    session_id="s1",
                    seq=int(pts // 40),
                    wall_recv_ts=time.monotonic(),
                )
            # A camera that yields nothing still burns wall clock, exactly as the
            # real one does while it sits in reconnect backoff.
            self._stop.wait(0.02)

    def probe(self) -> CameraCapabilities:  # pragma: no cover - not exercised
        raise NotImplementedError

    def health(self) -> HealthReport:  # pragma: no cover - not exercised
        raise NotImplementedError

    def observed_at(self, frame: Frame) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def clock_confidence(self) -> float:
        return 0.5

    def close(self) -> None:
        self.closed = True


def _drain(source: Deadlined, limit: float = 5.0) -> int:
    started = time.monotonic()
    count = 0
    for _ in source.open():
        count += 1
        if time.monotonic() - started > limit:  # pragma: no cover - safety net
            pytest.fail("Deadlined did not terminate within the safety limit")
    return count


def test_silent_camera_is_abandoned_at_the_deadline() -> None:
    """The regression: a source that never yields must still be bounded.

    This is the case the first implementation got wrong. It checked the clock after
    each yielded frame, so a camera stuck in reconnect backoff was never released.
    """
    inner = FakeSource(yields=False)
    source = Deadlined(inner, seconds=0.2)

    started = time.monotonic()
    assert _drain(source) == 0
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"deadline did not fire; ran for {elapsed:.2f}s"
    assert source.timed_out is True
    assert inner.stop_calls == 1


def test_frames_flow_until_the_deadline() -> None:
    inner = FakeSource(yields=True)
    source = Deadlined(inner, seconds=0.3)

    count = _drain(source)

    assert count > 0
    assert source.frames_seen == count
    assert source.timed_out is True


def test_deadline_does_not_close_the_capture() -> None:
    """The timer must set the stop Event and nothing else.

    Releasing a VideoCapture from another thread is undefined behaviour in OpenCV and
    previously crashed the reader with `Unknown C++ exception`. `close()` belongs to
    the owning thread, so expiry must not trigger it.
    """
    inner = FakeSource(yields=False)
    source = Deadlined(inner, seconds=0.1)

    _drain(source)

    assert inner.stop_calls == 1
    assert inner.closed is False, "expiry must not close the source"

    source.close()
    assert inner.closed is True


def test_clock_confidence_is_a_property_not_a_method() -> None:
    """Wrapping it as a method made the pipeline store a bound method as a float."""
    source = Deadlined(FakeSource(yields=True), seconds=1.0)

    assert isinstance(source.clock_confidence, float)
    assert source.clock_confidence == pytest.approx(0.5)


def test_non_positive_deadline_is_rejected() -> None:
    with pytest.raises(ValueError):
        Deadlined(FakeSource(yields=True), seconds=0)
