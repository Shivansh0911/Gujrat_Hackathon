"""Adversarial tests for the SSRF guard.

Each attack must be rejected with a *distinct* error, so an operator can tell a typo
from an attack, and none of the messages may leak an internal address.
"""

from __future__ import annotations

import socket

import pytest

from services.common import ssrf
from services.common.ssrf import (
    DnsRebindingDetected,
    HostNotResolvable,
    MalformedUri,
    PortNotAllowed,
    PrivateAddressBlocked,
    SchemeNotAllowed,
    validate_uri,
)


# --------------------------------------------------------------- scheme allowlist


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "gopher://127.0.0.1:6379/_INFO",
        "dict://127.0.0.1:11211/stats",
        "ftp://internal.example/",
        "jar:http://x/!/",
    ],
)
def test_disallowed_schemes_are_rejected(uri):
    with pytest.raises(SchemeNotAllowed) as exc:
        validate_uri(uri)
    assert exc.value.reason == "scheme_not_allowed"


# ------------------------------------------------------------ literal private space


@pytest.mark.parametrize(
    "uri",
    [
        "http://169.254.169.254/",  # cloud metadata
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/",
        "http://[::1]:8080/",
        "http://10.0.0.5:8080/",
        "http://192.168.1.1:80/",
        "http://172.16.0.1:80/",
        "http://0.0.0.0:80/",
        "http://[::ffff:169.254.169.254]:80/",  # v4-mapped metadata address
    ],
)
def test_private_and_link_local_literals_are_blocked(uri):
    with pytest.raises(PrivateAddressBlocked) as exc:
        validate_uri(uri)
    assert exc.value.reason == "private_address_blocked"


def test_metadata_rejection_does_not_echo_the_address():
    # A message repeating the target turns a rejection into a scanning oracle.
    with pytest.raises(PrivateAddressBlocked) as exc:
        validate_uri("http://169.254.169.254/latest/meta-data/iam/")
    assert "169.254" not in str(exc.value)


# ---------------------------------------------------------------- port allowlist


def test_internal_service_port_is_rejected_before_the_host_check():
    # 127.0.0.1:5432 is blocked on the port rule, so the distinct error tells the
    # operator it was the port, not the host.
    with pytest.raises(PortNotAllowed) as exc:
        validate_uri("http://127.0.0.1:5432/")
    assert exc.value.reason == "port_not_allowed"


@pytest.mark.parametrize("port", [22, 25, 3306, 5432, 6379, 9200, 11211, 27017])
def test_common_internal_ports_are_not_allowlisted(port):
    with pytest.raises(PortNotAllowed):
        validate_uri(f"http://example.com:{port}/")


def test_permitted_ports_pass(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    for port in (80, 443, 554, 8554):
        target = validate_uri(f"http://cam.example.test:{port}/stream")
        assert target.port == port


# ------------------------------------------------------------- name resolution


def _stub_resolution(monkeypatch, mapping: dict[str, list[str]]):
    """Replace getaddrinfo so DNS behaviour is deterministic and offline."""

    def fake_getaddrinfo(host, *_a, **_kw):
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in mapping[host]]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


def test_hostname_resolving_to_private_space_is_blocked(monkeypatch):
    # The classic bypass: a public name whose A record points inside the network.
    _stub_resolution(monkeypatch, {"evil.example.test": ["10.1.2.3"]})
    with pytest.raises(PrivateAddressBlocked) as exc:
        validate_uri("http://evil.example.test/")
    assert "10.1.2.3" not in str(exc.value)


