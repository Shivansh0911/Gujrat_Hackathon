"""Signed evidence media.

Crops are photographs of vehicles and their plates, and the endpoint that serves them
is reachable without a session -- it has to be, because a browser cannot put an
`Authorization` header on an `<img>`. It was previously open, and crop filenames are
structured (`camera_pts_plate.jpg`), so they can be guessed rather than merely leaked.
"""

from __future__ import annotations

import time

from services.api.media_signing import (
    sign_media_name,
    signed_media_url,
    verify_media_name,
)

SECRET = "test-secret-not-used-anywhere-else"


def test_a_signature_we_issued_verifies() -> None:
    exp, sig = sign_media_name("7_1234_GJ01AB1234.jpg", SECRET)
    assert verify_media_name("7_1234_GJ01AB1234.jpg", exp, sig, SECRET) is True


def test_an_expired_signature_is_refused() -> None:
    """A link copied out of a browser history stops working."""
    exp, sig = sign_media_name("crop.jpg", SECRET, ttl_s=-1)
    assert verify_media_name("crop.jpg", exp, sig, SECRET) is False


def test_a_signature_does_not_transfer_to_another_file() -> None:
    """The whole point: a leaked link exposes one crop, not the store."""
    exp, sig = sign_media_name("crop-a.jpg", SECRET)
    assert verify_media_name("crop-b.jpg", exp, sig, SECRET) is False


def test_a_signature_from_a_different_key_is_refused() -> None:
    exp, sig = sign_media_name("crop.jpg", SECRET)
    assert verify_media_name("crop.jpg", exp, sig, "a-different-secret") is False


def test_extending_the_expiry_invalidates_the_signature() -> None:
    """The expiry is signed, so it cannot be edited in the URL."""
    exp, sig = sign_media_name("crop.jpg", SECRET)
    assert verify_media_name("crop.jpg", exp + 86_400, sig, SECRET) is False


def test_an_absent_signature_is_refused() -> None:
    """The endpoint's defaults (exp=0, sig="") must not authorise anything."""
    assert verify_media_name("crop.jpg", 0, "", SECRET) is False


def test_the_signature_covers_only_the_basename() -> None:
    """A directory in the name must not change what the signature asserts."""
    exp, sig = sign_media_name("crop.jpg", SECRET)
    assert verify_media_name("../../etc/crop.jpg", exp, sig, SECRET) is True


def test_the_url_carries_both_parameters() -> None:
    url = signed_media_url("/media/crops", "some/dir/crop.jpg", SECRET)
    assert url.startswith("/media/crops/crop.jpg?")
    assert "exp=" in url and "sig=" in url
    # The path the browser requests never reveals the server's layout.
    assert "some/dir" not in url


def test_the_expiry_is_in_the_future_by_the_requested_ttl() -> None:
    exp, _ = sign_media_name("crop.jpg", SECRET, ttl_s=120)
    remaining = exp - int(time.time())
    assert 100 <= remaining <= 120
