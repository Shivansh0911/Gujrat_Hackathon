"""A minimal ONVIF client: four calls, no new dependency.

Why hand-rolled
---------------
ONVIF is SOAP over HTTP with a WS-Security UsernameToken, and the platform needs exactly
four operations out of a specification with hundreds. The usual Python client pulls in a
full WSDL/SOAP stack to reach them, which is a large dependency surface and another
licence to audit for a government submission.

Four requests and four XPath-ish reads is less code than the integration glue for that
library would be, and every byte on the wire is visible in this file -- which matters
when a reviewer asks how the device is authenticated.

The four operations
-------------------
* ``GetSystemDateAndTime`` -- unauthenticated by specification, which makes it the right
  reachability probe and the right place to read the device clock.
* ``GetProfiles`` -- the media profiles, each with an encoder configuration giving codec
  and resolution. This is where declared properties come from, and they are recorded as
  *declared* and verified by decoding, never trusted.
* ``GetStreamUri`` -- the RTSP URL for a profile.
* ``GetServiceCapabilities`` -- what the device admits to supporting, used for health.

Authentication
--------------
WS-Security UsernameToken with a password digest: ``Base64(SHA1(nonce + created +
password))``. The password never crosses the wire, which is the whole point of the digest
profile and the reason it is used here rather than the plaintext one that some sample
code reaches for.

A fresh nonce per request. Reusing one makes the digest replayable, and a replayed
``GetStreamUri`` hands an attacker a stream URL.

Clock confidence, and why ONVIF raises it
------------------------------------------
``GatewaySource`` cannot know when a frame was captured: it anchors the PTS timeline at
the moment it joined the stream, so wall-clock times are an estimate and
``clock_confidence`` reflects that. An ONVIF device exposes its own clock through
``GetSystemDateAndTime``, so the offset between device time and platform time is
*measured* rather than assumed. That makes an ONVIF camera's timestamps better evidence
than a gateway camera's, not worse -- a point worth making explicitly, because adding a
protocol usually dilutes a guarantee rather than strengthening one.

Every request goes through the SSRF guard
-----------------------------------------
A camera URL arrives from an operator pasting it into a form. ``safe_fetch`` is not used
because these are POSTs, so the guard's ``validate_uri`` and ``reverify_before_connect``
are applied directly here -- including the re-resolution immediately before connecting,
which is what closes the DNS-rebinding window.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from services.common.credentials import CameraCredential
from services.common.ssrf import reverify_before_connect, validate_uri

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0

#: Cap on a SOAP response. A device answering with a gigabyte of XML is either broken or
#: hostile, and either way the adapter should not buffer it.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    "{header}<s:Body>{body}</s:Body></s:Envelope>"
)

_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
    "#PasswordDigest"
)
_BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0"
    "#Base64Binary"
)


class OnvifError(RuntimeError):
    """Any ONVIF failure. Messages never include the credential."""


@dataclass(frozen=True)
class OnvifProfile:
    token: str
    name: str
    codec: str | None
    width: int | None
    height: int | None
    #: The device's claim about its frame rate. Recorded as declared and never used for
    #: timing -- the platform measures FPS from stream PTS, and a CI guard enforces that
    #: there is exactly one reference-only read of a declared rate in the codebase.
    declared_fps: float | None


@dataclass(frozen=True)
class OnvifDeviceTime:
    device_utc: datetime
    #: Offset between the device's clock and ours, in seconds. Positive means the device
    #: is ahead. This is what turns an estimated capture time into a measured one.
    offset_s: float


def _security_header(credential: CameraCredential | None) -> str:
    """A WS-Security UsernameToken with a password digest, or nothing.

    The digest is Base64(SHA1(nonce + created + password)). The password itself never
    crosses the wire.
    """
    if credential is None:
        return ""

    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(  # noqa: S324 -- the ONVIF spec mandates SHA1 here
            nonce + created.encode() + credential.password.encode()
        ).digest()
    ).decode()

    return (
        f'<s:Header><Security xmlns="{_WSSE}">'
        f"<UsernameToken><Username>{_escape(credential.username)}</Username>"
        f'<Password Type="{_PASSWORD_DIGEST}">{digest}</Password>'
        f'<Nonce EncodingType="{_BASE64_BINARY}">{base64.b64encode(nonce).decode()}</Nonce>'
        f'<Created xmlns="{_WSU}">{created}</Created>'
        f"</UsernameToken></Security></s:Header>"
    )


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


class OnvifClient:
    """Four ONVIF operations against one device."""

    def __init__(
        self,
        service_url: str,
        credential: CameraCredential | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # Validated at construction so a bad URL fails before any request is attempted.
        self._target = validate_uri(service_url)
        if self._target.scheme not in ("http", "https"):
            raise OnvifError(f"ONVIF is http(s); '{self._target.scheme}' is not supported")
        self._credential = credential
        self._timeout = timeout_s

    @property
    def service_url(self) -> str:
        return self._target.url

    def _call(self, body: str, *, authenticated: bool = True) -> ElementTree.Element:
        import requests

        envelope = _ENVELOPE.format(
            header=_security_header(self._credential if authenticated else None),
            body=body,
        )

        # Re-resolve immediately before connecting. Between validation and the socket
        # opening, a hostile nameserver can change its answer.
        reverify_before_connect(self._target)

        try:
            response = requests.post(
                self._target.url,
                data=envelope.encode(),
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise OnvifError(f"{self._target.url} unreachable: {type(exc).__name__}") from exc

        try:
            if response.is_redirect or response.is_permanent_redirect:
                raise OnvifError("device returned a redirect; redirects are not followed")

            raw = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OnvifError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
        finally:
            response.close()

        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            # A device that is not ONVIF usually answers with an HTML login page, and
            # saying so is more useful than "syntax error at line 1".
            raise OnvifError(
                f"{self._target.url} did not return SOAP XML "
                f"(HTTP {response.status_code}); is this an ONVIF service endpoint?"
            ) from exc

        fault = root.find(".//s:Fault", _NS)
        if fault is not None:
            reason = fault.findtext(".//s:Text", default="", namespaces=_NS).strip()
            code = fault.findtext(".//s:Value", default="", namespaces=_NS).strip()
            # 401/NotAuthorized is by far the most common, and the useful message names
            # the credential without quoting it.
            raise OnvifError(f"device refused the request: {code or 'Fault'} {reason}".strip())

        if response.status_code >= 400:
            raise OnvifError(f"device returned HTTP {response.status_code}")
        return root

    def get_system_date_and_time(self) -> OnvifDeviceTime:
        """The device's own clock. Unauthenticated per the ONVIF specification.

        Used both as the reachability probe and as the clock reference that makes an
        ONVIF camera's timestamps better evidence than a gateway camera's.
        """
        root = self._call("<tds:GetSystemDateAndTime/>", authenticated=False)
        utc = root.find(".//tt:UTCDateTime", _NS)
        if utc is None:
            raise OnvifError("device did not report a UTC clock")

        def _part(path: str) -> int:
            text = utc.findtext(path, default="", namespaces=_NS)
            if not text.strip():
                raise OnvifError(f"device clock is missing {path}")
            return int(text)

        device = datetime(
            _part("tt:Date/tt:Year"),
            _part("tt:Date/tt:Month"),
            _part("tt:Date/tt:Day"),
            _part("tt:Time/tt:Hour"),
            _part("tt:Time/tt:Minute"),
            _part("tt:Time/tt:Second"),
            tzinfo=timezone.utc,
        )
        return OnvifDeviceTime(
            device_utc=device,
            offset_s=(device - datetime.now(timezone.utc)).total_seconds(),
        )

    def get_profiles(self) -> list[OnvifProfile]:
        """Media profiles, with the codec and resolution each one *declares*."""
        root = self._call("<trt:GetProfiles/>")
        profiles: list[OnvifProfile] = []
        for node in root.findall(".//trt:Profiles", _NS):
            encoder = node.find("tt:VideoEncoderConfiguration", _NS)
            resolution = encoder.find("tt:Resolution", _NS) if encoder is not None else None
            rate = encoder.find("tt:RateControl", _NS) if encoder is not None else None

            def _int(parent: Any, tag: str) -> int | None:
                if parent is None:
                    return None
                text = parent.findtext(tag, default="", namespaces=_NS)
                try:
                    return int(text) if text.strip() else None
                except ValueError:
                    return None

            fps_text = (
                rate.findtext("tt:FrameRateLimit", default="", namespaces=_NS)
                if rate is not None
                else ""
            )
            try:
                declared_fps = float(fps_text) if fps_text.strip() else None
            except ValueError:
                declared_fps = None

            profiles.append(
                OnvifProfile(
                    token=node.get("token", ""),
                    name=node.findtext("tt:Name", default="", namespaces=_NS),
                    codec=(
                        encoder.findtext("tt:Encoding", default="", namespaces=_NS) or None
                        if encoder is not None
                        else None
                    ),
                    width=_int(resolution, "tt:Width"),
                    height=_int(resolution, "tt:Height"),
                    declared_fps=declared_fps,
                )
            )
        if not profiles:
            raise OnvifError("device reported no media profiles")
        return profiles

    def get_stream_uri(self, profile_token: str) -> str:
        """The RTSP URL for one profile.

        RTP/RTSP/TCP is requested explicitly. The platform forces TCP everywhere (see
        `cv_env`): UDP survives a laptop demo and fails behind the evaluation network's
        NAT, producing corrupt frames that look like model defects.
        """
        body = (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{_escape(profile_token)}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        root = self._call(body)
        uri = root.findtext(".//tt:Uri", default="", namespaces=_NS).strip()
        if not uri:
            raise OnvifError(f"device returned no stream URI for profile {profile_token}")
        if not uri.lower().startswith("rtsp://"):
            raise OnvifError(f"device returned a non-RTSP stream URI scheme for {profile_token}")
        return uri

    def get_service_capabilities(self) -> dict[str, str]:
        """What the device admits to supporting. Used for health, not for trust."""
        root = self._call("<tds:GetServiceCapabilities/>")
        capabilities: dict[str, str] = {}
        for node in root.iter():
            tag = re.sub(r"^\{.*\}", "", node.tag)
            for key, value in node.attrib.items():
                capabilities[f"{tag}.{re.sub(r'^\{.*\}', '', key)}"] = value
        return capabilities


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "OnvifClient",
    "OnvifDeviceTime",
    "OnvifError",
    "OnvifProfile",
]
