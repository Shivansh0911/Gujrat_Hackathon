import pytest
from pydantic import ValidationError

from services.common.config import Settings


def _s(**kw):
    return Settings(gateway_host="example.test", **kw)


def test_gateway_host_is_required():
    # No source default: an unset host must fail loudly rather than silently
    # pointing at the wrong estate when Phase 2 adds a second gateway.
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_gateway_host_rejects_a_full_url():
    with pytest.raises(ValidationError):
        Settings(gateway_host="https://example.test/api")


def test_derived_endpoints_use_configured_host_and_ports():
    s = _s(gateway_rtsp_port=8554, gateway_whep_port=8889)
    assert s.catalogue_url == "https://example.test/api/ingest"
    assert s.rtsp_url("17") == "rtsp://example.test:8554/stream/17"
    assert s.whep_url("17") == "http://example.test:8889/stream/17/whep"
    assert s.hls_url("17") == "https://example.test/live/stream/17/index.m3u8"


def test_backoff_envelope_must_be_ordered():
    with pytest.raises(ValidationError):
        _s(backoff_min_s=30.0, backoff_max_s=2.0)


def test_transport_rejects_anything_but_tcp_or_udp():
    with pytest.raises(ValidationError):
        _s(rtsp_transport="quic")
