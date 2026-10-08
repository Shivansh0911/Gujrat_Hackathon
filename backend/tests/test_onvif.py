"""ONVIF against a mock device: the SOAP it sends, and what it refuses to trust.

A real camera is not available in CI, so these drive a fake that answers with the XML a
device would. That is enough to test everything this adapter is responsible for, because
the adapter's job is exactly to turn four SOAP responses into registry facts -- the frame
path belongs to `StreamSession` and is contract-tested there.

Two themes run through the file:

* **The request is checked, not just the response.** The password digest, the fresh
  nonce, and the explicit RTSP-over-TCP transport request are all things a device would
  accept silently if we got them wrong, and whose absence would only surface as a
  mysterious 401 or a UDP stream that dies behind a firewall.
* **A declared property is recorded, never believed.** The adapter reports the device's
  claimed frame rate as `declared_fps` and never uses it for timing.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree

import pytest

from services.common.credentials import CameraCredential
from services.ingest.onvif_client import OnvifClient, OnvifError
from services.ingest.onvif_source import (
    MAX_TRUSTED_SKEW_S,
    ONVIF_CLOCK_CONFIDENCE,
    OnvifSource,
    _best_profile,
)

CRED = CameraCredential(username="operator", password="s3cr3t-device-pw")

#: A publicly routable literal, deliberately not a documentation address.
#: 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 are all `is_reserved`, so the
#: SSRF guard refuses them at construction -- correctly, since nothing should be
#: reachable there. Nothing connects to this one either: every `_call` is mocked.
DEVICE_HOST = "8.8.8.8"


def _time_response(when: datetime) -> str:
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>
  <tt:UTCDateTime>
   <tt:Time><tt:Hour>{when.hour}</tt:Hour><tt:Minute>{when.minute}</tt:Minute>
            <tt:Second>{when.second}</tt:Second></tt:Time>
   <tt:Date><tt:Year>{when.year}</tt:Year><tt:Month>{when.month}</tt:Month>
            <tt:Day>{when.day}</tt:Day></tt:Date>
  </tt:UTCDateTime>
 </tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>"""


_PROFILES = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><trt:GetProfilesResponse>
  <trt:Profiles token="sub">
   <tt:Name>Substream</tt:Name>
   <tt:VideoEncoderConfiguration>
    <tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>704</tt:Width><tt:Height>396</tt:Height></tt:Resolution>
    <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl>
   </tt:VideoEncoderConfiguration>
  </trt:Profiles>
  <trt:Profiles token="main">
   <tt:Name>Mainstream</tt:Name>
   <tt:VideoEncoderConfiguration>
    <tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>2560</tt:Width><tt:Height>1440</tt:Height></tt:Resolution>
    <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit></tt:RateControl>
   </tt:VideoEncoderConfiguration>
  </trt:Profiles>
 </trt:GetProfilesResponse></s:Body></s:Envelope>"""


def _stream_response(uri: str) -> str:
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
 <s:Body><trt:GetStreamUriResponse><trt:MediaUri>
  <tt:Uri>{uri}</tt:Uri>
 </trt:MediaUri></trt:GetStreamUriResponse></s:Body></s:Envelope>"""


