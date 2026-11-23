"""Repository that persists parsed OpenAPI endpoints (TASK-204).

The repository works on the session it is given and never commits or rolls back: the caller
owns the transaction (``app.core.database.get_db`` commits on success and rolls back on any
exception). After :class:`PersistenceError` the session's transaction is unusable until that
rollback happens, so let the error propagate.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DocumentNotFoundError, PersistenceError
from app.models.document import Document
from app.models.endpoint import Endpoint
from app.schemas.endpoint import Endpoint as ParsedEndpoint
from app.schemas.endpoint import ParsedSpecification

logger = logging.getLogger(__name__)


def _to_row(project_id: str, document_id: str, parsed: ParsedEndpoint) -> Endpoint:
    """Convert one parser result into an :class:`Endpoint` row.

    ``model_dump(mode="json")`` yields only JSON-native values (dates inside schema examples
    become ISO strings, non-string keys become strings), which is what JSONB can store.
    """
    data = parsed.model_dump(mode="json")
    # The parser output is a list; the table keys responses by status code for direct lookup,
    # so the code moves out of each entry and becomes its key.
    responses = {entry.pop("status_code"): entry for entry in data["responses"]}
    # ``security`` is intentionally not set: the parser (TASK-202) does not extract it yet, so the
    # column keeps its default of ``[]``.
    return Endpoint(
        project_id=project_id,
        document_id=document_id,
        http_method=data["method"],
        route_path=data["path"],
        summary=data["summary"],
        description=data["description"],
        operation_id=data["operation_id"],
        tags=data["tags"],
        parameters=data["parameters"],
        request_body=data["request_body"] or {},
        responses=responses,
    )


class EndpointRepository:
    """Reads and writes the ``endpoints`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_parsed_specification(
        self, document_id: str, specification: ParsedSpecification
    ) -> list[Endpoint]:
        """Persist every endpoint of ``specification`` under ``document_id``.

        The endpoints inherit the document's ``project_id`` (so the two can never disagree), and the
        specification's recursive schemas are stored on the document. All rows are written in the
        caller's transaction; nothing is committed here.

        Raises:
            DocumentNotFoundError: no document has this id.
            PersistenceError: the database rejected the write.
        """
        document = await self._session.get(Document, document_id)
        if document is None:
            raise DocumentNotFoundError(
                "Document not found; endpoints must belong to an existing document.",
                code="DOCUMENT_NOT_FOUND",
            )

        rows = [_to_row(document.project_id, document.id, ep) for ep in specification.endpoints]
        document.recursive_schemas = specification.model_dump(
            mode="json", include={"recursive_schemas"}
        )["recursive_schemas"]
        self._session.add_all(rows)
        try:
            await self._session.flush()
        except SQLAlchemyError as exc:
            logger.exception(
                "failed to persist endpoints",
                extra={"document_id": document_id, "endpoint_count": len(rows)},
            )
            raise PersistenceError("Failed to persist endpoints.") from exc
        logger.info(
            "persisted endpoints",
            extra={"document_id": document_id, "endpoint_count": len(rows)},
        )
        return rows

    async def list_by_document(self, document_id: str) -> list[Endpoint]:
        """Return the endpoints of one document ordered by route path, then HTTP method."""
        result = await self._session.scalars(
            select(Endpoint)
            .where(Endpoint.document_id == document_id)
            .order_by(Endpoint.route_path, Endpoint.http_method)
        )
        return list(result)
