"""Authentication, authorisation and scoped access.

Three rules the rest of the API depends on:

1. **`alg=none` is rejected explicitly.** The classic JWT bypass is a token whose
   header claims no algorithm; a library configured loosely will accept it and treat
   an unsigned, attacker-authored token as valid. We pin the permitted algorithm and
   reject the header before verification.

2. **No bare primary-key lookup in a route handler.** Every object is fetched through
   a scoped accessor that takes the actor. This is what prevents IDOR: a handler
   cannot accidentally return a camera outside the caller's department, because it
   never has the option of an unscoped query.

3. **Passwords are hashed with bcrypt**, never compared as plaintext, and the login
   path takes the same time whether or not the user exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
# PyJWT, not python-jose.
#
# GHSA-3qf3-8w2g-rqmx is a critical algorithm-confusion bypass in python-jose: a
# DER-encoded public key gets past the guard that is supposed to stop a token
# declaring its own algorithm. It affects every version up to and including 3.5.0 and
# there is no patched release -- the advisory's fixed version is null. An
# unreachable-code argument does not apply here the way it did to the earlier `ecdsa`
# advisory: this one is in the verification path, which is the path we are on.
#
# SECURITY.md already named this swap as the first security task after submission.
# PyJWT also drops the transitive `ecdsa` dependency that the other suppression
# existed for, so both advisories go with one change.
import jwt
from jwt import PyJWTError

# passlib 1.7.4 reads `bcrypt.__about__.__version__` purely to log which backend it
# found. bcrypt 4.1 removed `__about__`, so that lookup raises, passlib catches it, and
# every process start prints a red AttributeError traceback under the heading
# "(trapped) error reading bcrypt version".
#
# It is cosmetic -- hashing and verification are untouched, and both are exercised on
# every login -- but a traceback in a deployment log is a thing an operator has to rule
# out, and one that appears on every boot trains them to ignore logs that matter.
#
# The shim tells passlib the version bcrypt already reports through the modern
# attribute. Pinning bcrypt below 4.1 would also silence it and would be the wrong
# trade: a cosmetic log line is not worth an older crypto library.
import bcrypt as _bcrypt

if not hasattr(_bcrypt, "__about__"):  # pragma: no cover - depends on the installed pin

    class _About:
        __version__ = getattr(_bcrypt, "__version__", "unknown")

    _bcrypt.__about__ = _About  # type: ignore[attr-defined]

from passlib.context import CryptContext
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from services.api.api_keys import KEY_PREFIX as API_KEY_PREFIX
from services.api.config import ApiSettings, get_api_settings
from services.api.errors import ApiError, ErrorCode
from services.api.rate_limit import key_limiter
from services.api.db import get_session
from services.registry.enums import Role
from services.registry.models import Camera

# bcrypt: deliberate, adaptive, and the cost factor can be raised later without
# invalidating stored hashes.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

ALGORITHM = "HS256"


class Actor:
    """The authenticated caller. Passed to every scoped accessor.

    A caller is either a person holding a JWT or a service account holding an API key.
    Both resolve to an Actor so every scoped accessor, every RLS binding and every audit
    entry works unchanged -- but `api_key_id` is set for one and not the other, so the
    ledger can tell a department's integration apart from a person at a keyboard.
    """

    def __init__(
        self,
        subject: str,
        role: str,
        department_id: uuid.UUID | None,
        *,
        api_key_id: uuid.UUID | None = None,
        scopes: frozenset[str] | None = None,
    ) -> None:
        self.subject = subject
        self.role = role
        self.department_id = department_id
        self.api_key_id = api_key_id
        # None for a human: a JWT session is not scope-limited, and an empty set would
        # read as "permitted nothing" at every scope check.
        self.scopes = scopes

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    @property
    def is_service_account(self) -> bool:
        return self.api_key_id is not None

    @property
    def sees_all_departments(self) -> bool:
        """Whether row-level security should show this caller the whole estate.

        **Row visibility and endpoint authorisation are different questions**, and
        conflating them is how an estate-wide service key ended up seeing nothing.

        `is_admin` answers "may this caller reach admin-only endpoints". It is the
        wrong input for RLS, because an estate-wide key is deliberately *not* an admin
        -- it must never reach a mutating admin endpoint -- while its entire purpose is
        to read across departments. Deriving visibility from the role meant such a key
        bound NO_DEPARTMENT and every query returned zero rows, silently, which reads
        as "there is no data" rather than "you cannot see it".

        So visibility is its own property. A human admin has it by role. A service key
        has it when it was issued without a department, which is an explicit act an
        admin performed and the audit ledger recorded. Everyone else, including a human
        operator with no department, fails closed exactly as before.
        """
        if self.is_admin:
            return True
        return self.is_service_account and self.department_id is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "key" if self.is_service_account else "user"
        return f"Actor({kind} subject={self.subject!r}, role={self.role!r})"


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(
    subject: str,
    role: str,
    settings: ApiSettings,
    department_id: uuid.UUID | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_min),
        "dept": str(department_id) if department_id else None,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, settings: ApiSettings) -> dict[str, Any]:
    """Decode and verify. Raises HTTPException(401) on anything suspicious."""
    unauthorised = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Inspect the header before verifying. An `alg` of "none" (in any casing) is
        # an unsigned token; python-jose would raise anyway, but rejecting it here
        # makes the defence explicit and testable rather than incidental.
        header = jwt.get_unverified_header(token)
        if str(header.get("alg", "")).lower() in ("none", ""):
            raise unauthorised
        if header.get("alg") != ALGORITHM:
            raise unauthorised

        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],  # allowlist, never the token's own claim
            issuer=settings.jwt_issuer,
            # PyJWT spells the required-claim check as a list rather than as
            # `require_exp` / `require_sub` flags. Getting this wrong fails open --
            # a token with no `exp` would simply never expire -- so it is covered by
            # a test rather than left to the reader.
            options={"require": ["exp", "sub"], "verify_aud": False},
        )
    except PyJWTError as exc:
        raise unauthorised from exc


# --------------------------------------------------------------- FastAPI wiring


def get_current_actor(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
    session: Annotated[Session, Depends(get_session)] = None,  # type: ignore[assignment]
) -> Actor:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # A service account presents its key through the same Authorization: Bearer header
    # a person's session uses. The prefix tells them apart, which is one of the reasons
    # issued keys carry one.
    if token.startswith(API_KEY_PREFIX):
        actor = _actor_from_api_key(token, session)
    else:
        claims = decode_token(token, settings)
        dept = claims.get("dept")
        actor = Actor(
            subject=str(claims["sub"]),
            role=str(claims.get("role", Role.OPERATOR.value)),
            department_id=uuid.UUID(dept) if dept else None,
        )

    # Bind tenancy to this transaction before any route touches data, so row-level
    # security applies even to a query that bypassed the scoped accessors. Done here
    # rather than per-route because a route can forget; a dependency cannot.
    if session is not None:
        from services.api.tenancy import apply_context

        apply_context(session, actor)
    return actor


CurrentActor = Annotated[Actor, Depends(get_current_actor)]


def _actor_from_api_key(token: str, session: Session | None) -> Actor:
    """Resolve a service-account key to an Actor, enforcing its own rate ceiling.

    A key is never an admin. Estate-wide reach is expressed by a NULL department, which
    RLS already understands; granting the admin *role* as well would hand it every
    mutating endpoint a human admin can reach, which is not what estate-wide read access
    is meant to mean.
    """
    if session is None:  # pragma: no cover - the dependency always provides one
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "database unavailable")

    from services.api import api_keys
    from services.api.tenancy import set_admin_context

    # The key table is RLS-protected and the caller has no identity yet -- this is the
    # request that establishes one. Elevating for the lookup is the only way to read it,
    # and the lookup is by token hash, so it cannot return another department's key.
    set_admin_context(session)
    try:
        principal = api_keys.authenticate(session, token)
    except api_keys.ApiKeyError as exc:
        raise ApiError(ErrorCode.CREDENTIALS_INVALID, str(exc)) from exc

    if not key_limiter.allow(str(principal.id), limit=principal.rate_limit_per_minute):
        raise ApiError(
            ErrorCode.RATE_LIMITED,
            f"this key is limited to {principal.rate_limit_per_minute} requests per minute",
            retry_after_s=60,
        )

    api_keys.record_use(session, principal)
    return Actor(
        subject=principal.audit_subject,
        role=Role.OPERATOR.value,
        department_id=principal.department_id,
        api_key_id=principal.id,
        scopes=principal.scopes,
    )


def require_scope(scope: str) -> Any:
    """Dependency factory: this endpoint needs this scope from a service account.

    A human session has `scopes is None` and passes every check. Deliberate -- scopes
    exist to narrow a machine integration, and retrofitting them onto human roles would
    be a second, parallel authorisation system saying different things about one person.
    """

    def _check(actor: CurrentActor) -> Actor:
        if actor.scopes is not None and scope not in actor.scopes:
            raise ApiError(
                ErrorCode.PERMISSION_DENIED,
                f"this API key does not carry the '{scope}' scope",
                detail={"required_scope": scope, "granted": sorted(actor.scopes)},
            )
        return actor

    return Depends(_check)


def require_admin(actor: CurrentActor) -> Actor:
    if not actor.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this operation requires the admin role",
        )
    return actor


AdminActor = Annotated[Actor, Depends(require_admin)]


# ------------------------------------------------------------- scoped accessors


def camera_scope(actor: Actor) -> Select[tuple[Camera]]:
    """A SELECT over cameras the actor is permitted to see.

    Every camera read in the API starts here. An operator scoped to a department
    sees only that department's cameras; an admin sees the estate. Route handlers
    never build their own camera query, so there is no path that forgets the filter.
    """
    stmt = select(Camera)
    if not actor.is_admin and actor.department_id is not None:
        stmt = stmt.where(Camera.department_id == actor.department_id)
    return stmt


def get_camera_or_404(session: Session, actor: Actor, camera_id: uuid.UUID) -> Camera:
    """Fetch one camera within the actor's scope.

    Returns 404 -- not 403 -- for a camera outside scope. A 403 would confirm that
    the id exists, letting a caller enumerate the estate they cannot see.
    """
    camera = session.execute(camera_scope(actor).where(Camera.id == camera_id)).scalar_one_or_none()
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="camera not found")
    return camera
