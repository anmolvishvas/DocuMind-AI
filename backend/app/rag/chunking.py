"""Endpoint-aware and narrative chunking (TASK-301).

Two independent strategies produce :class:`~app.schemas.chunk.Chunk` objects of at most
``MAX_CHUNK_TOKENS`` (512) tokens, measured with the tokenizer of the embedding model
(``tiktoken``, ``cl100k_base``):

* **Endpoint chunks** (:func:`chunk_endpoint`, :func:`chunk_endpoints`,
  :func:`chunk_specification`): one self-contained Markdown chunk per extracted operation
  (route signature, parameters, request body, responses and their schemas). The parser
  (TASK-202) already inlines every ``$ref``; only recursive ones are left as
  ``{"$ref": pointer}`` and are expanded here from ``recursive_schemas``, once per path, so
  cyclic schemas terminate. Schemas are rendered as a compact field list rather than raw JSON.
  When a chunk is too large the renderer retries with less detail, and as a last resort cuts
  the text; the chunk is then flagged ``truncated`` and the cut is marked in the text.
  Naive token chunking is never applied to OpenAPI content (docs/RULES.md section 5.1).
* **Narrative chunks** (:func:`chunk_markdown`, :func:`chunk_text`, :func:`split_text`):
  recursive splitting on paragraph, line, sentence and word boundaries, then on token-sized
  windows as a last resort. Fenced code blocks are kept whole (a block larger than the budget is
  split by lines and every piece is re-fenced). Consecutive chunks of one section overlap by
  up to ``CHUNK_OVERLAP_TOKENS`` (64) tokens, and every chunk of a Markdown section starts with
  that section's heading.

The module is synchronous and CPU-bound: async callers must dispatch it with
``starlette.concurrency.run_in_threadpool`` (docs/RULES.md section 3). The first use of the
default tokenizer downloads its vocabulary (cached afterwards by ``tiktoken``).
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Protocol, cast

import tiktoken

from app.core.exceptions import ChunkingError
from app.schemas.chunk import Chunk, ChunkMetadata
from app.schemas.endpoint import (
    ContentSchema,
    Endpoint,
    Parameter,
    ParsedSpecification,
    RequestBody,
    Response,
)
from app.schemas.extraction import MarkdownSection, ParsedMarkdown

logger = logging.getLogger(__name__)

MAX_CHUNK_TOKENS: Final = 512
CHUNK_OVERLAP_TOKENS: Final = 64
MIN_CHUNK_TOKENS: Final = 16
EMBEDDING_MODEL: Final = "text-embedding-3-small"

_MAX_VARIANTS: Final = 8  # oneOf / anyOf options rendered
_MAX_ALL_OF: Final = 16  # allOf members merged
_MAX_REF_HOPS: Final = 16  # chained $ref -> $ref
_MAX_HOPS: Final = 32  # same-depth recursion (array items, variants, allOf)
_TRUNCATION_MARKER: Final = "\n[truncated: details exceeded the token limit]"

# =============================================================================== tokenizer


class TokenCounter(Protocol):
    """Anything that can measure text in tokens (lets the chunker stay tokenizer-agnostic)."""

    def count(self, text: str) -> int:
        """Return the number of tokens in ``text``."""
        ...


class TiktokenCounter:
    """:class:`TokenCounter` backed by the ``tiktoken`` encoding of an OpenAI model."""

    def __init__(self, model: str = EMBEDDING_MODEL) -> None:
        try:
            self._encoding = tiktoken.encoding_for_model(model)
        except Exception as exc:  # tiktoken raises KeyError, network and cache errors alike
            raise ChunkingError(
                f"Tokenizer for model '{model}' is unavailable.", "TOKENIZER_UNAVAILABLE"
            ) from exc

    def count(self, text: str) -> int:
        # Special-token look-alikes ('<|endoftext|>') in documentation are ordinary text.
        return len(self._encoding.encode(_clean(text), disallowed_special=()))


@lru_cache(maxsize=1)
def default_token_counter() -> TokenCounter:
    """The shared counter for the embedding model (the encoding is immutable)."""
    return TiktokenCounter()


def _clean(text: str) -> str:
    """Replace characters that cannot be encoded as UTF-8 (lone surrogates)."""
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _validate_sizes(max_tokens: int, overlap_tokens: int) -> None:
    if max_tokens < MIN_CHUNK_TOKENS:
        raise ChunkingError(
            f"max_tokens must be at least {MIN_CHUNK_TOKENS}, got {max_tokens}.",
            "INVALID_CHUNK_SIZE",
        )
    if overlap_tokens < 0 or overlap_tokens >= max_tokens // 2:
        raise ChunkingError(
            f"overlap_tokens must be in [0, max_tokens/2), got {overlap_tokens}.",
            "INVALID_CHUNK_OVERLAP",
        )


# =============================================================================== text helpers


def _one_line(value: Any, limit: int | None = None) -> str:
    """Collapse whitespace; ``limit`` None = unlimited, 0 = omit, otherwise clip with '…'."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if limit is None or len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return text[: limit - 1].rstrip() + "…"


