"""OpenAPI 2.0 / 3.0 / 3.1 parser, internal ``$ref`` resolver and endpoint extractor (TASK-202).

The parser is synchronous and CPU-bound: async callers must dispatch it with
``starlette.concurrency.run_in_threadpool`` (docs/RULES.md section 3).

Reference resolution
--------------------
Only internal references (``#/...``) are followed; external ones (files, URLs) are rejected
so the parser can never read local files or reach the network.

* Resolution is copy-on-write: the parsed document is never mutated, and sub-trees that
  contain no ``$ref`` are shared with it rather than copied.
* Each target pointer is resolved once and memoised, so a schema used from many places is
  one shared object (a *shared* reference, not a cycle).
* The reference graph is split into strongly connected components (Tarjan). Expanding a
  target inlines everything it references *except* references back into its own component:
  those are *cycles*, are not expanded, and stay as ``{"$ref": "<canonical pointer>"}``. The
  pointer is reported in :attr:`RefResolver.recursive_references` so the relationship is
  preserved and the target can be looked up (``ParsedSpecification.recursive_schemas``).
  Because the result for a target never depends on where it is referenced from, memoising is
  always valid, work stays linear in the document size (naive stack-based expansion is
  exponential on real specs such as Stripe's) and the output is independent of path order.
* ``$ref`` siblings are merged over the resolved target (OpenAPI 3.1 semantics).
* Values under ``example``, ``examples``, ``enum``, ``const``, ``default`` and ``x-*``
  extensions are free-form data, not schemas, and are left untouched.
* Nesting depth and total work are bounded, so hostile documents (very deep nesting, YAML
  alias bombs, exponential reference graphs) fail with a clear error instead of exhausting
  the stack or memory.
"""

import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Final
from urllib.parse import unquote

import yaml
from pydantic import ValidationError

from app.core.exceptions import DocumentParsingError
from app.schemas.endpoint import (
    ContentSchema,
    Endpoint,
    HttpMethod,
    Parameter,
    ParameterLocation,
    ParsedSpecification,
    RequestBody,
    Response,
    SchemaDefinition,
    SpecVersion,
)

logger = logging.getLogger(__name__)

MAX_RESOLVE_DEPTH: Final[int] = 150
MAX_RESOLVE_NODES: Final[int] = 3_000_000

_HTTP_METHODS: Final[tuple[HttpMethod, ...]] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
    "TRACE",
)
_METHOD_BY_KEY: Final[dict[str, HttpMethod]] = {m.lower(): m for m in _HTTP_METHODS}

_OPENAPI_3_VERSION: Final[re.Pattern[str]] = re.compile(r"^3\.([01])(?:\.\d+)?(?:-[\w.]+)?$")
_INVALID_TILDE: Final[re.Pattern[str]] = re.compile(r"~(?![01])")
_MAX_REF_DISPLAY: Final[int] = 120

# Keys whose value is free-form data rather than a schema/object that may hold ``$ref``.
_OPAQUE_KEYS: Final[frozenset[str]] = frozenset({"example", "examples", "enum", "const", "default"})
# Keys whose mapping value is keyed by arbitrary names, so a child called e.g. "default" or
# "$ref" is a name, not a keyword.
_NAME_MAP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "properties",
        "patternProperties",
        "definitions",
        "$defs",
        "dependentSchemas",
        "schemas",
        "responses",
        "paths",
        "content",
        "headers",
        "requestBodies",
        "callbacks",
        "links",
        "securitySchemes",
        "securityDefinitions",
    }
)

_DEFAULT_MEDIA_TYPE: Final[str] = "application/json"
_FORM_URLENCODED: Final[str] = "application/x-www-form-urlencoded"
_FORM_MULTIPART: Final[str] = "multipart/form-data"
_FORM_MEDIA_TYPES: Final[frozenset[str]] = frozenset({_FORM_URLENCODED, _FORM_MULTIPART})
_PARAMETER_LOCATIONS: Final[frozenset[str]] = frozenset({"path", "query", "header", "cookie"})
_KNOWN_PARAMETER_LOCATIONS: Final[frozenset[str]] = _PARAMETER_LOCATIONS | {"body", "formData"}
# JSON-Schema-like keywords carried inline by OpenAPI 2.0 non-body parameters.
_SWAGGER2_SCHEMA_KEYS: Final[tuple[str, ...]] = (
    "type",
    "format",
    "items",
    "enum",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
)

