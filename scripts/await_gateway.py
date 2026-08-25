#!/usr/bin/env python
"""Wait for the gateway's media plane to recover, then run the evidence jobs.

The catalogue and the media plane fail independently: on 2026-08-25T16:39Z
`/api/ingest` returned 200 with all 30 cameras flagged `live: true` while every
`/live/stream/<id>/index.m3u8` returned 502. A camera's `live` flag is a claim by the
control plane about the media plane, and this script exists because that claim is not
trustworthy -- readiness means a playlist actually served, nothing less.

Usage:
    python scripts/await_gateway.py [--timeout-min 120] [--then-probe] [--then-preflight]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from services.common import redact  # noqa: E402
from services.common.catalogue import fetch_catalogue  # noqa: E402
from services.common.config import get_settings  # noqa: E402

log = logging.getLogger("await")


def media_plane_ready(settings, sample: int = 3) -> tuple[bool, list[str]]:
    """True when a sample of catalogued cameras actually serve a playlist.

    Sampling rather than checking all 30: this polls repeatedly, and hammering every
    camera each cycle is the load pattern §2.2 tells us not to create.
    """
    try:
        cameras = [c for c in fetch_catalogue(settings) if c.live]
    except requests.RequestException as exc:
        return False, [f"catalogue unreachable: {exc}"]
    if not cameras:
        return False, ["catalogue lists no live cameras"]

    notes: list[str] = []
    ok = 0
    for cam in cameras[:sample]:
        url = settings.hls_url(cam.external_id)
        try:
            # cookieCheck is the gateway's gate; without it every request 302s.
            resp = requests.get(
                url, params={"cookieCheck": "1"}, timeout=15, allow_redirects=True
            )
            notes.append(f"camera {cam.external_id}: HTTP {resp.status_code}")
            if resp.status_code == 200 and "#EXTM3U" in resp.text:
                ok += 1
        except requests.RequestException as exc:
            notes.append(f"camera {cam.external_id}: {type(exc).__name__}")
    return ok > 0, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout-min", type=float, default=120.0)
    ap.add_argument("--interval-s", type=float, default=120.0)
    ap.add_argument("--then-probe", action="store_true")
    ap.add_argument("--then-preflight", action="store_true")
    args = ap.parse_args()

    redact.install(level=logging.INFO)
    settings = get_settings()

    deadline = time.monotonic() + args.timeout_min * 60
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        ready, notes = media_plane_ready(settings)
        log.info("attempt %d: %s | %s", attempt, "READY" if ready else "not ready", "; ".join(notes))
        if ready:
            break
        time.sleep(args.interval_s)
    else:
        log.error("gateway media plane did not recover within %.0f minutes", args.timeout_min)
        return 1

    py = sys.executable
    rc = 0
    if args.then_preflight:
        log.info("running preflight with evidence emission")
        rc |= subprocess.run(
            [py, "-u", "scripts/preflight_check.py", "--seconds", "10", "--emit-evidence"],
            cwd=REPO_ROOT, check=False,
        ).returncode
    if args.then_probe:
        log.info("running 30-camera probe with evidence emission")
        rc |= subprocess.run(
            [py, "-u", "scripts/probe_catalogue.py", "--seconds", "8",
             "--sequential", "--emit-evidence"],
            cwd=REPO_ROOT, check=False,
        ).returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
