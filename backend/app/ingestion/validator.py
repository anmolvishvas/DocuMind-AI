"""Upload validation and secure staging (TASK-201).

Validates an uploaded document (extension, declared MIME type, size, content) while
streaming it to a UUID-named file inside the configured storage directory. Nothing
is parsed here; parsing belongs to later ingestion tasks.

Security notes:
- The client filename is never used on disk; only its validated extension is kept.
- The client ``Content-Type`` is only checked for consistency, never trusted: content
  is always inspected (``%PDF-`` signature, or strict UTF-8 text checks).
- Size is enforced on the bytes actually received, in bounded chunks.
- Any failure (including cancellation) removes the partially written file.
"""

import codecs
import logging
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final, Protocol

import aiofiles
import aiofiles.os
from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import FileTooLargeError, InvalidUploadError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES: Final[int] = 20 * 1024 * 1024
CHUNK_SIZE: Final[int] = 64 * 1024
PDF_SIGNATURE: Final[bytes] = b"%PDF-"
_MAX_NAME_LENGTH: Final[int] = 255
_MAX_NAME_COLLISION_RETRIES: Final[int] = 5

_OCTET_STREAM: Final[str] = "application/octet-stream"
_YAML_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
        "text/plain",
        _OCTET_STREAM,
    }
)

# extension -> (canonical MIME type, acceptable client-declared MIME types).
# ``application/octet-stream`` is tolerated everywhere because many clients send it
# for unknown types; it never skips content validation.
_MIME_RULES: Final[dict[str, tuple[str, frozenset[str]]]] = {
    ".json": ("application/json", frozenset({"application/json", "text/json", _OCTET_STREAM})),
    ".yaml": ("application/yaml", _YAML_MIME_TYPES),
    ".yml": ("application/yaml", _YAML_MIME_TYPES),
    ".pdf": ("application/pdf", frozenset({"application/pdf", "application/x-pdf", _OCTET_STREAM})),
    ".md": (
        "text/markdown",
        frozenset({"text/markdown", "text/x-markdown", "text/plain", _OCTET_STREAM}),
    ),
    ".txt": ("text/plain", frozenset({"text/plain", _OCTET_STREAM})),
}
ALLOWED_EXTENSIONS: Final[frozenset[str]] = frozenset(_MIME_RULES)

# C0 control bytes that never appear in real text files (tab, LF, FF, CR are allowed).
_FORBIDDEN_CONTROL_BYTES: Final[bytes] = (
    bytes(b for b in range(0x20) if b not in (0x09, 0x0A, 0x0C, 0x0D)) + b"\x7f"
)


class SupportsAsyncRead(Protocol):
    """Minimal async file interface (satisfied by ``fastapi.UploadFile``)."""

    async def read(self, size: int = -1, /) -> bytes: ...


class StagedFile(BaseModel):
    """Metadata describing a validated, staged upload."""

    model_config = ConfigDict(strict=True, frozen=True)

    original_filename: str = Field(description="Sanitised client-supplied filename (display only).")
    stored_filename: str = Field(description="Generated '<uuid><ext>' name used on disk.")
    extension: str = Field(description="Validated lowercase extension, including the dot.")
    mime_type: str = Field(description="Canonical MIME type derived from the extension.")
    size_bytes: int = Field(ge=1, description="Number of bytes received and stored.")
    staged_path: Path = Field(description="Absolute path of the staged file (internal use only).")


def sanitize_filename(filename: str | None) -> str:
    """Return a display-safe basename: no directories, no control characters, bounded."""
    raw = filename or ""
    # Handle both separator styles regardless of the host OS.
    base = PureWindowsPath(PurePosixPath(raw).name).name
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip()
    return cleaned[-_MAX_NAME_LENGTH:]


def validate_extension(filename: str) -> str:
    """Return the lowercase extension of ``filename`` or raise ``InvalidUploadError``."""
    # Only the final suffix counts: "a.json.exe" -> ".exe" -> rejected.
    suffix = PurePosixPath(filename).suffix.lower()
    if not suffix:
        raise InvalidUploadError("File must have an extension.", "MISSING_EXTENSION")
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise InvalidUploadError(
            f"Unsupported file extension. Allowed extensions: {allowed}.", "UNSUPPORTED_EXTENSION"
        )
    return suffix


def validate_mime_type(extension: str, declared: str | None) -> str:
    """Check the client-declared MIME type is plausible; return the canonical MIME type."""
    canonical, acceptable = _MIME_RULES[extension]
    if declared:
        normalized = declared.split(";", 1)[0].strip().lower()
        if normalized and normalized not in acceptable:
            raise InvalidUploadError(
                f"Content type does not match the '{extension}' file extension.", "MIME_MISMATCH"
            )
    return canonical


