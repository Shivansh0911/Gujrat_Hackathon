"""Probe a camera source live, one visible step at a time.

Onboarding already worked before this module: a URL went in, a row came out. What it
could not do was *show its working*. That matters for two different reasons, and only
one of them is about the stage.

The operational reason: when onboarding fails across 26 departments, "it did not work"
is the least useful thing a platform can say. Whether the host refused the connection,
or answered and refused RTSP, or streamed happily at a resolution that will never
produce a readable plate, are three different problems with three different owners --
the network team, the VMS admin, and whoever chose where to bolt the camera. A single
boolean collapses all three into a support ticket.

The demonstrative reason: the organiser's integration guide warns not to trust a
camera's declared frame rate. Saying "we took that seriously" is a slide. Putting a
camera on screen that announces 25 fps and is measured delivering 15 is not.

So `probe()` returns an ordered list of steps, each with its own status, duration and
detail, and the caller renders them as they are. A step that fails stops the probe and
the remaining steps are reported as `skipped` rather than silently absent -- "we never
got that far" and "that part was fine" must not look the same.

Three constraints this works within rather than around:

  Every URL is user-supplied, so every URL goes through `services.common.ssrf`. The
  probe re-verifies immediately before connecting, because validation and connection
  are separated in time and a nameserver that answers differently the second time is
  the whole DNS-rebinding attack.

  The declared frame rate is read exactly once in this codebase, by `StreamSession`,
  which `check_fps_guard.py` asserts. This module gets `declared_fps` from the session's
  stats rather than opening its own capture, so the guard's count is unchanged and no
  timing path can reach the declared value.

  Readiness is graded by `readiness._grade`, the same function the estate-wide grading
  uses. A probe that invented its own thresholds would give a camera one grade at
  onboarding and a different one a day later.
"""

from __future__ import annotations

import base64
import socket
import time
from dataclasses import dataclass, field
from statistics import median
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from services.analytics.readiness import ReadinessGrade, _grade
from services.common import ssrf

if TYPE_CHECKING:  # pragma: no cover - import cost kept out of the API process
    from numpy.typing import NDArray

StepStatus = Literal["ok", "warn", "fail", "skipped"]

#: How many frames to pull before reporting a measured frame rate. `StreamSession`
#: computes the rate from PTS deltas over a window, so this is the window and not a
#: frame budget: fewer than about 30 and a single late packet moves the answer.
FRAME_TARGET = 40

#: Total wall-clock ceiling. An operator watching a progress list will abandon a probe
#: well before a TCP connection to a dead host times out on its own.
DEADLINE_S = 30.0

#: Frames sampled for readiness grading. Every frame would be more evidence and far
#: more CPU; the measurement is a median over detections, not over frames.
GRADE_FRAMES = 8


@dataclass
class ProbeStep:
    """One stage of the probe, as the operator sees it."""

    key: str
    label: str
    status: StepStatus
    detail: str
    duration_ms: float | None = None
    #: Anything the UI renders specially -- a resolution, a grade, an image.
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "data": self.data,
        }


@dataclass
class ProbeReport:
    url_scheme: str
    host: str
    port: int
    reachable: bool
    steps: list[ProbeStep]
    #: Set only when the stream opened. None otherwise, never 0 -- "not measured" and
    #: "measured as zero" are different findings and must not render identically.
    measured_fps: float | None = None
    declared_fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    first_frame_jpeg_b64: str | None = None
    grade: str | None = None
    grade_reasoning: str | None = None
    recommended_analytics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "url_scheme": self.url_scheme,
            "host": self.host,
            "port": self.port,
            "reachable": self.reachable,
            "steps": [s.as_dict() for s in self.steps],
            "measured_fps": self.measured_fps,
            "declared_fps": self.declared_fps,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "first_frame_jpeg_b64": self.first_frame_jpeg_b64,
            "grade": self.grade,
            "grade_reasoning": self.grade_reasoning,
            "recommended_analytics": list(self.recommended_analytics),
        }


