"""Per-camera credentials, encrypted at rest and never returned to any client.

Why this exists
---------------
Every camera the platform has integrated so far shares one credential: the gateway's
access code, held in configuration. A real federation does not work that way. Twenty-six
departments mean twenty-six sets of device passwords, rotated on their own schedules by
people who will not hand them over as a single environment variable, and an ONVIF camera
authenticates per device.

So credentials become data rather than configuration, and the moment they do, three
properties stop being automatic and have to be built:

**Encrypted at rest.** The database holds them, so a database backup holds them, and a
backup is the thing most likely to leave the building. AES-256-GCM with a key derived
from `SETU_CREDENTIAL_KEY`, which stays in the environment and never in a row.

**Never returned.** There is no read path to plaintext outside the adapter process. The
API can tell you a camera *has* credentials and when they were last rotated; it cannot
tell you what they are, and no endpoint is provided that could. A credential store with
a convenient getter becomes a credential-exfiltration endpoint the first time an
authorisation check is written wrong.

**Never logged.** `redact.py` already exists for exactly this and every error raised here
is written to be safe to log verbatim. The usual failure -- a `ValueError` whose message
contains the thing that failed to parse -- is the one that puts a password in a log file
that is then shipped to a vendor.

Why AES-GCM and not Fernet
--------------------------
Fernet would be fewer lines. It is AES-128-CBC with HMAC and no support for additional
authenticated data, and AAD is what lets the camera id be bound into the ciphertext: a
row moved from one camera to another fails to decrypt rather than silently
authenticating the wrong device against the wrong estate. That is worth the extra
handling.

Key rotation
------------
`key_version` is stored beside every ciphertext. Rotating means adding a new key, writing
new rows under it, and re-encrypting old ones in the background -- not a flag day. The
store refuses to decrypt under a version it does not have, loudly, rather than guessing.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.hashes import SHA256

log = logging.getLogger(__name__)

#: Environment variable holding the master key, as 32+ bytes of base64 or hex. Never
#: read into a model, never written to a row, never included in an error.
KEY_ENV = "SETU_CREDENTIAL_KEY"

#: Current key version. Stored with every ciphertext so rotation does not need a flag
#: day: add a key, write new rows under the new version, re-encrypt old rows in the
#: background.
CURRENT_KEY_VERSION = 1

#: AES-GCM nonce length. 96 bits is the size the mode is defined for; anything else
#: forces an internal rehash and loses the security proof.
_NONCE_BYTES = 12

_INFO = b"setu-camera-credential-v1"


class CredentialError(RuntimeError):
    """Raised for every failure here.

    One exception type, and every message is written to be safe to log verbatim. The
    classic leak is a ValueError carrying the value that failed to parse, which ends up
    in a log that is then attached to a support ticket.
    """


@dataclass(frozen=True)
class CameraCredential:
    """What an adapter needs to authenticate to one device."""

    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        # Without this, any traceback, any `repr()` in a debugger, any structlog field
        # rendering the dataclass prints the password. Overridden rather than trusted.
        return f"CameraCredential(username={self.username!r}, password=<redacted>)"

    def __str__(self) -> str:  # pragma: no cover
        return self.__repr__()


def _master_key(env: dict[str, str] | None = None) -> bytes:
    raw = (env or dict(os.environ)).get(KEY_ENV, "").strip()
    if not raw:
        raise CredentialError(
            f"{KEY_ENV} is not set. Per-camera credentials cannot be stored or read "
            f'without it. Generate one with: python -c "import secrets,base64; '
            f'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
        )
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            key = decoder(raw)
        except Exception:  # noqa: BLE001 -- trying formats, not handling an error
            continue
        if len(key) >= 32:
            return bytes(key)
    # Deliberately says nothing about what the value was.
    raise CredentialError(
        f"{KEY_ENV} must be at least 32 bytes, base64- or hex-encoded. "
        f"The configured value is neither, or is too short."
    )


def _derive(master: bytes, camera_id: str, key_version: int) -> bytes:
    """One key per camera per version, derived rather than stored.

    HKDF with the camera id in the salt means compromising one camera's derived key
    does not compromise another's, and the master key never touches the cipher directly.
    """
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=f"{camera_id}:{key_version}".encode(),
        info=_INFO,
    ).derive(master)


@dataclass(frozen=True)
class SealedCredential:
    """What goes in the database. Opaque by construction."""

    ciphertext: bytes
    key_version: int
    sealed_at: datetime

    def as_row(self) -> dict[str, Any]:
        return {
            "credential_ciphertext": self.ciphertext,
            "credential_key_version": self.key_version,
            "credential_sealed_at": self.sealed_at,
        }


def seal(
    credential: CameraCredential,
    *,
    camera_id: str,
    env: dict[str, str] | None = None,
    key_version: int = CURRENT_KEY_VERSION,
) -> SealedCredential:
    """Encrypt a credential for one camera.

    The camera id is bound in as additional authenticated data, so a ciphertext copied
    to another camera's row fails to open rather than silently authenticating the wrong
    device.
    """
    if not credential.username:
        raise CredentialError("a credential needs a username")

    key = _derive(_master_key(env), camera_id, key_version)
    nonce = os.urandom(_NONCE_BYTES)
    payload = f"{len(credential.username)}:{credential.username}{credential.password}".encode()
    sealed = AESGCM(key).encrypt(nonce, payload, camera_id.encode())
    return SealedCredential(
        ciphertext=nonce + sealed,
        key_version=key_version,
        sealed_at=datetime.now(timezone.utc),
    )


def unseal(
    sealed: SealedCredential | bytes,
    *,
    camera_id: str,
    key_version: int = CURRENT_KEY_VERSION,
    env: dict[str, str] | None = None,
) -> CameraCredential:
    """Decrypt a credential. Called only inside an adapter, never from a request path.

    There is intentionally no API endpoint behind this. A credential store with a
    convenient getter becomes an exfiltration endpoint the first time an authorisation
    check is written wrong, and the adapter is the only code that needs the plaintext.
    """
    blob = sealed.ciphertext if isinstance(sealed, SealedCredential) else sealed
    version = sealed.key_version if isinstance(sealed, SealedCredential) else key_version

    if version != CURRENT_KEY_VERSION:
        # Refused loudly rather than attempted under the current key: a wrong key
        # produces an InvalidTag, which is indistinguishable from tampering, and the
        # two should not be confused in an incident.
        raise CredentialError(
            f"credential for camera {camera_id} was sealed under key version {version}; "
            f"this process holds version {CURRENT_KEY_VERSION}. Re-encrypt it before "
            f"rotating the key out."
        )
    if len(blob) <= _NONCE_BYTES:
        raise CredentialError(f"credential for camera {camera_id} is truncated")

    key = _derive(_master_key(env), camera_id, version)
    try:
        payload = AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], camera_id.encode())
    except InvalidTag as exc:
        raise CredentialError(
            f"credential for camera {camera_id} failed authentication. Either it was "
            f"sealed for a different camera, the key is wrong, or the row was altered."
        ) from exc

    try:
        head, rest = payload.decode().split(":", 1)
        length = int(head)
    except (UnicodeDecodeError, ValueError) as exc:
        # Says nothing about the contents. The usual mistake here is an error message
        # that quotes the value it could not parse.
        raise CredentialError(
            f"credential for camera {camera_id} decrypted to an unexpected format"
        ) from exc

    return CameraCredential(username=rest[:length], password=rest[length:])


def fingerprint(sealed: SealedCredential | bytes) -> str:
    """A short, non-reversible tag so two stored credentials can be told apart.

    Used in audit entries and in the console: "the credential changed" is a useful thing
    to be able to say without ever saying what it changed to.
    """
    import hashlib

    blob = sealed.ciphertext if isinstance(sealed, SealedCredential) else sealed
    return hashlib.sha256(blob).hexdigest()[:12]


__all__ = [
    "CURRENT_KEY_VERSION",
    "KEY_ENV",
    "CameraCredential",
    "CredentialError",
    "SealedCredential",
    "fingerprint",
    "seal",
    "unseal",
]
