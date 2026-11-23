"""Integration tests for relational endpoint persistence (TASK-204).

Tests marked ``requires_postgres`` need a real, disposable PostgreSQL (``TEST_DATABASE_URL``,
``postgresql+asyncpg://...``) and are skipped otherwise; they never fall back to SQLite or to
``DATABASE_URL``. The schema is created by running the real Alembic migrations (and dropped again
afterwards), so the migration is exercised by every test. The tests at the top of the file need no
server.

Endpoints are produced by the real TASK-202 parser from the spec fixtures in
``backend/tests/fixtures/openapi``.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.core.config import get_settings
from app.core.database import dispose_engine, get_db, get_engine, get_session_factory
from app.core.exceptions import DocumentNotFoundError, PersistenceError
from app.ingestion.openapi_parser import parse_openapi
from app.models import (
    Base,
    Document,
    DocumentFileType,
    DocumentStatus,
    Endpoint,
    Project,
)
from app.models.base import generate_uuid
from app.repositories.endpoint import EndpointRepository
from app.schemas.endpoint import Endpoint as ParsedEndpoint
from app.schemas.endpoint import ParsedSpecification
from sqlalchemy import func, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, InvalidRequestError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import configure_mappers, selectinload

pytestmark = pytest.mark.asyncio

BACKEND = Path(__file__).resolve().parents[2] / "backend"
FIXTURES = BACKEND / "tests" / "fixtures" / "openapi"
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL (a disposable PostgreSQL) is not set"
)

MINIMAL_SPEC = """
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /ping:
    get: {}
"""

DATE_SPEC = """
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /events:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: object
                example: {day: 2024-01-31, at: 2024-01-31T10:00:00Z}
