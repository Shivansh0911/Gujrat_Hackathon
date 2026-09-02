"""The proxy caches because the estate is slow, and prefetches because it is slow per connection.

Measured against the live estate on 2026-09-02, with no proxy in the path:

  playlist (211 KB)                       25 s
  one 8-second segment (~200 KB)          46 s      -> about 5 KB/s
  six segments fetched in parallel        46 s wall -> 48 s of video, so it keeps up

The throttle is per connection rather than per client, which is the whole reason a
lookahead is worth having: hls.js asks for fragments one at a time and in order, so on
its own it can never see more than the 5 KB/s figure.
"""

from __future__ import annotations

import time

import pytest

from services.api import gateway_proxy as gp


@pytest.fixture(autouse=True)
def _clear_caches():  # type: ignore[no-untyped-def]
    with gp._cache_lock:
        gp._playlists.clear()
        gp._segments.clear()
        gp._in_flight.clear()
    yield


def test_a_segment_round_trips_through_the_cache() -> None:
    gp._cache_put("cam01__seg00001.ts", b"payload")
    assert gp._cache_get("cam01__seg00001.ts") == b"payload"


def test_the_cache_stays_inside_its_budget() -> None:
    """It shares 512 MB with the API and two ONNX models, so it must evict."""
    blob = b"x" * (1024 * 1024)
    for i in range(40):
        gp._cache_put(f"cam01__seg{i:05d}.ts", blob)
    assert gp._segments_bytes <= gp._SEG_CACHE_MAX_BYTES
    # The most recent survives and the oldest is gone.
    assert gp._cache_get("cam01__seg00039.ts") is not None
    assert gp._cache_get("cam01__seg00000.ts") is None


def test_a_cached_playlist_is_served_without_asking_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six tiles opening at once used to mean six identical 25-second waits."""
    calls: list[str] = []

    class Resp:
        ok = True
        status_code = 200
        headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        text = "#EXTM3U\n#EXTINF:8,\nseg00000.ts\n#EXT-X-ENDLIST\n"
        content = text.encode()

    def fake_get(settings, url, timeout=0):  # type: ignore[no-untyped-def]
        calls.append(url)
        return Resp()

    monkeypatch.setattr(gp.gateway_auth, "get", fake_get)
    monkeypatch.setattr(gp, "_prefetch", lambda *a, **k: None)

    secret = "unit-test-secret"

    class ApiS:
        jwt_secret = secret

    monkeypatch.setattr(gp, "get_api_settings", lambda: ApiS())
    monkeypatch.setattr(gp, "get_feed_settings", lambda: None)
    monkeypatch.setattr(gp, "_upstream_url", lambda feed, ref, name: f"https://estate/{ref}/{name}")
    monkeypatch.setattr(gp, "verify_media_name", lambda *a, **k: True)

    token = f"cam01{gp._SEP}index.m3u8"
    first = gp.gateway_media(token)
    second = gp.gateway_media(token)

    assert first.body == second.body
    assert len(calls) == 1, f"upstream was asked {len(calls)} times, not 1"


def test_an_expired_playlist_is_fetched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached playlist must never outlive the signatures inside it."""
    token = f"cam01{gp._SEP}index.m3u8"
    with gp._cache_lock:
        gp._playlists[token] = (time.monotonic() - gp._PLAYLIST_TTL_S - 1, "#EXTM3U\n")
    assert gp._PLAYLIST_TTL_S < 900, "must stay under the media signature lifetime"


def test_the_lookahead_asks_for_the_following_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """In order, starting at the next one -- that is what hls.js will ask for."""
    asked: list[str] = []

    monkeypatch.setattr(gp, "get_feed_settings", lambda: None)
    monkeypatch.setattr(gp, "_upstream_url", lambda feed, ref, name: name)

    class Resp:
        ok = True
        status_code = 200
        headers = {"Content-Type": "video/mp2t"}
        content = b"seg"

    def fake_get(settings, url, timeout=0):  # type: ignore[no-untyped-def]
        asked.append(url)
        return Resp()

    monkeypatch.setattr(gp.gateway_auth, "get", fake_get)
    monkeypatch.setattr(gp.gateway_auth, "looks_like_login", lambda r: False)

    gp._prefetch("cam01", "seg00007.ts")
    gp._prefetch_pool.shutdown(wait=True)
    gp._prefetch_pool = gp.ThreadPoolExecutor(max_workers=gp._LOOKAHEAD)

    assert sorted(asked) == [f"seg{7 + i:05d}.ts" for i in range(1, gp._LOOKAHEAD + 1)]


def test_a_non_segment_name_is_not_extrapolated() -> None:
    """`index.m3u8` has no successor; inventing `index1.m3u8` would be nonsense."""
    gp._prefetch("cam01", "index.m3u8")
    with gp._cache_lock:
        assert not gp._in_flight


def test_a_request_waits_for_a_prefetch_instead_of_duplicating_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Racing our own lookahead cost 31 seconds on the deployed instance.

    The estate throttles per connection, so fetching the same bytes twice does not
    just waste time -- it spends bandwidth the next segment needed.
    """
    import threading as _t

    fetches: list[str] = []
    release = _t.Event()

    def slow_fetch(settings, url, timeout=0):  # type: ignore[no-untyped-def]
        fetches.append(url)
        release.wait(5)

        class R:
            ok = True
            status_code = 200
            headers = {"Content-Type": "video/mp2t"}
            content = b"prefetched"

        return R()

    monkeypatch.setattr(gp.gateway_auth, "get", slow_fetch)
    monkeypatch.setattr(gp.gateway_auth, "looks_like_login", lambda r: False)
    monkeypatch.setattr(gp, "get_feed_settings", lambda: None)
    monkeypatch.setattr(gp, "_upstream_url", lambda feed, ref, name: name)

    gp._prefetch("cam01", "seg00000.ts")
    token = f"cam01{gp._SEP}seg00001.ts"

    got: list[bytes | None] = []
    waiter = _t.Thread(target=lambda: got.append(gp._await_in_flight(token, 5)))
    waiter.start()
    release.set()
    waiter.join(6)
    gp._prefetch_pool.shutdown(wait=True)
    gp._prefetch_pool = gp.ThreadPoolExecutor(max_workers=gp._LOOKAHEAD)

    assert got == [b"prefetched"], "the waiter should get the prefetched bytes"
    assert fetches.count("seg00001.ts") == 1, "seg00001 was fetched more than once"
