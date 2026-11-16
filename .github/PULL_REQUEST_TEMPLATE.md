## Description

<!-- Provide a concise summary of the changes introduced by this pull request. -->

### Motivation and Context
<!-- Why is this change required? What problem does it solve? Link any related GitHub issues below. -->
Fixes / Relates to issue: #

---

## Type of Change

- [ ] `feat`: A new user-facing feature or API capability
- [ ] `fix`: A bug fix
- [ ] `refactor`: Code change that neither fixes a bug nor adds a feature
- [ ] `perf`: A code change that improves performance or latency
- [ ] `docs`: Documentation updates only
- [ ] `test`: Adding missing tests or correcting existing tests
- [ ] `chore`: Changes to build process, CI/CD, dependencies, or tooling

---

## Architectural & System Impact

- **Breaking Changes:**
  - [ ] Yes (explain migration path below)
  - [ ] No

- **Database Migrations:**
  - [ ] New Alembic migration included
  - [ ] Requires database re-indexing or backfill
  - [ ] No database changes

- **LLM / RAG Impact:**
  - [ ] Modifies prompt templates or boundary fencing
  - [ ] Changes embedding model, vector index, or chunking strategy
  - [ ] No LLM/RAG changes

- **MCP Integration Impact:**
  - [ ] Modifies MCP tool definitions, input schemas, or outputs
  - [ ] No MCP changes

---

## Security & Privacy Considerations

- [ ] Input data sanitized and validated using Pydantic v2
- [ ] Tenant data isolation enforced (mandatory `project_id` filter)
- [ ] No secrets, credentials, or personal information exposed in logs or commits
- [ ] Untrusted documentation contents enclosed in XML safety boundaries

---

## Testing Performed

<!-- Describe the specific tests executed to verify your changes. Include commands used and key results. -->
- **Unit Tests:** `pytest tests/unit/`
- **Integration Tests:** `pytest tests/integration/`
- **Static Analysis & Formatting:** `ruff check backend tests && ruff format --check backend tests`
- **Type Checking:** `mypy --config-file backend/pyproject.toml backend`

---

## Pull Request Checklist

Please ensure all the following checklist items are completed before requesting a review:

- [ ] My code strictly adheres to the standards defined in **[docs/RULES.md](docs/RULES.md)**.
- [ ] I have added unit or integration tests that prove my fix or feature works as expected.
- [ ] I have maintained or improved the project's **>= 90% branch coverage** requirement.
- [ ] All new and existing tests pass locally.
- [ ] Ruff linting and formatting checks pass with zero warnings.
- [ ] Strict mypy type checks pass with zero errors.
- [ ] Database migrations have been tested forward (`upgrade head`) and backward (`downgrade -1`).
- [ ] Docker build succeeds locally (`docker compose build`).
- [ ] Relevant documentation in `docs/` or `README.md` has been updated.
- [ ] My PR title follows [Conventional Commits](https://www.conventionalcommits.org/) (e.g., `feat(rag): add endpoint-aware chunker`).
