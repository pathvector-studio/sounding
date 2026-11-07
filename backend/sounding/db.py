"""Engine and session factory (synchronous SQLAlchemy 2.0 on psycopg 3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from sounding.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url.get_secret_value(), pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, roll back on error."""
    with get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
