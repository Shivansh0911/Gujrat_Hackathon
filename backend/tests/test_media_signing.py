"""Signed evidence media.

Crops are photographs of vehicles and their plates, and the endpoint that serves them
is reachable without a session -- it has to be, because a browser cannot put an
`Authorization` header on an `<img>`. It was previously open, and crop filenames are
structured (`camera_pts_plate.jpg`), so they can be guessed rather than merely leaked.
"""

from __future__ import annotations

import time

import pytest

from services.api.media_signing import (
    media_basename,
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


# ------------------------------------------------- cross-platform crop paths


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # A Linux worker.
        ("/srv/setu/data/evidence/crops/CAM_1_GJ01AB1234.jpg", "CAM_1_GJ01AB1234.jpg"),
        # A Windows worker. This is the one that broke the first deployment.
        (
            "C:"
            + chr(92)
            + "Users"
            + chr(92)
            + "dev"
            + chr(92)
            + "crops"
            + chr(92)
            + "CAM_1_GJ01AB1234.jpg",
            "CAM_1_GJ01AB1234.jpg",
        ),
        # A relative Windows path.
        ("data" + chr(92) + "evidence" + chr(92) + "crops" + chr(92) + "CAM_1.jpg", "CAM_1.jpg"),
        # Already bare.
        ("CAM_1.jpg", "CAM_1.jpg"),
    ],
)
def test_the_basename_survives_either_separator(stored: str, expected: str) -> None:
    """`crop_path` is written by the ingest worker and read by the API, and the two
    need not run on the same operating system.

    Seeding from Windows and serving from Linux stored a path whose separators the
    server did not recognise, so asking `PurePosixPath` for the basename returned the
    entire string -- drive letter and all. Every evidence image then 404'd behind a
    perfectly valid signature, with an intact database behind that.
    """
    assert media_basename(stored) == expected


def test_a_windows_path_signs_and_verifies_as_its_basename() -> None:
    """End to end: a URL built on Linux from a Windows path is actually fetchable."""
    stored = "C:" + chr(92) + "Users" + chr(92) + "dev" + chr(92) + "CAM_9_GJ18XY4242.jpg"
    url = signed_media_url("/media/crops", stored, SECRET)

    assert url.startswith("/media/crops/CAM_9_GJ18XY4242.jpg?")
    # Neither a drive letter nor a separator reaches the browser.
    assert "C:" not in url
    assert chr(92) not in url

    name = url.split("/media/crops/")[1].split("?")[0]
    exp = int(url.split("exp=")[1].split("&")[0])
    sig = url.split("sig=")[1]
    assert verify_media_name(name, exp, sig, SECRET) is True
