# DocuMind AI — Phased Implementation Roadmap & Task Matrix

**Document Version:** 1.0.0  
**Status:** Active Execution Plan  
**Technical Lead:** Anmol Vishvas (anmol23vishvas@gmail.com)  
**Target Repository:** [https://github.com/anmolvishvas/DocuMind-AI](https://github.com/anmolvishvas/DocuMind-AI)  
**Default Branch:** `main`  

---

## Overview

This implementation roadmap decomposes the development of **DocuMind AI** into seven sequential, test-driven phases. Each phase contains granular, actionable tasks equipped with dependencies, file paths, concrete acceptance criteria, and corresponding automated tests.

```
+----------------------------------------------------------------------------------------------------+
|                                      EXECUTION ROADMAP                                             |
|                                                                                                    |
|  [PHASE 1] Foundation, Scaffolding & DevOps Baseline ───────────────────────────► (Completed)     |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 2] Documentation Ingestion & OpenAPI AST Parsing ────────────────────────► (Next Focus)    |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 3] Endpoint-Aware RAG Pipeline & pgvector Engine ────────────────────────► (Scheduled)     |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 4] Grounded LLM Assistant & Code Generator ──────────────────────────────► (Scheduled)     |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 5] Model Context Protocol (MCP) Server Suite ────────────────────────────► (Scheduled)     |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 6] Developer Dashboard & React UI Integration ───────────────────────────► (Scheduled)     |
|       │                                                                                            |
|       ▼                                                                                            |
|  [PHASE 7] Production Hardening, CI/CD & Security Audits ────────────────────────► (Scheduled)     |
+----------------------------------------------------------------------------------------------------+
```

---

## Phase 1: Foundation, Scaffolding & DevOps Baseline

- [x] **TASK-101: Repository Monorepo Scaffolding & Tooling Config**
  - **Description:** Initialize repository directory tree, configure Git, `.gitignore`, `.dockerignore`, `README.md`, PR template, and GitHub Actions workflows (`ci.yml`, `pr-audit.yml`).
  - **Dependencies:** None.
  - **Files & Modules:** `.gitignore`, `.dockerignore`, `.github/workflows/ci.yml`, `.github/workflows/pr-audit.yml`, `.github/PULL_REQUEST_TEMPLATE.md`.
  - **Acceptance Criteria:** Directory tree matches architectural blueprint; CI workflow validates YAML syntax and triggers on pull requests.
  - **Corresponding Tests:** CI workflow run and YAML syntax validation.

- [x] **TASK-102: Backend Dependency Specification & Tooling Setup**
  - **Description:** Define Python 3.12 dependencies in `backend/pyproject.toml` including FastAPI, Pydantic v2, SQLAlchemy 2.0, asyncpg, pgvector, OpenAI, mcp, PyPDF, and PyYAML. Configure Ruff and strict mypy.
  - **Dependencies:** TASK-101.
  - **Files & Modules:** `backend/pyproject.toml`.
  - **Acceptance Criteria:** `ruff check backend` and `mypy backend` execute with zero configuration syntax errors.
  - **Corresponding Tests:** `ruff check`, `mypy --version`.

- [x] **TASK-103: Core Application Configuration & Structured Logging**
  - **Description:** Implement `backend/app/core/config.py` using `pydantic-settings.BaseSettings` to load environment variables from `.env`. Implement `backend/app/core/logging.py` for structured JSON logging with correlation IDs.
  - **Dependencies:** TASK-102.
  - **Files & Modules:** `backend/app/core/config.py`, `backend/app/core/logging.py`, `.env.example`.
  - **Acceptance Criteria:** Application loads settings with strict type checking; invalid environment variables cause immediate graceful startup failure with clear diagnostics.
  - **Corresponding Tests:** `tests/unit/test_config.py`.

- [x] **TASK-104: Database Connection Layer & Base Models**
  - **Description:** Implement asynchronous SQLAlchemy 2.0 database engine and session factory in `backend/app/core/database.py`. Define `Base` model with declarative mapping helpers in `backend/app/models/base.py`.
  - **Dependencies:** TASK-103.
  - **Files & Modules:** `backend/app/core/database.py`, `backend/app/models/base.py`.
  - **Acceptance Criteria:** Async database session dependency yields active session; rollback triggers on exceptions.
  - **Corresponding Tests:** `tests/integration/test_database_connection.py`.

- [x] **TASK-105: FastAPI Entrypoint & Health Check Probes**
  - **Description:** Build `backend/app/main.py` configuring CORS, exception handlers, request correlation ID middleware, and mount `/health` and `/ready` probes.
  - **Dependencies:** TASK-104.
  - **Files & Modules:** `backend/app/main.py`, `backend/app/api/v1/health.py`, `backend/app/api/router.py`.
  - **Acceptance Criteria:** `GET /health` returns HTTP 200 `{"status": "ok"}`; `GET /ready` checks DB and pgvector extension status.
  - **Corresponding Tests:** `tests/test_health.py`.

- [x] **TASK-106: Multi-Container Docker Orchestration**
  - **Description:** Author `docker-compose.yml`, `backend/Dockerfile`, and `frontend/Dockerfile`. Configure PostgreSQL 16 container with `pgvector/pgvector:pg16` image and persistent named volumes.
  - **Dependencies:** TASK-105.
  - **Files & Modules:** `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`.
  - **Acceptance Criteria:** `docker compose config` validates cleanly; container services start and report healthy status.
  - **Corresponding Tests:** `docker compose up -d` integration check in CI.

---

## Phase 2: Documentation Ingestion & OpenAPI AST Parsing

- [ ] **TASK-201: File Upload Security & Staging Validator**
  - **Description:** Implement `backend/app/ingestion/validator.py` to validate uploaded documents against file size (<= 20MB), allowed extensions (`.json`, `.yaml`, `.yml`, `.pdf`, `.md`, `.txt`), MIME types, and binary magic bytes. Stage validated files to disk using UUID names.
  - **Dependencies:** TASK-105.
  - **Files & Modules:** `backend/app/ingestion/validator.py`, `backend/app/api/v1/documents.py`.
  - **Acceptance Criteria:** Valid files return staged path and metadata; invalid extensions or oversized files return HTTP 400 / 413 with detailed error payload.
  - **Corresponding Tests:** `tests/unit/test_validator.py`.

- [ ] **TASK-202: OpenAPI 2.0 / 3.0 / 3.1 AST Parser & $ref Resolver**
  - **Description:** Implement `backend/app/ingestion/openapi_parser.py` using `PyYAML` and recursive dictionary traversal. Dereference internal `$ref` pointers (e.g. `#/components/schemas/User`) to make all models self-contained.
  - **Dependencies:** TASK-201.
  - **Files & Modules:** `backend/app/ingestion/openapi_parser.py`, `backend/app/schemas/endpoint.py`.
  - **Acceptance Criteria:** Successfully dereferences complex cyclic and nested `$ref` objects; correctly extracts HTTP Method, Route Path, Summary, Description, Parameters, Request Body, and Response Schemas.
  - **Corresponding Tests:** `tests/unit/test_openapi_parser.py` (tested against Swagger Petstore and Stripe API samples).

- [ ] **TASK-203: Markdown & PDF Text Extraction Subsystem**
  - **Description:** Implement `backend/app/ingestion/markdown_parser.py` to parse Markdown headings and code blocks. Implement `backend/app/ingestion/pdf_parser.py` using `pypdf` running off-thread via `run_in_threadpool`.
  - **Dependencies:** TASK-201.
  - **Files & Modules:** `backend/app/ingestion/markdown_parser.py`, `backend/app/ingestion/pdf_parser.py`.
  - **Acceptance Criteria:** Markdown headings preserved as hierarchical context; PDF text extracted with page numbers without blocking the async event loop.
  - **Corresponding Tests:** `tests/unit/test_unstructured_parsers.py`.

- [ ] **TASK-204: Relational Persistence for Extracted Endpoints**
  - **Description:** Implement models and migrations for `projects`, `documents`, and `endpoints`. Write repository logic to persist parsed endpoints into the relational database under the corresponding `document_id`.
  - **Dependencies:** TASK-202, TASK-104.
  - **Files & Modules:** `backend/app/models/endpoint.py`, `backend/app/models/document.py`, `backend/alembic/versions/*`.
  - **Acceptance Criteria:** Ingested OpenAPI spec populates `endpoints` table with valid JSONB schemas and parameters.
  - **Corresponding Tests:** `tests/integration/test_endpoint_persistence.py`.

---

## Phase 3: RAG Pipeline & pgvector Engine

- [ ] **TASK-301: Endpoint-Aware Semantic Chunker**
  - **Description:** Implement `backend/app/rag/chunking.py`. Synthesize self-contained Markdown chunks for each extracted endpoint containing its route signature, parameters, and dereferenced JSON schemas. Apply recursive token chunking for narrative docs.
  - **Dependencies:** TASK-204.
  - **Files & Modules:** `backend/app/rag/chunking.py`.
  - **Acceptance Criteria:** Every endpoint produces a coherent chunk <= 512 tokens with attached metadata (`http_method`, `route_path`, `project_id`).
  - **Corresponding Tests:** `tests/unit/test_chunking.py`.

- [ ] **TASK-302: OpenAI Vector Embedding Integration**
  - **Description:** Implement `backend/app/rag/embeddings.py` interfacing with OpenAI `text-embedding-3-small`. Support batch processing of up to 64 chunks per request with exponential backoff on HTTP 429 rate limits.
  - **Dependencies:** TASK-301, TASK-103.
  - **Files & Modules:** `backend/app/rag/embeddings.py`.
  - **Acceptance Criteria:** Returns 1536-dimensional floating point vectors for chunk batches; handles rate limits gracefully.
  - **Corresponding Tests:** `tests/unit/test_embeddings.py` (with mocked OpenAI responses).

- [ ] **TASK-303: pgvector Storage & HNSW Indexing**
  - **Description:** Define `DocumentChunk` model with `pgvector.sqlalchemy.Vector(1536)` in `backend/app/models/chunk.py`. Configure HNSW index with `vector_cosine_ops` in Alembic migrations.
  - **Dependencies:** TASK-302, TASK-104.
  - **Files & Modules:** `backend/app/models/chunk.py`, `backend/alembic/versions/*`.
  - **Acceptance Criteria:** Chunks and embeddings are persisted; HNSW index is created and utilized during query execution plans (`EXPLAIN ANALYZE`).
  - **Corresponding Tests:** `tests/integration/test_pgvector_indexing.py`.

- [ ] **TASK-304: Hybrid & Semantic Vector Retriever**
  - **Description:** Implement `backend/app/rag/retriever.py` to query top-$k$ similar chunks using cosine distance (`<=>`), enforcing strict `WHERE project_id = :project_id` tenant filtering and a 0.70 similarity cutoff.
  - **Dependencies:** TASK-303.
  - **Files & Modules:** `backend/app/rag/retriever.py`.
  - **Acceptance Criteria:** Returns top relevant chunks filtered by project; discards results below 0.70 score threshold.
  - **Corresponding Tests:** `tests/integration/test_retriever.py`.

---

## Phase 4: Grounded LLM Assistant & Generation

- [ ] **TASK-401: Grounded Prompt Templates & XML Boundary Fencing**
  - **Description:** Author prompt construction logic in `backend/app/rag/prompts.py`. Inject retrieved chunks into `<api_context>` XML blocks with strict anti-hallucination refusal instructions.
  - **Dependencies:** TASK-304.
  - **Files & Modules:** `backend/app/rag/prompts.py`.
  - **Acceptance Criteria:** Prompt cleanly isolates user query from context; explicitly commands refusal if information is absent.
  - **Corresponding Tests:** `tests/unit/test_prompts.py`.

- [ ] **TASK-402: LLM Service & Streaming Token Generator**
  - **Description:** Implement `backend/app/core/llm.py` and `backend/app/rag/service.py` to execute chat completions via OpenAI `gpt-4o`/`gpt-4o-mini`. Expose async generator yielding Server-Sent Events (SSE) data frames.
  - **Dependencies:** TASK-401.
  - **Files & Modules:** `backend/app/core/llm.py`, `backend/app/rag/service.py`, `backend/app/api/v1/chat.py`.
  - **Acceptance Criteria:** Streams tokens via SSE to client with initial time-to-first-token < 1.5s; calculates and logs token usage.
  - **Corresponding Tests:** `tests/integration/test_chat_service.py`.

- [ ] **TASK-403: Citation Extraction & Reference Mapping**
  - **Description:** Parse LLM output for citation markers and construct verifiable citation objects containing document filename, route path, and chunk ID.
  - **Dependencies:** TASK-402.
  - **Files & Modules:** `backend/app/rag/service.py`, `backend/app/schemas/chat.py`.
  - **Acceptance Criteria:** Completed chat response includes structured citations matching retrieved context chunks.
  - **Corresponding Tests:** `tests/unit/test_citations.py`.

- [ ] **TASK-404: Multilingual Code Example & Error Diagnosis Logic**
  - **Description:** Implement specialized prompt modes for generating idiomatic cURL, Python (`httpx`), and JavaScript (`fetch`) requests, and analyzing HTTP error payloads against documented schemas.
  - **Dependencies:** TASK-402.
  - **Files & Modules:** `backend/app/rag/service.py`.
  - **Acceptance Criteria:** Produces valid, copy-pasteable code examples; diagnoses error payloads accurately.
  - **Corresponding Tests:** `tests/unit/test_code_generation.py`.

---

## Phase 5: Model Context Protocol (MCP) Server Suite

- [ ] **TASK-501: MCP Server Initialization & stdio/SSE Transports**
  - **Description:** Implement `backend/app/mcp/server.py` using the official Python MCP SDK (`mcp`). Initialize server capabilities and expose stdio transport for IDE integration.
  - **Dependencies:** TASK-304, TASK-204.
  - **Files & Modules:** `backend/app/mcp/server.py`.
  - **Acceptance Criteria:** MCP server initializes, passes protocol handshake with Claude Desktop / Cursor, and lists registered tools.
  - **Corresponding Tests:** `tests/integration/test_mcp_handshake.py`.

- [ ] **TASK-502: Core Documentation Search & Listing Tools**
  - **Description:** Register `search_api_docs`, `list_api_endpoints`, and `get_endpoint_details` MCP tools in `backend/app/mcp/tools.py`.
  - **Dependencies:** TASK-501.
  - **Files & Modules:** `backend/app/mcp/tools.py`, `backend/app/schemas/mcp.py`.
  - **Acceptance Criteria:** Tools validate input parameters with Pydantic and return structured JSON summaries.
  - **Corresponding Tests:** `tests/integration/test_mcp_search_tools.py`.

- [ ] **TASK-503: Schema Extraction, Code Generation & Error Analysis Tools**
  - **Description:** Register `get_request_schema`, `get_response_schema`, `generate_code_example`, and `explain_api_error` MCP tools.
  - **Dependencies:** TASK-502, TASK-404.
  - **Files & Modules:** `backend/app/mcp/tools.py`.
  - **Acceptance Criteria:** Returns dereferenced schemas and diagnoses error codes against documented endpoints.
  - **Corresponding Tests:** `tests/integration/test_mcp_schema_tools.py`.

---

## Phase 6: Developer Dashboard & React UI Integration

- [ ] **TASK-601: Frontend Build System & Tailwind Design Tokens**
  - **Description:** Configure Vite, TypeScript, and Tailwind CSS in `frontend/` matching design system tokens (Zinc neutral palette, Indigo brand accent, HTTP method semantic colors).
  - **Dependencies:** TASK-101.
  - **Files & Modules:** `frontend/tailwind.config.js`, `frontend/src/index.css`.
  - **Acceptance Criteria:** Vite development server builds with zero errors; Tailwind utility classes render accurately.
  - **Corresponding Tests:** Frontend build verification (`npm run build`).

- [ ] **TASK-602: API Client & Server-Sent Events (SSE) Consumer**
  - **Description:** Implement typed fetch client in `frontend/src/api/client.ts` and streaming SSE consumer in `frontend/src/api/sse.ts`.
  - **Dependencies:** TASK-601, TASK-402.
  - **Files & Modules:** `frontend/src/api/client.ts`, `frontend/src/api/sse.ts`.
  - **Acceptance Criteria:** Consumes backend REST endpoints and streams LLM tokens into UI state with automatic reconnect and error handling.
  - **Corresponding Tests:** Unit tests for SSE parser.

- [ ] **TASK-603: Documentation Upload Dropzone & Project Management UI**
  - **Description:** Build `ProjectsPage.tsx` and `Dropzone.tsx` supporting drag-and-drop file upload, file format validation, and animated ingestion progress tracking.
  - **Dependencies:** TASK-602, TASK-201.
  - **Files & Modules:** `frontend/src/pages/ProjectsPage.tsx`, `frontend/src/components/common/Dropzone.tsx`.
  - **Acceptance Criteria:** User can create projects and upload OpenAPI specs with visual feedback at each ingestion stage.
  - **Corresponding Tests:** Component interaction tests.

- [ ] **TASK-604: Interactive API Endpoint Explorer & Schema Viewer**
  - **Description:** Build `ExplorerPage.tsx` with two-column split layout: filterable endpoint list with color-coded method badges, search bar, and interactive JSON schema tree inspector.
  - **Dependencies:** TASK-602, TASK-204.
  - **Files & Modules:** `frontend/src/pages/ExplorerPage.tsx`, `frontend/src/components/explorer/SchemaViewer.tsx`.
  - **Acceptance Criteria:** Endpoints can be filtered by method, tag, or path; schemas render with collapsible nodes and type indicators.
  - **Corresponding Tests:** UI rendering tests for schema inspector.

- [ ] **TASK-605: AI Documentation Assistant & Citation Drawer**
  - **Description:** Build `AssistantPage.tsx` featuring multi-turn chat interface, streaming message bubbles, copyable code snippet viewer with language tabs, and interactive citation chips opening the source context drawer.
  - **Dependencies:** TASK-602, TASK-403.
  - **Files & Modules:** `frontend/src/pages/AssistantPage.tsx`, `frontend/src/components/chat/MessageBubble.tsx`.
  - **Acceptance Criteria:** Messages stream in real-time; clicking a citation chip highlights the exact source chunk in the drawer.
  - **Corresponding Tests:** Chat interface component tests.

---

## Phase 7: Production Hardening, CI/CD & Observability

- [ ] **TASK-701: End-to-End Automated Test Suite & Coverage Gate**
  - **Description:** Assemble comprehensive unit, integration, and API test suites. Enforce >= 90% backend branch coverage in CI.
  - **Dependencies:** Phases 1 through 6.
  - **Files & Modules:** `tests/`, `.github/workflows/ci.yml`.
  - **Acceptance Criteria:** `pytest` passes with >= 90% branch coverage; CI pipeline reports green.
  - **Corresponding Tests:** Full test suite execution in CI.

- [ ] **TASK-702: Security Audit & Vulnerability Scanning**
  - **Description:** Integrate `pip-audit`, `Bandit`, and `npm audit` into CI. Validate file upload sanitization and prompt injection resistance.
  - **Dependencies:** TASK-701.
  - **Files & Modules:** `.github/workflows/ci.yml`.
  - **Acceptance Criteria:** Zero high or critical vulnerabilities identified in Python or Node dependencies; Bandit security scan passes.
  - **Corresponding Tests:** CI security scan job.

- [ ] **TASK-703: Production Multi-Stage Container Hardening**
  - **Description:** Optimize `backend/Dockerfile` and `frontend/Dockerfile` with non-root users, minimal image layers, health checks, and Nginx production caching.
  - **Dependencies:** TASK-702, TASK-106.
  - **Files & Modules:** `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`.
  - **Acceptance Criteria:** Containers build cleanly without security warnings; non-root user execution confirmed.
  - **Corresponding Tests:** Docker image build and run verification in CI.

- [ ] **TASK-704: Documentation Verification & Release v0.1.0 Tagging**
  - **Description:** Verify all documentation links, review `docs/MEMORY.md` status, ensure README reflects operational quickstart, and tag baseline release `v0.1.0`.
  - **Dependencies:** All previous tasks.
  - **Files & Modules:** `README.md`, `docs/*`.
  - **Acceptance Criteria:** Clean repo state; all documentation links verified; repository ready for production rollout.
  - **Corresponding Tests:** Markdown link and syntax validation.
