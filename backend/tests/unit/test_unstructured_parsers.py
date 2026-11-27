"""Tests for the Markdown and PDF text extractors (TASK-203)."""

import io
import threading
from collections.abc import Sequence

import pytest
from pypdf import PdfWriter

from app.core.exceptions import DocumentParsingError
from app.ingestion import pdf_parser
from app.ingestion.markdown_parser import parse_markdown
from app.ingestion.pdf_parser import extract_pdf_text, extract_pdf_text_async

# ------------------------------------------------------------------------------ helpers


def make_pdf(pages: Sequence[str | None]) -> bytes:
    """Build a minimal valid PDF; ``None`` makes a page with no /Contents entry."""
    objects: list[bytes] = []
    n = len(pages)
    # Every page reserves two object ids (page, contents) even if the contents are absent.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        page_id = 4 + 2 * i
        contents = f" /Contents {page_id + 1} 0 R" if text is not None else ""
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300]{contents} "
                "/Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 20 150 Td ({text or ''}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


# ------------------------------------------------------------------------------ markdown


def test_headings_levels_and_text() -> None:
    parsed = parse_markdown("# Title\nintro\n\n## Sub\nbody text\n")
    assert [(s.heading, s.level, s.text) for s in parsed.sections] == [
        ("Title", 1, "intro"),
        ("Sub", 2, "body text"),
    ]


def test_heading_path_is_parent_to_child() -> None:
    md = "# A\n## B\n### C\n## D\n### E\n# F\n"
    paths = [s.heading_path for s in parse_markdown(md).sections]
    assert paths == [["A"], ["A", "B"], ["A", "B", "C"], ["A", "D"], ["A", "D", "E"], ["F"]]


def test_skipped_heading_levels() -> None:
    sections = parse_markdown("# A\n#### Deep\n## Mid\n").sections
    assert [s.heading_path for s in sections] == [["A"], ["A", "Deep"], ["A", "Mid"]]


def test_deeper_heading_first_then_shallower() -> None:
    sections = parse_markdown("### Deep\n# Top\n").sections
    assert [s.heading_path for s in sections] == [["Deep"], ["Top"]]


def test_document_without_headings() -> None:
    sections = parse_markdown("just some\ntext\n").sections
    assert len(sections) == 1
    assert (sections[0].heading, sections[0].level, sections[0].heading_path) == (None, 0, [])
    assert sections[0].text == "just some\ntext"


def test_preamble_before_first_heading() -> None:
    sections = parse_markdown("preamble\n# H\nbody").sections
    assert [(s.heading, s.text) for s in sections] == [(None, "preamble"), ("H", "body")]


def test_empty_document_has_no_sections() -> None:
    assert parse_markdown("").sections == []
    assert parse_markdown("\n\n  \n").sections == []


def test_heading_without_body_is_kept() -> None:
    sections = parse_markdown("# A\n## B\ntext").sections
    assert [(s.heading, s.text) for s in sections] == [("A", ""), ("B", "text")]


def test_code_block_language_and_content_preserved() -> None:
    md = "# API\nUse it:\n```python\ndef f():\n    return 1\n\nprint(f())\n```\nafter\n"
    (section,) = parse_markdown(md).sections
    assert section.text == "Use it:\nafter"
    assert [(b.language, b.content) for b in section.code_blocks] == [
        ("python", "def f():\n    return 1\n\nprint(f())")
    ]


def test_multiple_code_blocks_and_no_language() -> None:
    md = "# H\n```\nplain\n```\n~~~bash {x}\nls\n~~~\n"
    (section,) = parse_markdown(md).sections
    assert [(b.language, b.content) for b in section.code_blocks] == [
        (None, "plain"),
        ("bash", "ls"),
    ]


def test_code_blocks_belong_to_their_own_heading() -> None:
    md = "# A\n```js\na\n```\n## B\n```go\nb\n```\n"
    a, b = parse_markdown(md).sections
    assert [c.language for c in a.code_blocks] == ["js"]
    assert [c.language for c in b.code_blocks] == ["go"]


def test_headings_inside_code_are_not_headings() -> None:
    md = "# Real\n```bash\n# comment\n## also comment\n```\n"
    (section,) = parse_markdown(md).sections
    assert section.heading == "Real"
    assert section.code_blocks[0].content == "# comment\n## also comment"


