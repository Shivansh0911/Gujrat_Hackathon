"""Token issuance and verification, pinned directly.

Authentication was exercised only through endpoint tests, every one of which needs a
database. That meant the JWT library could be swapped with no test in CI that touched
the thing being swapped -- and the swap was made to close a critical
algorithm-confusion advisory in python-jose, so the attack the new library must resist
was exactly the behaviour nothing checked.

These run with no database and no network. Each one is a way a token has been forged
in the wild: an unsigned token, a token that declares its own algorithm, one whose
signature was re-made with a key of the attacker's choosing, and one with no expiry.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt as pyjwt
import pytest
from fastapi import HTTPException

SECRET = "a-test-secret-long-enough-for-the-validator"
ISSUER = "setu-test"


def _settings() -> Any:
    """ApiSettings built from values here, not from a deployment's configuration.

    Constructed directly and touching no environment variable. An earlier version
    called `os.environ.setdefault` for the database URL "just in case", which turned
    out to set it: pydantic-settings reads `.env` when it builds the object and never
    puts those values in `os.environ`, so the default was not already there. Twenty-one
    endpoint tests then ran against a dummy database for the rest of the session --
    passing alone and failing in the suite, which is the worst way for a fixture to be
    wrong.
    """
    from services.api.config import ApiSettings

    return ApiSettings(
        database_url="postgresql+psycopg://u:p@127.0.0.1/none",
        jwt_secret=SECRET,
        jwt_issuer=ISSUER,
    )


def _decode(token: str) -> dict[str, Any]:
    from services.api.security import decode_token

    return decode_token(token, _settings())


def test_a_token_this_platform_issued_verifies() -> None:
    from services.api.security import create_access_token

    dept = uuid.uuid4()
    token = create_access_token(
        subject="admin", role="ADMIN", settings=_settings(), department_id=dept
    )
    claims = _decode(token)

    assert claims["sub"] == "admin"
    assert claims["role"] == "ADMIN"
    assert claims["iss"] == ISSUER
    assert claims["dept"] == str(dept)
    assert "exp" in claims


def test_an_unsigned_token_is_refused() -> None:
    """`alg: none` is the oldest forgery there is."""
    forged = pyjwt.encode(
        {
            "sub": "admin",
            "role": "ADMIN",
            "iss": ISSUER,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(HTTPException) as excinfo:
        _decode(forged)
    assert excinfo.value.status_code == 401


def test_a_token_declaring_a_different_algorithm_is_refused() -> None:
    """Algorithm confusion: the token names the algorithm and the verifier obeys.

    This is the shape of GHSA-3qf3-8w2g-rqmx, the advisory that caused the move off
    python-jose. The header is checked against the allowlist before verification, and
    the allowlist is passed to decode as well, so neither path trusts the token.
    """
    forged = pyjwt.encode(
        {
            "sub": "admin",
            "role": "ADMIN",
            "iss": ISSUER,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        key=SECRET,
        algorithm="HS512",
    )
    with pytest.raises(HTTPException) as excinfo:
        _decode(forged)
    assert excinfo.value.status_code == 401


def test_a_token_signed_with_another_key_is_refused() -> None:
    forged = pyjwt.encode(
        {
            "sub": "admin",
            "role": "ADMIN",
            "iss": ISSUER,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        key="an-attackers-own-secret-which-is-long-enough",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        _decode(forged)


def test_an_expired_token_is_refused() -> None:
    expired = pyjwt.encode(
        {
            "sub": "admin",
            "role": "ADMIN",
            "iss": ISSUER,
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        key=SECRET,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        _decode(expired)


def test_a_token_with_no_expiry_is_refused() -> None:
    """The check that fails open if it is spelled wrong.

    PyJWT takes required claims as `options={"require": [...]}`; python-jose took
    `require_exp` / `require_sub` flags. Carrying the old spelling across would have
    silently accepted a token that never expires, which is why this is a test and not
    a comment.
    """
    forever = pyjwt.encode(
        {"sub": "admin", "role": "ADMIN", "iss": ISSUER}, key=SECRET, algorithm="HS256"
    )
    with pytest.raises(HTTPException):
        _decode(forever)


def test_a_token_with_no_subject_is_refused() -> None:
    anonymous = pyjwt.encode(
        {"role": "ADMIN", "iss": ISSUER, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        key=SECRET,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        _decode(anonymous)


def test_a_token_from_another_issuer_is_refused() -> None:
    elsewhere = pyjwt.encode(
        {
            "sub": "admin",
            "role": "ADMIN",
            "iss": "some-other-service",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        key=SECRET,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException):
        _decode(elsewhere)


def test_a_tampered_payload_is_refused() -> None:
    """Flip a role from OPERATOR to ADMIN and the signature stops matching."""
    from services.api.security import create_access_token

    token = create_access_token(subject="operator", role="OPERATOR", settings=_settings())
    header, payload, signature = token.split(".")
    import base64
    import json

    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    claims = json.loads(raw)
    claims["role"] = "ADMIN"
    tampered_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

    with pytest.raises(HTTPException):
        _decode(f"{header}.{tampered_payload}.{signature}")


def test_python_jose_is_gone() -> None:
    """The advisory had no patched release, so the only fix was removing the package.

    A test rather than a note: a transitive dependency could quietly reintroduce it,
    and the suppression that used to excuse it has been deleted from CI.
    """
    with pytest.raises(ImportError):
        __import__("jose")
