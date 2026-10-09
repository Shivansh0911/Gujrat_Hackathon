"""The onboarding probe reports what it reached and what it never got to.

The thing worth testing here is not that a working camera works -- there is no camera
in CI. It is that a *failure* produces a usable report: the step that failed says why,
and every step after it is present and marked `skipped`, rather than being absent. An
absent step and a passing step look identical to a reader scanning a list of ticks,
which is exactly the confusion this probe exists to remove.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from services.ingest import onboarding_probe
from services.ingest.onboarding_probe import ProbeStep, probe

ALL_KEYS = ("validate", "tcp", "transport", "stream", "fps", "frame", "readiness", "analytics")


def _by_key(steps: list[ProbeStep]) -> dict[str, ProbeStep]:
    return {s.key: s for s in steps}


def test_every_step_is_reported_even_when_the_url_is_refused() -> None:
    report = probe("file:///etc/passwd")

    steps = _by_key(report.steps)
    assert [s.key for s in report.steps] == list(ALL_KEYS)
    assert steps["validate"].status == "fail"
    assert "scheme" in steps["validate"].detail
    assert report.reachable is False
    # Everything downstream is explicitly not-reached, not quietly missing.
    assert all(steps[k].status == "skipped" for k in ALL_KEYS[1:])


def test_a_private_address_is_blocked_without_naming_it() -> None:
    report = probe("rtsp://127.0.0.1:554/stream")

    validate = _by_key(report.steps)["validate"]
    assert validate.status == "fail"
    # The guard deliberately does not echo the address back; doing so would turn the
    # probe into an internal-network scanner with a nice progress list.
    assert "127.0.0.1" not in validate.detail
    assert report.reachable is False


def test_a_disallowed_port_is_refused_before_anything_is_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the probe must not connect when validation refused the URL")

    monkeypatch.setattr(socket, "create_connection", explode)
    report = probe("rtsp://example.org:9999/stream")

    assert _by_key(report.steps)["validate"].status == "fail"
    assert report.reachable is False


def test_an_unreachable_host_fails_at_tcp_and_skips_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        onboarding_probe.ssrf,
        "validate_uri",
        lambda uri: onboarding_probe.ssrf.ValidatedTarget(
            scheme="rtsp", host="cam.example.org", port=554, url=uri, resolved_ips=("203.0.113.7",)
        ),
    )
    monkeypatch.setattr(onboarding_probe.ssrf, "reverify_before_connect", lambda _t: None)
    monkeypatch.setattr(
        onboarding_probe,
        "_tcp_connect",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    report = probe("rtsp://cam.example.org:554/stream")
    steps = _by_key(report.steps)

    assert steps["validate"].status == "ok"
    assert steps["tcp"].status == "fail"
    assert "554" in steps["tcp"].detail
    assert report.reachable is False
    for key in ("transport", "stream", "fps", "frame", "readiness", "analytics"):
        assert steps[key].status == "skipped", key


def test_rtsp_is_preferred_and_hls_is_reported_as_a_fallback() -> None:
    transport, why = onboarding_probe._transport_for("rtsp")
    assert transport == "rtsp"
    assert "timestamp" in why

    transport, why = onboarding_probe._transport_for("https")
    assert transport == "hls"
    # The operator is told what the fallback costs, not merely that it happened.
    assert "coarser" in why


def test_the_probe_never_returns_the_url_it_was_given() -> None:
    """An RTSP authority routinely carries credentials; the report must not echo it."""
    secret = "rtsp://operator:hunter2@cam.example.org:554/stream"
    report = probe(secret)

    rendered = str(report.as_dict())
    assert "hunter2" not in rendered
    assert "operator:hunter2" not in rendered


class _FakeFrame:
    def __init__(self, image: Any, seq: int) -> None:
        self.image = image
        self.seq = seq
        self.pts_ms = seq * 40.0


class _FakeSession:
    """Stands in for StreamSession so the success path can be exercised without a camera.

    There is no camera in CI and the organiser's gateway has a finite watch-time quota,
    so the alternative to a fake here is not a better test -- it is no test of the
    success path at all.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        import numpy as np

        from services.common.stream_client import SessionStats

        self._image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.stats = SessionStats(external_id="fake", transport="rtsp")
        self.stats.width = 1280
        self.stats.height = 720
        self.stats.fourcc = "H264"
        self.stats.first_frame_latency_s = 0.8
        # The discrepancy the integration guide warns about: declares 25, delivers 15.
        self.stats.declared_fps = 25.0
        self.stats.measured_fps = 15.0

    def frames(self) -> Any:
        for seq in range(40):
            self.stats.frames = seq + 1
            yield _FakeFrame(self._image, seq)

    def stop(self) -> None: ...

    def close(self) -> None: ...


def test_the_success_path_reports_the_declared_versus_measured_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.common.stream_client as stream_client

    monkeypatch.setattr(
        onboarding_probe.ssrf,
        "validate_uri",
        lambda uri: onboarding_probe.ssrf.ValidatedTarget(
            scheme="rtsp", host="cam.example.org", port=554, url=uri, resolved_ips=("203.0.113.7",)
        ),
    )
    monkeypatch.setattr(onboarding_probe.ssrf, "reverify_before_connect", lambda _t: None)
    monkeypatch.setattr(onboarding_probe, "_tcp_connect", lambda *_a, **_k: 12.0)
    monkeypatch.setattr(stream_client, "StreamSession", _FakeSession)

    # Readiness loads two ONNX models; the divergence is what this test is about.
    report = probe("rtsp://cam.example.org:554/stream", grade_readiness=False)
    steps = _by_key(report.steps)

    assert steps["stream"].status == "ok"
    assert report.width == 1280 and report.height == 720
    assert report.codec == "H264"
    assert report.first_frame_jpeg_b64, "the operator must see the frame, not a green tick"

    fps = steps["fps"]
    assert fps.status == "warn", "a 10 fps gap must not be reported as healthy"
    assert "25.0" in fps.detail and "15.0" in fps.detail
    assert fps.data["divergence_fps"] == pytest.approx(10.0)
    assert report.declared_fps == 25.0
    assert report.measured_fps == 15.0


def test_a_rate_matching_its_declaration_is_not_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.common.stream_client as stream_client

    class _Honest(_FakeSession):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.stats.measured_fps = 24.6

    monkeypatch.setattr(
        onboarding_probe.ssrf,
        "validate_uri",
        lambda uri: onboarding_probe.ssrf.ValidatedTarget(
            scheme="rtsp", host="cam.example.org", port=554, url=uri, resolved_ips=("203.0.113.7",)
        ),
    )
    monkeypatch.setattr(onboarding_probe.ssrf, "reverify_before_connect", lambda _t: None)
    monkeypatch.setattr(onboarding_probe, "_tcp_connect", lambda *_a, **_k: 12.0)
    monkeypatch.setattr(stream_client, "StreamSession", _Honest)

    report = probe("rtsp://cam.example.org:554/stream", grade_readiness=False)
    assert _by_key(report.steps)["fps"].status == "ok"
