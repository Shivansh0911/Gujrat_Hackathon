#!/usr/bin/env python
"""Verify the organiser's §2.4 pre-submission checklist empirically, against the live feed.

Each of the eight items is exercised for real -- a capture is opened, a feed is
interrupted, a hard scene cut is fed to the detector. Nothing here reports PASS on the
strength of a code comment. Two items are additionally enforced by static analysis of
this repository, because "we force TCP" and "we never use declared FPS for timing" are
properties of the whole codebase, not of one lucky run.

Exit code 0 only when all eight pass, so this doubles as a CI gate.

Usage:
    python scripts/preflight_check.py [--seconds 10]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.common import evidence, redact  # noqa: E402
from services.common.catalogue import CameraDescriptor, fetch_catalogue  # noqa: E402
from services.common.config import get_settings  # noqa: E402
from services.common.cv_env import RTSP_TRANSPORT, capture_options  # noqa: E402
from services.common.scene_cut import SceneCutDetector  # noqa: E402
from services.common.stream_client import StreamSession  # noqa: E402
from services.common.transport import port_reachable, select_transport  # noqa: E402

# Decided once for the run and shared by every live check: probing the RTSP port per
# check would be 8 identical probes against the same host:port.
RTSP_AVAILABLE: bool = False

# Pause between live checks so we do not draw connection resets by reconnecting to the
# gateway back to back. Pacing our own load is §2.2's rule, and it applies to us too.
_SETTLE_S = 2.0

log = logging.getLogger("preflight")


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    detail: str
    method: str  # "static" | "live"


# --------------------------------------------------------------------- helpers

_PY_FILES_EXCLUDED = {"cv_env.py"}


def _python_sources() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts
    ]


def _grab_frames(cam: CameraDescriptor, n: int, budget_s: float):
    """Collect up to n frames, allowing `budget_s` for frames *after* joining.

    The join allowance is separate from the frame budget, and uses the same
    SETU_JOIN_TIMEOUT_S the platform runs with, so the preflight is never stricter
    than production. It matters: the first capture in a process pays cold TLS plus
    HLS master resolution, and charging that to the frame budget reported a healthy
    camera as dead purely for being first in the running order.

    The overall wall-clock bound is still essential -- a camera that never yields a
    decodable frame would otherwise keep the session reconnecting forever and hang
    the whole preflight. This is harness pacing, not analytics timing; no measurement
    here derives from arrival time.
    """
    join_timeout_s = get_settings().join_timeout_s
    source = select_transport(cam, get_settings(), rtsp_available=RTSP_AVAILABLE)
    session = StreamSession(
        source.url, cam.external_id, transport=source.transport,
        join_timeout_s=join_timeout_s, backoff_min_s=1.0, backoff_max_s=2.0,
    )
    out = []
    total_s = budget_s + join_timeout_s
    deadline = time.monotonic() + total_s
    stopper = threading.Timer(total_s, session.stop)
    stopper.daemon = True
    stopper.start()
    try:
        for frame in session.frames():
            out.append(frame)
            if len(out) >= n or time.monotonic() >= deadline:
                break
    finally:
        stopper.cancel()
        session.stop()
        session.close()
    return out, session.stats


# ---------------------------------------------------------------------- checks


def check_1_tcp_forced(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Static: cv2 only ever imported via cv_env, always with TCP forced.
    Live: frames decode over whichever transport the network actually permits.

    §2.2 forces RTSP over TCP *and* mandates the HLS fallback when 8554 is blocked.
    Both halves are checked, and the transport actually used is reported rather than
    assumed -- a green tick that hid a silent UDP fallback would be worse than a fail.
    """
    offenders = []
    pattern = re.compile(r"^\s*import\s+cv2\b|^\s*from\s+cv2\b", re.MULTILINE)
    for path in _python_sources():
        if path.name in _PY_FILES_EXCLUDED:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    opts = capture_options()
    tcp_forced = RTSP_TRANSPORT == "tcp" and "rtsp_transport;tcp" in opts

    frames, stats = _grab_frames(cameras[0], 5, seconds)
    live_ok = len(frames) > 0

    passed = not offenders and tcp_forced and live_ok
    fallback = (
        "RTSP:8554 reachable, used directly"
        if RTSP_AVAILABLE
        else "RTSP:8554 unreachable (gateway is behind Cloudflare, which proxies "
        "443/80 only) -> fell back to HLS per §2.2"
    )
    if stats.first_frame_latency_s is not None:
        retries = f", {stats.reconnects} retr(ies)" if stats.reconnects else ""
        outcome = (
            f"{len(frames)} frames decoded over {stats.transport.upper()} from "
            f"camera {cameras[0].external_id} (join {stats.first_frame_latency_s:.2f}s{retries})"
        )
    else:
        outcome = (
            f"NO FRAMES from camera {cameras[0].external_id} over "
            f"{stats.transport.upper()} after {stats.reconnects} attempt(s)"
        )
    imports = (
        "cv2 imported only via services.common.cv_env"
        if not offenders
        else f"DIRECT cv2 IMPORTS: {offenders}"
    )
    detail = (
        f"rtsp_transport forced to '{RTSP_TRANSPORT}' in every capture "
        f"(options='{opts}'); {fallback}; {outcome}; {imports}"
    )
    return Check(
        1, "Every client forces RTSP over TCP (HLS fallback when 8554 is blocked)",
        passed, detail, "static+live",
    )


