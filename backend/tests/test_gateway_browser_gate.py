"""The two things the estate checks before it will serve a playlist.

Both were wrong at once, and together they produced "Live feed unavailable, upstream
returned HTTP 502" on every government tile in the console while RTSP ingest from the
same cameras was working perfectly -- because RTSP and HLS are different planes on
different hosts, and only the HLS one has these gates.

Measured against the live estate on 2026-09-02:

  * `GET /cam01/index.m3u8` with `SETU/1.0 (...)`      -> 403 `browser required`
  * the same request with a browser string             -> 200, real playlist
  * `GET /live/stream/cam01/index.m3u8` (old template) -> 404
  * `GET /cam01/index.m3u8`                            -> 200, 216 KB playlist

Neither can be caught by a unit test talking to the network, so these assert the two
values that were wrong. They are cheap, and they fail loudly if someone "tidies" the
user-agent back to something honest-looking that the estate will refuse.
"""

from __future__ import annotations

from services.common import gateway_auth


def test_the_session_presents_itself_as_a_browser() -> None:
    """A bare library user-agent is answered with `403 browser required`."""
    ua = gateway_auth.session().headers["User-Agent"]
    assert ua.startswith("Mozilla/"), ua


def test_the_session_still_says_who_we_are() -> None:
    """Passing the gate must not cost the operator the ability to identify us."""
    assert "SETU" in gateway_auth.session().headers["User-Agent"]


def test_hls_paths_are_built_at_the_site_root() -> None:
    """The estate's own player uses `${cam.id}/index.m3u8`, relative to the root."""
    from services.common.config import Settings

    settings = Settings(gateway_host="example.invalid", jwt_secret="x", database_url="x")
    assert settings.hls_url("cam01").endswith("/cam01/index.m3u8")
    assert "/live/stream/" not in settings.hls_url("cam01")
