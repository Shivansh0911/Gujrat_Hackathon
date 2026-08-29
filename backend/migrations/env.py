"""Alembic environment.

The database URL comes from SETU_DATABASE_URL at runtime and is never written into
alembic.ini -- that file is committed, and a connection string in it would be a
credential in source history.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.registry.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations run as the schema OWNER, not as the runtime role. The app role is
# deliberately unable to create tables or alter policies.
_url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ.get("SETU_DATABASE_URL")
if not _url:
    # Fall back to .env so `alembic upgrade head` works the same way `make migrate`
    # does, without requiring the caller to export the variable by hand.
    from services.common.paths import ENV_FILE

    env_file = ENV_FILE
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
        for key in ("SETU_MIGRATION_DATABASE_URL", "SETU_DATABASE_URL"):
            for line in lines:
                if line.startswith(f"{key}="):
                    _url = line.split("=", 1)[1].strip()
                    break
            if _url:
                break
if not _url:
    raise RuntimeError("SETU_DATABASE_URL is not set; cannot run migrations")

from services.common.dburl import normalise_pg_url  # noqa: E402

_url = normalise_pg_url(_url)

# `%` is doubled because set_main_option writes into a ConfigParser that performs
# `%`-interpolation. A percent-encoded password -- which is what you get the moment a
# platform-generated superuser password contains `/`, `@` or `+` -- otherwise fails
# with `ValueError: invalid interpolation syntax`, during migrations, on first deploy.
# It never appears locally: the generated development passwords are token_urlsafe,
# whose alphabet needs no encoding at all.
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = Base.metadata

# PostGIS, TimescaleDB and pgvector create objects in the database that we do not
# manage and must never try to drop. Without these filters, `--autogenerate` proposes
# dropping spatial_ref_sys and the extensions' internal tables on every run.
_IGNORED_TABLES = {"spatial_ref_sys", "geometry_columns", "geography_columns"}
_IGNORED_SCHEMAS = {
    "tiger",
    "tiger_data",
    "topology",
    "_timescaledb_internal",
    "_timescaledb_catalog",
    "_timescaledb_config",
    "_timescaledb_cache",
    "timescaledb_information",
    "timescaledb_experimental",
}


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table":
        if name in _IGNORED_TABLES:
            return False
        if getattr(obj, "schema", None) in _IGNORED_SCHEMAS:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
