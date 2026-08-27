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
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.registry.enums import Role
from services.registry.models import Camera

# bcrypt: deliberate, adaptive, and the cost factor can be raised later without
# invalidating stored hashes.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

ALGORITHM = "HS256"


class Actor:
    """The authenticated caller. Passed to every scoped accessor."""

    def __init__(self, subject: str, role: str, department_id: uuid.UUID | None) -> None:
        self.subject = subject
        self.role = role
        self.department_id = department_id

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Actor(subject={self.subject!r}, role={self.role!r})"


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
            options={"require_exp": True, "require_sub": True, "verify_aud": False},
        )
    except JWTError as exc:
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
