"""Document upload routes."""

from fastapi import APIRouter, Depends, UploadFile

from app.core.config import get_settings
from app.ingestion.validator import UploadValidator
from app.schemas.document import DocumentUploadResponse

router = APIRouter(prefix="/documents", tags=["documents"])


def get_upload_validator() -> UploadValidator:
    """Build the validator from application settings (overridable in tests)."""
    settings = get_settings()
    return UploadValidator(settings.document_storage_dir, settings.max_upload_size_bytes)


@router.post("/upload", status_code=201)
async def upload_document(
    file: UploadFile,
    validator: UploadValidator = Depends(get_upload_validator),  # noqa: B008
) -> DocumentUploadResponse:
    """Validate an uploaded document and stage it on disk.

    Invalid uploads raise ``InvalidUploadError`` (400) or ``FileTooLargeError`` (413),
    which the application's domain error handler renders.
    """
    staged = await validator.validate_and_stage(file, file.filename, file.content_type)
    return DocumentUploadResponse.model_validate(staged)
