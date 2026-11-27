"""Tests for the endpoint-aware and narrative chunker (TASK-301).

They use the real tokenizer (``tiktoken``, cached after its first use) so every token-limit
assertion is a measurement, not an estimate.
"""

import itertools
import logging
from pathlib import Path
from typing import Any, TypedDict

import pytest

from app.core.exceptions import ChunkingError
from app.ingestion.markdown_parser import parse_markdown
from app.ingestion.openapi_parser import parse_openapi
from app.rag import chunking
from app.rag.chunking import (
    CHUNK_OVERLAP_TOKENS,
    MAX_CHUNK_TOKENS,
    TiktokenCounter,
    chunk_endpoint,
    chunk_endpoints,
    chunk_markdown,
    chunk_specification,
    chunk_text,
    default_token_counter,
    split_text,
)
from app.schemas.endpoint import (
    ContentSchema,
    Endpoint,
    Parameter,
    ParsedSpecification,
    RequestBody,
    Response,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "openapi"
COUNTER = default_token_counter()


class ChunkIds(TypedDict):
    project_id: str
    document_id: str
    source_filename: str


IDS: ChunkIds = {
    "project_id": "proj-1",
    "document_id": "doc-1",
    "source_filename": "spec.yaml",
}
NODE_REF = "#/components/schemas/Node"


def tokens(text: str) -> int:
    return COUNTER.count(text)


def json_content(schema: Any, media_type: str = "application/json") -> list[ContentSchema]:
    return [ContentSchema(media_type=media_type, schema_definition=schema)]


def make_endpoint(**overrides: Any) -> Endpoint:
    values: dict[str, Any] = {"method": "GET", "path": "/pets"}
    values.update(overrides)
    return Endpoint(**values)


def one(endpoint: Endpoint, **kwargs: Any) -> Any:
    return chunk_endpoint(endpoint, **IDS, **kwargs)


# ============================================================================ endpoint chunks


def test_single_endpoint_chunk_is_self_contained_markdown() -> None:
    endpoint = make_endpoint(
        method="POST",
        path="/v1/customers",
        summary="Create a customer",
        description="Registers a new customer.",
        operation_id="createCustomer",
        tags=["Customers", "Billing"],
    )
    chunk = one(endpoint)

    assert chunk.content.startswith("# Endpoint: POST /v1/customers\n")
    assert "Operation ID: createCustomer" in chunk.content
    assert "Tags: Customers, Billing" in chunk.content
    assert "Summary: Create a customer" in chunk.content
    assert "Description: Registers a new customer." in chunk.content
    assert chunk.metadata.chunk_type == "endpoint"
    assert chunk.metadata.tags == ["Customers", "Billing"]
    assert chunk.metadata.token_count == tokens(chunk.content)
    assert chunk.metadata.truncated is False


def test_multiple_endpoints_each_get_their_own_chunk_in_order() -> None:
    endpoints = [
        make_endpoint(method="GET", path="/pets"),
        make_endpoint(method="POST", path="/pets"),
        make_endpoint(method="DELETE", path="/pets/{id}"),
    ]
    chunks = chunk_endpoints(endpoints, **IDS)

    assert [(c.metadata.http_method, c.metadata.route_path) for c in chunks] == [
        ("GET", "/pets"),
        ("POST", "/pets"),
        ("DELETE", "/pets/{id}"),
    ]
    assert len({c.content for c in chunks}) == 3


def test_metadata_envelope_has_method_route_and_project() -> None:
    chunk = one(make_endpoint(method="PUT", path="/orders/{orderId}"))

    assert chunk.metadata.http_method == "PUT"
    assert chunk.metadata.route_path == "/orders/{orderId}"
    assert chunk.metadata.project_id == "proj-1"
    assert chunk.metadata.document_id == "doc-1"
    assert chunk.metadata.source_filename == "spec.yaml"


def test_project_id_is_taken_from_the_caller_for_every_chunk() -> None:
    chunks = chunk_endpoints(
        [make_endpoint(), make_endpoint(method="POST")],
        project_id="tenant-42",
        document_id="d",
        source_filename="f",
    )
    assert {c.metadata.project_id for c in chunks} == {"tenant-42"}


def test_path_query_header_and_cookie_parameters_are_included() -> None:
    endpoint = make_endpoint(
        path="/pets/{petId}",
        parameters=[
            Parameter(
                name="petId",
                location="path",
                required=True,
                description="Pet identifier",
                schema_definition={"type": "string", "format": "uuid"},
            ),
            Parameter(
                name="limit",
                location="query",
                required=False,
                schema_definition={"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            ),
            Parameter(
                name="X-Trace",
                location="header",
                required=False,
                schema_definition={"type": "string"},
            ),
            Parameter(name="session", location="cookie", required=False),
        ],
    )
    content = one(endpoint).content

    assert "### Path parameters\n- petId (string, uuid, required): Pet identifier" in content
    assert "### Query parameters" in content
    assert "- limit (integer, optional) [default: 20; min: 1; max: 100]" in content
    assert "### Header parameters\n- X-Trace (string, optional)" in content
    assert "### Cookie parameters\n- session (any, optional)" in content


def test_request_and_response_schemas_are_included() -> None:
    schema = {
        "type": "object",
        "required": ["email"],
        "properties": {
            "email": {"type": "string", "format": "email", "description": "Contact email"},
            "age": {"type": "integer", "minimum": 0},
        },
    }
    endpoint = make_endpoint(
        method="POST",
        request_body=RequestBody(
            description="Customer to create", required=True, content=json_content(schema)
        ),
        responses=[
            Response(status_code="201", description="Created", content=json_content(schema)),
            Response(status_code="400", description="Invalid input"),
        ],
    )
    content = one(endpoint).content

    assert "## Request Body (required)\nCustomer to create" in content
    assert "Content type: application/json" in content
    assert "- email (string, email, required): Contact email" in content
    assert "- age (integer, optional) [min: 0]" in content
    assert "## Responses" in content
    assert "### 201 - Created" in content
    assert "### 400 - Invalid input\nNo response body." in content


def test_identical_schemas_are_rendered_once_and_then_referenced() -> None:
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    endpoint = make_endpoint(
        method="POST",
        request_body=RequestBody(
            content=[
                ContentSchema(media_type="application/json", schema_definition=schema),
                ContentSchema(media_type="application/xml", schema_definition=schema),
            ]
        ),
        responses=[
            Response(status_code="200", content=json_content(schema)),
            Response(status_code="201", content=json_content(schema)),
        ],
    )
    content = one(endpoint).content

    assert "Content type: application/json, application/xml" in content
    assert content.count("- id (string, optional)") == 1
    assert content.count("Schema: same as the request body (application/json)") == 2


def test_ref_dereferencing_already_inlined_schema_keeps_nested_names() -> None:
    # The parser inlines references, so the chunker sees the dereferenced tree.
    spec = parse_openapi(
        """
openapi: 3.0.0
info: {title: t, version: "1"}
paths:
  /orders:
    post:
      requestBody:
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Order'}
      responses:
        '200': {description: ok}
components:
  schemas:
    Address:
      type: object
      required: [city]
      properties:
        city: {type: string}
        zip: {type: string}
    Order:
      type: object
      properties:
        shipping: {$ref: '#/components/schemas/Address'}
        billing: {$ref: '#/components/schemas/Address'}
"""
    )
    chunk = chunk_specification(spec, **IDS)[0]

    assert "- shipping (object, optional)\n  - city (string, required)" in chunk.content
    assert "- billing (object, optional)\n  - city (string, required)" in chunk.content
    assert "$ref" not in chunk.content


def test_nested_objects_arrays_enums_and_required_fields() -> None:
    schema = {
        "type": "object",
        "required": ["items", "status"],
        "properties": {
            "status": {"type": "string", "enum": ["open", "closed"], "default": "open"},
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["sku"],
                    "properties": {
                        "sku": {"type": "string", "pattern": "^[A-Z]+$"},
                        "dims": {
                            "type": "object",
                            "properties": {"w": {"type": "number"}, "h": {"type": "number"}},
                        },
                    },
                },
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "note": {"type": ["string", "null"]},
            "maybe": {"type": "string", "nullable": True},
        },
    }
    content = one(
        make_endpoint(method="POST", request_body=RequestBody(content=json_content(schema)))
    ).content

    assert '- status (string, required) [enum: "open" | "closed"; default: "open"]' in content
    assert "- items (array of object, required) [min items: 1]" in content
    assert '  - sku (string, required) [pattern: "^[A-Z]+$"]' in content
    assert "  - dims (object, optional)\n    - w (number, optional)" in content
    assert "- tags (array of string, optional)" in content
    assert "- note (string|null, optional)" in content
    assert "- maybe (string|null, optional)" in content


def test_one_of_any_of_and_all_of_are_rendered() -> None:
    schema = {
        "type": "object",
        "properties": {
            "payment": {
                "oneOf": [
                    {"type": "object", "properties": {"card": {"type": "string"}}},
                    {"type": "object", "properties": {"iban": {"type": "string"}}},
                ]
            },
            "extra": {
                "allOf": [
                    {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
                    {"type": "object", "properties": {"b": {"type": "integer"}}},
                ]
            },
        },
        "additionalProperties": {"type": "string"},
    }
    content = one(
        make_endpoint(method="POST", request_body=RequestBody(content=json_content(schema)))
    ).content

    assert "exactly one of the following:" in content
    assert "option 1 (object)" in content
    assert "- card (string, optional)" in content
    assert "- iban (string, optional)" in content
    assert "- a (string, required)" in content  # allOf members merged into one field list
    assert "- b (integer, optional)" in content
    assert "- <any other key> (string, optional)" in content


def test_circular_reference_is_expanded_once_and_then_marked_recursive() -> None:
    node = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "children": {"type": "array", "items": {"$ref": NODE_REF}},
            "parent": {"$ref": NODE_REF},
        },
    }
    endpoint = make_endpoint(
        responses=[Response(status_code="200", content=json_content({"$ref": NODE_REF}))]
    )
    content = one(endpoint, recursive_schemas={NODE_REF: node}).content

    assert "Schema: Node object" in content
    assert "- name (string, optional)" in content
    assert "- children (array of Node (recursive reference), optional)" in content
    assert "- parent (Node (recursive reference), optional)" in content
    assert content.count("- name (string, optional)") == 1  # not expanded a second time


def test_mutually_recursive_schemas_terminate() -> None:
    a_ref, b_ref = "#/components/schemas/A", "#/components/schemas/B"
    definitions = {
        a_ref: {"type": "object", "properties": {"b": {"$ref": b_ref}, "x": {"type": "string"}}},
        b_ref: {"type": "object", "properties": {"a": {"$ref": a_ref}, "y": {"type": "string"}}},
    }
    endpoint = make_endpoint(
        responses=[Response(status_code="200", content=json_content({"$ref": a_ref}))]
    )
    content = one(endpoint, recursive_schemas=definitions).content

    assert "- b (B object, optional)" in content
    assert "- a (A (recursive reference), optional)" in content
    assert "- x (string, optional)" in content
    assert "- y (string, optional)" in content


def test_python_level_cycle_in_a_schema_does_not_recurse_forever() -> None:
    loop: dict[str, Any] = {"type": "object", "properties": {}}
    loop["properties"]["self"] = loop
    loop["items"] = loop
    endpoint = make_endpoint(responses=[Response(status_code="200", content=json_content(loop))])

    chunk = one(endpoint)

    assert tokens(chunk.content) <= MAX_CHUNK_TOKENS
    assert "- self (object, optional)" in chunk.content


def test_missing_optional_fields_are_handled() -> None:
    chunk = one(make_endpoint())  # no summary, params, body, responses, tags

    assert chunk.content == "# Endpoint: GET /pets"
    assert chunk.metadata.tags == []


def test_missing_schema_and_description_are_handled() -> None:
    endpoint = make_endpoint(
        parameters=[Parameter(name="q", location="query", required=False)],
        request_body=RequestBody(content=[ContentSchema(media_type="text/plain")]),
        responses=[Response(status_code="default", content=json_content(True))],
    )
    content = one(endpoint).content

    assert "- q (any, optional)" in content
    assert "## Request Body (optional)\nContent type: text/plain\nSchema: not declared" in content
    assert "### default\nContent type: application/json\nSchema: any" in content


def test_request_body_without_content_is_stated() -> None:
    content = one(make_endpoint(request_body=RequestBody())).content
    assert "## Request Body (optional)\nNo content types declared." in content


@pytest.mark.parametrize(
    "schema",
    [
        {"type": 5, "properties": "nope", "required": "email", "enum": "x", "items": 3},
        {"properties": {"a": None, "b": [1, 2], "c": "str", 5: {"type": "string"}}},
        {"oneOf": "bad", "anyOf": [None, 3, {"$ref": "#/missing"}], "allOf": [1, {"$ref": 7}]},
        {"$ref": "#/components/schemas/DoesNotExist"},
        {"$ref": 12},
        {"type": "array", "items": {"$ref": "#/nope"}},
        {"enum": [object(), {"a": 1}, None]},
        False,
    ],
)
def test_malformed_schemas_never_raise_and_stay_within_the_limit(schema: Any) -> None:
    endpoint = make_endpoint(
        method="POST",
        parameters=[Parameter(name="p", location="query", required=True, schema_definition=schema)],
        request_body=RequestBody(content=json_content(schema)),
        responses=[Response(status_code="200", content=json_content(schema))],
    )
    chunk = one(endpoint)

    assert chunk.content.startswith("# Endpoint: POST /pets")
    assert tokens(chunk.content) <= MAX_CHUNK_TOKENS


def test_unresolvable_reference_is_reported_not_dropped() -> None:
    schema = {"type": "object", "properties": {"x": {"$ref": "#/components/schemas/Ghost"}}}
    content = one(
        make_endpoint(responses=[Response(status_code="200", content=json_content(schema))])
    ).content
    assert "- x (Ghost (unresolved reference), optional)" in content


def test_unicode_and_special_token_text_are_handled() -> None:
    endpoint = make_endpoint(
        summary="Liste des animaux 🐾 <|endoftext|>",
        description="Lone surrogate: \ud800 end",
    )
    chunk = one(endpoint)

    assert "<|endoftext|>" in chunk.content
    assert "\ud800" not in chunk.content
    chunk.content.encode("utf-8")  # must be encodable


def _wide_schema(fields: int, depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "description": "leaf " * 8}
    for level in range(depth):
        node = {
            "type": "object",
            "properties": {
                f"level{level}_field{i}": (
                    node if i == 0 else {"type": "string", "description": "x"}
                )
                for i in range(fields)
            },
        }
    return node


def test_large_schema_is_reduced_to_fit_and_flagged() -> None:
    endpoint = make_endpoint(
        method="POST",
        description="Long description. " * 100,
        request_body=RequestBody(content=json_content(_wide_schema(6, 5))),
        responses=[Response(status_code="200", content=json_content(_wide_schema(6, 5)))],
    )
    chunk = one(endpoint)

    assert tokens(chunk.content) <= MAX_CHUNK_TOKENS
    assert chunk.metadata.token_count == tokens(chunk.content)
    assert chunk.metadata.truncated is True
    assert chunk.content.startswith("# Endpoint: POST /pets")


def test_hard_truncation_marks_the_cut_and_keeps_the_signature() -> None:
    endpoint = make_endpoint(
        method="POST",
        path="/huge",
        request_body=RequestBody(content=json_content(_wide_schema(400, 1))),
    )
    chunk = one(endpoint)

    assert tokens(chunk.content) <= MAX_CHUNK_TOKENS
    assert chunk.metadata.truncated is True
    assert chunk.content.startswith("# Endpoint: POST /huge")
    assert chunk.content.endswith("[truncated: details exceeded the token limit]")


def test_reduction_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    endpoint = make_endpoint(
        description="Long description. " * 100,
        request_body=RequestBody(content=json_content(_wide_schema(6, 5))),
        responses=[Response(status_code="200", content=json_content(_wide_schema(6, 5)))],
    )
    with caplog.at_level(logging.WARNING, logger="app.rag.chunking"):
        one(endpoint)
    assert any("reduced" in record.getMessage() for record in caplog.records)


def test_custom_max_tokens_is_enforced() -> None:
    endpoint = make_endpoint(
        description="word " * 200,
        request_body=RequestBody(content=json_content(_wide_schema(5, 2))),
    )
    chunk = one(endpoint, max_tokens=100)

    assert tokens(chunk.content) <= 100
    assert chunk.metadata.truncated is True


def test_every_endpoint_of_real_specifications_fits_and_is_labelled() -> None:
    for name in ("petstore_3_0.yaml", "petstore_2_0.json", "stripe_sample.yaml"):
        spec = parse_openapi((FIXTURES / name).read_bytes())
        chunks = chunk_specification(spec, **IDS)

        assert len(chunks) == len(spec.endpoints) > 0
        for endpoint, chunk in zip(spec.endpoints, chunks, strict=True):
            assert tokens(chunk.content) <= MAX_CHUNK_TOKENS, (name, endpoint.path)
            assert chunk.metadata.token_count == tokens(chunk.content)
            assert chunk.metadata.http_method == endpoint.method
            assert chunk.metadata.route_path == endpoint.path
            assert chunk.content.startswith(f"# Endpoint: {endpoint.method} {endpoint.path}")


def test_recursive_stripe_schemas_are_cut_by_the_budget_and_never_leak_refs() -> None:
    spec = parse_openapi((FIXTURES / "stripe_sample.yaml").read_bytes())
    assert spec.recursive_schemas  # the fixture really contains cycles
    chunks = chunk_specification(spec, **IDS)

    for chunk in chunks:
        assert "$ref" not in chunk.content
        assert tokens(chunk.content) <= MAX_CHUNK_TOKENS
        if "nested fields omitted" in chunk.content:
            assert chunk.metadata.truncated is True  # omissions are never silent


def test_chunk_specification_with_no_endpoints_is_empty() -> None:
    assert chunk_specification(ParsedSpecification(spec_version="3.0"), **IDS) == []
    assert chunk_endpoints([], **IDS) == []


# ============================================================================ narrative chunks


def paragraphs(count: int, words: int = 40) -> str:
    return "\n\n".join(" ".join(f"p{p}w{w}" for w in range(words)) + "." for p in range(count))


def test_short_text_is_one_chunk() -> None:
    assert split_text("Hello world.") == ["Hello world."]


def test_markdown_sections_become_narrative_chunks_with_metadata() -> None:
    parsed = parse_markdown(
        "# Guide\n\nIntro text.\n\n## Auth\n\nUse a bearer token.\n\n"
        "```bash\ncurl -H 'Authorization: Bearer x' /v1\n```\n"
    )
    chunks = chunk_markdown(parsed, **IDS)

    assert [c.metadata.heading_path for c in chunks] == [["Guide"], ["Guide", "Auth"]]
    assert chunks[0].content == "# Guide\n\nIntro text."
    assert chunks[1].content.startswith("## Auth\n\nUse a bearer token.")
    assert "```bash\ncurl -H 'Authorization: Bearer x' /v1\n```" in chunks[1].content
    for chunk in chunks:
        assert chunk.metadata.chunk_type == "narrative"
        assert chunk.metadata.project_id == "proj-1"
        assert chunk.metadata.document_id == "doc-1"
        assert chunk.metadata.source_filename == "spec.yaml"
        assert chunk.metadata.http_method is None
        assert chunk.metadata.route_path is None
        assert chunk.metadata.token_count == tokens(chunk.content)


def test_content_before_any_heading_has_no_heading_line() -> None:
    chunks = chunk_markdown(parse_markdown("Just a preface.\n\n# Title\n\nBody."), **IDS)

    assert chunks[0].content == "Just a preface."
    assert chunks[0].metadata.heading_path == []
    assert chunks[1].content == "# Title\n\nBody."


def test_heading_only_sections_produce_no_chunk() -> None:
    chunks = chunk_markdown(parse_markdown("# Parent\n\n## Child\n\nText."), **IDS)

    assert len(chunks) == 1
    assert chunks[0].metadata.heading_path == ["Parent", "Child"]


def test_large_document_is_split_recursively_within_the_limit() -> None:
    text = paragraphs(60)
    assert tokens(text) > 5 * MAX_CHUNK_TOKENS

    pieces = split_text(text)

    assert len(pieces) > 5
    assert all(tokens(p) <= MAX_CHUNK_TOKENS for p in pieces)
    # nothing is lost: every word of the source survives in some chunk
    joined = " ".join(pieces)
    assert all(f"p{p}w{w}" in joined for p in range(60) for w in (0, 39))


def test_splits_prefer_paragraph_boundaries() -> None:
    pieces = split_text(paragraphs(30), overlap_tokens=0)

    for piece in pieces:
        assert piece.endswith(".")  # never cut inside a paragraph
        assert piece.startswith("p")


def test_a_single_oversized_paragraph_falls_back_to_sentences_then_words() -> None:
    sentences = " ".join(f"Sentence {i} is here and says something." for i in range(200))
    pieces = split_text(sentences, overlap_tokens=0)

    assert len(pieces) > 1
    assert all(tokens(p) <= MAX_CHUNK_TOKENS for p in pieces)
    assert all(p.endswith(".") for p in pieces)  # cut only at sentence ends

    blob = "word " * 2000  # no sentence boundaries: word level
    assert all(tokens(p) <= MAX_CHUNK_TOKENS for p in split_text(blob))


def test_text_without_any_separator_is_cut_by_tokens() -> None:
    blob = "abcdefghij" * 800
    pieces = split_text(blob, overlap_tokens=0)

    assert len(pieces) > 1
    assert all(tokens(p) <= MAX_CHUNK_TOKENS for p in pieces)
    assert "".join(pieces) == blob


def test_multibyte_text_without_separators_is_cut_without_corruption() -> None:
    blob = "日本語のテキスト🐾" * 600
    pieces = split_text(blob, overlap_tokens=0)

    assert all(tokens(p) <= MAX_CHUNK_TOKENS for p in pieces)
    assert "".join(pieces) == blob
    assert "�" not in "".join(pieces)


@pytest.mark.parametrize("limit", [32, 100, 512])
def test_no_chunk_ever_exceeds_the_configured_limit(limit: int) -> None:
    text = (
        "# Title\n\n"
        + paragraphs(20, 30)
        + "\n\n```python\n"
        + "\n".join(f"value_{i} = compute({i})" for i in range(120))
        + "\n```\n\n"
        + "x" * 3000
    )
    pieces = split_text(text, max_tokens=limit, overlap_tokens=limit // 8)

    assert pieces
    assert all(tokens(p) <= limit for p in pieces)


def test_consecutive_chunks_overlap_by_trailing_sentences() -> None:
    sentences = [f"Sentence number {i} explains part {i} of the feature." for i in range(120)]
    pieces = split_text(" ".join(sentences), max_tokens=128, overlap_tokens=40)

    assert len(pieces) > 3
    for previous, following in itertools.pairwise(pieces):
        last_sentence = previous.rsplit(". ", 1)[-1] if ". " in previous else previous
        shared = [s for s in sentences if s in previous and s in following]
        assert shared, "chunks must share at least one sentence"
        assert last_sentence.strip() in following
        # the overlap is bounded
        carry = following[: following.index(shared[-1]) + len(shared[-1])]
        assert tokens(carry) <= 40 + 2


def test_overlap_defaults_to_the_architecture_value() -> None:
    assert (MAX_CHUNK_TOKENS, CHUNK_OVERLAP_TOKENS) == (512, 64)
    pieces = split_text(paragraphs(60))
    carry = pieces[1].split("\n\n")[0]  # the repeated tail comes first in the next chunk
    assert pieces[0].endswith(carry)
    assert 0 < tokens(carry) <= CHUNK_OVERLAP_TOKENS


def test_zero_overlap_means_no_repeated_text() -> None:
    pieces = split_text(paragraphs(40), overlap_tokens=0)
    seen = [word for piece in pieces for word in piece.split()]
    assert len(seen) == len(set(seen))


def test_continuation_chunks_repeat_the_section_heading() -> None:
    body = paragraphs(40)
    chunks = chunk_markdown(parse_markdown(f"# Intro\n\n## Long section\n\n{body}\n"), **IDS)

    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.content.startswith("## Long section\n\n")
        assert chunk.metadata.heading_path == ["Intro", "Long section"]
        assert chunk.metadata.token_count <= MAX_CHUNK_TOKENS


def test_sections_are_never_mixed_in_one_chunk() -> None:
    chunks = chunk_markdown(parse_markdown("# A\n\nalpha text\n\n# B\n\nbeta text\n"), **IDS)

    assert len(chunks) == 2
    assert "beta" not in chunks[0].content
    assert "alpha" not in chunks[1].content


def test_code_block_that_fits_is_never_split() -> None:
    code = "\n".join(f"line_{i} = {i}" for i in range(30))
    text = paragraphs(8, 45) + f"\n\n```python\n{code}\n```\n\n" + paragraphs(8, 45)
    pieces = split_text(text)

    assert len(pieces) > 1
    holders = [p for p in pieces if "line_0 = 0" in p]
    assert any(f"```python\n{code}\n```" in p for p in holders)
    for piece in pieces:
        assert piece.count("```") % 2 == 0  # no dangling fence in any chunk


def test_oversized_code_block_is_split_into_refenced_pieces() -> None:
    code = "\n".join(f"call_{i}(argument_{i}, option={i})" for i in range(400))
    pieces = split_text(f"```python\n{code}\n```", overlap_tokens=0)

    assert len(pieces) > 1
    for piece in pieces:
        assert piece.startswith("```python\n")
        assert piece.endswith("\n```")
        assert tokens(piece) <= MAX_CHUNK_TOKENS
    rebuilt = "\n".join(p.removeprefix("```python\n").removesuffix("\n```") for p in pieces)
    assert rebuilt == code


def test_code_blocks_from_the_markdown_parser_survive_chunking() -> None:
    code = "SELECT *\nFROM t;"
    chunks = chunk_markdown(parse_markdown(f"# SQL\n\nRun:\n\n```sql\n{code}\n```\n"), **IDS)

    assert f"```sql\n{code}\n```" in chunks[0].content


def test_code_containing_backticks_gets_a_longer_fence() -> None:
    inner = "```\nnested\n```"
    chunks = chunk_markdown(parse_markdown(f"# T\n\nText\n\n````md\n{inner}\n````\n"), **IDS)

    assert f"````md\n{inner}\n````" in chunks[0].content


def test_overlap_never_starts_inside_a_code_block() -> None:
    code = "\n".join(f"x{i} = {i}" for i in range(40))
    text = paragraphs(6, 45) + f"\n\n```python\n{code}\n```\n\n" + paragraphs(6, 45)
    for piece in split_text(text, max_tokens=300, overlap_tokens=60):
        assert piece.count("```") % 2 == 0


def test_chunk_text_builds_narrative_chunks() -> None:
    chunks = chunk_text(paragraphs(40), **IDS)

    assert len(chunks) > 1
    assert all(c.metadata.chunk_type == "narrative" for c in chunks)
    assert all(c.metadata.heading_path == [] for c in chunks)
    assert all(c.metadata.token_count == tokens(c.content) <= MAX_CHUNK_TOKENS for c in chunks)
    assert {c.metadata.project_id for c in chunks} == {"proj-1"}


def test_chunking_is_deterministic() -> None:
    text = paragraphs(40)
    assert split_text(text) == split_text(text)


def test_windows_line_endings_are_normalised() -> None:
    assert split_text("one\r\n\r\ntwo\rthree") == ["one\n\ntwo\nthree"]


# ============================================================================ empty & malformed


@pytest.mark.parametrize("text", ["", " ", "\n\n\t\n", "\r\n"])
def test_empty_or_blank_text_produces_no_chunks(text: str) -> None:
    assert split_text(text) == []
    assert chunk_text(text, **IDS) == []


def test_empty_markdown_document_produces_no_chunks() -> None:
    assert chunk_markdown(parse_markdown(""), **IDS) == []
    assert chunk_markdown(parse_markdown("\n\n"), **IDS) == []


def test_special_token_text_and_lone_surrogates_in_narrative_are_handled() -> None:
    pieces = split_text("Docs mention <|endoftext|> and \ud800 oddities.")

    assert pieces == ["Docs mention <|endoftext|> and ? oddities."]


@pytest.mark.parametrize(
    ("max_tokens", "overlap"),
    [(0, 0), (-5, 0), (8, 0), (100, -1), (100, 50), (100, 400)],
)
def test_invalid_sizes_are_rejected(max_tokens: int, overlap: int) -> None:
    with pytest.raises(ChunkingError):
        split_text("text", max_tokens=max_tokens, overlap_tokens=overlap)
    with pytest.raises(ChunkingError):
        chunk_text("text", **IDS, max_tokens=max_tokens, overlap_tokens=overlap)
    with pytest.raises(ChunkingError):
        chunk_markdown(
            parse_markdown("# t\n\nx"), **IDS, max_tokens=max_tokens, overlap_tokens=overlap
        )


def test_invalid_endpoint_limit_is_rejected() -> None:
    with pytest.raises(ChunkingError) as error:
        one(make_endpoint(), max_tokens=4)
    assert error.value.code == "INVALID_CHUNK_SIZE"


def test_unavailable_tokenizer_is_a_domain_error() -> None:
    with pytest.raises(ChunkingError) as error:
        TiktokenCounter("not-a-real-model")
    assert error.value.code == "TOKENIZER_UNAVAILABLE"


def test_custom_token_counter_is_used() -> None:
    class WordCounter:
        def count(self, text: str) -> int:
            return len(text.split())

    pieces = split_text(
        " ".join(f"w{i}" for i in range(100)),
        max_tokens=20,
        overlap_tokens=0,
        counter=WordCounter(),
    )
    assert all(len(p.split()) <= 20 for p in pieces)
    assert len(pieces) >= 5


def test_default_counter_matches_the_embedding_model_encoding() -> None:
    assert chunking.EMBEDDING_MODEL == "text-embedding-3-small"
    assert default_token_counter() is default_token_counter()
    assert tokens("hello world") == 2