"""


# ------------------------------------------------------------------------------ helpers


def _spec(name: str) -> ParsedSpecification:
    return parse_openapi((FIXTURES / name).read_bytes())


def _alembic(action: str, *args: str) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    getattr(command, action)(config, *args)


async def _run_alembic(action: str, *args: str) -> None:
    # Alembic's async env.py starts its own event loop, so it must run off this one.
    await asyncio.to_thread(_alembic, action, *args)


async def _seed_document(
    *, project_id: str | None = None, filename: str = "spec.yaml"
) -> tuple[str, str]:
    """Insert a project (unless ``project_id`` is given) and a document; return their ids."""
    async with get_session_factory()() as session:
        if project_id is None:
            project = Project(name="Test project")
            session.add(project)
            await session.flush()
            project_id = project.id
        document = Document(
            project_id=project_id,
            filename=filename,
            file_type=DocumentFileType.OPENAPI_YAML,
            storage_path=f"storage/documents/{uuid.uuid4().hex}.yaml",
        )
        session.add(document)
        await session.commit()
        return project_id, document.id


async def _persist(
    document_id: str, specification: ParsedSpecification
) -> list[Endpoint]:
    async with get_session_factory()() as session:
        rows = await EndpointRepository(session).add_parsed_specification(
            document_id, specification
        )
        await session.commit()
        return rows


async def _load(document_id: str) -> list[Endpoint]:
    async with get_session_factory()() as session:
        return await EndpointRepository(session).list_by_document(document_id)


async def _count_endpoints(document_id: str | None = None) -> int:
    statement = select(func.count()).select_from(Endpoint)
    if document_id is not None:
        statement = statement.where(Endpoint.document_id == document_id)
    async with get_session_factory()() as session:
        return (await session.execute(statement)).scalar_one()


async def _scalar(sql: str, **params: object) -> Any:
    async with get_session_factory()() as session:
        return (await session.execute(text(sql), params)).scalar_one()


def _missing_id() -> str:
    return generate_uuid()


def _endpoint(path: str = "/a", method: str = "GET", **fields: Any) -> ParsedEndpoint:
    return ParsedEndpoint.model_validate({"method": method, "path": path, **fields})


# ------------------------------------------------------------------------------ fixtures


@pytest_asyncio.fixture
async def pg_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    assert TEST_DB_URL
    for name in list(os.environ):
        if name.startswith(("DB_", "DATABASE_URL", "OPENAI_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no real .env is picked up
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()
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


@pytest_asyncio.fixture
async def unit_of_work(db: None) -> AsyncIterator[AsyncGenerator[AsyncSession, None]]:
    """The ``get_db`` dependency, closed on teardown even if the test fails half-way.

    An abandoned generator would keep its transaction open and block the schema teardown.
    """
    generator = get_db()
    yield generator
    await generator.aclose()


# --------------------------------------------------------- no server needed


async def test_models_declare_the_expected_tables_and_relationships() -> None:
    configure_mappers()  # raises if any relationship / back_populates pair is inconsistent
    tables = Base.metadata.tables
    assert {"projects", "documents", "endpoints"} <= set(tables)

    def targets(table: str) -> dict[str, str]:
        return {
            fk.parent.name: fk.column.table.name
            for fk in tables[table].foreign_keys  # type: ignore[attr-defined]
        }

    assert targets("documents") == {"project_id": "projects"}
    assert targets("endpoints") == {
        "project_id": "projects",
        "document_id": "documents",
    }
    for table, column in [("documents", "project_id"), ("endpoints", "document_id")]:
        (fk,) = [f for f in tables[table].foreign_keys if f.parent.name == column]
        assert fk.ondelete == "CASCADE"
        assert not fk.parent.nullable

    for column in ("tags", "parameters", "request_body", "responses", "security"):
        assert isinstance(tables["endpoints"].c[column].type, JSONB)

    assert Project.documents.property.mapper.class_ is Document
    assert Document.endpoints.property.mapper.class_ is Endpoint
    assert Endpoint.document.property.mapper.class_ is Document
    assert Endpoint.project.property.mapper.class_ is Project


async def test_generate_uuid_returns_distinct_36_character_strings() -> None:
    first, second = generate_uuid(), generate_uuid()
    assert first != second
    assert len(first) == 36
    assert str(uuid.UUID(first)) == first


# --------------------------------------------------------- migration


@requires_postgres
async def test_migration_creates_tables_columns_foreign_keys_and_indexes(
    db: None,
) -> None:
    def read(connection: Connection) -> dict[str, Any]:
        inspector = inspect(connection)
        return {
            "tables": set(inspector.get_table_names()),
            "endpoint_columns": {
                c["name"]: c for c in inspector.get_columns("endpoints")
            },
            "endpoint_fks": inspector.get_foreign_keys("endpoints"),
            "document_fks": inspector.get_foreign_keys("documents"),
            "endpoint_indexes": {
                i["name"]: i for i in inspector.get_indexes("endpoints")
            },
            "document_indexes": {i["name"] for i in inspector.get_indexes("documents")},
        }

    async with get_engine().connect() as connection:
        info = await connection.run_sync(read)

    assert {"projects", "documents", "endpoints"} <= info["tables"]
    for column in ("tags", "parameters", "request_body", "responses", "security"):
        assert isinstance(info["endpoint_columns"][column]["type"], JSONB)
        assert not info["endpoint_columns"][column]["nullable"]

    fks = {
        (fk["referred_table"], tuple(fk["constrained_columns"])): fk
        for fk in info["endpoint_fks"]
    }
    assert set(fks) == {("projects", ("project_id",)), ("documents", ("document_id",))}
    assert all(fk["options"]["ondelete"] == "CASCADE" for fk in fks.values())
    assert [fk["referred_table"] for fk in info["document_fks"]] == ["projects"]

    assert info["endpoint_indexes"]["ix_endpoints_project_id_http_method_route_path"][
        "column_names"
    ] == ["project_id", "http_method", "route_path"]
    assert {"ix_endpoints_project_id", "ix_endpoints_document_id"} <= set(
        info["endpoint_indexes"]
    )
    assert "ix_documents_project_id" in info["document_indexes"]


@requires_postgres
async def test_models_and_migration_have_no_drift(db: None) -> None:
    # ``alembic check`` fails if autogenerate would still produce operations.
    await _run_alembic("check")


@requires_postgres
async def test_downgrade_removes_the_tables(db: None) -> None:
    await _run_alembic("downgrade", "base")

    async def table_names() -> set[str]:
        async with get_engine().connect() as connection:
            return await connection.run_sync(
                lambda c: set(inspect(c).get_table_names())
            )

    assert not {"projects", "documents", "endpoints"} & await table_names()
    await _run_alembic(
        "upgrade", "head"
    )  # leave the schema in place for the fixture teardown
    assert {"projects", "documents", "endpoints"} <= await table_names()


# --------------------------------------------------------- persistence


@requires_postgres
async def test_parsed_openapi_endpoints_are_persisted(db: None) -> None:
    specification = _spec("petstore_3_0.yaml")
    project_id, document_id = await _seed_document()

    returned = await _persist(document_id, specification)

    stored = await _load(document_id)
    assert len(returned) == len(stored) == len(specification.endpoints) > 0
    assert {(e.http_method, e.route_path) for e in stored} == {
        (e.method, e.path) for e in specification.endpoints
    }
    by_operation = {e.operation_id: e for e in stored}
    pet = by_operation["findPetsByStatus"]
    assert pet.http_method == "GET"
    assert pet.route_path == "/pet/findByStatus"
    assert pet.summary == "Finds Pets by status"
    assert pet.tags == ["pet"]
    assert pet.id and uuid.UUID(pet.id)
    assert pet.created_at is not None
    assert all(e.project_id == project_id for e in stored)


@requires_postgres
async def test_endpoints_belong_to_the_correct_document(db: None) -> None:
    petstore, stripe = _spec("petstore_3_0.yaml"), _spec("stripe_sample.yaml")
    project_id, petstore_id = await _seed_document(filename="petstore.yaml")
    _, stripe_id = await _seed_document(project_id=project_id, filename="stripe.yaml")

    await _persist(petstore_id, petstore)
    await _persist(stripe_id, stripe)

    petstore_rows, stripe_rows = await _load(petstore_id), await _load(stripe_id)
    assert {(e.http_method, e.route_path) for e in petstore_rows} == {
        (e.method, e.path) for e in petstore.endpoints
    }
    assert {(e.http_method, e.route_path) for e in stripe_rows} == {
        (e.method, e.path) for e in stripe.endpoints
    }
    assert {e.document_id for e in petstore_rows} == {petstore_id}
    assert {e.document_id for e in stripe_rows} == {stripe_id}
    assert await _count_endpoints() == len(petstore.endpoints) + len(stripe.endpoints)


@requires_postgres
async def test_endpoints_take_their_project_from_the_document(db: None) -> None:
    first_project, first_document = await _seed_document()
    second_project, second_document = await _seed_document()
    specification = _spec("petstore_3_0.yaml")

    await _persist(first_document, specification)
    await _persist(second_document, specification)

    assert {e.project_id for e in await _load(first_document)} == {first_project}
    assert {e.project_id for e in await _load(second_document)} == {second_project}


@requires_postgres
async def test_many_endpoints_are_persisted_for_one_document(db: None) -> None:
    _, document_id = await _seed_document()
    specification = ParsedSpecification(
        spec_version="3.0",
        endpoints=[
            _endpoint(f"/items/{n}", method)
            for n in range(20)
            for method in ("GET", "POST")
        ],
    )

    await _persist(document_id, specification)

    stored = await _load(document_id)
    assert len(stored) == 40
    assert len({e.id for e in stored}) == 40
    assert {e.document_id for e in stored} == {document_id}


@requires_postgres
async def test_parameters_request_body_and_responses_are_stored_as_jsonb(
    db: None,
) -> None:
    _, document_id = await _seed_document()
    await _persist(document_id, _spec("petstore_3_0.yaml"))

    row = (
        "SELECT jsonb_typeof(tags), jsonb_typeof(parameters), jsonb_typeof(request_body), "
        "jsonb_typeof(responses), jsonb_typeof(security) FROM endpoints WHERE operation_id = :op"
    )
    async with get_session_factory()() as session:
        find = (await session.execute(text(row), {"op": "findPetsByStatus"})).one()
    assert tuple(find) == ("array", "array", "object", "object", "array")

    # JSONB operators work on the stored data: containment, path extraction, nested schemas.
    assert await _scalar(
        "SELECT parameters @> CAST(:needle AS jsonb) FROM endpoints WHERE operation_id = :op",
        needle='[{"name": "status", "location": "query", "required": false}]',
        op="findPetsByStatus",
    )
    assert (
        await _scalar(
            "SELECT parameters #>> '{0,schema_definition,default}' FROM endpoints "
            "WHERE operation_id = :op",
            op="findPetsByStatus",
        )
        == "available"
    )
    assert (
        await _scalar(
            "SELECT responses #>> '{200,content,0,schema_definition,type}' FROM endpoints "
            "WHERE operation_id = :op",
            op="findPetsByStatus",
        )
        == "array"
    )
    assert (
        await _scalar(
            "SELECT responses #>> '{404,description}' FROM endpoints WHERE operation_id = :op",
            op="getPetById",
        )
        is not None
    )
    assert (
        await _scalar(
            "SELECT request_body #>> '{content,0,media_type}' FROM endpoints "
            "WHERE operation_id = :op",
            op="addPet",
        )
        == "application/json"
    )
    assert (
        await _scalar(
            "SELECT jsonb_array_length(parameters) FROM endpoints WHERE operation_id = :op",
            op="deletePet",
        )
        == 2
    )


@requires_postgres
async def test_stored_json_round_trips_the_parser_output(db: None) -> None:
    specification = _spec("petstore_3_0.yaml")
    _, document_id = await _seed_document()
    await _persist(document_id, specification)

    stored = {(e.http_method, e.route_path): e for e in await _load(document_id)}
    for parsed in specification.endpoints:
        data = parsed.model_dump(mode="json")
        row = stored[(parsed.method, parsed.path)]
        assert row.parameters == data["parameters"]
        assert row.request_body == (data["request_body"] or {})
        assert row.responses == {
            r["status_code"]: {k: v for k, v in r.items() if k != "status_code"}
            for r in data["responses"]
        }


@requires_postgres
async def test_optional_fields_default_to_null_or_empty_json(db: None) -> None:
    _, document_id = await _seed_document()
    await _persist(document_id, parse_openapi(MINIMAL_SPEC))

    (row,) = await _load(document_id)
    assert (row.http_method, row.route_path) == ("GET", "/ping")
    assert row.summary is None
    assert row.description is None
    assert row.operation_id is None
    assert row.tags == []
    assert row.parameters == []
    assert row.request_body == {}
    assert row.responses == {}
    assert row.security == []


@requires_postgres
async def test_non_json_native_values_in_specs_are_converted(db: None) -> None:
    # YAML parses unquoted dates into ``date``/``datetime`` objects, which JSONB cannot store as is.
    _, document_id = await _seed_document()
    await _persist(document_id, parse_openapi(DATE_SPEC))

    (row,) = await _load(document_id)
    example = row.responses["200"]["content"][0]["schema_definition"]["example"]
    assert example == {"day": "2024-01-31", "at": "2024-01-31T10:00:00Z"}


@requires_postgres
async def test_recursive_schemas_are_stored_on_the_document(db: None) -> None:
    specification = _spec("stripe_sample.yaml")
    assert specification.recursive_schemas  # the fixture really contains cycles
    _, document_id = await _seed_document()

    await _persist(document_id, specification)

    async with get_session_factory()() as session:
        document = await session.get(Document, document_id)
    assert document is not None
    assert set(document.recursive_schemas) == set(specification.recursive_schemas)
    assert (
        document.recursive_schemas["#/components/schemas/customer"]
        == (specification.recursive_schemas["#/components/schemas/customer"])
    )
    # The endpoint keeps the unexpanded ``$ref`` so the cycle can be resolved from the document.
    assert await _scalar(
        "SELECT count(*) FROM endpoints WHERE CAST(responses AS text) LIKE '%\"$ref\"%'"
    )


@requires_postgres
async def test_listing_is_ordered_and_scoped_to_one_document(db: None) -> None:
    _, document_id = await _seed_document()
    _, other_id = await _seed_document()
    await _persist(
        document_id,
        ParsedSpecification(
            spec_version="3.0",
            endpoints=[
                _endpoint("/b"),
                _endpoint("/a", "POST"),
                _endpoint("/a", "GET"),
            ],
        ),
    )
    await _persist(
        other_id, ParsedSpecification(spec_version="3.0", endpoints=[_endpoint("/z")])
    )

    assert [(e.route_path, e.http_method) for e in await _load(document_id)] == [
        ("/a", "GET"),
        ("/a", "POST"),
        ("/b", "GET"),
    ]
    assert await _load(_missing_id()) == []


# --------------------------------------------------------- invalid references


@requires_postgres
async def test_unknown_document_is_rejected_without_writing(db: None) -> None:
    async with get_session_factory()() as session:
        with pytest.raises(DocumentNotFoundError) as info:
            await EndpointRepository(session).add_parsed_specification(
                _missing_id(), _spec("petstore_3_0.yaml")
            )
    assert info.value.code == "DOCUMENT_NOT_FOUND"
    assert info.value.status_code == 404
    assert await _count_endpoints() == 0


@requires_postgres
async def test_database_rejects_endpoints_with_dangling_foreign_keys(db: None) -> None:
    project_id, document_id = await _seed_document()
    orphans = {
        "unknown document": {"project_id": project_id, "document_id": _missing_id()},
        "unknown project": {"project_id": _missing_id(), "document_id": document_id},
    }
    for fields in orphans.values():
        async with get_session_factory()() as session:
            session.add(Endpoint(http_method="GET", route_path="/x", **fields))
            with pytest.raises(IntegrityError):
                await session.flush()
    assert await _count_endpoints() == 0


# --------------------------------------------------------- transactions and errors


@requires_postgres
async def test_dependency_session_commits_persisted_endpoints(
    db: None, unit_of_work: AsyncGenerator[AsyncSession, None]
) -> None:
    _, document_id = await _seed_document()
    specification = _spec("petstore_3_0.yaml")

    generator = unit_of_work
    session = await anext(generator)
    await EndpointRepository(session).add_parsed_specification(
        document_id, specification
    )
    assert await _count_endpoints() == 0  # not visible to others before the commit
    with pytest.raises(StopAsyncIteration):
        await anext(generator)

    assert await _count_endpoints(document_id) == len(specification.endpoints)


@requires_postgres
async def test_dependency_session_rolls_back_on_error(
    db: None, unit_of_work: AsyncGenerator[AsyncSession, None]
) -> None:
    _, document_id = await _seed_document()

    generator = unit_of_work
    session = await anext(generator)
    await EndpointRepository(session).add_parsed_specification(
        document_id, _spec("petstore_3_0.yaml")
    )
    with pytest.raises(RuntimeError, match="later step failed"):
        await generator.athrow(RuntimeError("later step failed"))

    assert await _count_endpoints() == 0


@requires_postgres
async def test_database_errors_become_persistence_errors_and_nothing_is_kept(
    db: None, unit_of_work: AsyncGenerator[AsyncSession, None]
) -> None:
    _, document_id = await _seed_document()
    # PostgreSQL text cannot contain NUL, so the second endpoint makes the batch insert fail.
    specification = ParsedSpecification(
        spec_version="3.0",
        endpoints=[_endpoint("/ok"), _endpoint("/bad", summary="nul\x00byte")],
    )

    generator = unit_of_work
    session = await anext(generator)
    with pytest.raises(PersistenceError) as info:
        await EndpointRepository(session).add_parsed_specification(
            document_id, specification
        )
    assert info.value.code == "PERSISTENCE_ERROR"
    assert info.value.status_code == 500
    assert isinstance(info.value.__cause__, SQLAlchemyError)
    assert "nul" not in info.value.message  # no row data leaks into the message
    with pytest.raises(PersistenceError):
        await generator.athrow(info.value)

    assert await _count_endpoints() == 0


# --------------------------------------------------------- relationships and cascades


@requires_postgres
async def test_deleting_a_document_deletes_only_its_endpoints(db: None) -> None:
    project_id, first = await _seed_document()
    _, second = await _seed_document(project_id=project_id)
    specification = _spec("petstore_3_0.yaml")
    await _persist(first, specification)
    await _persist(second, specification)

    async with get_session_factory()() as session:
        document = await session.get(Document, first)
        assert document is not None
        await session.delete(document)
        await session.commit()

    assert await _count_endpoints(first) == 0
    assert await _count_endpoints(second) == len(specification.endpoints)


@requires_postgres
async def test_deleting_a_project_deletes_documents_and_endpoints(db: None) -> None:
    project_id, document_id = await _seed_document()
    await _persist(document_id, _spec("petstore_3_0.yaml"))
    _, other_document = await _seed_document()  # belongs to a different project
    await _persist(other_document, _spec("petstore_3_0.yaml"))

    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        await session.delete(project)
        await session.commit()

    assert await _count_endpoints(document_id) == 0
    assert (
        await _scalar(
            "SELECT count(*) FROM documents WHERE project_id = :p", p=project_id
        )
        == 0
    )
    assert await _count_endpoints(other_document) > 0  # other tenants are untouched


@requires_postgres
async def test_relationships_load_explicitly_and_are_never_lazy(db: None) -> None:
    project_id, document_id = await _seed_document()
    await _persist(document_id, _spec("petstore_3_0.yaml"))

    async with get_session_factory()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        with pytest.raises(InvalidRequestError):  # lazy="raise": no hidden queries
            _ = project.documents

    async with get_session_factory()() as session:
        loaded = (
            await session.execute(
                select(Project)
                .where(Project.id == project_id)
                .options(
                    selectinload(Project.documents).selectinload(Document.endpoints)
                )
            )
        ).scalar_one()
        assert [d.id for d in loaded.documents] == [document_id]
        assert len(loaded.documents[0].endpoints) == 5


@requires_postgres
async def test_document_defaults_and_updated_at(db: None) -> None:
    _, document_id = await _seed_document()

    async with get_session_factory()() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        assert document.status is DocumentStatus.PENDING
        assert document.file_type is DocumentFileType.OPENAPI_YAML
        assert document.error_message is None
        assert document.recursive_schemas == {}
        created_at = document.created_at

        document.status = DocumentStatus.FAILED
        document.error_message = "boom"
        await session.commit()
        # ``updated_at`` is refreshed by the database and readable without a lazy reload.
        assert document.updated_at > created_at

    assert (
        await _scalar("SELECT status FROM documents WHERE id = :i", i=document_id)
        == "FAILED"
    )
