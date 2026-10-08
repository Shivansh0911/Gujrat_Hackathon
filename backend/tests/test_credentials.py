"""A credential store is judged by what it refuses and by what it never says.

Most of this file is about leaks rather than correctness. The round trip is three lines;
the rest asserts that a password does not appear in a repr, in an exception message, or
in a ciphertext that was moved to another camera's row.
"""

from __future__ import annotations

import base64
import secrets

import pytest

from services.common.credentials import (
    CURRENT_KEY_VERSION,
    KEY_ENV,
    CameraCredential,
    CredentialError,
    SealedCredential,
    fingerprint,
    seal,
    unseal,
)

SECRET = "tr0ub4dor&3:with:colons"


@pytest.fixture
def env() -> dict[str, str]:
    return {KEY_ENV: base64.b64encode(secrets.token_bytes(32)).decode()}


@pytest.fixture
def credential() -> CameraCredential:
    return CameraCredential(username="admin", password=SECRET)


# ------------------------------------------------------------------- round trip


def test_a_credential_survives_sealing_and_unsealing(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    sealed = seal(credential, camera_id="cam-1", env=env)
    back = unseal(sealed, camera_id="cam-1", env=env)
    assert back.username == credential.username
    assert back.password == credential.password


def test_a_password_containing_the_delimiter_round_trips(env: dict[str, str]) -> None:
    """The payload is length-prefixed precisely so a colon in a password is not a bug.

    Device passwords contain punctuation. A naive `username:password` split would
    silently truncate at the first colon and authenticate with a shorter password, which
    fails at the camera and looks like a wrong credential rather than a parsing defect.
    """
    awkward = CameraCredential(username="oper:ator", password="a:b:c::d")
    back = unseal(seal(awkward, camera_id="c", env=env), camera_id="c", env=env)
    assert back.username == "oper:ator"
    assert back.password == "a:b:c::d"


def test_an_empty_password_is_allowed_but_an_empty_username_is_not(
    env: dict[str, str],
) -> None:
    """Some devices ship with a blank password; none ship with a blank username."""
    back = unseal(
        seal(CameraCredential(username="admin", password=""), camera_id="c", env=env),
        camera_id="c",
        env=env,
    )
    assert back.password == ""

    with pytest.raises(CredentialError, match="needs a username"):
        seal(CameraCredential(username="", password="x"), camera_id="c", env=env)


def test_sealing_twice_gives_different_ciphertext(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    """A fresh nonce each time, so identical credentials are not identifiable as equal."""
    a = seal(credential, camera_id="c", env=env)
    b = seal(credential, camera_id="c", env=env)
    assert a.ciphertext != b.ciphertext


# ------------------------------------------------------------------- it refuses


def test_a_ciphertext_moved_to_another_camera_will_not_open(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    """The camera id is authenticated data, so a copied row fails rather than working.

    Without this, a row copied between cameras silently authenticates the wrong device
    against the wrong estate -- which succeeds often enough to be dangerous, because a
    department's cameras frequently share a password.
    """
    sealed = seal(credential, camera_id="cam-1", env=env)
    with pytest.raises(CredentialError, match="failed authentication"):
        unseal(sealed, camera_id="cam-2", env=env)


def test_a_tampered_ciphertext_will_not_open(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    sealed = seal(credential, camera_id="cam-1", env=env)
    corrupted = bytearray(sealed.ciphertext)
    corrupted[-1] ^= 0x01
    with pytest.raises(CredentialError, match="failed authentication"):
        unseal(
            SealedCredential(bytes(corrupted), sealed.key_version, sealed.sealed_at),
            camera_id="cam-1",
            env=env,
        )


def test_the_wrong_master_key_will_not_open(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    sealed = seal(credential, camera_id="cam-1", env=env)
    other = {KEY_ENV: base64.b64encode(secrets.token_bytes(32)).decode()}
    with pytest.raises(CredentialError, match="failed authentication"):
        unseal(sealed, camera_id="cam-1", env=other)


def test_a_truncated_ciphertext_is_refused_before_the_cipher(env: dict[str, str]) -> None:
    with pytest.raises(CredentialError, match="truncated"):
        unseal(SealedCredential(b"short", CURRENT_KEY_VERSION, None), camera_id="c", env=env)  # type: ignore[arg-type]


def test_an_older_key_version_is_refused_loudly_rather_than_attempted(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    """A wrong key and a tampered row both produce InvalidTag; they must not be confused.

    During an incident, "this was encrypted under a key we retired" and "this row was
    altered" lead to completely different responses.
    """
    sealed = seal(credential, camera_id="c", env=env)
    old = SealedCredential(sealed.ciphertext, CURRENT_KEY_VERSION - 1, sealed.sealed_at)
    with pytest.raises(CredentialError, match="key version"):
        unseal(old, camera_id="c", env=env)


# ------------------------------------------------------------------ missing key


def test_no_master_key_fails_with_instructions_and_no_value(
    credential: CameraCredential,
) -> None:
    with pytest.raises(CredentialError) as exc:
        seal(credential, camera_id="c", env={})
    assert KEY_ENV in str(exc.value)
    assert "secrets.token_bytes" in str(exc.value)


def test_a_short_master_key_is_refused_without_quoting_it() -> None:
    """The classic leak is an error message that includes the value it rejected."""
    bad = "aGVsbG8="  # "hello", five bytes
    with pytest.raises(CredentialError) as exc:
        seal(CameraCredential("a", "b"), camera_id="c", env={KEY_ENV: bad})
    assert bad not in str(exc.value)
    assert "32 bytes" in str(exc.value)


def test_a_hex_master_key_is_accepted_as_well_as_base64(
    credential: CameraCredential,
) -> None:
    env = {KEY_ENV: secrets.token_bytes(32).hex()}
    back = unseal(seal(credential, camera_id="c", env=env), camera_id="c", env=env)
    assert back.password == SECRET


# --------------------------------------------------------------------- no leaks


def test_the_password_is_not_in_the_repr(credential: CameraCredential) -> None:
    """Any traceback, any debugger, any structlog field rendering the dataclass."""
    assert SECRET not in repr(credential)
    assert SECRET not in str(credential)
    assert "<redacted>" in repr(credential)
    assert credential.username in repr(credential), "the username is not the secret"


@pytest.mark.parametrize(
    "camera_id,other_id",
    [("cam-1", "cam-2"), ("a" * 64, "b" * 64)],
    ids=["short-ids", "long-ids"],
)
def test_no_failure_message_contains_the_secret(
    credential: CameraCredential, env: dict[str, str], camera_id: str, other_id: str
) -> None:
    """Every message here is written to be safe to log verbatim."""
    sealed = seal(credential, camera_id=camera_id, env=env)
    with pytest.raises(CredentialError) as exc:
        unseal(sealed, camera_id=other_id, env=env)
    assert SECRET not in str(exc.value)
    assert credential.username not in str(exc.value)


def test_the_plaintext_is_not_recoverable_from_the_ciphertext(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    sealed = seal(credential, camera_id="c", env=env)
    assert SECRET.encode() not in sealed.ciphertext
    assert b"admin" not in sealed.ciphertext


# ------------------------------------------------------------------ fingerprint


def test_a_fingerprint_distinguishes_credentials_without_revealing_them(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    """ "The credential changed" is useful to say without saying what it changed to."""
    a = seal(credential, camera_id="c", env=env)
    b = seal(CameraCredential("admin", "different"), camera_id="c", env=env)
    assert fingerprint(a) != fingerprint(b)
    assert len(fingerprint(a)) == 12
    assert SECRET not in fingerprint(a)


def test_a_fingerprint_is_stable_for_one_stored_value(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    sealed = seal(credential, camera_id="c", env=env)
    assert fingerprint(sealed) == fingerprint(sealed.ciphertext)


# ------------------------------------------------------------ the row it produces


def test_the_row_carries_the_key_version_for_rotation(
    credential: CameraCredential, env: dict[str, str]
) -> None:
    """Rotation adds a key and re-encrypts in the background; it is not a flag day."""
    row = seal(credential, camera_id="c", env=env).as_row()
    assert set(row) == {
        "credential_ciphertext",
        "credential_key_version",
        "credential_sealed_at",
    }
    assert row["credential_key_version"] == CURRENT_KEY_VERSION
    assert isinstance(row["credential_ciphertext"], bytes)
