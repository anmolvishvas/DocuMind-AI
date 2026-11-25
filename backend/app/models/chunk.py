"""DocumentChunk model: one embedded chunk of a document (docs/ARCHITECTURE.md 4.3).

A row stores a TASK-301 :class:`~app.schemas.chunk.Chunk` together with the TASK-302 embedding
of its content. The columns that searches filter or rank on (tenant, document, chunk type,
embedding) are relational; the full immutable metadata envelope (docs/RULES.md 5.2) is kept as
JSONB so retrieval can cite route paths, headings and tags without a join.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, generate_uuid

if TYPE_CHECKING:
    from app.models.document import Document

# Output size of text-embedding-3-small (TASK-302, ``Settings.embedding_dimensions``).
EMBEDDING_DIMENSIONS = 1536


class DocumentChunk(Base):
    """A chunk of a document and its embedding. Deleting the document cascades to its chunks.

    ``project_id`` is denormalised from the document (as on ``endpoints``) so every vector search
    can be tenant-filtered without a join (docs/RULES.md 5.3). ``chunk_index`` is the chunk's
    position in the chunker's output for its document, which identifies it within the document
    and restores document order.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        # Approximate nearest-neighbour index for cosine distance (``<=>``). pgvector's default
        # build parameters (m = 16, ef_construction = 64) are used.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Set for endpoint chunks once linked to their catalog row; NULL for narrative chunks.
    endpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``ChunkMetadata.model_dump(mode="json")``. The attribute cannot be called ``metadata``
    # (reserved by the declarative base), so only the column carries that name.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # A TASK-302 vector (``list[float]``) is assigned as is. NULL until the chunk is embedded.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["Document"] = relationship(back_populates="chunks", lazy="raise")
