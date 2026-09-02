import pytest
import requests

from services.common.catalogue import CameraDescriptor
from services.common.config import Settings
from services.common import transport as T

SETTINGS = Settings(gateway_host="live.example.test")

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:10\n"
    "#EXT-X-INDEPENDENT-SEGMENTS\n"
    "\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=2038800,CODECS="hvc1.1.6.L123.b0",RESOLUTION=1920x1080\n'
    "video1_stream.m3u8?session=3d006a25-408d-4bfe-b91e-7e61aa28a344\n"
)


def _cam(**kw) -> CameraDescriptor:
    base = dict(
        external_id="6",
        name="Camera 6",
        location_text="06 Timbavadi",
        live=True,
        rtsp_url="rtsp://live.example.test:8554/stream/6",
        whep_url=None,
        hls_url="https://live.example.test/live/stream/6/index.m3u8",
        declared_codec="hevc",
        declared_width=1920,
        declared_height=1080,
        declared_fps=25.0,
        declared_bitrate_kbps=1923,
    )
    base.update(kw)
    return CameraDescriptor(**base)


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_variant_is_resolved_absolute_and_keeps_cookie_check(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return _Resp(MASTER)

    monkeypatch.setattr(T.gateway_auth, "get", lambda settings, url, timeout=None: fake_get(url))
    out = T.resolve_hls_variant("https://live.example.test/live/stream/6/index.m3u8")

    # The master must itself be requested with cookieCheck, or the gateway 302s.
    assert seen["url"].endswith("?cookieCheck=1")
    # And the variant must carry it too, or FFmpeg's segment fetches stall on the
    # redirect until the socket times out -- the bug this function exists to prevent.
    assert out == (
        "https://live.example.test/live/stream/6/video1_stream.m3u8"
        "?session=3d006a25-408d-4bfe-b91e-7e61aa28a344&cookieCheck=1"
    )


def test_media_playlist_served_directly_is_returned_as_is(monkeypatch):
    media = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.04,\nseg1.mp4\n"
    monkeypatch.setattr(T.gateway_auth, "get", lambda settings, url, timeout=None: _Resp(media))
    out = T.resolve_hls_variant("https://h/live/stream/6/index.m3u8?cookieCheck=1")
    assert out.endswith("index.m3u8?cookieCheck=1")


def test_non_playlist_response_raises(monkeypatch):
    monkeypatch.setattr(
        T.gateway_auth,
        "get",
        lambda settings, url, timeout=None: _Resp('{"detail":"Not Found"}'),
    )
    with pytest.raises(T.StreamResolutionError):
        T.resolve_hls_variant("https://h/live/stream/6/index.m3u8")


def test_rtsp_selected_when_port_is_reachable():
    src = T.select_transport(_cam(), SETTINGS, rtsp_available=True)
    assert src.transport == "rtsp"
    assert src.url() == "rtsp://live.example.test:8554/stream/6"


def test_falls_back_to_hls_when_rtsp_port_is_blocked(monkeypatch):
    # The evaluation network resolves the gateway to a Cloudflare edge, which does not
    # proxy 8554 -- so this is the normal path, not an edge case. See DISCOVERY.md.
    monkeypatch.setattr(T.gateway_auth, "get", lambda settings, url, timeout=None: _Resp(MASTER))
    src = T.select_transport(_cam(), SETTINGS, rtsp_available=False)
    assert src.transport == "hls"
    assert "cookieCheck=1" in src.url()


def test_no_transport_available_raises_rather_than_returning_a_dead_source():
    with pytest.raises(T.StreamResolutionError):
        T.select_transport(_cam(hls_url=None), SETTINGS, rtsp_available=False)


def test_port_probe_reports_false_for_a_closed_port():
    # Port 1 on localhost is reliably closed; asserts the probe fails closed rather
    # than raising, so one unreachable camera cannot abort estate-wide discovery.
    assert T.port_reachable("127.0.0.1", 1, timeout=1.0) is False


def test_resolving_a_playlist_does_not_require_a_configured_gateway(monkeypatch):
    """URL resolution must work on a machine with no gateway configuration.

    `SETU_GATEWAY_HOST` has no default on purpose, so `get_settings()` raises wherever
    there is no .env -- CI, for one. A version of `resolve_hls_variant` that reached for
    settings to find an access code therefore turned "resolve this URL" into "require a
    fully configured estate", and every transport test failed in CI while passing on
    every developer machine, because developers have a .env.

    Asserting the absence of that call rather than the success of the function: the
    function succeeds either way here, which is exactly why the original defect was
    invisible locally.
    """
    import services.common.config as config_module

    def _explode():
        raise AssertionError("resolve_hls_variant must not need the settings object")

    monkeypatch.setattr(config_module, "get_settings", _explode)
    monkeypatch.setattr(T.gateway_auth, "get", lambda settings, url, timeout=None: _Resp(MASTER))

    out = T.resolve_hls_variant("https://h/live/stream/6/index.m3u8")
    assert out.endswith("cookieCheck=1")
