#!/usr/bin/env python
"""Create the unprivileged application role that row-level security depends on.

Why this exists
---------------
The docker-compose Postgres creates `POSTGRES_USER` as a **superuser**, and a
superuser has `rolbypassrls` set: it ignores every row-level security policy
unconditionally, no matter how the policies are written or whether FORCE is applied.

That was discovered by writing the isolation tests and watching them fail against a
schema whose policies were, in fact, correct. The RLS migration was fine; the
connection was wrong.

Connecting an application as a database superuser is a bad posture on its own terms —
it can drop tables, read every row, and disable the very controls meant to constrain
it. So the fix is not a workaround for the tests, it is the correct arrangement:

  * `setu`     — owns the schema, runs migrations. Superuser, used by Alembic only.
  * `setu_app` — the runtime role. NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE.
                 It holds exactly the DML privileges the API needs and nothing more,
                 so RLS policies actually bind to it.

Idempotent: safe to run repeatedly, and re-running rotates the password to whatever
is currently configured.

Usage:
    python scripts/create_app_role.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from services.common import redact  # noqa: E402
from services.common.paths import ENV_FILE  # noqa: E402

log = logging.getLogger("approle")

APP_ROLE = "setu_app"

# Tables the runtime role reads and writes. Deliberately enumerated rather than
# granted schema-wide: a future table is unreachable until someone decides it should
# be, which is the behaviour that keeps a privilege audit meaningful.
TABLES = (
    "department",
    "site",
    "camera",
    "camera_capability",
    "detection",
    "watchlist_entry",
    "alert",
    "audit_entry",
)

# The audit ledger is append-only for the application. Nothing in the API updates or
# deletes an audit entry, and granting those privileges would make the tamper-evident
# chain defensible only by convention.
APPEND_ONLY = ("audit_entry",)


def _env(key: str) -> str | None:
    """Real environment first, then the project .env file.

    That precedence is not cosmetic. This script runs in two very different places:
    on a developer machine, where configuration lives in .env, and inside a
    container, where there is no .env file at all and everything arrives as real
    environment variables. Reading only the file made the container entrypoint fail
    with "SETU_DATABASE_URL is not configured" while the variable was plainly set --
    and because this step gates row-level security, that failure is the difference
    between a deployment that isolates departments and one that does not.
    """
    value = os.environ.get(key)
    if value:
        return value.strip()
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    redact.install(level=logging.INFO)

    owner_url = _env("SETU_MIGRATION_DATABASE_URL") or _env("SETU_DATABASE_URL")
    if not owner_url:
        log.error("SETU_DATABASE_URL is not configured")
        return 2

    app_password = _env("SETU_APP_DB_PASSWORD")
    if not app_password:
        log.error(
            "SETU_APP_DB_PASSWORD is not set. Generate one with "
            'python -c "import secrets;print(secrets.token_urlsafe(24))" '
            "and add it to .env."
        )
        return 2

    engine = create_engine(owner_url, future=True, isolation_level="AUTOCOMMIT")
    parsed = urlparse(owner_url.replace("postgresql+psycopg", "postgresql"))
    database = (parsed.path or "/setu").lstrip("/")

    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
        ).scalar_one_or_none()

        # CREATE/ALTER ROLE is DDL and does not accept bind parameters, so the
        # password must be embedded as a literal. It is escaped by Postgres itself
        # via quote_literal rather than by hand -- hand-rolled SQL escaping is how
        # injection bugs are written, and the database already knows its own rules.
        quoted = conn.execute(text("SELECT quote_literal(:pw)"), {"pw": app_password}).scalar_one()
        attributes = "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT"

        verb = "ALTER" if exists else "CREATE"
        conn.execute(text(f'{verb} ROLE "{APP_ROLE}" WITH LOGIN PASSWORD {quoted} {attributes}'))
        log.info("%s role %s", "updated" if exists else "created", APP_ROLE)

        conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{APP_ROLE}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}"'))

        # A table that does not exist yet is skipped with a warning rather than
        # aborting. This script is safe to run before every table is migrated, and a
        # hard failure here would crash-loop a container over a table that the next
        # migration is about to create.
        missing: list[str] = []
        for table in TABLES:
            exists = conn.execute(
                text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
            ).scalar_one_or_none()
            if exists is None:
                missing.append(table)
                continue
            if table in APPEND_ONLY:
                conn.execute(text(f'GRANT SELECT, INSERT ON {table} TO "{APP_ROLE}"'))
            else:
                conn.execute(
                    text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{APP_ROLE}"')
                )
        if missing:
            log.warning(
                "not yet migrated, no grant applied: %s. Re-run after `alembic upgrade head`.",
                ", ".join(missing),
            )

        # Sequences backing bigserial columns; without USAGE an INSERT fails on the
        # nextval() rather than on the table, which is a confusing way to find out.
        conn.execute(text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{APP_ROLE}"'))

        # TimescaleDB places hypertable chunks in an internal schema. Without these the
        # role can read `detection` but not the chunks that actually hold its rows.
        #
        # Conditional, because TimescaleDB is optional (see migration 0001): on a
        # Postgres that only offers PostGIS the schema simply does not exist, and
        # granting on it aborts the transaction with `InvalidSchemaName` after the role
        # has already been created -- so the deploy fails at a point that looks nothing
        # like the missing extension that caused it.
        has_timescale = conn.execute(
            text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": "_timescaledb_internal"},
        ).scalar()
        if has_timescale:
            conn.execute(text(f'GRANT USAGE ON SCHEMA _timescaledb_internal TO "{APP_ROLE}"'))
            conn.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                    f'_timescaledb_internal TO "{APP_ROLE}"'
                )
            )
            conn.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA _timescaledb_internal "
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{APP_ROLE}"'
                )
            )
        else:
            log.info(
                "no _timescaledb_internal schema; skipping chunk grants "
                "(detection is a plain table on this server)"
            )

        role = (
            conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"),
                {"r": APP_ROLE},
            )
            .mappings()
            .one()
        )

    engine.dispose()

    # Verified rather than assumed: if either flag were set, every RLS policy in the
    # database would be silently inert for this role.
    if role["rolsuper"] or role["rolbypassrls"]:
        log.error(
            "%s has rolsuper=%s rolbypassrls=%s; row-level security would not bind",
            APP_ROLE,
            role["rolsuper"],
            role["rolbypassrls"],
        )
        return 1

    print(f"\nRole {APP_ROLE} ready: NOSUPERUSER, NOBYPASSRLS, table-scoped grants.")
    print("Point SETU_DATABASE_URL at it and keep the owner URL in")
    print("SETU_MIGRATION_DATABASE_URL for Alembic.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
