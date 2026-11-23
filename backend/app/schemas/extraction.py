"""Output models for Markdown and PDF text extraction (TASK-203).

These are the parsers' output contract, consumed later by the chunker (TASK-301).
"""

from pydantic import BaseModel, ConfigDict, Field


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True, frozen=True)


class CodeBlock(_ExtractionModel):
    """A fenced code block, preserved verbatim."""

    language: str | None = Field(
        default=None, description="Language identifier from the info string, if any."
    )
    content: str = Field(description="Code between the fences, exactly as written.")


class MarkdownSection(_ExtractionModel):
    """The content that sits directly under one heading (up to the next heading)."""

    heading: str | None = Field(
        description="Heading text, or None for content that appears before any heading."
    )
    level: int = Field(ge=0, le=6, description="Heading level 1-6, or 0 when there is no heading.")
    heading_path: list[str] = Field(
        description="Headings from the top-level ancestor down to this one (parent -> child)."
    )
    text: str = Field(
        description="Prose under the heading, excluding code blocks and sub-sections."
    )
    code_blocks: list[CodeBlock] = Field(
        default_factory=list, description="Fenced code blocks directly under the heading."
    )


class ParsedMarkdown(_ExtractionModel):
    """A Markdown document split into heading-scoped sections, in document order."""

    sections: list[MarkdownSection] = Field(description="Sections in document order.")


class PdfPage(_ExtractionModel):
    """Text extracted from one PDF page."""

    page_number: int = Field(ge=1, description="1-based page number.")
    text: str = Field(description="Extracted text; empty when the page has no extractable text.")


class ParsedPdf(_ExtractionModel):
    """Text of a PDF, one entry per page (empty pages are kept so numbering stays correct)."""

    pages: list[PdfPage] = Field(description="All pages in order.")
