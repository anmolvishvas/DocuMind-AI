"""Integration tests for chunk + embedding storage and the HNSW index (TASK-303).

Tests marked ``requires_postgres`` need a real, disposable PostgreSQL with pgvector >= 0.5.0
(``TEST_DATABASE_URL``, ``postgresql+asyncpg://...``) and are skipped otherwise; they never fall
back to SQLite or to ``DATABASE_URL``. The schema is built by the real Alembic migrations and
dropped afterwards. The tests at the top of the file need no server.

HNSW is an approximate index and the planner only uses it when it estimates that to be cheaper
than a sequential scan, which it never is for a handful of rows. Ranking tests therefore check
exact cosine distances (whatever plan is chosen), and the plan test disables sequential scans
for its own transaction so the index path can be observed with ``EXPLAIN ANALYZE``.
"""

import asyncio
import io
import json
import math
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from pgvector.sqlalchemy import Vector
from sqlalchemy import Table, UniqueConstraint, delete, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine, get_session_factory
from app.models import Base, Document, DocumentChunk, DocumentFileType, Project
from app.models.chunk import EMBEDDING_DIMENSIONS
from app.schemas.chunk import Chunk, ChunkMetadata

pytestmark = pytest.mark.asyncio

TABLE: Table = Base.metadata.tables["document_chunks"]
BACKEND = Path(__file__).resolve().parents[2]
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
OFFLINE_URL = "postgresql+asyncpg://user:offline-secret-pw@localhost:1/none"
HNSW_INDEX = "ix_document_chunks_embedding_hnsw"

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL (a disposable PostgreSQL) is not set"
)


# ------------------------------------------------------------------------------ helpers


def _alembic(action: str, *args: str, **kwargs: Any) -> None:
    config = Config(str(BACKEND / "alembic.ini"), output_buffer=kwargs.pop("buffer", None))
    getattr(command, action)(config, *args, **kwargs)


