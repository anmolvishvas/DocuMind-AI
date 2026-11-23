"""Document upload DTOs."""

from pydantic import BaseModel, ConfigDict, Field


class DocumentUploadResponse(BaseModel):
    """Public metadata for a staged upload. Never includes server filesystem paths."""

    model_config = ConfigDict(strict=True, from_attributes=True)

    original_filename: str = Field(description="Sanitised filename supplied by the client.")
    stored_filename: str = Field(description="Server-generated '<uuid><ext>' identifier.")
    extension: str = Field(description="Validated lowercase file extension.")
    mime_type: str = Field(description="Canonical MIME type for the file format.")
    size_bytes: int = Field(description="Size of the stored file in bytes.")
