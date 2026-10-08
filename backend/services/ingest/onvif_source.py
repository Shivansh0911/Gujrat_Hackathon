"""A `CameraSource` for any ONVIF device: the third live adapter.

ONVIF has been designed-but-not-built in this project since the beginning, and it is the
single most credible gap in the federation story. With it the claim becomes three live
adapters plus a vendor-SDK path, rather than two live adapters and a promise.

What this adds over `GatewaySource`
------------------------------------
Almost nothing in the frame path, deliberately. `open()`, reconnection, backoff,
transport forcing, PTS handling and scene-cut detection all come from the existing
`StreamSession`, which is contract-tested. An adapter that reimplemented any of those
would be a second place for the same bugs to live.

What it does add is at the edges:

**A measured clock instead of an estimated one.** `GatewaySource` anchors its timeline at
the wall-clock instant of its first frame, which already contains join latency, and
carries a confidence of 0.85 to say so. An ONVIF device reports its own clock through
`GetSystemDateAndTime`, so the offset between device time and platform time is a
*measurement*. `clock_confidence` therefore starts **higher** for ONVIF, at 0.95, and
falls if the device's clock is badly skewed. Adding a protocol usually dilutes a
guarantee; this one strengthens it, and the number says so rather than a paragraph.

**Real declared properties to disagree with.** The hackathon gateway publishes `codec:
""`, `0x0` and `fps: 0.0` for 20 of 30 cameras (DISCOVERY finding 2), so there is often
nothing to compare a measurement against. An ONVIF device states its encoder
configuration, which means the declared-versus-measured divergence the integration guide
warns about becomes visible for the first time: a camera claiming 25 fps and delivering
15 is a finding, and this is the adapter that can produce it.

**Per-device credentials.** The gateway has one shared access code. ONVIF authenticates
per device, which is what `services.common.credentials` exists for. The plaintext lives
only inside this process, is never returned by any endpoint, and never appears in a log.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

from services.common.credentials import CameraCredential
from services.common.stream_client import Frame, StreamSession
from services.ingest.onvif_client import OnvifClient, OnvifError, OnvifProfile
from services.ingest.source import CameraCapabilities, HealthReport

log = logging.getLogger(__name__)

#: How far a device's clock may be from ours before timestamps stop being better than a
#: gateway's estimate. Two seconds: beyond that, the device is not NTP-synchronised and
#: its clock is a worse reference than our own join-time anchor.
MAX_TRUSTED_SKEW_S: float = 2.0

#: Clock confidence for a device whose clock we have read and which is closely
#: synchronised. Higher than GatewaySource's 0.85 because the mapping is measured.
ONVIF_CLOCK_CONFIDENCE: float = 0.95


def _best_profile(profiles: list[OnvifProfile]) -> OnvifProfile:
    """The highest-resolution profile the device offers.

    Highest, not the first or the "main" one by name. DISCOVERY finding 12 measured the
    resolution ceiling directly: this pipeline reads 8 plates at 2560x1440, 2 at
    1280x720 and none at 704x396. A sub-stream is cheaper and reads nothing, so choosing
    one would be choosing a throughput figure over a working system -- the same trap
    HLD_RECONCILIATION documents.
    """
    return max(profiles, key=lambda p: (p.width or 0) * (p.height or 0))


class OnvifSource:
    """One ONVIF camera, reached by its device service URL."""

    transport = "rtsp"

    def __init__(
        self,
        service_url: str,
        camera_ref: str,
        credential: CameraCredential | None = None,
        *,
        profile_token: str | None = None,
        join_timeout_s: float = 20.0,
        backoff_min_s: float = 2.0,
        backoff_max_s: float = 30.0,
        client: OnvifClient | None = None,
    ) -> None:
        self.camera_ref = camera_ref
        # Injectable so contract tests can drive a mock device without a socket, in the
        # same style as the existing source tests.
        self._client = client or OnvifClient(service_url, credential)
        self._requested_profile = profile_token

        self._profile: OnvifProfile | None = None
        self._stream_uri: str | None = None
        self._clock_offset_s: float | None = None
        self._last_error: str | None = None
        self._first_pts_ms: float | None = None
        self._epoch: datetime | None = None

        self._join_timeout_s = join_timeout_s
        self._backoff_min_s = backoff_min_s
        self._backoff_max_s = backoff_max_s
        self._session: StreamSession | None = None

    # ------------------------------------------------------------------ device

    def _resolve(self) -> str:
        """Pick a profile and get its RTSP URL, reading the device clock on the way.

        Called lazily and memoised. A stream URL from a device can carry a session
        token, so it is fetched once per adapter rather than once per reconnect -- but
        the fetch is cheap enough that a future change to re-fetch per reconnect would
        be safe, which is why `StreamSession` is given a callable.
        """
        if self._stream_uri is not None:
            return self._stream_uri

        try:
            clock = self._client.get_system_date_and_time()
            self._clock_offset_s = clock.offset_s
            if abs(clock.offset_s) > MAX_TRUSTED_SKEW_S:
                log.warning(
                    "camera=%s device clock is %.1fs from ours; timestamps will be "
                    "treated as no better than an estimate",
                    self.camera_ref,
                    clock.offset_s,
                )
        except OnvifError as exc:
            # Not fatal. GetSystemDateAndTime is unauthenticated by specification, but
            # some devices refuse it anyway, and a camera that will not say the time can
            # still deliver frames. The confidence drops to a gateway's instead.
            self._clock_offset_s = None
            log.info("camera=%s did not report its clock: %s", self.camera_ref, exc)

        profiles = self._client.get_profiles()
        if self._requested_profile:
            match = next((p for p in profiles if p.token == self._requested_profile), None)
            if match is None:
                raise OnvifError(
                    f"profile '{self._requested_profile}' not offered by this device; "
                    f"available: {', '.join(p.token for p in profiles)}"
                )
            self._profile = match
        else:
            self._profile = _best_profile(profiles)

        self._stream_uri = self._client.get_stream_uri(self._profile.token)
        return self._stream_uri

    def _ensure_session(self) -> StreamSession:
        if self._session is None:
            self._session = StreamSession(
                # A callable rather than a string, so a reconnect can re-resolve a URL
                # that carried a session token. Memoised, so it costs nothing today.
                self._resolve,
                self.camera_ref,
                transport=self.transport,
                join_timeout_s=self._join_timeout_s,
                backoff_min_s=self._backoff_min_s,
                backoff_max_s=self._backoff_max_s,
            )
        return self._session

    # ------------------------------------------------------------------- probe

    def probe(self) -> CameraCapabilities:
        """Connect briefly and measure, then report declared beside measured.

        Unlike the gateway, this device states what it believes it is sending, so the
        divergence the integration guide warns about is finally observable.
        """
        session = self._ensure_session()
        deadline = time.monotonic() + 8.0
        try:
            for _frame in session.frames():
                if time.monotonic() >= deadline:
                    break
        except Exception as exc:  # noqa: BLE001 - a probe characterises, never crashes
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("probe failed camera=%s: %s", self.camera_ref, self._last_error)
        finally:
            session.stop()
            session.close()

        stats = session.stats
        profile = self._profile
        return CameraCapabilities(
            codec=stats.fourcc or (profile.codec if profile else None),
            width=stats.width or (profile.width if profile else None),
            height=stats.height or (profile.height if profile else None),
            measured_fps=round(stats.measured_fps, 3) if stats.measured_fps else None,
            declared_fps=profile.declared_fps if profile else None,
            transport=self.transport,
            # ONVIF devices commonly support replay through a separate service; this
            # adapter speaks only to the live stream, so it does not claim seeking.
            supports_seek=False,
            extra={
                "onvif_profile": profile.token if profile else "",
                "onvif_profile_name": profile.name if profile else "",
                "device_clock_offset_s": (
                    round(self._clock_offset_s, 3) if self._clock_offset_s is not None else ""
                ),
            },
        )

    # ------------------------------------------------------------------ frames

    def open(self) -> Iterator[Frame]:
        session = self._ensure_session()
        for frame in session.frames():
            if self._first_pts_ms is None:
                self._first_pts_ms = frame.pts_ms
                if self._epoch is None:
                    # Anchored on the device's clock where it is trustworthy, ours
                    # otherwise. This is the one place ONVIF gives a better answer than
                    # the gateway can.
                    now = datetime.now(timezone.utc)
                    offset = self._clock_offset_s
                    if offset is not None and abs(offset) <= MAX_TRUSTED_SKEW_S:
                        self._epoch = now + timedelta(seconds=offset)
                    else:
                        self._epoch = now
            yield frame

    # ------------------------------------------------------------------- clock

    def observed_at(self, frame: Frame) -> datetime:
        if self._epoch is None or self._first_pts_ms is None:
            return datetime.now(timezone.utc)
        return self._epoch + timedelta(milliseconds=frame.pts_ms - self._first_pts_ms)

    @property
    def clock_confidence(self) -> float:
        """Higher than a gateway's when the device's clock is readable and synchronised.

        Falls back to the gateway's 0.85 when the device would not report its clock or
        is badly skewed, and decays with discontinuities exactly as the gateway's does,
        because a PTS reset re-derives the mapping either way.
        """
        if self._epoch is None:
            return 0.0
        trusted = (
            self._clock_offset_s is not None and abs(self._clock_offset_s) <= MAX_TRUSTED_SKEW_S
        )
        base = ONVIF_CLOCK_CONFIDENCE if trusted else 0.85
        discontinuities = self._session.stats.discontinuities if self._session else 0
        return round(max(0.4, base - 0.05 * discontinuities), 3)

    # ------------------------------------------------------------------ health

    def health(self) -> HealthReport:
        stats = self._session.stats if self._session else None
        capabilities: dict[str, str] = {}
        try:
            capabilities = self._client.get_service_capabilities()
        except OnvifError as exc:
            # A device that will not answer a capability request can still be streaming
            # fine, so this is recorded rather than treated as unhealthy.
            log.debug("camera=%s capabilities unavailable: %s", self.camera_ref, exc)

        return HealthReport(
            camera_ref=self.camera_ref,
            # A device that answered a capability request, or that has delivered
            # frames, is reachable. Either alone is enough: some devices refuse
            # GetServiceCapabilities while streaming perfectly, and a device can be
            # answering SOAP while its stream is down.
            reachable=bool(capabilities) or bool(stats and stats.frames),
            transport=self.transport,
            frames=stats.frames if stats else 0,
            measured_fps=round(stats.measured_fps, 3) if stats and stats.measured_fps else None,
            declared_fps=self._profile.declared_fps if self._profile else None,
            reconnects=stats.reconnects if stats else 0,
            discontinuities=stats.discontinuities if stats else 0,
            last_error=self._last_error,
            codec=stats.fourcc if stats else None,
            width=stats.width if stats else None,
            height=stats.height if stats else None,
        )

    def stop(self) -> None:
        if self._session is not None:
            self._session.stop()

    def close(self) -> None:
        if self._session is not None:
            self._session.stop()
            self._session.close()


__all__ = ["MAX_TRUSTED_SKEW_S", "ONVIF_CLOCK_CONFIDENCE", "OnvifSource"]
