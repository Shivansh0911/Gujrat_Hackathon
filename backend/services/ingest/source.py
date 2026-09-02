"""The CameraSource protocol — the only way the platform touches a camera.

One interface, several implementations. This is the vendor-neutrality claim in the HLD
expressed as code: onboarding a new source family is writing one class against this
protocol, not changing the pipeline.

Timing contract, binding on every implementation:

    `Frame.pts_ms` is the stream's own presentation timestamp and is the ONLY
    legitimate timing source. It resets at a loop point, exactly as the gateway's
    does. Consumers that need wall-clock must call `observed_at()`, which folds in
    the source's epoch and accumulated loop offset, and must never derive time from
    when a frame arrived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Protocol, runtime_checkable

from services.common.stream_client import Frame


@dataclass(frozen=True)
class CameraCapabilities:
    """What a source actually offers, established by probing rather than declaration."""

    codec: str | None
    width: int | None
    height: int | None
    measured_fps: float | None
    declared_fps: float | None
    transport: str
    duration_s: float | None = None
    supports_seek: bool = False
    # Free-form extras an adapter wants to advertise (PTZ, ONVIF events, sub-streams).
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def fps_disagrees(self) -> bool:
        """Declared rate absent, or materially different from what we measured."""
        if self.measured_fps is None:
            return False
        if self.declared_fps is None:
            return True
        return abs(self.declared_fps - self.measured_fps) / self.declared_fps > 0.15


@dataclass
class HealthReport:
    """Per-camera health. Feeds the Health screen and the §2.5 fault report."""

    camera_ref: str
    reachable: bool
    transport: str
    measured_fps: float | None = None
    declared_fps: float | None = None
    frames: int = 0
    reconnects: int = 0
    decode_failures: int = 0
    discontinuities: int = 0
    time_to_first_frame_s: float | None = None
    last_error: str | None = None
    # Observed stream properties. The estate's catalogue publishes only `id` and
    # `name` -- confirmed by fetching it: 1,373 bytes for thirty cameras -- so the
    # only way the registry can hold a codec or a resolution for a government camera
    # is to record what the decoder actually saw. §2.2 of the integration guide asks
    # consumers to read per-camera properties rather than assume a uniform grid;
    # where they are not published, measuring them is the same requirement.
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def fps_drift(self) -> float | None:
        """Relative drift of measured from declared. Surfaced as a maintenance signal."""
        if self.measured_fps is None or not self.declared_fps:
            return None
        return (self.measured_fps - self.declared_fps) / self.declared_fps


@runtime_checkable
class CameraSource(Protocol):
    """One implementation per source family."""

    camera_ref: str
    transport: str

    def probe(self) -> CameraCapabilities:
        """Establish real properties by connecting. Never trusts a declaration."""
        ...

    def open(self) -> Iterator[Frame]:
        """Yield frames until closed. Reconnects internally; does not raise on a gap."""
        ...

    def health(self) -> HealthReport: ...

    def observed_at(self, frame: Frame) -> datetime:
        """Map a frame's stream PTS onto the wall clock.

        Separate from `Frame` because the mapping is a property of the source, not of
        the frame: a file source knows its timeline exactly, a live stream's mapping
        is an estimate, and the confidence in that mapping is recorded per detection.
        """
        ...

    @property
    def clock_confidence(self) -> float:
        """How much to trust `observed_at`, in [0, 1].

        1.0 for a file whose timeline we control. Lower for a live stream where the
        mapping from PTS to wall clock is inferred and can drift.
        """
        ...

    def close(self) -> None: ...
