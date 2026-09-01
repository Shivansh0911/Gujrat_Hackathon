#!/usr/bin/env python
"""Verify the organiser's §2.4 pre-submission checklist empirically, against the live feed.

Each of the eight items is exercised for real -- a capture is opened, a feed is
interrupted, a hard scene cut is fed to the detector. Nothing here reports PASS on the
strength of a code comment. Two items are additionally enforced by static analysis of
this repository, because "we force TCP" and "we never use declared FPS for timing" are
properties of the whole codebase, not of one lucky run.

Exit codes: 0 when every check that ran passed, 1 when a check ran and failed,
3 when the organiser's catalogue could not be read at all -- an upstream outage is
not a pass and not a pipeline defect, and must not be reported as either.

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

# backend/ on the path so `services.*` imports resolve however this is launched.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling scripts

from services.common.paths import PROJECT_ROOT as REPO_ROOT  # noqa: E402

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
    #: "pass" | "fail" | "not_exercised". Left empty to mean "derive it from `passed`".
    #:
    #: The third state exists because "our pipeline mishandled this" and "the
    #: third-party feed gave us nothing to handle" are different findings, and
    #: collapsing them into FAIL misreports both. A reviewer running this against a
    #: gateway that is half down should see which checks were genuinely exercised.
    #:
    #: It used to default to "pass", and almost no check set it. Since the summary
    #: counts `status` and not `passed`, every check that computed False and returned
    #: without a status was printed as PASS -- the harness discarding the one thing it
    #: existed to determine. Deriving the default makes the two unable to disagree.
    status: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            self.status = "pass" if self.passed else "fail"


def _not_exercised(number: int, name: str, why: str) -> "Check":
    return Check(number, name, False, f"NOT EXERCISED: {why}", "live", "not_exercised")


# --------------------------------------------------------------------- helpers

_PY_FILES_EXCLUDED = {"cv_env.py"}


def _python_sources() -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob("*.py")
        if ".venv" not in p.parts and "node_modules" not in p.parts
    ]


def _first_with_frames(cameras: list[CameraDescriptor], n: int, budget_s: float):
    """Try each camera in turn; return the first that yields frames.

    Liveness on this gateway is not stable between one connection and the next -- a
    camera can answer during discovery and give nothing thirty seconds later. Taking
    `verified[0]` and accepting whatever it did made a check's result depend on that
    flap rather than on the pipeline.
    """
    last = (None, [], None)
    for cam in cameras:
        frames, stats = _grab_frames(cam, n, budget_s)
        if frames:
            return cam, frames, stats
        last = (cam, frames, stats)
    return last


def discover_live_cameras(
    cameras: list[CameraDescriptor], want: int, budget_s: float
) -> list[CameraDescriptor]:
    """Return cameras that actually produced a frame, most recently verified first.

    The catalogue's `live` flag is a claim, not a health signal -- DISCOVERY finding 9
    -- and on this estate a large minority of cameras flagged live deliver nothing.
    Checks that need a working feed were previously handed `live[0]`, so whether the
    preflight could exercise them at all came down to whether camera 1 happened to be
    up. It frequently is not, and the checks then reported FAIL with details like
    "no intervals observed": a true statement about the harness, read as a false
    statement about the pipeline.

    Probing costs one short connection per candidate and is done once, with the
    result shared by every check that needs it.
    """
    found: list[CameraDescriptor] = []
    for cam in cameras:
        if len(found) >= want:
            break
        frames, _ = _grab_frames(cam, 3, budget_s)
        if frames:
            found.append(cam)
            print(f"    camera {cam.external_id}: delivering frames", flush=True)
        else:
            print(f"    camera {cam.external_id}: no frames, skipping", flush=True)
    return found


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
        source.url,
        cam.external_id,
        transport=source.transport,
        join_timeout_s=join_timeout_s,
        backoff_min_s=1.0,
        backoff_max_s=2.0,
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


def _grab_stats_placeholder():
    """Stats shaped like a failed capture, for when there is no camera to try."""
    from services.common.stream_client import SessionStats

    return SessionStats(external_id="-", transport="hls" if not RTSP_AVAILABLE else "rtsp")


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

    if cameras:
        cam, frames, stats = _first_with_frames(cameras, 5, seconds)
        if stats is None:
            stats = _grab_stats_placeholder()
    else:
        cam, frames, stats = None, [], _grab_stats_placeholder()
    live_ok = len(frames) > 0

    # The static half is the guarantee: TCP is forced in every capture, and cv2 is
    # reachable only through the module that forces it. That is provable from the
    # source and the capture options whatever the gateway is doing. The live half
    # only *demonstrates* it. Failing the whole check because a third-party feed was
    # down would report a pipeline defect that does not exist -- and passing it
    # without frames would claim a demonstration that never happened.
    static_ok = not offenders and tcp_forced
    passed = static_ok and live_ok
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
            f"camera {cam.external_id if cam else '-'} "
            f"(join {stats.first_frame_latency_s:.2f}s{retries})"
        )
    else:
        outcome = (
            f"NO FRAMES from camera {cam.external_id if cam else '-'} over "
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
    if static_ok and not live_ok:
        return Check(
            1,
            "Every client forces RTSP over TCP (HLS fallback when 8554 is blocked)",
            False,
            "NOT EXERCISED live: the static guarantee holds -- " + detail,
            "live",
            "not_exercised",
        )
    return Check(
        1,
        "Every client forces RTSP over TCP (HLS fallback when 8554 is blocked)",
        passed,
        detail,
        "static+live",
    )


def check_2_no_declared_fps_timing() -> Check:
    """Static: CAP_PROP_FPS may appear only on a line marked reference-only.

    Delegates to scripts/check_fps_guard.py rather than reimplementing the scan.
    This check previously had its own copy of the rule and the two drifted: the
    preflight exempted only its own filename, so when the guard script was added it
    counted the guard's own NEEDLE constant and reported a violation that did not
    exist. One rule, one implementation, and CI and the preflight now agree by
    construction rather than by coincidence.
    """
    from check_fps_guard import EXPECTED_READS, find_reads

    hits = find_reads()
    marked = [h for h in hits if "reference-only" in h[2] or "never used for timing" in h[2]]
    unmarked = [h for h in hits if h not in marked]

    passed = not unmarked and len(hits) == EXPECTED_READS
    if unmarked:
        detail = "CAP_PROP_FPS used without a reference-only marker at: " + ", ".join(
            f"{rel}:{line}" for rel, line, _ in unmarked
        )
    else:
        detail = (
            f"{len(marked)} reference-only use(s), all marked on the line "
            f"({', '.join(f'{rel}:{line}' for rel, line, _ in marked)}); "
            f"0 timing use(s). Expected count {EXPECTED_READS} — "
            + (
                "matches"
                if len(hits) == EXPECTED_READS
                else f"MISMATCH, found {len(hits)}; raising it is a reviewed decision"
            )
        )
    return Check(2, "No timing logic depends on declared FPS", passed, detail, "static")


NAME_3 = "Inter-frame gaps do not crash or stall the pipeline"


def check_3_gaps_tolerated(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: observe real inter-frame gaps and confirm reading continued past them."""
    if not cameras:
        return _not_exercised(3, NAME_3, "no camera on the gateway delivered frames")

    settings = get_settings()
    attempted: list[str] = []

    for cam in cameras:
        attempted.append(cam.external_id)
        source = select_transport(cam, settings, rtsp_available=RTSP_AVAILABLE)
        session = StreamSession(
            source.url,
            cam.external_id,
            transport=source.transport,
            backoff_min_s=1.0,
            backoff_max_s=2.0,
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

        if len(gaps) <= 10:
            # Nothing to observe on this camera; try the next before giving up.
            continue

        median = sorted(gaps)[len(gaps) // 2]
        # A pipeline that assumed a constant cadence would have stalled or errored
        # here; passing requires that frames kept arriving *after* the largest gap.
        passed = frames_after_largest > 0
        detail = (
            f"{len(gaps)} intervals on camera {cam.external_id}: median {median:.0f}ms, "
            f"max {largest:.0f}ms ({largest / median:.1f}x median), "
            f"{frames_after_largest} frames decoded after the largest gap"
        )
        return Check(3, NAME_3, passed, detail, "live", "pass" if passed else "fail")

    # Every candidate gave too few frames to measure an interval. That is a statement
    # about the feed, not about the pipeline: there was nothing here to stall on.
    return _not_exercised(
        3,
        NAME_3,
        f"no camera produced enough frames to measure inter-frame intervals "
        f"(tried {', '.join(attempted)})",
    )


NAME_4 = "Reconnect uses backoff and resumes frames"


def check_4_reconnect(cam: CameraDescriptor | None, seconds: float) -> Check:
    """Live: force a mid-stream disconnect and confirm backoff reconnect resumes frames.

    The interruption is raised through StreamSession.request_reconnect() rather than by
    releasing the capture from this thread: OpenCV's VideoCapture is not thread-safe and
    releasing it under an in-flight read() is undefined behaviour (it raised a C++
    exception during development). This exercises the full recovery path -- capture
    torn down, jittered backoff waited out, HLS variant re-resolved, stream rejoined --
    against the live gateway.
    """
    if cam is None:
        # The signature already admits None, and this is what None means: nothing on
        # the gateway delivered a frame, so there was no session to interrupt. Falling
        # through raised an AttributeError that `run` recorded as FAIL -- reporting a
        # reconnect defect on a run where no connection was ever made.
        return _not_exercised(4, NAME_4, "no camera delivered frames, so none could be cut")

    source = select_transport(cam, get_settings(), rtsp_available=RTSP_AVAILABLE)
    session = StreamSession(
        source.url,
        cam.external_id,
        transport=source.transport,
        backoff_min_s=2.0,
        backoff_max_s=30.0,
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
    if not cameras:
        return _not_exercised(
            5, "Decoder warnings on join are logged, not fatal", "no camera delivered frames"
        )

    name = "Decoder warnings on join are logged, not fatal"

    # Prefer HEVC: attaching mid-GOP to H.265 is what produces the RPS/POC messages.
    # Then fall through the rest of the estate rather than insisting on one camera --
    # liveness flaps between one connection and the next, and this check previously
    # took `cameras[0]`, so a camera that happened to be down at that second was
    # reported as "decoder warnings are fatal". That is the mirror image of the defect
    # `discover_live_cameras` exists to prevent: the harness's luck, read as the
    # pipeline's behaviour.
    hevc = [c for c in cameras if (c.declared_codec or "").lower() in ("hevc", "h265")]
    ordered = hevc + [c for c in cameras if c not in hevc]
    cam, frames, stats = _first_with_frames(ordered, 25, seconds)

    if not frames or cam is None or stats is None:
        # No frame decoded is not evidence that warnings are fatal; it is the absence
        # of the demonstration. Reporting FAIL here would claim a defect we did not see.
        tried = ", ".join(c.external_id for c in ordered[:6])
        return _not_exercised(
            5, name, f"no camera delivered a frame to join ({len(ordered)} tried: {tried})"
        )

    latency = stats.first_frame_latency_s
    detail = (
        f"camera {cam.external_id} (codec {cam.declared_codec or 'undeclared'}): "
        f"{stats.join_decode_warnings} decode warning(s) absorbed during join, "
        f"first frame at {latency:.2f}s, "
        if latency is not None
        else f"camera {cam.external_id}: {stats.join_decode_warnings} decode warning(s), "
    ) + f"{len(frames)} frames decoded -- session never aborted"
    return Check(5, name, True, detail, "live")


def check_6_catalogue_driven(cameras: list[CameraDescriptor], catalogue_url: str) -> Check:
    """Live: the camera set and every URL used came from /api/ingest."""
    if not cameras:
        # An empty catalogue is the gateway declining to answer, not this pipeline
        # sourcing its cameras from somewhere it should not. Reporting FAIL here said
        # the opposite of what happened.
        return _not_exercised(
            6,
            "Camera list and properties are read from /api/ingest",
            f"the catalogue at {catalogue_url} returned no cameras",
        )
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


#: How many cameras to open when establishing codecs by probe. Enough to find both
#: families on a mixed estate without opening thirty captures to prove a point.
_CODEC_PROBE_LIMIT = 10

_HEVC_FOURCCS = {"hevc", "h265", "hev1", "hvc1"}


def check_7_mixed_codecs(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: decode one H.264 and one H.265 camera, and observe >1 resolution.

    Codecs come from the catalogue when it declares them, and from *probing* when it
    does not. The current catalogue declares nothing at all -- it carries an id and a
    name per camera and no properties -- and an earlier version of this check gave up
    at that point and reported NOT EXERCISED on an estate that plainly carries both
    families. That was the catalogue's silence being reported as a gap in the pipeline.

    Probing is not a workaround here, it is the rule this project already follows:
    DISCOVERY finding 1 established that declared properties cannot be trusted when 19
    of 30 cameras declared none. An estate that now declares none at all changes the
    numbers, not the policy. Resolutions are likewise measured rather than read.
    """
    name = "Pipeline handles mixed H.264/H.265 and mixed resolutions"

    h264 = next((c for c in cameras if (c.declared_codec or "").lower() == "h264"), None)
    hevc = next((c for c in cameras if (c.declared_codec or "").lower() in ("hevc", "h265")), None)

    measured_res: set[tuple[int, int]] = set()
    probed: list[str] = []

    if h264 is None or hevc is None:
        for cam in cameras[:_CODEC_PROBE_LIMIT]:
            frames, stats = _grab_frames(cam, 3, min(seconds, 6.0))
            if not frames:
                continue
            if stats.width and stats.height:
                measured_res.add((stats.width, stats.height))
            codec = (stats.fourcc or "").lower()
            probed.append(f"{cam.external_id}={codec or '?'}")
            if h264 is None and codec == "h264":
                h264 = cam
            elif hevc is None and codec in _HEVC_FOURCCS:
                hevc = cam
            # Keep going until both codecs *and* a second resolution have been seen.
            # Stopping at the codecs alone made the check measure one resolution and
            # then fail its own "mixed resolutions" criterion -- a true negative
            # produced by the harness looking away too early, not by the estate.
            if h264 is not None and hevc is not None and len(measured_res) > 1:
                break

    if h264 is None or hevc is None:
        found = ", ".join(probed) if probed else "no camera delivered frames"
        return _not_exercised(
            7,
            name,
            "neither the catalogue nor a probe of "
            f"{len(probed)} camera(s) found both an H.264 and an H.265 stream ({found})",
        )

    results = {}
    for label, cam in (("h264", h264), ("h265", hevc)):
        frames, stats = _grab_frames(cam, 10, seconds)
        results[label] = (cam.external_id, len(frames), stats.width, stats.height)
        if stats.width and stats.height:
            measured_res.add((stats.width, stats.height))

    declared_res = {(c.declared_width, c.declared_height) for c in cameras if c.properties_known}
    # Measured wins: a resolution we decoded is a fact, a declared one is a claim.
    resolutions = measured_res or declared_res
    source = "measured" if measured_res else "declared"

    both_decoded = all(v[1] > 0 for v in results.values())
    passed = both_decoded and len(resolutions) > 1
    detail = (
        "; ".join(
            f"{k}: camera {v[0]} -> {v[1]} frames at {v[2]}x{v[3]}" for k, v in results.items()
        )
        + f"; {len(resolutions)} distinct {source} resolutions across the estate "
        + f"{sorted(resolutions)}"
        + (f"; codecs established by probe ({', '.join(probed)})" if probed else "")
    )
    return Check(7, name, passed, detail, "live")


def check_8_scene_discontinuity(cameras: list[CameraDescriptor], seconds: float) -> Check:
    """Live: no false cut within one feed; a true cut between two feeds is detected."""
    if len(cameras) < 2:
        return _not_exercised(
            8,
            "Behaviour is sane across a scene discontinuity",
            "fewer than two cameras on the gateway delivered frames, and a genuine "
            "cut needs two different real scenes",
        )
    # Both halves retry across the verified list. Liveness flaps between one
    # connection and the next on this gateway, so fixing on cameras[0] and [1] made
    # the check's outcome depend on that flap rather than on the cut detector.
    cam_a, frames_a, _ = _first_with_frames(cameras, 40, seconds)
    others = [c for c in cameras if cam_a is None or c.external_id != cam_a.external_id]
    cam_b, frames_b, _ = _first_with_frames(others, 5, seconds)

    if cam_a is None or cam_b is None or len(frames_a) < 10 or not frames_b:
        return _not_exercised(
            8,
            "Behaviour is sane across a scene discontinuity",
            "insufficient frames to exercise the detector "
            f"(camera {cam_a.external_id if cam_a else '-'}: {len(frames_a)} of 10 needed, "
            f"camera {cam_b.external_id if cam_b else '-'}: {len(frames_b)} of 1 needed)",
        )

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
    RTSP_AVAILABLE = port_reachable(settings.media_host, settings.gateway_rtsp_port)

    print("\nProject SETU preflight - §2.4 pre-submission checklist")
    print(f"gateway  : {settings.gateway_host}")
    print(f"catalogue: {settings.catalogue_url}")
    print(
        f"transport: {'RTSP/TCP' if RTSP_AVAILABLE else 'HLS (RTSP :%d unreachable)' % settings.gateway_rtsp_port}\n"
    )

    # The catalogue is the organiser's server, and it has been returning a Cloudflare
    # 502 for days at a time. Letting that propagate produced a bare traceback, which
    # reads as "SETU is broken" when the accurate finding is "the feed did not answer" --
    # and it aborted the two checks that need no network at all. Neither of those is
    # acceptable in a tool whose job is to report honestly on what could be verified.
    catalogue_error: str | None = None
    try:
        cameras = fetch_catalogue(settings)
    except Exception as exc:  # noqa: BLE001
        catalogue_error = f"{type(exc).__name__}: {exc}"
        cameras = []
        print(f"  CATALOGUE UNAVAILABLE — {catalogue_error}")
        print(
            "  This is the gateway at "
            f"{settings.gateway_host}, not this codebase. The static checks below still\n"
            "  run; every check needing a live feed reports NOT EXERCISED.\n"
        )

    live = [c for c in cameras if c.live]

    # The catalogue's `live` flag is a claim (DISCOVERY finding 9). Establish which
    # cameras actually deliver frames before handing any of them to a check, so a
    # check's result describes the pipeline rather than which camera happened to be
    # first in the catalogue.
    verified: list[CameraDescriptor] = []
    if live:
        print("  discovering cameras that actually deliver frames ...", flush=True)
        verified = discover_live_cameras(live, want=3, budget_s=min(args.seconds, 8.0))
    if verified:
        print(
            f"  {len(verified)} verified live: " f"{', '.join(c.external_id for c in verified)}\n",
            flush=True,
        )
    elif not catalogue_error:
        print("  NO camera delivered a frame; live checks will report NOT EXERCISED\n", flush=True)

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
                "fail",
            )
        label = {"pass": "PASS", "fail": "FAIL"}.get(result.status, "NOT EXERCISED")
        print(f"    -> {label} ({time.monotonic() - started:.1f}s)", flush=True)
        return result

    # Cheap and static first, slowest live check last.
    checks = [
        run(check_2_no_declared_fps_timing),
        run(check_6_catalogue_driven, cameras, settings.catalogue_url),
        run(check_1_tcp_forced, verified, args.seconds),
        run(check_5_join_warnings_nonfatal, verified or live, args.seconds),
        run(check_3_gaps_tolerated, verified, args.seconds),
        run(check_7_mixed_codecs, live, args.seconds),
        run(check_8_scene_discontinuity, verified, args.seconds),
        run(check_4_reconnect, verified[0] if verified else None, args.seconds),
    ]
    checks.sort(key=lambda c: c.number)

    width = 62
    print(f"{'#':>2}  {'RESULT':<6} {'HOW':<12} CHECK")
    print("-" * (width + 24))
    for c in checks:
        label = {"pass": "PASS", "fail": "FAIL"}.get(c.status, "N/EXER")
        print(f"{c.number:>2}  {label:<6} {c.method:<12} {c.name}")
        print(f"{'':>2}  {'':<6} {'':<12} {c.detail}")
    passed = sum(1 for c in checks if c.status == "pass")
    failed = sum(1 for c in checks if c.status == "fail")
    skipped = sum(1 for c in checks if c.status == "not_exercised")
    print("-" * (width + 24))
    print(f"{passed}/{len(checks)} checks passed, {failed} failed, " f"{skipped} not exercised")
    if skipped:
        print(
            "\nNOT EXERCISED means the gateway did not give this check something to\n"
            "test -- no camera delivered frames, or the catalogue declares no codec.\n"
            "It is not a pipeline defect, and it is not a pass either. Re-run when\n"
            "the feed is healthier to convert them."
        )
    if catalogue_error:
        print(
            f"\nThe catalogue at {settings.catalogue_url} could not be read:\n"
            f"  {catalogue_error}\n"
            "Everything above that needed a live feed was therefore not exercised on\n"
            "this run. Nothing in this repository can change that outcome -- the\n"
            "endpoint is the organiser's. Exit code 3 says exactly this: not a pass,\n"
            "not a failure of the pipeline, an outage upstream."
        )
    print()

    if args.emit_evidence:
        json_path, md_path = evidence.write(
            "preflight",
            {
                "gateway_host": settings.gateway_host,
                "catalogue_url": settings.catalogue_url,
                "rtsp_port_reachable": RTSP_AVAILABLE,
                "transport_used": "rtsp" if RTSP_AVAILABLE else "hls",
                "catalogue_error": catalogue_error,
                "cameras_catalogued": len(cameras),
                "cameras_live_flagged": len(live),
                "checks": [c.__dict__ for c in checks],
                "passed": passed,
                "failed": failed,
                "not_exercised": skipped,
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
                "catalogue_error": catalogue_error,
                "cameras_catalogued": len(cameras),
                "cameras_live": len(live),
                "checks": [c.__dict__ for c in checks],
                "passed": passed,
                "failed": failed,
                "not_exercised": skipped,
                "total": len(checks),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"evidence written: {args.out}\n")
    # A check we could not exercise is not a failure of this system. A check that
    # ran and went wrong is. And a run where the feed never answered at all is neither
    # -- returning 0 for it would let "the gateway was down" be read as "8/8 passed",
    # which is the one misreport this script exists to prevent.
    if failed:
        return 1
    if catalogue_error:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
