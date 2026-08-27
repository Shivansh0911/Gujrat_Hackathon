"""FileSource — local footage replayed as if it were a live camera.

Built first, and the whole pipeline validated against it, for three reasons:

1. The submission requires an own-feed demonstration as a **separate deliverable**
   from the government feed.
2. The gateway's media plane returned 502 for every camera for over two and a half
   hours on 2026-08-25 (DISCOVERY finding 9). A pipeline that can only be exercised
   against third-party infrastructure cannot be developed on a schedule.
3. It is the only source where ANPR recall can be measured against known ground
   truth, because we can watch the footage and count what is actually there.

The replay deliberately reproduces the gateway's awkward properties rather than
offering a clean feed: real PTS from the container, real-time pacing, and a hard
`SCENE_DISCONTINUITY` at the loop point with PTS resetting to zero. Code that works
here works there.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from services.common.cv_env import cv2
from services.common.scene_cut import SceneCutDetector
from services.common.stream_client import Frame
from services.ingest.source import CameraCapabilities, HealthReport

log = logging.getLogger(__name__)


class FileSource:
    """Replays a local video file at real-time pace, looping indefinitely."""

    transport = "file"

    def __init__(
        self,
        path: str | Path,
        camera_ref: str | None = None,
        *,
        realtime: bool = True,
        loop: bool = True,
        epoch: datetime | None = None,
        detect_scene_cuts: bool = True,
    ) -> None:
        self._path = Path(path)
        if not self._path.is_file():
            raise FileNotFoundError(f"no such video file: {self._path}")

        self.camera_ref = camera_ref or self._path.stem
        # realtime=False lets tests and the ANPR recall measurement run the whole file
        # as fast as it decodes. It is never used for a live demonstration.
        self._realtime = realtime
        self._loop = loop
        # The wall-clock instant the replay's timeline starts at. Defaults to now, so
        # a demo produces detections timestamped in the present.
        self._epoch = epoch or datetime.now(timezone.utc)

        self._cap: cv2.VideoCapture | None = None
        self._session_id = uuid.uuid4().hex
        self._seq = itertools.count()
        self._scene = SceneCutDetector() if detect_scene_cuts else None
        self._stop = threading.Event()

        # Accumulated duration of completed loops. Added to the raw PTS so that
        # `observed_at` keeps advancing across the wrap even though PTS resets --
        # otherwise every loop would overwrite the previous loop's detections in time.
        self._loop_offset_ms = 0.0
        self._last_pts_ms = 0.0

        self._frames = 0
        self._discontinuities = 0
        self._decode_failures = 0
        self._measured_fps: float | None = None
        self._ttff_s: float | None = None
        self._last_error: str | None = None

    # ---------------------------------------------------------------- lifecycle

    def _open_capture(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(str(self._path), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError(f"cannot decode video file: {self._path}")
        return cap

    def close(self) -> None:
        self._stop.set()
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> "FileSource":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- probe

    def probe(self) -> CameraCapabilities:
        """Measure real properties by decoding, not by reading container metadata."""
        cap = self._open_capture()
        try:
            declared = cap.get(cv2.CAP_PROP_FPS)  # reference-only; never used for timing
            declared_fps = float(declared) if declared and declared > 0 else None

            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            codec = (
                "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip()
                if fourcc
                else None
            )
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)

            # Measure the rate the same way the live path does: from PTS deltas over a
            # sample of real frames. Container metadata is a declaration like any other.
            width = height = None
            pts: list[float] = []
            for _ in range(60):
                ok, image = cap.read()
                if not ok or image is None:
                    break
                if width is None:
                    height, width = image.shape[:2]
                pts.append(float(cap.get(cv2.CAP_PROP_POS_MSEC)))

            measured_fps = None
            if len(pts) >= 2:
                span = pts[-1] - pts[0]
                if span > 0:
                    measured_fps = (len(pts) - 1) / (span / 1000.0)

            duration_s = None
            if frame_count and frame_count > 0 and measured_fps:
                duration_s = float(frame_count) / measured_fps

            return CameraCapabilities(
                codec=codec,
                width=width,
                height=height,
                measured_fps=round(measured_fps, 3) if measured_fps else None,
                declared_fps=declared_fps,
                transport=self.transport,
                duration_s=duration_s,
                # A file can seek; a live gateway stream explicitly cannot (§2.1).
                # Consumers must not assume this is available on every source.
                supports_seek=True,
                extra={"path": str(self._path), "frame_count": int(frame_count or 0)},
            )
        finally:
            cap.release()

    # ------------------------------------------------------------------ frames

    def open(self) -> Iterator[Frame]:
        """Yield frames at real-time pace, looping with a discontinuity at the wrap."""
        self._cap = self._open_capture()
        started = time.monotonic()
        pts_window: list[float] = []

        while not self._stop.is_set():
            ok, image = self._cap.read()

            if not ok or image is None:
                if not self._loop:
                    log.info("file exhausted camera=%s frames=%d", self.camera_ref, self._frames)
                    break
                # Loop point. This is the gateway's hard cut, reproduced: PTS resets to
                # zero, the scene changes abruptly, and long-lived state must recover.
                self._loop_offset_ms += self._last_pts_ms
                self._raise_discontinuity("loop_wrap")
                self._cap.release()
                self._cap = self._open_capture()
                started = time.monotonic()
                pts_window.clear()
                continue

            pts_ms = float(self._cap.get(cv2.CAP_PROP_POS_MSEC))
            self._last_pts_ms = pts_ms

            if self._frames == 0:
                self._ttff_s = time.monotonic() - started

            # Real-time pacing from PTS, not from a frame counter times a nominal rate.
            # An uneven cadence in the source is preserved rather than smoothed away,
            # which is the point: the live feed's cadence is uneven too.
            if self._realtime:
                target = started + (pts_ms / 1000.0)
                delay = target - time.monotonic()
                if delay > 0:
                    if self._stop.wait(delay):
                        break

            if self._scene is not None and self._scene.update(image):
                self._raise_discontinuity("scene_change")

            pts_window.append(pts_ms)
            if len(pts_window) > 60:
                pts_window.pop(0)
            if len(pts_window) >= 2:
                span = pts_window[-1] - pts_window[0]
                if span > 0:
                    self._measured_fps = (len(pts_window) - 1) / (span / 1000.0)

            self._frames += 1
            yield Frame(
                image=image,
                pts_ms=pts_ms,
                seq=next(self._seq),
                session_id=self._session_id,
                wall_recv_ts=time.time(),  # telemetry only
            )

    def _raise_discontinuity(self, reason: str) -> None:
        self._discontinuities += 1
        # New session id, exactly as the live path does: downstream trackers key local
        # track ids on it, so a hard cut cannot merge two vehicles into one track.
        self._session_id = uuid.uuid4().hex
        if self._scene is not None:
            self._scene.reset()
        log.info(
            "SCENE_DISCONTINUITY camera=%s reason=%s session=%s",
            self.camera_ref,
            reason,
            self._session_id,
        )

    # ------------------------------------------------------------------- clock

    def observed_at(self, frame: Frame) -> datetime:
        """Wall-clock instant for a frame, folding in completed loops."""
        return self._epoch + timedelta(milliseconds=self._loop_offset_ms + frame.pts_ms)

    @property
    def clock_confidence(self) -> float:
        # We define this timeline ourselves from an exact container PTS, so the
        # mapping is not an estimate. Live sources report lower.
        return 1.0

    def health(self) -> HealthReport:
        return HealthReport(
            camera_ref=self.camera_ref,
            reachable=self._cap is not None or self._path.is_file(),
            transport=self.transport,
            measured_fps=round(self._measured_fps, 3) if self._measured_fps else None,
            frames=self._frames,
            decode_failures=self._decode_failures,
            discontinuities=self._discontinuities,
            time_to_first_frame_s=self._ttff_s,
            last_error=self._last_error,
        )