def _block_text(value: str | None, limit: int | None) -> str:
    """Endpoint-level prose: paragraphs are kept unless the text has to be clipped."""
    if not value or not value.strip():
        return ""
    if limit is None:
        return re.sub(r"\n{3,}", "\n\n", value.strip())
    return _one_line(value, limit)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(value)


def _canonical(schema: Any) -> str:
    """Stable key for 'is this the same schema' (cyclic input falls back to identity)."""
    try:
        return json.dumps(schema, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return f"id:{id(schema)}"


# =============================================================================== schema renderer

_RANGE_KEYWORDS: Final = (
    ("minimum", "min"),
    ("maximum", "max"),
    ("exclusiveMinimum", "exclusive min"),
    ("exclusiveMaximum", "exclusive max"),
    ("multipleOf", "multiple of"),
    ("minLength", "min length"),
    ("maxLength", "max length"),
    ("pattern", "pattern"),
    ("minItems", "min items"),
    ("maxItems", "max items"),
    ("uniqueItems", "unique items"),
    ("minProperties", "min properties"),
    ("maxProperties", "max properties"),
)


@dataclass(frozen=True)
class _Detail:
    """How much a rendering keeps; the ladder below trades detail for size."""

    max_depth: int  # nesting levels of fields rendered
    field_desc_limit: int | None  # chars of a field description (None = all, 0 = none)
    text_limit: int | None  # chars of endpoint / body / response descriptions
    constraints: bool  # min/max/pattern/default/... annotations
    response_schemas: bool  # False: responses list status and description only


_LADDER: Final = (
    _Detail(8, None, None, True, True),
    _Detail(5, 160, 600, True, True),
    _Detail(3, 80, 300, True, True),
    _Detail(2, 40, 200, False, True),
    _Detail(1, 0, 120, False, True),
    _Detail(1, 0, 120, False, False),
)

_Resolved = tuple[dict[str, Any] | None, tuple[str, ...], str | None, str]


class _SchemaRenderer:
    """Renders JSON Schemas as indented Markdown field lists.

    Malformed input (wrong-typed keywords, dangling references) degrades to 'any' instead of
    raising. Every recursion is bounded: by ``max_depth`` for nesting, by the ``active`` set of
    references being expanded for ``$ref`` cycles, and by hop counters for same-depth chains.
    """

    def __init__(self, definitions: Mapping[str, Any], detail: _Detail) -> None:
        self._definitions = definitions
        self._detail = detail
        self.omitted = False  # True once anything was left out because of ``detail``
        self._open: set[int] = set()  # schemas whose children are being rendered

    # ---------------------------------------------------------------- resolution

    def _deref(self, node: Any, active: tuple[str, ...]) -> _Resolved:
        """Follow ``$ref`` chains. Returns ``(schema, active, ref_name, status)``.

        ``status`` is 'ok', 'cycle' (the target is already being expanded), 'unresolved'
        (no such recursive schema) or 'invalid' (not a schema). ``schema`` is None unless 'ok'.
        """
        name: str | None = None
        for _ in range(_MAX_REF_HOPS):
            if node is None or node is True:
                return {}, active, name, "ok"
            if node is False:
                return {"not": {}}, active, name, "ok"
            if not isinstance(node, Mapping):
                return None, active, name, "invalid"
            ref = node.get("$ref")
            if not isinstance(ref, str):
                return cast(dict[str, Any], node), active, name, "ok"
            name = ref.rsplit("/", 1)[-1] or ref
            if ref in active:
                return None, active, name, "cycle"
            target = self._definitions.get(ref)
            if not isinstance(target, Mapping):
                return None, active, name, "unresolved"
            node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            active = (*active, ref)
        return None, active, name, "unresolved"

    def _prepare(self, node: Any, active: tuple[str, ...]) -> _Resolved:
        """Dereference ``node`` and merge its ``allOf`` members into one schema."""
        schema, active, name, status = self._deref(node, active)
        if schema is not None:
            schema = self._merge_all_of(schema, active, 0)
        return schema, active, name, status

    def _merge_all_of(
        self, schema: dict[str, Any], active: tuple[str, ...], hops: int
    ) -> dict[str, Any]:
        members = schema.get("allOf")
        if not isinstance(members, list) or hops > _MAX_HOPS:
            return schema
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        properties = dict(p) if isinstance(p := merged.get("properties"), Mapping) else {}
        required = list(r) if isinstance(r := merged.get("required"), list) else []
        for member in members[:_MAX_ALL_OF]:
            resolved, member_active, _, _ = self._deref(member, active)
            if resolved is None:
                continue
            resolved = self._merge_all_of(resolved, member_active, hops + 1)
            if isinstance(p := resolved.get("properties"), Mapping):
                properties.update(p)
            if isinstance(r := resolved.get("required"), list):
                required.extend(r)
            for key, value in resolved.items():
                if key not in ("properties", "required"):
                    merged.setdefault(key, value)
        if properties:
            merged["properties"] = properties
        if required:
            merged["required"] = required
        return merged

    # ---------------------------------------------------------------- labels

    def _type_label(self, schema: Mapping[str, Any], active: tuple[str, ...], hops: int = 0) -> str:
        declared = schema.get("type")
        if isinstance(declared, list):
            base = "|".join(t for t in declared if isinstance(t, str)) or "any"
        elif isinstance(declared, str):
            base = declared
        elif "properties" in schema or isinstance(schema.get("additionalProperties"), Mapping):
            base = "object"
        elif "items" in schema:
            base = "array"
        elif "oneOf" in schema:
            base = "oneOf"
        elif "anyOf" in schema:
            base = "anyOf"
        elif "const" in schema:
            base = "const"
        elif "enum" in schema:
            base = "enum"
        elif schema.get("not") == {}:
            base = "never"
        else:
            base = "any"
        if "items" in schema and "array" in base.split("|") and hops < _MAX_HOPS // 4:
            item, item_active, name, status = self._prepare(schema["items"], active)
            if item is not None:
                item_label = self._type_label(item, item_active, hops + 1)
                if name:
                    item_label = f"{name} {item_label}"
            elif status == "cycle":
                item_label = f"{name} (recursive reference)"
            elif status == "unresolved":
                item_label = f"{name} (unresolved reference)"
            else:
                item_label = "any"
            base = base.replace("array", f"array of {item_label}", 1)
        if schema.get("nullable") is True and "null" not in base.split("|"):
            base += "|null"
        return base

    def _annotations(self, schema: Mapping[str, Any]) -> str:
        """Bracketed enum / default / range constraints, or ''."""
        notes: list[str] = []
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            limit = 20 if self._detail.constraints else 6
            shown = " | ".join(_json(v) for v in enum[:limit])
            more = f" | … (+{len(enum) - limit} more)" if len(enum) > limit else ""
            notes.append(f"enum: {shown}{more}")
            if len(enum) > limit:
                self.omitted = True
        if self._detail.constraints:
            if "const" in schema:
                notes.append(f"const: {_json(schema['const'])}")
            if "default" in schema:
                notes.append(f"default: {_json(schema['default'])}")
            notes.extend(
                f"{label}: {_json(schema[key])}" for key, label in _RANGE_KEYWORDS if key in schema
            )
            notes.extend(
                flag for flag in ("readOnly", "writeOnly", "deprecated") if schema.get(flag)
            )
            if schema.get("additionalProperties") is False:
                notes.append("no additional properties")
        return f" [{'; '.join(notes)}]" if notes else ""

    # ---------------------------------------------------------------- fields

    def block(self, schema: Any) -> list[str]:
        """``Schema: <summary>`` followed by the field list of a body or response schema."""
        resolved, active, ref, status = self._prepare(schema, ())
        if resolved is None:
            return [f"Schema: {self._unusable_label(ref, status)}"]
        head = f"Schema: {self._head_label(resolved, active, ref)}"
        desc = _one_line(resolved.get("description") or resolved.get("title"), self._text_limit())
        if desc:
            head += f" - {desc}"
        head += self._annotations(resolved)
        return [head, *self._children(resolved, active, 0, 1, 0)]

    def summary(self, schema: Any) -> str:
        """One-line type summary (used when field lists are dropped to save space)."""
        resolved, active, ref, status = self._prepare(schema, ())
        if resolved is None:
            return self._unusable_label(ref, status)
        if self._has_structure(resolved, active, 0):
            self.omitted = True
        return self._head_label(resolved, active, ref)

    def field(
        self,
        name: str,
        node: Any,
        required: bool,
        indent: int,
        depth: int,
        active: tuple[str, ...],
        description: str | None = None,
    ) -> list[str]:
        """One ``- name (type, required): description [constraints]`` line plus children."""
        pad = "  " * indent
        flag = "required" if required else "optional"
        resolved, active, ref, status = self._prepare(node, active)
        if resolved is None:
            label = self._unusable_label(ref, status)
            return [f"{pad}- {name} ({label}, {flag})"]
        parts = [self._head_label(resolved, active, ref)]
        if isinstance(fmt := resolved.get("format"), str):
            parts.append(fmt)
        parts.append(flag)
        desc = _one_line(
            description or resolved.get("description") or resolved.get("title"),
            self._detail.field_desc_limit,
        )
        line = f"{pad}- {name} ({', '.join(parts)})"
        if desc:
            line += f": {desc}"
        line += self._annotations(resolved)
        return [line, *self._children(resolved, active, indent + 1, depth + 1, 0)]

    def _head_label(
        self, schema: Mapping[str, Any], active: tuple[str, ...], ref: str | None
    ) -> str:
        label = self._type_label(schema, active)
        return f"{ref} {label}" if ref else label

    @staticmethod
    def _unusable_label(ref: str | None, status: str) -> str:
        if status == "cycle":
            return f"{ref} (recursive reference)"
        if status == "unresolved":
            return f"{ref} (unresolved reference)"
        return "any"

    def _text_limit(self) -> int | None:
        return self._detail.text_limit

    def _has_structure(self, schema: Mapping[str, Any], active: tuple[str, ...], hops: int) -> bool:
        if hops > _MAX_HOPS // 4:
            return False
        if schema.get("properties") or isinstance(schema.get("additionalProperties"), Mapping):
            return True
        if schema.get("oneOf") or schema.get("anyOf"):
            return True
        if "items" in schema:
            item, item_active, _, _ = self._prepare(schema["items"], active)
            return item is not None and self._has_structure(item, item_active, hops + 1)
        return False

    def _children(
        self,
        schema: Mapping[str, Any],
        active: tuple[str, ...],
        indent: int,
        depth: int,
        hops: int,
    ) -> list[str]:
        """Field lines for the contents of an (already prepared) schema."""
        if hops > _MAX_HOPS or id(schema) in self._open:
            return []  # too deep, or a schema object that contains itself
        self._open.add(id(schema))
        try:
            return self._children_of(schema, active, indent, depth, hops)
        finally:
            self._open.discard(id(schema))

    def _children_of(
        self,
        schema: Mapping[str, Any],
        active: tuple[str, ...],
        indent: int,
        depth: int,
        hops: int,
    ) -> list[str]:
        pad = "  " * indent
        if depth > self._detail.max_depth:
            if self._has_structure(schema, active, 0):
                self.omitted = True
                return [f"{pad}- … nested fields omitted"]
            return []
        lines: list[str] = []
        properties = schema.get("properties")
        required_raw = schema.get("required")
        required = (
            {r for r in required_raw if isinstance(r, str)}
            if isinstance(required_raw, list)
            else set()
        )
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                lines += self.field(str(name), sub, str(name) in required, indent, depth, active)
        extra = schema.get("additionalProperties")
        if isinstance(extra, Mapping):
            lines += self.field("<any other key>", extra, False, indent, depth, active)
        if "items" in schema:
            item, item_active, _, _ = self._prepare(schema["items"], active)
            if item is not None:
                lines += self._children(item, item_active, indent, depth, hops + 1)
        lines += self._variants(schema, active, indent, depth)
        return lines

    def _variants(
        self, schema: Mapping[str, Any], active: tuple[str, ...], indent: int, depth: int
    ) -> list[str]:
        lines: list[str] = []
        pad = "  " * indent
        for key, words in (("oneOf", "exactly one of"), ("anyOf", "any of")):
            options = schema.get(key)
            if not isinstance(options, list) or not options:
                continue
            lines.append(f"{pad}- {words} the following:")
            for number, option in enumerate(options[:_MAX_VARIANTS], start=1):
                resolved, option_active, ref, status = self._prepare(option, active)
                if resolved is None:
                    label = self._unusable_label(ref, status)
                    lines.append(f"{pad}  - option {number} ({label})")
                    continue
                desc = _one_line(resolved.get("description"), self._detail.field_desc_limit)
                lines.append(
                    f"{pad}  - option {number} ({self._head_label(resolved, option_active, ref)})"
                    + (f": {desc}" if desc else "")
                    + self._annotations(resolved)
                )
                lines += self._children(resolved, option_active, indent + 2, depth + 1, 0)
            if len(options) > _MAX_VARIANTS:
                self.omitted = True
                lines.append(f"{pad}  - … {len(options) - _MAX_VARIANTS} more options omitted")
        return lines


# ======================================================================= endpoint chunks


def _group_content(content: Sequence[ContentSchema]) -> list[tuple[list[str], Any]]:
    """Group media types that declare the same schema, keeping first-seen order."""
    groups: dict[str, tuple[list[str], Any]] = {}
    for entry in content:
        key = _canonical(entry.schema_definition)
        if key in groups:
            groups[key][0].append(entry.media_type)
        else:
            groups[key] = ([entry.media_type], entry.schema_definition)
    return list(groups.values())


def _content_lines(
    renderer: _SchemaRenderer,
    content: Sequence[ContentSchema],
    owner: str,
    seen: dict[str, str],
    *,
    schemas: bool,
) -> list[str]:
    """Render the media types and schemas of a body or response; repeats become references."""
    lines: list[str] = []
    for media_types, schema in _group_content(content):
        lines.append(f"Content type: {', '.join(media_types)}")
        if schema is None:
            lines.append("Schema: not declared")
        elif not schemas:
            lines.append(f"Schema: {renderer.summary(schema)}")
        else:
            block = renderer.block(schema)
            key = _canonical(schema)
            if len(block) > 1 and key in seen:
                lines.append(f"Schema: same as {seen[key]}")
            else:
                if len(block) > 1:
                    seen[key] = f"{owner} ({media_types[0]})"
                lines += block
    return lines


def _request_body_lines(
    body: RequestBody, renderer: _SchemaRenderer, seen: dict[str, str], detail: _Detail
) -> list[str]:
    lines = ["## Request Body" + (" (required)" if body.required else " (optional)")]
    if desc := _block_text(body.description, detail.text_limit):
        lines.append(desc)
    if body.content:
        lines += _content_lines(renderer, body.content, "the request body", seen, schemas=True)
    else:
        lines.append("No content types declared.")
    return lines


def _response_lines(
    response: Response, renderer: _SchemaRenderer, seen: dict[str, str], detail: _Detail
) -> list[str]:
    heading = f"### {response.status_code}"
    if desc := _one_line(response.description, detail.text_limit):
        heading += f" - {desc}"
    lines = [heading]
    if response.content:
        lines += _content_lines(
            renderer,
            response.content,
            f"response {response.status_code}",
            seen,
            schemas=detail.response_schemas,
        )
    else:
        lines.append("No response body.")
    return lines


def _parameter_lines(parameters: Sequence[Parameter], renderer: _SchemaRenderer) -> list[str]:
    lines: list[str] = []
    for location in ("path", "query", "header", "cookie"):
        group = [p for p in parameters if p.location == location]
        if not group:
            continue
        lines.append(f"### {location.capitalize()} parameters")
        for parameter in group:
            lines += renderer.field(
                parameter.name,
                parameter.schema_definition,
                parameter.required,
                0,
                1,
                (),
                description=parameter.description,
            )
    return lines


def _render_endpoint(
    endpoint: Endpoint, definitions: Mapping[str, Any], detail: _Detail
) -> tuple[str, bool]:
    """Render ``endpoint`` at one detail level. Returns ``(markdown, omitted_something)``."""
    renderer = _SchemaRenderer(definitions, detail)
    header = [f"# Endpoint: {endpoint.method} {endpoint.path}"]
    if endpoint.operation_id:
        header.append(f"Operation ID: {endpoint.operation_id}")
    if endpoint.tags:
        header.append(f"Tags: {', '.join(endpoint.tags)}")
    if summary := _one_line(endpoint.summary, detail.text_limit):
        header.append(f"Summary: {summary}")
    if description := _block_text(endpoint.description, detail.text_limit):
        header.append(f"Description: {description}")
    sections = ["\n".join(header)]

    if endpoint.parameters:
        sections.append(
            "\n".join(["## Parameters", *_parameter_lines(endpoint.parameters, renderer)])
        )
    seen: dict[str, str] = {}
    if endpoint.request_body is not None:
        sections.append(
            "\n".join(_request_body_lines(endpoint.request_body, renderer, seen, detail))
        )
    if endpoint.responses:
        lines = ["## Responses"]
        for response in endpoint.responses:
            lines += _response_lines(response, renderer, seen, detail)
        sections.append("\n".join(lines))
    return "\n\n".join(sections), renderer.omitted


def _hard_truncate(text: str, max_tokens: int, counter: TokenCounter) -> str:
    """Cut ``text`` so that text plus the truncation marker fits ``max_tokens``."""
    budget = max_tokens - counter.count(_TRUNCATION_MARKER)
    while True:
        head = _split_by_budget(text, max(budget, 1), counter)[0].rstrip()
        cut = head + _TRUNCATION_MARKER
        if counter.count(cut) <= max_tokens or budget <= 1:
            return cut
        budget -= max(1, counter.count(cut) - max_tokens)


def chunk_endpoint(
    endpoint: Endpoint,
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    recursive_schemas: Mapping[str, Any] | None = None,
    counter: TokenCounter | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> Chunk:
    """Build the self-contained Markdown chunk of one endpoint.

    Args:
        endpoint: Parser output (TASK-202); its schemas have internal ``$ref`` already inlined.
        project_id: Tenant of the document.
        document_id: Document the endpoint was extracted from.
        source_filename: Original filename of that document.
        recursive_schemas: ``ParsedSpecification.recursive_schemas`` (or the persisted
            ``Document.recursive_schemas``): targets of the ``$ref`` pointers the parser left
            in place because of recursion. Without them such references render as unresolved.
        counter: Token counter; defaults to the embedding model's tokenizer.
        max_tokens: Hard limit of the chunk, in the counter's tokens.

    Returns:
        A chunk of at most ``max_tokens`` tokens. If the full rendering was larger, detail is
        reduced step by step (shallower nesting, shorter descriptions, no constraints, no
        response schemas) and finally the text is cut at the limit; ``metadata.truncated`` is
        then True (a cut is also marked inside the text).

    Raises:
        ChunkingError: ``max_tokens`` is too small or the tokenizer is unavailable.
    """
    _validate_sizes(max_tokens, 0)
    token_counter = counter or default_token_counter()
    definitions = recursive_schemas or {}

    text = ""
    truncated = True
    for level, detail in enumerate(_LADDER):
        text, omitted = _render_endpoint(endpoint, definitions, detail)
        text = _clean(text)
        if token_counter.count(text) <= max_tokens:
            truncated = level > 0 or omitted
            break
    else:
        text = _hard_truncate(text, max_tokens, token_counter)
    if truncated:
        logger.warning(
            "endpoint chunk reduced to fit the token limit",
            extra={"http_method": endpoint.method, "route_path": endpoint.path},
        )
    return Chunk(
        content=text,
        metadata=ChunkMetadata(
            project_id=project_id,
            document_id=document_id,
            source_filename=source_filename,
            chunk_type="endpoint",
            http_method=endpoint.method,
            route_path=endpoint.path,
            tags=list(endpoint.tags),
            token_count=token_counter.count(text),
            truncated=truncated,
        ),
    )


def chunk_endpoints(
    endpoints: Sequence[Endpoint],
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    recursive_schemas: Mapping[str, Any] | None = None,
    counter: TokenCounter | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[Chunk]:
    """One chunk per endpoint, in input order (see :func:`chunk_endpoint`)."""
    token_counter = counter or default_token_counter()
    return [
        chunk_endpoint(
            endpoint,
            project_id=project_id,
            document_id=document_id,
            source_filename=source_filename,
            recursive_schemas=recursive_schemas,
            counter=token_counter,
            max_tokens=max_tokens,
        )
        for endpoint in endpoints
    ]


def chunk_specification(
    specification: ParsedSpecification,
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    counter: TokenCounter | None = None,
    max_tokens: int = MAX_CHUNK_TOKENS,
) -> list[Chunk]:
    """Endpoint chunks of a whole parsed specification (uses its recursive schemas)."""
    return chunk_endpoints(
        specification.endpoints,
        project_id=project_id,
        document_id=document_id,
        source_filename=source_filename,
        recursive_schemas=specification.recursive_schemas,
        counter=counter,
        max_tokens=max_tokens,
    )


# ======================================================================= narrative splitting

_PARAGRAPH: Final = re.compile(r"\n{2,}")
_LINE: Final = re.compile(r"\n")
_SENTENCE: Final = re.compile(r"(?<=[.!?])\s+")
_WORD: Final = re.compile(r"\s+")
_LEVELS: Final = (_PARAGRAPH, _LINE, _SENTENCE, _WORD)
_SENTENCE_LEVELS: Final = 3  # levels 0..2 only: paragraph, line, sentence
_LINES: Final = re.compile(r"[^\n]*\n|[^\n]+")
_FENCE_OPEN: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n?$")


@dataclass(frozen=True)
class _Unit:
    """An indivisible piece of text; ``code`` marks (re-fenced) code, which is never overlapped."""

    text: str
    code: bool


def _cut_after(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Split after every match, keeping separators so that ``''.join(pieces) == text``."""
    pieces: list[str] = []
    start = 0
    for match in pattern.finditer(text):
        if match.end() > start:
            pieces.append(text[start : match.end()])
            start = match.end()
    if start < len(text):
        pieces.append(text[start:])
    return pieces


def _split_by_budget(text: str, budget: int, counter: TokenCounter) -> list[str]:
    """Cut ``text`` into windows of at most ``budget`` tokens (character-safe, last resort)."""
    pieces: list[str] = []
    rest = text
    while rest:
        high = min(len(rest), budget * 4)
        while high < len(rest) and counter.count(rest[:high]) <= budget:
            high = min(len(rest), high * 2)
        low = 1
        while low < high:
            middle = (low + high + 1) // 2
            if counter.count(rest[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        pieces.append(rest[:low])
        rest = rest[low:]
    return pieces


def _atomize(
    text: str,
    budget: int,
    counter: TokenCounter,
    level: int = 0,
    last_level: int = len(_LEVELS),
    *,
    hard: bool = True,
) -> list[str]:
    """Recursively split ``text`` into pieces of at most ``budget`` tokens.

    Splits at paragraph, then line, sentence and word boundaries, descending only for pieces that
    are still too large. After the last level an oversized piece is cut into token-sized windows
    (``hard``) or returned as is.
    """
    if not text:
        return []
    if counter.count(text) <= budget:
        return [text]
    if level >= last_level:
        return _split_by_budget(text, budget, counter) if hard else [text]
    pieces = _cut_after(text, _LEVELS[level])
    if len(pieces) <= 1:
        return _atomize(text, budget, counter, level + 1, last_level, hard=hard)
    result: list[str] = []
    for piece in pieces:
        result += _atomize(piece, budget, counter, level + 1, last_level, hard=hard)
    return result


def _segments(text: str) -> list[tuple[str, bool]]:
    """Split ``text`` into ``(segment, is_fenced_code)`` runs; an unclosed fence runs to the end."""
    segments: list[tuple[str, bool]] = []
    prose: list[str] = []
    code: list[str] = []
    closing: re.Pattern[str] | None = None
    for line in _LINES.findall(text):
        if closing is None:
            if match := _FENCE_OPEN.match(line):
                if prose:
                    segments.append(("".join(prose), False))
                    prose = []
                code = [line]
                fence = match.group(1)
                closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*\n?$")
            else:
                prose.append(line)
        else:
            code.append(line)
            if closing.match(line):
                segments.append(("".join(code), True))
                code = []
                closing = None
    if closing is not None:
        segments.append(("".join(code), True))
    if prose:
        segments.append(("".join(prose), False))
    return segments


def _split_code(block: str, budget: int, counter: TokenCounter) -> list[str]:
    """Split an oversized fenced block by lines; each piece is wrapped in the original fence."""
    lines: list[str] = _LINES.findall(block)
    opener = lines[0] if lines[0].endswith("\n") else lines[0] + "\n"
    fence = _FENCE_OPEN.match(lines[0])
    marker = fence.group(1) if fence else "```"
    has_closer = len(lines) > 1 and re.match(
        rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$", lines[-1].rstrip("\n")
    )
    body = lines[1:-1] if has_closer else lines[1:]
    closer = f"{marker}\n"
    overhead = counter.count(opener + closer)

    def wrap(content: list[str]) -> str:
        text = "".join(content)
        return opener + text + ("" if text.endswith("\n") else "\n") + closer

    pieces: list[str] = []
    current: list[str] = []
    for line in body:
        if current and counter.count(wrap([*current, line])) > budget:
            pieces.append(wrap(current))
            current = []
        if not current and counter.count(wrap([line])) > budget:
            pieces.extend(
                wrap([part])
                for part in _split_by_budget(line, max(1, budget - overhead - 1), counter)
            )
        else:
            current.append(line)
    if current:
        pieces.append(wrap(current))
    return pieces or [block]


def _units(text: str, budget: int, counter: TokenCounter) -> list[_Unit]:
    """Indivisible units of ``text``: prose pieces and whole (or re-fenced) code blocks."""
    units: list[_Unit] = []
    for segment, is_code in _segments(text):
        if not is_code:
            units += [_Unit(piece, False) for piece in _atomize(segment, budget, counter)]
        elif counter.count(segment) <= budget:
            units.append(_Unit(segment, True))
        else:
            units += [_Unit(piece, True) for piece in _split_code(segment, budget, counter)]
    return units


def _overlap_tail(prose: str, overlap: int, counter: TokenCounter) -> str:
    """The longest tail of ``prose`` of at most ``overlap`` tokens, at the coarsest boundary.

    Whole sentences / lines are preferred; word boundaries are used only when no sentence fits.
    """
    if overlap <= 0:
        return ""
    for last_level in (_SENTENCE_LEVELS, len(_LEVELS)):
        tail = ""
        for piece in reversed(_atomize(prose, overlap, counter, 0, last_level, hard=False)):
            if counter.count(piece + tail) > overlap:
                break
            tail = piece + tail
        if tail.strip():
            return tail.lstrip()
    return ""


def _compose(header: str, body: str) -> str:
    body = body.strip()
    return f"{header}\n\n{body}" if header else body


def _split(
    text: str,
    max_tokens: int,
    overlap: int,
    counter: TokenCounter,
    header: str = "",
) -> list[str]:
    """Core splitter: chunks of ``header + body`` of at most ``max_tokens`` tokens each."""
    text = _clean(text).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    header_cost = counter.count(f"{header}\n\n") if header else 0
    units = _units(text, max(max_tokens - header_cost - 2, 1), counter)

    chunks: list[str] = []
    body = ""
    prose_run = ""  # text since the last code unit: the only source of overlap
    for unit in units:
        if body.strip() and counter.count(_compose(header, body + unit.text)) > max_tokens:
            chunks.append(_compose(header, body))
            carry = _overlap_tail(prose_run, overlap, counter)
            body = carry + unit.text
            prose_run = carry
            if carry and counter.count(_compose(header, body)) > max_tokens:
                body, prose_run = unit.text, ""
        else:
            body += unit.text
        prose_run = "" if unit.code else prose_run + unit.text
    if body.strip():
        chunks.append(_compose(header, body))

    # Token counts of a joined text can differ by a token from the sum of its parts; guarantee
    # the limit by measuring the final chunks.
    final: list[str] = []
    for chunk in chunks:
        if counter.count(chunk) <= max_tokens:
            final.append(chunk)
        else:
            final += [p.strip() for p in _split_by_budget(chunk, max_tokens, counter) if p.strip()]
    return final


def split_text(
    text: str,
    *,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
    counter: TokenCounter | None = None,
) -> list[str]:
    """Recursively split narrative text into chunks of at most ``max_tokens`` tokens.

    Splits at paragraph, line, sentence and word boundaries (largest first); fenced code blocks
    are kept whole when they fit. Consecutive chunks overlap by up to ``overlap_tokens`` tokens
    of trailing prose. Empty or whitespace-only input gives ``[]``.

    Raises:
        ChunkingError: ``max_tokens`` / ``overlap_tokens`` are invalid or the tokenizer is
            unavailable.
    """
    _validate_sizes(max_tokens, overlap_tokens)
    return _split(text, max_tokens, overlap_tokens, counter or default_token_counter())


def _narrative_chunk(
    content: str,
    heading_path: list[str],
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    counter: TokenCounter,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            project_id=project_id,
            document_id=document_id,
            source_filename=source_filename,
            chunk_type="narrative",
            heading_path=heading_path,
            token_count=counter.count(content),
        ),
    )


def chunk_text(
    text: str,
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
    counter: TokenCounter | None = None,
) -> list[Chunk]:
    """Narrative chunks of plain text (``.txt``, PDF text); see :func:`split_text`."""
    _validate_sizes(max_tokens, overlap_tokens)
    token_counter = counter or default_token_counter()
    return [
        _narrative_chunk(
            piece,
            [],
            project_id=project_id,
            document_id=document_id,
            source_filename=source_filename,
            counter=token_counter,
        )
        for piece in _split(text, max_tokens, overlap_tokens, token_counter)
    ]


def _fence(content: str) -> str:
    """A backtick fence longer than any backtick run inside ``content``."""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _section_text(section: MarkdownSection) -> str:
    """Prose followed by the section's code blocks (the parser does not keep their interleaving)."""
    parts = [section.text.strip()] if section.text.strip() else []
    for block in section.code_blocks:
        fence = _fence(block.content)
        body = block.content if block.content.endswith("\n") else block.content + "\n"
        parts.append(f"{fence}{block.language or ''}\n{body}{fence}")
    return "\n\n".join(parts)


def chunk_markdown(
    parsed: ParsedMarkdown,
    *,
    project_id: str,
    document_id: str,
    source_filename: str,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
    counter: TokenCounter | None = None,
) -> list[Chunk]:
    """Narrative chunks of a parsed Markdown document (TASK-203 output), in document order.

    Each section is chunked on its own, so a chunk never mixes sections. Every chunk starts with
    its section's heading line (re-added on continuation chunks so they keep their context) and
    carries the full ``heading_path`` in its metadata. Sections without content produce nothing.
    """
    _validate_sizes(max_tokens, overlap_tokens)
    token_counter = counter or default_token_counter()
    chunks: list[Chunk] = []
    for section in parsed.sections:
        body = _section_text(section)
        if not body.strip():
            continue
        header = ""
        if section.heading and section.level > 0:
            header = _one_line(f"{'#' * section.level} {section.heading}")
            if token_counter.count(header) > max_tokens // 4:
                header = _split_by_budget(header, max_tokens // 4, token_counter)[0].rstrip() + "…"
        for piece in _split(body, max_tokens, overlap_tokens, token_counter, header):
            chunks.append(
                _narrative_chunk(
                    piece,
                    list(section.heading_path),
                    project_id=project_id,
                    document_id=document_id,
                    source_filename=source_filename,
                    counter=token_counter,
                )
            )
    return chunks
