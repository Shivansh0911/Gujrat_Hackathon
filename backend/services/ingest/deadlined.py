"""A CameraSource decorator that bounds how long an ingest may run.

`StreamSession` reconnects forever by design, and that is the right behaviour for a
production ingest worker: a camera that comes back should be picked up without anyone
restarting anything. It is the wrong behaviour for any caller that has to finish --
a batch sweep across the estate, a preflight probe, a request handler.

Putting the bound here rather than in `StreamSession` keeps that policy where the
caller can choose it, and keeps the pipeline identical on live and recorded input,
which is what lets a government-feed run be evidence about the same system the
own-feed run describes.

The deadline is enforced from a timer thread rather than from the consuming loop. An
earlier version checked the clock after each yielded frame, which bounded a healthy
camera and did nothing at all for a sick one: a camera that never connects never
yields, so the check never ran. One camera sat in exponential backoff against an
HTTP 500 for eleven minutes before this was found.

`stop()` sets a `threading.Event` that the session tests both in its retry loop and
inside the backoff `wait()`, so a stuck camera is abandoned promptly rather than after
its current sleep expires. Setting an Event from another thread is safe. Releasing a
`VideoCapture` from another thread is not -- that is undefined behaviour in OpenCV and
previously surfaced as `Unknown C++ exception from OpenCV code` raised out of `read()`
in the reading thread. The timer therefore touches the Event and nothing else; the
capture is released by the thread that owns it, when the loop unwinds.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Iterator

from services.common.stream_client import Frame
from services.ingest.source import CameraCapabilities, CameraSource, HealthReport


class Deadlined:
    """Wrap a CameraSource so that `open()` finishes within `seconds`."""

    def __init__(self, inner: CameraSource, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("deadline must be positive")
        self._inner = inner
        self._seconds = seconds
        self.camera_ref = inner.camera_ref
        self.transport = getattr(inner, "transport", None)
        self.frames_seen = 0
        self.timed_out = False

    # ------------------------------------------------------------------ deadline

    def _expire(self) -> None:
        self.timed_out = True
        stop = getattr(self._inner, "stop", None)
        if callable(stop):
            stop()

    def open(self) -> Iterator[Frame]:
        timer = threading.Timer(self._seconds, self._expire)
        timer.daemon = True
        timer.start()
        deadline = time.monotonic() + self._seconds
        try:
            for frame in self._inner.open():
                self.frames_seen += 1
                yield frame
                # A belt-and-braces check for sources that do not implement `stop()`:
                # the timer cannot end those, but the consuming loop can.
                if time.monotonic() >= deadline:
                    self.timed_out = True
                    break
        finally:
            timer.cancel()

    # ------------------------------------------------------- delegated protocol

    def probe(self) -> CameraCapabilities:
        return self._inner.probe()

    def health(self) -> HealthReport:
        return self._inner.health()

    def observed_at(self, frame: Frame) -> datetime:
        return self._inner.observed_at(frame)

    @property
    def clock_confidence(self) -> float:
        # A property on the protocol, not a method. An earlier wrapper exposed it as
        # a method, so the pipeline stored a bound method where a float belonged and
        # every camera failed with "'float' object is not callable".
        return self._inner.clock_confidence

    def close(self) -> None:
        self._inner.close()
