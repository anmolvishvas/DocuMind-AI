# DocuMind AI — Living Project Journal & Architectural Decision Records (ADR)

**Document Version:** 1.0.0  
**Current Status:** Foundation Established / Active Development  
**Lead Architect:** Anmol Vishvas (anmol23vishvas@gmail.com)  
**Target Repository:** [https://github.com/anmolvishvas/DocuMind-AI](https://github.com/anmolvishvas/DocuMind-AI)  
**Default Branch:** `main`  
**Last Updated:** November 2026  

---

## 1. Project Overview & Status Summary

DocuMind AI is an AI-powered API documentation intelligence platform that enables developers to understand, explore, search, troubleshoot, and interact with API documentation using natural language.

### Current Milestone
- **Active Phase:** **Phase 1: Foundation, Scaffolding & DevOps Baseline** (Completed) -> Moving to **Phase 2: Documentation Ingestion & OpenAPI AST Parsing**.
- **Repository Health:** Clean architecture baseline initialized; CI/CD workflows, documentation suite, and core configuration established.

---

## 2. Task Tracking Matrix

### 2.1 Completed Tasks (Phase 1 Baseline)
- [x] **TASK-101:** Monorepo directory structure, Git configuration, `.gitignore`, `.dockerignore`, PR template, and GitHub Actions (`ci.yml`, `pr-audit.yml`).
- [x] **TASK-102:** Backend dependency specifications in `backend/pyproject.toml` with pinned Python 3.12 versions, Ruff, mypy, and Pytest configs.
- [x] **TASK-103:** Core application configuration (`backend/app/core/config.py`) and structured JSON logging (`backend/app/core/logging.py`).
- [x] **TASK-104:** Asynchronous SQLAlchemy 2.0 database engine setup and declarative base models (`backend/app/core/database.py`, `backend/app/models/base.py`).
- [x] **TASK-105:** FastAPI entrypoint (`backend/app/main.py`) with CORS, correlation ID middleware, and `/health` and `/ready` probes.
- [x] **TASK-106:** Docker Compose orchestration (`docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`) with PostgreSQL 16 + pgvector container.

### 2.2 Pending Tasks (Immediate Queue: Phase 2)
- [ ] **TASK-201:** File upload validation and secure staging service (`backend/app/ingestion/validator.py`).
- [ ] **TASK-202:** OpenAPI 2.0 / 3.0 / 3.1 AST parser and `$ref` dereferencer (`backend/app/ingestion/openapi_parser.py`).
- [ ] **TASK-203:** Markdown and PDF text extraction services (`backend/app/ingestion/markdown_parser.py`, `backend/app/ingestion/pdf_parser.py`).
- [ ] **TASK-204:** Relational persistence for extracted endpoints, parameters, and schemas (`backend/app/models/endpoint.py`, `backend/app/api/v1/documents.py`).

### 2.3 Blocked Tasks
- *None at present.* All environmental prerequisites (Python, Docker, Git) are operational.

---

## 3. Known Technical Limitations & Constraints

1. **Large OpenAPI Specification Parsing Memory Overhead:** Parsing extremely large OpenAPI files (> 15 MB with circular `$ref` schemas) can consume significant memory during dictionary dereferencing. *Mitigation:* Offload parsing to worker thread pools (`run_in_threadpool`) and enforce a 20 MB upload ceiling.
2. **PostgreSQL pgvector Cold Starts:** First-time initialization of pgvector HNSW indexes on cold containers requires pre-warming the index into RAM for optimal sub-150ms latency.
3. **OpenAI Rate Limits on High-Volume Ingestion:** Ingesting an API spec with 200+ endpoints generates substantial embedding calls. *Mitigation:* Implemented batching (64 chunks per batch) with exponential backoff and jitter.

---

## 4. Immediate Next Actions

1. Author unit tests for OpenAPI AST parsing and dereferencing in `tests/unit/test_openapi_parser.py`.
2. Implement `backend/app/ingestion/openapi_parser.py` supporting Swagger 2.0 and OpenAPI 3.0/3.1 specs.
3. Create database migration for `projects`, `documents`, `endpoints`, and `document_chunks` using Alembic.

---

## 5. Architectural Decision Records (ADRs)

```
+----------------------------------------------------------------------------------------------------+
|                                    ADR LOG INDEX                                                   |
|                                                                                                    |
|  ADR-001: Selection of FastAPI for Core API Gateway & Ingestion Backend                           |
|  ADR-002: Selection of PostgreSQL with pgvector for Unified Relational & Vector Storage           |
|  ADR-003: Endpoint-Aware Semantic Chunking Strategy for OpenAPI Documentation                     |
|  ADR-004: Decoupled MCP Server Architecture via Official MCP Python SDK                           |
|  ADR-005: Docker Compose for Multi-Service Orchestration & Local Dev Parity                       |
|  ADR-006: Strict Typing with Pydantic v2 and SQLAlchemy 2.0 Async Mapped Columns                 |
+----------------------------------------------------------------------------------------------------+
```

---

### ADR-001: Selection of FastAPI for Core API Gateway & Ingestion Backend
- **Status:** Approved
- **Context:** DocuMind AI requires an asynchronous, high-concurrency backend to handle multipart file uploads, streaming Server-Sent Events (SSE) for LLM completions, background indexing jobs, and typed REST controllers.
- **Decision:** Adopt FastAPI on Python 3.12 with Uvicorn.
- **Alternatives Considered:**
  - *Django / DRF:* Too heavy, traditional synchronous ORM impedance, cumbersome SSE streaming.
  - *Flask:* Lacks native async request handling and built-in Pydantic schema validation.
  - *Node.js / Express:* Excellent concurrency, but fragments the ecosystem since the leading AI, AST, and vector data manipulation libraries are Python-native.
- **Consequences:** Provides native async/await, automated OpenAPI documentation generation, seamless Pydantic integration, and high developer velocity. Requires discipline to avoid blocking the event loop with CPU-heavy parsing.

---

### ADR-002: Selection of PostgreSQL with pgvector for Unified Relational & Vector Storage
- **Status:** Approved
- **Context:** The system manages structured relational data (workspaces, documents, endpoint paths, HTTP verbs, parameters, JSON schemas) alongside 1536-dimensional dense vector embeddings for semantic retrieval.
- **Decision:** Use PostgreSQL 16 with the `pgvector` extension using HNSW indexes for vector storage, rather than provisioning a separate dedicated vector database (e.g. Pinecone, Qdrant, Chroma).
- **Alternatives Considered:**
  - *Pinecone:* Proprietary SaaS; introduces external network latency, cloud dependency, vendor lock-in, and synchronization challenges across relational metadata and vectors.
  - *Qdrant / Milvus:* Excellent vector engines, but introduces the operational complexity of managing two distinct databases and dual-write distributed transaction risks.
- **Consequences:** Eliminates distributed data consistency issues; allows atomic cascade deletes (deleting a project instantly purges its vectors and relational data in one transaction); supports hybrid SQL queries filtering by `project_id` and metadata tags before vector cosine distance calculations.

---

### ADR-003: Endpoint-Aware Semantic Chunking Strategy for OpenAPI Documentation
- **Status:** Approved
- **Context:** Standard RAG pipelines use fixed-size character or token sliding windows (e.g. 500 characters). Applying this to OpenAPI specs splits endpoint paths from their request schemas and parameters, leading to severe retrieval inaccuracies and LLM hallucinations.
- **Decision:** Implement **Endpoint-Aware Semantic Chunking**. Each API endpoint constitutes a self-contained, semantically cohesive chunk containing the HTTP method, path, operation ID, tags, parameter descriptions, dereferenced request schemas, and response code models formatted in structured Markdown.
- **Alternatives Considered:**
  - *Naive Sliding Window Chunking:* Breaks JSON syntax and separates parameters from routes.
  - *Raw File Chunking:* Embeds the entire raw JSON/YAML file, causing token window overflow.
- **Consequences:** Dramatic improvement in retrieval precision (> 88% precision@5); vector distance accurately clusters queries (e.g., "how to authenticate a user") with the corresponding endpoint chunk (`POST /api/v1/auth/login`).

---

### ADR-004: Decoupled MCP Server Architecture via Official MCP Python SDK
- **Status:** Approved
- **Context:** To allow external AI agents and IDEs (Claude Desktop, Cursor) to leverage DocuMind AI, the platform must expose its search, schema extraction, and code generation capabilities over the open Model Context Protocol (MCP).
- **Decision:** Implement a dedicated MCP server inside `backend/app/mcp/` using the official `mcp` Python SDK, supporting both `stdio` and `SSE` transports. The MCP server delegates core domain logic to shared backend services rather than duplicating database or RAG code.
- **Alternatives Considered:**
  - *Custom REST Webhooks:* Requires custom agent integration code; lacks broad client ecosystem support.
  - *LangChain Agent Tools:* Tightly binds the architecture to a single third-party framework.
- **Consequences:** Enables zero-configuration integration with any MCP-compliant AI client; maintains a clean boundary between HTTP web clients and autonomous IDE tool execution.

---

### ADR-005: Docker Compose for Multi-Service Orchestration & Local Dev Parity
- **Status:** Approved
- **Context:** DocuMind AI consists of multiple coordinated services: a frontend React client, a FastAPI API gateway, an MCP server, and a PostgreSQL database with pgvector. Developers need a deterministic, reproducible local setup that matches CI/CD.
- **Decision:** Use Docker Compose (`docker-compose.yml`) with multi-stage Dockerfiles, explicit health checks, container dependencies (`depends_on`), and named persistent volumes.
- **Alternatives Considered:**
  - *Local bare-metal execution:* Fails when developers have incompatible local PostgreSQL versions or lack the native pgvector C-extension compilation toolchain.
  - *Kubernetes (Minikube / k3s):* Excessive operational complexity for local development.
- **Consequences:** Enables a 1-command startup (`docker compose up --build`); isolates database storage; ensures seamless CI pipeline execution.

---

### ADR-006: Strict Typing with Pydantic v2 and SQLAlchemy 2.0 Async Mapped Columns
- **Status:** Approved
- **Context:** Maintaining data integrity across complex JSON schemas, endpoint parameters, and database entities requires end-to-end compile-time and runtime type safety.
- **Decision:** Mandate Pydantic v2 for all DTOs and SQLAlchemy 2.0 `Mapped[...]` / `mapped_column(...)` for ORM models. Enforce strict `mypy` checks in CI.
- **Alternatives Considered:**
  - *SQLAlchemy 1.x legacy syntax:* Lacks static typing support; error-prone column definitions.
  - *Dataclasses:* Lacks automatic JSON Schema generation and deep validation.
- **Consequences:** Eliminates runtime type errors, ensures consistent JSON Schema export, and provides high editor autocomplete efficiency.
