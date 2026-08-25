"""Single, mandatory entry point for OpenCV in this codebase.

OpenCV's FFmpeg backend reads OPENCV_FFMPEG_CAPTURE_OPTIONS *once, at import time*.
Setting it after `import cv2` silently has no effect and every capture quietly falls
back to RTSP-over-UDP -- which survives a laptop demo and fails behind the
evaluation network's NAT/firewall, producing corrupt frames that look like model bugs.

Making this module the only place cv2 is imported turns "we force TCP everywhere"
from a convention reviewers must trust into a property of the import graph.
A CI check (scripts/preflight_check.py, check 1) fails the build if any other file
imports cv2 directly.

Usage:
    from services.common.cv_env import cv2, RTSP_TRANSPORT
"""

from __future__ import annotations

import os

# Read straight from the process environment rather than services.common.config:
# config imports pydantic, pydantic could transitively import a module that imports
# cv2, and that race is exactly the bug this module exists to prevent.
RTSP_TRANSPORT: str = os.environ.get("SETU_RTSP_TRANSPORT", "tcp").strip().lower()
if RTSP_TRANSPORT not in ("tcp", "udp"):
    raise ValueError(
        f"SETU_RTSP_TRANSPORT must be 'tcp' or 'udp', got {RTSP_TRANSPORT!r}"
    )

_FFMPEG_LOGLEVEL: str = os.environ.get("SETU_FFMPEG_LOGLEVEL", "16")

# Semicolon-separated key;value pairs, pipe-separated between options -- OpenCV's own
# format, not FFmpeg's. Values are in microseconds.
#
# The set covers both transports because OpenCV applies one option string to every
# capture and the estate is reached over RTSP or HLS depending on the network
# (services/common/transport.py). Options irrelevant to the protocol in use are
# ignored by libav.
#   timeout            bounds a dead RTSP socket, so a black-holed camera surfaces as
#                      a reconnect instead of a hung worker. ('stimeout' is the
#                      removed pre-FFmpeg-6 spelling and is silently ignored.)
#   rw_timeout         the equivalent bound for HTTP/HLS segment reads.
#   reconnect*         let libav retry a dropped HLS segment fetch before we tear the
#                      whole session down and pay a full re-resolve.
_CAPTURE_OPTIONS = "|".join(
    [
        f"rtsp_transport;{RTSP_TRANSPORT}",
        "timeout;15000000",
        "max_delay;500000",
        "reorder_queue_size;0",
        "rw_timeout;20000000",
        "reconnect;1",
        "reconnect_streamed;1",
        "live_start_index;-1",
    ]
)
# live_start_index;-1 starts HLS playback at the newest segment. libav's default (-3)
# starts three segments back, and this gateway serves ~1s low-latency segments with a
# window of only about six -- so on a slow join the default start point can expire
# before it is fetched, producing "Error when loading first segment" and a 30s stall
# on a camera that is perfectly healthy.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _CAPTURE_OPTIONS

# Suppress the expected join-time decoder chatter ("Error constructing the frame RPS",
# "Could not find ref with POC") at the libav level. These are normal when attaching
# mid-stream before the first IDR and are NOT errors; see §2.2. They are surfaced as
# debug-level telemetry by RtspSession instead of as console noise.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", _FFMPEG_LOGLEVEL)

import cv2  # noqa: E402  -- import order is the entire point of this module

__all__ = ["cv2", "RTSP_TRANSPORT", "capture_options"]


def capture_options() -> str:
    """The option string actually handed to FFmpeg. Logged at startup as evidence."""
    return _CAPTURE_OPTIONS