def _transport_for(scheme: str) -> tuple[str, str]:
    """Which transport to use, and why. The 'why' is shown, not just the choice.

    RTSP is preferred wherever it is offered because it carries presentation
    timestamps. HLS does not: it segments, and segment boundaries are not frame
    timings. Every time-derived figure this platform produces -- dwell, implied speed,
    the gap between two hops -- is computed from PTS, so a camera reachable only over
    HLS is a camera whose timings are coarser, and the operator should know that at
    onboarding rather than discover it in an evidence export.
    """
    if scheme in ("rtsp", "rtsps"):
        return (
            "rtsp",
            "RTSP offered; preferred because it carries per-frame presentation timestamps",
        )
    return (
        "hls",
        "no RTSP endpoint; falling back to HLS, whose segment boundaries are coarser "
        "than per-frame timestamps",
    )


def _tcp_connect(host: str, port: int, timeout_s: float) -> float:
    """Connect and return the handshake time in milliseconds. Raises OSError."""
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout_s):
        pass
    return (time.perf_counter() - started) * 1000.0


def _encode_jpeg(image: NDArray[Any]) -> str | None:
    import cv2

    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    if not ok:
        return None
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def _measure_readiness(frames: list[NDArray[Any]]) -> tuple[ProbeStep, dict[str, Any]]:
    """Grade ANPR readiness from the frames just decoded.

    Deliberately uses the same `_grade` the estate-wide readiness screen uses. The
    sample is far smaller -- a handful of frames against a camera's whole history --
    and `_grade` already returns `insufficient_sample` when it is, which is the honest
    outcome for a camera nobody has watched yet rather than an error.
    """
    from services.analytics.anpr import OpenImagePlateDetector
    from services.analytics.vehicle_detect import YoloxVehicleDetector

    plate_widths: list[float] = []
    vehicle_widths: list[float] = []

    plate_detector = OpenImagePlateDetector()
    vehicle_detector = YoloxVehicleDetector()

    for frame in frames:
        for (x1, _y1, x2, _y2), _score in plate_detector.detect(frame):
            plate_widths.append(float(x2 - x1))
        for box in vehicle_detector.detect(frame):
            vehicle_widths.append(float(box.width))

    plate_median = median(plate_widths) if plate_widths else None
    vehicle_median = median(vehicle_widths) if vehicle_widths else None
    # Written out rather than as a conditional expression: `vehicle_median not in
    # (None, 0)` reads fine and narrows nothing, so the division is on `float | None`.
    ratio: float | None = None
    if plate_median is not None and vehicle_median is not None and vehicle_median > 0:
        ratio = plate_median / vehicle_median

    grade, reasoning, remedy = _grade(
        len(plate_widths), plate_median, len(vehicle_widths), vehicle_median, ratio
    )

    status: StepStatus
    if grade is ReadinessGrade.ANPR_CAPABLE:
        status = "ok"
    elif grade is ReadinessGrade.INSUFFICIENT_SAMPLE:
        status = "skipped"
    else:
        status = "warn"

    data = {
        "grade": grade.value,
        "reasoning": reasoning,
        "remedy": remedy,
        "plate_samples": len(plate_widths),
        "plate_px_median": plate_median,
        "vehicle_samples": len(vehicle_widths),
        "vehicle_px_median": vehicle_median,
        "frames_examined": len(frames),
    }
    step = ProbeStep(
        key="readiness",
        label="ANPR readiness",
        status=status,
        detail=reasoning,
        data=data,
    )
    return step, data