_MISSING: Final[Any] = object()


# ----------------------------------------------------------------------------- loading


def load_openapi_document(content: str | bytes) -> tuple[SpecVersion, dict[str, Any]]:
    """Safely parse JSON/YAML ``content`` and detect the specification version.

    Raises:
        DocumentParsingError: not valid YAML/JSON, root is not a mapping, or the
            specification version is missing/unsupported.
    """
    document = _load_mapping(content)
    return _detect_version(document), document


def _load_mapping(content: str | bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    except UnicodeDecodeError:
        raise DocumentParsingError(
            "Document is not valid UTF-8 text.", "INVALID_ENCODING"
        ) from None
    try:
        loaded = _load_text(text)
    except yaml.YAMLError as exc:
        raise DocumentParsingError(_yaml_error_message(exc), "INVALID_YAML") from None
    except RecursionError:
        raise DocumentParsingError("Document is nested too deeply.", "SPEC_TOO_COMPLEX") from None
    if not isinstance(loaded, dict):
        raise DocumentParsingError(
            "Document root must be a mapping (object).", "INVALID_SPEC_STRUCTURE"
        )
    return loaded


def _load_text(text: str) -> Any:
    # JSON goes through the stdlib parser first: faster, and PyYAML rejects some valid JSON
    # (e.g. tab indentation). Anything else, including flow-style YAML, uses safe_load.
    if text.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return yaml.safe_load(text)


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    """Describe a YAML error by position only; never echo document content."""
    mark = exc.problem_mark if isinstance(exc, yaml.MarkedYAMLError) else None
    if mark is not None:
        return f"Document is not valid YAML/JSON (line {mark.line + 1}, column {mark.column + 1})."
    return "Document is not valid YAML/JSON."


def _detect_version(document: Mapping[str, Any]) -> SpecVersion:
    if "swagger" in document:
        if str(document["swagger"]).strip() == "2.0":
            return "2.0"
        raise DocumentParsingError(
            "Unsupported Swagger version; only 2.0 is supported.", "UNSUPPORTED_SPEC_VERSION"
        )
    declared = document.get("openapi")
    if declared is None:
        raise DocumentParsingError(
            "Missing 'openapi' or 'swagger' version field.", "UNSUPPORTED_SPEC_VERSION"
        )
    match = _OPENAPI_3_VERSION.match(str(declared).strip())
    if match is None:
        raise DocumentParsingError(
            "Unsupported OpenAPI version; supported versions are 2.0, 3.0.x and 3.1.x.",
            "UNSUPPORTED_SPEC_VERSION",
        )
    return "3.0" if match.group(1) == "0" else "3.1"


# ----------------------------------------------------------------------------- $ref


def parse_json_pointer(ref: str) -> tuple[str, ...]:
    """Split an internal reference (``#/a/b~1c``) into unescaped tokens.

    Raises:
        DocumentParsingError: the reference is external or not a valid JSON Pointer.
    """
    if not ref.startswith("#"):
        raise DocumentParsingError(
            f"Only internal references are supported, got '{_display(ref)}'.", "UNSUPPORTED_REF"
        )
    fragment = unquote(ref[1:])
    if not fragment:
        return ()
    if not fragment.startswith("/"):
        raise DocumentParsingError(
            f"Malformed JSON Pointer in reference '{_display(ref)}'.", "INVALID_REF"
        )
    tokens: list[str] = []
    for raw in fragment[1:].split("/"):
        if _INVALID_TILDE.search(raw):
            raise DocumentParsingError(
                f"Malformed escape sequence in reference '{_display(ref)}'.", "INVALID_REF"
            )
        # RFC 6901 order: '~1' first, then '~0'.
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


def _canonical_pointer(tokens: Iterable[str]) -> str:
    return "#" + "".join("/" + t.replace("~", "~0").replace("/", "~1") for t in tokens)


def _display(ref: str) -> str:
    return ref if len(ref) <= _MAX_REF_DISPLAY else ref[:_MAX_REF_DISPLAY] + "..."


def _is_opaque_key(key: object) -> bool:
    return isinstance(key, str) and (key in _OPAQUE_KEYS or key.startswith("x-"))


class RefResolver:
    """Dereferences internal ``$ref`` pointers of one parsed document (see module docstring).

    The resolver never mutates ``document``. Returned structures may share sub-objects with
    the document and with each other, so callers must treat them as read-only.
    """

    def __init__(self, document: Mapping[str, Any]) -> None:
        self._document = document
        self._tokens: dict[str, tuple[str, ...]] = {}  # canonical pointer -> unescaped tokens
        self._cache: dict[str, Any] = {}  # canonical pointer -> resolved target
        self._recursive: dict[str, None] = {}  # insertion-ordered set of cut-off pointers
        self._visited = 0
        # Strongly connected components of the reference graph (Tarjan, computed lazily).
        self._component: dict[str, int] = {}
        self._edges_cache: dict[str, tuple[str, ...]] = {}
        self._tarjan_index: dict[str, int] = {}
        self._tarjan_low: dict[str, int] = {}
        self._group_count = 0

    @property
    def recursive_references(self) -> tuple[str, ...]:
        """Canonical pointers left as ``$ref`` because they are part of a cycle."""
        return tuple(self._recursive)

    def resolve(self, node: Any) -> Any:
        """Return ``node`` with every internal ``$ref`` replaced by its target.

        Raises:
            DocumentParsingError: missing, malformed or external reference, or a document
                that is too deeply nested / too large to resolve safely.
        """
        return self._resolve_node(node, None, 0, names_map=False)

    # -- resolution ---------------------------------------------------------------------

    def _tick(self) -> None:
        self._visited += 1
        if self._visited > MAX_RESOLVE_NODES:
            raise DocumentParsingError("Document is too complex to resolve.", "SPEC_TOO_COMPLEX")

    def _resolve_node(self, node: Any, group: int | None, depth: int, *, names_map: bool) -> Any:
        """Resolve ``node``; ``group`` is the cycle group of the reference being expanded."""
        if not isinstance(node, dict | list):
            return node
        if depth > MAX_RESOLVE_DEPTH:
            raise DocumentParsingError("Document is nested too deeply.", "SPEC_TOO_COMPLEX")
        self._tick()
        if isinstance(node, list):
            return self._resolve_items(node, group, depth)
        if not names_map:
            ref = node.get("$ref")
            if isinstance(ref, str):
                return self._resolve_reference(ref, node, group, depth)
        return self._resolve_entries(node, group, depth, names_map=names_map)

    def _resolve_items(self, items: list[Any], group: int | None, depth: int) -> Any:
        result = [self._resolve_node(item, group, depth + 1, names_map=False) for item in items]
        changed = any(new is not old for new, old in zip(result, items, strict=True))
        return result if changed else items

    def _resolve_entries(
        self, mapping: dict[Any, Any], group: int | None, depth: int, *, names_map: bool
    ) -> Any:
        result: dict[Any, Any] = {}
        changed = False
        for key, item in mapping.items():
            if not names_map and _is_opaque_key(key):
                result[key] = item
                continue
            value = self._resolve_node(
                item, group, depth + 1, names_map=not names_map and key in _NAME_MAP_KEYS
            )
            changed = changed or value is not item
            result[key] = value
        return result if changed else mapping

    def _resolve_reference(
        self, ref: str, node: dict[Any, Any], group: int | None, depth: int
    ) -> Any:
        pointer = self._canonicalize(ref)
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        resolved_siblings: Any = (
            self._resolve_entries(siblings, group, depth, names_map=False) if siblings else {}
        )

        target_group = self._component_of(pointer)
        if target_group == group:
            # Cycle: the target belongs to the recursive group currently being expanded.
            self._recursive.setdefault(pointer)
            target: Any = {"$ref": pointer}
        else:
            cached = self._cache.get(pointer, _MISSING)
            if cached is _MISSING:  # first use; later uses are shared references
                raw = self._lookup(self._tokens[pointer], ref)
                cached = self._resolve_node(raw, target_group, depth + 1, names_map=False)
                self._cache[pointer] = cached
            target = cached

        if siblings and isinstance(target, dict):
            return {**target, **resolved_siblings}
        return target

    def _canonicalize(self, ref: str) -> str:
        tokens = parse_json_pointer(ref)
        pointer = _canonical_pointer(tokens)
        self._tokens.setdefault(pointer, tokens)
        return pointer

    def _lookup(self, tokens: tuple[str, ...], ref: str) -> Any:
        current: Any = self._document
        for token in tokens:
            if isinstance(current, dict):
                if token in current:
                    current = current[token]
                elif token.isdecimal() and int(token) in current:  # YAML integer keys (200:)
                    current = current[int(token)]
                else:
                    raise _ref_not_found(ref)
            elif isinstance(current, list) and token.isdecimal() and token == str(int(token)):
                index = int(token)
                if index >= len(current):
                    raise _ref_not_found(ref)
                current = current[index]
            else:
                raise _ref_not_found(ref)
        return current

    # -- cycle groups (strongly connected components of the reference graph) -----------

    def _component_of(self, pointer: str) -> int:
        if pointer not in self._component:
            self._compute_components(pointer)
        return self._component[pointer]

    def _edges(self, pointer: str) -> tuple[str, ...]:
        """Pointers referenced directly from the target of ``pointer``."""
        edges = self._edges_cache.get(pointer)
        if edges is None:
            raw = self._lookup(self._tokens[pointer], pointer)
            edges = tuple(dict.fromkeys(self._canonicalize(r) for r in self._direct_refs(raw)))
            self._edges_cache[pointer] = edges
        return edges

    def _direct_refs(self, root: Any) -> Iterator[str]:
        """Yield ``$ref`` strings under ``root`` without following them (iterative).

        Mirrors the traversal rules of ``_resolve_node`` so both see the same references.
        """
        pending: list[tuple[Any, bool]] = [(root, False)]
        while pending:
            node, names_map = pending.pop()
            self._tick()
            if isinstance(node, list):
                pending.extend((item, False) for item in node)
            elif isinstance(node, dict):
                ref = None if names_map else node.get("$ref")
                if isinstance(ref, str):
                    yield ref
                for key, item in node.items():
                    if names_map or not _is_opaque_key(key):
                        if not names_map and key == "$ref" and isinstance(ref, str):
                            continue
                        pending.append((item, not names_map and key in _NAME_MAP_KEYS))

    def _compute_components(self, start: str) -> None:
        """Tarjan's algorithm, iterative so reference chains cannot exhaust the stack."""
        index, low = self._tarjan_index, self._tarjan_low
        stack: list[str] = []
        on_stack: set[str] = set()
        work: list[tuple[str, Iterator[str]]] = []

        def visit(node: str) -> None:
            index[node] = low[node] = len(index)
            stack.append(node)
            on_stack.add(node)
            work.append((node, iter(self._edges(node))))

        visit(start)
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child in self._component:
                    continue  # already assigned to a finished group
                if child not in index:
                    visit(child)
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                group_id = self._group_count
                self._group_count += 1
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    self._component[member] = group_id
                    if member == node:
                        break


def _ref_not_found(ref: str) -> DocumentParsingError:
    return DocumentParsingError(f"Reference '{_display(ref)}' does not exist.", "REF_NOT_FOUND")


# ----------------------------------------------------------------------------- extraction


def _structure_error(what: str) -> DocumentParsingError:
    return DocumentParsingError(f"Malformed {what} in OpenAPI document.", "INVALID_SPEC_STRUCTURE")


def _as_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _structure_error(what)
    return value


def _as_list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _structure_error(what)
    return value


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _declared_media_types(
    operation: Mapping[str, Any], document: Mapping[str, Any], key: str
) -> list[str]:
    """OpenAPI 2.0 ``consumes``/``produces``: operation overrides the document-level list."""
    declared = _str_list(operation.get(key)) or _str_list(document.get(key))
    return declared or [_DEFAULT_MEDIA_TYPE]


def _swagger2_schema(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in _SWAGGER2_SCHEMA_KEYS if key in raw}


def _first_media_schema(content: Any) -> SchemaDefinition | None:
    if isinstance(content, dict):
        for media in content.values():
            if isinstance(media, dict) and isinstance(media.get("schema"), dict | bool):
                return media["schema"]  # type: ignore[no-any-return]
    return None


def _schema_value(value: Any) -> SchemaDefinition | None:
    return value if isinstance(value, dict | bool) else None


def _build_parameter(raw: Mapping[str, Any], version: SpecVersion) -> Parameter:
    location: ParameterLocation = raw["in"]
    if version == "2.0":
        schema: SchemaDefinition | None = _swagger2_schema(raw)
    else:
        schema = _schema_value(raw.get("schema")) or _first_media_schema(raw.get("content"))
    return Parameter(
        name=raw["name"],
        location=location,
        required=location == "path" or raw.get("required") is True,
        description=_opt_str(raw.get("description")),
        schema_definition=schema,
    )


def _merge_parameters(path_level: Any, operation_level: Any) -> list[dict[str, Any]]:
    """Merge parameter lists; an operation entry replaces a path entry with the same
    ``(name, in)``."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in (*_as_list(path_level, "parameters"), *_as_list(operation_level, "parameters")):
        param = _as_mapping(raw, "parameter")
        name, location = param.get("name"), param.get("in")
        if (
            not isinstance(name, str)
            or not isinstance(location, str)
            or location not in _KNOWN_PARAMETER_LOCATIONS
        ):
            raise _structure_error("parameter")
        merged[(name, location)] = param
    return list(merged.values())


def _content_schemas(content: Any, what: str) -> list[ContentSchema]:
    if content is None:
        return []
    schemas: list[ContentSchema] = []
    for media_type, media in _as_mapping(content, what).items():
        media_object = _as_mapping(media, what)
        schemas.append(
            ContentSchema(
                media_type=str(media_type),
                schema_definition=_schema_value(media_object.get("schema")),
            )
        )
    return schemas


def _openapi3_request_body(operation: Mapping[str, Any]) -> RequestBody | None:
    raw = operation.get("requestBody")
    if raw is None:
        return None
    body = _as_mapping(raw, "requestBody")
    return RequestBody(
        description=_opt_str(body.get("description")),
        required=body.get("required") is True,
        content=_content_schemas(body.get("content"), "requestBody content"),
    )


def _swagger2_request_body(
    params: list[dict[str, Any]], operation: Mapping[str, Any], document: Mapping[str, Any]
) -> RequestBody | None:
    body_param = next((p for p in params if p["in"] == "body"), None)
    if body_param is not None:
        schema = _schema_value(body_param.get("schema"))
        return RequestBody(
            description=_opt_str(body_param.get("description")),
            required=body_param.get("required") is True,
            content=[
                ContentSchema(media_type=media_type, schema_definition=schema)
                for media_type in _declared_media_types(operation, document, "consumes")
            ],
        )
    form_params = [p for p in params if p["in"] == "formData"]
    if not form_params:
        return None
    properties: dict[str, Any] = {}
    for param in form_params:
        property_schema = _swagger2_schema(param)
        if isinstance(param.get("description"), str):
            property_schema["description"] = param["description"]
        properties[param["name"]] = property_schema
    required_names = [p["name"] for p in form_params if p.get("required") is True]
    schema = {"type": "object", "properties": properties}
    if required_names:
        schema["required"] = required_names
    declared = [
        m for m in _declared_media_types(operation, document, "consumes") if m in _FORM_MEDIA_TYPES
    ]
    has_file = any(p.get("type") == "file" for p in form_params)
    media_types = declared or [_FORM_MULTIPART if has_file else _FORM_URLENCODED]
    return RequestBody(
        required=bool(required_names),
        content=[ContentSchema(media_type=m, schema_definition=schema) for m in media_types],
    )


def _build_responses(
    operation: Mapping[str, Any], document: Mapping[str, Any], version: SpecVersion
) -> list[Response]:
    raw_responses = operation.get("responses")
    if raw_responses is None:
        return []
    responses: list[Response] = []
    for code, raw in _as_mapping(raw_responses, "responses").items():
        if isinstance(code, str) and code.startswith("x-"):
            continue
        response = _as_mapping(raw, "response")
        if version == "2.0":
            schema = _schema_value(response.get("schema"))
            content = (
                [
                    ContentSchema(media_type=media_type, schema_definition=schema)
                    for media_type in _declared_media_types(operation, document, "produces")
                ]
                if schema is not None
                else []
            )
        else:
            content = _content_schemas(response.get("content"), "response content")
        responses.append(
            Response(
                status_code=str(code),
                description=_opt_str(response.get("description")),
                content=content,
            )
        )
    return responses


def _build_endpoint(
    method: HttpMethod,
    path: str,
    path_item: Mapping[str, Any],
    operation: Mapping[str, Any],
    document: Mapping[str, Any],
    version: SpecVersion,
) -> Endpoint:
    raw_params = _merge_parameters(path_item.get("parameters"), operation.get("parameters"))
    if version != "2.0" and any(p["in"] not in _PARAMETER_LOCATIONS for p in raw_params):
        raise _structure_error("parameter location")  # body/formData exist only in 2.0
    request_body = (
        _swagger2_request_body(raw_params, operation, document)
        if version == "2.0"
        else _openapi3_request_body(operation)
    )
    parameters = [
        _build_parameter(p, version) for p in raw_params if p["in"] in _PARAMETER_LOCATIONS
    ]
    return Endpoint(
        method=method,
        path=path,
        summary=_opt_str(operation.get("summary")),
        description=_opt_str(operation.get("description")),
        operation_id=_opt_str(operation.get("operationId")),
        tags=_str_list(operation.get("tags")),
        parameters=parameters,
        request_body=request_body,
        responses=_build_responses(operation, document, version),
    )


def _extract_endpoints(
    document: Mapping[str, Any], version: SpecVersion, resolver: RefResolver
) -> list[Endpoint]:
    if "paths" not in document:
        if version != "3.1":  # 3.1 allows documents with only webhooks/components
            raise _structure_error("document: missing 'paths'")
        return []
    paths = _as_mapping(document["paths"], "paths")
    endpoints: list[Endpoint] = []
    for path, raw_item in paths.items():
        if not isinstance(path, str) or path.startswith("x-"):
            continue
        if (
            isinstance(raw_item, dict)
            and isinstance(raw_item.get("$ref"), str)
            and not raw_item["$ref"].startswith("#")
        ):
            logger.warning("skipping path item with external $ref")
            continue
        path_item = _as_mapping(resolver.resolve(raw_item), "path item")
        for key, method in _METHOD_BY_KEY.items():
            if key not in path_item:
                continue
            operation = _as_mapping(path_item[key], "operation")
            try:
                endpoints.append(
                    _build_endpoint(method, path, path_item, operation, document, version)
                )
            except ValidationError:
                raise _structure_error(f"operation {method} {path}") from None
    return endpoints


def _collect_recursive_schemas(resolver: RefResolver) -> dict[str, dict[str, Any]]:
    """Resolve every pointer that was left as ``$ref`` (this may reveal further ones)."""
    schemas: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(resolver.recursive_references):
        pointer = resolver.recursive_references[index]
        index += 1
        target = resolver.resolve({"$ref": pointer})
        if isinstance(target, dict):
            schemas[pointer] = target
    return schemas


def parse_openapi(content: str | bytes) -> ParsedSpecification:
    """Parse an OpenAPI 2.0/3.0/3.1 document (JSON or YAML) into endpoint models.

    Raises:
        DocumentParsingError: the document is malformed, unsupported, or contains an invalid
            reference. Messages never include document content beyond reference strings.
    """
    version, document = load_openapi_document(content)
    resolver = RefResolver(document)
    try:
        endpoints = _extract_endpoints(document, version, resolver)
    except RecursionError:
        raise DocumentParsingError("Document is nested too deeply.", "SPEC_TOO_COMPLEX") from None
    recursive_schemas = _collect_recursive_schemas(resolver)
    logger.debug(
        "parsed openapi document",
        extra={"spec_version": version, "endpoint_count": len(endpoints)},
    )
    return ParsedSpecification(
        spec_version=version, endpoints=endpoints, recursive_schemas=recursive_schemas
    )
