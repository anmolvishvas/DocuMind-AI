"""Semantic vector retriever over ``document_chunks`` (TASK-304).

Chunks are ranked by pgvector cosine distance (``<=>``) and returned with their cosine
similarity, ``1 - distance``. Every query is tenant-filtered on ``project_id`` inside the SQL
statement itself (docs/RULES.md section 5.3), so the filter is applied before ``LIMIT`` and a
chunk of another project can never be returned, however similar it is. Chunks below the
similarity threshold (default 0.70, docs/RULES.md section 5.4) are discarded in SQL as well.

No keyword/full-text index exists in the schema, so this is the semantic half of hybrid
retrieval only; nothing here invents a second search system.

Like the repositories, the retriever works on the session it is given and never commits.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import PersistenceError, RetrievalError
from app.models.chunk import EMBEDDING_DIMENSIONS, DocumentChunk
from app.rag.embeddings import Embedding, EmbeddingService

logger = logging.getLogger(__name__)

MAX_TOP_K = 50  # Same upper bound as ``Settings.retrieval_top_k``.


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One search hit: the stored chunk and its cosine similarity to the query."""

    chunk_id: str
    project_id: str
    document_id: str
    endpoint_id: str | None
    chunk_index: int
    chunk_type: str
    content: str
    metadata: dict[str, Any]
    similarity: float


class VectorRetriever:
    """Top-k cosine-similarity search, scoped to one project."""

    def __init__(
        self,
        session: AsyncSession,
        embedding_service: EmbeddingService,
        *,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Defaults for ``top_k`` and the threshold come from settings (TASK-103).

        Raises:
            RetrievalError: ``top_k`` or the threshold is out of range.
        """
        settings = settings or get_settings()
        self._session = session
        self._embedder = embedding_service
        self._top_k = _check_top_k(settings.retrieval_top_k if top_k is None else top_k)
        threshold = (
            settings.similarity_threshold if similarity_threshold is None else similarity_threshold
        )
        if not 0.0 <= threshold <= 1.0:
            msg = f"similarity_threshold must be between 0 and 1, got {threshold}"
            raise RetrievalError(msg, "INVALID_SIMILARITY_THRESHOLD")
        self._threshold = threshold

    async def retrieve(
        self, project_id: str, query: str, *, top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """Embed ``query`` and return the project's most similar chunks.

        Raises:
            RetrievalError: invalid ``project_id``, ``query`` or ``top_k``.
            EmbeddingError: the query could not be embedded.
            PersistenceError: the database rejected the search.
        """
        if not query.strip():
            raise RetrievalError("query must not be blank", "INVALID_RETRIEVAL_QUERY")
        _check_project_id(project_id)
        (embedding,) = await self._embedder.embed_texts([query])
        return await self.retrieve_by_embedding(project_id, embedding, top_k=top_k)

    async def retrieve_by_embedding(
        self, project_id: str, embedding: Sequence[float], *, top_k: int | None = None
    ) -> list[RetrievedChunk]:
        """Return up to ``top_k`` chunks of ``project_id`` with similarity >= the threshold.

        Results are ordered most similar first. An empty list means nothing was relevant.

        Raises:
            RetrievalError: invalid ``project_id``, ``embedding`` or ``top_k``.
            PersistenceError: the database rejected the search.
        """
        _check_project_id(project_id)
        _check_embedding(embedding)
        limit = self._top_k if top_k is None else _check_top_k(top_k)

        distance = DocumentChunk.embedding.cosine_distance(list(embedding))
        similarity = (1 - distance).label("similarity")
        statement = (
            select(DocumentChunk, similarity)
            .where(
                DocumentChunk.project_id == project_id,  # tenant filter: before LIMIT
                DocumentChunk.embedding.is_not(None),
                (1 - distance) >= self._threshold,
            )
            .order_by(distance)  # ascending distance == descending similarity
            .limit(limit)
        )
        try:
            rows = (await self._session.execute(statement)).all()
        except SQLAlchemyError as exc:
            logger.exception("vector search failed", extra={"project_id": project_id})
            raise PersistenceError("Failed to search document chunks.") from exc

        hits = [
            RetrievedChunk(
                chunk_id=chunk.id,
                project_id=chunk.project_id,
                document_id=chunk.document_id,
                endpoint_id=chunk.endpoint_id,
                chunk_index=chunk.chunk_index,
                chunk_type=chunk.chunk_type,
                content=chunk.content,
                metadata=chunk.chunk_metadata,
                similarity=float(score),
            )
            for chunk, score in rows
        ]
        logger.info(
            "vector search completed",
            extra={"project_id": project_id, "top_k": limit, "hits": len(hits)},
        )
        return hits


def _check_top_k(top_k: int) -> int:
    if isinstance(top_k, bool) or not 1 <= top_k <= MAX_TOP_K:
        msg = f"top_k must be an integer between 1 and {MAX_TOP_K}, got {top_k!r}"
        raise RetrievalError(msg, "INVALID_TOP_K")
    return top_k


def _check_project_id(project_id: str) -> None:
    if not project_id.strip():
        raise RetrievalError("project_id must not be blank", "INVALID_PROJECT_ID")


def _check_embedding(embedding: Embedding | Sequence[float]) -> None:
    if len(embedding) != EMBEDDING_DIMENSIONS:
        msg = f"query embedding must have {EMBEDDING_DIMENSIONS} dimensions, got {len(embedding)}"
        raise RetrievalError(msg, "INVALID_QUERY_EMBEDDING")
    if not all(math.isfinite(value) for value in embedding):
        raise RetrievalError(
            "query embedding contains non-finite values", "INVALID_QUERY_EMBEDDING"
        )
    if not any(embedding):
        # Cosine distance to a zero vector is NaN, which would silently match nothing.
        raise RetrievalError(
            "query embedding must not be the zero vector", "INVALID_QUERY_EMBEDDING"
        )
