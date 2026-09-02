"""Transport selection and stream-URL resolution.

§2.2 requires RTSP over TCP, and says: "If port 8554 is blocked on the network, fall
back to the HLS endpoint." On the evaluation network that fallback is not a contingency,
it is the only path -- see docs/DISCOVERY.md, finding 6. `live.corp8.cloud` resolves to
a Cloudflare edge, and Cloudflare proxies 80/443 only, so ports 8554 (RTSP) and 8889
(WHEP) never reach the origin. Verified: TCP connect to both fails while 443 succeeds.

So transport is decided per camera, at runtime, by probing -- never assumed. RTSP is
always tried first, because on the Grand Finale network (or a departmental VMS reached
directly) it will be available and is the lower-latency, lower-overhead path.

## The HLS quirk this module exists to absorb

The gateway gates every HLS request on a `cookieCheck=1` **query parameter**. A cookie
does not satisfy it (a master-playlist request carrying only the cookie still returns
302). FFmpeg takes the variant URI from the master playlist verbatim, which drops the
parameter, so its segment requests hit the redirect and stall until the socket times
out -- which presents as "HLS is broken" rather than as an auth problem.

The fix is to resolve the master ourselves, re-append `cookieCheck=1` to the variant
URI, and hand FFmpeg the *variant* playlist. Segment URIs inside that playlist then
carry the parameter already, and decoding works.

The variant URI also carries a per-client `session` UUID with a live window of only a
few seconds, which is why resolution must be repeated on every reconnect rather than
cached -- see `StreamSource.url()`.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit


from services.common import gateway_auth
from services.common.catalogue import CameraDescriptor
from services.common.config import Settings

log = logging.getLogger(__name__)

Transport = Literal["rtsp", "hls"]

# How long to wait on a bare TCP connect when deciding whether RTSP is reachable.
# Short on purpose: this runs once per camera at startup and a blocked port fails fast
# (RST or silent drop), while the real cost of guessing wrong is a 30s decoder stall.
_PORT_PROBE_TIMEOUT_S = 4.0


class StreamResolutionError(RuntimeError):
    """No usable transport could be resolved for a camera."""


def port_reachable(host: str, port: int, timeout: float = _PORT_PROBE_TIMEOUT_S) -> bool:
    """True if a TCP connection to host:port completes within `timeout`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        log.info("port probe failed %s:%d (%s)", host, port, exc.__class__.__name__)
        return False


def resolve_hls_variant(
    master_url: str, timeout: float = 20.0, settings: Settings | None = None
) -> str:
    """Resolve an HLS master playlist to a directly-openable variant playlist URL.

    Returns the first variant with `cookieCheck=1` re-appended. Raises rather than
    returning the master unchanged, because handing FFmpeg the master is precisely
    the failure mode documented above and would be diagnosed as a decoder problem.
    """
    if "cookieCheck" not in master_url:
        sep = "&" if "?" in master_url else "?"
        master_url = f"{master_url}{sep}cookieCheck=1"

    # Through the shared gateway session: the estate serves playlists behind a login,
    # and an unauthenticated fetch returns the sign-in page with HTTP 200. Without this
    # the resolver reported `not an HLS playlist`, which reads as the feed having
    # changed format rather than as a missing credential.
    # Settings are optional: without them the fetch is unauthenticated, which is
    # right for an open estate and is also what a unit test wants. Reaching for
    # get_settings() here made resolving a URL require a configured gateway host,
    # and that raises on any machine without a .env.
    resp = gateway_auth.get(settings, master_url, timeout)  # TLS always verified
    resp.raise_for_status()
    body = resp.text

    if "#EXTM3U" not in body:
        raise StreamResolutionError(f"not an HLS playlist: {urlsplit(master_url).path}")

    # Test for a media playlist FIRST. Both playlist kinds have non-'#' lines -- in a
    # master they are variant playlists, in a media playlist they are segments -- so
    # collecting URIs before discriminating would return a single 1-second .mp4
    # segment as though it were a stream, and decoding would stop after one second.
    # #EXTINF appears only in a media playlist; #EXT-X-STREAM-INF only in a master.
    if "#EXTINF" in body and "#EXT-X-STREAM-INF" not in body:
        return master_url

    variants = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not variants:
        raise StreamResolutionError("HLS master playlist listed no variants")

    # Highest-bandwidth variant is the first entry in practice; taking [0] keeps this
    # deterministic. Analytics wants the best available detail for plate recognition.
    variant = variants[0]
    absolute = urljoin(master_url.split("?")[0].rsplit("/", 1)[0] + "/", variant)
    sep = "&" if "?" in absolute else "?"
    return f"{absolute}{sep}cookieCheck=1"


@dataclass
class StreamSource:
    """A camera plus the transport chosen for it. `url()` is re-evaluated per connect."""

    external_id: str
    transport: Transport
    rtsp_url: str | None
    hls_master_url: str | None
    #: Carried so `url()` can authenticate without a global settings lookup. Optional
    #: because a source can legitimately be constructed in a test with no configuration.
    settings: Settings | None = None

    def url(self) -> str:
        """The URL to open *now*.

        Re-resolved on every call rather than cached: the HLS variant carries a
        per-client session UUID with a live window of a few seconds, so a cached URL
        would reconnect onto a dead session and look like a flapping camera.
        """
        if self.transport == "rtsp":
            if not self.rtsp_url:
                raise StreamResolutionError(f"camera {self.external_id}: no RTSP URL")
            return self.rtsp_url
        if not self.hls_master_url:
            raise StreamResolutionError(f"camera {self.external_id}: no HLS URL")
        return resolve_hls_variant(self.hls_master_url, settings=self.settings)


def select_transport(
    cam: CameraDescriptor,
    settings: Settings,
    *,
    rtsp_available: bool | None = None,
) -> StreamSource:
    """Choose a transport for one camera, probing RTSP reachability if not told.

    `rtsp_available` is passed in by callers handling many cameras so the port probe
    runs once for the whole estate instead of 30 times against the same host:port.
    """
    if rtsp_available is None:
        rtsp_available = port_reachable(settings.media_host, settings.gateway_rtsp_port)

    transport: Transport = "rtsp" if (rtsp_available and cam.rtsp_url) else "hls"
    if transport == "hls" and not cam.hls_url:
        raise StreamResolutionError(
            f"camera {cam.external_id}: RTSP unreachable and catalogue gave no HLS URL"
        )
    return StreamSource(
        external_id=cam.external_id,
        transport=transport,
        rtsp_url=cam.rtsp_url,
        hls_master_url=cam.hls_url,
        settings=settings,
    )
