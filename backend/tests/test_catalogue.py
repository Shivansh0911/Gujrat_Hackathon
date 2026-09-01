import pytest

from services.common.catalogue import parse_catalogue
from services.common.config import Settings

SETTINGS = Settings(gateway_host="live.example.test")

# Shaped on the real 2026-08-25 response: most cameras report empty codec and zeros.
PAYLOAD = {
    "cameras": [
        {
            "id": "6",
            "name": "Camera 6",
            "location": "06 Timbavadi gate-Junagadh",
            "codec": "hevc",
            "live": True,
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "bitrate_kbps": 1923,
            "rtsp_url": "rtsp://live.example.test:8554/stream/6",
            "webrtc_url": "http://live.example.test:8889/stream/6/whep",
            "hls_live_url": "/live/stream/6/index.m3u8",
        },
        {
            "id": "2",
            "name": "Camera 2",
            "location": "02 Janpath",
            "codec": "",
            "live": True,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "bitrate_kbps": 0,
            "rtsp_url": "rtsp://live.example.test:8554/stream/2",
            "webrtc_url": "http://live.example.test:8889/stream/2/whep",
            "hls_live_url": "/live/stream/2/index.m3u8",
        },
    ]
}


def test_zero_sentinels_become_none_not_real_values():
    # A width of 0 is "unknown", not a measurement. Propagating 0 would let a
    # downstream batcher size a tensor to nothing and fail far from the cause.
    cams = {c.external_id: c for c in parse_catalogue(PAYLOAD, SETTINGS)}
    unknown = cams["2"]
    assert unknown.declared_codec is None
    assert unknown.declared_width is None and unknown.declared_height is None
    assert unknown.declared_fps is None
    assert unknown.properties_known is False


def test_declared_properties_survive_when_present():
    cams = {c.external_id: c for c in parse_catalogue(PAYLOAD, SETTINGS)}
    known = cams["6"]
    assert known.declared_codec == "hevc"
    assert (known.declared_width, known.declared_height) == (1920, 1080)
    assert known.declared_fps == 25.0
    assert known.properties_known is True


def test_relative_hls_path_is_resolved_against_the_configured_gateway():
    cams = {c.external_id: c for c in parse_catalogue(PAYLOAD, SETTINGS)}
    assert cams["6"].hls_url == "https://live.example.test/live/stream/6/index.m3u8"


def test_malformed_rows_are_skipped_not_fatal():
    # Only two things make a row unusable now: not being an object, and having no id.
    # A row carrying an id and nothing else is ordinary -- the current catalogue is
    # made entirely of those -- and its URLs are derived from configuration.
    payload = {"cameras": ["garbage", {"rtsp_url": "rtsp://x/1"}] + PAYLOAD["cameras"]}
    cams = parse_catalogue(payload, SETTINGS)
    # A single bad row must not cost us the other 29 cameras during a re-poll.
    assert {c.external_id for c in cams} == {"6", "2"}


def test_a_row_with_only_an_id_gets_urls_from_configuration():
    """The shape the estate publishes today: an id, a name, and nothing else."""
    cams = parse_catalogue([{"id": "9", "name": "Nine"}], SETTINGS)
    assert len(cams) == 1
    cam = cams[0]
    assert cam.rtsp_url == SETTINGS.rtsp_url("9")
    assert cam.hls_url == SETTINGS.hls_url("9")
    # Nothing was declared, so nothing may be trusted: the camera must be probed.
    assert cam.properties_known is False
    # And silence about liveness is a candidate to probe, not a camera to discard.
    assert cam.live is True


def test_a_bare_list_payload_is_accepted():
    """`/cameras.json` returns a list; `/api/ingest` returned {"cameras": [...]}."""
    assert len(parse_catalogue([{"id": "1"}, {"id": "2"}], SETTINGS)) == 2


def test_missing_cameras_key_is_an_error():
    with pytest.raises(ValueError):
        parse_catalogue({"data": []}, SETTINGS)
