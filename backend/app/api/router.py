"""Top-level API router. Add new feature routers here."""

from fastapi import APIRouter

from app.api.v1 import health

# Probes live at the root (/health, /ready), as required by TASK-105.
# Versioned business APIs will be added to ``api_v1_router`` (prefix /api/v1) by later tasks.
api_v1_router = APIRouter(prefix="/api/v1")

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(api_v1_router)