_FAULT = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Body><s:Fault>
  <s:Code><s:Value>s:Sender</s:Value>
   <s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
  <s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason>
 </s:Fault></s:Body></s:Envelope>"""


class _MockDevice:
    """Answers the four operations, and records every request it was sent."""

    def __init__(
        self,
        *,
        clock_skew_s: float = 0.0,
        stream_uri: str = "rtsp://8.8.8.8:554/Streaming/main",
        fault_on: tuple[str, ...] = (),
        profiles_xml: str = _PROFILES,
    ) -> None:
        self.requests: list[str] = []
        self._skew = clock_skew_s
        self._stream_uri = stream_uri
        self._fault_on = fault_on
        self._profiles_xml = profiles_xml

    def __call__(self, body: str, *, authenticated: bool = True) -> ElementTree.Element:
        self.requests.append(body)
        for name, xml in (
            (
                "GetSystemDateAndTime",
                _time_response(datetime.now(timezone.utc) + timedelta(seconds=self._skew)),
            ),
            ("GetProfiles", self._profiles_xml),
            ("GetStreamUri", _stream_response(self._stream_uri)),
            (
                "GetServiceCapabilities",
                '<?xml version="1.0"?><s:Envelope '
                'xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                '<Capabilities Media="true" PTZ="false"/></s:Body></s:Envelope>',
            ),
        ):
            if name in body:
                if name in self._fault_on:
                    raise OnvifError("device refused the request: ter:NotAuthorized")
                return ElementTree.fromstring(xml)
        raise AssertionError(f"mock device got an unexpected request: {body}")


def _client(device: _MockDevice, credential: CameraCredential | None = CRED) -> OnvifClient:
    client = OnvifClient(f"http://{DEVICE_HOST}/onvif/device_service", credential)
    client._call = device  # type: ignore[method-assign]
    return client


# --------------------------------------------------------------- the SOAP we send


def test_the_password_digest_is_sent_not_the_password() -> None:
    """Base64(SHA1(nonce + created + password)). The password never crosses the wire.

    Some sample code reaches for the plaintext profile instead, which works against most
    devices and puts the password in every packet capture on the network.
    """
    from services.ingest.onvif_client import _security_header

    header = _security_header(CRED)
    assert CRED.password not in header
    assert "PasswordDigest" in header
    assert CRED.username in header, "the username is not the secret"

    nonce_b64 = re.search(r"<Nonce[^>]*>([^<]+)</Nonce>", header)
    created = re.search(r"<Created[^>]*>([^<]+)</Created>", header)
    digest = re.search(r"<Password[^>]*>([^<]+)</Password>", header)
    assert nonce_b64 and created and digest

    expected = base64.b64encode(
        hashlib.sha1(
            base64.b64decode(nonce_b64.group(1))
            + created.group(1).encode()
            + CRED.password.encode()
        ).digest()
    ).decode()
    assert digest.group(1) == expected


def test_each_request_carries_a_fresh_nonce() -> None:
    """A reused nonce makes the digest replayable, and a replayed GetStreamUri hands
    an attacker a stream URL."""
    from services.ingest.onvif_client import _security_header

    nonces = {
        re.search(r"<Nonce[^>]*>([^<]+)</Nonce>", _security_header(CRED)).group(1)  # type: ignore[union-attr]
        for _ in range(8)
    }
    assert len(nonces) == 8


def test_no_security_header_without_a_credential() -> None:
    """GetSystemDateAndTime is unauthenticated by specification."""
    from services.ingest.onvif_client import _security_header

    assert _security_header(None) == ""


def test_rtsp_over_tcp_is_requested_explicitly() -> None:
    """UDP survives a laptop demo and fails behind the evaluation network's NAT."""
    device = _MockDevice()
    _client(device).get_stream_uri("main")
    request = next(r for r in device.requests if "GetStreamUri" in r)
    assert "RTSP" in request
    assert "RTP-Unicast" in request


def test_a_username_with_xml_characters_is_escaped() -> None:
    """An unescaped & or < produces malformed SOAP that a device rejects opaquely."""
    from services.ingest.onvif_client import _security_header

    header = _security_header(CameraCredential(username='a&b<c>"d', password="x"))
    assert "&amp;" in header and "&lt;" in header and "&quot;" in header
    ElementTree.fromstring(f"<r xmlns:s='x'>{header.replace('s:', '')}</r>")


# ------------------------------------------------------------- what we read back


def test_profiles_are_parsed_with_their_declared_properties() -> None:
    profiles = _client(_MockDevice()).get_profiles()
    assert {p.token for p in profiles} == {"sub", "main"}
    main = next(p for p in profiles if p.token == "main")
    assert (main.codec, main.width, main.height) == ("H264", 2560, 1440)
    assert main.declared_fps == 25.0


def test_the_highest_resolution_profile_is_chosen() -> None:
    """Not the first, and not the one named 'main'.

    DISCOVERY finding 12 measured the ceiling: 8 plates read at 2560x1440, 2 at
    1280x720, none at 704x396. Choosing a sub-stream buys throughput and reads nothing.
    """
    profiles = _client(_MockDevice()).get_profiles()
    assert _best_profile(profiles).token == "main"


def test_the_device_clock_and_its_offset_are_read() -> None:
    reported = _client(_MockDevice(clock_skew_s=1.0)).get_system_date_and_time()
    assert abs(reported.offset_s - 1.0) < 2.0
    assert reported.device_utc.tzinfo is timezone.utc


def test_a_non_rtsp_stream_uri_is_refused() -> None:
    """A device offering HTTP here is not offering the stream this adapter can read."""
    device = _MockDevice(stream_uri="http://8.8.8.8/video.cgi")
    with pytest.raises(OnvifError, match="non-RTSP"):
        _client(device).get_stream_uri("main")


def test_a_device_with_no_profiles_is_an_error_not_an_empty_list() -> None:
    empty = """<?xml version="1.0"?><s:Envelope
      xmlns:s="http://www.w3.org/2003/05/soap-envelope"
      xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
      <s:Body><trt:GetProfilesResponse/></s:Body></s:Envelope>"""
    with pytest.raises(OnvifError, match="no media profiles"):
        _client(_MockDevice(profiles_xml=empty)).get_profiles()