async def _run_alembic(action: str, *args: str, **kwargs: Any) -> None:
    # Alembic's async env.py starts its own event loop, so it must run off this one.
    await asyncio.to_thread(_alembic, action, *args, **kwargs)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str) -> None:
    for name in list(os.environ):
        if name.startswith(("DB_", "DATABASE_URL", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no real .env is picked up
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()


def _axis(*weights: tuple[int, float]) -> list[float]:
    """A 1536-dimension vector that is zero except at the given ``(position, value)`` pairs."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for position, value in weights:
        vector[position] = value
    return vector


def _patterned_embedding() -> list[float]:
    # Multiples of 1/64 in [-1, 1]: exactly representable as float32 (pgvector's storage type),
    # so a round trip must return identical values.
    return [((index * 37) % 129 - 64) / 64 for index in range(EMBEDDING_DIMENSIONS)]


def _chunk(project_id: str, document_id: str, content: str = "GET /pets lists pets") -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            project_id=project_id,
            document_id=document_id,
            source_filename="petstore.yaml",
            chunk_type="endpoint",
            token_count=7,
            http_method="GET",
            route_path="/pets",
            tags=["pets"],
        ),
    )


def _row(chunk: Chunk, chunk_index: int = 0, embedding: list[float] | None = None) -> DocumentChunk:
    """Map a TASK-301 chunk (plus its TASK-302 embedding) onto a ``document_chunks`` row."""
    metadata = chunk.metadata
    return DocumentChunk(
        project_id=metadata.project_id,
        document_id=metadata.document_id,
        chunk_index=chunk_index,
        chunk_type=metadata.chunk_type,
        content=chunk.content,
        token_count=metadata.token_count,
        chunk_metadata=metadata.model_dump(mode="json"),
        embedding=embedding,
    )


async def _seed_document(project_id: str | None = None) -> tuple[str, str]:
    """Insert a project (unless ``project_id`` is given) and a document; return their ids."""
    async with get_session_factory()() as session:
        if project_id is None:
            project = Project(name="Test project")
            session.add(project)
            await session.flush()
            project_id = project.id
        document = Document(
            project_id=project_id,
            filename="petstore.yaml",
            file_type=DocumentFileType.OPENAPI_YAML,
            storage_path=f"storage/documents/{uuid.uuid4().hex}.yaml",
        )
        session.add(document)
        await session.commit()
        return project_id, document.id


async def _add(*rows: DocumentChunk) -> list[str]:
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
        return [row.id for row in rows]


async def _get(chunk_id: str) -> DocumentChunk:
    async with get_session_factory()() as session:
        row = await session.get(DocumentChunk, chunk_id)
        assert row is not None
        return row


async def _rows(sql: str, **params: object) -> list[Any]:
    async with get_session_factory()() as session:
        return list((await session.execute(text(sql), params)).all())


def _vector_literal(vector: list[float]) -> str:
    """The text form pgvector accepts, produced by the column type's own bind processor."""
    process = TABLE.c.embedding.type.bind_processor(postgresql.dialect())  # type: ignore[no-untyped-call]
    assert process is not None
    literal: str = process(vector)
    return literal


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


# ------------------------------------------------------------------------------ fixtures


@pytest_asyncio.fixture
async def pg_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    assert TEST_DB_URL
    _configure(monkeypatch, tmp_path, TEST_DB_URL)
    await dispose_engine()
    yield
    await dispose_engine()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(pg_db: None) -> AsyncIterator[None]:
    """A database whose schema was built by ``alembic upgrade head`` and is dropped afterwards."""
    await _run_alembic("downgrade", "base")
    await _run_alembic("upgrade", "head")
    yield
    await dispose_engine()
    await _run_alembic("downgrade", "base")


# --------------------------------------------------------- no server needed


async def test_model_declares_columns_foreign_keys_and_constraints() -> None:
    table = TABLE
    assert DocumentChunk.__table__ is table

    embedding = table.c.embedding.type
    assert isinstance(embedding, Vector)
    assert embedding.dim == EMBEDDING_DIMENSIONS == 1536
    assert table.c.embedding.nullable
    assert isinstance(table.c.metadata.type, JSONB)
    assert DocumentChunk.chunk_metadata.property.columns[0] is table.c.metadata
    for column in ("project_id", "document_id", "chunk_index", "chunk_type", "content"):
        assert not table.c[column].nullable, column
    assert not table.c.token_count.nullable

    fks = {fk.parent.name: fk for fk in table.foreign_keys}
    assert {name: fk.column.table.name for name, fk in fks.items()} == {
        "project_id": "projects",
        "document_id": "documents",
        "endpoint_id": "endpoints",
    }
    assert all(fk.ondelete == "CASCADE" for fk in fks.values())
    assert table.c.endpoint_id.nullable

    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("document_id", "chunk_index") in unique

    assert Document.chunks.property.mapper.class_ is DocumentChunk
    assert DocumentChunk.document.property.mapper.class_ is Document


async def test_model_declares_an_hnsw_cosine_index_on_the_embedding() -> None:
    (index,) = [i for i in TABLE.indexes if i.name == HNSW_INDEX]
    assert [c.name for c in index.columns] == ["embedding"]
    assert index.dialect_options["postgresql"]["using"] == "hnsw"
    assert index.dialect_options["postgresql"]["ops"] == {"embedding": "vector_cosine_ops"}
    assert not index.dialect_options["postgresql"]["with"]  # pgvector's default m/ef_construction


async def test_a_task_302_embedding_binds_without_conversion() -> None:
    # TASK-302 returns ``list[float]``; the column's bind processor accepts it as is.
    process = TABLE.c.embedding.type.bind_processor(postgresql.dialect())  # type: ignore[no-untyped-call]
    assert process is not None
    assert process([0.5, -1.0, 0.25]) == "[0.5,-1.0,0.25]"
    assert process(None) is None


async def test_migration_0002_follows_0001_and_is_the_head() -> None:
    script = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    assert script.get_heads() == ["0002"]
    revision = script.get_revision("0002")
    assert revision is not None
    assert revision.down_revision == "0001"


async def test_migration_sql_enables_pgvector_and_builds_the_hnsw_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path, OFFLINE_URL)
    try:
        upgrade, downgrade = io.StringIO(), io.StringIO()
        await _run_alembic("upgrade", "0001:0002", sql=True, buffer=upgrade)
        await _run_alembic("downgrade", "0002:0001", sql=True, buffer=downgrade)
    finally:
        get_settings.cache_clear()

    sql = upgrade.getvalue()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert sql.index("CREATE EXTENSION") < sql.index("CREATE TABLE document_chunks")
    assert "embedding VECTOR(1536)" in sql
    assert (
        f"CREATE INDEX {HNSW_INDEX} ON document_chunks USING hnsw (embedding vector_cosine_ops)"
        in sql
    )
    assert "DROP TABLE document_chunks" in downgrade.getvalue()
    assert "DROP EXTENSION" not in downgrade.getvalue()


# --------------------------------------------------------- A. table persistence


@requires_postgres
async def test_chunk_content_and_metadata_are_persisted(db: None) -> None:
    project_id, document_id = await _seed_document()
    chunk = _chunk(project_id, document_id)

    (chunk_id,) = await _add(_row(chunk, chunk_index=3))

    stored = await _get(chunk_id)
    assert uuid.UUID(stored.id)
    assert (stored.project_id, stored.document_id) == (project_id, document_id)
    assert stored.endpoint_id is None
    assert stored.chunk_index == 3
    assert stored.chunk_type == "endpoint"
    assert stored.content == chunk.content
    assert stored.token_count == 7
    assert stored.chunk_metadata == chunk.metadata.model_dump(mode="json")
    assert ChunkMetadata.model_validate(stored.chunk_metadata) == chunk.metadata
    assert stored.embedding is None
    assert stored.created_at is not None


@requires_postgres
async def test_chunk_index_is_unique_per_document(db: None) -> None:
    project_id, document_id = await _seed_document()
    _, other_document_id = await _seed_document(project_id)
    await _add(_row(_chunk(project_id, document_id), chunk_index=0))
    await _add(_row(_chunk(project_id, other_document_id), chunk_index=0))  # other document: ok

    with pytest.raises(IntegrityError):
        await _add(_row(_chunk(project_id, document_id), chunk_index=0))


@requires_postgres
async def test_deleting_a_document_deletes_its_chunks(db: None) -> None:
    project_id, document_id = await _seed_document()
    (chunk_id,) = await _add(_row(_chunk(project_id, document_id)))

    async with get_session_factory()() as session:
        await session.execute(delete(Document).where(Document.id == document_id))
        await session.commit()
        assert await session.get(DocumentChunk, chunk_id) is None


# --------------------------------------------------------- B. embedding persistence


@requires_postgres
async def test_a_1536_dimension_embedding_round_trips_exactly(db: None) -> None:
    project_id, document_id = await _seed_document()
    embedding = _patterned_embedding()

    (chunk_id,) = await _add(_row(_chunk(project_id, document_id), embedding=embedding))

    stored = await _get(chunk_id)
    assert stored.embedding is not None
    assert len(stored.embedding) == EMBEDDING_DIMENSIONS
    assert stored.embedding == embedding
    (dims,) = await _rows("SELECT vector_dims(embedding) FROM document_chunks")
    assert dims[0] == EMBEDDING_DIMENSIONS


@requires_postgres
@pytest.mark.parametrize("dimensions", [3, EMBEDDING_DIMENSIONS - 1, EMBEDDING_DIMENSIONS + 1])
async def test_an_embedding_of_the_wrong_dimension_is_rejected(db: None, dimensions: int) -> None:
    project_id, document_id = await _seed_document()

    with pytest.raises(DBAPIError, match="expected 1536 dimensions"):
        await _add(_row(_chunk(project_id, document_id), embedding=[0.5] * dimensions))


# --------------------------------------------------------- C. extension and index


@requires_postgres
async def test_pgvector_extension_supports_hnsw(db: None) -> None:
    (row,) = await _rows("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    version = tuple(int(part) for part in row[0].split(".")[:2])
    assert version >= (0, 5)  # HNSW was added in pgvector 0.5.0


@requires_postgres
async def test_hnsw_index_uses_cosine_ops_on_the_embedding_column(db: None) -> None:
    (row,) = await _rows(
        """
        SELECT am.amname, attribute.attname, opclass.opcname, index.indnatts
        FROM pg_index AS index
        JOIN pg_class AS index_class ON index_class.oid = index.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index.indrelid
        JOIN pg_am AS am ON am.oid = index_class.relam
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = index.indrelid AND attribute.attnum = index.indkey[0]
        JOIN pg_opclass AS opclass ON opclass.oid = index.indclass[0]
        WHERE index_class.relname = :index_name AND table_class.relname = 'document_chunks'
        """,
        index_name=HNSW_INDEX,
    )
    assert tuple(row) == ("hnsw", "embedding", "vector_cosine_ops", 1)

    (definition,) = await _rows(
        "SELECT indexdef FROM pg_indexes WHERE indexname = :index_name", index_name=HNSW_INDEX
    )
    assert "USING hnsw (embedding vector_cosine_ops)" in definition[0]


@requires_postgres
async def test_migration_creates_the_table_foreign_keys_and_indexes(db: None) -> None:
    def read(connection: Connection) -> dict[str, Any]:
        inspector = inspect(connection)
        return {
            "fks": inspector.get_foreign_keys("document_chunks"),
            "indexes": {i["name"] for i in inspector.get_indexes("document_chunks")},
            "unique": inspector.get_unique_constraints("document_chunks"),
        }

    async with get_engine().connect() as connection:
        info = await connection.run_sync(read)

    assert {(fk["referred_table"], tuple(fk["constrained_columns"])) for fk in info["fks"]} == {
        ("projects", ("project_id",)),
        ("documents", ("document_id",)),
        ("endpoints", ("endpoint_id",)),
    }
    assert all(fk["options"]["ondelete"] == "CASCADE" for fk in info["fks"])
    assert {
        HNSW_INDEX,
        "ix_document_chunks_project_id",
        "ix_document_chunks_document_id",
        "ix_document_chunks_endpoint_id",
    } <= info["indexes"]
    assert [u["column_names"] for u in info["unique"]] == [["document_id", "chunk_index"]]


@requires_postgres
async def test_downgrade_drops_the_chunks_table_and_keeps_earlier_tables(db: None) -> None:
    await _run_alembic("downgrade", "0001")

    async def table_names() -> set[str]:
        async with get_engine().connect() as connection:
            return await connection.run_sync(lambda c: set(inspect(c).get_table_names()))

    tables = await table_names()
    assert "document_chunks" not in tables
    assert {"projects", "documents", "endpoints"} <= tables
    await _run_alembic("upgrade", "head")  # leave the schema in place for the teardown
    assert "document_chunks" in await table_names()


# --------------------------------------------------------- D. similarity search


@requires_postgres
async def test_cosine_search_ranks_chunks_by_similarity_within_the_project(db: None) -> None:
    project_id, document_id = await _seed_document()
    other_project_id, other_document_id = await _seed_document()
    query = _axis((0, 1.0))
    expected = {
        "same direction": (_axis((0, 2.0)), 0.0),  # magnitude does not matter for cosine
        "45 degrees": (_axis((0, 1.0), (1, 1.0)), 1 - 1 / math.sqrt(2)),
        "orthogonal": (_axis((1, 1.0)), 1.0),
        "opposite": (_axis((0, -1.0)), 2.0),
    }
    rows = [
        _row(_chunk(project_id, document_id, content=name), chunk_index=position, embedding=vector)
        for position, (name, (vector, _)) in enumerate(reversed(expected.items()))
    ]
    # Identical to the query, but in another tenant: must never be returned.
    rows.append(_row(_chunk(other_project_id, other_document_id, "foreign"), embedding=query))
    # Not embedded yet: must not be returned either.
    rows.append(_row(_chunk(project_id, document_id, "pending"), chunk_index=len(expected)))
    await _add(*rows)

    distance = DocumentChunk.embedding.cosine_distance(query).label("distance")
    statement = (
        select(DocumentChunk.content, distance)
        .where(DocumentChunk.project_id == project_id, DocumentChunk.embedding.is_not(None))
        .order_by(distance)
        .limit(10)
    )
    async with get_session_factory()() as session:
        results = (await session.execute(statement)).all()

    assert [content for content, _ in results] == list(expected)
    for (content, actual), (_, wanted) in zip(results, expected.values(), strict=True):
        assert actual == pytest.approx(wanted, abs=1e-6), content


# --------------------------------------------------------- E. query execution plan


@requires_postgres
async def test_explain_analyze_shows_the_hnsw_index_serving_the_similarity_query(
    db: None,
) -> None:
    project_id, document_id = await _seed_document()
    await _add(
        *(
            _row(
                _chunk(project_id, document_id, f"chunk {position}"),
                chunk_index=position,
                embedding=_axis((position, 1.0), (position + 1, 0.5)),
            )
            for position in range(50)
        )
    )
    async with get_session_factory()() as session:
        await session.execute(text("ANALYZE document_chunks"))
        await session.commit()

    async with get_session_factory()() as session:
        # A few dozen rows are always cheaper to scan; take the sequential scan away (for this
        # transaction only) so the planner shows whether the index can serve the query.
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        explained = (
            await session.execute(
                text(
                    "EXPLAIN (ANALYZE, FORMAT JSON) "
                    "SELECT id FROM document_chunks "
                    "ORDER BY embedding <=> CAST(:query AS vector) LIMIT 5"
                ),
                {"query": _vector_literal(_axis((0, 1.0)))},
            )
        ).scalar_one()
        await session.rollback()

    plan = (json.loads(explained) if isinstance(explained, str) else explained)[0]["Plan"]
    scans = [node for node in _plan_nodes(plan) if node.get("Index Name") == HNSW_INDEX]
    assert scans, f"HNSW index not used: {json.dumps(plan)}"
    assert scans[0]["Node Type"] == "Index Scan"
    assert scans[0]["Actual Rows"] == 5
