"""Project model: the tenant that owns documents and endpoints (docs/ARCHITECTURE.md 4.3)."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.endpoint import Endpoint


class Project(Base):
    """A tenant workspace. Deleting a project cascades to its documents and endpoints."""

    __tablename__ = "projects"
    # Fetch the server-generated ``updated_at`` in the same statement; an async session cannot
    # lazily reload an expired attribute.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ``lazy="raise"``: collections can be large, so they are only loaded when a query asks for
    # them explicitly (``selectinload``). ``passive_deletes`` leaves cascading to ON DELETE CASCADE.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
