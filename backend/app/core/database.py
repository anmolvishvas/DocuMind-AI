"""Async SQLAlchemy engine, session factory and FastAPI session dependency.

The engine and session factory are created lazily, once per process, from
:func:`app.core.config.get_settings`. Importing this module never connects to
the database or reads configuration. Database URLs (which contain credentials)
are never logged or included in raised error messages.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        try:
            _engine = create_async_engine(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_pre_ping=True,
            )
        except ArgumentError:
            # SQLAlchemy may echo the URL in its message; do not leak credentials.
            msg = "Invalid database configuration: DATABASE_URL could not be parsed."
            raise RuntimeError(msg) from None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory bound to :func:`get_engine`."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,  # keep attributes readable after commit in async code
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session for one unit of work.

    Commits when the caller finishes normally, rolls back and re-raises on any
    exception, and always closes the session (returning its connection to the pool).
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close all pooled connections and reset state. Safe to call repeatedly."""
    global _engine, _session_factory
    engine, _engine, _session_factory = _engine, None, None
    if engine is not None:
        await engine.dispose()
