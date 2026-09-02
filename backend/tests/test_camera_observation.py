"""An ingest run must leave the registry knowing what it saw.

Before this, every government camera sat at `DRAFT` with a null codec, resolution and
frame rate, while the ingest logs from the same minute recorded `measured_fps=14.937`
across 393 decoded frames. The measurement was taken and thrown away.

The estate's catalogue carries only `id` and `name` -- verified by fetching it -- so
there is no other source for these values.
"""

from __future__ import annotations

from services.registry.enums import CameraStatus
from services.registry.observation import _walk


class FakeCamera:
    """Only the fields the status walk touches."""

    def __init__(self, status: CameraStatus) -> None:
        self.camera_ref = "cam01"
        self.status = status.value


def test_a_draft_camera_that_delivered_frames_becomes_active() -> None:
    """`DRAFT -> ACTIVE` is not a legal hop, so it must be walked through PROBING."""
    cam = FakeCamera(CameraStatus.DRAFT)
    _walk(cam, CameraStatus.ACTIVE)  # type: ignore[arg-type]
    assert cam.status == CameraStatus.ACTIVE.value


def test_a_draft_camera_that_answered_nothing_becomes_unreachable() -> None:
    cam = FakeCamera(CameraStatus.DRAFT)
    _walk(cam, CameraStatus.UNREACHABLE)  # type: ignore[arg-type]
    assert cam.status == CameraStatus.UNREACHABLE.value


def test_a_camera_that_comes_back_returns_to_active() -> None:
    """The media plane has failed independently of the control plane before."""
    cam = FakeCamera(CameraStatus.UNREACHABLE)
    _walk(cam, CameraStatus.ACTIVE)  # type: ignore[arg-type]
    assert cam.status == CameraStatus.ACTIVE.value


def test_a_decommissioned_camera_is_never_revived() -> None:
    """Terminal means terminal: evidence rows reference camera identities."""
    cam = FakeCamera(CameraStatus.DECOMMISSIONED)
    _walk(cam, CameraStatus.ACTIVE)  # type: ignore[arg-type]
    assert cam.status == CameraStatus.DECOMMISSIONED.value


def test_an_already_active_camera_is_left_alone() -> None:
    cam = FakeCamera(CameraStatus.ACTIVE)
    _walk(cam, CameraStatus.ACTIVE)  # type: ignore[arg-type]
    assert cam.status == CameraStatus.ACTIVE.value


def test_the_health_report_can_carry_stream_properties() -> None:
    """Without these fields the measurement has nowhere to travel."""
    from services.ingest.source import HealthReport

    h = HealthReport(
        camera_ref="cam01",
        reachable=True,
        transport="rtsp",
        codec="h264",
        width=1920,
        height=1080,
        measured_fps=14.937,
    )
    assert (h.codec, h.width, h.height) == ("h264", 1920, 1080)
