# DocuMind AI — Engineering Rulebook & Contributor Standards

**Document Version:** 1.0.0  
**Enforcement Level:** Mandatory / Strict  
**Author:** Anmol Vishvas (anmol23vishvas@gmail.com)  
**Target Repository:** [https://github.com/anmolvishvas/DocuMind-AI](https://github.com/anmolvishvas/DocuMind-AI)  
**Default Branch:** `main`  

---

## 1. Core Architecture Principles

Every contributor, whether human or AI agent, must adhere to these foundational architectural principles:

1. **Separation of Concerns (SoC):** The system is segregated into distinct layers: Presentation (`frontend/`), Gateway & Controllers (`backend/app/api/`), Domain Services (`backend/app/rag/`, `backend/app/ingestion/`), and Persistence (`backend/app/models/`). Code in one layer must never leak into another.
2. **Dependency Inversion (DIP):** High-level business logic must not depend directly on concrete low-level infrastructure implementations. Services consume abstractions (e.g., an abstract embedding generator interface, an abstract storage manager).
3. **Single Responsibility Principle (SRP):** Each class, function, and module must have one reason to change. Parsers parse, chunkers chunk, retrievers retrieve, and controllers serialize.
4. **Thin Route Handlers (Zero Business Logic in Controllers):** FastAPI route handlers in `backend/app/api/` are strictly controllers. Their sole duties are:
   - Dependency injection resolution (`Depends()`)
   - HTTP request parsing and Pydantic validation
   - Calling the appropriate domain service method
   - Mapping service domain objects into Pydantic HTTP response models
   - Handling HTTP-specific status codes
   *Under no circumstances may database queries, raw SQL, or LLM prompt generation occur directly inside a route function.*
5. **No Premature Abstraction / No Overengineering:** Do not construct speculative generic plugin frameworks or meta-programming wrappers. Favor explicit, readable code over clever abstractions.

---

## 2. Python 3.12+ Engineering Standards

### 2.1 Typing & Validation
- **Strict Type Annotations:** 100% of functions, methods, parameters, and return types must be explicitly annotated. No implicit `Any`.
- **Modern Typing Syntax:** Use Python 3.12+ native union syntax (`X | None` instead of `Optional[X]`, `list[str]` instead of `List[str]`).
- **Pydantic v2 Standardization:**
  - All data transfer objects (DTOs) and API request/response payloads must inherit from `pydantic.BaseModel`.
  - Use `model_config = ConfigDict(strict=True, from_attributes=True)`.
  - Validate inputs using `@field_validator` and `@model_validator` with explicit mode definitions (`mode="before"` or `mode="after"`).
  - Use `pydantic.Field(...)` with descriptions for all schema properties.

### 2.2 SQLAlchemy 2.0 Declarative Standards
- Use SQLAlchemy 2.0 style declarative mapping using `Mapped[...]` and `mapped_column(...)`.
- Never use legacy SQLAlchemy 1.x `Column(...)` syntax.
- All database queries must use modern `select(...)` statements executed asynchronously via `session.execute(select(...))` or `session.scalars(...)`.
- Relationships must explicitly specify `lazy="selectin"` or `lazy="raise"` to eliminate unexpected N+1 queries.

```python
# CORRECT: Modern SQLAlchemy 2.0 Syntax
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

### 2.3 Exception Hierarchy & Error Handling
- Never swallow exceptions with bare `except:` or `except Exception: pass`.
- Every domain layer must define custom exceptions inheriting from `DocuMindException`:
  ```python
  class DocuMindException(Exception):
      """Base exception for all domain errors."""
      def __init__(self, message: str, code: str = "INTERNAL_ERROR"):
          self.message = message
          self.code = code
          super().__init__(self.message)

  class DocumentParsingError(DocuMindException):
      """Raised when an uploaded document cannot be parsed."""

  class EndpointNotFoundError(DocuMindException):
      """Raised when a requested API endpoint is absent from the catalog."""
  ```
- Catch and translate low-level errors (e.g. `asyncpg.PostgresError`, `openai.APIError`) into structured domain exceptions with clear logging.

### 2.4 State & Mutability
- Avoid global mutable state. Store application configuration in a single immutable Pydantic `Settings` instance loaded at startup.
- Never instantiate module-level database sessions or stateful clients that cross request boundaries.

---

## 3. Asynchronous Execution & Concurrency Standards

1. **Pure Async Request Pipelines:** All FastAPI route handlers and database queries must use `async`/`await`.
2. **Never Block the Async Event Loop:**
   - Any synchronous, CPU-intensive operation (e.g. PDF parsing with `pypdf`, complex AST manipulation with `pyyaml`, image processing) must be dispatched to a background thread pool using:
     ```python
     from starlette.concurrency import run_in_threadpool
     extracted_text = await run_in_threadpool(sync_pdf_extractor, file_bytes)
     ```
   - Never use `time.sleep()`, synchronous `requests.get()`, or blocking file I/O inside `async def` functions. Use `asyncio.sleep()`, `httpx.AsyncClient()`, and `aiofiles`.
3. **Database Connection Safety:**
   - Always yield database sessions via an `async_sessionmaker` dependency:
     ```python
     async def get_db() -> AsyncGenerator[AsyncSession, None]:
         async with AsyncSessionFactory() as session:
             try:
                 yield session
                 await session.commit()
             except Exception:
                 await session.rollback()
                 raise
     ```
4. **Timeouts on External Calls:** Every external HTTP or database request must have an explicit timeout configured (e.g. `httpx.AsyncClient(timeout=10.0)`).

---

## 4. LLM Engineering Standards

1. **Provider Isolation:** Never call `openai` directly inside routers or chunkers. All completions and embeddings must pass through `LLMService` and `EmbeddingService` interfaces.
2. **Zero Hardcoded Secrets:** API keys must be loaded exclusively from validated environment variables via `app.core.config.Settings`.
3. **Strict Structured Outputs:** When expecting JSON from an LLM, use OpenAI's native Structured Outputs (`response_format={"type": "json_schema", ...}`) or Pydantic validation on the returned payload.
4. **Token Budget Enforcement:**
   - Every prompt assembly must compute and log input token counts using `tiktoken`.
   - Hard cap prompt context size to avoid context exhaustion and cost runaway.
5. **Prompt Isolation & Fencing:**
   - Keep prompt templates in dedicated modules (`backend/app/rag/prompts.py`).
   - Wrap untrusted documentation in XML delimiters (`<api_context>...</api_context>`) to defeat prompt injection attempts.
6. **Hallucination Refusal Constraint:** Every prompt must include the strict instruction: *"If the information requested cannot be found directly in the supplied documentation, explicitly state: 'The provided documentation does not contain information regarding [topic].' Do not guess or extrapolate."*
7. **Never Execute LLM Outputs:** Code generated by the LLM (e.g. cURL, Python, JavaScript) is returned to the user or MCP client as formatted text strings. It must never be evaluated using `eval()`, `exec()`, or subshell execution.

---

## 5. RAG & Vector Retrieval Standards

1. **Endpoint-Aware Chunking First:** OpenAPI specifications must be normalized into discrete, semantically cohesive endpoint chunks containing route, parameters, and schemas. Naive token chunking is strictly prohibited for OpenAPI documents.
2. **Metadata Envelope Requirement:** Every single chunk stored in `document_chunks` must retain an immutable metadata envelope:
   ```json
   {
     "project_id": "...",
     "document_id": "...",
     "source_filename": "...",
     "chunk_type": "endpoint|narrative|schema",
     "http_method": "POST",
     "route_path": "/v1/orders",
     "token_count": 312
   }
   ```
3. **Mandatory Tenant Filtering:** Every vector search query against `pgvector` must include a strict SQL filter for `project_id`. Cross-project vector search is a critical defect.
4. **Cosine Similarity Threshold:** Discard any retrieved chunk with a cosine similarity score below $0.70$. Irrelevant context must not be fed to the LLM.
5. **Verifiable Citations:** When the LLM outputs an answer, it must attach citation metadata referencing the `document_id`, `filename`, and `route_path` from which the answer was derived.

---

## 6. Model Context Protocol (MCP) Standards

1. **Official SDK Compliance:** Implement the MCP server exclusively using the official Python SDK (`mcp`).
2. **Strongly Typed Tool Contracts:** Every tool must define explicit Pydantic or JSON Schema input and output models.
3. **Predictable JSON Payloads:** Tool execution results must be formatted as valid JSON strings enclosed within standard MCP `TextContent` or `CallToolResult` structures.
4. **Granular Error Reporting:** Never crash the MCP server on invalid tool arguments. Return structured error JSON with status code and actionable remediation guidance.
5. **No Secret Leakage:** Tool outputs must be sanitized to ensure database connection strings, API keys, or internal filesystem paths are never returned to the MCP client.

---

## 7. Security & Privacy Standards

1. **File Upload Security:**
   - Check file size before buffering (maximum 20 MB).
   - Validate file extensions (`.json`, `.yaml`, `.yml`, `.pdf`, `.md`, `.txt`).
   - Validate MIME types and magic bytes (`%PDF-` for PDFs, valid UTF-8 text for JSON/YAML).
   - Generate unique UUID filenames on disk (`uuid4().hex + ext`). Never use the client-supplied filename as an on-disk path (prevents path traversal).
2. **SQL Injection Defense:** All database interactions must use parameterized SQLAlchemy ORM statements. Raw string concatenation or f-strings in SQL are strictly prohibited.
3. **Prompt Injection Mitigation:** System prompts must instruct the LLM that content within `<api_context>` is external documentation and should never be interpreted as instructions to change its behavior or ignore previous guidelines.
4. **Log Sanitization:** Sensitive headers (e.g. `Authorization`, `Cookie`) and environment secrets must be scrubbed from structured logs.

---

## 8. Quality Assurance & Testing Standards

1. **Test Pyramid:**
   - **Unit Tests (`tests/unit/`):** Fast, in-memory testing of parsers, chunkers, validators, and utility functions without network or database dependencies.
   - **Integration Tests (`tests/integration/`):** Test database queries against real PostgreSQL + pgvector instances, test end-to-end RAG retrieval, and test MCP tool execution.
   - **API Smoke Tests (`tests/test_health.py`):** Validate HTTP endpoints and status codes using `httpx.AsyncClient`.
2. **Coverage Threshold:** Maintain a minimum of **90% branch coverage** across all backend code.
3. **Deterministic Fixtures:** Use Pytest fixtures (`tests/conftest.py`) for database provisioning, session rollback, and sample OpenAPI test files.
4. **Mocking External Services:** Never make real network calls to OpenAI or external services during automated unit tests. Use `unittest.mock` or `pytest-mock` with pre-recorded response fixtures.

---

## 9. Tooling & Enforcement Commands

Contributors must verify their code locally prior to submitting pull requests using the following commands:

```bash
# 1. Run Ruff linter checks
ruff check backend tests

# 2. Run Ruff format validation
ruff format --check backend tests

# 3. Run strict mypy type checking
mypy --config-file backend/pyproject.toml backend

# 4. Run Pytest with branch coverage enforcement
pytest --cov=backend/app --cov-report=term-missing --cov-report=xml --cov-fail-under=90 tests/
```
