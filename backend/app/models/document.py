"""Document model: an uploaded specification or documentation file (docs/ARCHITECTURE.md 4.3)."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.chunk import DocumentChunk
    from app.models.endpoint import Endpoint
    from app.models.project import Project


class DocumentFileType(StrEnum):
    """Kind of uploaded document, stored in ``documents.file_type``."""

    OPENAPI_JSON = "openapi_json"
    OPENAPI_YAML = "openapi_yaml"
    PDF = "pdf"
    MARKDOWN = "markdown"


class DocumentStatus(StrEnum):
    """Ingestion lifecycle of a document, stored in ``documents.status``."""

    PENDING = "PENDING"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"


def _enum_column(enum_class: type[StrEnum]) -> Enum:
    """VARCHAR(50) column that stores enum *values* and returns enum members.

    Non-native and unconstrained, matching the architecture DDL (plain VARCHAR): adding a
    status later needs no ``ALTER TYPE`` migration.
    """
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=50,
        values_callable=lambda members: [member.value for member in members],
    )


class Document(Base):
    """An uploaded file of one project. Deleting it cascades to its endpoints and chunks."""

    __tablename__ = "documents"
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012  (see Project)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[DocumentFileType] = mapped_column(
        _enum_column(DocumentFileType), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        _enum_column(DocumentStatus),
        nullable=False,
        default=DocumentStatus.PENDING,
        server_default=DocumentStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    # Targets of the ``$ref`` pointers the OpenAPI parser left in place because of recursion
    # (ParsedSpecification.recursive_schemas), keyed by pointer. They are document-wide, so they
    # live here rather than being duplicated on every endpoint that reaches them.
    recursive_schemas: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="documents", lazy="raise")
    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
