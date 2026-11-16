# DocuMind AI — Product Requirements Document (PRD)

**Document Version:** 1.0.0  
**Status:** Approved / Foundational  
**Author:** Anmol Vishvas (anmol23vishvas@gmail.com)  
**Target Repository:** [https://github.com/anmolvishvas/DocuMind-AI](https://github.com/anmolvishvas/DocuMind-AI)  
**Default Branch:** `main`  
**Last Updated:** November 2026  

---

## 1. Executive Summary & Product Vision

### 1.1 Product Overview
**DocuMind AI** is an enterprise-ready, AI-powered API documentation intelligence platform that enables developers, architects, and technical teams to explore, search, understand, troubleshoot, and interact with complex API ecosystems using natural language. 

Traditional API documentation platforms (e.g., Swagger UI, Redoc, ReadMe, static PDF guides) present static, human-linear representations of APIs. When APIs scale to hundreds of endpoints with deeply nested schemas, polymorphic data models, dynamic headers, and cryptic error conditions, developers waste hours reading reference specs, cross-referencing schema definitions, and constructing sample payloads through trial and error.

DocuMind AI transforms static API specifications and documentation into an **active, contextual, and grounded knowledge graph** powered by Large Language Models (LLMs), Retrieval-Augmented Generation (RAG) with `pgvector`, and the open Model Context Protocol (MCP).

### 1.2 One-Line Pitch
> *An AI-powered API documentation intelligence platform that enables developers to understand, explore, search, troubleshoot, and interact with API documentation using natural language.*

### 1.3 Core Value Proposition
- **Zero-Friction Ingestion:** Ingest OpenAPI v2.0/v3.0/v3.1 (JSON/YAML), unstructured PDF technical guides, Markdown developer docs, and plain text.
- **Endpoint-Aware Semantic RAG:** Discards naive text-chunking in favor of AST-level and endpoint-aware normalization that preserves route hierarchies, parameters, and schemas.
- **Hallucination-Mitigated Assistance:** Enforces citation-grounded Q&A with strict context boundaries, refusing to invent parameters or non-existent endpoints.
- **Bi-Directional Interface:** Web-based developer dashboard with an interactive API explorer and a headless Model Context Protocol (MCP) server that empowers external IDEs and agents (e.g., Claude Desktop, Cursor) to directly query the API knowledge base.

---

## 2. Problem Statement & Market Analysis

### 2.1 The Developer Documentation Bottleneck
Modern software engineering depends on third-party SaaS APIs, internal microservice fabrics, and partner platforms. Despite the proliferation of OpenAPI specifications, developers face severe friction points:

1. **Information Fragmentation:** Documentation is split across OpenAPI JSON files, Postman collections, PDF migration guides, and internal Confluence/Markdown pages.
2. **Schema Complexity:** Navigating polymorphic JSON schemas (`oneOf`, `anyOf`, `$ref`) across nested data structures requires manual mental tracing.
3. **Cryptic Error Diagnosis:** When a downstream service returns `422 Unprocessable Entity` or `400 Bad Request`, developers must correlate opaque field paths with buried documentation footnotes.
4. **Context Switching:** Developers constantly toggle between IDEs, terminal shells, Postman, and browser tabs to check header formats and authentication flows.
5. **Static Search Deficiencies:** Keyword-based documentation search fails when queries do not match exact wording (e.g., searching "how to charge a customer card" fails on an endpoint documented strictly as `POST /v1/payment_intents`).

---

## 3. Product Objectives, Goals & Non-Goals

### 3.1 Objectives
- Establish a single source of truth for all API reference documentation within a workspace.
- Reduce developer time-to-first-successful-call (TTFSC) by at least 60%.
- Deliver deterministic, verifiable technical answers with line-level or schema-path citations.
- Provide seamless developer tooling integration via the Model Context Protocol (MCP).

### 3.2 Explicit Goals
- **G-1:** Ingest and parse valid OpenAPI 3.0/3.1 and Swagger 2.0 specifications without schema loss.
- **G-2:** Implement an Endpoint-Aware Chunking Engine that preserves HTTP method, path, parameter constraints, and request/response models.
- **G-3:** Store vector embeddings using PostgreSQL and `pgvector` with HNSW indexing for sub-second retrieval.
- **G-4:** Expose a FastAPI REST API backed by Pydantic v2 validation and SQLAlchemy 2.0 async ORM.
- **G-5:** Implement an interactive React/TypeScript/Tailwind CSS frontend with an API Explorer, Schema Inspector, and Chat Assistant.
- **G-6:** Implement an official MCP server exposing 7 discrete developer tools to external AI clients.
- **G-7:** Provide 100% containerized execution via Docker and Docker Compose with health checks and persistent storage.

### 3.3 Explicit Non-Goals
- **NG-1 (Not an API Gateway or Proxy):** DocuMind AI does not proxy live production traffic, route user requests to third-party endpoints, or handle runtime rate-limiting.
- **NG-2 (Not an Arbitrary Code Execution Sandbox):** DocuMind AI does not execute generated Python/cURL/JavaScript snippets on the server.
- **NG-3 (Not an API Design Studio):** The platform is optimized for reading, searching, and understanding documentation, not editing or generating new OpenAPI specifications.
- **NG-4 (No Unrestricted Web Crawling):** The MVP will not crawl arbitrary authenticated external web portals; documentation must be explicitly uploaded or imported.

---

## 4. Target Personas & User Journeys

### 4.1 Target Personas

| Persona | Role | Key Pain Points | Core Goals |
| :--- | :--- | :--- | :--- |
| **Alex Rivera** | Senior Full-Stack Engineer | Integrates payment and identity SaaS APIs; loses hours deciphering webhook signatures and error codes. | Rapidly generate copy-pasteable, type-safe integration code; resolve integration errors instantly. |
| **Priya Patel** | Backend Platform Lead | Manages 40+ internal microservice specifications; struggles with cross-team API discovery and onboarding. | Centralized searchable API catalog across internal services; reduced onboarding support tickets. |
| **Marcus Vance** | AI Systems Architect | Builds autonomous agent pipelines; needs reliable machine-readable API context for tool execution. | Standardized MCP tool endpoints to provide live documentation context to LLM coding agents. |
| **Elena Rostova** | Technical Writer / DevRel | Maintains public developer portals; needs to identify undocumented endpoints, missing descriptions, or broken examples. | Evaluate documentation quality, discover coverage gaps, and test real-world developer queries. |

### 4.2 Primary User Journeys

#### Journey 1: Ingesting a Complex OpenAPI Spec and Querying via Web UI
```
[User] -> [Upload OpenAPI YAML] -> [DocuMind Parsing & Normalization]
      -> [Endpoint-Aware Chunking] -> [Vector Embedding Generation]
      -> [Indexed in pgvector] -> [User asks: "How do I implement pagination?"]
      -> [Hybrid Semantic Retrieval] -> [Contextual Prompt Grounding]
      -> [Grounded Response with Schema Citations & cURL/Python Snippets]
```

#### Journey 2: Diagnosing an HTTP 422 Error via Natural Language
```
[User] -> [Inputs error response payload and endpoint context]
      -> [Retriever locates Endpoint Schema + Error Definitions]
      -> [LLM compares payload against JSON Schema validation rules]
      -> [Explains missing mandatory field 'idempotency_key' with reference]
```

#### Journey 3: Autonomous IDE Integration via Model Context Protocol (MCP)
```
[Cursor / Claude Desktop] -> [Calls MCP tool 'get_endpoint_details']
                         -> [DocuMind MCP Server validates input]
                         -> [Executes domain service query]
                         -> [Returns structured JSON endpoint contract]
                         -> [External AI generates precise client code]
```

---

## 5. Functional Requirements (FR)

### FR-1: Documentation Ingestion Subsystem
- **FR-1.1:** Support file uploads in `.json` (OpenAPI/Swagger), `.yaml`/`.yml` (OpenAPI), `.pdf` (developer manuals), `.md` (Markdown docs), and `.txt`.
- **FR-1.2:** Enforce file validation (MIME-type check, magic byte verification, 20 MB size limit).
- **FR-1.3:** Isolate ingested documentation under distinct `Projects` to guarantee workspace separation.
- **FR-1.4:** Provide asynchronous ingestion tracking with explicit status progression: `PENDING`, `PARSING`, `CHUNKING`, `INDEXING`, `READY`, or `FAILED`.

### FR-2: OpenAPI & Specification Parsing
- **FR-2.1:** Detect and parse OpenAPI 3.0.x, 3.1.x, and Swagger 2.0 specifications.
- **FR-2.2:** Resolve internal `$ref` schema pointers within the document scope.
- **FR-2.3:** Extract and normalize endpoint primitives: HTTP Method, Route Path, Summary, Description, Tags, Path/Query/Header Parameters, Request Body (MIME types, Schema), Response Codes (2xx, 4xx, 5xx with Schemas), and Security Schemes (Bearer, OAuth2, APIKey, Basic).
- **FR-2.4:** Support unstructured document parsing (PDF text extraction, Markdown heading hierarchies).

### FR-3: Semantic & Endpoint-Aware Chunking Engine
- **FR-3.1:** Implement **Endpoint-Aware Chunking** for structured specs: each endpoint constitutes an autonomous, semantically cohesive chunk containing its method, path, description, request schema, and response schemas.
- **FR-3.2:** For large endpoints exceeding context token limits, perform hierarchical sub-chunking:
  - *Summary & Parameters Chunk*
  - *Request Body Schema Chunk*
  - *Response & Error Code Chunk*
- **FR-3.3:** For unstructured documents (PDF/Markdown), implement recursive token chunking with a 512-token target size and 64-token overlap, preserving header breadcrumbs.
- **FR-3.4:** Attach mandatory structured metadata to every chunk: `project_id`, `document_id`, `chunk_id`, `chunk_type` (`endpoint`, `schema`, `narrative`), `http_method`, `route_path`, and `source_filename`.

### FR-4: Vector Indexing & Hybrid Retrieval Subsystem
- **FR-4.1:** Generate dense vector embeddings using OpenAI `text-embedding-3-small` (1536 dimensions) with batching and retry logic.
- **FR-4.2:** Store vector representations inside PostgreSQL using `pgvector` with HNSW (`vector_cosine_ops`) indexing.
- **FR-4.3:** Support cosine distance similarity search with configurable relevance score thresholds (minimum similarity cutoff = 0.70).
- **FR-4.4:** Support metadata-filtered vector queries (filter by `project_id`, `http_method`, `tags`, or `route_path`).

### FR-5: LLM Question Answering & Generation
- **FR-5.1:** Multi-turn conversational interface backed by OpenAI `gpt-4o` / `gpt-4o-mini`.
- **FR-5.2:** Construct grounded RAG prompts injecting top-$k$ retrieved chunks with explicit XML boundary fencing (`<api_context>...</api_context>`).
- **FR-5.3:** Enforce strict negative refusal: if the retrieved context does not contain sufficient details to answer the query, the assistant must explicitly state that the documentation does not provide that information.
- **FR-5.4:** Generate verifiable source citations specifying the exact document name, endpoint (`GET /api/v1/orders`), and schema section.
- **FR-5.5:** Generate syntactically valid code snippets in Python (`requests` / `httpx`), JavaScript (`fetch` / `axios`), and cURL based directly on the documented parameters and schemas.
- **FR-5.6:** Support Server-Sent Events (SSE) for streaming LLM responses to the frontend.

### FR-6: API Explorer & Schema Viewer
- **FR-6.1:** Display a searchable, filterable catalog of all extracted endpoints grouped by tags or resources.
- **FR-6.2:** Color-coded HTTP method badges (GET: Emerald, POST: Blue, PUT: Amber, PATCH: Orange, DELETE: Rose).
- **FR-6.3:** Interactive JSON schema tree viewer supporting nested objects, property types, required fields, and enum constraints.
- **FR-6.4:** Instant cURL command generator pre-populated with endpoint paths and sample payload skeletons.

### FR-7: Model Context Protocol (MCP) Server
- **FR-7.1:** Implement a standalone MCP server using the official MCP Python SDK (`mcp`).
- **FR-7.2:** Support `stdio` transport for desktop IDE integration (e.g., Claude Desktop, Cursor) and `SSE` transport for remote agent networks.
- **FR-7.3:** Register and expose 7 foundational MCP tools:
  1. `search_api_docs`: Natural language semantic search across documentation chunks.
  2. `list_api_endpoints`: Filter and list endpoints by method, tag, or path keyword.
  3. `get_endpoint_details`: Retrieve full parameter, header, and auth contract for a specific route.
  4. `get_request_schema`: Extract exact request body JSON schema for a target endpoint.
  5. `get_response_schema`: Extract response schema and status code mappings.
  6. `generate_code_example`: Generate client code examples in target languages.
  7. `explain_api_error`: Diagnose an error code or payload against documented error responses.
- **FR-7.4:** Return structured, machine-parsable JSON payloads from all tool executions.

### FR-8: User, Project & Workspace Management
- **FR-8.1:** Support multi-project organization allowing users to partition separate API ecosystems.
- **FR-8.2:** Cascade deletion: deleting a project purges its associated documents, endpoints, chunks, and vector embeddings.

---

## 6. Non-Functional Requirements (NFR)

### NFR-1: Performance & Latency
- **NFR-1.1:** Vector similarity search execution latency must be under 150 ms for an index of up to 50,000 chunks.
- **NFR-1.2:** End-to-end LLM first-token latency (TTFT) via SSE streaming must be under 1.5 seconds.
- **NFR-1.3:** OpenAPI specification parsing and endpoint normalization must take under 3.0 seconds for files up to 5 MB.
- **NFR-1.4:** Frontend initial page load (LCP) must be under 1.8 seconds on standard broadband.

### NFR-2: Accuracy & Grounding (RAG Quality)
- **NFR-2.1:** Hallucination rate must not exceed 2% on benchmarked evaluation datasets.
- **NFR-2.2:** Retrieved context precision@5 must exceed 85% on standard API query test sets.
- **NFR-2.3:** 100% of LLM answers claiming endpoint or schema facts must include traceable citation chips linking directly to extracted chunks.

### NFR-3: Scalability & Reliability
- **NFR-3.1:** Stateless backend services capable of horizontal scaling behind a reverse proxy.
- **NFR-3.2:** PostgreSQL database connection pooling using SQLAlchemy asyncpg with configurable pool sizes and overflow limits.
- **NFR-3.3:** Graceful degradation: in case of vector search service degradation, allow fallback to exact endpoint keyword lookup.

### NFR-4: Security & Compliance
- **NFR-4.1:** Zero hardcoded API keys or credentials; all configuration managed via validated environment variables.
- **NFR-4.2:** Protection against Prompt Injection: retrieved document contents must be enclosed in system-level demarcations and treated as untrusted data.
- **NFR-4.3:** File upload protection: strict validation of file extensions, MIME types, and magic bytes; prevention of path traversal attacks using UUID-based on-disk naming.
- **NFR-4.4:** Rate limiting on API endpoints to prevent denial-of-service and LLM quota exhaustion.

### NFR-5: Observability & Logging
- **NFR-5.1:** Structured JSON logging across all backend services with correlation IDs (`X-Correlation-ID`) injected into request headers.
- **NFR-5.2:** LLM token accounting: record prompt tokens, completion tokens, model name, and estimated cost per completion event.
- **NFR-5.3:** Standardized `/health` and `/ready` endpoints reporting database connectivity, pgvector extension availability, and worker state.

### NFR-6: Portability & Containerization
- **NFR-6.1:** Entire application deployable via a single command: `docker compose up --build`.
- **NFR-6.2:** Multi-stage Docker builds producing minimal, non-root user production images.

---

## 7. Scope & Roadmap

```
+---------------------------------------------------------------------------------------+
| PHASE 1: MVP Scope (Release v0.1.0)                                                   |
| - OpenAPI 3.0/3.1 JSON/YAML parsing and schema extraction                             |
| - Markdown & PDF documentation upload                                                 |
| - Endpoint-Aware Semantic Chunking                                                    |
| - pgvector HNSW indexing & OpenAI embeddings                                          |
| - Multi-turn conversational UI with streaming SSE and citations                       |
| - API Endpoint Explorer with color-coded badges and schema viewer                    |
| - Official MCP Server exposing 7 tools via stdio transport                            |
| - Docker Compose local orchestration with healthchecks                                |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
| PHASE 2: Advanced Capabilities (Release v0.2.0 - v0.3.0)                               |
| - Multi-tenant workspace RBAC & API Key authentication                                |
| - Dynamic Postman Collection & GraphQL Schema ingestion                              |
| - Real-time documentation synchronization via GitHub Webhooks                         |
| - Remote MCP SSE transport with OAuth2 token validation                               |
| - Automated API drift detection (alerting when specs change between versions)          |
| - Interactive mock API server sandbox generating synthetic responses                  |
+---------------------------------------------------------------------------------------+
```

### 7.1 MoSCoW Prioritization Matrix

| Category | Requirements Included |
| :--- | :--- |
| **Must Have (MVP)** | OpenAPI 3.0/3.1 parsing, Endpoint chunking, pgvector integration, FastAPI backend, Chat UI with SSE streaming, Schema citations, 7 MCP tools via stdio, Docker Compose. |
| **Should Have** | PDF/Markdown parsing, JSON schema tree viewer, cURL/Python/JS code generator, Error diagnosis mode, Token usage tracking, Full-text keyword + vector hybrid search. |
| **Could Have** | GitHub repo auto-sync, Postman collection importer, Remote MCP SSE transport, Dark/Light theme toggle, Export chat to Markdown. |
| **Won't Have (v1)** | Live API proxy execution, Schema mutation/editing, Custom fine-tuned embedding models, On-premise air-gapped LLM deployment. |

---

## 8. Acceptance Criteria (Structured Tables)

### AC-1: Documentation Upload
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-1.1 | User uploads valid OpenAPI 3.0 YAML (< 20MB) | File saved with UUID name; DB record created with status `PARSING`; 201 Created returned. | API integration test with sample `petstore.yaml`. |
| AC-1.2 | User uploads file with invalid MIME type (e.g. `.exe`) | Request rejected with HTTP 400 Bad Request; descriptive error message returned. | Unit test with binary file payload. |
| AC-1.3 | User uploads file > 20MB | Request rejected with HTTP 413 Payload Too Large before buffering file in memory. | Test with 25MB synthetic file. |

### AC-2: OpenAPI Specification Parsing
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-2.1 | Spec contains internal `$ref` schemas | Parser resolves all `$ref` pointers to root schemas without circular recursion crashes. | Unit test with deeply nested `$ref` schema. |
| AC-2.2 | Spec contains 50 endpoints across 8 tags | Exactly 50 endpoint entities extracted with matching paths, methods, parameters, and schemas. | Automated validation against reference spec. |
| AC-2.3 | Spec has missing optional descriptions | Parser completes successfully, assigning default empty strings without throwing `KeyError`. | Test with minimal OpenAPI 3.0 spec. |

### AC-3: Document Indexing & Chunking
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-3.1 | Endpoint-aware chunking executed on OpenAPI spec | Each endpoint produces at least 1 discrete chunk with embedded route metadata and schema context. | DB query asserting `chunk_type = 'endpoint'` and `route_path IS NOT NULL`. |
| AC-3.2 | PDF document indexed | Document split into chunks <= 512 tokens with 64-token overlap and valid page references. | Chunk count and token length assertion test. |
| AC-3.3 | Document deleted by user | All related chunks and vector embeddings are deleted from PostgreSQL via cascade deletion. | DB assertion: zero orphaned rows in `document_chunks`. |

### AC-4: RAG Retrieval
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-4.1 | Query: "How do I create an order?" | Top 3 retrieved chunks contain `POST /orders` endpoint with cosine similarity > 0.75. | Semantic retrieval evaluation test. |
| AC-4.2 | Query with project filter `project_id = X` | Only chunks belonging to project `X` are retrieved; zero cross-project leakage. | Multi-tenant isolation test. |
| AC-4.3 | Out-of-domain query (e.g., "What is the capital of France?") | All similarity scores fall below the 0.70 threshold; empty context returned. | Benchmark test with irrelevant queries. |

### AC-5: AI Question Answering & Grounding
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-5.1 | User asks for required fields of `POST /users` | LLM lists exact required fields matching the JSON schema; zero hallucinated fields. | Automated ground-truth schema comparison. |
| AC-5.2 | Retrieved context does not contain answer | LLM responds: "The provided documentation does not contain information regarding [topic]." | Negative prompt test suite. |
| AC-5.3 | Streaming response initiated | Frontend receives valid SSE chunks formatted as `data: {"token": "..."}\n\n`. | E2E streaming test using HTTP client. |

### AC-6: Source Citations
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-6.1 | LLM generates answer using 2 distinct endpoints | Response includes citation objects referencing document ID, route, and chunk ID. | Regex and JSON validation of citation metadata. |
| AC-6.2 | User clicks citation badge in UI | UI highlights corresponding endpoint card or opens schema view modal. | Frontend component interaction test. |

### AC-7: API Endpoint Exploration
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-7.1 | User filters by HTTP method `GET` | Explorer lists only `GET` endpoints with green method badge. | Cypress / Playwright UI test. |
| AC-7.2 | User enters search term `/checkout` | Explorer dynamically filters endpoints matching path or description in real time. | Frontend state filtering unit test. |

### AC-8: MCP Tool Execution
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-8.1 | MCP client calls `list_api_endpoints(project_id="...")` | Returns JSON array of endpoints matching schema without runtime exception. | MCP client test using official Python MCP SDK. |
| AC-8.2 | MCP client calls `get_request_schema` for non-existent endpoint | Returns structured error `{ "error": "Endpoint not found", "code": 404 }`. | Error boundary test on MCP server. |
| AC-8.3 | MCP client invokes `explain_api_error(status_code=422)` | Returns documented 422 schema, reasons, and suggested developer resolution. | Contract test against sample error spec. |

### AC-9: Docker Deployment
| ID | Condition | Expected Result | Verification Method |
| :--- | :--- | :--- | :--- |
| AC-9.1 | Operator executes `docker compose up -d` | All containers (`db`, `backend`, `frontend`, `mcp`) start and transition to `healthy`. | `docker compose ps` health check assertion. |
| AC-9.2 | PostgreSQL container restarted | Data persists across container teardown and restart via Docker volume mount. | Database persistence test across restarts. |

---

## 9. Security, Privacy & Data Governance

1. **Authentication & Tenant Isolation:** Each project's data is isolated at the database level by foreign key constraints referencing `project_id`. Vector searches must always include a mandatory `WHERE project_id = :project_id` filter clause.
2. **Untrusted Data Boundary:** All uploaded documentation and extracted text chunks are treated as untrusted user input. When rendered in prompt templates, content is enclosed in `<untrusted_api_documentation>` tags with explicit system instructions prohibiting execution of instruction directives found within docs.
3. **Secret Hygiene:** Database passwords, OpenAI API keys, and JWT secrets must be loaded from system environment variables or `.env` files. No credentials shall ever be written to source code, logs, or MCP tool responses.
4. **Data Retention & Purging:** Hard delete requests for a project trigger transactional cascade deletion across `projects`, `documents`, `endpoints`, `document_chunks`, and the vector index in `pgvector`.

---

## 10. Success Metrics & Measurable KPIs

| Metric | Baseline (Manual Search) | Target (DocuMind AI MVP) | Measurement Mechanism |
| :--- | :--- | :--- | :--- |
| **Time-to-First-Call (TTFSC)** | 45 minutes | < 10 minutes | User onboarding testing & study |
| **RAG Retrieval Precision@5** | N/A | > 88% | Evaluation test suite against ground truth |
| **Hallucination Rate** | N/A | < 2.0% | Synthetically evaluated adversarial test cases |
| **API Exploration Search Latency** | 2-5 seconds | < 150 ms | Backend telemetry (P95 query duration) |
| **MCP Tool Response Time** | N/A | < 400 ms (non-LLM), < 2.5s (LLM) | MCP server request latency logs |
| **Test Branch Coverage** | N/A | >= 90% | Automated CI coverage gate (pytest-cov) |
