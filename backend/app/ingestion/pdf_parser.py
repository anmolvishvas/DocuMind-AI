"""PDF text extraction with ``pypdf`` (TASK-203).

``extract_pdf_text`` is synchronous and CPU-bound; ``extract_pdf_text_async`` runs it in a
worker thread via ``starlette.concurrency.run_in_threadpool`` so it never blocks the event
loop (docs/RULES.md section 3). Async callers must use the wrapper.

Scanned (image-only) PDFs have no text layer: their pages come back with empty text. OCR is
out of scope.
"""

import io
import logging

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import DocumentParsingError
from app.schemas.extraction import ParsedPdf, PdfPage

logger = logging.getLogger(__name__)

# pypdf mostly raises PyPdfError subclasses, but malformed files can also surface these.
_PYPDF_FAILURES = (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError)


def _open_reader(content: bytes) -> PdfReader:
    """Open ``content`` as a PDF, translating pypdf failures into domain errors."""
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:  # must be checked before touching ``pages``
            raise DocumentParsingError("Encrypted PDFs are not supported.", "PDF_ENCRYPTED")
        # Forces the page tree to load, so a broken document fails here, not mid-extraction.
        len(reader.pages)
    except _PYPDF_FAILURES:
        logger.warning("pdf could not be opened")
        raise DocumentParsingError("File is not a readable PDF.", "INVALID_PDF") from None
    return reader


def _extract_page(reader: PdfReader, index: int) -> PdfPage:
    """Extract one page (``index`` is 0-based; ``page_number`` is 1-based)."""
    number = index + 1
    try:
        text = reader.pages[index].extract_text() or ""
    except _PYPDF_FAILURES:
        logger.warning("pdf page extraction failed", extra={"page_number": number})
        raise DocumentParsingError(
            f"Could not extract text from PDF page {number}.", "PDF_PAGE_UNREADABLE"
        ) from None
    return PdfPage(page_number=number, text=text.strip())


def extract_pdf_text(content: bytes) -> ParsedPdf:
    """Extract text page by page (synchronous, blocking).

    Args:
        content: Raw bytes of a PDF file.

    Returns:
        ``ParsedPdf`` with one ``PdfPage`` per page, numbered from 1. Pages without
        extractable text are kept with ``text == ""``.

    Raises:
        DocumentParsingError: corrupt/invalid, encrypted, or unreadable-page PDFs.
    """
    reader = _open_reader(content)
    return ParsedPdf(pages=[_extract_page(reader, i) for i in range(len(reader.pages))])


async def extract_pdf_text_async(content: bytes) -> ParsedPdf:
    """Async wrapper: run ``extract_pdf_text`` in a worker thread."""
    return await run_in_threadpool(extract_pdf_text, content)
