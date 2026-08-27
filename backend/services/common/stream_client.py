"""Supervised video capture. The single ingest primitive for the whole platform.

Transport-agnostic by design: the session is handed a *URL provider*, not a URL, so
RTSP and HLS are the same code path and the HLS variant (whose per-client session UUID
expires within seconds) is re-resolved on every reconnect. See transport.py for why
that matters on this network.

Every rule in §2.2 that concerns reading frames is implemented here exactly once, so
that no pipeline can accidentally violate one:

  * RTSP over TCP is forced by importing cv2 through services.common.cv_env.
  * All timing derives from CAP_PROP_POS_MSEC (stream PTS). Arrival time is recorded
    only as `wall_recv_ts` for latency telemetry and is never used for motion,
    speed, dwell or correlation.
  * Declared FPS is never read for timing. `measured_fps` is computed from PTS deltas.
  * Inter-frame gaps are normal and never treated as a disconnect.
  * Decode failures during the join window are debug-level until the first decodable
    frame; only a join timeout escalates.
  * Reconnect uses exponential backoff with full jitter, 2s -> 30s.
  * Scene discontinuity (the recording loop point) raises an event and starts a new
    session id, so downstream trackers reset without losing written evidence.
"""

from __future__ import annotations

import itertools
import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np

from services.common.cv_env import RTSP_TRANSPORT, capture_options, cv2
from services.common.scene_cut import SceneCutDetector

log = logging.getLogger(__name__)

# A PTS that jumps backwards, or forwards by more than this, is a stream restart or
# loop point rather than a dropped-frame gap. 10s is comfortably longer than any
# plausible network stall on a supervised feed and far shorter than a 12h recording.
PTS_DISCONTINUITY_MS = 10_000.0


@dataclass(frozen=True)
class Frame:
    """One decoded frame. `pts_ms` is the only legitimate timing source."""

    image: np.ndarray
    pts_ms: float
    seq: int
    session_id: str
    wall_recv_ts: float  # telemetry only -- never for correlation. See §2.2.


@dataclass
class SessionStats:
    """Per-camera health signals. Surfaced on the Health screen (§9.5)."""

    external_id: str
    transport: str = "rtsp"
    frames: int = 0
    reconnects: int = 0
    resolve_failures: int = 0
    decode_failures: int = 0
    join_decode_warnings: int = 0
    discontinuities: int = 0
    measured_fps: float | None = None
    max_interframe_gap_ms: float = 0.0
    width: int | None = None
    height: int | None = None
    fourcc: str | None = None
    declared_fps: float | None = None  # recorded ONLY to display the discrepancy
    first_frame_latency_s: float | None = None
    last_pts_ms: float | None = None
    rtsp_transport: str = field(default=RTSP_TRANSPORT)


