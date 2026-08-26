"""Department-scoped row-level security.

The HLD claims three independent levels of tenant isolation: a gateway policy check,
scoped accessors in the application, and row-level security in the database. The
first two existed; this is the third, and until now the document asserted something
the schema did not do.

The point of the third level is that it does not depend on the other two. Scoped
accessors are correct only while every query goes through them; one `session.query`
written in a hurry, one reporting script, one debugging endpoint, and a department
boundary is crossed. RLS is enforced by Postgres on every statement regardless of
which code path issued it, so an application defect stops being a cross-department
breach.

How the session context is set
------------------------------
Each request sets two GUCs on its connection before touching data:

    SET LOCAL setu.department_id = '<uuid>'
    SET LOCAL setu.is_admin      = 'on' | 'off'

`SET LOCAL` scopes them to the transaction, so a pooled connection cannot leak one
request's identity into the next -- which is the failure mode that makes naive
connection-pooled RLS worse than none.

Why `current_setting(..., true)`
--------------------------------
The second argument returns NULL instead of raising when the setting is absent. A
connection that has not been given an identity therefore matches nothing rather than
erroring, so the failure mode of forgetting to set the context is "no rows", not
"every row".

Postgres note: the table owner bypasses RLS unless FORCE is used. The application
role owns these tables, so `FORCE ROW LEVEL SECURITY` is required or the policies
would be silently inert for exactly the role that matters.

Revision ID: 0005_row_level_security
Revises: 0004_watchlist_alerts
"""

from __future__ import annotations

from alembic import op

revision = "0005_row_level_security"
down_revision = "0004_watchlist_alerts"
branch_labels = None
depends_on = None

# Tables carrying a department boundary. `detection` and `alert` reach it through
# `camera`, so their policies join rather than reading a local column -- denormalising
# department_id onto them would create a second source of truth that can disagree.
DIRECT = ("camera", "site")
VIA_CAMERA = ("detection", "alert")

_ADMIN = "current_setting('setu.is_admin', true) = 'on'"
_DEPT = "current_setting('setu.department_id', true)"


def upgrade() -> None:
    for table in DIRECT + VIA_CAMERA:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Without FORCE the owning role bypasses every policy below.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    for table in DIRECT:
        op.execute(
            f"""
            CREATE POLICY {table}_department_isolation ON {table}
            USING (
                {_ADMIN}
                OR department_id::text = {_DEPT}
            )
            WITH CHECK (
                {_ADMIN}
                OR department_id::text = {_DEPT}
            )
            """
        )

    for table in VIA_CAMERA:
        op.execute(
            f"""
            CREATE POLICY {table}_department_isolation ON {table}
            USING (
                {_ADMIN}
                OR EXISTS (
                    SELECT 1 FROM camera c
                    WHERE c.id = {table}.camera_id
                      AND c.department_id::text = {_DEPT}
                )
            )
            WITH CHECK (
                {_ADMIN}
                OR EXISTS (
                    SELECT 1 FROM camera c
                    WHERE c.id = {table}.camera_id
                      AND c.department_id::text = {_DEPT}
                )
            )
            """
        )


def downgrade() -> None:
    for table in DIRECT + VIA_CAMERA:
        op.execute(f"DROP POLICY IF EXISTS {table}_department_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