def probe(
    url: str,
    *,
    frame_target: int = FRAME_TARGET,
    deadline_s: float = DEADLINE_S,
    grade_readiness: bool = True,
) -> ProbeReport:
    """Run the full probe and return every step, in order, including the failures."""
    steps: list[ProbeStep] = []
    started = time.perf_counter()

    def remaining() -> float:
        return max(deadline_s - (time.perf_counter() - started), 0.0)

    def skip_rest(from_index: int, why: str) -> None:
        for key, label in _REMAINING_STEPS[from_index:]:
            steps.append(ProbeStep(key=key, label=label, status="skipped", detail=why))

    # --- 1. Validate -------------------------------------------------------------
    step_started = time.perf_counter()
    try:
        target = ssrf.validate_uri(url)
    except ssrf.SsrfBlocked as exc:
        steps.append(
            ProbeStep(
                key="validate",
                label="URL accepted",
                status="fail",
                detail=str(exc),
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        skip_rest(0, "the URL was rejected before anything was contacted")
        parts = urlsplit(url)
        return ProbeReport(
            url_scheme=(parts.scheme or "").lower(),
            host=parts.hostname or "",
            port=parts.port or 0,
            reachable=False,
            steps=steps,
        )

    steps.append(
        ProbeStep(
            key="validate",
            label="URL accepted",
            status="ok",
            detail=(
                f"{target.scheme}://{target.host}:{target.port} resolves to "
                f"{len(target.resolved_ips)} public address"
                f"{'' if len(target.resolved_ips) == 1 else 'es'}"
            ),
            duration_ms=(time.perf_counter() - step_started) * 1000.0,
            data={"scheme": target.scheme, "host": target.host, "port": target.port},
        )
    )

    report = ProbeReport(
        url_scheme=target.scheme,
        host=target.host,
        port=target.port,
        reachable=False,
        steps=steps,
    )

    # --- 2. TCP reachability -----------------------------------------------------
    step_started = time.perf_counter()
    try:
        ssrf.reverify_before_connect(target)
        handshake_ms = _tcp_connect(target.host, target.port, min(remaining(), 8.0))
    except ssrf.SsrfBlocked as exc:
        steps.append(
            ProbeStep(
                key="tcp",
                label="Host reachable",
                status="fail",
                detail=f"blocked on re-verification: {exc}",
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        skip_rest(1, "the host was not contacted")
        return report
    except OSError as exc:
        steps.append(
            ProbeStep(
                key="tcp",
                label="Host reachable",
                status="fail",
                detail=f"TCP connect to port {target.port} failed: {exc}",
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        skip_rest(1, "nothing answered on the port, so no stream could be negotiated")
        return report

    report.reachable = True
    steps.append(
        ProbeStep(
            key="tcp",
            label="Host reachable",
            status="ok",
            detail=f"TCP handshake in {handshake_ms:.0f} ms",
            duration_ms=handshake_ms,
        )
    )

    # --- 3. Transport negotiation ------------------------------------------------
    transport, why = _transport_for(target.scheme)
    steps.append(
        ProbeStep(
            key="transport",
            label="Transport chosen",
            status="ok" if transport == "rtsp" else "warn",
            detail=why,
            data={"transport": transport},
        )
    )

    # --- 4. Open the stream and measure ------------------------------------------
    from services.common.stream_client import StreamSession

    step_started = time.perf_counter()
    frames: list[NDArray[Any]] = []
    first_image: NDArray[Any] | None = None

    session = StreamSession(
        url,
        external_id="onboarding-probe",
        transport=transport,
        join_timeout_s=min(remaining(), 15.0),
        # One attempt. A probe that silently retries reports the health of the retry
        # loop rather than the health of the camera, and the operator is waiting.
        backoff_min_s=0.0,
        backoff_max_s=0.0,
        fps_window=frame_target,
        detect_scene_cuts=False,
    )
    try:
        for frame in session.frames():
            if first_image is None:
                first_image = frame.image
            if len(frames) < GRADE_FRAMES and frame.seq % 4 == 0:
                frames.append(frame.image)
            if session.stats.frames >= frame_target or remaining() <= 2.0:
                break
    except Exception as exc:  # noqa: BLE001 -- any decode failure is a probe result
        steps.append(
            ProbeStep(
                key="stream",
                label="Stream opened",
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        skip_rest(3, "no frames were decoded, so nothing could be measured")
        return report
    finally:
        session.stop()
        session.close()

    stats = session.stats
    if first_image is None:
        steps.append(
            ProbeStep(
                key="stream",
                label="Stream opened",
                status="fail",
                detail=(
                    "the host accepted the connection but delivered no decodable frame "
                    f"within {deadline_s:.0f} s"
                ),
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        skip_rest(3, "no frames were decoded, so nothing could be measured")
        return report

    report.measured_fps = stats.measured_fps
    report.declared_fps = stats.declared_fps
    report.width = stats.width
    report.height = stats.height
    report.codec = stats.fourcc

    steps.append(
        ProbeStep(
            key="stream",
            label="Stream opened",
            status="ok",
            detail=(
                f"{stats.width}x{stats.height} {stats.fourcc or 'unknown codec'}, "
                f"first frame in {stats.first_frame_latency_s:.1f} s"
                if stats.first_frame_latency_s is not None
                else f"{stats.width}x{stats.height} {stats.fourcc or 'unknown codec'}"
            ),
            duration_ms=(time.perf_counter() - step_started) * 1000.0,
            data={
                "width": stats.width,
                "height": stats.height,
                "codec": stats.fourcc,
                "frames": stats.frames,
            },
        )
    )

    # --- 5. Declared versus measured ---------------------------------------------
    # The integration guide's warning, demonstrated rather than cited.
    declared = stats.declared_fps
    measured = stats.measured_fps
    if measured is None:
        fps_step = ProbeStep(
            key="fps",
            label="Frame rate",
            status="warn",
            detail=(
                f"too few frames to measure a rate ({stats.frames} decoded). Declared "
                f"{declared:.1f} fps is what the camera claims, not what it delivers."
                if declared
                else f"too few frames to measure a rate ({stats.frames} decoded)"
            ),
            data={"declared_fps": declared, "measured_fps": None},
        )
    elif declared is None:
        fps_step = ProbeStep(
            key="fps",
            label="Frame rate",
            status="ok",
            detail=f"measured {measured:.1f} fps; the camera declares no rate",
            data={"declared_fps": None, "measured_fps": measured},
        )
    else:
        divergence = abs(declared - measured)
        fps_step = ProbeStep(
            key="fps",
            label="Frame rate",
            status="warn" if divergence > 2.0 else "ok",
            detail=(
                f"declares {declared:.1f} fps, delivers {measured:.1f} fps "
                f"({divergence:.1f} fps apart). Every time-derived figure on this "
                "platform is computed from presentation timestamps, never from the "
                "declared rate."
                if divergence > 2.0
                else f"declares {declared:.1f} fps, delivers {measured:.1f} fps"
            ),
            data={
                "declared_fps": declared,
                "measured_fps": measured,
                "divergence_fps": divergence,
            },
        )
    steps.append(fps_step)

    # --- 6. First frame ----------------------------------------------------------
    report.first_frame_jpeg_b64 = _encode_jpeg(first_image)
    steps.append(
        ProbeStep(
            key="frame",
            label="First frame decoded",
            status="ok" if report.first_frame_jpeg_b64 else "warn",
            detail=(
                "decoded and shown below"
                if report.first_frame_jpeg_b64
                else "decoded, but could not be re-encoded for display"
            ),
            data={"has_image": report.first_frame_jpeg_b64 is not None},
        )
    )

    # --- 7. Readiness ------------------------------------------------------------
    if not grade_readiness:
        steps.append(
            ProbeStep(
                key="readiness",
                label="ANPR readiness",
                status="skipped",
                detail="grading was not requested",
            )
        )
        return report

    step_started = time.perf_counter()
    try:
        grade_step, grade_data = _measure_readiness(frames or [first_image])
    except Exception as exc:  # noqa: BLE001 -- a missing model must not fail the probe
        steps.append(
            ProbeStep(
                key="readiness",
                label="ANPR readiness",
                status="skipped",
                detail=(
                    f"could not grade: {type(exc).__name__}. The stream is fine; the "
                    "camera can be onboarded and graded once it has been watched."
                ),
                duration_ms=(time.perf_counter() - step_started) * 1000.0,
            )
        )
        return report

    grade_step.duration_ms = (time.perf_counter() - step_started) * 1000.0
    steps.append(grade_step)
    report.grade = str(grade_data["grade"])
    report.grade_reasoning = str(grade_data["reasoning"])

    from services.analytics.readiness import analytics_for

    report.recommended_analytics = analytics_for(ReadinessGrade(report.grade))
    steps.append(
        ProbeStep(
            key="analytics",
            label="Analytics assigned",
            status="ok",
            detail=(
                "assigned from the measured pixel budget, not from configuration: "
                + ", ".join(report.recommended_analytics)
            ),
            data={"analytics": list(report.recommended_analytics)},
        )
    )
    return report


#: The steps after validation, in order, so a failure can report what it never reached.
_REMAINING_STEPS: tuple[tuple[str, str], ...] = (
    ("tcp", "Host reachable"),
    ("transport", "Transport chosen"),
    ("stream", "Stream opened"),
    ("fps", "Frame rate"),
    ("frame", "First frame decoded"),
    ("readiness", "ANPR readiness"),
    ("analytics", "Analytics assigned"),
)