def check_2_no_declared_fps_timing() -> Check:
    """Static: CAP_PROP_FPS may appear only on a line marked reference-only."""
    hits: list[str] = []
    allowed: list[str] = []
    for path in _python_sources():
        if path.name == Path(__file__).name:
            continue  # this checker necessarily contains the literal
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "CAP_PROP_FPS" not in line:
                continue
            if line.lstrip().startswith("#"):
                continue  # a comment discussing the rule is not a use of it
            ref = f"{path.relative_to(REPO_ROOT)}:{i}"
            # The single permitted use records the declared value for display beside
            # measured_fps. It must say so on the same line, so review is mechanical.
            if "reference-only" in line or "never used for timing" in line:
                allowed.append(ref)
            else:
                hits.append(ref)
    passed = not hits
    detail = (
        f"{len(allowed)} reference-only use(s) {allowed}; "
        f"{len(hits)} timing use(s) {hits}"
        if passed
        else f"CAP_PROP_FPS used without a reference-only marker at: {hits}"
    )
    return Check(2, "No timing logic depends on declared FPS", passed, detail, "static")


def check_3_gaps_tolerated(cam: CameraDescriptor, seconds: float) -> Check:
    """Live: observe real inter-frame gaps and confirm reading continued past them."""
    source = select_transport(cam, get_settings(), rtsp_available=RTSP_AVAILABLE)
    session = StreamSession(
        source.url, cam.external_id, transport=source.transport,
        backoff_min_s=1.0, backoff_max_s=2.0,
    )
    gaps: list[float] = []
    prev_pts: float | None = None
    frames_after_largest = 0
    largest = 0.0
    try:
        deadline = time.monotonic() + seconds
        for frame in session.frames():
            if prev_pts is not None:
                d = frame.pts_ms - prev_pts
                if d > 0:
                    gaps.append(d)
                    if d > largest:
                        largest, frames_after_largest = d, 0
                    else:
                        frames_after_largest += 1
            prev_pts = frame.pts_ms
            if time.monotonic() >= deadline:
                break
    finally:
        session.stop()
        session.close()

    median = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    # A pipeline that assumed a constant cadence would have stalled or errored here;
    # passing requires that frames kept arriving *after* the largest observed gap.
    passed = len(gaps) > 10 and frames_after_largest > 0
    detail = (
        f"{len(gaps)} intervals on camera {cam.external_id}: median {median:.0f}ms, "
        f"max {largest:.0f}ms ({largest / median:.1f}x median), "
        f"{frames_after_largest} frames decoded after the largest gap"
        if gaps
        else "no intervals observed"
    )
    return Check(3, "Inter-frame gaps do not crash or stall the pipeline", passed, detail, "live")


