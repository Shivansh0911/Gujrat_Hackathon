"""Service-account key management.

Admin-only, and every operation is audited. A key is a credential that can read a
department's detections without a person being involved, so creating one is exactly the
kind of act an incident review needs to be able to reconstruct: who issued it, when, to
which department, with which scopes, and who turned it off.

The token is returned **once**, in the creation response, and is not recoverable
afterwards. That is not a limitation to apologise for -- it is what makes the stored hash
worth storing. An endpoint that could show a key again would make the hash decorative.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.api import api_keys, audit
from services.api.api_keys import ALL_SCOPES, DEFAULT_EXPIRY_DAYS, ApiKeyError
from services.api.db import get_session
from services.api.errors import ApiError, ErrorCode
from services.api.security import AdminActor

router = APIRouter(prefix="/service-keys", tags=["service accounts"])
SessionDep = Annotated[Session, Depends(get_session)]


class CreateKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="How a human will recognise it")
    scopes: list[str] = Field(description=f"Any of: {', '.join(ALL_SCOPES)}")
    department_id: uuid.UUID | None = Field(
        default=None,
        description="Omit for an estate-wide key. Estate-wide keys read every department.",
    )
    expires_in_days: int | None = Field(
        default=DEFAULT_EXPIRY_DAYS,
        ge=1,
        le=3650,
        description="Null for a key that never expires. Prefer rotation over never.",
    )
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)


class CreatedKeyOut(BaseModel):
    """The only response that ever contains a token."""

    id: uuid.UUID
    name: str
    #: Shown once. Store it now; it cannot be retrieved again.
    token: str
    key_prefix: str
    scopes: list[str]
    department_id: uuid.UUID | None
    expires_at: datetime | None
    warning: str


class KeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    department_id: str | None
    scopes: list[str]
    created_by: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    last_used_at: datetime | None
    use_count: int
    rate_limit_per_minute: int
    active: bool


class ScopeOut(BaseModel):
    scope: str
    description: str


_SCOPE_DESCRIPTIONS = {
    "read:registry": "Cameras, health, ANPR readiness grading and coverage analysis.",
    "read:detections": "Plate reads, vehicle sightings, journeys, candidates and analytics.",
    "read:alerts": "Watchlist entries and the alerts raised against them.",
    "manage:webhooks": "Register and manage webhook subscriptions for this department.",
    "write:import": "Bulk camera and watchlist import.",
}

_TOKEN_WARNING = (
    "This token is shown once and cannot be retrieved again. Store it in your secret "
    "manager now. If it is lost, revoke this key and issue another."
)


@router.get("/scopes", response_model=list[ScopeOut])
def available_scopes(actor: AdminActor) -> list[ScopeOut]:
    """What a key can be granted, in words.

    Served rather than only documented, because the person deciding which scopes an
    integration needs is often not the person who reads the repository.
    """
    return [ScopeOut(scope=s, description=_SCOPE_DESCRIPTIONS[s]) for s in ALL_SCOPES]


@router.get("", response_model=list[KeyOut])
def list_service_keys(session: SessionDep, actor: AdminActor) -> list[KeyOut]:
    """Every key, with its usage. Never a token and never a hash.

    `last_used_at` is the column that makes retirement possible: a key nothing has
    presented in six months is one somebody can safely turn off.
    """
    return [KeyOut(**row) for row in api_keys.list_keys(session)]


@router.post("", response_model=CreatedKeyOut, status_code=201)
def create_service_key(body: CreateKeyIn, session: SessionDep, actor: AdminActor) -> CreatedKeyOut:
    """Issue a key. The token is in this response and nowhere else, ever."""
    try:
        issued = api_keys.issue(
            session,
            name=body.name,
            scopes=body.scopes,
            department_id=body.department_id,
            created_by=actor.subject,
            expires_in_days=body.expires_in_days,
            rate_limit_per_minute=body.rate_limit_per_minute,
        )
    except ApiKeyError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc

    # Audited with the prefix, never the token. The prefix is enough to tie a later
    # request back to this issuance and useless for authenticating.
    audit.append(
        session,
        action="ISSUE_API_KEY",
        subject_type="api_key",
        subject_id=str(issued.id),
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=f"service account: {issued.name}",
        detail={
            "key_prefix": issued.key_prefix,
            "scopes": list(issued.scopes),
            "department_id": str(issued.department_id) if issued.department_id else None,
            "expires_at": issued.expires_at.isoformat() if issued.expires_at else None,
        },
    )
    session.flush()

    return CreatedKeyOut(
        id=issued.id,
        name=issued.name,
        token=issued.token,
        key_prefix=issued.key_prefix,
        scopes=list(issued.scopes),
        department_id=issued.department_id,
        expires_at=issued.expires_at,
        warning=_TOKEN_WARNING,
    )


class RevokeOut(BaseModel):
    revoked: bool
    detail: str


@router.delete("/{key_id}", response_model=RevokeOut)
def revoke_service_key(
    key_id: uuid.UUID, session: SessionDep, actor: AdminActor, reason: str = ""
) -> RevokeOut:
    """Turn a key off. Not a delete -- its audit history must keep resolving."""
    revoked = api_keys.revoke(session, key_id, revoked_by=actor.subject)
    if not revoked:
        # Either it does not exist or it was already revoked. Both are "there is
        # nothing live with this id", and the caller's next action is the same.
        raise ApiError(
            ErrorCode.RESOURCE_NOT_FOUND, "no live key with that id; it may already be revoked"
        )

    audit.append(
        session,
        action="REVOKE_API_KEY",
        subject_type="api_key",
        subject_id=str(key_id),
        actor_id=actor.subject,
        actor_role=actor.role,
        purpose=reason or "revoked via the console",
        detail={"reason": reason},
    )
    session.flush()
    return RevokeOut(revoked=True, detail="the key will be refused from the next request")


__all__: list[str] = ["router"]
