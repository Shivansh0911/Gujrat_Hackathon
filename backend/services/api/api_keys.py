"""Service accounts: authentication that is not a person.

The problem
-----------
A departmental integration currently has to log in as somebody. That is wrong three ways
at once. The credential belongs to a human who will eventually leave or change role; the
audit trail attributes machine traffic to that person, so "who queried this plate" has a
misleading answer; and revoking the integration means changing someone's password, which
nobody will do promptly.

A key is a first-class principal: it has a name, a department, a scope set, an expiry, a
revocation, and its own audit identity.

Why SHA-256 and not bcrypt
---------------------------
This is the one place in this codebase where the "always use a slow hash" rule is wrong,
and it is worth stating because it looks like a mistake.

A password is low-entropy -- a person chose it -- so a slow hash is what makes a stolen
database survivable. An API key here is 32 bytes from `secrets.token_urlsafe`: there is
no dictionary to attack and no plausible brute force. What a slow hash *would* add is a
bcrypt round on every single API request, which is a self-inflicted rate limit. The login
endpoint already demonstrates the cost -- `rate_limit.py` exists precisely because bcrypt
on an unauthenticated path lets a caller burn server CPU on demand.

So: SHA-256 over a high-entropy secret, constant-time comparison, and the key shown once
at creation and never again.

Scopes
------
Deliberately coarse. A permission system with forty scopes is one nobody configures
correctly, and the failure mode of a too-fine scope is an integration granted everything
because narrowing it was too fiddly. Five scopes, each corresponding to something a
department actually wants to do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Prefix on every issued key. Makes a leaked key identifiable in a log or a paste --
#: secret scanners key off exactly this kind of marker -- and makes it obvious that a
#: string is a SETU credential rather than an opaque id.
KEY_PREFIX = "setu_sk_"

#: Bytes of entropy in the secret portion. 32 bytes is ~256 bits.
KEY_BYTES = 32

#: Characters of the token stored in the clear, after the prefix. Enough to tell two
#: keys apart in a list or an audit entry; far too few to authenticate with.
VISIBLE_PREFIX_CHARS = 8

#: Default ceiling for a key with none of its own.
DEFAULT_RATE_LIMIT_PER_MINUTE = 120

#: Keys do not live forever by default. An integration that is still wanted gets its key
#: rotated; one that is forgotten expires instead of remaining a live credential
#: indefinitely.
DEFAULT_EXPIRY_DAYS = 365


class Scope(StrEnum):
    """What a key is allowed to do. Coarse on purpose -- see the module docstring."""

    #: Cameras, health, readiness grading, coverage.
    READ_REGISTRY = "read:registry"
    #: Detections, sightings, journeys, candidates, analytics.
    READ_DETECTIONS = "read:detections"
    #: Alerts and the watchlist.
    READ_ALERTS = "read:alerts"
    #: Register and manage webhook subscriptions.
    MANAGE_WEBHOOKS = "manage:webhooks"
    #: Bulk camera and watchlist import.
    WRITE_IMPORT = "write:import"


ALL_SCOPES: tuple[str, ...] = tuple(s.value for s in Scope)


class ApiKeyError(RuntimeError):
    """Any failure here. Messages never contain a token."""


@dataclass(frozen=True)
class IssuedKey:
    """What key creation returns. The token appears here and nowhere else, ever."""

    id: uuid.UUID
    token: str
    key_prefix: str
    name: str
    scopes: tuple[str, ...]
    department_id: uuid.UUID | None
    expires_at: datetime | None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"IssuedKey(id={self.id}, key_prefix={self.key_prefix!r}, token=<shown once>)"


@dataclass(frozen=True)
class KeyPrincipal:
    """An authenticated key, as the request pipeline sees it."""

    id: uuid.UUID
    name: str
    key_prefix: str
    department_id: uuid.UUID | None
    scopes: frozenset[str]
    rate_limit_per_minute: int

    @property
    def is_estate_wide(self) -> bool:
        return self.department_id is None

    #: The identity written into audit entries. A key, not a person -- which is the
    #: entire point of this module.
    @property
    def audit_subject(self) -> str:
        return f"apikey:{self.key_prefix}"

    def has(self, scope: Scope | str) -> bool:
        return str(scope) in self.scopes


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """A new token, its hash, and the portion stored in the clear."""
    secret = secrets.token_urlsafe(KEY_BYTES)
    token = f"{KEY_PREFIX}{secret}"
    return token, _hash(token), secret[:VISIBLE_PREFIX_CHARS]


def issue(
    session: Session,
    *,
    name: str,
    scopes: list[str],
    department_id: uuid.UUID | None,
    created_by: str,
    expires_in_days: int | None = DEFAULT_EXPIRY_DAYS,
    rate_limit_per_minute: int | None = None,
) -> IssuedKey:
    """Create a key. The token is returned once and is not recoverable afterwards."""
    if not name.strip():
        raise ApiKeyError("a key needs a name; it is how a human will recognise it later")

    unknown = sorted(set(scopes) - set(ALL_SCOPES))
    if unknown:
        # Refused rather than ignored. A typo'd scope that is silently dropped produces
        # a key that appears to work until it reaches the endpoint it was meant for.
        raise ApiKeyError(
            f"unknown scopes: {', '.join(unknown)}. Valid scopes: {', '.join(ALL_SCOPES)}"
        )
    if not scopes:
        raise ApiKeyError("a key with no scopes can do nothing; grant at least one")

    token, token_hash, visible = generate_token()
    key_id = uuid.uuid4()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days) if expires_in_days else None
    )

    session.execute(
        text(
            "INSERT INTO api_key (id, name, token_hash, key_prefix, department_id, scopes,"
            " created_by, created_at, expires_at, use_count, rate_limit_per_minute)"
            " VALUES (:id, :name, :hash, :prefix, :dept, CAST(:scopes AS jsonb),"
            " :by, now(), :expires, 0, :rate)"
        ),
        {
            "id": key_id,
            "name": name.strip(),
            "hash": token_hash,
            "prefix": visible,
            "dept": department_id,
            "scopes": json.dumps(sorted(set(scopes))),
            "by": created_by,
            "expires": expires_at,
            "rate": rate_limit_per_minute,
        },
    )
    session.flush()

    return IssuedKey(
        id=key_id,
        token=token,
        key_prefix=visible,
        name=name.strip(),
        scopes=tuple(sorted(set(scopes))),
        department_id=department_id,
        expires_at=expires_at,
    )


def authenticate(session: Session, token: str) -> KeyPrincipal:
    """Resolve a presented token to a principal, or refuse.

    Every refusal is the same shape from the outside. Distinguishing "no such key" from
    "revoked" from "expired" in the response would let a holder of a dead token learn
    whether it ever existed, which is a small leak with no compensating benefit -- the
    legitimate holder of a revoked key already knows, because they were told.
    """
    if not token.startswith(KEY_PREFIX):
        raise ApiKeyError("not a SETU API key")

    row = (
        session.execute(
            text(
                "SELECT id, name, key_prefix, department_id, scopes, expires_at,"
                "       revoked_at, rate_limit_per_minute, token_hash"
                " FROM api_key WHERE token_hash = :hash"
            ),
            {"hash": _hash(token)},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiKeyError("key not recognised")

    # The lookup above is already by hash, so this comparison is belt-and-braces. It is
    # here because a future change to look up by prefix -- which is the obvious
    # optimisation -- would otherwise silently become a timing oracle.
    if not hmac.compare_digest(row["token_hash"], _hash(token)):  # pragma: no cover
        raise ApiKeyError("key not recognised")

    if row["revoked_at"] is not None:
        raise ApiKeyError("key not recognised")
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
        raise ApiKeyError("key not recognised")

    return KeyPrincipal(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        department_id=row["department_id"],
        scopes=frozenset(row["scopes"] or []),
        rate_limit_per_minute=row["rate_limit_per_minute"] or DEFAULT_RATE_LIMIT_PER_MINUTE,
    )


def record_use(session: Session, principal: KeyPrincipal) -> None:
    """Note that a key was used. Best-effort by design.

    `last_used_at` answers "is this integration still running?", which is the question
    that decides whether a key can be retired. It is a counter update on a hot path, so
    a failure here must never fail the request it is attached to -- the request was
    already authorised.
    """
    try:
        session.execute(
            text(
                "UPDATE api_key SET last_used_at = now(), use_count = use_count + 1"
                " WHERE id = :id"
            ),
            {"id": principal.id},
        )
    except Exception:  # noqa: BLE001
        log.warning("could not record use of key %s", principal.key_prefix)


def revoke(session: Session, key_id: uuid.UUID, *, revoked_by: str) -> bool:
    """Turn a key off. Not a delete.

    A revoked key's audit history has to keep resolving to something, and "when was this
    turned off, and by whom" is a question an incident review asks.
    """
    # `Session.execute` is typed as returning `Result`, which has no rowcount; the
    # concrete object for a DML statement is a CursorResult. Same cast as persistence.py.
    result = cast(
        CursorResult[Any],
        session.execute(
            text(
                "UPDATE api_key SET revoked_at = now(), revoked_by = :by"
                " WHERE id = :id AND revoked_at IS NULL"
            ),
            {"id": key_id, "by": revoked_by},
        ),
    )
    session.flush()
    return bool(result.rowcount)


def list_keys(session: Session) -> list[dict[str, Any]]:
    """Every key the caller may see. Never includes a token or a hash."""
    rows = session.execute(
        text(
            "SELECT id, name, key_prefix, department_id, scopes, created_by, created_at,"
            "       expires_at, revoked_at, revoked_by, last_used_at, use_count,"
            "       rate_limit_per_minute"
            " FROM api_key ORDER BY created_at DESC"
        )
    ).mappings()

    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["id"] = str(record["id"])
        record["department_id"] = str(record["department_id"]) if record["department_id"] else None
        # Derived rather than stored, so it cannot go stale.
        record["active"] = record["revoked_at"] is None and (
            record["expires_at"] is None or record["expires_at"] > now
        )
        record["rate_limit_per_minute"] = (
            record["rate_limit_per_minute"] or DEFAULT_RATE_LIMIT_PER_MINUTE
        )
        out.append(record)
    return out


__all__ = [
    "ALL_SCOPES",
    "DEFAULT_EXPIRY_DAYS",
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "KEY_PREFIX",
    "ApiKeyError",
    "IssuedKey",
    "KeyPrincipal",
    "Scope",
    "authenticate",
    "generate_token",
    "issue",
    "list_keys",
    "record_use",
    "revoke",
]
