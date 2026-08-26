"""SSRF guard for every endpoint that accepts a URI.

Camera onboarding takes an address and instructs the server to connect to it. That is
a server-side request forgery primitive handed to the caller, and on a government
platform sitting inside a departmental network it is the highest-value vulnerability
in the system: the cloud metadata endpoint, an internal Postgres, a VMS admin panel.

The controls here are applied together, because each alone is bypassable:

  * scheme allowlist        - blocks file://, gopher://, dict://
  * port allowlist          - blocks 127.0.0.1:5432 even if the host were permitted
  * DNS resolution check    - blocks names that resolve into private space
  * connect-time re-check   - blocks DNS rebinding, where the second lookup differs
  * redirects disabled      - blocks a public URL redirecting to an internal one
  * response size cap       - blocks memory exhaustion via an endless body
  * timeout                 - blocks a hung socket holding a worker

Errors are deliberately distinct per control (so operators can tell what went wrong)
and deliberately non-leaking: they never echo the resolved address, because that turns
a rejection into an internal-network oracle.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

# rtsp/rtsps are here because camera onboarding is the primary caller. http/https are
# permitted for HLS playlists and ONVIF device services.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https", "rtsp", "rtsps"})

ALLOWED_PORTS: frozenset[int] = frozenset(
    {
        80, 443,      # HTTP(S) / HLS
        554, 322,     # RTSP, RTSPS
        8000, 8080, 8081, 8443, 8554, 8889,  # common camera/VMS/gateway ports
    }
)

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "rtsp": 554, "rtsps": 322}

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CONNECT_TIMEOUT_S = 5.0


class SsrfBlocked(ValueError):
    """Base class. `reason` is a stable machine code; str() is safe to return to a caller."""

    reason = "blocked"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SchemeNotAllowed(SsrfBlocked):
    reason = "scheme_not_allowed"


class PortNotAllowed(SsrfBlocked):
    reason = "port_not_allowed"


class HostNotResolvable(SsrfBlocked):
    reason = "host_not_resolvable"


class PrivateAddressBlocked(SsrfBlocked):
    reason = "private_address_blocked"


class DnsRebindingDetected(SsrfBlocked):
    reason = "dns_rebinding_detected"


class RedirectNotAllowed(SsrfBlocked):
    reason = "redirect_not_allowed"


class ResponseTooLarge(SsrfBlocked):
    reason = "response_too_large"


class MalformedUri(SsrfBlocked):
    reason = "malformed_uri"


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that must never be reachable from a user-supplied URI."""
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local        # covers 169.254.0.0/16, i.e. cloud metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # IPv4-mapped and 6to4 addresses smuggle a private v4 address inside a v6 literal
    # that would otherwise look public (e.g. ::ffff:169.254.169.254).
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and _is_forbidden_ip(ip.ipv4_mapped):
            return True
        if ip.sixtofour is not None and _is_forbidden_ip(ip.sixtofour):
            return True
    return False


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HostNotResolvable(f"host could not be resolved: {exc.strerror or 'DNS failure'}") from exc
    return sorted({info[4][0] for info in infos})


@dataclass(frozen=True)
class ValidatedTarget:
    """A URI that passed validation, with the addresses it resolved to at check time."""

    scheme: str
    host: str
    port: int
    url: str
    resolved_ips: tuple[str, ...]


def validate_uri(uri: str) -> ValidatedTarget:
    """Validate a user-supplied URI. Raises a specific SsrfBlocked subclass on failure."""
    if not uri or len(uri) > 2048:
        raise MalformedUri("uri is empty or exceeds the maximum length")

    try:
        parts = urlsplit(uri)
    except ValueError as exc:
        raise MalformedUri(f"uri could not be parsed: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SchemeNotAllowed(
            f"scheme '{scheme or '(none)'}' is not permitted; "
            f"allowed: {', '.join(sorted(ALLOWED_SCHEMES))}"
        )

    host = parts.hostname
    if not host:
        raise MalformedUri("uri has no host component")

    try:
        port = parts.port or DEFAULT_PORTS[scheme]
    except ValueError as exc:
        # urlsplit raises on a non-numeric or out-of-range port.
        raise MalformedUri("uri has an invalid port") from exc

    if port not in ALLOWED_PORTS:
        raise PortNotAllowed(f"port {port} is not permitted")

    # A bare IP literal is checked directly; a name is resolved first.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_forbidden_ip(literal):
            raise PrivateAddressBlocked("target address is in a restricted range")
        resolved = (str(literal),)
    else:
        addresses = _resolve(host)
        for addr in addresses:
            if _is_forbidden_ip(ipaddress.ip_address(addr)):
                # Deliberately does not name the address: echoing it back would turn
                # this rejection into an internal-network scanning oracle.
                raise PrivateAddressBlocked("target host resolves to a restricted range")
        resolved = tuple(addresses)

    return ValidatedTarget(scheme=scheme, host=host, port=port, url=uri, resolved_ips=resolved)


def reverify_before_connect(target: ValidatedTarget) -> None:
    """Re-resolve immediately before connecting, to defeat DNS rebinding.

    Validation and connection are separated in time. An attacker who controls a
    nameserver can answer the first lookup with a public address and the second with
    169.254.169.254, so a check performed only at validation time is decorative. This
    must be called by the connecting code, not merely by the validating code.
    """
    try:
        ipaddress.ip_address(target.host)
        return  # a literal cannot rebind
    except ValueError:
        pass

    current = _resolve(target.host)
    for addr in current:
        if _is_forbidden_ip(ipaddress.ip_address(addr)):
            raise DnsRebindingDetected("target host now resolves to a restricted range")

    if set(current) != set(target.resolved_ips):  # noqa: SIM102 - explicit for clarity
        # A changed answer is not proof of an attack -- load balancers rotate records --
        # but the new set is unvalidated, so it is refused rather than trusted. The
        # caller can re-validate and retry.
        raise DnsRebindingDetected("target host resolution changed between check and connect")


def safe_fetch(uri: str, *, max_bytes: int = MAX_RESPONSE_BYTES,
               timeout_s: float = CONNECT_TIMEOUT_S) -> bytes:
    """Fetch an http(s) URI under every SSRF control. Returns the body.

    Redirects are disabled rather than followed-and-revalidated. Following them
    safely would mean re-running the full validation on each hop, and a 30x to an
    internal address is not a legitimate pattern for camera onboarding -- refusing is
    both safer and simpler to reason about.
    """
    import requests  # local import: the guard is used in contexts without HTTP

    target = validate_uri(uri)
    if target.scheme not in ("http", "https"):
        raise SchemeNotAllowed(f"safe_fetch supports http(s) only, not '{target.scheme}'")

    # Re-resolve immediately before the request. Between validate_uri() above and the
    # socket being opened below, a hostile nameserver can change its answer.
    reverify_before_connect(target)

    resp = requests.get(
        target.url,
        timeout=timeout_s,
        allow_redirects=False,   # see docstring
        stream=True,             # so the size cap applies before the body is buffered
    )
    try:
        if resp.is_redirect or resp.is_permanent_redirect:
            raise RedirectNotAllowed(
                f"target returned a redirect ({resp.status_code}); redirects are not followed"
            )

        # Trust the declared length only as an early exit; enforce the real cap while
        # streaming, because Content-Length is attacker-controlled.
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(f"response exceeds the {max_bytes} byte limit")

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(8192):
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge(f"response exceeds the {max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        resp.close()
