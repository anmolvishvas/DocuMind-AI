"""Output models of the chunker (TASK-301).

A :class:`Chunk` is the unit that is embedded (TASK-302) and stored in ``document_chunks``
(TASK-303). :class:`ChunkMetadata` is the immutable metadata envelope required by
docs/RULES.md section 5.2.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.endpoint import HttpMethod

ChunkType = Literal["endpoint", "narrative", "schema"]


class _ChunkModel(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True, frozen=True)


class ChunkMetadata(_ChunkModel):
    """Immutable metadata envelope attached to every chunk."""

    project_id: str = Field(description="Tenant the chunk belongs to; every search filters on it.")
    document_id: str = Field(description="Document the chunk was produced from.")
    source_filename: str = Field(description="Original filename of that document.")
    chunk_type: ChunkType = Field(description="'endpoint' or 'narrative' ('schema' is reserved).")
    token_count: int = Field(ge=0, description="Tokens in the chunk content, as measured.")
    http_method: HttpMethod | None = Field(
        default=None, description="HTTP method; set on endpoint chunks only."
    )
    route_path: str | None = Field(
        default=None, description="Route path template; set on endpoint chunks only."
    )
    tags: list[str] = Field(default_factory=list, description="Endpoint tags (endpoint chunks).")
    heading_path: list[str] = Field(
        default_factory=list,
        description="Headings from the top-level ancestor down to the chunk's section (narrative).",
    )
    truncated: bool = Field(
        default=False,
        description="True when detail had to be reduced or cut to fit the token limit.",
    )


class Chunk(_ChunkModel):
    """Self-contained text to embed, plus its metadata."""

    content: str = Field(description="Chunk text (Markdown).")
    metadata: ChunkMetadata = Field(description="Metadata envelope.")
