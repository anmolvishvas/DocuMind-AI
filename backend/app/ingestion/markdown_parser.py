"""Markdown heading-hierarchy and fenced-code-block extractor (TASK-203).

The parser is synchronous and cheap (a single pass over the lines). It is a purpose-built
line scanner rather than a full CommonMark implementation, and supports:

* ATX headings (``# Title`` .. ``###### Title``, optional closing ``#``s).
* Fenced code blocks with backticks or tildes, with an optional language identifier.
  Headings inside a fence are code, not headings. An unclosed fence runs to the end.

Not supported: setext headings (``Title`` underlined with ``===``), indented code blocks.
"""

import re
from typing import Final

from app.core.exceptions import DocumentParsingError
from app.schemas.extraction import CodeBlock, MarkdownSection, ParsedMarkdown

# Up to 3 leading spaces, 1-6 '#', then a space/tab or end of line, then the title.
_HEADING: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES: Final[re.Pattern[str]] = re.compile(r"(?:^|[ \t]+)#+$")
# Opening fence: up to 3 spaces of indent, 3+ backticks or tildes, then an info string.
_FENCE_OPEN: Final[re.Pattern[str]] = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")


class _SectionBuilder:
    """Accumulates the lines and code blocks of the section currently being read."""

    def __init__(self, heading: str | None, level: int, path: list[str]) -> None:
        self.heading = heading
        self.level = level
        self.path = path
        self.lines: list[str] = []
        self.code_blocks: list[CodeBlock] = []

    def text(self) -> str:
        return "\n".join(self.lines).strip()

    def build(self) -> MarkdownSection:
        return MarkdownSection(
            heading=self.heading,
            level=self.level,
            heading_path=list(self.path),
            text=self.text(),
            code_blocks=self.code_blocks,
        )


def _decode(content: str | bytes) -> str:
    """Return normalised text (UTF-8 BOM removed, ``\\n`` newlines)."""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DocumentParsingError(
                "Markdown must be valid UTF-8.", "INVALID_MARKDOWN"
            ) from None
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _parse_heading(line: str) -> tuple[int, str] | None:
    """Return ``(level, title)`` if ``line`` is an ATX heading, else ``None``."""
    match = _HEADING.match(line)
    if match is None:
        return None
    title = _CLOSING_HASHES.sub("", match.group(2) or "").strip()
    return len(match.group(1)), title


def _next_stack(stack: list[tuple[int, str]], level: int, title: str) -> list[tuple[int, str]]:
    """Heading stack after entering ``title``: drop siblings/descendants, then push it."""
    kept = [entry for entry in stack if entry[0] < level]
    kept.append((level, title))
    return kept


def _read_code_block(lines: list[str], start: int) -> tuple[CodeBlock, int] | None:
    """If ``lines[start]`` opens a fence, return the block and the index after its end."""
    opening = _FENCE_OPEN.match(lines[start])
    if opening is None:
        return None
    indent, fence, info = len(opening.group(1)), opening.group(2), opening.group(3)
    if fence[0] == "`" and "`" in info:  # a line like "```x```" is inline code, not a fence
        return None
    language = info.split()[0] if info else None
    closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    indent_strip = re.compile(rf"^ {{0,{indent}}}")
    body: list[str] = []
    index = start + 1
    while index < len(lines) and not closing.match(lines[index]):
        body.append(indent_strip.sub("", lines[index]))
        index += 1
    # ``index`` is the closing fence (skip it) or len(lines) for an unclosed fence.
    return CodeBlock(language=language, content="\n".join(body)), index + 1


def parse_markdown(content: str | bytes) -> ParsedMarkdown:
    """Split Markdown into heading-scoped sections.

    Args:
        content: Markdown source as ``str`` or UTF-8 ``bytes``.

    Returns:
        ``ParsedMarkdown`` whose sections follow document order. Each section carries its
        heading, level, full ``heading_path`` (parent -> child), prose, and code blocks.
        Content before the first heading becomes a section with ``heading=None``; it is
        omitted when empty. Headings with no body are kept (they still give context).

    Raises:
        DocumentParsingError: ``content`` is not valid UTF-8.
    """
    lines = _decode(content).split("\n")
    stack: list[tuple[int, str]] = []
    current = _SectionBuilder(None, 0, [])
    sections: list[MarkdownSection] = []

    def close_section() -> None:
        # The heading-less preamble is dropped when empty; real headings are always kept.
        if current.heading is not None or current.text() or current.code_blocks:
            sections.append(current.build())

    index = 0
    while index < len(lines):
        block = _read_code_block(lines, index)
        if block is not None:
            current.code_blocks.append(block[0])
            index = block[1]
            continue
        heading = _parse_heading(lines[index])
        if heading is not None:
            close_section()
            stack = _next_stack(stack, *heading)
            current = _SectionBuilder(heading[1], heading[0], [title for _, title in stack])
        else:
            current.lines.append(lines[index])
        index += 1
    close_section()
    return ParsedMarkdown(sections=sections)
