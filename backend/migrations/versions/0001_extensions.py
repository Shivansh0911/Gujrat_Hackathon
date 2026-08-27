"""Database extensions.

Split into required and optional deliberately.

**Required** extensions carry schema semantics. `postgis` provides the
`geography(Point,4326)` columns and `ST_Distance`, without which the registry cannot
store a camera position at all; `pgcrypto` provides `gen_random_uuid()`, used as a
column default; `pg_trgm` backs the fuzzy plate index. Missing any of these means the
schema cannot be created, so the migration fails with a message naming the extension
rather than with whatever error the next statement happens to raise.

**Optional** extensions are performance or future capability. `timescaledb` turns
`detection` into a hypertable, which changes time-window query performance at scale
and nothing about correctness. `vector` is for the vehicle re-identification
embeddings that are not built yet. A managed Postgres that offers PostGIS but not
these is common -- most platforms' default images are exactly that -- and refusing to
deploy on one would be choosing a nicety over running at all.

Revision ID: 0001_extensions
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0001_extensions"
down_revision = None
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: Without these the schema cannot be created.
REQUIRED_EXTENSIONS = (
    ("postgis", "geography(Point,4326) columns and ST_Distance for route plausibility"),
    ("pgcrypto", "gen_random_uuid() column defaults"),
    ("pg_trgm", "trigram index for fuzzy plate search"),
)

#: Nice to have. Absence is degraded performance or a dormant feature, not a broken one.
OPTIONAL_EXTENSIONS = (
    ("timescaledb", "detection hypertable; without it detection stays a plain table"),
    ("vector", "pgvector embeddings for vehicle re-identification (not yet built)"),
)


def _is_available(conn, name: str) -> bool:
    return bool(
        conn.exec_driver_sql(
            "SELECT 1 FROM pg_available_extensions WHERE name = %s", (name,)
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    for name, why in REQUIRED_EXTENSIONS:
        try:
            conn.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {name}")
        except Exception as exc:  # noqa: BLE001 -- re-raised with context that helps
            raise RuntimeError(
                f"the required PostgreSQL extension '{name}' is unavailable, and SETU "
                f"needs it for {why}. Deploy against an image that provides it -- "
                "timescale/timescaledb-ha:pg16 carries all of them, and is what the "
                "compose stack and the deployment guide use."
            ) from exc

    for name, why in OPTIONAL_EXTENSIONS:
        if not _is_available(conn, name):
            log.warning(
                "optional extension '%s' is not available on this server (%s); "
                "continuing without it.",
                name,
                why,
            )
            continue

        # Available does not guarantee creatable -- timescaledb also needs to be in
        # shared_preload_libraries, for instance. A SAVEPOINT is what makes that
        # survivable: a failed CREATE EXTENSION aborts the transaction it runs in, and
        # this one is Alembic's own. Rolling the whole thing back discards Alembic's
        # bookkeeping along with it, and the migration then dies further downstream
        # with `relation "alembic_version" does not exist` -- which says nothing at all
        # about the extension that actually failed.
        savepoint = conn.begin_nested()
        try:
            conn.exec_driver_sql(f"CREATE EXTENSION IF NOT EXISTS {name}")
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 -- absence is a supported state
            savepoint.rollback()
            log.warning(
                "optional extension '%s' could not be created (%s); continuing " "without it: %s",
                name,
                why,
                exc.__class__.__name__,
            )


def downgrade() -> None:
    # Deliberately ordered opposite to creation. postgis is dropped last because other
    # objects may depend on its types.
    for name, _ in reversed(OPTIONAL_EXTENSIONS + REQUIRED_EXTENSIONS):
        # RESTRICT (the default) rather than CASCADE: if something still depends on an
        # extension, that is a fact the operator needs to see, not one to silently
        # destroy tables over.
        op.execute(f"DROP EXTENSION IF EXISTS {name}")