def test_longer_fence_can_contain_shorter_fence() -> None:
    md = "````md\n```py\nx\n```\n````\n"
    (section,) = parse_markdown(md).sections
    assert section.code_blocks[0].content == "```py\nx\n```"


def test_unclosed_fence_runs_to_end() -> None:
    (section,) = parse_markdown("# H\n```py\nx = 1\n# not heading\n").sections
    assert section.code_blocks[0].content == "x = 1\n# not heading\n"


def test_not_headings() -> None:
    md = "#hashtag\n####### seven\n    # indented code-ish\n"
    (section,) = parse_markdown(md).sections
    assert section.heading is None


def test_heading_formatting_variants() -> None:
    sections = parse_markdown("## Title ##\n#\n###   Spaced   \n").sections
    assert [s.heading for s in sections] == ["Title", "", "Spaced"]


def test_bytes_bom_and_crlf() -> None:
    parsed = parse_markdown(b"\xef\xbb\xbf# T\r\nline\r\n```py\r\nx\r\n```\r\n")
    (section,) = parsed.sections
    assert (section.heading, section.text, section.code_blocks[0].content) == ("T", "line", "x")


def test_invalid_utf8_bytes_rejected() -> None:
    with pytest.raises(DocumentParsingError) as exc:
        parse_markdown(b"# \xff\xfe")
    assert exc.value.code == "INVALID_MARKDOWN"


# ------------------------------------------------------------------------------ pdf


def test_pdf_pages_text_and_numbers() -> None:
    parsed = extract_pdf_text(make_pdf(["Hello page one", "Second page"]))
    assert [(p.page_number, p.text) for p in parsed.pages] == [
        (1, "Hello page one"),
        (2, "Second page"),
    ]


def test_pdf_empty_pages_keep_numbering() -> None:
    parsed = extract_pdf_text(make_pdf(["first", None, "third"]))
    assert [(p.page_number, p.text) for p in parsed.pages] == [(1, "first"), (2, ""), (3, "third")]


def test_pdf_all_blank_pages() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    parsed = extract_pdf_text(buffer.getvalue())
    assert [(p.page_number, p.text) for p in parsed.pages] == [(1, "")]


@pytest.mark.parametrize("content", [b"", b"not a pdf at all", b"%PDF-1.4\ngarbage"])
def test_invalid_pdf_rejected(content: bytes) -> None:
    with pytest.raises(DocumentParsingError) as exc:
        extract_pdf_text(content)
    assert exc.value.code == "INVALID_PDF"


def test_truncated_pdf_rejected() -> None:
    data = make_pdf(["hello"])
    with pytest.raises(DocumentParsingError):
        extract_pdf_text(data[: len(data) // 3])


def test_encrypted_pdf_rejected() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(DocumentParsingError) as exc:
        extract_pdf_text(buffer.getvalue())
    assert exc.value.code == "PDF_ENCRYPTED"


def test_page_extraction_failure_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    from pypdf._page import PageObject

    def boom(self: PageObject, *args: object, **kwargs: object) -> str:
        raise ValueError("bad content stream")

    monkeypatch.setattr(PageObject, "extract_text", boom)
    with pytest.raises(DocumentParsingError) as exc:
        extract_pdf_text(make_pdf(["x"]))
    assert exc.value.code == "PDF_PAGE_UNREADABLE"
    assert "page 1" in exc.value.message


async def test_async_wrapper_runs_in_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    main_thread = threading.get_ident()
    seen: list[int] = []
    real = pdf_parser.extract_pdf_text

    def spy(content: bytes) -> object:
        seen.append(threading.get_ident())
        return real(content)

    monkeypatch.setattr(pdf_parser, "extract_pdf_text", spy)
    parsed = await extract_pdf_text_async(make_pdf(["async text"]))
    assert parsed.pages[0].text == "async text"
    assert seen
    assert seen[0] != main_thread


async def test_async_wrapper_propagates_errors() -> None:
    with pytest.raises(DocumentParsingError):
        await extract_pdf_text_async(b"junk")


def test_inline_triple_backticks_are_not_a_fence() -> None:
    (section,) = parse_markdown("```not a fence```\ntext\n").sections
    assert section.code_blocks == []
    assert "text" in section.text
