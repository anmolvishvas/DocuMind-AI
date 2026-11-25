"""Tests for the OpenAI embedding service (TASK-302).

A real ``AsyncOpenAI`` client talks to an ``httpx.MockTransport``, so requests and errors go
through the SDK's own serialisation and error mapping without any network access. Backoff
sleeps are recorded, never awaited for real.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import EmbeddingError, EmbeddingRateLimitError
from app.rag.embeddings import (
    DEFAULT_MAX_RETRIES,
    MAX_BATCH_SIZE,
    OpenAIEmbeddingService,
    create_embedding_service,
)
from app.schemas.chunk import Chunk, ChunkMetadata

DIMS = 1536
API_KEY = "sk-test-secret-key-123"
Handler = Callable[[httpx.Request], httpx.Response]


def vector_for(text: str) -> list[float]:
    """Deterministic per-text vector so ordering can be checked after the round trip."""
    seed = float(int(text.rsplit("-", 1)[-1])) if text[-1].isdigit() else 0.5
    return [seed] + [0.001] * (DIMS - 1)


def embedding_body(texts: list[str], *, reverse: bool = False) -> dict[str, Any]:
    data = [
        {"object": "embedding", "index": i, "embedding": vector_for(t)} for i, t in enumerate(texts)
    ]
    if reverse:
        data.reverse()
    return {
        "object": "list",
        "data": data,
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
    }


def error_response(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"error": {"message": f"{code} for {API_KEY[:6]}***", "type": code, "code": code}}
    return httpx.Response(status, json=body, headers=headers)


class FakeOpenAI:
    """Records each request body and answers from a queue of responses (or a default)."""

    def __init__(self, *responses: httpx.Response | Handler) -> None:
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.client = AsyncOpenAI(
            api_key=API_KEY,
            base_url="https://api.test/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        self.requests.append(payload)
        if self.queue:
            nxt = self.queue.pop(0)
            return nxt if isinstance(nxt, httpx.Response) else nxt(request)
        return httpx.Response(200, json=embedding_body(payload["input"]))

    @property
    def batches(self) -> list[list[str]]:
        return [request["input"] for request in self.requests]


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


def make_service(fake: FakeOpenAI, sleeper: SleepRecorder, **kwargs: Any) -> OpenAIEmbeddingService:
    return OpenAIEmbeddingService(fake.client, sleep=sleeper, **kwargs)


def texts(n: int) -> list[str]:
    return [f"chunk-{i}" for i in range(n)]


def make_chunk(content: str) -> Chunk:
    metadata = ChunkMetadata(
        project_id="proj-1",
        document_id="doc-1",
        source_filename="spec.yaml",
        chunk_type="narrative",
        token_count=3,
    )
    return Chunk(content=content, metadata=metadata)


# ------------------------------------------------------------------ success paths


async def test_single_chunk_returns_one_1536_dim_float_vector(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    vectors = await make_service(fake, sleeper).embed_chunks([make_chunk("GET /pets lists pets")])
    assert len(vectors) == 1
    assert len(vectors[0]) == DIMS
    assert all(isinstance(value, float) for value in vectors[0])
    assert fake.batches == [["GET /pets lists pets"]]


async def test_request_uses_configured_model_dimensions_and_float_encoding(
    sleeper: SleepRecorder,
) -> None:
    fake = FakeOpenAI()
    await make_service(fake, sleeper).embed_texts(["hello"])
    request = fake.requests[0]
    assert request["model"] == "text-embedding-3-small"
    assert request["dimensions"] == DIMS
    assert request["encoding_format"] == "float"


async def test_custom_model_name_is_passed_through(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    await make_service(fake, sleeper, model="my-embedding-model").embed_texts(["hello"])
    assert fake.requests[0]["model"] == "my-embedding-model"


async def test_multiple_chunks_fit_in_one_batch(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    vectors = await make_service(fake, sleeper).embed_texts(texts(10))
    assert fake.batches == [texts(10)]
    assert [v[0] for v in vectors] == [float(i) for i in range(10)]


async def test_exactly_64_chunks_use_one_request(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    await make_service(fake, sleeper).embed_texts(texts(MAX_BATCH_SIZE))
    assert len(fake.requests) == 1


async def test_more_than_64_chunks_are_split_into_batches(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    vectors = await make_service(fake, sleeper).embed_texts(texts(150))
    assert [len(batch) for batch in fake.batches] == [64, 64, 22]
    assert [text for batch in fake.batches for text in batch] == texts(150)
    assert len(vectors) == 150


async def test_order_is_preserved_across_batches(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    vectors = await make_service(fake, sleeper).embed_texts(texts(130))
    assert [v[0] for v in vectors] == [float(i) for i in range(130)]


async def test_out_of_order_response_items_are_reordered_by_index(sleeper: SleepRecorder) -> None:
    def reversed_items(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json=embedding_body(inputs, reverse=True))

    fake = FakeOpenAI(reversed_items)
    vectors = await make_service(fake, sleeper).embed_texts(texts(5))
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_smaller_configured_batch_size_is_honoured(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    await make_service(fake, sleeper, batch_size=4).embed_texts(texts(10))
    assert [len(batch) for batch in fake.batches] == [4, 4, 2]


# ------------------------------------------------------------------ input validation


async def test_empty_input_returns_empty_list_without_calling_api(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI()
    service = make_service(fake, sleeper)
    assert await service.embed_texts([]) == []
    assert await service.embed_chunks([]) == []
    assert fake.requests == []


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
async def test_blank_text_is_rejected_before_any_request(sleeper: SleepRecorder, bad: str) -> None:
    fake = FakeOpenAI()
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts([*texts(70), bad])
    assert exc.value.code == "INVALID_EMBEDDING_INPUT"
    assert "70" in exc.value.message
    assert fake.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_size": 0},
        {"batch_size": MAX_BATCH_SIZE + 1},
        {"dimensions": 0},
        {"max_retries": -1},
        {"base_delay": -1.0},
        {"base_delay": 10.0, "max_delay": 5.0},
    ],
)
def test_invalid_configuration_is_rejected(sleeper: SleepRecorder, kwargs: dict[str, Any]) -> None:
    with pytest.raises(EmbeddingError) as exc:
        make_service(FakeOpenAI(), sleeper, **kwargs)
    assert exc.value.code == "INVALID_EMBEDDING_CONFIG"


# ------------------------------------------------------------------ rate limiting


async def test_429_is_retried_with_exponential_backoff(sleeper: SleepRecorder) -> None:
    rate_limited = [error_response(429, "rate_limit_exceeded") for _ in range(3)]
    fake = FakeOpenAI(*rate_limited)
    vectors = await make_service(fake, sleeper, base_delay=1.0).embed_texts(["a-1"])
    assert sleeper.delays == [1.0, 2.0, 4.0]
    assert len(fake.requests) == 4
    assert vectors[0][0] == 1.0


async def test_success_after_single_rate_limit(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(error_response(429, "rate_limit_exceeded"))
    vectors = await make_service(fake, sleeper).embed_texts(texts(3))
    assert len(vectors) == 3
    assert len(sleeper.delays) == 1
    assert fake.batches == [texts(3), texts(3)]


async def test_retry_only_resends_the_rate_limited_batch(sleeper: SleepRecorder) -> None:
    ok_first = httpx.Response(200, json=embedding_body(texts(64)))
    fake = FakeOpenAI(ok_first, error_response(429, "rate_limit_exceeded"))
    vectors = await make_service(fake, sleeper).embed_texts(texts(70))
    assert [len(batch) for batch in fake.batches] == [64, 6, 6]
    assert [v[0] for v in vectors] == [float(i) for i in range(70)]


async def test_backoff_is_capped_at_max_delay(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(*[error_response(429, "rate_limit_exceeded") for _ in range(5)])
    service = make_service(fake, sleeper, base_delay=1.0, max_delay=5.0)
    await service.embed_texts(["x"])
    assert sleeper.delays == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_retry_after_header_extends_the_delay(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(
        error_response(429, "rate_limit_exceeded", {"retry-after": "7"}),
        error_response(429, "rate_limit_exceeded", {"retry-after": "999"}),
        error_response(429, "rate_limit_exceeded", {"retry-after": "Wed, 21 Oct 2026 07:28:00"}),
    )
    await make_service(fake, sleeper, base_delay=1.0, max_delay=30.0).embed_texts(["x"])
    assert sleeper.delays == [7.0, 30.0, 4.0]


async def test_exhausted_retries_raise_rate_limit_error(
    sleeper: SleepRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeOpenAI(*[error_response(429, "rate_limit_exceeded") for _ in range(10)])
    service = make_service(fake, sleeper, max_retries=3)
    with (
        caplog.at_level(logging.WARNING, logger="app.rag.embeddings"),
        pytest.raises(EmbeddingRateLimitError) as exc,
    ):
        await service.embed_texts(["x"])
    assert exc.value.code == "EMBEDDING_RATE_LIMITED"
    assert exc.value.status_code == 503
    assert len(fake.requests) == 4  # first attempt + 3 retries
    assert sleeper.delays == [1.0, 2.0, 4.0]
    assert API_KEY not in exc.value.message
    assert API_KEY not in caplog.text


async def test_default_retry_budget(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(*[error_response(429, "rate_limit_exceeded") for _ in range(10)])
    with pytest.raises(EmbeddingRateLimitError):
        await make_service(fake, sleeper).embed_texts(["x"])
    assert len(fake.requests) == DEFAULT_MAX_RETRIES + 1


async def test_insufficient_quota_429_is_not_retried(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(error_response(429, "insufficient_quota"))
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts(["x"])
    assert not isinstance(exc.value, EmbeddingRateLimitError)
    assert exc.value.code == "EMBEDDING_QUOTA_EXCEEDED"
    assert len(fake.requests) == 1
    assert sleeper.delays == []


# ------------------------------------------------------------------ other provider errors


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_request_error"),
        (401, "invalid_api_key"),
        (403, "permission_denied"),
        (500, "server_error"),
        (503, "service_unavailable"),
    ],
)
async def test_non_429_status_errors_are_translated_without_retry(
    sleeper: SleepRecorder, status: int, code: str
) -> None:
    fake = FakeOpenAI(error_response(status, code))
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts(["x"])
    assert exc.value.code == "EMBEDDING_PROVIDER_ERROR"
    assert str(status) in exc.value.message
    assert API_KEY[:6] not in exc.value.message  # provider text is never copied
    assert len(fake.requests) == 1
    assert sleeper.delays == []


async def test_connection_error_is_translated_without_retry(sleeper: SleepRecorder) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    fake = FakeOpenAI(refuse)
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts(["x"])
    assert exc.value.code == "EMBEDDING_PROVIDER_ERROR"
    assert "APIConnectionError" in exc.value.message
    assert sleeper.delays == []


# ------------------------------------------------------------------ malformed responses


def _respond(body: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def _item(index: Any, embedding: Any) -> dict[str, Any]:
    return {"object": "embedding", "index": index, "embedding": embedding}


GOOD = [0.1] * DIMS


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"object": "list"}, id="missing-data"),
        pytest.param({"data": None}, id="null-data"),
        pytest.param({"data": [_item(0, GOOD)]}, id="too-few-items"),
        pytest.param({"data": [_item(0, GOOD), _item(0, GOOD)]}, id="duplicate-index"),
        pytest.param({"data": [_item(0, GOOD), _item(2, GOOD)]}, id="index-out-of-range"),
        pytest.param({"data": [_item(0, GOOD), _item("1", GOOD)]}, id="string-index"),
        pytest.param({"data": [_item(0, GOOD), _item(1, [0.1] * 3)]}, id="wrong-dimensions"),
        pytest.param({"data": [_item(0, GOOD), _item(1, None)]}, id="null-embedding"),
        pytest.param({"data": [_item(0, GOOD), _item(1, "abc")]}, id="base64-string"),
        pytest.param({"data": [_item(0, GOOD), _item(1, ["x"] * DIMS)]}, id="non-numeric"),
    ],
)
async def test_unexpected_response_shapes_are_rejected(sleeper: SleepRecorder, body: Any) -> None:
    fake = FakeOpenAI(_respond(body))
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts(["a", "b"])
    assert exc.value.code == "INVALID_EMBEDDING_RESPONSE"


async def test_non_finite_values_are_rejected(sleeper: SleepRecorder) -> None:
    raw = '{"data": [{"index": 0, "embedding": [NaN' + ", 0.1" * (DIMS - 1) + "]}]}"
    fake = FakeOpenAI(
        httpx.Response(200, content=raw, headers={"content-type": "application/json"})
    )
    with pytest.raises(EmbeddingError) as exc:
        await make_service(fake, sleeper).embed_texts(["a"])
    assert exc.value.code == "INVALID_EMBEDDING_RESPONSE"


async def test_integer_components_are_converted_to_float(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(_respond({"data": [_item(0, [1] * DIMS)]}))
    vectors = await make_service(fake, sleeper).embed_texts(["a"])
    assert all(isinstance(value, float) for value in vectors[0])


async def test_non_json_body_is_rejected(sleeper: SleepRecorder) -> None:
    fake = FakeOpenAI(httpx.Response(200, text="<html>bad gateway</html>"))
    with pytest.raises(EmbeddingError):
        await make_service(fake, sleeper).embed_texts(["a"])


# ------------------------------------------------------------------ factory


def test_factory_builds_service_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        openai_api_key=API_KEY,  # type: ignore[arg-type]
        openai_timeout=12.0,
        embedding_batch_size=32,
    )
    service = create_embedding_service(settings)
    client = service._client
    assert client.api_key == API_KEY
    assert client.max_retries == 0  # backoff is owned by the service, not the SDK
    assert client.timeout == 12.0
    assert service._batch_size == 32
    assert service._model == "text-embedding-3-small"
    assert service._dimensions == DIMS


@pytest.mark.parametrize("batch_size", [MAX_BATCH_SIZE + 1, 2048])
def test_settings_reject_batch_size_above_64(batch_size: int) -> None:
    """The 64-per-request cap is enforced at configuration load, before any service exists."""
    with pytest.raises(ValidationError, match="embedding_batch_size"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
            openai_api_key=API_KEY,  # type: ignore[arg-type]
            embedding_batch_size=batch_size,
        )


def test_settings_accept_batch_size_of_64() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        openai_api_key=API_KEY,  # type: ignore[arg-type]
        embedding_batch_size=MAX_BATCH_SIZE,
    )
    assert create_embedding_service(settings)._batch_size == MAX_BATCH_SIZE
