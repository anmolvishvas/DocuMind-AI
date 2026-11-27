"""FastAPI application entrypoint."""

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.database import dispose_engine
from app.core.exceptions import DocuMindException
from app.core.logging import (
    CORRELATION_ID_HEADER,
    configure_logging,
    generate_correlation_id,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger(__name__)

_HEADER_KEY = CORRELATION_ID_HEADER.lower().encode("latin-1")
# Incoming IDs are only trusted if they are short and log/header safe.
_VALID_CORRELATION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _error_body(message: str, code: str) -> dict[str, object]:
    return {"error": message, "code": code, "correlation_id": get_correlation_id()}


class CorrelationIdMiddleware:
    """Bind a correlation ID per request, echo it in the response, always reset it.

    Also turns unhandled exceptions into a generic 500 so the header is still sent
    and nothing internal reaches the client.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next((v.decode("latin-1") for k, v in scope["headers"] if k == _HEADER_KEY), "")
        correlation_id = (
            incoming if _VALID_CORRELATION_ID.fullmatch(incoming) else generate_correlation_id()
        )
        token = set_correlation_id(correlation_id)
        started = False

        async def send_with_header(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message["headers"] if k.lower() != _HEADER_KEY]
                headers.append((_HEADER_KEY, correlation_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        except Exception:
            logger.exception("unhandled error")
            if started:
                raise
            body = json.dumps(_error_body("Internal server error", "INTERNAL_ERROR")).encode()
            await send_with_header(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        finally:
            reset_correlation_id(token)


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DocuMindException):  # pragma: no cover
        raise exc
    if exc.status_code >= 500:
        logger.error("domain error", extra={"code": exc.code})
    return JSONResponse(_error_body(exc.message, exc.code), exc.status_code)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc
    return JSONResponse(
        _error_body(str(exc.detail), f"HTTP_{exc.status_code}"),
        exc.status_code,
        headers=exc.headers,
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    details = [
        {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    body = {**_error_body("Request validation failed", "VALIDATION_ERROR"), "details": details}
    return JSONResponse(body, 422)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Startup: fail fast on invalid configuration (raises ConfigurationError).
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("application starting", extra={"environment": settings.environment})
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("application stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(title="DocuMind AI", version="0.1.0", lifespan=lifespan)
    application.add_exception_handler(DocuMindException, _domain_error_handler)
    application.add_exception_handler(StarletteHTTPException, _http_error_handler)
    application.add_exception_handler(RequestValidationError, _validation_error_handler)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_ID_HEADER],
    )
    # Added last => outermost, so the ID is bound for CORS and everything inside it.
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(api_router)
    return application


def __getattr__(name: str) -> FastAPI:
    """Lazily build ``app`` so importing this module needs no configuration."""
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
