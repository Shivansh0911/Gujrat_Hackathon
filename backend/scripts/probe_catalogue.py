#!/usr/bin/env python
"""Probe every catalogued camera and reconcile declared properties against measured.

Why this exists: the live catalogue reports `codec: ""`, `0x0` and `fps: 0.0` for the
majority of cameras, and where it does report an FPS that figure is a declaration, not
a measurement. §2.2 forbids using declared FPS for timing, so the registry needs a
`measured_fps` obtained the only legitimate way -- from PTS deltas on real frames.

The output table is also submission evidence: it demonstrates the declared-vs-measured
discrepancy the organiser's own integration guide warns about, on their own feed.

Usage:
    python scripts/probe_catalogue.py [--seconds 8] [--camera 1 --camera 17]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# backend/ on the path so `services.*` imports resolve however this is launched.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import PROJECT_ROOT as REPO_ROOT  # noqa: E402

from services.common import evidence, redact  # noqa: E402
from services.common.catalogue import CameraDescriptor, fetch_catalogue  # noqa: E402
from services.common.config import get_settings  # noqa: E402
from services.common.stream_client import StreamSession  # noqa: E402
from services.common.transport import port_reachable, select_transport  # noqa: E402

log = logging.getLogger("probe")


@contextlib.contextmanager
def capture_stderr_fd():
    """Capture writes to file descriptor 2, including those from libav's C code.

    FFmpeg's decoder messages ("Error constructing the frame RPS", "Could not find ref
    with POC") are emitted from C to fd 2 and are invisible to `contextlib.redirect_stderr`,
    which only rebinds the Python-level `sys.stderr` object. Recording them is the whole
    point of the evidence run: §2.2 says these messages are expected at join and must not
    be fatal, and the only way to show a jury we handled them is to show what arrived.

    fd-level redirection is process-wide, so this is only correct under --sequential.
    In concurrent mode messages cannot be attributed to a camera and are not collected.
    """
    saved = os.dup(2)
    tmp = tempfile.TemporaryFile(mode="w+b")
    try:
        sys.stderr.flush()
        os.dup2(tmp.fileno(), 2)
        yield tmp
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)


def _read_captured(tmp) -> list[str]:
    tmp.seek(0)
    raw = tmp.read().decode("utf-8", errors="replace")
    tmp.close()
    # Keep every line: a jury asking "what did the decoder actually say?" is entitled
    # to the unfiltered answer, and redaction still runs over it before it is written.
    return [ln.rstrip() for ln in raw.splitlines() if ln.strip()]


@dataclass
class ProbeResult:
    external_id: str
    location_text: str
    transport: str
    ok: bool
    error: str | None
    frames: int
    declared_codec: str | None
    measured_fourcc: str | None
    declared_res: str | None
    measured_res: str | None
    declared_fps: float | None
    measured_fps: float | None
    join_latency_s: float | None
    max_gap_ms: float
    join_warnings: int
    discontinuities: int
    # Populated only under --sequential; see capture_stderr_fd().
    decoder_messages: list[str] = field(default_factory=list)

    @property
    def fps_disagrees(self) -> bool:
        """True when the catalogue's FPS is absent or materially wrong."""
        if self.measured_fps is None:
            return False
        if self.declared_fps is None:
            return True
        return abs(self.declared_fps - self.measured_fps) / self.declared_fps > 0.15


