"""Models for endpoints extracted from OpenAPI / Swagger documents (TASK-202).

These are the parser's output contract. Schemas are kept as plain JSON-Schema mappings
with internal ``$ref`` pointers already dereferenced (see ``app.ingestion.openapi_parser``);
the mappings may share sub-objects and must be treated as read-only.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"]
ParameterLocation = Literal["path", "query", "header", "cookie"]
SpecVersion = Literal["2.0", "3.0", "3.1"]
# A JSON Schema is normally a mapping; OpenAPI 3.1 (JSON Schema 2020-12) also allows booleans.
SchemaDefinition = dict[str, Any] | bool


class _EndpointModel(BaseModel):
    model_config = ConfigDict(strict=True, from_attributes=True, frozen=True)


class Parameter(_EndpointModel):
    """A path, query, header or cookie parameter of an operation."""

    name: str = Field(description="Parameter name.")
    location: ParameterLocation = Field(description="Where the parameter is sent ('in').")
    required: bool = Field(description="Whether the parameter is mandatory (always true for path).")
    description: str | None = Field(default=None, description="Human-readable description.")
    schema_definition: SchemaDefinition | None = Field(
        default=None, description="Dereferenced JSON Schema of the parameter value."
    )


class ContentSchema(_EndpointModel):
    """A schema associated with one media type of a request body or response."""

    media_type: str = Field(description="Media type, e.g. 'application/json'.")
    schema_definition: SchemaDefinition | None = Field(
        default=None, description="Dereferenced JSON Schema, or None when none is declared."
    )


class RequestBody(_EndpointModel):
    """The request body of an operation."""

    description: str | None = Field(default=None, description="Human-readable description.")
    required: bool = Field(default=False, description="Whether a request body is mandatory.")
    content: list[ContentSchema] = Field(
        default_factory=list, description="Accepted media types and their schemas."
    )


class Response(_EndpointModel):
    """One response of an operation."""

    status_code: str = Field(
        description="HTTP status code, range ('2XX') or 'default'. Not necessarily numeric."
    )
    description: str | None = Field(default=None, description="Human-readable description.")
    content: list[ContentSchema] = Field(
        default_factory=list, description="Produced media types and their schemas."
    )


class Endpoint(_EndpointModel):
    """A single HTTP operation (method + route path) of an API."""

    method: HttpMethod = Field(description="Upper-case HTTP method.")
    path: str = Field(description="Route path template, e.g. '/pets/{petId}'.")
    summary: str | None = Field(default=None, description="Short summary.")
    description: str | None = Field(default=None, description="Longer description.")
    operation_id: str | None = Field(default=None, description="Unique operation identifier.")
    tags: list[str] = Field(default_factory=list, description="Grouping tags.")
    parameters: list[Parameter] = Field(
        default_factory=list, description="Merged path-level and operation-level parameters."
    )
    request_body: RequestBody | None = Field(default=None, description="Request body, if any.")
    responses: list[Response] = Field(default_factory=list, description="Declared responses.")


class ParsedSpecification(_EndpointModel):
    """Result of parsing an OpenAPI document."""

    spec_version: SpecVersion = Field(description="Detected specification family.")
    endpoints: list[Endpoint] = Field(default_factory=list, description="Extracted operations.")
    recursive_schemas: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Targets of the '$ref' pointers left in place because of recursion, keyed by the "
            "canonical pointer. Each value is dereferenced except for its own recursive refs."
        ),
    )