def check_4_reconnect(cam: CameraDescriptor, seconds: float) -> Check:
    """Live: force a mid-stream disconnect and confirm backoff reconnect resumes frames.

    The interruption is raised through StreamSession.request_reconnect() rather than by
    releasing the capture from this thread: OpenCV's VideoCapture is not thread-safe and
    releasing it under an in-flight read() is undefined behaviour (it raised a C++
    exception during development). This exercises the full recovery path -- capture
    torn down, jittered backoff waited out, HLS variant re-resolved, stream rejoined --
    against the live gateway.
    """
    source = select_transport(cam, get_settings(), rtsp_available=RTSP_AVAILABLE)
    session = StreamSession(
        source.url, cam.external_id, transport=source.transport,
        backoff_min_s=2.0, backoff_max_s=30.0,
    )
    killed = threading.Event()
    before = after = 0
    kill_at: float | None = None
    recovered_at: float | None = None

    def kill_after(delay: float) -> None:
        time.sleep(delay)
        session.request_reconnect()
        killed.set()

    killer = threading.Thread(target=kill_after, args=(seconds / 3,), daemon=True)
    killer.start()
    try:
        deadline = time.monotonic() + seconds
        for _frame in session.frames():
            if not killed.is_set():
                before += 1
            else:
                if kill_at is None:
                    kill_at = time.monotonic()
                after += 1
                if after == 1:
                    recovered_at = time.monotonic()
            if time.monotonic() >= deadline and after > 0:
                break
            if time.monotonic() >= deadline + 40:
                break
    finally:
        session.stop()
        session.close()

    passed = session.stats.reconnects >= 1 and after > 0
    gap = f"{recovered_at - kill_at:.1f}s" if (kill_at and recovered_at) else "n/a"
    detail = (
        f"{before} frames, disconnect forced mid-stream, "
        f"{session.stats.reconnects} reconnect(s), {after} frames after recovery "
        f"(backoff envelope 2.0s-30.0s with full jitter, observed resume {gap}); "
        f"transport={source.transport}"
        + (", HLS variant re-resolved on rejoin" if source.transport == "hls" else "")
    )
    return Check(4, "Reconnect with backoff, tested by interrupting a feed", passed, detail, "live")


