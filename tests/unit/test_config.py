import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import ConfigurationError, Settings, get_settings, load_settings
from app.core.logging import (
    configure_logging,
    generate_correlation_id,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

SECRET_KEY = "sk-super-secret-value"
DB_URL = "postgresql+asyncpg://user:db-password-xyz@localhost:5432/db"

_ENV_NAMES = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate every test from the real environment and any real .env file."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DB_POOL_SIZE", "25")
    settings = load_settings()
    assert settings.log_level == "DEBUG"
    assert settings.db_pool_size == 25
    assert settings.openai_api_key.get_secret_value() == SECRET_KEY


def test_loads_from_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"DATABASE_URL={DB_URL}\nOPENAI_API_KEY={SECRET_KEY}\nENVIRONMENT=test\nUNRELATED_VAR=1\n"
    )
    settings = load_settings()
    assert settings.environment == "test"
    assert settings.database_url.get_secret_value() == DB_URL


def test_environment_overrides_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"DATABASE_URL={DB_URL}\nOPENAI_API_KEY=k\nLOG_LEVEL=ERROR\n"
    )
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert load_settings().log_level == "WARNING"


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    settings = load_settings()
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.similarity_threshold == 0.70
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_dimensions == 1536
    assert settings.embedding_batch_size == 64
    assert settings.max_upload_size_bytes == 20 * 1024 * 1024


def test_missing_required_settings_listed_by_name() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings()
    message = str(exc_info.value)
    assert "DATABASE_URL" in message
    assert "OPENAI_API_KEY" in message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "LOUD"),
        ("DB_POOL_SIZE", "0"),
        ("DB_POOL_SIZE", "many"),
        ("SIMILARITY_THRESHOLD", "1.5"),
        ("MAX_UPLOAD_SIZE_MB", "21"),
        ("ENVIRONMENT", "staging"),
    ],
)
def test_invalid_values_raise_clear_error(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=name):
        load_settings()


def test_non_asyncpg_database_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:db-password-xyz@localhost/db")
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings()
    assert "postgresql+asyncpg://" in str(exc_info.value)
    assert "db-password-xyz" not in str(exc_info.value)


def test_secrets_not_exposed_in_errors_or_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    settings = load_settings()
    assert SECRET_KEY not in repr(settings)
    assert SECRET_KEY not in str(settings)
    assert "db-password-xyz" not in repr(settings)

    monkeypatch.setenv("DB_POOL_SIZE", "bad")
    monkeypatch.setenv("DATABASE_URL", "mysql://u:db-password-xyz@h/db")
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings()
    assert SECRET_KEY not in str(exc_info.value)
    assert "db-password-xyz" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_settings_are_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    assert get_settings() is get_settings()


# ----------------------------------------------------------------- logging


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    configure_logging("DEBUG", stream)
    yield stream
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _last_record(stream: io.StringIO) -> dict[str, object]:
    parsed: dict[str, object] = json.loads(stream.getvalue().strip().splitlines()[-1])
    return parsed


def test_json_log_has_required_fields(log_stream: io.StringIO) -> None:
    logging.getLogger("documind.test").info("hello %s", "world")
    record = _last_record(log_stream)
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "documind.test"
    assert isinstance(record["timestamp"], str)
    assert record["correlation_id"] is None


def test_correlation_id_attached_and_reset(log_stream: io.StringIO) -> None:
    logger = logging.getLogger("documind.test")
    cid = generate_correlation_id()
    token = set_correlation_id(cid)
    try:
        logger.info("inside")
        assert get_correlation_id() == cid
    finally:
        reset_correlation_id(token)
    logger.info("outside")
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert lines[0]["correlation_id"] == cid
    assert lines[1]["correlation_id"] is None


def test_extra_fields_included_and_sensitive_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("documind.test").info(
        "req",
        extra={
            "project_id": "p1",
            "authorization": "Bearer abc",
            "headers": {"Cookie": "x"},
        },
    )
    record = _last_record(log_stream)
    assert record["project_id"] == "p1"
    assert record["authorization"] == "[REDACTED]"
    assert record["headers"] == {"Cookie": "[REDACTED]"}
    assert "Bearer abc" not in log_stream.getvalue()


def test_exception_is_serialised(log_stream: io.StringIO) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("documind.test").exception("failed")
    assert "RuntimeError: boom" in str(_last_record(log_stream)["exception"])


def test_configure_logging_is_idempotent(log_stream: io.StringIO) -> None:
    configure_logging("INFO", log_stream)
    configure_logging("INFO", log_stream)
    logging.getLogger("documind.test").info("once")
    assert log_stream.getvalue().count("once") == 1


def test_importing_logging_module_has_no_side_effects() -> None:
    import importlib

    import app.core.logging as module

    before = list(logging.getLogger().handlers)
    importlib.reload(module)
    assert list(logging.getLogger().handlers) == before
