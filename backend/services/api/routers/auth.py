"""Authentication. Interim JWT; Keycloak/OIDC replaces this later."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api import audit
from services.api.config import ApiSettings, get_api_settings
from services.api.db import get_session
from services.api.schemas import Token
from services.api.security import CurrentActor, create_access_token, hash_password, verify_password
from services.registry.enums import Role
from services.registry.models import Department

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[Session, Depends(get_session)]


def _demo_users(settings: ApiSettings) -> dict[str, tuple[str, str]]:
    """Operator accounts, from configuration.

    Credentials come from the settings object, which reads the environment and .env,
    so there is one source of truth rather than two half-honoured ones. There is no
    fallback default password, because a fallback is the credential that survives
    into production.
    """
    users: dict[str, tuple[str, str]] = {}
    for password, username, role in (
        (settings.admin_password, "admin", Role.ADMIN.value),
        (settings.operator_password, "operator", Role.OPERATOR.value),
    ):
        if password:
            users[username] = (hash_password(password), role)
    return users


_USER_CACHE: dict[str, tuple[str, str]] | None = None


def _users(settings: ApiSettings) -> dict[str, tuple[str, str]]:
    # Hashing with bcrypt is deliberately slow, so the result is cached rather than
    # recomputed on every login attempt.
    global _USER_CACHE
    if _USER_CACHE is None:
        _USER_CACHE = _demo_users(settings)
    return _USER_CACHE


# A hash to verify against when the user does not exist, so a failed login costs the
# same time either way and cannot be used to enumerate valid usernames.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def _audit_in_new_transaction(*, action: str, subject_id: str, detail: dict[str, Any]) -> None:
    """Commit one audit entry independently of the request transaction."""
    from services.api.db import get_sessionmaker

    audit_session = get_sessionmaker()()
    try:
        audit.append(
            audit_session,
            action=action,
            subject_type="user",
            subject_id=subject_id,
            actor_id=subject_id,
            detail=detail,
        )
        audit_session.commit()
    except Exception:
        audit_session.rollback()
        # Never let an audit failure mask the authentication failure the caller must
        # still receive; the exception is logged by the handler above.
        log.exception("failed to record %s for subject %s", action, subject_id)
    finally:
        audit_session.close()


@router.post("/login", response_model=Token)
def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    settings: Annotated[ApiSettings, Depends(get_api_settings)],
) -> Token:
    users = _users(settings)
    if not users:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no accounts configured; set SETU_ADMIN_PASSWORD and "
            "SETU_OPERATOR_PASSWORD in the environment",
        )

    record = users.get(form.username)
    hashed, role = record if record else (_DUMMY_HASH, Role.OPERATOR.value)
    ok = verify_password(form.password, hashed)

    if not record or not ok:
        # Written in its own committed transaction. The request session is rolled
        # back when the 401 propagates, which would silently discard this entry --
        # an audit trail that loses failed authentication attempts is worse than
        # none, because it looks complete while omitting exactly the events an
        # investigator needs.
        _audit_in_new_transaction(
            action="LOGIN_FAILED",
            subject_id=form.username[:200],
            detail={"reason": "invalid credentials"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Admins are unscoped; an operator could be bound to a department here once
    # per-user department assignment exists (T1.5 / Keycloak).
    department_id = None
    if role != Role.ADMIN.value:
        department_id = session.execute(
            select(Department.id).where(Department.code == "HOME")
        ).scalar_one_or_none()

    token = create_access_token(form.username, role, settings, department_id)
    audit.append(
        session,
        action="LOGIN_SUCCEEDED",
        subject_type="user",
        subject_id=form.username,
        actor_id=form.username,
        actor_role=role,
        detail={"role": role},
    )
    return Token(
        access_token=token,
        role=role,
        expires_in_s=settings.access_token_ttl_min * 60,
    )


@router.get("/me")
def whoami(actor: CurrentActor) -> dict[str, str | None]:
    return {
        "subject": actor.subject,
        "role": actor.role,
        "department_id": str(actor.department_id) if actor.department_id else None,
    }