def _validate_text_chunk(
    chunk: bytes, decoder: codecs.IncrementalDecoder, *, final: bool = False
) -> None:
    """Incrementally verify text content: valid UTF-8 with no NUL/control bytes."""
    if chunk.translate(None, _FORBIDDEN_CONTROL_BYTES) != chunk:
        raise InvalidUploadError("File content is binary, not text.", "INVALID_CONTENT")
    try:
        decoder.decode(chunk, final=final)
    except UnicodeDecodeError:
        raise InvalidUploadError("Text files must be valid UTF-8.", "INVALID_CONTENT") from None


def _validate_first_chunk(extension: str, head: bytes) -> None:
    """Format-specific checks that only need the start of the file."""
    if extension == ".pdf":
        if not head.startswith(PDF_SIGNATURE):
            raise InvalidUploadError("File is not a valid PDF.", "INVALID_CONTENT")
    elif extension == ".json":
        # A leading '{' or '[' (after optional BOM/whitespace) cheaply rejects non-JSON
        # text without parsing it (parsing is a later task).
        stripped = head.removeprefix(codecs.BOM_UTF8).lstrip()
        if stripped[:1] not in (b"{", b"["):
            raise InvalidUploadError("File is not valid JSON.", "INVALID_CONTENT")


class UploadValidator:
    """Validates uploads and stages them on disk under UUID filenames."""

    def __init__(self, storage_dir: Path, max_size_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self._storage_dir = storage_dir
        self._max_size_bytes = max_size_bytes

    async def validate_and_stage(
        self, file: SupportsAsyncRead, filename: str | None, content_type: str | None
    ) -> StagedFile:
        """Validate ``file`` and stream it into the storage directory.

        Raises:
            InvalidUploadError: bad extension, MIME type, or content (HTTP 400).
            FileTooLargeError: more than the allowed number of bytes received (HTTP 413).
        """
        display_name = sanitize_filename(filename)
        extension = validate_extension(display_name)
        mime_type = validate_mime_type(extension, content_type)

        await aiofiles.os.makedirs(self._storage_dir, exist_ok=True)
        staged_path, size = await self._stream_to_disk(file, extension)
        return StagedFile(
            original_filename=display_name,
            stored_filename=staged_path.name,
            extension=extension,
            mime_type=mime_type,
            size_bytes=size,
            staged_path=staged_path,
        )

    async def _stream_to_disk(self, file: SupportsAsyncRead, extension: str) -> tuple[Path, int]:
        """Write the upload chunk by chunk, validating as we go. Returns (path, size)."""
        staged_path = await self._reserve_path(extension)
        size = 0
        is_text = extension != ".pdf"
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            async with aiofiles.open(staged_path, "wb") as handle:
                while chunk := await file.read(CHUNK_SIZE):
                    if size == 0:
                        _validate_first_chunk(extension, chunk)
                    size += len(chunk)
                    if size > self._max_size_bytes:
                        limit_mb = self._max_size_bytes // (1024 * 1024)
                        raise FileTooLargeError(f"File exceeds the maximum size of {limit_mb} MB.")
                    if is_text:
                        _validate_text_chunk(chunk, decoder)
                    await handle.write(chunk)
            if size == 0:
                raise InvalidUploadError("File is empty.", "EMPTY_FILE")
            if is_text:
                _validate_text_chunk(b"", decoder, final=True)
        except BaseException:
            # BaseException so cancellation also removes the partial file; always re-raised.
            await self._discard(staged_path)
            raise
        return staged_path, size

    async def _reserve_path(self, extension: str) -> Path:
        """Atomically create an empty UUID-named file (never overwrites an existing one)."""
        storage_dir = self._storage_dir.resolve()
        for _ in range(_MAX_NAME_COLLISION_RETRIES):
            candidate = (storage_dir / f"{uuid.uuid4()}{extension}").resolve()
            if candidate.parent != storage_dir:  # defence in depth against path escape
                raise InvalidUploadError("Invalid storage path.", "STAGING_FAILED")
            try:
                async with aiofiles.open(candidate, "xb"):
                    pass
            except FileExistsError:
                continue
            return candidate
        raise InvalidUploadError("Could not allocate a storage file.", "STAGING_FAILED")

    @staticmethod
    async def _discard(path: Path) -> None:
        try:
            await aiofiles.os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            # Never mask the original error; log the type only (paths are internal).
            logger.warning("failed to remove partial upload", extra={"error_type": "OSError"})
