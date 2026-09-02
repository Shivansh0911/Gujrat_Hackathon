"""GatewaySource — the organiser's feed, behind the CameraSource protocol.

This is a thin adapter over the already-verified `stream_client` / `transport` /
`catalogue` modules, not a reimplementation. Everything §2.2 requires -- forced TCP
where reachable, HLS fallback, PTS-only timing, jittered backoff reconnect, non-fatal
join warnings, scene-discontinuity handling -- lives in `StreamSession` and stays
there. Duplicating any of it here would create a second place for the feed contract to
drift out of compliance.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

from services.common.catalogue import CameraDescriptor
from services.common.config import Settings, get_settings
from services.common.stream_client import Frame, StreamSession
from services.common.transport import select_transport
from services.ingest.source import CameraCapabilities, HealthReport

log = logging.getLogger(__name__)


class GatewaySource:
    """One camera on the hackathon gateway."""

    def __init__(
        self,
        descriptor: CameraDescriptor,
        settings: Settings | None = None,
        *,
        rtsp_available: bool = False,
        epoch: datetime | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._settings = settings or get_settings()
        self.camera_ref = descriptor.external_id

        source = select_transport(descriptor, self._settings, rtsp_available=rtsp_available)
        self.transport = source.transport
        self._session = StreamSession(
            source.url,  # callable for HLS: variant URLs expire and are re-resolved
            descriptor.external_id,
            transport=source.transport,
            join_timeout_s=self._settings.join_timeout_s,
            backoff_min_s=self._settings.backoff_min_s,
            backoff_max_s=self._settings.backoff_max_s,
        )

        # A live stream gives us PTS relative to when we joined, not wall clock. We
        # anchor the timeline at the moment of the first frame and carry an explicit
        # confidence, rather than pretending the mapping is exact.
        self._epoch = epoch
        self._first_pts_ms: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------- probe

    def probe(self) -> CameraCapabilities:
        """Connect briefly and measure. The catalogue's declarations are not trusted.

        DISCOVERY finding 2: 20 of 30 catalogued cameras report `codec: ""`, `0x0` and
        `fps: 0.0`. Everything below marked measured comes from decoded frames.
        """
        deadline = time.monotonic() + 8.0
        try:
            for _frame in self._session.frames():
                if time.monotonic() >= deadline:
                    break
        except Exception as exc:  # noqa: BLE001 - a probe characterises, never crashes
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("probe failed camera=%s: %s", self.camera_ref, self._last_error)
        finally:
            self._session.stop()
            self._session.close()

        st = self._session.stats
        return CameraCapabilities(
            codec=st.fourcc or self._descriptor.declared_codec,
            width=st.width or self._descriptor.declared_width,
            height=st.height or self._descriptor.declared_height,
            measured_fps=round(st.measured_fps, 3) if st.measured_fps else None,
            declared_fps=self._descriptor.declared_fps,
            transport=self.transport,
            # The gateway explicitly does not support seeking or byte-range fetching
            # (§2.1); a consumer that assumes otherwise silently gets a partial file.
            supports_seek=False,
            extra={"location_text": self._descriptor.location_text},
        )

    # ------------------------------------------------------------------ frames

    def open(self) -> Iterator[Frame]:
        for frame in self._session.frames():
            if self._first_pts_ms is None:
                self._first_pts_ms = frame.pts_ms
                if self._epoch is None:
                    self._epoch = datetime.now(timezone.utc)
            yield frame

    # ------------------------------------------------------------------- clock

    def observed_at(self, frame: Frame) -> datetime:
        """Wall clock for a frame, anchored at the first frame we received."""
        if self._epoch is None or self._first_pts_ms is None:
            # Called before any frame arrived; the only honest answer is "now".
            return datetime.now(timezone.utc)
        return self._epoch + timedelta(milliseconds=frame.pts_ms - self._first_pts_ms)

    @property
    def clock_confidence(self) -> float:
        """Lower than a file source, and lower again after a discontinuity.

        The anchor is the wall-clock instant of our first frame, which already
        includes join latency. Every scene discontinuity resets the stream's PTS, so
        the mapping is re-derived and accumulated error grows. Recording that as a
        number lets the journey view show which timestamps are firm and which are
        approximate, instead of presenting all of them as equally certain.
        """
        if self._epoch is None:
            return 0.0
        base = 0.85
        return round(max(0.4, base - 0.05 * self._session.stats.discontinuities), 3)

    def health(self) -> HealthReport:
        st = self._session.stats
        return HealthReport(
            camera_ref=self.camera_ref,
            reachable=st.frames > 0,
            transport=self.transport,
            measured_fps=round(st.measured_fps, 3) if st.measured_fps else None,
            declared_fps=self._descriptor.declared_fps,
            frames=st.frames,
            reconnects=st.reconnects,
            decode_failures=st.decode_failures,
            discontinuities=st.discontinuities,
            time_to_first_frame_s=st.first_frame_latency_s,
            last_error=self._last_error,
            codec=st.fourcc or self._descriptor.declared_codec,
            width=st.width or self._descriptor.declared_width,
            height=st.height or self._descriptor.declared_height,
        )

    def stop(self) -> None:
        """Ask the reader to finish, without touching the capture handle.

        Deliberately separate from `close()`. `close()` releases the underlying
        `VideoCapture`, which is only safe on the thread that reads it -- doing it
        from elsewhere previously raised `Unknown C++ exception from OpenCV code`
        out of `read()` and killed the process. `stop()` sets an Event and nothing
        more, so a supervisor, a timeout or a signal handler can end an ingest from
        any thread. The reading loop then unwinds and `close()` runs where it must.
        """
        self._session.stop()

    def close(self) -> None:
        self._session.stop()
        self._session.close()
