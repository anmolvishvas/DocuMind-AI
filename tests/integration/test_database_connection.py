"""Integration tests for the async database layer (TASK-104).

Tests marked ``requires_postgres`` need a real PostgreSQL server and are skipped
unless ``TEST_DATABASE_URL`` (``postgresql+asyncpg://...``) is set. They never fall
back to SQLite or to ``DATABASE_URL``. The remaining tests need no server: engine
creation is lazy, and session lifecycle is verified with a recording session.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Self

import pytest
import pytest_asyncio
from app.core import database
from app.core.config import get_settings
from app.core.database import dispose_engine, get_db, get_engine, get_session_factory
from app.models.base import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

OFFLINE_URL = "postgresql+asyncpg://user:offline-secret-pw@localhost:1/none"
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

# Explicit marks: pytest config lives in backend/pyproject.toml, not at the repo root.
requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL (a disposable PostgreSQL) is not set"
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    for name in list(os.environ):
        if name.startswith(("DB_", "DATABASE_URL", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no real .env is picked up
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "2")
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def offline_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[None]:
    _configure(monkeypatch, tmp_path, OFFLINE_URL)
    await dispose_engine()
    yield
    await dispose_engine()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def pg_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    assert TEST_DB_URL
    _configure(monkeypatch, tmp_path, TEST_DB_URL)
    await dispose_engine()
    yield
    await dispose_engine()
    get_settings.cache_clear()


class RecordingSession:
    """Stands in for AsyncSession and records lifecycle calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __aenter__(self) -> Self:
        self.calls.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.calls.append("close")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> RecordingSession:
    session = RecordingSession()
    monkeypatch.setattr(database, "get_session_factory", lambda: lambda: session)
    return session


# ----------------------------------------------------------- no server needed


async def test_engine_and_factory_initialise_once(offline_db: None) -> None:
    engine = get_engine()
    factory = get_session_factory()
    assert isinstance(engine, AsyncEngine)
    assert isinstance(factory, async_sessionmaker)
    assert get_engine() is engine
    assert get_session_factory() is factory
    assert factory.kw["bind"] is engine
    assert engine.pool.size() == 3  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == 2  # type: ignore[attr-defined]


async def test_invalid_url_error_does_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, OFFLINE_URL)

    def fail(*_: object, **__: object) -> None:
        raise database.ArgumentError(OFFLINE_URL)

    monkeypatch.setattr(database, "create_async_engine", fail)
    await dispose_engine()
    with pytest.raises(RuntimeError) as info:
        get_engine()
    assert "offline-secret-pw" not in str(info.value)
    get_settings.cache_clear()


async def test_dependency_commits_and_closes_on_success(
    recording: RecordingSession,
) -> None:
    gen = get_db()
    assert await anext(gen) is recording
    assert recording.calls == ["enter"]
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert recording.calls == ["enter", "commit", "close"]


async def test_dependency_rolls_back_and_closes_on_exception(
    recording: RecordingSession,
) -> None:
    gen = get_db()
    await anext(gen)
    with pytest.raises(ValueError, match="boom"):
        await gen.athrow(ValueError("boom"))
    assert recording.calls == ["enter", "rollback", "close"]


async def test_dispose_engine_resets_state_and_is_idempotent(offline_db: None) -> None:
    first = get_engine()
    await dispose_engine()
    assert database._engine is None
    assert database._session_factory is None
    assert get_engine() is not first
    await dispose_engine()
    await dispose_engine()


async def test_base_metadata_uses_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


# --------------------------------------------------------- real PostgreSQL


@pytest_asyncio.fixture
async def scratch_table(pg_db: None) -> AsyncIterator[str]:
    name = "task104_scratch"
    async with get_engine().begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await conn.execute(text(f"CREATE TABLE {name} (id integer PRIMARY KEY)"))
    yield name
    async with get_engine().begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


async def _count(name: str) -> int:
    async with get_session_factory()() as s:
        return int((await s.execute(text(f"SELECT count(*) FROM {name}"))).scalar_one())


@requires_postgres
async def test_pg_session_is_active_and_closes(pg_db: None) -> None:
    gen = get_db()
    session = await anext(gen)
    assert isinstance(session, AsyncSession)
    assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    assert session.in_transaction()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert not session.in_transaction()
    assert get_engine().pool.checkedout() == 0  # type: ignore[attr-defined]


@requires_postgres
async def test_pg_success_commits_and_exception_rolls_back(scratch_table: str) -> None:
    gen = get_db()
    session = await anext(gen)
    await session.execute(text(f"INSERT INTO {scratch_table} VALUES (1)"))
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert await _count(scratch_table) == 1

    gen = get_db()
    session = await anext(gen)
    await session.execute(text(f"INSERT INTO {scratch_table} VALUES (2)"))
    with pytest.raises(RuntimeError, match="fail"):
        await gen.athrow(RuntimeError("fail"))
    assert await _count(scratch_table) == 1
    assert get_engine().pool.checkedout() == 0  # type: ignore[attr-defined]


@requires_postgres
async def test_pg_dispose_closes_pooled_connections(pg_db: None) -> None:
    async with get_session_factory()() as s:
        await s.execute(text("SELECT 1"))
    engine = get_engine()
    assert engine.pool.checkedin() >= 1  # type: ignore[attr-defined]
    await dispose_engine()
    assert engine.pool.checkedin() == 0  # type: ignore[attr-defined]
