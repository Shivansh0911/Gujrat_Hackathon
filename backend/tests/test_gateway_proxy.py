"""The HLS proxy: what it rewrites, and what it refuses.

No network. The CI comment on the test job is explicit that unit tests must not touch
the gateway, and everything worth pinning here is decidable without it: which upstream
URL is built, what a playlist turns into, and which requests are refused.

The refusals matter most. This endpoint fetches from a third-party host on behalf of an
unauthenticated caller, which is the shape of an open proxy, and the only things
standing between it and being one are the pattern checks below.
"""

from __future__ import annotations

import re

import pytest

from services.api import gateway_proxy as P
from services.common.config import Settings

SECRET = "unit-test-secret-key-that-is-long-enough"

FEED = Settings(
    _env_file=None,
    gateway_host="cctv.example.test",
    gateway_media_host="10.0.0.1",
    catalogue_path="/cameras.json",
    hls_path_template="/{id}/index.m3u8",
)

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:8
#EXT-X-KEY:METHOD=AES-128,URI="/enc.key",IV=0x00
#EXTINF:7.92,
seg00000.ts
#EXTINF:6.00,
seg00001.ts
#EXT-X-ENDLIST
"""


def test_upstream_urls_are_built_from_configuration():
    """The one thing a caller influences is a camera reference, never a host."""
    assert P._upstream_url(FEED, "cam01", "index.m3u8") == (
        "https://cctv.example.test/cam01/index.m3u8"
    )
    # Segments are siblings of the playlist...
    assert P._upstream_url(FEED, "cam01", "seg00007.ts") == (
        "https://cctv.example.test/cam01/seg00007.ts"
    )
    # ...and the key sits at the site root on this estate, not under the camera.
    assert P._upstream_url(FEED, "cam01", "enc.key") == "https://cctv.example.test/enc.key"


@pytest.mark.parametrize(
    "token",
    [
        "cam01__../../etc/passwd",
        "cam01__index.m3u8/../secret",
        "../../__index.m3u8",
        "cam01__index.exe",
        "cam01__index",
        "noseparator.m3u8",
        "cam01__seg.ts.extra",
        # A reference long enough to be something other than a camera name.
        ("x" * 40) + "__index.m3u8",
    ],
)
def test_a_token_that_could_climb_out_of_the_path_is_refused(token):
    """Traversal is impossible because neither half may contain a separator.

    Checked as its own test rather than trusted to the regexes being obviously right:
    this is the boundary between a media proxy and an open one.
    """
    with pytest.raises(Exception) as excinfo:
        P._split(token)
    assert getattr(excinfo.value, "status_code", None) == 404


def test_a_valid_token_splits_into_a_reference_and_a_filename():
    assert P._split("cam01__index.m3u8") == ("cam01", "index.m3u8")
    assert P._split("REPLAY-01__seg00012.ts") == ("REPLAY-01", "seg00012.ts")


def test_every_segment_and_the_key_are_rewritten_to_this_proxy():
    """A playlist left untouched is useless to the browser.

    Its segment lines are relative and its key URI points at a protected origin the
    browser has no session for -- which is the whole reason this module exists.
    """
    out = P._rewrite_playlist(PLAYLIST, "cam01", SECRET)

    media_lines = [ln for ln in out.splitlines() if ln and not ln.startswith("#")]
    assert len(media_lines) == 2
    assert all(ln.startswith("/media/gateway/cam01__seg") for ln in media_lines)
    assert all("sig=" in ln and "exp=" in ln for ln in media_lines)

    key_line = next(ln for ln in out.splitlines() if ln.startswith("#EXT-X-KEY"))
    uri = re.search(r'URI="([^"]+)"', key_line).group(1)
    assert uri.startswith("/media/gateway/cam01__enc.key?")
    # The rest of the tag must survive: dropping METHOD or IV breaks decryption.
    assert "METHOD=AES-128" in key_line and "IV=0x00" in key_line


def test_playlist_metadata_is_left_alone():
    out = P._rewrite_playlist(PLAYLIST, "cam01", SECRET)
    for tag in ("#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:8", "#EXT-X-ENDLIST"):
        assert tag in out
    # Durations must survive or the player cannot schedule anything.
    assert "#EXTINF:7.92," in out


def test_an_unexpected_playlist_entry_is_dropped_rather_than_proxied():
    """A line naming something that is not a media file is not passed through.

    Whatever produced it, forwarding it would mean signing a proxy URL for a name this
    module has not validated.
    """
    weird = "#EXTM3U\n#EXTINF:1,\nhttps://evil.example/payload.bin\n"
    out = P._rewrite_playlist(weird, "cam01", SECRET)
    assert "evil.example" not in out
    assert not [ln for ln in out.splitlines() if ln and not ln.startswith("#")]


def test_a_signed_url_verifies_and_a_tampered_one_does_not():
    from services.api.media_signing import verify_media_name

    url = P.signed_proxy_url("cam01", "index.m3u8", SECRET)
    token = url.split("/media/gateway/")[1].split("?")[0]
    exp = int(re.search(r"exp=(\d+)", url).group(1))
    sig = re.search(r"sig=([0-9a-f]+)", url).group(1)

    assert verify_media_name(token, exp, sig, SECRET)
    # A different camera cannot ride the same signature.
    assert not verify_media_name("cam02__index.m3u8", exp, sig, SECRET)
    # Nor can a different secret produce it.
    assert not verify_media_name(token, exp, sig, "another-secret-entirely-x")
