"""Liveness (/health) and readiness (/ready) probes."""

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

READINESS_TIMEOUT_SECONDS = 5.0


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: the process is up. Never touches the database."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_db)) -> JSONResponse:  # noqa: B008
    """Readiness probe: PostgreSQL is reachable and the pgvector extension is installed."""
    checks = {"database": "error", "vector_extension": "unknown"}
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
            result = await session.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
            checks["vector_extension"] = "ok" if result.first() is not None else "missing"
    except Exception as exc:
        # Messages can contain connection details: log only the exception type.
        logger.warning(
            "readiness check failed",
            extra={"checks": dict(checks), "error_type": type(exc).__name__},
        )
    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready", "checks": checks},
    )