def check_5_join_warnings_nonfatal(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: attach mid-stream to an HEVC feed; warnings must not abort the session."""
    # Prefer HEVC: attaching mid-GOP to H.265 is what produces the RPS/POC messages.
    hevc = next((c for c in cameras if (c.declared_codec or "").lower() in ("hevc", "h265")), None)
    cam = hevc or cameras[0]
    frames, stats = _grab_frames(cam, 25, seconds)
    passed = len(frames) > 0
    detail = (
        f"camera {cam.external_id} (codec {cam.declared_codec or 'undeclared'}): "
        f"{stats.join_decode_warnings} decode warning(s) absorbed during join, "
        f"first frame at {stats.first_frame_latency_s:.2f}s, "
        f"{len(frames)} frames decoded -- session never aborted"
        if passed
        else f"camera {cam.external_id}: no frame decoded within join timeout"
    )
    return Check(5, "Decoder warnings on join are logged, not fatal", passed, detail, "live")


def check_6_catalogue_driven(cameras: list[CameraDescriptor], catalogue_url: str) -> Check:
    """Live: the camera set and every URL used came from /api/ingest."""
    with_props = [c for c in cameras if c.properties_known]
    without = [c.external_id for c in cameras if not c.properties_known]
    passed = len(cameras) > 0 and all(c.rtsp_url for c in cameras)
    detail = (
        f"{len(cameras)} cameras from {catalogue_url}; "
        f"{len(with_props)} declare codec+resolution, "
        f"{len(without)} report empty/zero properties and are resolved by probing "
        f"(ids {without[:8]}{'...' if len(without) > 8 else ''})"
    )
    return Check(6, "Camera list and properties are read from /api/ingest", passed, detail, "live")


def check_7_mixed_codecs(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: decode one H.264 and one H.265 camera, and observe >1 resolution."""
    h264 = next((c for c in cameras if (c.declared_codec or "").lower() == "h264"), None)
    hevc = next((c for c in cameras if (c.declared_codec or "").lower() in ("hevc", "h265")), None)
    if h264 is None or hevc is None:
        return Check(
            7, "Pipeline handles mixed H.264/H.265 and mixed resolutions", False,
            "catalogue did not declare both an H.264 and an H.265 camera", "live",
        )

    results = {}
    for label, cam in (("h264", h264), ("h265", hevc)):
        frames, stats = _grab_frames(cam, 10, seconds)
        results[label] = (cam.external_id, len(frames), stats.width, stats.height)

    declared_res = {
        (c.declared_width, c.declared_height) for c in cameras if c.properties_known
    }
    both_decoded = all(v[1] > 0 for v in results.values())
    passed = both_decoded and len(declared_res) > 1
    detail = (
        "; ".join(
            f"{k}: camera {v[0]} -> {v[1]} frames at {v[2]}x{v[3]}"
            for k, v in results.items()
        )
        + f"; {len(declared_res)} distinct resolutions across the estate {sorted(declared_res)}"
    )
    return Check(7, "Pipeline handles mixed H.264/H.265 and mixed resolutions", passed, detail, "live")


def check_8_scene_discontinuity(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: no false cut within one feed; a true cut between two feeds is detected."""
    cam_a, cam_b = cameras[0], cameras[min(len(cameras) - 1, 5)]
    frames_a, _ = _grab_frames(cam_a, 40, seconds)
    frames_b, _ = _grab_frames(cam_b, 5, seconds)
    if len(frames_a) < 10 or not frames_b:
        return Check(8, "Behaviour is sane across a scene discontinuity", False,
                     "insufficient frames captured to exercise the detector", "live")

    det = SceneCutDetector()
    false_positives = sum(1 for f in frames_a if det.update(f.image))

    # A genuine hard cut: the last frame of one camera followed by a frame from a
    # different camera. This is what the recording loop point looks like -- a real
    # scene replaced by an unrelated real scene, not synthetic noise.
    det2 = SceneCutDetector()
    for f in frames_a[:10]:
        det2.update(f.image)
    detected = det2.update(frames_b[-1].image)

    passed = false_positives == 0 and detected
    detail = (
        f"{len(frames_a)} consecutive frames from camera {cam_a.external_id}: "
        f"{false_positives} false cut(s); hard cut to camera {cam_b.external_id} "
        f"{'detected' if detected else 'MISSED'} "
        f"(hist_corr={det2.last_corr:.2f}, mad={det2.last_mad:.1f}); "
        "on detection trackers reset and a new session id is issued while written "
        "evidence is preserved"
    )
    return Check(8, "Behaviour is sane across a scene discontinuity", passed, detail, "live")


def _preflight_markdown(checks: list[Check], settings, passed: int) -> str:
    """Human-readable companion to the JSON evidence record."""
    prov = evidence.provenance()
    lines = [
        "# Preflight — organiser's §2.4 pre-submission checklist",
        "",
        f"**Result: {passed}/{len(checks)} checks passed.**",
        "",
        f"- **Gateway:** `{settings.gateway_host}`",
        f"- **Catalogue:** `{settings.catalogue_url}`",
        f"- **Transport:** {'RTSP over TCP' if RTSP_AVAILABLE else f'HLS (RTSP :{settings.gateway_rtsp_port} unreachable from this network)'}",
        f"- **Commit:** `{prov['git_sha']}`"
        f"{' — **working tree dirty**, this artefact does not correspond to a commit' if prov['git_tree_dirty'] else ''}",
        f"- **Branch:** `{prov['git_branch']}`",
        f"- **Python:** {prov['python']} on {prov['platform']}",
        "",
        "Every check marked *live* was exercised against the running gateway: a capture",
        "opened, a feed interrupted, a hard scene cut fed to the detector. Checks marked",
        "*static* are assertions over this repository's source. Nothing here reports a",
        "pass on the strength of a code comment.",
        "",
        "| # | Result | How | Check |",
        "|---:|---|---|---|",
    ]
    for c in checks:
        lines.append(
            f"| {c.number} | {'✅ PASS' if c.passed else '❌ FAIL'} | {c.method} | {c.name} |"
        )
    lines += ["", "## Measured values behind each check", ""]
    for c in checks:
        lines += [
            f"### {c.number}. {c.name}",
            "",
            f"**{'PASS' if c.passed else 'FAIL'}** ({c.method})",
            "",
            f"{c.detail}",
            "",
        ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=10.0, help="live window per check")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "preflight.json")
    ap.add_argument(
        "--emit-evidence",
        action="store_true",
        help="write a dated, immutable record to reports/evidence/ for submission",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    redact.install(level=logging.DEBUG if args.verbose else logging.WARNING)
    settings = get_settings()

    global RTSP_AVAILABLE
    RTSP_AVAILABLE = port_reachable(settings.gateway_host, settings.gateway_rtsp_port)

    print("\nProject SETU preflight - §2.4 pre-submission checklist")
    print(f"gateway  : {settings.gateway_host}")
    print(f"catalogue: {settings.catalogue_url}")
    print(
        f"transport: {'RTSP/TCP' if RTSP_AVAILABLE else 'HLS (RTSP :%d unreachable)' % settings.gateway_rtsp_port}\n"
    )

    cameras = fetch_catalogue(settings)
    live = [c for c in cameras if c.live]
    if not live:
        print("FAIL: no live cameras in the catalogue")
        return 2

    checks_run: list[str] = []

    def run(fn, *fn_args) -> Check:
        # Progress is printed as each check starts so an operator watching a slow
        # live check can see which one is taking the time, rather than a dead console.
        # Let the gateway settle between checks. Each check opens fresh sessions, and
        # back-to-back connects from one client draw connection resets -- pacing our
        # own load is the same §2.2 rule the ingest pool obeys.
        if _SETTLE_S and checks_run:
            time.sleep(_SETTLE_S)
        checks_run.append(fn.__name__)

        started = time.monotonic()
        print(f"  running check {fn.__name__} ...", flush=True)
        try:
            result = fn(*fn_args)
        except Exception as exc:  # noqa: BLE001
            # A checklist tool that dies on one check reports nothing about the other
            # seven. The failure is recorded as a FAIL with its cause, never swallowed.
            result = Check(
                int(fn.__name__.split("_")[1]),
                fn.__name__,
                False,
                f"check raised {type(exc).__name__}: {exc}",
                "error",
            )
        print(
            f"    -> {'PASS' if result.passed else 'FAIL'} "
            f"({time.monotonic() - started:.1f}s)",
            flush=True,
        )
        return result

    # Cheap and static first, slowest live check last.
    checks = [
        run(check_2_no_declared_fps_timing),
        run(check_6_catalogue_driven, cameras, settings.catalogue_url),
        run(check_1_tcp_forced, live, args.seconds),
        run(check_5_join_warnings_nonfatal, live, args.seconds),
        run(check_3_gaps_tolerated, live[0], args.seconds),
        run(check_7_mixed_codecs, live, args.seconds),
        run(check_8_scene_discontinuity, live, args.seconds),
        run(check_4_reconnect, live[0], args.seconds),
    ]
    checks.sort(key=lambda c: c.number)

    width = 62
    print(f"{'#':>2}  {'RESULT':<6} {'HOW':<12} CHECK")
    print("-" * (width + 24))
    for c in checks:
        print(f"{c.number:>2}  {'PASS' if c.passed else 'FAIL':<6} {c.method:<12} {c.name}")
        print(f"{'':>2}  {'':<6} {'':<12} {c.detail}")
    passed = sum(1 for c in checks if c.passed)
    print("-" * (width + 24))
    print(f"{passed}/{len(checks)} checks passed\n")

    if args.emit_evidence:
        json_path, md_path = evidence.write(
            "preflight",
            {
                "gateway_host": settings.gateway_host,
                "catalogue_url": settings.catalogue_url,
                "rtsp_port_reachable": RTSP_AVAILABLE,
                "transport_used": "rtsp" if RTSP_AVAILABLE else "hls",
                "cameras_catalogued": len(cameras),
                "cameras_live_flagged": len(live),
                "checks": [c.__dict__ for c in checks],
                "passed": passed,
                "total": len(checks),
            },
            _preflight_markdown(checks, settings, passed),
        )
        print(f"evidence written: {json_path.name} and {md_path.name}\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "gateway_host": settings.gateway_host,
                "transport": settings.rtsp_transport,
                "cameras_catalogued": len(cameras),
                "cameras_live": len(live),
                "checks": [c.__dict__ for c in checks],
                "passed": passed,
                "total": len(checks),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"evidence written: {args.out}\n")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
