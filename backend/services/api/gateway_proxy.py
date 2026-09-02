"""Serve the gateway's HLS to a browser that has no gateway session.

The problem
-----------
The estate put its media behind a login. Fetching a playlist without a session returns
the sign-in page with **HTTP 200 and `text/html`**, so hls.js reports
`networkError: manifestLoadError` and the console shows "Live feed unavailable" on a
camera that is working perfectly.

A browser cannot solve this on its own. The session cookie belongs to
`cctv.corp8.cloud`, a third-party origin the console cannot set a cookie for, and the
segments are AES-128 encrypted with a key served from that same protected origin. RTSP,
which needs no credential, is not playable in a browser at all.

The API can, because it already holds the access code for ingest. So it fetches the
playlist with its session, rewrites every segment and key reference to point back at
itself, and streams the bytes through. The credential stays server-side, which is where
a shared access code belongs -- shipping it to every browser that opens the Control Room
would hand the estate's key to anyone who opens developer tools.

Why this is not an open proxy
-----------------------------
Nothing here takes a URL. The upstream address is built from configuration and a camera
reference, the reference and filename are both pattern-checked, and traversal is
impossible because neither may contain a separator. A caller can ask for a segment of a
camera; it cannot ask for a host.

Links are HMAC-signed and short-lived, using the same scheme as evidence crops, because
`<video>` cannot carry an Authorization header.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from services.api.config import get_api_settings
from services.api.media_signing import sign_media_name, verify_media_name
from services.common import gateway_auth
from services.common.config import Settings as FeedSettings
from services.common.config import get_settings as get_feed_settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

#: Camera references we will proxy for. Deliberately narrow: this is the only part of
#: the upstream URL a caller influences, so it may not contain a separator, a dot, or
#: anything else that could climb out of the path it is interpolated into.
_REF = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Playlist, segment or key filenames. No slashes, no `..`, one extension.
_FILE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(m3u8|ts|key|m4s|mp4)$")

#: The estate throttles each connection to roughly 5 KB/s. Measured 2026-09-02, direct
#: from a workstation with no proxy in the path: an 8-second segment of ~200 KB took
#: 46 seconds, and the playlist itself took 25. Our own rewriting of that playlist costs
#: 0.3s, so essentially all of this is upstream. The previous 20-second timeout was
#: therefore shorter than a normal response, and turned a slow camera into a dead one.
_UPSTREAM_TIMEOUT_S = 90.0

#: Playlists are VOD recordings -- they carry `#EXT-X-ENDLIST` and do not change -- so
#: the 25-second fetch is worth paying once rather than once per tile. Six tiles opening
#: at once previously meant six independent 25-second waits for identical bytes.
#: Kept below the 900s signature lifetime so a cached playlist never hands a browser
#: URLs that expire while it is still playing them.
_PLAYLIST_TTL_S = 300.0

#: What makes playback possible at all. The throttle is per connection, not per client:
#: six segments fetched in parallel returned 48 seconds of video in 46 seconds of wall
#: clock, where the same six fetched one after another took 277. hls.js asks for
#: fragments strictly in order, one at a time, so on its own it can only ever see the
#: 5 KB/s figure. Fetching the next few in the background while it plays the current one
#: converts a stall into a buffer.
_LOOKAHEAD = 4

#: Bounded because this shares 512 MB with the API and two ONNX models. At ~200 KB a
#: segment this holds roughly two minutes of video per camera.
_SEG_CACHE_MAX_BYTES = 24 * 1024 * 1024

_SEG_NAME = re.compile(r"^(?P<stem>[A-Za-z0-9_-]*?)(?P<num>\d+)\.ts$")

_cache_lock = threading.Lock()
_playlists: dict[str, tuple[float, str]] = {}
_segments: "OrderedDict[str, bytes]" = OrderedDict()
_segments_bytes = 0
_in_flight: set[str] = set()
_prefetch_pool = ThreadPoolExecutor(max_workers=_LOOKAHEAD, thread_name_prefix="hls-prefetch")


def _cache_get(token: str) -> bytes | None:
    with _cache_lock:
        data = _segments.get(token)
        if data is not None:
            _segments.move_to_end(token)
        return data


def _cache_put(token: str, data: bytes) -> None:
    """Store a segment, evicting the least recently used until we are back under budget."""
    global _segments_bytes
    with _cache_lock:
        if token in _segments:
            return
        _segments[token] = data
        _segments_bytes += len(data)
        while _segments_bytes > _SEG_CACHE_MAX_BYTES and _segments:
            _, evicted = _segments.popitem(last=False)
            _segments_bytes -= len(evicted)


def _prefetch(camera_ref: str, filename: str) -> None:
    """Warm the next few segments in the background, in parallel.

    Never raises into the request that scheduled it: a failed prefetch simply means the
    browser waits for that segment the slow way, which is what would have happened
    anyway.
    """
    m = _SEG_NAME.match(filename)
    if m is None:
        return
    stem, num, width = m.group("stem"), int(m.group("num")), len(m.group("num"))
    feed = get_feed_settings()

    def fetch(name: str) -> None:
        token = f"{camera_ref}{_SEP}{name}"
        try:
            resp = gateway_auth.get(
                feed, _upstream_url(feed, camera_ref, name), _UPSTREAM_TIMEOUT_S
            )
            if resp.ok and not gateway_auth.looks_like_login(resp):
                _cache_put(token, resp.content)
        except Exception:  # noqa: BLE001 - a warm cache is an optimisation, never a duty
            log.debug("prefetch failed for %s", token, exc_info=True)
        finally:
            with _cache_lock:
                _in_flight.discard(token)

    for offset in range(1, _LOOKAHEAD + 1):
        name = f"{stem}{num + offset:0{width}d}.ts"
        token = f"{camera_ref}{_SEP}{name}"
        with _cache_lock:
            if token in _segments or token in _in_flight:
                continue
            _in_flight.add(token)
        _prefetch_pool.submit(fetch, name)


#: The token that gets signed. A single opaque name, because the signing helper signs
#: bare filenames -- and a signature over a path would be asserting something about a
#: directory the endpoint then has to re-derive.
_SEP = "__"

#: Segments expire from the origin quickly; a link long enough to open the page and
#: watch is enough, and a shorter life limits what a leaked URL is worth.
_TTL_S = 900


def token_for(camera_ref: str, filename: str) -> str:
    return f"{camera_ref}{_SEP}{filename}"


def signed_proxy_url(camera_ref: str, filename: str, secret: str) -> str:
    name = token_for(camera_ref, filename)
    expires_at, signature = sign_media_name(name, secret, _TTL_S)
    return f"/media/gateway/{name}?exp={expires_at}&sig={signature}"


def _split(token: str) -> tuple[str, str]:
    if _SEP not in token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    ref, _, filename = token.partition(_SEP)
    if not _REF.match(ref) or not _FILE.match(filename):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return ref, filename


def _upstream_url(feed: FeedSettings, camera_ref: str, filename: str) -> str:
    """Where the bytes actually live. Built from configuration, never from input."""
    base = f"{feed.gateway_scheme}://{feed.gateway_host}"
    if filename.endswith(".key"):
        # The key sits at the site root on this estate, not under the camera.
        return f"{base}/{filename}"
    if filename.endswith(".m3u8"):
        return feed.hls_url(camera_ref)
    # Segments are siblings of the playlist.
    playlist_path = urlsplit(feed.hls_url(camera_ref)).path
    directory = playlist_path.rsplit("/", 1)[0]
    return f"{base}{directory}/{filename}"


def _rewrite_playlist(body: str, camera_ref: str, secret: str) -> str:
    """Point every segment and key reference back at this proxy.

    Untouched, the playlist names `seg00000.ts` and `URI="/enc.key"`, which the browser
    resolves against *our* origin and we do not serve -- and even if it resolved them
    upstream, it has no session to fetch them with.
    """
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()

        if stripped.startswith("#EXT-X-KEY"):

            def _sub(m: "re.Match[str]") -> str:
                key_name = m.group(1).rsplit("/", 1)[-1] or "enc.key"
                return f'URI="{signed_proxy_url(camera_ref, key_name, secret)}"'

            out.append(re.sub(r'URI="([^"]+)"', _sub, line))
            continue

        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue

        # A media line. Take the bare name; the upstream builder decides the directory.
        name = stripped.rsplit("/", 1)[-1].split("?", 1)[0]
        if _FILE.match(name):
            out.append(signed_proxy_url(camera_ref, name, secret))
        else:
            log.warning("dropping unexpected playlist entry %r", stripped[:60])
    return "\n".join(out) + "\n"


@router.get("/media/gateway/{token}", include_in_schema=False)
def gateway_media(token: str, exp: int = 0, sig: str = "") -> Response:
    """One playlist, segment or key from the gateway, with our session in front."""
    api_settings = get_api_settings()
    if not verify_media_name(token, exp, sig, api_settings.jwt_secret):
        # 404 rather than 403: an unsigned request should not learn that the camera
        # exists, for the same reason a camera outside an operator's scope 404s.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    camera_ref, filename = _split(token)
    feed = get_feed_settings()
    is_playlist = filename.endswith(".m3u8")

    if is_playlist:
        with _cache_lock:
            hit = _playlists.get(token)
        if hit is not None and time.monotonic() - hit[0] < _PLAYLIST_TTL_S:
            return Response(
                content=hit[1],
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )
    else:
        cached = _cache_get(token)
        if cached is not None:
            # Already warmed by a prefetch, so the browser gets it immediately instead
            # of waiting out the upstream throttle.
            _prefetch(camera_ref, filename)
            return Response(
                content=cached,
                media_type="video/mp2t",
                headers={"Cache-Control": "no-store"},
            )

    url = _upstream_url(feed, camera_ref, filename)

    try:
        upstream = gateway_auth.get(feed, url, timeout=_UPSTREAM_TIMEOUT_S)
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"gateway did not answer: {type(exc).__name__}",
        ) from exc

    if gateway_auth.looks_like_login(upstream):
        # Our own session failed rather than the camera being down, and saying so is
        # the difference between "check the access code" and "check the camera".
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "the gateway returned its sign-in page: SETU_GATEWAY_ACCESS_CODE is "
                "missing or no longer valid on this deployment"
            ),
        )
    if not upstream.ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="gateway error")

    if is_playlist:
        rewritten = _rewrite_playlist(upstream.text, camera_ref, api_settings.jwt_secret)
        with _cache_lock:
            _playlists[token] = (time.monotonic(), rewritten)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    if filename.endswith(".ts"):
        _cache_put(token, upstream.content)
        _prefetch(camera_ref, filename)

    media_type = "application/octet-stream" if filename.endswith(".key") else "video/mp2t"
    return StreamingResponse(
        iter([upstream.content]),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )
