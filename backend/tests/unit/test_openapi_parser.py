"""Tests for the OpenAPI parser, $ref resolver and endpoint extraction (TASK-202)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.exceptions import DocumentParsingError
from app.ingestion import openapi_parser
from app.ingestion.openapi_parser import (
    RefResolver,
    load_openapi_document,
    parse_json_pointer,
    parse_openapi,
)
from app.schemas.endpoint import Endpoint, ParsedSpecification

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "openapi"
SCHEMAS = "#/components/schemas"


# ------------------------------------------------------------------------------ helpers


def dump(document: dict[str, Any]) -> str:
    return json.dumps(document)


def spec3(
    paths: dict[str, Any] | None = None,
    schemas: dict[str, Any] | None = None,
    *,
    version: str = "3.0.3",
    components: dict[str, Any] | None = None,
) -> str:
    """Build a minimal OpenAPI 3.x document as JSON text."""
    document: dict[str, Any] = {
        "openapi": version,
        "info": {"title": "t", "version": "1"},
        "paths": paths or {},
    }
    if schemas or components:
        document["components"] = {**(components or {}), **({"schemas": schemas} if schemas else {})}
    return dump(document)


def get_op(response_schema: dict[str, Any], **operation: Any) -> dict[str, Any]:
    """A path item with a GET whose 200 response uses ``response_schema``."""
    responses = {
        "200": {"description": "ok", "content": {"application/json": {"schema": response_schema}}}
    }
    return {"get": {"responses": responses, **operation}}


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"{SCHEMAS}/{name}"}


def parse_one(text: str) -> Endpoint:
    parsed = parse_openapi(text)
    assert len(parsed.endpoints) == 1
    return parsed.endpoints[0]


def response_schema(endpoint: Endpoint, status: str = "200") -> Any:
    response = next(r for r in endpoint.responses if r.status_code == status)
    return response.content[0].schema_definition


def remaining_refs(node: Any) -> set[str]:
    """All '$ref' strings left in ``node`` (ignoring free-form x- extensions)."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value)
            elif not (isinstance(key, str) and key.startswith("x-")):
                found |= remaining_refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= remaining_refs(item)
    return found


def error_code(text: str | bytes) -> str:
    with pytest.raises(DocumentParsingError) as exc_info:
        parse_openapi(text)
    return exc_info.value.code


# ------------------------------------------------------------------------------ basic parsing


def test_openapi_3_0_yaml() -> None:
    parsed = parse_openapi(
        "openapi: 3.0.3\ninfo: {title: t, version: '1'}\n"
        "paths:\n  /ping:\n    get:\n      summary: Ping\n      responses:\n"
        "        '200': {description: pong}\n"
    )
    assert parsed.spec_version == "3.0"
    assert [(e.method, e.path, e.summary) for e in parsed.endpoints] == [("GET", "/ping", "Ping")]


def test_openapi_3_1_yaml() -> None:
    parsed = parse_openapi(
        "openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths:\n  /a:\n    get: {}\n"
    )
    assert parsed.spec_version == "3.1"
    assert len(parsed.endpoints) == 1


def test_openapi_3_1_without_paths_has_no_endpoints() -> None:
    parsed = parse_openapi("openapi: 3.1.0\ninfo: {title: t, version: '1'}\nwebhooks: {}\n")
    assert parsed.endpoints == []


def test_swagger_2_yaml() -> None:
    parsed = parse_openapi(
        'swagger: "2.0"\ninfo: {title: t, version: "1"}\n'
        "paths:\n  /a:\n    get:\n      responses:\n        200: {description: ok}\n"
    )
    assert parsed.spec_version == "2.0"
    assert parsed.endpoints[0].responses[0].status_code == "200"


def test_json_document_and_bytes_with_bom() -> None:
    text = spec3({"/a": {"get": {}}})
    assert parse_openapi(text).spec_version == "3.0"
    assert len(parse_openapi(b"\xef\xbb\xbf" + text.encode()).endpoints) == 1


def test_tab_indented_json_is_supported() -> None:
    text = json.dumps(json.loads(spec3({"/a": {"get": {}}})), indent="\t")
    assert len(parse_openapi(text).endpoints) == 1


def test_unquoted_yaml_float_version_is_accepted() -> None:
    version, _ = load_openapi_document("openapi: 3.0\npaths: {}\n")
    assert version == "3.0"


@pytest.mark.parametrize("content", ["a: [unclosed", "key: : :\n  - x", "a: 'unterminated"])
def test_invalid_yaml(content: str) -> None:
    assert error_code(content) == "INVALID_YAML"


def test_unsafe_yaml_tags_are_rejected_without_echoing_content() -> None:
    payload = 'openapi: 3.0.0\nx: !!python/object/apply:os.system ["echo pwned"]\n'
    with pytest.raises(DocumentParsingError) as exc_info:
        parse_openapi(payload)
    assert exc_info.value.code == "INVALID_YAML"
    assert "pwned" not in exc_info.value.message
    assert "os.system" not in exc_info.value.message


@pytest.mark.parametrize("content", ["- a\n- b\n", "just a string", "", "42", "[1, 2]"])
def test_invalid_root_structure(content: str) -> None:
    assert error_code(content) == "INVALID_SPEC_STRUCTURE"


def test_non_utf8_bytes() -> None:
    assert error_code(b"openapi: \xff\xfe") == "INVALID_ENCODING"


@pytest.mark.parametrize(
    "content",
    [
        "swagger: '1.2'\npaths: {}",
        "openapi: 4.0.0\npaths: {}",
        "openapi: 2.0\npaths: {}",
        "openapi: banana\npaths: {}",
        "info: {title: x}\npaths: {}",
    ],
)
def test_unsupported_or_missing_version(content: str) -> None:
    assert error_code(content) == "UNSUPPORTED_SPEC_VERSION"


@pytest.mark.parametrize("header", ['swagger: "2.0"', "openapi: 3.0.3"])
def test_missing_paths_is_rejected_before_3_1(header: str) -> None:
    assert error_code(f"{header}\ninfo: {{}}\n") == "INVALID_SPEC_STRUCTURE"


def test_paths_must_be_a_mapping() -> None:
    assert error_code("openapi: 3.0.0\npaths: [1]\n") == "INVALID_SPEC_STRUCTURE"


# ------------------------------------------------------------------------------ $ref resolution


def test_simple_schema_reference() -> None:
    text = spec3(
        {"/a": get_op(ref("User"))},
        {"User": {"type": "object", "properties": {"id": {"type": "integer"}}}},
    )
    schema = response_schema(parse_one(text))
    assert schema == {"type": "object", "properties": {"id": {"type": "integer"}}}


def test_nested_references_across_components() -> None:
    schemas = {
        "Order": {"type": "object", "properties": {"customer": ref("Customer")}},
        "Customer": {"type": "object", "properties": {"address": ref("Address")}},
        "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
    }
    parsed = parse_openapi(spec3({"/a": get_op(ref("Order"))}, schemas))
    schema = response_schema(parsed.endpoints[0])
    assert schema["properties"]["customer"]["properties"]["address"]["properties"]["city"] == {
        "type": "string"
    }
    assert remaining_refs(schema) == set()
    assert parsed.recursive_schemas == {}


def test_reference_chain_of_aliases() -> None:
    schemas = {"A": ref("B"), "B": ref("C"), "C": {"type": "string"}}
    assert response_schema(parse_one(spec3({"/a": get_op(ref("A"))}, schemas))) == {
        "type": "string"
    }


def test_shared_reference_is_resolved_once_and_shared() -> None:
    document = json.loads(
        spec3(
            {"/a": get_op({"properties": {"x": ref("Tag"), "y": ref("Tag")}})},
            {
                "Tag": {"type": "object", "properties": {"n": ref("Name")}},
                "Name": {"type": "string"},
            },
        )
    )
    resolved = RefResolver(document).resolve(document["paths"])
    props = resolved["/a"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "properties"
    ]
    assert props["x"] is props["y"]
    assert props["x"] == {"type": "object", "properties": {"n": {"type": "string"}}}


def test_shared_reference_is_not_reported_as_recursive() -> None:
    schemas = {"Tag": {"type": "string"}}
    schema = {"properties": {"a": ref("Tag"), "b": {"items": ref("Tag")}}}
    parsed = parse_openapi(spec3({"/a": get_op(schema)}, schemas))
    assert parsed.recursive_schemas == {}
    assert remaining_refs(response_schema(parsed.endpoints[0])) == set()


def test_document_is_not_mutated() -> None:
    document = json.loads(
        spec3({"/a": get_op(ref("User"))}, {"User": {"properties": {"me": ref("User")}}})
    )
    snapshot = copy.deepcopy(document)
    resolver = RefResolver(document)
    resolver.resolve(document["paths"])
    resolver.resolve({"$ref": f"{SCHEMAS}/User"})
    assert document == snapshot


def test_subtrees_without_refs_are_not_copied() -> None:
    document = {"components": {"schemas": {"S": {"type": "string"}}}, "plain": {"a": [1, {"b": 2}]}}
    resolved = RefResolver(document).resolve(document["plain"])
    assert resolved is document["plain"]


def test_missing_reference() -> None:
    text = spec3({"/a": get_op(ref("Nope"))}, {"User": {}})
    with pytest.raises(DocumentParsingError) as exc_info:
        parse_openapi(text)
    assert exc_info.value.code == "REF_NOT_FOUND"
    assert "#/components/schemas/Nope" in exc_info.value.message


@pytest.mark.parametrize(
    "pointer",
    [
        "#components/schemas/User",  # missing leading slash
        "#/components/schemas/a~2b",  # invalid escape
        "#/components/schemas/a~",  # dangling tilde
    ],
)
def test_invalid_json_pointer(pointer: str) -> None:
    text = spec3({"/a": get_op({"$ref": pointer})}, {"User": {}})
    assert error_code(text) == "INVALID_REF"


@pytest.mark.parametrize("pointer", ["other.yaml#/User", "https://example.com/x.json#/User", ""])
def test_external_references_are_rejected(pointer: str) -> None:
    text = spec3({"/a": get_op({"$ref": pointer})}, {"User": {}})
    assert error_code(text) == "UNSUPPORTED_REF"


def test_array_index_pointer_and_out_of_range() -> None:
    document = {"list": [{"type": "string"}, {"type": "integer"}]}
    resolver = RefResolver(document)
    assert resolver.resolve({"$ref": "#/list/1"}) == {"type": "integer"}
    for bad in ("#/list/2", "#/list/01", "#/list/-1", "#/list/x"):
        with pytest.raises(DocumentParsingError) as exc_info:
            resolver.resolve({"$ref": bad})
        assert exc_info.value.code == "REF_NOT_FOUND"


def test_escaped_pointer_tokens() -> None:
    schemas = {
        "a/b": {"type": "string"},
        "c~d": {"type": "integer"},
        "~1": {"type": "boolean"},  # name is literally "~1"; pointer token "~01"
    }
    schema = {
        "properties": {
            "slash": {"$ref": f"{SCHEMAS}/a~1b"},
            "tilde": {"$ref": f"{SCHEMAS}/c~0d"},
            "both": {"$ref": f"{SCHEMAS}/~01"},
            "encoded": {"$ref": f"{SCHEMAS}/a%7E1b"},
        }
    }
    props = response_schema(parse_one(spec3({"/a": get_op(schema)}, schemas)))["properties"]
    assert props["slash"] == {"type": "string"}
    assert props["tilde"] == {"type": "integer"}
    assert props["both"] == {"type": "boolean"}
    assert props["encoded"] == {"type": "string"}


def test_parse_json_pointer_tokens() -> None:
    assert parse_json_pointer("#") == ()
    assert parse_json_pointer("#/a~1b/c~0d/~01") == ("a/b", "c~d", "~1")


def test_ref_to_parameters_and_responses_openapi3() -> None:
    components = {
        "parameters": {"Id": {"name": "id", "in": "path", "required": True, "schema": ref("Id")}},
        "responses": {
            "NotFound": {
                "description": "missing",
                "content": {"application/json": {"schema": ref("Err")}},
            }
        },
        "requestBodies": {
            "Create": {"required": True, "content": {"application/json": {"schema": ref("Err")}}}
        },
    }
    schemas = {"Id": {"type": "integer"}, "Err": {"type": "object"}}
    paths = {
        "/a/{id}": {
            "post": {
                "parameters": [{"$ref": "#/components/parameters/Id"}],
                "requestBody": {"$ref": "#/components/requestBodies/Create"},
                "responses": {"404": {"$ref": "#/components/responses/NotFound"}},
            }
        }
    }
    endpoint = parse_one(spec3(paths, schemas, components=components))
    assert endpoint.parameters[0].name == "id"
    assert endpoint.parameters[0].schema_definition == {"type": "integer"}
    assert endpoint.request_body is not None
    assert endpoint.request_body.required is True
    assert endpoint.request_body.content[0].schema_definition == {"type": "object"}
    assert endpoint.responses[0].description == "missing"
    assert response_schema(endpoint, "404") == {"type": "object"}


def test_ref_siblings_are_merged_over_target() -> None:
    schemas = {"Base": {"type": "string", "description": "base"}}
    schema = {"$ref": f"{SCHEMAS}/Base", "description": "override"}
    assert response_schema(parse_one(spec3({"/a": get_op(schema)}, schemas, version="3.1.0"))) == {
        "type": "string",
        "description": "override",
    }


def test_free_form_values_are_not_dereferenced() -> None:
    schema = {
        "type": "string",
        "example": {"$ref": "#/does/not/exist"},
        "default": {"$ref": "#/nope"},
        "x-note": {"$ref": "#/nope"},
        "properties": {"default": ref("S"), "example": {"type": "integer"}},
    }
    result = response_schema(parse_one(spec3({"/a": get_op(schema)}, {"S": {"type": "null"}})))
    assert result["example"] == {"$ref": "#/does/not/exist"}
    assert result["x-note"] == {"$ref": "#/nope"}
    # A *property* that happens to be called "default" is still a schema.
    assert result["properties"]["default"] == {"type": "null"}


def test_property_named_ref_is_not_treated_as_a_reference() -> None:
    schema = {"type": "object", "properties": {"$ref": {"type": "string"}}}
    assert response_schema(parse_one(spec3({"/a": get_op(schema)}))) == schema


def test_openapi2_ref_targets() -> None:
    parsed = parse_openapi(FIXTURES.joinpath("petstore_2_0.json").read_text(encoding="utf-8"))
    get = next(e for e in parsed.endpoints if e.method == "GET")
    assert get.parameters[0].name == "petId"
    assert response_schema(get)["properties"]["category"]["properties"]["name"] == {
        "type": "string"
    }
    assert response_schema(get, "404")["properties"]["code"] == {"type": "integer"}


# ------------------------------------------------------------------------------ cyclic references


def test_self_referencing_schema() -> None:
    schemas = {
        "Node": {
            "type": "object",
            "properties": {"children": {"type": "array", "items": ref("Node")}},
        }
    }
    parsed = parse_openapi(spec3({"/a": get_op(ref("Node"))}, schemas))
    items = response_schema(parsed.endpoints[0])["properties"]["children"]["items"]
    assert items == {"$ref": f"{SCHEMAS}/Node"}
    assert set(parsed.recursive_schemas) == {f"{SCHEMAS}/Node"}
    assert parsed.recursive_schemas[f"{SCHEMAS}/Node"]["properties"]["children"]["items"] == items


def test_two_schemas_referencing_each_other() -> None:
    schemas = {
        "User": {"type": "object", "properties": {"address": ref("Address")}},
        "Address": {"type": "object", "properties": {"owner": ref("User")}},
    }
    parsed = parse_openapi(
        spec3({"/u": get_op(ref("User")), "/a": get_op(ref("Address"))}, schemas)
    )
    user_endpoint, address_endpoint = parsed.endpoints
    # A reference back into the recursive group is preserved instead of expanded.
    assert response_schema(user_endpoint) == {
        "type": "object",
        "properties": {"address": {"$ref": f"{SCHEMAS}/Address"}},
    }
    assert response_schema(address_endpoint) == {
        "type": "object",
        "properties": {"owner": {"$ref": f"{SCHEMAS}/User"}},
    }
    # ...and the preserved targets are exposed, so no relationship is lost.
    assert parsed.recursive_schemas == {
        f"{SCHEMAS}/User": response_schema(user_endpoint),
        f"{SCHEMAS}/Address": response_schema(address_endpoint),
    }


def test_acyclic_reference_into_recursive_schema_is_inlined() -> None:
    schemas = {
        "Order": {"properties": {"owner": ref("User")}},
        "User": {"properties": {"name": {"type": "string"}, "friend": ref("User")}},
    }
    parsed = parse_openapi(spec3({"/o": get_op(ref("Order"))}, schemas))
    owner = response_schema(parsed.endpoints[0])["properties"]["owner"]
    assert owner["properties"]["name"] == {"type": "string"}
    assert owner["properties"]["friend"] == {"$ref": f"{SCHEMAS}/User"}
    assert set(parsed.recursive_schemas) == {f"{SCHEMAS}/User"}


def test_complex_cycle_graph_terminates_and_keeps_all_relationships() -> None:
    schemas = {
        "A": {"properties": {"b": ref("B"), "self": ref("A")}},
        "B": {"properties": {"c": ref("C"), "b": ref("B")}},
        "C": {"properties": {"a": ref("A"), "d": ref("D")}},
        "D": {"properties": {"a": ref("A"), "c": ref("C")}},
        "Leaf": {"properties": {"a": ref("A"), "name": {"type": "string"}}},
    }
    parsed = parse_openapi(spec3({"/a": get_op(ref("A")), "/l": get_op(ref("Leaf"))}, schemas))
    a_schema, leaf_schema = (response_schema(e) for e in parsed.endpoints)
    assert a_schema["properties"] == {
        "b": {"$ref": f"{SCHEMAS}/B"},
        "self": {"$ref": f"{SCHEMAS}/A"},
    }
    # Leaf is not part of the cycle, so it inlines A (one layer) and keeps the way out.
    assert leaf_schema["properties"]["a"] == a_schema
    assert set(parsed.recursive_schemas) == {f"{SCHEMAS}/{name}" for name in "ABCD"}
    for pointer, target in parsed.recursive_schemas.items():
        assert remaining_refs(target) <= set(parsed.recursive_schemas), pointer


def test_cycle_through_parameters_and_responses() -> None:
    components = {
        "parameters": {"P": {"name": "p", "in": "query", "schema": ref("S")}},
    }
    schemas = {"S": {"properties": {"again": ref("S")}}}
    endpoint = parse_one(
        spec3(
            {"/a": {"get": {"parameters": [{"$ref": "#/components/parameters/P"}]}}},
            schemas,
            components=components,
        )
    )
    assert endpoint.parameters[0].schema_definition == {
        "properties": {"again": {"$ref": f"{SCHEMAS}/S"}}
    }


def test_cyclic_output_does_not_depend_on_path_order() -> None:
    schemas = {
        "A": {"properties": {"b": ref("B")}},
        "B": {"properties": {"a": ref("A"), "leaf": ref("Leaf")}},
        "Leaf": {"type": "string"},
    }
    forward = parse_openapi(spec3({"/a": get_op(ref("A")), "/b": get_op(ref("B"))}, schemas))
    backward = parse_openapi(spec3({"/b": get_op(ref("B")), "/a": get_op(ref("A"))}, schemas))
    assert {e.path: e for e in forward.endpoints} == {e.path: e for e in backward.endpoints}
    assert forward.recursive_schemas == backward.recursive_schemas
    assert response_schema(forward.endpoints[1])["properties"]["leaf"] == {"type": "string"}


def test_ref_to_document_root_is_a_cycle_not_infinite() -> None:
    document = {"a": {"$ref": "#"}}
    assert RefResolver(document).resolve(document) == {"a": {"a": {"$ref": "#"}}}


def test_deeply_nested_structure_is_rejected_cleanly() -> None:
    node: dict[str, Any] = {"type": "string"}
    for _ in range(400):
        node = {"type": "array", "items": node}
    assert error_code(spec3({"/a": get_op(node)})) == "SPEC_TOO_COMPLEX"


def test_extremely_deep_json_is_rejected_cleanly() -> None:
    assert error_code("[" * 100_000) == "SPEC_TOO_COMPLEX"


def test_alias_bomb_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openapi_parser, "MAX_RESOLVE_NODES", 5_000)
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 8)]
    document = yaml.safe_load("\n".join(lines))  # aliases share objects: cheap to load
    with pytest.raises(DocumentParsingError) as exc_info:
        RefResolver(document).resolve(document)
    assert exc_info.value.code == "SPEC_TOO_COMPLEX"


# ------------------------------------------------------------------------------ endpoint extraction


def test_get_endpoint_fields() -> None:
    paths = {
        "/pets": {
            "get": {
                "summary": "List",
                "description": "List all pets",
                "operationId": "listPets",
                "tags": ["pets"],
                "responses": {"200": {"description": "ok"}},
            }
        }
    }
    endpoint = parse_one(spec3(paths))
    assert (endpoint.method, endpoint.path) == ("GET", "/pets")
    assert endpoint.summary == "List"
    assert endpoint.description == "List all pets"
    assert endpoint.operation_id == "listPets"
    assert endpoint.tags == ["pets"]
    assert endpoint.request_body is None
    assert endpoint.parameters == []


def test_post_endpoint_with_request_body() -> None:
    body = {
        "description": "New pet",
        "required": True,
        "content": {
            "application/json": {"schema": {"type": "object"}},
            "text/plain": {},
        },
    }
    endpoint = parse_one(spec3({"/pets": {"post": {"requestBody": body}}}))
    assert endpoint.method == "POST"
    assert endpoint.request_body is not None
    assert endpoint.request_body.description == "New pet"
    assert endpoint.request_body.required is True
    assert [(c.media_type, c.schema_definition) for c in endpoint.request_body.content] == [
        ("application/json", {"type": "object"}),
        ("text/plain", None),
    ]


def test_request_body_defaults_to_not_required() -> None:
    body = {"content": {"application/json": {"schema": {"type": "string"}}}}
    endpoint = parse_one(spec3({"/a": {"post": {"requestBody": body}}}))
    assert endpoint.request_body is not None
    assert endpoint.request_body.required is False


@pytest.mark.parametrize(
    "method", ["get", "post", "put", "patch", "delete", "options", "head", "trace"]
)
def test_all_http_methods(method: str) -> None:
    endpoint = parse_one(spec3({"/a": {method: {"summary": method}}}))
    assert endpoint.method == method.upper()
    assert endpoint.summary == method


def test_path_item_metadata_is_not_an_operation() -> None:
    path_item = {
        "summary": "shared",
        "description": "shared description",
        "servers": [{"url": "https://x"}],
        "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
        "x-internal": {"get": {}},
        "get": {"summary": "real"},
    }
    parsed = parse_openapi(spec3({"/a": path_item, "x-ignored": {"get": {}}}))
    assert [(e.method, e.summary) for e in parsed.endpoints] == [("GET", "real")]
    assert parsed.endpoints[0].parameters[0].name == "q"


def test_parameter_locations_and_requiredness() -> None:
    params = [
        {"name": "id", "in": "path", "schema": {"type": "string"}},  # path => always required
        {"name": "q", "in": "query", "description": "search", "schema": {"type": "string"}},
        {"name": "X-Key", "in": "header", "required": True, "schema": {"type": "string"}},
        {"name": "sid", "in": "cookie", "schema": {"type": "string"}},
        {
            "name": "filter",
            "in": "query",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
    ]
    endpoint = parse_one(spec3({"/a/{id}": {"get": {"parameters": params}}}))
    by_name = {p.name: p for p in endpoint.parameters}
    assert [p.location for p in endpoint.parameters] == [
        "path",
        "query",
        "header",
        "cookie",
        "query",
    ]
    assert by_name["id"].required is True
    assert by_name["q"].required is False
    assert by_name["q"].description == "search"
    assert by_name["X-Key"].required is True
    assert by_name["filter"].schema_definition == {"type": "object"}


def test_path_level_and_operation_level_parameters_merge() -> None:
    path_item = {
        "parameters": [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "description": "path", "schema": {"type": "integer"}},
        ],
        "get": {
            "parameters": [
                {
                    "name": "limit",
                    "in": "query",
                    "description": "op",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {"name": "limit", "in": "header", "schema": {"type": "string"}},
            ]
        },
        "delete": {},
    }
    parsed = parse_openapi(spec3({"/a/{id}": path_item}))
    get, delete = parsed.endpoints
    assert [p.name for p in delete.parameters] == ["id", "limit"]
    assert delete.parameters[1].description == "path"
    limit_query = next(p for p in get.parameters if (p.name, p.location) == ("limit", "query"))
    assert (limit_query.description, limit_query.required) == ("op", True)
    assert limit_query.schema_definition == {"type": "string"}
    # Same name but different location is a different parameter.
    assert ("limit", "header") in {(p.name, p.location) for p in get.parameters}
    assert len(get.parameters) == 3


def test_put_patch_delete_endpoints_together() -> None:
    item = {"put": {"summary": "p"}, "patch": {"summary": "pa"}, "delete": {"summary": "d"}}
    parsed = parse_openapi(spec3({"/a": item}))
    assert {e.method: e.summary for e in parsed.endpoints} == {
        "PUT": "p",
        "PATCH": "pa",
        "DELETE": "d",
    }


def test_multiple_responses_default_and_non_numeric_codes() -> None:
    responses = {
        "200": {
            "description": "ok",
            "content": {"application/json": {"schema": {"type": "string"}}},
        },
        "2XX": {"description": "range"},
        "404": {"description": "missing"},
        "default": {
            "description": "error",
            "content": {"application/json": {"schema": ref("Err")}},
        },
        "x-extension": {"ignored": True},
    }
    endpoint = parse_one(
        spec3({"/a": {"get": {"responses": responses}}}, {"Err": {"type": "object"}})
    )
    assert [r.status_code for r in endpoint.responses] == ["200", "2XX", "404", "default"]
    assert response_schema(endpoint, "default") == {"type": "object"}
    assert endpoint.responses[1].content == []
    assert endpoint.responses[2].description == "missing"


def test_response_with_multiple_content_types() -> None:
    content = {
        "application/json": {"schema": {"type": "object"}},
        "application/xml": {"schema": {"type": "string"}},
    }
    endpoint = parse_one(
        spec3({"/a": {"get": {"responses": {"200": {"description": "x", "content": content}}}}})
    )
    assert [(c.media_type, c.schema_definition) for c in endpoint.responses[0].content] == [
        ("application/json", {"type": "object"}),
        ("application/xml", {"type": "string"}),
    ]


def test_missing_optional_fields() -> None:
    endpoint = parse_one(spec3({"/a": {"get": {}}}))
    assert endpoint.summary is None
    assert endpoint.description is None
    assert endpoint.operation_id is None
    assert endpoint.tags == []
    assert endpoint.parameters == []
    assert endpoint.request_body is None
    assert endpoint.responses == []


def test_multiple_paths_and_methods() -> None:
    paths: dict[str, Any] = {
        "/a": {"get": {}, "post": {}},
        "/b": {"get": {}},
        "/c/{id}": {"delete": {}, "put": {}},
    }
    parsed = parse_openapi(spec3(paths))
    assert {(e.method, e.path) for e in parsed.endpoints} == {
        ("GET", "/a"),
        ("POST", "/a"),
        ("GET", "/b"),
        ("DELETE", "/c/{id}"),
        ("PUT", "/c/{id}"),
    }


def test_path_item_reference_and_external_path_item_reference() -> None:
    paths = {
        "/a": {"$ref": "#/components/pathItems/Shared"},
        "/b": {"$ref": "other.yaml#/paths/~1b"},
    }
    text = spec3(
        paths, version="3.1.0", components={"pathItems": {"Shared": {"get": {"summary": "shared"}}}}
    )
    parsed = parse_openapi(text)
    assert [(e.path, e.summary) for e in parsed.endpoints] == [("/a", "shared")]


@pytest.mark.parametrize(
    "paths",
    [
        {"/a": "nope"},
        {"/a": {"get": "nope"}},
        {"/a": {"get": {"parameters": "nope"}}},
        {"/a": {"get": {"parameters": [{"in": "query"}]}}},
        {"/a": {"get": {"parameters": ["nope"]}}},
        {"/a": {"get": {"responses": ["nope"]}}},
        {"/a": {"get": {"responses": {"200": "nope"}}}},
        {"/a": {"post": {"requestBody": []}}},
        {"/a": {"get": {"parameters": [{"name": "p", "in": "nowhere"}]}}},
    ],
)
def test_malformed_operations_are_rejected(paths: dict[str, Any]) -> None:
    assert error_code(spec3(paths)) in {"INVALID_SPEC_STRUCTURE"}


def test_non_string_scalars_become_none() -> None:
    endpoint = parse_one(spec3({"/a": {"get": {"summary": 5, "description": ["x"], "tags": "x"}}}))
    assert (endpoint.summary, endpoint.description, endpoint.tags) == (None, None, [])


# ------------------------------------------------------------------- OpenAPI 2.0 specifics


def swagger(paths: dict[str, Any], **extra: Any) -> str:
    return dump({"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": paths, **extra})


def test_swagger2_body_parameter_and_consumes() -> None:
    body = {
        "in": "body",
        "name": "body",
        "required": True,
        "description": "d",
        "schema": {"type": "object"},
    }
    path_item = {"post": {"consumes": ["application/xml"], "parameters": [body]}}
    endpoint = parse_one(swagger({"/a": path_item}, consumes=["application/json"]))
    assert endpoint.parameters == []
    request_body = endpoint.request_body
    assert request_body is not None
    assert (request_body.required, request_body.description) == (True, "d")
    assert [(c.media_type, c.schema_definition) for c in request_body.content] == [
        ("application/xml", {"type": "object"})
    ]


def test_swagger2_consumes_falls_back_to_document_then_json() -> None:
    body = {"in": "body", "name": "b", "schema": {"type": "string"}}
    item = {"post": {"parameters": [body]}}
    for extra, expected in (({"consumes": ["text/plain"]}, "text/plain"), ({}, "application/json")):
        request_body = parse_one(swagger({"/a": item}, **extra)).request_body
        assert request_body is not None
        assert request_body.content[0].media_type == expected


def test_swagger2_path_level_body_parameter_overridden_by_operation() -> None:
    path_item = {
        "parameters": [{"in": "body", "name": "body", "schema": {"type": "string"}}],
        "post": {"parameters": [{"in": "body", "name": "body", "schema": {"type": "integer"}}]},
        "put": {},
    }
    post, put = parse_openapi(swagger({"/a": path_item})).endpoints[0:2]
    assert post.request_body is not None
    assert put.request_body is not None
    assert post.request_body.content[0].schema_definition == {"type": "integer"}
    assert put.request_body.content[0].schema_definition == {"type": "string"}


def test_swagger2_form_data_parameters() -> None:
    params = [
        {"name": "note", "in": "formData", "type": "string", "description": "n"},
        {"name": "file", "in": "formData", "type": "file", "required": True},
    ]
    endpoint = parse_one(swagger({"/a": {"post": {"parameters": params}}}))
    assert endpoint.parameters == []
    request_body = endpoint.request_body
    assert request_body is not None
    assert request_body.required is True
    assert request_body.content[0].media_type == "multipart/form-data"
    assert request_body.content[0].schema_definition == {
        "type": "object",
        "properties": {"note": {"type": "string", "description": "n"}, "file": {"type": "file"}},
        "required": ["file"],
    }


def test_swagger2_form_data_without_file_defaults_to_urlencoded() -> None:
    params = [{"name": "a", "in": "formData", "type": "string"}]
    endpoint = parse_one(
        swagger({"/a": {"post": {"parameters": params, "consumes": ["application/json"]}}})
    )
    assert endpoint.request_body is not None
    assert endpoint.request_body.required is False
    assert endpoint.request_body.content[0].media_type == "application/x-www-form-urlencoded"


def test_swagger2_non_body_parameters_get_inline_schema() -> None:
    params = [
        {
            "name": "ids",
            "in": "query",
            "type": "array",
            "items": {"type": "integer"},
            "collectionFormat": "csv",
        },
        {"name": "id", "in": "path", "required": True, "type": "string", "format": "uuid"},
        {"name": "X-T", "in": "header", "type": "string", "enum": ["a", "b"], "default": "a"},
    ]
    endpoint = parse_one(swagger({"/a/{id}": {"get": {"parameters": params}}}))
    schemas = {p.name: p.schema_definition for p in endpoint.parameters}
    assert schemas["ids"] == {"type": "array", "items": {"type": "integer"}}
    assert schemas["id"] == {"type": "string", "format": "uuid"}
    assert schemas["X-T"] == {"type": "string", "enum": ["a", "b"], "default": "a"}


def test_swagger2_response_schema_uses_produces() -> None:
    responses = {
        "200": {"description": "ok", "schema": {"type": "string"}},
        "204": {"description": "none"},
        "default": {"description": "err", "schema": {"type": "object"}},
    }
    item = {"get": {"produces": ["text/plain"], "responses": responses}}
    endpoint = parse_one(swagger({"/a": item}, produces=["application/json"]))
    assert [(c.media_type, c.schema_definition) for c in endpoint.responses[0].content] == [
        ("text/plain", {"type": "string"})
    ]
    assert endpoint.responses[1].content == []
    assert endpoint.responses[2].status_code == "default"


def test_swagger2_definitions_parameters_and_responses_refs() -> None:
    document = {
        "parameters": {"Id": {"name": "id", "in": "query", "type": "integer"}},
        "responses": {"Err": {"description": "bad", "schema": {"$ref": "#/definitions/Err"}}},
        "definitions": {
            "Err": {"type": "object", "properties": {"self": {"$ref": "#/definitions/Err"}}}
        },
    }
    item = {
        "get": {
            "parameters": [{"$ref": "#/parameters/Id"}],
            "responses": {"400": {"$ref": "#/responses/Err"}},
        }
    }
    parsed = parse_openapi(swagger({"/a": item}, **document))
    endpoint = parsed.endpoints[0]
    assert endpoint.parameters[0].schema_definition == {"type": "integer"}
    assert response_schema(endpoint, "400")["properties"]["self"] == {"$ref": "#/definitions/Err"}
    assert "#/definitions/Err" in parsed.recursive_schemas


# ------------------------------------------------------------------------------ real-world samples


def load_fixture(name: str) -> ParsedSpecification:
    return parse_openapi((FIXTURES / name).read_bytes())


def test_petstore_openapi_3() -> None:
    parsed = load_fixture("petstore_3_0.yaml")
    assert parsed.spec_version == "3.0"
    assert {(e.method, e.path) for e in parsed.endpoints} == {
        ("PUT", "/pet"),
        ("POST", "/pet"),
        ("GET", "/pet/findByStatus"),
        ("GET", "/pet/{petId}"),
        ("DELETE", "/pet/{petId}"),
    }
    by_key = {(e.method, e.path): e for e in parsed.endpoints}

    put = by_key["PUT", "/pet"]
    assert put.operation_id == "updatePet"
    assert put.request_body is not None
    assert [c.media_type for c in put.request_body.content] == [
        "application/json",
        "application/xml",
    ]
    pet = put.request_body.content[0].schema_definition
    assert isinstance(pet, dict)
    assert pet["required"] == ["name", "photoUrls"]
    assert pet["properties"]["category"]["properties"]["name"] == {"type": "string"}
    assert pet["properties"]["tags"]["items"]["properties"]["id"]["format"] == "int64"
    assert pet["properties"]["id"]["example"] == 10

    find = by_key["GET", "/pet/findByStatus"]
    assert find.parameters[0].schema_definition == {
        "type": "string",
        "default": "available",
        "enum": ["available", "pending", "sold"],
    }

    get = by_key["GET", "/pet/{petId}"]
    assert [(p.name, p.location, p.required) for p in get.parameters] == [("petId", "path", True)]
    assert response_schema(get, "404")["properties"]["message"] == {"type": "string"}

    delete = by_key["DELETE", "/pet/{petId}"]
    assert {(p.name, p.location) for p in delete.parameters} == {
        ("petId", "path"),
        ("api_key", "header"),
    }
    assert parsed.recursive_schemas == {}


def test_petstore_swagger_2() -> None:
    parsed = load_fixture("petstore_2_0.json")
    assert parsed.spec_version == "2.0"
    by_key = {(e.method, e.path): e for e in parsed.endpoints}
    post = by_key["POST", "/pet"]
    assert post.request_body is not None
    assert [c.media_type for c in post.request_body.content] == [
        "application/json",
        "application/xml",
    ]
    upload = by_key["POST", "/pet/{petId}/uploadImage"]
    assert [p.name for p in upload.parameters] == ["petId"]
    assert upload.request_body is not None
    assert upload.request_body.content[0].media_type == "multipart/form-data"
    assert upload.request_body.required is True
    assert remaining_refs([r.model_dump() for e in parsed.endpoints for r in e.responses]) == set()


def test_stripe_sample_resolves_nested_and_cyclic_references() -> None:
    parsed = load_fixture("stripe_sample.yaml")
    by_key = {(e.method, e.path): e for e in parsed.endpoints}
    assert set(by_key) == {
        ("GET", "/v1/customers"),
        ("POST", "/v1/customers"),
        ("GET", "/v1/customers/{customer}"),
        ("GET", "/v1/charges/{charge}"),
    }

    listing = by_key["GET", "/v1/customers"]
    assert listing.parameters[1].schema_definition == {
        "type": "array",
        "items": {"type": "string", "maxLength": 5000},
    }
    data_schema = response_schema(listing)["properties"]["data"]["items"]
    assert data_schema["properties"]["address"]["anyOf"][0]["properties"]["city"] == {
        "nullable": True,
        "type": "string",
    }
    # customer -> default_source -> payment_source -> card -> customer is a cycle: the way
    # back into the group is preserved rather than expanded.
    assert data_schema["properties"]["default_source"]["anyOf"][1] == {
        "$ref": f"{SCHEMAS}/payment_source"
    }

    create = by_key["POST", "/v1/customers"]
    assert create.request_body is not None
    assert create.request_body.content[0].media_type == "application/x-www-form-urlencoded"
    assert [r.status_code for r in create.responses] == ["200", "default"]

    charge = response_schema(by_key["GET", "/v1/charges/{charge}"])
    assert charge["properties"]["billing_details"]["properties"]["address"]["properties"]["city"]
    # charge <-> payment_intent is a second cycle; customer lies outside it and is inlined.
    assert charge["properties"]["payment_intent"]["anyOf"][1] == {
        "$ref": f"{SCHEMAS}/payment_intent"
    }
    assert charge["properties"]["customer"]["anyOf"][1]["properties"]["id"] == {"type": "string"}

    # Error schema is shared by every endpoint and is itself part of a cycle via payment_intent.
    error = response_schema(listing, "default")
    payment_intent = error["properties"]["error"]["properties"]["payment_intent"]
    assert payment_intent["properties"]["latest_charge"]["anyOf"][1] == {
        "$ref": f"{SCHEMAS}/charge"
    }
    assert set(parsed.recursive_schemas) == {
        f"{SCHEMAS}/{name}"
        for name in ("customer", "payment_source", "card", "charge", "payment_intent")
    }

    # Every leftover $ref points at a schema exposed in recursive_schemas.
    leftovers: set[str] = set()
    for endpoint in parsed.endpoints:
        leftovers |= remaining_refs(endpoint.model_dump())
    assert leftovers
    assert leftovers <= set(parsed.recursive_schemas)
    for target in parsed.recursive_schemas.values():
        assert remaining_refs(target) <= set(parsed.recursive_schemas)


def test_stripe_sample_extension_content_is_left_untouched() -> None:
    parsed = load_fixture("stripe_sample.yaml")
    customer = parsed.recursive_schemas[f"{SCHEMAS}/customer"]
    assert customer["x-resourceId"] == "customer"
    assert customer["properties"]["default_source"]["x-expansionResources"] == {
        "oneOf": [{"$ref": f"{SCHEMAS}/payment_source"}]
    }


def test_parsing_is_repeatable() -> None:
    first = load_fixture("stripe_sample.yaml")
    second = load_fixture("stripe_sample.yaml")
    assert first == second
