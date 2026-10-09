"""API versioning and the deprecation policy, stated where a client can read it.

The policy
----------
The API is versioned in the URL path: `/v1/...`. Header-based negotiation was the other
option and was rejected for this audience -- a departmental integration is as likely to
be a scheduled `curl` or a vendor's HTTP connector as a hand-written client, and a
version you can see in a log line is one that can be diagnosed over the phone.

Unversioned paths continue to work and resolve to the current version. Every endpoint
this platform shipped before versioning existed is reachable at its original path, and
breaking those to tidy the URL space would be the kind of change this policy exists to
prevent.

**Commitments**

1. Within a major version, only compatible changes are made: new endpoints, new optional
   request fields, new response fields. A client that ignores unknown fields keeps
   working.
2. A field is never repurposed. If its meaning changes, it gets a new name and the old
   one is deprecated.
3. `ErrorCode` members are never reworded or reused. New codes may be added; a client
   must treat an unrecognised code as its HTTP status implies.
4. A version is announced as deprecated at least **180 days** before it is withdrawn.
   Deprecated responses carry `Deprecation` and `Sunset` headers (RFC 8594) so a client
   learns of it from traffic rather than from an email that went to someone who left.
5. The OpenAPI document is the contract. The console's TypeScript types are generated
   from it, so the client this team controls cannot silently drift -- and a third party
   generating their own client gets the same guarantee.

Why 180 days
------------
A government department's integration change goes through a procurement and testing
cycle measured in months, not weeks. A deprecation window shorter than that is a
deprecation window in name only.
"""

from __future__ import annotations

from typing import Any

#: The current major version. Bumped only for a breaking change, which per the policy
#: above requires a 180-day deprecation window on the version being replaced.
API_VERSION = "v1"

#: Versions this build serves. A version stays here through its deprecation window.
SUPPORTED_VERSIONS: tuple[str, ...] = ("v1",)

#: Versions still served but announced for withdrawal, mapped to their sunset date in
#: RFC 1123 form for the `Sunset` header. Empty today; the mechanism exists so that the
#: first deprecation is a data change rather than a code change made under time pressure.
DEPRECATED_VERSIONS: dict[str, str] = {}

#: How long a version is supported after deprecation is announced.
DEPRECATION_NOTICE_DAYS = 180


def deprecation_headers(version: str) -> dict[str, str]:
    """RFC 8594 headers for a deprecated version, or nothing.

    A client learns about a withdrawal from the traffic it is already sending, rather
    than from an email to an address that may no longer have a person behind it.
    """
    sunset = DEPRECATED_VERSIONS.get(version)
    if not sunset:
        return {}
    return {
        "Deprecation": "true",
        "Sunset": sunset,
        "Link": '</docs>; rel="deprecation"; type="text/html"',
    }


def version_info() -> dict[str, Any]:
    return {
        "current": API_VERSION,
        "supported": list(SUPPORTED_VERSIONS),
        "deprecated": dict(DEPRECATED_VERSIONS),
        "deprecation_notice_days": DEPRECATION_NOTICE_DAYS,
        "policy": (
            "Versioned in the URL path. Unversioned paths resolve to the current "
            "version. Within a major version only additive changes are made; fields are "
            "never repurposed and error codes are never reworded or reused. A version is "
            f"announced as deprecated at least {DEPRECATION_NOTICE_DAYS} days before "
            "withdrawal, and deprecated responses carry Deprecation and Sunset headers."
        ),
    }


__all__ = [
    "API_VERSION",
    "DEPRECATED_VERSIONS",
    "DEPRECATION_NOTICE_DAYS",
    "SUPPORTED_VERSIONS",
    "deprecation_headers",
    "version_info",
]
