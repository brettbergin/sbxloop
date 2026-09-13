"""The FastAPI application: routers, error rendering, the two middlewares.

``create_app`` builds it over an :class:`ApiContext`; the daemon serves it
through :class:`~sbxloop.api.server.ApiServer`, a test through Starlette's
client. OpenAPI is published at ``/v1/openapi.json``; the interactive docs
are off — a remote client reads the contract, not a page.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from sbxloop import __version__
from sbxloop.api import errors, ws
from sbxloop.api.context import ApiContext
from sbxloop.api.routes import (
    admin,
    artifacts,
    auth,
    catalog,
    collaboration,
    control,
    diagnostics,
    events,
    health,
    items,
    meta,
    operations,
    runs,
    status,
    usage,
)
from sbxloop.config import ApiConfig

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
log = structlog.get_logger(__name__)

#: The version the committed contract snapshot carries: the document is
#: compared without the build's own version string.
SNAPSHOT_VERSION = "snapshot"


def openapi_document(config: ApiConfig | None = None, *, snapshot: bool = False) -> dict[str, Any]:
    """The contract the listener publishes at ``/v1/openapi.json``, built
    without a daemon: the routes and models are static, and only the
    body-size and CORS middleware read the config. ``snapshot`` replaces
    the build's version with a constant so two builds compare equal."""
    from types import SimpleNamespace

    stand_in = SimpleNamespace(api=config or ApiConfig())
    document = create_app(stand_in).openapi()  # type: ignore[arg-type]
    if snapshot:
        document = json.loads(json.dumps(document))
        document["info"]["version"] = SNAPSHOT_VERSION
    return document


def create_app(ctx: ApiContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await ctx.call(ctx.recover_collaboration)
        yield

    app = FastAPI(
        title="sbxloop",
        version=__version__,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    max_body = int(ctx.api.max_body_bytes)

    async def _request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A client's own id is echoed so it can correlate; otherwise one is
        # minted. Either way it rides every problem body and log line.
        given = request.headers.get("x-request-id", "").strip()
        trace_id = "req_" + uuid.uuid4().hex[:16]
        request.state.request_id = given[:64] if given else trace_id
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            # Templates exclude user-supplied paths; never log headers, query or body.
            log.info(
                "api.request",
                method=request.method,
                route=getattr(request.scope.get("route"), "path", "unmatched"),
                status=status,
                trace_id=trace_id,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.middleware("http")
    async def _body_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in _BODY_METHODS:
            length = request.headers.get("content-length")
            if length is None:
                if request.headers.get("transfer-encoding", "").lower() == "chunked":
                    return errors.render(
                        request,
                        errors.Problem(411, "length_required", "a Content-Length is required"),
                    )
            else:
                try:
                    size = int(length)
                except ValueError:
                    return errors.render(
                        request, errors.Problem(400, "invalid_request", "bad Content-Length")
                    )
                if size > max_body:
                    return errors.render(
                        request,
                        errors.Problem(
                            413,
                            "body_too_large",
                            f"request bodies are limited to {max_body} bytes",
                            limit=max_body,
                        ),
                    )
        return await call_next(request)

    if ctx.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(ctx.api.cors_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
            expose_headers=["X-Request-Id", "Location", "Retry-After"],
            allow_credentials=False,
            max_age=600,
        )

    app.middleware("http")(_request_id)
    errors.install(app)
    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(status.router)
    app.include_router(operations.router)
    app.include_router(items.router)
    app.include_router(runs.router)
    app.include_router(catalog.router)
    app.include_router(control.router)
    app.include_router(artifacts.router)
    app.include_router(usage.router)
    app.include_router(admin.router)
    app.include_router(diagnostics.router)
    app.include_router(events.router)
    app.include_router(ws.router)
    app.include_router(auth.router)
    app.include_router(collaboration.router)
    return app
