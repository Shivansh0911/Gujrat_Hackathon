"""Database session management."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services.api.config import get_api_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_api_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,  # a connection idle across a Postgres restart is reaped
        pool_size=10,
        max_overflow=20,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request.

    Commit on success, roll back on any exception. This is what makes the audit
    guarantee hold -- an audit entry and the mutation it records share a transaction,
    so a failure cannot leave a change recorded but unaudited, or audited but unmade.
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
