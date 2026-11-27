"""Embedding generation with OpenAI ``text-embedding-3-small`` (TASK-302).

Texts are sent in batches of at most :data:`MAX_BATCH_SIZE` and the vectors come back in input
order. HTTP 429 rate limits are retried with exponential backoff by this module only: the
OpenAI client is built with ``max_retries=0`` so the SDK's own retry loop (fixed 8 s cap, and
it also retries 5xx and ``insufficient_quota``) never stacks on top. Every other provider error
is translated to :class:`EmbeddingError` at once, without echoing the provider's message.
"""

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import openai
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse

from app.core.config import Settings, get_settings
from app.core.exceptions import EmbeddingError, EmbeddingRateLimitError
from app.schemas.chunk import Chunk

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 64
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 30.0

# A 429 with this code means the account is out of credit; waiting will not help.
_NON_RETRYABLE_RATE_LIMIT_CODES = frozenset({"insufficient_quota"})

Embedding = list[float]
Sleep = Callable[[float], Awaitable[None]]


class EmbeddingService(Protocol):
    """Abstraction the RAG pipeline depends on (docs/RULES.md sections 1.2 and 4.1)."""

    async def embed_texts(self, texts: Sequence[str]) -> list[Embedding]:
        """Return one vector per text, in input order."""
        ...

    async def embed_chunks(self, chunks: Sequence[Chunk]) -> list[Embedding]:
        """Return one vector per chunk's content, in input order."""
        ...


class OpenAIEmbeddingService:
    """:class:`EmbeddingService` backed by the OpenAI embeddings endpoint."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        batch_size: int = MAX_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Configure the service; ``sleep`` is injectable so tests need not wait."""
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            msg = f"batch_size must be between 1 and {MAX_BATCH_SIZE}, got {batch_size}"
            raise EmbeddingError(msg, "INVALID_EMBEDDING_CONFIG")
        if dimensions < 1 or max_retries < 0 or base_delay < 0 or max_delay < base_delay:
            msg = (
                "dimensions must be positive, max_retries and base_delay non-negative, "
                "and max_delay at least base_delay"
            )
            raise EmbeddingError(msg, "INVALID_EMBEDDING_CONFIG")
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep

    async def embed_chunks(self, chunks: Sequence[Chunk]) -> list[Embedding]:
        """Embed each chunk's ``content``; vectors are returned in chunk order."""
        return await self.embed_texts([chunk.content for chunk in chunks])

    async def embed_texts(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed ``texts`` in batches of at most ``batch_size``; vectors keep input order.

        An empty input returns ``[]`` without calling the API. Blank texts are rejected before
        any request is sent, so a bad input never costs a partial run.
        """
        blank = [index for index, text in enumerate(texts) if not text.strip()]
        if blank:
            msg = f"cannot embed blank text at input positions {blank[:10]}"
            raise EmbeddingError(msg, "INVALID_EMBEDDING_INPUT")

        embeddings: list[Embedding] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            embeddings.extend(await self._embed_batch(batch))
        return embeddings

    async def _embed_batch(self, batch: list[str]) -> list[Embedding]:
        """Embed one batch, retrying HTTP 429 with exponential backoff."""
        attempt = 0
        while True:
            try:
                response = await self._client.embeddings.create(
                    input=batch,
                    model=self._model,
                    dimensions=self._dimensions,
                    encoding_format="float",
                )
            except openai.RateLimitError as exc:
                if exc.code in _NON_RETRYABLE_RATE_LIMIT_CODES:
                    logger.error("embedding quota exhausted", extra={"error_code": exc.code})
                    msg = "embedding provider quota exhausted"
                    raise EmbeddingError(msg, "EMBEDDING_QUOTA_EXCEEDED") from exc
                if attempt >= self._max_retries:
                    logger.error(
                        "embedding rate limit persisted after retries",
                        extra={"retries": attempt, "batch_size": len(batch)},
                    )
                    msg = f"embedding provider still rate limiting after {attempt} retries"
                    raise EmbeddingRateLimitError(msg) from exc
                delay = self._backoff_delay(attempt, exc)
                attempt += 1
                logger.warning(
                    "embedding request rate limited; retrying",
                    extra={"attempt": attempt, "delay_seconds": delay, "batch_size": len(batch)},
                )
                await self._sleep(delay)
            except openai.APIStatusError as exc:
                logger.error("embedding request failed", extra={"status_code": exc.status_code})
                msg = f"embedding provider returned HTTP {exc.status_code}"
                raise EmbeddingError(msg, "EMBEDDING_PROVIDER_ERROR") from exc
            except openai.APIError as exc:
                # Connection failures, timeouts and undecodable responses.
                logger.error("embedding request failed", extra={"error_type": type(exc).__name__})
                msg = f"embedding request failed ({type(exc).__name__})"
                raise EmbeddingError(msg, "EMBEDDING_PROVIDER_ERROR") from exc
            else:
                return self._extract_vectors(response, len(batch))

    def _backoff_delay(self, attempt: int, exc: openai.RateLimitError) -> float:
        """``base_delay * 2**attempt``, raised to the server's Retry-After, capped at max_delay."""
        delay = self._base_delay * 2**attempt
        retry_after = exc.response.headers.get("retry-after")
        if retry_after is not None:
            # The HTTP-date form (or garbage) is ignored: the computed backoff stands.
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        return min(delay, self._max_delay) if math.isfinite(delay) else self._max_delay

    def _extract_vectors(self, response: CreateEmbeddingResponse, expected: int) -> list[Embedding]:
        """Validate the response shape and return vectors ordered by their ``index``."""
        # The SDK builds response models without validation, so the declared types are not
        # guaranteed; every field is checked before use.
        items: object = getattr(response, "data", None)
        if not isinstance(items, list) or len(items) != expected:
            raise _invalid_response(f"expected {expected} embeddings")
        ordered: list[Embedding | None] = [None] * expected
        for item in items:
            index: object = getattr(item, "index", None)
            vector: object = getattr(item, "embedding", None)
            if (
                not isinstance(index, int)
                or not 0 <= index < expected
                or ordered[index] is not None
            ):
                raise _invalid_response("missing, duplicate or out-of-range embedding index")
            if not isinstance(vector, list) or len(vector) != self._dimensions:
                raise _invalid_response(f"embedding is not a {self._dimensions}-dimension vector")
            if not all(isinstance(value, int | float) and math.isfinite(value) for value in vector):
                raise _invalid_response("embedding contains non-numeric or non-finite values")
            ordered[index] = [float(value) for value in vector]
        return [vector for vector in ordered if vector is not None]


def _invalid_response(reason: str) -> EmbeddingError:
    logger.error("invalid embedding response", extra={"reason": reason})
    return EmbeddingError(f"invalid embedding response: {reason}", "INVALID_EMBEDDING_RESPONSE")


def create_embedding_service(settings: Settings | None = None) -> OpenAIEmbeddingService:
    """Build the service from application settings (TASK-103).

    The SDK's retries are disabled because this module owns 429 handling.
    """
    settings = settings or get_settings()
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout,
        max_retries=0,
    )
    return OpenAIEmbeddingService(
        client,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
    )
