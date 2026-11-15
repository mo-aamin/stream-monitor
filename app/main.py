"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from . import APP_NAME, __version__
from .config import AppConfig, load_config
from .context import STATIC_DIR, build_context, render
from .routes import api as api_routes
from .routes import pages as page_routes

logger = logging.getLogger("streammonitor")


def configure_logging(config: AppConfig) -> None:
    config.ensure_directories()
    root = logging.getLogger("streammonitor")
    if getattr(root, "_streammonitor_configured", False):
        return
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        file_handler = RotatingFileHandler(
            config.app_log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        root.warning("Could not open application log file", exc_info=True)
    root.propagate = False
    root._streammonitor_configured = True  # type: ignore[attr-defined]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        return response


def create_app(config: AppConfig | None = None, *, start_monitor: bool | None = None) -> FastAPI:
    app_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(app_config)
        ctx = build_context(app_config)
        app.state.ctx = ctx
        orphaned = ctx.db.mark_orphaned_recordings()
        if orphaned:
            ctx.db.add_event(
                "warning",
                "system",
                f"Marked {orphaned} recording(s) as interrupted after application restart",
            )
            logger.warning("Marked %s interrupted recording(s) from a previous run", orphaned)
        should_start = app_config.monitor_autostart if start_monitor is None else start_monitor
        if should_start:
            ctx.monitor.start()
        logger.info(
            "%s v%s started (data=%s, recordings=%s)",
            APP_NAME,
            __version__,
            app_config.data_dir,
            app_config.recordings_dir,
        )
        try:
            yield
        finally:
            await ctx.monitor.shutdown()

    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = app_config

    app.add_middleware(GZipMiddleware, minimum_size=600)
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(page_routes.router)
    app.include_router(api_routes.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request):
        ctx = getattr(request.app.state, "ctx", None)
        return {
            "status": "ok",
            "app": APP_NAME,
            "version": __version__,
            "monitor": ctx.monitor.state if ctx else "starting",
        }

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": str(exc.detail)},
                status_code=exc.status_code,
            )
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is None:
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        return render(
            ctx,
            request,
            "error.html",
            status_code=exc.status_code,
            page_title="Error",
            code=exc.status_code,
            message=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "Internal server error. Check the application log."},
                status_code=500,
            )
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is None:
            return PlainTextResponse("Internal server error", status_code=500)
        return render(
            ctx,
            request,
            "error.html",
            status_code=500,
            page_title="Error",
            code=500,
            message="Internal server error. Check the application log.",
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual/dev entrypoint
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=app.state.config.host,
        port=app.state.config.port,
        reload=False,
    )
