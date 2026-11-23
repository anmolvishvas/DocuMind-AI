"""Endpoint model: one extracted API operation (docs/ARCHITECTURE.md 4.3).

Flexible, nested OpenAPI data (tags, parameters, request body, responses, security) is stored as
JSONB; the columns that are queried and filtered on (method, path, operation id) stay relational.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.project import Project


class Endpoint(Base):
    """A single HTTP operation (method + route path) extracted from a document.

    ``project_id`` is denormalised from the document (``document.project_id``) so every catalog
    query can be tenant-filtered without a join; the persistence repository keeps them equal.
    """

    __tablename__ = "endpoints"
    __table_args__ = (
        Index(
            "ix_endpoints_project_id_http_method_route_path",
            "project_id",
            "http_method",
            "route_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    http_method: Mapped[str] = mapped_column(String(10), nullable=False)
    route_path: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # ``{}`` means the operation declares no request body.
    request_body: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Keyed by status code ("200", "2XX", "default").
    responses: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    security: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="endpoints", lazy="raise")
    document: Mapped["Document"] = relationship(back_populates="endpoints", lazy="raise")
