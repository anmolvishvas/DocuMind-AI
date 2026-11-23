"""Tests for upload validation, secure staging and the upload endpoint (TASK-201)."""

import io
import re
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles.os
import httpx
import pytest
from pydantic import SecretStr

from app.api.v1.documents import get_upload_validator
from app.core.config import Settings
from app.core.exceptions import FileTooLargeError, InvalidUploadError
from app.ingestion.validator import MAX_UPLOAD_BYTES, StagedFile, UploadValidator
from app.main import create_app

UUID_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[a-z]+")
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeUpload:
    """Async reader over bytes, mimicking ``UploadFile.read``."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    async def read(self, size: int = -1, /) -> bytes:
        return self._buffer.read(size)


class FailingUpload:
    """Yields one chunk, then raises, to simulate a failure mid-stream."""

    def __init__(self) -> None:
        self._sent = False

    async def read(self, size: int = -1, /) -> bytes:
        if self._sent:
            raise OSError("disk went away")
        self._sent = True
        return b"hello"


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    return tmp_path.resolve() / "staging"


@pytest.fixture
def validator(storage_dir: Path) -> UploadValidator:
    return UploadValidator(storage_dir)


async def stage(
    validator: UploadValidator,
    data: bytes,
    filename: str | None,
    content_type: str | None = None,
) -> StagedFile:
    return await validator.validate_and_stage(FakeUpload(data), filename, content_type)


def staged_files(storage_dir: Path) -> list[Path]:
    return list(storage_dir.iterdir()) if storage_dir.exists() else []


# ------------------------------------------------------------------ valid files
@pytest.mark.parametrize(
    ("filename", "data", "content_type", "mime"),
    [
        ("spec.json", b'{"openapi": "3.0.0"}', "application/json", "application/json"),
        ("spec.yaml", b"openapi: 3.0.0\n", "application/x-yaml", "application/yaml"),
        ("spec.yml", b"openapi: 3.0.0\n", "text/yaml", "application/yaml"),
        ("guide.pdf", PDF_BYTES, "application/pdf", "application/pdf"),
        ("README.md", b"# Title\n\nbody\n", "text/markdown", "text/markdown"),
        ("notes.txt", b"plain text\r\nline two\ttabbed", "text/plain", "text/plain"),
        ("spec.json", b'[{"a": 1}]', "application/octet-stream", "application/json"),
        ("spec.json", b'\xef\xbb\xbf {"a": 1}', None, "application/json"),
        ("notes.txt", b"x", "text/plain; charset=utf-8", "text/plain"),
    ],
)
async def test_valid_files_are_staged(
    validator: UploadValidator,
    storage_dir: Path,
    filename: str,
    data: bytes,
    content_type: str | None,
    mime: str,
) -> None:
    staged = await stage(validator, data, filename, content_type)

    assert staged.mime_type == mime
    assert staged.size_bytes == len(data)
    assert staged.original_filename == filename
    assert staged.staged_path.read_bytes() == data
    assert staged.staged_path.parent == storage_dir


async def test_unicode_text_is_accepted(validator: UploadValidator) -> None:
    data = ("# Привет мир — 日本語 🚀 café\n" * 5000).encode()  # spans multiple chunks
    staged = await stage(validator, data, "unicode.md", "text/markdown")
    assert staged.staged_path.read_bytes() == data


async def test_multibyte_character_split_across_chunks_is_accepted(
    validator: UploadValidator,
) -> None:
    # 64 KiB - 1 ASCII bytes, then a 3-byte char straddling the chunk boundary.
    data = b"a" * (64 * 1024 - 1) + "€".encode() + b"tail"
    staged = await stage(validator, data, "boundary.txt", "text/plain")
    assert staged.size_bytes == len(data)


@pytest.mark.parametrize("filename", ["SPEC.JSON", "Spec.Yaml", "doc.PDF", "a.Md", "b.TXT"])
async def test_extensions_are_case_insensitive(validator: UploadValidator, filename: str) -> None:
    data = PDF_BYTES if filename.lower().endswith(".pdf") else b'{"a": 1}'
    staged = await stage(validator, data, filename)
    assert staged.extension == Path(filename).suffix.lower()
    assert staged.stored_filename.endswith(staged.extension)


async def test_exactly_max_size_is_accepted(validator: UploadValidator) -> None:
    staged = await stage(validator, b"a" * MAX_UPLOAD_BYTES, "big.txt", "text/plain")
    assert staged.size_bytes == MAX_UPLOAD_BYTES


# ---------------------------------------------------------------- invalid files
@pytest.mark.parametrize("filename", ["malware.exe", "a.json.exe", "archive.zip", "x.jsonl"])
async def test_unsupported_extension_rejected(validator: UploadValidator, filename: str) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b"{}", filename)
    assert exc.value.status_code == 400
    assert exc.value.code == "UNSUPPORTED_EXTENSION"


@pytest.mark.parametrize("filename", ["README", "", None, ".json"])
async def test_missing_extension_rejected(validator: UploadValidator, filename: str | None) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b"{}", filename)
    assert exc.value.code == "MISSING_EXTENSION"


async def test_oversized_file_rejected_with_413(
    validator: UploadValidator, storage_dir: Path
) -> None:
    with pytest.raises(FileTooLargeError) as exc:
        await stage(validator, b"a" * (MAX_UPLOAD_BYTES + 1), "big.txt", "text/plain")
    assert exc.value.status_code == 413
    assert staged_files(storage_dir) == []


async def test_pdf_with_wrong_magic_bytes_rejected(
    validator: UploadValidator, storage_dir: Path
) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b"<html>not a pdf</html>", "fake.pdf", "application/pdf")
    assert exc.value.code == "INVALID_CONTENT"
    assert staged_files(storage_dir) == []


@pytest.mark.parametrize("filename", ["a.txt", "a.md", "a.yaml", "a.json"])
async def test_binary_disguised_as_text_rejected(
    validator: UploadValidator, storage_dir: Path, filename: str
) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b'{"a": 1}\x00\x01\x02binary', filename)
    assert exc.value.code == "INVALID_CONTENT"
    assert staged_files(storage_dir) == []


async def test_octet_stream_mime_cannot_bypass_content_check(validator: UploadValidator) -> None:
    with pytest.raises(InvalidUploadError):
        await stage(validator, b"MZ\x90\x00\x03\x00\x00\x00", "a.txt", "application/octet-stream")


async def test_invalid_utf8_rejected(validator: UploadValidator) -> None:
    with pytest.raises(InvalidUploadError):
        await stage(validator, b"caf\xe9 latin-1", "a.txt", "text/plain")


async def test_truncated_utf8_at_end_rejected(validator: UploadValidator) -> None:
    with pytest.raises(InvalidUploadError):
        await stage(validator, b"abc" + "€".encode()[:2], "a.txt", "text/plain")


async def test_non_json_content_rejected(validator: UploadValidator) -> None:
    with pytest.raises(InvalidUploadError):
        await stage(validator, b"just words", "a.json", "application/json")


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("a.json", "text/html"),
        ("a.pdf", "text/plain"),
        ("a.txt", "application/pdf"),
        ("a.yaml", "application/json"),
    ],
)
async def test_mime_inconsistent_with_extension_rejected(
    validator: UploadValidator, filename: str, content_type: str
) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b"{}", filename, content_type)
    assert exc.value.code == "MIME_MISMATCH"


async def test_empty_file_rejected(validator: UploadValidator, storage_dir: Path) -> None:
    with pytest.raises(InvalidUploadError) as exc:
        await stage(validator, b"", "empty.txt", "text/plain")
    assert exc.value.code == "EMPTY_FILE"
    assert staged_files(storage_dir) == []


# ---------------------------------------------------------------------- staging
async def test_staged_name_is_uuid_with_extension_and_ignores_original(
    validator: UploadValidator,
) -> None:
    staged = await stage(validator, b"hi", "../../etc/passwd.TXT", "text/plain")

    assert UUID_NAME.fullmatch(staged.stored_filename)
    assert staged.stored_filename.endswith(".txt")
    assert staged.staged_path.name == staged.stored_filename
    assert "passwd" not in staged.staged_path.name
    assert staged.original_filename == "passwd.TXT"  # directories stripped


async def test_windows_style_path_traversal_is_stripped(
    validator: UploadValidator, storage_dir: Path
) -> None:
    staged = await stage(validator, b"hi", "..\\..\\evil.txt", "text/plain")
    assert staged.original_filename == "evil.txt"
    assert staged.staged_path.parent == storage_dir


async def test_each_upload_gets_distinct_file(validator: UploadValidator) -> None:
    first = await stage(validator, b"one", "same.txt")
    second = await stage(validator, b"two", "same.txt")
    assert first.staged_path != second.staged_path
    assert first.staged_path.read_bytes() == b"one"
    assert second.staged_path.read_bytes() == b"two"


async def test_existing_file_is_never_overwritten(
    validator: UploadValidator, storage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = storage_dir / "11111111-1111-4111-8111-111111111111.txt"
    await aiofiles.os.makedirs(storage_dir)
    existing.write_bytes(b"precious")
    ids = iter(
        [
            "11111111-1111-4111-8111-111111111111",  # collides with the existing file
            "22222222-2222-4222-8222-222222222222",
        ]
    )
    monkeypatch.setattr("app.ingestion.validator.uuid.uuid4", lambda: next(ids))

    staged = await stage(validator, b"new", "a.txt")

    assert existing.read_bytes() == b"precious"
    assert staged.stored_filename == "22222222-2222-4222-8222-222222222222.txt"


async def test_partial_file_removed_when_staging_fails(
    validator: UploadValidator, storage_dir: Path
) -> None:
    with pytest.raises(OSError, match="disk went away"):
        await validator.validate_and_stage(FailingUpload(), "a.txt", "text/plain")
    assert staged_files(storage_dir) == []


async def test_partial_file_removed_when_oversized_midstream(storage_dir: Path) -> None:
    small = UploadValidator(storage_dir, max_size_bytes=100)
    with pytest.raises(FileTooLargeError):
        await stage(small, b"a" * 101, "a.txt", "text/plain")
    assert staged_files(storage_dir) == []


# ------------------------------------------------------------------ API layer
@pytest.fixture
async def client(storage_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=SecretStr("postgresql+asyncpg://u:p@localhost/db"),
        openai_api_key=SecretStr("test-key"),
        _env_file=None,
    )
    app = create_app(settings)
    app.dependency_overrides[get_upload_validator] = lambda: UploadValidator(storage_dir)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def _upload(name: str, data: bytes, content_type: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, content_type)}


async def test_api_successful_upload(client: httpx.AsyncClient, storage_dir: Path) -> None:
    response = await client.post(
        "/api/v1/documents/upload", files=_upload("spec.json", b'{"a": 1}', "application/json")
    )

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "spec.json"
    assert UUID_NAME.fullmatch(body["stored_filename"])
    assert body["extension"] == ".json"
    assert body["mime_type"] == "application/json"
    assert body["size_bytes"] == 8
    assert "staged_path" not in body
    assert (storage_dir / body["stored_filename"]).read_bytes() == b'{"a": 1}'


async def test_api_invalid_extension_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/documents/upload", files=_upload("run.exe", b"abc", "text/plain")
    )
    assert response.status_code == 400
    assert response.json()["code"] == "UNSUPPORTED_EXTENSION"


async def test_api_invalid_content_returns_400_without_leaking_paths(
    client: httpx.AsyncClient, storage_dir: Path
) -> None:
    response = await client.post(
        "/api/v1/documents/upload", files=_upload("a.pdf", b"not a pdf", "application/pdf")
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_CONTENT"
    assert str(storage_dir) not in response.text
    assert staged_files(storage_dir) == []


async def test_api_oversized_upload_returns_413(
    client: httpx.AsyncClient, storage_dir: Path
) -> None:
    response = await client.post(
        "/api/v1/documents/upload",
        files=_upload("big.txt", b"a" * (MAX_UPLOAD_BYTES + 1), "text/plain"),
    )
    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"
    assert staged_files(storage_dir) == []