def test_a_soap_fault_becomes_a_readable_error_without_the_credential() -> None:
    device = _MockDevice(fault_on=("GetProfiles",))
    with pytest.raises(OnvifError) as exc:
        _client(device).get_profiles()
    assert CRED.password not in str(exc.value)


# --------------------------------------------------------------- the adapter


def _source(device: _MockDevice, **kw: Any) -> OnvifSource:
    return OnvifSource(
        f"http://{DEVICE_HOST}/onvif/device_service",
        "ONVIF-01",
        CRED,
        client=_client(device),
        **kw,
    )


def test_the_adapter_resolves_a_stream_url_from_the_device() -> None:
    source = _source(_MockDevice())
    assert source._resolve() == "rtsp://8.8.8.8:554/Streaming/main"
    assert source._profile is not None and source._profile.token == "main"


def test_a_requested_profile_is_honoured() -> None:
    source = _source(_MockDevice(), profile_token="sub")
    source._resolve()
    assert source._profile is not None and source._profile.token == "sub"


def test_an_unknown_profile_names_what_is_available() -> None:
    source = _source(_MockDevice(), profile_token="nope")
    with pytest.raises(OnvifError, match="available: "):
        source._resolve()


def test_a_synchronised_device_gives_better_clock_confidence_than_a_gateway() -> None:
    """This is the point of the adapter beyond federation breadth.

    GatewaySource anchors at its own join time and carries 0.85 to say the mapping is
    an estimate. An ONVIF device reports its clock, so the offset is measured.
    """
    source = _source(_MockDevice(clock_skew_s=0.2))
    source._resolve()
    source._epoch = datetime.now(timezone.utc)
    assert source.clock_confidence == pytest.approx(ONVIF_CLOCK_CONFIDENCE)
    assert source.clock_confidence > 0.85


def test_a_badly_skewed_device_falls_back_to_the_gateway_confidence() -> None:
    """A device that is not NTP-synchronised is a worse reference than our own anchor."""
    source = _source(_MockDevice(clock_skew_s=MAX_TRUSTED_SKEW_S + 30))
    source._resolve()
    source._epoch = datetime.now(timezone.utc)
    assert source.clock_confidence == pytest.approx(0.85)


def test_a_device_that_refuses_its_clock_still_works() -> None:
    """GetSystemDateAndTime is unauthenticated by spec; some devices refuse anyway.

    A camera that will not say the time can still deliver frames, so this degrades the
    confidence rather than failing the adapter.
    """
    source = _source(_MockDevice(fault_on=("GetSystemDateAndTime",)))
    assert source._resolve() == "rtsp://8.8.8.8:554/Streaming/main"
    source._epoch = datetime.now(timezone.utc)
    assert source.clock_confidence == pytest.approx(0.85)


def test_clock_confidence_is_zero_before_any_frame() -> None:
    """Nothing has been observed, so there is no mapping to be confident about."""
    assert _source(_MockDevice()).clock_confidence == 0.0


def test_health_reports_unreachable_when_the_device_answers_nothing() -> None:
    device = _MockDevice(fault_on=("GetServiceCapabilities",))
    report = _source(device).health()
    assert report.reachable is False
    assert report.transport == "rtsp"


def test_health_is_reachable_when_capabilities_answer() -> None:
    report = _source(_MockDevice()).health()
    assert report.reachable is True


def test_the_adapter_declares_rtsp_transport() -> None:
    """Forced TCP everywhere; the adapter does not get to choose otherwise."""
    assert OnvifSource.transport == "rtsp"


# ------------------------------------------------------------------- SSRF guard


def test_a_private_address_is_refused_at_construction() -> None:
    """A camera URL arrives from an operator pasting it into a form."""
    from services.common.ssrf import SsrfBlocked

    with pytest.raises(SsrfBlocked):
        OnvifClient("http://127.0.0.1/onvif/device_service", CRED)


def test_a_non_http_scheme_is_refused() -> None:
    with pytest.raises(Exception):
        OnvifClient("file:///etc/passwd", CRED)


@pytest.mark.parametrize(
    "host",
    ["192.0.2.10", "198.51.100.5", "203.0.113.7"],
    ids=["TEST-NET-1", "TEST-NET-2", "TEST-NET-3"],
)
def test_documentation_ranges_are_refused_too(host: str) -> None:
    """Found by writing these tests: the guard treats reserved ranges as restricted.

    That is right -- nothing should be reachable at a documentation address, and a
    camera URL pointing at one is a misconfiguration or a probe. It is asserted here so
    the behaviour is deliberate rather than incidental, and so the next person who
    reaches for 192.0.2.x in a fixture finds out why it does not work.
    """
    from services.common.ssrf import SsrfBlocked

    with pytest.raises(SsrfBlocked):
        OnvifClient(f"http://{host}/onvif/device_service", CRED)
