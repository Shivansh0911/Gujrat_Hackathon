"""Service API keys, webhook subscriptions and their deliveries, idempotency records.

Four tables, one migration, because they are one capability: the surface another
department's system integrates against without ever holding a human's credentials.

`api_key` -- authentication that is not a person
-------------------------------------------------
A departmental integration currently has to log in as somebody. That is wrong in three
ways at once: the credential belongs to a human who will leave, the audit trail
attributes machine traffic to a person, and revoking the integration means changing
someone's password.

Keys are stored as a SHA-256 hash and never in plaintext. Not bcrypt, deliberately,
and this is the one place that argument goes the other way: a password is low-entropy
and needs a slow hash to survive a dictionary attack, while these keys are 32 bytes of
`secrets.token_urlsafe` and a dictionary attack on them is not a thing. A slow hash on
every API request would instead be a self-inflicted rate limit -- and the login endpoint
already demonstrates how expensive that is (see `rate_limit.py`).

`key_prefix` is stored in the clear so a key can be identified in a list and in an audit
entry without being reconstructable. Eight characters of the token: enough to tell two
keys apart, useless for authenticating.

`webhook_subscription` and `webhook_delivery`
----------------------------------------------
Another department's control room should learn about an alert without polling for it.
Deliveries are a separate table rather than a queue in memory because the useful
properties -- at-least-once, exponential backoff, a dead-letter view, manual replay --
are all properties of *durable rows*. An in-process queue loses everything on the
restart that a failing receiver is most likely to coincide with.

Every attempt is recorded, not just the final outcome. "It was delivered" and "it was
delivered on the fourth attempt after two timeouts" are different operational facts, and
only the second one tells a department their endpoint is unhealthy.

`idempotency_key`
------------------
A retried request must not double-write. The stored response is returned verbatim on a
replay, so a client that retried because it never saw the first answer gets that answer
rather than a conflict -- which is the entire point, and the thing a naive
"reject duplicates" implementation gets backwards.

The scope is (key, endpoint, actor): the same key from a different caller is a different
request, and the same key against a different endpoint is a client bug worth surfacing
rather than silently serving the wrong cached body.

Revision ID: 0009_integration_surface
Revises: 0008_sighting_pass_extent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_integration_surface"
down_revision = "0008_sighting_pass_extent"
branch_labels = None
depends_on = None

_ADMIN = "current_setting('setu.is_admin', true) = 'on'"
_DEPT = "current_setting('setu.department_id', true)"


def upgrade() -> None:
    # --- api_key -------------------------------------------------------------------
    op.create_table(
        "api_key",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        # SHA-256 of the token. See the module docstring for why not bcrypt.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        # The first characters of the token, in the clear. Identifies a key in a list
        # or an audit entry; useless for authenticating.
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        # A key belongs to a department. NULL means estate-wide, which only an admin
        # can create and which the console marks as such.
        sa.Column("department_id", sa.UUID(), nullable=True),
        # Permission set, as a sorted JSON array of scope strings. An array rather than
        # a bitfield so a new scope does not need a migration and an audit entry reads
        # in words.
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Revocation is a timestamp, not a delete. A revoked key's audit history must
        # keep resolving to something, and "when was this turned off" is a question an
        # incident review asks.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=120), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.BigInteger(), nullable=False, server_default="0"),
        # Per-key ceiling. NULL uses the platform default; a department running a bulk
        # import legitimately needs more than one polling a dashboard.
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "rate_limit_per_minute IS NULL OR rate_limit_per_minute > 0",
            name="ck_api_key_rate_positive",
        ),
        sa.CheckConstraint("jsonb_typeof(scopes) = 'array'", name="ck_api_key_scopes_array"),
        sa.ForeignKeyConstraint(["department_id"], ["department.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_api_key_token_hash"),
    )
    op.create_index("ix_api_key_prefix", "api_key", ["key_prefix"], unique=False)
    # Partial: authentication only ever looks for live keys, and most of a long-lived
    # estate's keys will eventually be revoked.
    op.execute(
        "CREATE INDEX ix_api_key_active ON api_key (token_hash) WHERE revoked_at IS NULL"
    )

    # --- webhook_subscription -------------------------------------------------------
    op.create_table(
        "webhook_subscription",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        # Shared secret for the HMAC signature. Encrypted with the same scheme as
        # camera credentials; never returned by any endpoint.
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("secret_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("department_id", sa.UUID(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # Consecutive failures. A subscriber that has been failing for a long time is
        # disabled rather than retried forever, and this is what that decision reads.
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(event_types) = 'array'", name="ck_webhook_events_array"
        ),
        sa.ForeignKeyConstraint(["department_id"], ["department.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_active", "webhook_subscription", ["active", "department_id"], unique=False
    )

    # --- webhook_delivery -----------------------------------------------------------
    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        # The event's own id, so a receiver can deduplicate. At-least-once delivery
        # means a receiver WILL see a duplicate eventually, and giving them the means
        # to notice is part of the contract rather than their problem.
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        # pending | delivering | delivered | failed | dead
        sa.Column("state", sa.String(length=16), nullable=False, server_default="'pending'"),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Every attempt, not just the last outcome. "Delivered on the fourth attempt
        # after two timeouts" is the fact that tells a department their endpoint is sick.
        sa.Column("attempt_log", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','delivering','delivered','failed','dead')",
            name="ck_delivery_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_delivery_attempts"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["webhook_subscription.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The dispatcher's only query: what is due now. Partial, because delivered rows
    # vastly outnumber pending ones and are never selected by it.
    op.execute(
        "CREATE INDEX ix_delivery_due ON webhook_delivery (next_attempt_at) "
        "WHERE state IN ('pending','failed')"
    )
    op.create_index(
        "ix_delivery_subscription",
        "webhook_delivery",
        ["subscription_id", "created_at"],
        unique=False,
    )

    # --- idempotency_key ------------------------------------------------------------
    op.create_table(
        "idempotency_key",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        # Hash of the request body. A client reusing a key with a *different* body is a
        # bug, and returning the first response for it would hide the bug and the
        # second request's effect at once.
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # The scope. The same key from a different caller is a different request; the
        # same key against a different endpoint is a client bug worth surfacing.
        sa.UniqueConstraint("key", "endpoint", "actor", name="uq_idempotency_scope"),
    )
    op.create_index("ix_idempotency_expiry", "idempotency_key", ["expires_at"], unique=False)

    # --- Row-level security ---------------------------------------------------------
    # api_key, webhook_subscription and webhook_delivery all carry department scope and
    # must honour it, for the same reason `camera` does: an operator in one department
    # must not enumerate another's integrations. A NULL department_id is estate-wide
    # and visible only to an admin -- note `department_id IS NULL` is deliberately NOT
    # in the non-admin branch.
    for table in ("api_key", "webhook_subscription"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_department_isolation ON {table}
            USING ({_ADMIN} OR department_id::text = {_DEPT})
            WITH CHECK ({_ADMIN} OR department_id::text = {_DEPT})
            """
        )

    op.execute("ALTER TABLE webhook_delivery ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_delivery FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY webhook_delivery_department_isolation ON webhook_delivery
        USING (
            {_ADMIN}
            OR EXISTS (
                SELECT 1 FROM webhook_subscription s
                WHERE s.id = webhook_delivery.subscription_id
                  AND s.department_id::text = {_DEPT}
            )
        )
        WITH CHECK (
            {_ADMIN}
            OR EXISTS (
                SELECT 1 FROM webhook_subscription s
                WHERE s.id = webhook_delivery.subscription_id
                  AND s.department_id::text = {_DEPT}
            )
        )
        """
    )

    # idempotency_key is deliberately NOT department-scoped. It is keyed by actor, which
    # is already narrower, and a cross-department read of it would reveal only that some
    # actor used some key against some endpoint -- while scoping it would break the
    # replay guarantee for an estate-wide service account.


def downgrade() -> None:
    for table in ("api_key", "webhook_subscription", "webhook_delivery"):
        op.execute(f"DROP POLICY IF EXISTS {table}_department_isolation ON {table}")

    op.drop_index("ix_idempotency_expiry", table_name="idempotency_key")
    op.drop_table("idempotency_key")

    op.execute("DROP INDEX IF EXISTS ix_delivery_due")
    op.drop_index("ix_delivery_subscription", table_name="webhook_delivery")
    op.drop_table("webhook_delivery")

    op.drop_index("ix_webhook_active", table_name="webhook_subscription")
    op.drop_table("webhook_subscription")

    op.execute("DROP INDEX IF EXISTS ix_api_key_active")
    op.drop_index("ix_api_key_prefix", table_name="api_key")
    op.drop_table("api_key")
