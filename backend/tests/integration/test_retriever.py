"""Integration tests for the semantic vector retriever (TASK-304).

Tests marked ``requires_postgres`` need a disposable PostgreSQL with pgvector (see
``test_pgvector_indexing``) and are skipped without ``TEST_DATABASE_URL``. The tests at the top
need no server: they capture the statement the retriever sends and check its SQL.
"""

import math
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.exceptions import EmbeddingError, PersistenceError, RetrievalError
from app.models.chunk import EMBEDDING_DIMENSIONS
from app.rag.retriever import MAX_TOP_K, VectorRetriever

from .test_pgvector_indexing import (  # noqa: F401  (fixtures are used by name)
    _add,
    _axis,
    _chunk,
    _row,
    _seed_document,
    db,
    requires_postgres,
)

pytestmark = pytest.mark.asyncio

QUERY = _axis((0, 1.0))


def _at_similarity(similarity: float) -> list[float]:
    """A vector whose cosine similarity to ``QUERY`` is exactly ``similarity``."""
    return _axis((0, similarity), (1, math.sqrt(1 - similarity**2)))


class FakeEmbedder:
    """Stands in for OpenAI: never touches the network."""

    def __init__(self, vector: list[float] = QUERY) -> None:
        self.vector = vector
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector for _ in texts]

    async def embed_chunks(self, chunks: Sequence[Any]) -> list[list[float]]:
        raise NotImplementedError


