import pytest
from pydantic import ValidationError

from services.common.config import Settings


def _s(**kw):
    # `_env_file=None` so these assert the *defaults*, not whatever the developer's
    # .env happens to hold. Without it these tests silently read local configuration:
    # pointing a working copy at a gateway with a different catalogue path made
    # `test_derived_endpoints_use_configured_host_and_ports` fail on a machine where
    # nothing was wrong. A unit test of defaults must not depend on the environment.
    return Settings(_env_file=None, gateway_host="example.test", **kw)


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
    # The estate serves HLS at the site root; see test_gateway_browser_gate.
    assert s.hls_url("17") == "https://example.test/17/index.m3u8"


def test_backoff_envelope_must_be_ordered():
    with pytest.raises(ValidationError):
        _s(backoff_min_s=30.0, backoff_max_s=2.0)


def test_transport_rejects_anything_but_tcp_or_udp():
    with pytest.raises(ValidationError):
        _s(rtsp_transport="quic")


def test_rtsp_carries_credentials_only_when_the_estate_asks():
    """RTSP was open until 2026-09-03, then began answering `401 Unauthorized`.

    Credentials are therefore optional: an estate that does not want them must not be
    sent a URL containing an empty `:@`, which is not a valid authority.
    """
    assert _s().rtsp_url("cam01") == "rtsp://example.test:8554/stream/cam01"

    url = _s(gateway_email="team@example.ac.in", gateway_access_code="AAAA-BBBB-CCCC").rtsp_url(
        "cam01"
    )
    # The `@` in an address and the dashes in a code both have meaning inside a URL's
    # authority, so they are quoted rather than pasted in.
    assert "team%40example.ac.in:AAAA-BBBB-CCCC@example.test:8554" in url, url
    assert url.endswith("/stream/cam01")


def test_an_access_code_without_an_email_is_not_put_in_the_url():
    """Half a credential is not a credential, and would break the authority section."""
    s = _s(gateway_access_code="AAAA-BBBB-CCCC")
    assert "@" not in s.rtsp_url("cam01").split("//", 1)[1].split("/", 1)[0]
