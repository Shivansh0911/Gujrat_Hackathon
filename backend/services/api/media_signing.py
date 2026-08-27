"""Short-lived signed URLs for evidence media.

Evidence crops are photographs of vehicles and their number plates, taken in public
but assembled here into an investigative record. They were served by filename with no
authentication at all, and the filenames are structured -- `camera_pts_plate.jpg` --
so anyone who reached the host could enumerate them.

The reason it was open is real rather than careless: a browser cannot attach an
`Authorization` header to an `<img>`, so the obvious fix would have meant fetching
every crop as a blob in JavaScript and holding evidence imagery in memory. Signing is
the standard answer to exactly that constraint, and a better one for this data:

* the URL is bound to **one filename**, so a leaked link exposes one crop rather than
  the whole store;
* it **expires**, so a link copied out of a browser history or a chat message stops
  working;
* it is verifiable without a session, which is what makes `<img>` work at all.

The signing key is the API's JWT secret, so there is no second secret to distribute
or rotate. A signature is not a substitute for the access control on the endpoints
that *list* evidence: those still require an authenticated, department-scoped actor,
and a crop URL is only ever handed out alongside a record the caller was allowed to
see.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

#: How long a crop link stays valid. Long enough to open a journey, read it, and
#: export it; short enough that a link pasted somewhere it should not be is dead
#: before anyone follows it.
DEFAULT_TTL_S = 3600


def _digest(name: str, expires_at: int, secret: str) -> str:
    payload = f"{name}:{expires_at}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]


def sign_media_name(name: str, secret: str, ttl_s: int = DEFAULT_TTL_S) -> tuple[int, str]:
    """Return `(expires_at, signature)` for one media filename."""
    # Only ever sign a bare filename. Signing a path would make the signature assert
    # something about a directory the endpoint then has to re-derive.
    bare = Path(name).name
    expires_at = int(time.time()) + ttl_s
    return expires_at, _digest(bare, expires_at, secret)


def verify_media_name(name: str, expires_at: int, signature: str, secret: str) -> bool:
    """True if `signature` is ours and has not expired."""
    if expires_at < int(time.time()):
        return False
    expected = _digest(Path(name).name, expires_at, secret)
    # Constant-time: a timing oracle on a 128-bit signature is not a practical forgery
    # route, but comparing secrets with `==` is a habit worth not having.
    return hmac.compare_digest(expected, signature)


def signed_media_url(prefix: str, name: str, secret: str, ttl_s: int = DEFAULT_TTL_S) -> str:
    """Build the console-facing URL for one media file."""
    bare = Path(name).name
    expires_at, signature = sign_media_name(bare, secret, ttl_s)
    return f"{prefix}/{bare}?exp={expires_at}&sig={signature}"


__all__ = [
    "DEFAULT_TTL_S",
    "sign_media_name",
    "verify_media_name",
    "signed_media_url",
]