class CapturingSession:
    """Records the statement and returns no rows (or raises ``error``)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.statement: Any = None
        self.error = error

    async def execute(self, statement: Any) -> Any:
        if self.error:
            raise self.error
        self.statement = statement

        class Result:
            def all(self) -> list[Any]:
                return []

        return Result()


def _retriever(session: Any = None, **kwargs: Any) -> VectorRetriever:
    return VectorRetriever(session or CapturingSession(), FakeEmbedder(), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------- no server needed


async def test_query_filters_by_project_in_sql_before_limit_and_uses_cosine_distance() -> None:
    session = CapturingSession()

    assert await _retriever(session).retrieve_by_embedding("proj-1", QUERY, top_k=3) == []

    compiled = session.statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    sql = " ".join(str(compiled).split())
    assert "document_chunks.project_id = %(project_id_1)s" in sql
    assert compiled.params["project_id_1"] == "proj-1"
    assert sql.index("WHERE") < sql.index("project_id =") < sql.index("LIMIT")
    assert "<=>" in sql
    assert "ORDER BY document_chunks.embedding <=>" in sql
    assert "DESC" not in sql  # ascending distance: most similar first
    assert compiled.params["param_1"] == 1  # similarity = 1 - distance
    assert 0.70 in compiled.params.values()  # default threshold from settings
    assert 3 in compiled.params.values()  # LIMIT


async def test_defaults_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
    try:
        session = CapturingSession()
        await _retriever(session).retrieve_by_embedding("p", QUERY)
    finally:
        get_settings.cache_clear()
    params = session.statement.compile(dialect=postgresql.dialect()).params  # type: ignore[no-untyped-call]
    assert 5 in params.values()  # Settings.retrieval_top_k default


async def test_retrieve_embeds_the_query_once() -> None:
    embedder = FakeEmbedder()
    retriever = VectorRetriever(CapturingSession(), embedder)  # type: ignore[arg-type]

    assert await retriever.retrieve("proj-1", "how do I list pets?") == []
    assert embedder.calls == [["how do I list pets?"]]


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1, True])
async def test_invalid_top_k_is_rejected(top_k: int) -> None:
    with pytest.raises(RetrievalError) as caught:
        await _retriever().retrieve_by_embedding("p", QUERY, top_k=top_k)
    assert caught.value.code == "INVALID_TOP_K"


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
async def test_invalid_threshold_is_rejected(threshold: float) -> None:
    with pytest.raises(RetrievalError, match="similarity_threshold"):
        _retriever(similarity_threshold=threshold)


@pytest.mark.parametrize(
    "embedding",
    [[0.1] * 3, [0.0] * EMBEDDING_DIMENSIONS, _axis((0, math.nan)), _axis((0, math.inf))],
)
async def test_invalid_query_embedding_is_rejected(embedding: list[float]) -> None:
    session = CapturingSession()
    with pytest.raises(RetrievalError) as caught:
        await _retriever(session).retrieve_by_embedding("p", embedding)
    assert caught.value.code == "INVALID_QUERY_EMBEDDING"
    assert session.statement is None  # never reached the database


async def test_blank_project_and_query_are_rejected() -> None:
    retriever = _retriever()
    with pytest.raises(RetrievalError, match="project_id"):
        await retriever.retrieve_by_embedding("  ", QUERY)
    with pytest.raises(RetrievalError, match="query"):
        await retriever.retrieve("p", "   ")
    with pytest.raises(RetrievalError, match="project_id"):
        await retriever.retrieve("", "hello")


async def test_embedding_errors_propagate() -> None:
    class Failing(FakeEmbedder):
        async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
            raise EmbeddingError("down", "EMBEDDING_PROVIDER_ERROR")

    with pytest.raises(EmbeddingError):
        await VectorRetriever(CapturingSession(), Failing()).retrieve("p", "q")  # type: ignore[arg-type]


async def test_database_errors_become_persistence_errors() -> None:
    session = CapturingSession(error=OperationalError("SELECT 1", {}, Exception("secret")))

    with pytest.raises(PersistenceError) as caught:
        await _retriever(session).retrieve_by_embedding("p", QUERY)
    assert "secret" not in caught.value.message


# --------------------------------------------------------- real PostgreSQL


async def _search(project_id: str, embedding: list[float] = QUERY, **kwargs: Any) -> list[Any]:
    async with get_session_factory()() as session:
        return await VectorRetriever(session, FakeEmbedder(embedding)).retrieve_by_embedding(
            project_id, embedding, **kwargs
        )


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_successful_retrieval_returns_relevant_chunks_best_first() -> None:
    project_id, document_id = await _seed_document()
    await _add(
        _row(_chunk(project_id, document_id, "good"), 0, _at_similarity(0.80)),
        _row(_chunk(project_id, document_id, "best"), 1, _axis((0, 5.0))),
        _row(_chunk(project_id, document_id, "ok"), 2, _at_similarity(0.75)),
    )

    hits = await _search(project_id)

    assert [hit.content for hit in hits] == ["best", "good", "ok"]
    assert [hit.similarity for hit in hits] == pytest.approx([1.0, 0.80, 0.75], abs=1e-5)
    assert hits == sorted(hits, key=lambda hit: hit.similarity, reverse=True)
    best = hits[0]
    assert (best.project_id, best.document_id, best.chunk_index) == (project_id, document_id, 1)
    assert best.chunk_type == "endpoint"
    assert best.metadata["route_path"] == "/pets"


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_other_projects_chunks_are_never_returned() -> None:
    project_id, document_id = await _seed_document()
    other_project_id, other_document_id = await _seed_document()
    await _add(
        _row(_chunk(project_id, document_id, "mine"), 0, _at_similarity(0.72)),
        # A perfect match, but another tenant's.
        _row(_chunk(other_project_id, other_document_id, "foreign"), 0, QUERY),
    )

    assert [hit.content for hit in await _search(project_id)] == ["mine"]
    assert [hit.content for hit in await _search(other_project_id)] == ["foreign"]
    assert await _search("no-such-project") == []


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_project_filter_applies_before_limit() -> None:
    project_id, document_id = await _seed_document()
    other_project_id, other_document_id = await _seed_document()
    await _add(
        # Three foreign chunks outrank the only local one; with top_k=1 a post-filter returns [].
        *(_row(_chunk(other_project_id, other_document_id, f"f{i}"), i, QUERY) for i in range(3)),
        _row(_chunk(project_id, document_id, "mine"), 0, _at_similarity(0.75)),
    )

    assert [hit.content for hit in await _search(project_id, top_k=1)] == ["mine"]


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_threshold_is_inclusive_and_discards_lower_scores() -> None:
    project_id, document_id = await _seed_document()
    await _add(
        _row(_chunk(project_id, document_id, "above"), 0, _at_similarity(0.71)),
        _row(_chunk(project_id, document_id, "below"), 1, _at_similarity(0.69)),
        _row(_chunk(project_id, document_id, "orthogonal"), 2, _axis((1, 1.0))),
        _row(_chunk(project_id, document_id, "opposite"), 3, _axis((0, -1.0))),
        _row(_chunk(project_id, document_id, "pending"), 4),  # not embedded
    )

    assert [hit.content for hit in await _search(project_id)] == ["above"]
    # The threshold is configurable per retriever.
    async with get_session_factory()() as session:
        lenient = VectorRetriever(session, FakeEmbedder(), similarity_threshold=0.5)
        hits = await lenient.retrieve_by_embedding(project_id, QUERY)
    assert [hit.content for hit in hits] == ["above", "below"]


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_exact_threshold_boundary_is_kept() -> None:
    project_id, document_id = await _seed_document()
    # 0.5 similarity is exact in float32, so ``1 - distance == 0.5`` has no rounding doubt.
    await _add(_row(_chunk(project_id, document_id, "edge"), 0, _axis((0, 1.0), (1, math.sqrt(3)))))

    async with get_session_factory()() as session:
        hits = await VectorRetriever(
            session, FakeEmbedder(), similarity_threshold=0.5
        ).retrieve_by_embedding(project_id, QUERY)
    assert [hit.content for hit in hits] == ["edge"]


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_top_k_limits_results() -> None:
    project_id, document_id = await _seed_document()
    await _add(
        *(
            _row(_chunk(project_id, document_id, f"c{i}"), i, _at_similarity(0.99 - i / 100))
            for i in range(6)
        )
    )

    assert [hit.content for hit in await _search(project_id, top_k=2)] == ["c0", "c1"]
    assert len(await _search(project_id, top_k=50)) == 6


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_no_chunks_or_no_relevant_chunks_returns_an_empty_list() -> None:
    project_id, document_id = await _seed_document()
    assert await _search(project_id) == []

    await _add(_row(_chunk(project_id, document_id, "unrelated"), 0, _axis((7, 1.0))))
    assert await _search(project_id) == []


@requires_postgres
@pytest.mark.usefixtures("db")
async def test_retrieve_embeds_the_text_query_then_searches() -> None:
    project_id, document_id = await _seed_document()
    await _add(_row(_chunk(project_id, document_id, "pets"), 0, _at_similarity(0.9)))
    embedder = FakeEmbedder()

    async with get_session_factory()() as session:
        hits = await VectorRetriever(session, embedder).retrieve(project_id, "list pets")

    assert embedder.calls == [["list pets"]]
    assert [hit.content for hit in hits] == ["pets"]
