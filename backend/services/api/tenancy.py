"""Per-request database session context for row-level security.

The policies added in migration 0005 read two Postgres settings. This module is what
sets them, and the details are load-bearing:

* **`SET LOCAL`, never `SET`.** Connections are pooled. A plain `SET` persists on the
  connection after the transaction ends, so the next request to borrow it inherits the
  previous caller's department. `SET LOCAL` is scoped to the transaction and is
  discarded on commit or rollback. This single word is the difference between RLS that
  isolates tenants and RLS that silently leaks them under load.

* **Parameters are bound, not interpolated.** `SET LOCAL` does not accept bind
  parameters directly, so the value goes through `set_config()`, which does. Building
  the statement by string formatting would put an attacker-influenced value into DDL.

* **Clearing is explicit.** `reset_context` sets the department to a value that
  matches nothing rather than unsetting it, so a code path that forgets to establish
  context sees zero rows instead of inheriting whatever was there.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api.security import Actor

log = logging.getLogger(__name__)

# A UUID that no department will ever have. Used when an actor has no department, so
# the policy evaluates to false rather than to NULL-ish behaviour.
NO_DEPARTMENT = "00000000-0000-0000-0000-000000000000"


def apply_context(session: Session, actor: Actor) -> None:
    """Bind the actor's tenancy to this transaction."""
    department = str(actor.department_id) if actor.department_id else NO_DEPARTMENT
    session.execute(
        text("SELECT set_config('setu.department_id', :dept, true)"),
        {"dept": department},
    )
    session.execute(
        text("SELECT set_config('setu.is_admin', :admin, true)"),
        {"admin": "on" if actor.is_admin else "off"},
    )


def reset_context(session: Session) -> None:
    """Drop tenancy to a value matching nothing. Fail closed, not open."""
    session.execute(
        text("SELECT set_config('setu.department_id', :dept, true)"),
        {"dept": NO_DEPARTMENT},
    )
    session.execute(text("SELECT set_config('setu.is_admin', 'off', true)"))


def set_admin_context(session: Session) -> None:
    """Full-estate access for background jobs: ingest, matching, seeding.

    These run without a human actor and legitimately span departments -- an ANPR
    worker writes detections for whichever camera it is reading. Kept as a named
    function rather than an inline SET so every such elevation is greppable.

    Set SESSION-scoped (`is_local=false`), unlike the per-request context. A batch
    job commits many times, and a transaction-local flag is discarded at each commit:
    the seeding script did exactly that, so its first statements ran elevated and
    everything after the first commit was silently filtered by RLS. The visible
    symptom was `StaleDataError: UPDATE ... expected to update 1 row(s); 0 were
    matched`, which reads as an ORM problem rather than a policy one.

    This is safe here precisely because a background job owns its connection for its
    whole life. It would NOT be safe on a pooled request connection, which is why
    `apply_context` below stays transaction-local.
    """
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("SELECT set_config('setu.department_id', :dept, false)"),
        {"dept": NO_DEPARTMENT},
    )


def department_of(session: Session) -> uuid.UUID | None:
    """The department currently bound to this transaction. For diagnostics."""
    value = session.execute(
        text("SELECT current_setting('setu.department_id', true)")
    ).scalar_one_or_none()
    if not value or value == NO_DEPARTMENT:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
