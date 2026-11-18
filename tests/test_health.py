"""API smoke tests for the FastAPI entrypoint and health probes (TASK-105).

Real-PostgreSQL tests need ``TEST_DATABASE_URL`` (``postgresql+asyncpg://...``) and are
skipped otherwise. "Database unavailable" tests use the real engine pointed at a closed
local port, so they exercise genuine connection failures without any server.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app.core import database
from app.core.config import ConfigurationError, get_settings
from app.core.database import dispose_engine, get_db
from app.core.exceptions import DocuMindException, EndpointNotFoundError
from app.core.logging import CORRELATION_ID_HEADER, get_correlation_id
from app.main import create_app
from fastapi import FastAPI
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

UNREACHABLE_URL = "postgresql+asyncpg://user:unreachable-secret-pw@127.0.0.1:1/none"
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL is not set"
)


def _set_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str | None) -> None:
    for name in list(os.environ):
        if name.startswith(
            ("DB_", "DATABASE_URL", "OPENAI_", "CORS_", "ENVIRONMENT", "LOG_")
        ):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no real .env is picked up
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CORS_ORIGINS", '["http://allowed.example"]')
    monkeypatch.setenv("DB_POOL_TIMEOUT", "2")
    get_settings.cache_clear()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest_asyncio.fixture
async def app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[FastAPI]:
    """App wired to an unreachable database (real engine, real connection failures)."""
    _set_env(monkeypatch, tmp_path, UNREACHABLE_URL)
    await dispose_engine()
    application = create_app()

    @application.get("/boom")
    async def boom() -> None:
        msg = "internal detail unreachable-secret-pw"
        raise RuntimeError(msg)

    @application.get("/domain-error")
    async def domain_error() -> None:
        raise EndpointNotFoundError("Endpoint not found", code="ENDPOINT_NOT_FOUND")

    @application.get("/domain-500")
    async def domain_500() -> None:
        raise DocuMindException("secret internals", code="INTERNAL_ERROR")

    @application.get("/typed/{item_id}")
    async def typed(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    yield application
    await dispose_engine()
    get_settings.cache_clear()


class FakeResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


class FakeSession:
    """Scripted stand-in for AsyncSession (used only for readiness *logic* tests)."""

    def __init__(self, vector_installed: bool = True, fail: bool = False) -> None:
        self.vector_installed = vector_installed
        self.fail = fail

    async def execute(self, statement: Any) -> FakeResult:
        if self.fail:
            msg = "connection to postgresql://user:fake-secret@host failed"
            raise ConnectionRefusedError(msg)
        if "pg_extension" in str(statement):
            return FakeResult((1,) if self.vector_installed else None)
        return FakeResult((1,))


def _override_session(app: FastAPI, session: FakeSession) -> None:
    async def fake_get_db() -> AsyncIterator[FakeSession]:
        yield session

    app.dependency_overrides[get_db] = fake_get_db


# ------------------------------------------------------------------ /health


async def test_health_returns_exact_ok_payload(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.text == '{"status":"ok"}'


async def test_health_works_when_database_unavailable(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> None:
        pytest.fail("/health must not touch the database")

    monkeypatch.setattr(database, "get_engine", forbidden)
    async with _client(app) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------------------- /ready


async def test_ready_succeeds_when_db_and_vector_available_logic(app: FastAPI) -> None:
    _override_session(app, FakeSession())
    async with _client(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "vector_extension": "ok"},
    }


async def test_ready_fails_when_vector_extension_missing_logic(app: FastAPI) -> None:
    _override_session(app, FakeSession(vector_installed=False))
    async with _client(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "vector_extension": "missing"},
    }


async def test_ready_fails_without_leaking_details_on_error_logic(app: FastAPI) -> None:
    _override_session(app, FakeSession(fail=True))
    async with _client(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"
    assert "fake-secret" not in response.text
    assert "postgresql://" not in response.text


async def test_ready_fails_with_real_unreachable_database(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "error", "vector_extension": "unknown"},
    }
    assert "unreachable-secret-pw" not in response.text


@pytest_asyncio.fixture
async def pg_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[FastAPI]:
    assert TEST_DB_URL
    _set_env(monkeypatch, tmp_path, TEST_DB_URL)
    await dispose_engine()
    yield create_app()
    await dispose_engine()
    get_settings.cache_clear()


@requires_postgres
async def test_ready_against_real_postgres_matches_extension_state(
    pg_app: FastAPI,
) -> None:
    async with database.get_engine().connect() as conn:
        installed = (
            await conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
        ).first() is not None
    async with _client(pg_app) as client:
        response = await client.get("/ready")
    assert response.status_code == (200 if installed else 503)
    assert response.json()["checks"]["database"] == "ok"
    assert response.json()["checks"]["vector_extension"] == (
        "ok" if installed else "missing"
    )


# ----------------------------------------------------------- correlation ID


async def test_correlation_id_generated_when_absent(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get("/health")
    generated = response.headers[CORRELATION_ID_HEADER]
    assert len(generated) == 32
    async with _client(app) as client:
        other = await client.get("/health")
    assert other.headers[CORRELATION_ID_HEADER] != generated


async def test_valid_incoming_correlation_id_is_echoed(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get(
            "/health", headers={CORRELATION_ID_HEADER: "abc-123.X_y"}
        )
    assert response.headers[CORRELATION_ID_HEADER] == "abc-123.X_y"


@pytest.mark.parametrize("bad", ["has space", "a" * 129, "semi;colon", "<script>"])
async def test_invalid_incoming_correlation_id_is_replaced(
    app: FastAPI, bad: str
) -> None:
    async with _client(app) as client:
        response = await client.get("/health", headers={CORRELATION_ID_HEADER: bad})
    assert response.headers[CORRELATION_ID_HEADER] != bad
    assert len(response.headers[CORRELATION_ID_HEADER]) == 32


async def test_correlation_id_context_reset_after_request(app: FastAPI) -> None:
    assert get_correlation_id() is None
    async with _client(app) as client:
        await client.get("/health", headers={CORRELATION_ID_HEADER: "ctx-1"})
        await client.get("/boom", headers={CORRELATION_ID_HEADER: "ctx-2"})
    assert get_correlation_id() is None


async def test_correlation_id_visible_inside_handler(app: FastAPI) -> None:
    seen: list[str | None] = []

    @app.get("/whoami")
    async def whoami() -> dict[str, str | None]:
        seen.append(get_correlation_id())
        return {"id": get_correlation_id()}

    async with _client(app) as client:
        response = await client.get(
            "/whoami", headers={CORRELATION_ID_HEADER: "inside-1"}
        )
    assert seen == ["inside-1"]
    assert response.json() == {"id": "inside-1"}


# ------------------------------------------------------- exception handling


async def test_unhandled_exception_returns_generic_500_with_header(
    app: FastAPI,
) -> None:
    async with _client(app) as client:
        response = await client.get("/boom", headers={CORRELATION_ID_HEADER: "err-1"})
    assert response.status_code == 500
    assert response.headers[CORRELATION_ID_HEADER] == "err-1"
    assert response.json() == {
        "error": "Internal server error",
        "code": "INTERNAL_ERROR",
        "correlation_id": "err-1",
    }
    assert "unreachable-secret-pw" not in response.text


async def test_domain_exception_maps_to_structured_error(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.get(
            "/domain-error", headers={CORRELATION_ID_HEADER: "dom-1"}
        )
    assert response.status_code == 404
    assert response.json() == {
        "error": "Endpoint not found",
        "code": "ENDPOINT_NOT_FOUND",
        "correlation_id": "dom-1",
    }


async def test_unknown_route_and_validation_errors_use_uniform_shape(
    app: FastAPI,
) -> None:
    async with _client(app) as client:
        missing = await client.get("/nope")
        invalid = await client.get("/typed/not-an-int")
        valid = await client.get("/typed/7")
    assert missing.status_code == 404
    assert missing.json()["code"] == "HTTP_404"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"
    assert invalid.json()["details"][0]["loc"] == ["path", "item_id"]
    assert valid.json() == {"item_id": 7}


# --------------------------------------------------------------------- CORS


async def test_cors_allows_configured_origin_only(app: FastAPI) -> None:
    async with _client(app) as client:
        allowed = await client.options(
            "/health",
            headers={
                "Origin": "http://allowed.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = await client.options(
            "/health",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        simple = await client.get(
            "/health", headers={"Origin": "http://allowed.example"}
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://allowed.example"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
    assert simple.headers["access-control-allow-origin"] == "http://allowed.example"
    assert CORRELATION_ID_HEADER in simple.headers["access-control-expose-headers"]


# ------------------------------------------------------- startup / shutdown


async def test_lifespan_disposes_engine_on_shutdown(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    calls: list[str] = []

    async def fake_dispose() -> None:
        calls.append("dispose")

    monkeypatch.setattr(main_module, "dispose_engine", fake_dispose)
    async with app.router.lifespan_context(app):
        assert calls == []
    assert calls == ["dispose"]


async def test_startup_fails_fast_on_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, tmp_path, UNREACHABLE_URL)
    application = create_app()  # built while configuration is valid
    monkeypatch.delenv("DATABASE_URL")
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError) as info:
        async with application.router.lifespan_context(application):
            pytest.fail("startup should have failed")
    assert "DATABASE_URL" in str(info.value)
    get_settings.cache_clear()


async def test_importing_main_needs_no_configuration() -> None:
    import importlib

    import app.main as main_module

    importlib.reload(main_module)  # must not raise without env/.env