def test_hostname_with_any_private_answer_is_blocked(monkeypatch):
    # Mixed answers must fail closed: one public record does not make it safe.
    _stub_resolution(monkeypatch, {"mixed.example.test": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(PrivateAddressBlocked):
        validate_uri("http://mixed.example.test/")


def test_unresolvable_host_has_its_own_error(monkeypatch):
    _stub_resolution(monkeypatch, {})
    with pytest.raises(HostNotResolvable) as exc:
        validate_uri("http://nowhere.example.test/")
    assert exc.value.reason == "host_not_resolvable"


def test_public_hostname_passes_and_records_resolution(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    target = validate_uri("rtsp://cam.example.test:554/stream/1")
    assert target.scheme == "rtsp" and target.port == 554
    assert target.resolved_ips == ("93.184.216.34",)


# ------------------------------------------------------------- DNS rebinding


def test_rebinding_to_private_space_is_caught_at_connect_time(monkeypatch):
    """The second lookup returns metadata; validation alone would have allowed it."""
    _stub_resolution(monkeypatch, {"rebind.example.test": ["93.184.216.34"]})
    target = validate_uri("http://rebind.example.test/")

    # The nameserver flips its answer between validation and connection.
    _stub_resolution(monkeypatch, {"rebind.example.test": ["169.254.169.254"]})
    with pytest.raises(DnsRebindingDetected) as exc:
        ssrf.reverify_before_connect(target)
    assert exc.value.reason == "dns_rebinding_detected"
    assert "169.254" not in str(exc.value)


def test_changed_public_resolution_is_refused_not_trusted(monkeypatch):
    # A rotated record is not proof of attack, but the new address is unvalidated.
    _stub_resolution(monkeypatch, {"lb.example.test": ["93.184.216.34"]})
    target = validate_uri("http://lb.example.test/")
    _stub_resolution(monkeypatch, {"lb.example.test": ["8.8.8.8"]})
    with pytest.raises(DnsRebindingDetected):
        ssrf.reverify_before_connect(target)


def test_stable_resolution_passes_reverification(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    target = validate_uri("http://cam.example.test/")
    ssrf.reverify_before_connect(target)  # must not raise


def test_ip_literal_cannot_rebind(monkeypatch):
    target = validate_uri("http://93.184.216.34/")
    # No DNS is consulted, so no rebinding is possible; must not raise or resolve.
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *a, **k: pytest.fail("a literal address must not trigger a DNS lookup"),
    )
    ssrf.reverify_before_connect(target)


# ------------------------------------------------------------------ malformed


@pytest.mark.parametrize("uri", ["", "not a uri", "http://", "://missing-scheme", "x" * 3000])
def test_malformed_uris_are_rejected(uri):
    with pytest.raises((MalformedUri, SchemeNotAllowed)):
        validate_uri(uri)


def test_every_control_has_a_distinct_reason_code():
    # Distinct codes are what let an operator tell a typo from an attack.
    codes = {
        SchemeNotAllowed.reason,
        PortNotAllowed.reason,
        PrivateAddressBlocked.reason,
        DnsRebindingDetected.reason,
        HostNotResolvable.reason,
        MalformedUri.reason,
        ssrf.RedirectNotAllowed.reason,
        ssrf.ResponseTooLarge.reason,
    }
    assert len(codes) == 8


# ------------------------------------------------------- redirect and size caps


class _FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b"ok",)):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    is_permanent_redirect = property(lambda self: self.status_code in (301, 308))

    def iter_content(self, _n):
        yield from self._chunks

    def close(self):
        pass


def _patch_requests(monkeypatch, response):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: response)
    return response


def test_redirect_to_internal_address_is_refused_not_followed(monkeypatch):
    # The attack: a public URL that 302s to the metadata endpoint. We never follow it,
    # so the internal address is never even requested.
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    _patch_requests(
        monkeypatch,
        _FakeResponse(302, {"Location": "http://169.254.169.254/latest/meta-data/"}),
    )
    with pytest.raises(ssrf.RedirectNotAllowed) as exc:
        ssrf.safe_fetch("http://cam.example.test/probe")
    assert exc.value.reason == "redirect_not_allowed"


def test_oversized_body_is_cut_off_while_streaming(monkeypatch):
    # Content-Length is attacker-controlled, so the cap is enforced on real bytes.
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    _patch_requests(monkeypatch, _FakeResponse(200, {}, chunks=[b"x" * 8192] * 40))
    with pytest.raises(ssrf.ResponseTooLarge):
        ssrf.safe_fetch("http://cam.example.test/probe", max_bytes=64 * 1024)


def test_lying_content_length_is_rejected_early(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    _patch_requests(monkeypatch, _FakeResponse(200, {"Content-Length": "999999999"}))
    with pytest.raises(ssrf.ResponseTooLarge):
        ssrf.safe_fetch("http://cam.example.test/probe", max_bytes=1024)


def test_safe_fetch_rejects_rtsp_scheme(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    with pytest.raises(SchemeNotAllowed):
        ssrf.safe_fetch("rtsp://cam.example.test:554/stream/1")


def test_safe_fetch_returns_body_for_a_permitted_target(monkeypatch):
    _stub_resolution(monkeypatch, {"cam.example.test": ["93.184.216.34"]})
    _patch_requests(monkeypatch, _FakeResponse(200, {}, chunks=[b"#EXTM3U\n"]))
    assert ssrf.safe_fetch("http://cam.example.test/index.m3u8") == b"#EXTM3U\n"