class StreamSession:
    """Reads frames from one camera, reconnecting until stopped.

    Not thread-safe; run one instance per camera. `frames()` is a generator so the
    caller controls pacing -- pulling slower than real time simply drops the oldest
    frames at the socket, which is the correct behaviour for analytics.
    """

    def __init__(
        self,
        url_provider: Callable[[], str] | str,
        external_id: str,
        *,
        transport: str = "rtsp",
        join_timeout_s: float = 20.0,
        backoff_min_s: float = 2.0,
        backoff_max_s: float = 30.0,
        fps_window: int = 60,
        on_discontinuity: Callable[[str, SessionStats], None] | None = None,
        detect_scene_cuts: bool = True,
    ) -> None:
        # A bare string is accepted for RTSP and tests; HLS callers must pass a
        # callable so each reconnect gets a fresh, unexpired variant URL.
        if callable(url_provider):
            self._url_provider: Callable[[], str] = url_provider
        else:
            # Bind the string to a local so the closure cannot observe a later
            # rebinding of the parameter. A default-argument lambda did the same job
            # but is untypeable: mypy sees the default as widening the signature.
            fixed_url = url_provider
            self._url_provider = lambda: fixed_url
        self._join_timeout_s = join_timeout_s
        self._backoff_min_s = backoff_min_s
        self._backoff_max_s = backoff_max_s
        self._on_discontinuity = on_discontinuity

        self.stats = SessionStats(external_id=external_id, transport=transport)
        self._cap: cv2.VideoCapture | None = None
        self._session_id = ""
        self._seq = itertools.count()
        self._pts_history: deque[float] = deque(maxlen=fps_window)
        self._scene = SceneCutDetector() if detect_scene_cuts else None
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        """Ask the frame generator to finish. Safe to call from another thread."""
        self._stop.set()

    def request_reconnect(self) -> None:
        """Drop the current capture and rejoin, without ending the session.

        Backs the Health screen's per-camera "reset" action, for the operational case
        where a feed is delivering frames but the content is stale or wrong and the
        fix is to rejoin rather than restart the worker.

        The flag is honoured by the reading thread at a frame boundary. Releasing the
        VideoCapture directly from another thread is undefined behaviour in OpenCV --
        it raised 'Unknown C++ exception from OpenCV code' from inside read() during
        preflight development -- so cross-thread control is a flag, never a release.
        """
        self._reconnect_requested.set()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "StreamSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
        self.close()

    # ------------------------------------------------------------------- internals

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Full jitter rather than fixed steps because ~50 workers reconnecting in
        lockstep after a gateway restart is a self-inflicted thundering herd against
        infrastructure we do not own.
        """
        ceiling = min(self._backoff_max_s, self._backoff_min_s * (2**attempt))
        return random.uniform(self._backoff_min_s, ceiling)

    def _open(self) -> bool:
        self.close()
        try:
            url = self._url_provider()
        except Exception as exc:  # noqa: BLE001 - resolution is remote and may fail
            # Counted and logged, never swallowed: an HLS master that stops resolving
            # is a camera outage and must reach the health dashboard as one.
            self.stats.resolve_failures += 1
            log.warning(
                "stream URL resolution failed camera=%s: %s",
                self.stats.external_id,
                exc,
            )
            return False

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return False
        self._cap = cap
        self._session_id = uuid.uuid4().hex
        self._pts_history.clear()
        if self._scene is not None:
            self._scene.reset()

        # Read declared properties once, for the sole purpose of showing the jury how
        # far they diverge from measured reality (§5, `declared_fps` vs `measured_fps`).
        # This is the ONLY CAP_PROP_FPS read in the codebase and it feeds no timing.
        declared = cap.get(cv2.CAP_PROP_FPS)  # reference-only; never used for timing
        self.stats.declared_fps = float(declared) if declared and declared > 0 else None
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.stats.fourcc = (
            "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip() if fourcc else None
        )
        return True

    def _record_pts(self, pts_ms: float) -> bool:
        """Track PTS; return True if this frame begins a new continuous segment."""
        prev = self.stats.last_pts_ms
        self.stats.last_pts_ms = pts_ms

        if prev is None:
            self._pts_history.append(pts_ms)
            return False

        delta = pts_ms - prev
        if delta < 0 or delta > PTS_DISCONTINUITY_MS:
            return True

        self.stats.max_interframe_gap_ms = max(self.stats.max_interframe_gap_ms, delta)
        self._pts_history.append(pts_ms)
        if len(self._pts_history) >= 2:
            span = self._pts_history[-1] - self._pts_history[0]
            # Measured from PTS span, not frame count / declared rate. On join the
            # gateway replays a buffered GOP, so the first frames arrive faster than
            # real time -- deriving fps from arrival would report an impossible rate.
            if span > 0:
                self.stats.measured_fps = (len(self._pts_history) - 1) / (span / 1000.0)
        return False

    def _raise_discontinuity(self, reason: str) -> None:
        self.stats.discontinuities += 1
        # New session id: downstream trackers key local track ids on it, so a hard cut
        # cannot silently merge two different vehicles into one track.
        self._session_id = uuid.uuid4().hex
        self._pts_history.clear()
        if self._scene is not None:
            self._scene.reset()
        log.info(
            "SCENE_DISCONTINUITY camera=%s reason=%s session=%s",
            self.stats.external_id,
            reason,
            self._session_id,
        )
        if self._on_discontinuity is not None:
            self._on_discontinuity(reason, self.stats)

    # ---------------------------------------------------------------------- public

    def frames(self) -> Iterator[Frame]:
        """Yield decoded frames forever, reconnecting as needed, until stop()."""
        attempt = 0
        log.info(
            "opening camera=%s transport=%s options=%s",
            self.stats.external_id,
            self.stats.transport,
            capture_options(),
        )

        while not self._stop.is_set():
            if not self._open():
                attempt += 1
                self.stats.reconnects += 1
                delay = self._backoff_delay(attempt)
                log.warning(
                    "connect failed camera=%s attempt=%d retry_in=%.1fs",
                    self.stats.external_id,
                    attempt,
                    delay,
                )
                if self._stop.wait(delay):
                    break
                continue

            got_first = False
            join_started = time.monotonic()

            while not self._stop.is_set():
                if self._reconnect_requested.is_set():
                    self._reconnect_requested.clear()
                    log.info(
                        "reconnect requested camera=%s frames=%d",
                        self.stats.external_id,
                        self.stats.frames,
                    )
                    break

                try:
                    ok, image = self._cap.read()  # type: ignore[union-attr]
                except cv2.error as exc:
                    # A malformed packet can surface as a C++ exception from the
                    # decoder rather than a False return. Observed on this gateway
                    # when an HLS segment 404s mid-session. It is a read failure like
                    # any other and must reconnect -- letting it propagate would kill
                    # the ingest worker for that camera permanently.
                    self.stats.decode_failures += 1
                    log.warning(
                        "decoder exception camera=%s frames=%d: %s",
                        self.stats.external_id,
                        self.stats.frames,
                        exc,
                    )
                    break

                if not ok or image is None:
                    if not got_first:
                        # Expected until the first IDR: "Error constructing the frame
                        # RPS", "Could not find ref with POC". Normal, self-correcting,
                        # and NOT a reason to abort the stream (§2.2).
                        self.stats.join_decode_warnings += 1
                        if time.monotonic() - join_started > self._join_timeout_s:
                            log.error(
                                "join timeout camera=%s after=%.1fs warnings=%d",
                                self.stats.external_id,
                                self._join_timeout_s,
                                self.stats.join_decode_warnings,
                            )
                            break
                        log.debug(
                            "decode warning during join camera=%s",
                            self.stats.external_id,
                        )
                        continue
                    self.stats.decode_failures += 1
                    log.warning(
                        "stream ended camera=%s frames=%d",
                        self.stats.external_id,
                        self.stats.frames,
                    )
                    break

                if not got_first:
                    got_first = True
                    attempt = 0  # a successful join resets the backoff envelope
                    self.stats.first_frame_latency_s = time.monotonic() - join_started
                    self.stats.height, self.stats.width = image.shape[:2]
                    log.info(
                        "camera=%s joined in %.2fs %dx%d declared_fps=%s",
                        self.stats.external_id,
                        self.stats.first_frame_latency_s,
                        self.stats.width,
                        self.stats.height,
                        self.stats.declared_fps,
                    )

                pts_ms = float(self._cap.get(cv2.CAP_PROP_POS_MSEC))  # type: ignore[union-attr]

                if self._record_pts(pts_ms):
                    self._raise_discontinuity("pts_jump")
                elif self._scene is not None and self._scene.update(image):
                    # A content cut without a PTS jump: the recording looped onto a
                    # different scene while timestamps kept advancing.
                    self._raise_discontinuity("scene_change")

                self.stats.frames += 1
                yield Frame(
                    image=image,
                    pts_ms=pts_ms,
                    seq=next(self._seq),
                    session_id=self._session_id,
                    wall_recv_ts=time.time(),
                )

            self.close()
            if self._stop.is_set():
                break

            attempt += 1
            self.stats.reconnects += 1
            delay = self._backoff_delay(attempt)
            log.info(
                "reconnecting camera=%s attempt=%d in=%.1fs",
                self.stats.external_id,
                attempt,
                delay,
            )
            if self._stop.wait(delay):
                break