def probe_one(cam: CameraDescriptor, seconds: float, rtsp_available: bool) -> ProbeResult:
    """Hold a capture open for `seconds` and measure what actually arrives."""
    declared_res = (
        f"{cam.declared_width}x{cam.declared_height}"
        if cam.declared_width and cam.declared_height
        else None
    )
    source = select_transport(cam, get_settings(), rtsp_available=rtsp_available)
    session = StreamSession(
        source.url,  # callable: HLS variant URLs expire and must be re-resolved
        cam.external_id,
        transport=source.transport,
        join_timeout_s=get_settings().join_timeout_s,
        # A single short probe should not sit in a reconnect loop; one attempt is
        # enough to characterise the stream and we must not hold gateway capacity.
        backoff_min_s=1.0,
        backoff_max_s=1.0,
    )
    error: str | None = None
    # A watchdog bounds the probe even if the capture blocks inside read(); without it
    # one unreachable camera would stall a worker for the whole run.
    watchdog = threading.Timer(seconds + 15.0, session.stop)
    watchdog.daemon = True
    watchdog.start()
    try:
        deadline = time.monotonic() + seconds
        for _frame in session.frames():
            if time.monotonic() >= deadline:
                break
        else:  # generator exhausted without us breaking -> stop() or hard failure
            error = "stream ended before probe window elapsed"
    except Exception as exc:  # noqa: BLE001 - probe must characterise, never crash the run
        # Deliberately broad and deliberately NOT swallowed: the reason is recorded
        # and reported. §0 forbids `except Exception: pass`, not error reporting.
        error = f"{type(exc).__name__}: {exc}"
    finally:
        watchdog.cancel()
        session.stop()
        session.close()

    st = session.stats
    if st.frames == 0 and error is None:
        error = "no decodable frame within join timeout"

    return ProbeResult(
        external_id=cam.external_id,
        location_text=cam.location_text,
        transport=source.transport,
        ok=st.frames > 0,
        error=error,
        frames=st.frames,
        declared_codec=cam.declared_codec,
        measured_fourcc=st.fourcc,
        declared_res=declared_res,
        measured_res=f"{st.width}x{st.height}" if st.width and st.height else None,
        declared_fps=cam.declared_fps,
        measured_fps=round(st.measured_fps, 2) if st.measured_fps else None,
        join_latency_s=round(st.first_frame_latency_s, 2)
        if st.first_frame_latency_s
        else None,
        max_gap_ms=round(st.max_interframe_gap_ms, 1),
        join_warnings=st.join_decode_warnings,
        discontinuities=st.discontinuities,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=8.0, help="probe window per camera")
    ap.add_argument(
        "--camera", action="append", default=None, help="external id; repeatable"
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "reports" / "probe_catalogue.json",
        help="where to write the machine-readable report",
    )
    ap.add_argument(
        "--sequential",
        action="store_true",
        help="probe one camera at a time so decoder messages can be attributed to it",
    )
    ap.add_argument(
        "--emit-evidence",
        action="store_true",
        help="write a dated, immutable record to reports/evidence/ for submission",
    )
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    redact.install(level=logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()

    log.info("catalogue: %s", settings.catalogue_url)
    cameras = fetch_catalogue(settings)
    if args.camera:
        wanted = set(args.camera)
        cameras = [c for c in cameras if c.external_id in wanted]
    if not cameras:
        log.error("no cameras selected")
        return 2

    # Probe the RTSP port once for the estate rather than 30 times against the same
    # host:port. On the evaluation network this is False and everything uses HLS.
    rtsp_available = port_reachable(settings.gateway_host, settings.gateway_rtsp_port)
    log.info(
        "probing %d cameras, %.0fs each, %d at a time, transport=%s",
        len(cameras),
        args.seconds,
        settings.max_concurrent_captures,
        "rtsp" if rtsp_available else "hls (RTSP port unreachable)",
    )

    results: list[ProbeResult] = []
    if args.sequential:
        # One at a time so fd-level stderr capture can be attributed to a camera.
        # Slower, but this is the evidence run -- accuracy beats wall-clock.
        for cam in cameras:
            with capture_stderr_fd() as cap:
                result = probe_one(cam, args.seconds, rtsp_available)
            result.decoder_messages = [
                redact.redact(m) for m in _read_captured(cap)
            ]
            log.info(
                "camera %s: %d frames, %d decoder message(s)",
                result.external_id,
                result.frames,
                len(result.decoder_messages),
            )
            results.append(result)
    else:
        # Bounded concurrency: each connected client gets its own copy of the stream, so
        # an unbounded fan-out is a load test against infrastructure we do not own (§2.2).
        with ThreadPoolExecutor(max_workers=settings.max_concurrent_captures) as pool:
            futures = {
                pool.submit(probe_one, c, args.seconds, rtsp_available): c
                for c in cameras
            }
            for fut in as_completed(futures):
                results.append(fut.result())

    results.sort(key=lambda r: int(r.external_id) if r.external_id.isdigit() else 0)

    hdr = (
        f"{'id':>3}  {'location':<42} {'via':<5} {'codec':<12} {'resolution':<22} "
        f"{'fps (decl -> measured)':<26} {'join':>6}  {'gap':>7}"
    )
    print("\n" + hdr)
    print("-" * len(hdr))
    for r in results:
        codec = f"{r.declared_codec or '—':<5} / {(r.measured_fourcc or '—'):<4}"
        res = f"{(r.declared_res or '—'):<10} -> {(r.measured_res or '—'):<9}"
        fps = f"{(r.declared_fps if r.declared_fps else '—')!s:<8} -> {(r.measured_fps if r.measured_fps else '—')!s:<8}"
        flag = " *" if r.fps_disagrees else "  "
        status = "" if r.ok else f"  FAIL: {r.error}"
        print(
            f"{r.external_id:>3}  {r.location_text[:42]:<42} {r.transport:<5} "
            f"{codec:<12} {res:<22} "
            f"{fps:<24}{flag} {(r.join_latency_s or 0):>5.2f}s {r.max_gap_ms:>6.0f}ms{status}"
        )

    live = [r for r in results if r.ok]
    disagree = [r for r in live if r.fps_disagrees]
    unknown_declared = [r for r in results if r.declared_codec is None]
    print(
        f"\n{len(live)}/{len(results)} cameras produced frames. "
        f"{len(unknown_declared)} had no declared properties in the catalogue. "
        f"* = declared FPS absent or >15% from measured ({len(disagree)} cameras).\n"
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "catalogue_url": settings.catalogue_url,
                "transport": settings.rtsp_transport,
                "probe_seconds": args.seconds,
                "results": [asdict(r) for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("report written: %s", args.out)

    if args.emit_evidence:
        json_path, md_path = evidence.write(
            "catalogue-probe",
            {
                "catalogue_url": settings.catalogue_url,
                "gateway_host": settings.gateway_host,
                "rtsp_port_reachable": rtsp_available,
                "probe_seconds": args.seconds,
                "sequential": args.sequential,
                "cameras_catalogued": len(cameras),
                "cameras_producing_frames": len(live),
                "cameras_with_declared_properties": len(cameras) - len(unknown_declared),
                "cameras_fps_disagreeing": len(disagree),
                "results": [asdict(r) for r in results],
            },
            _markdown_report(results, settings, rtsp_available, args),
        )
        log.info("evidence written: %s and %s", json_path.name, md_path.name)

    return 0 if live else 1


def _markdown_report(results, settings, rtsp_available: bool, args) -> str:
    """Human-readable companion to the JSON record."""
    live = [r for r in results if r.ok]
    disagree = [r for r in live if r.fps_disagrees]
    prov = evidence.provenance()

    lines = [
        "# Catalogue probe — measured stream properties",
        "",
        f"- **Gateway:** `{settings.gateway_host}`",
        f"- **Catalogue:** `{settings.catalogue_url}`",
        f"- **RTSP :{settings.gateway_rtsp_port} reachable:** "
        f"{'yes' if rtsp_available else 'no — all cameras probed over HLS'}",
        f"- **Sample window:** {args.seconds:.0f}s per camera"
        f"{', sequential' if args.sequential else ', concurrent'}",
        f"- **Commit:** `{prov['git_sha']}`"
        f"{' (working tree dirty)' if prov['git_tree_dirty'] else ''}",
        "",
        "## Why this exists",
        "",
        "The catalogue reports `codec: \"\"`, `0x0` and `fps: 0.0` for most cameras, and",
        "where it does declare an FPS that figure is a declaration, not a measurement.",
        "§2.2 forbids using declared FPS for timing, so every property below marked",
        "*measured* was derived from PTS deltas on real decoded frames.",
        "",
        f"**{len(live)}/{len(results)} cameras produced frames.** "
        f"{len(disagree)} had a declared FPS that was absent or more than 15% from measured.",
        "",
        "| id | location | via | codec | resolution (declared → measured) | fps (declared → measured) | TTFF | decoder msgs |",
        "|---:|---|---|---|---|---|---:|---:|",
    ]
    for r in results:
        codec = f"{r.declared_codec or '—'} / {r.measured_fourcc or '—'}"
        res = f"{r.declared_res or '—'} → {r.measured_res or '—'}"
        fps = f"{r.declared_fps or '—'} → {r.measured_fps or '—'}"
        if r.fps_disagrees:
            fps += " ⚠"
        ttff = f"{r.join_latency_s:.2f}s" if r.join_latency_s else "—"
        status = "" if r.ok else f" **FAIL: {r.error}**"
        lines.append(
            f"| {r.external_id} | {r.location_text}{status} | {r.transport} | {codec} "
            f"| {res} | {fps} | {ttff} | {len(r.decoder_messages)} |"
        )

    noisy = [r for r in results if r.decoder_messages]
    if noisy:
        lines += [
            "",
            "## Decoder messages observed during join",
            "",
            "§2.2: these are expected when attaching mid-stream before the first IDR and",
            "must not be treated as fatal. They are reproduced verbatim (credentials",
            "redacted) as evidence that they occurred and were absorbed.",
            "",
        ]
        for r in noisy:
            lines.append(f"### Camera {r.external_id} — {r.location_text}")
            lines.append("")
            lines.append("```")
            lines.extend(r.decoder_messages[:40])
            if len(r.decoder_messages) > 40:
                lines.append(f"... {len(r.decoder_messages) - 40} further line(s)")
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
