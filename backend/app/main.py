"""FastAPI application factory -- the composition root.

Everything the app depends on (database engine, event bus, provider registry)
is constructed here during lifespan startup and attached to `app.state`.
Modules never reach for a global connection; they receive one through a
dependency. That is what makes the whole thing testable with fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.session import Database

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of every long-lived resource.

    Acquire on the way in, release on the way out, in reverse order. Resources
    are added here as later phases introduce them (DB engine, event bus).
    """
    settings: Settings = app.state.settings
    log.info("startup", env=settings.app_env, bus=settings.event_bus_backend)

    app.state.database = Database(settings)

    try:
        yield
    finally:
        # Release in reverse acquisition order, and in a `finally` so a crash
        # during startup of a later resource still closes the earlier ones.
        await app.state.database.dispose()
        log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an app instance. Tests call this directly with overridden settings."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="LLM Inference Logger",
        version="0.1.0",
        summary="Instrumentation, ingestion and observability for multi-provider LLM inference",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # The frontend is a separate origin in development; in production it is
    # served behind the same ingress and this becomes a no-op.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_error_handlers(app)

    app.include_router(health.router)

    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Map the error taxonomy onto HTTP once, centrally."""

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        log.warning("app_error", code=exc.code, message=exc.message, **exc.context)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to the client; the traceback goes to the log.
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )


def run() -> None:
    """`llm-api` console entrypoint."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # containers bind all interfaces by design
        port=8000,
        reload=settings.app_env == "local",
        log_config=None,  # structlog owns logging
    )


app = create_app()
