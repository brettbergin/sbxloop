"""The FastAPI application: routers, error rendering, the two middlewares.

``create_app`` builds it over an :class:`ApiContext`; the daemon serves it
through :class:`~sbxloop.api.server.ApiServer`, a test through Starlette's
client. OpenAPI is published at ``/v1/openapi.json``; the interactive docs
are off — a remote client reads the contract, not a page.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from sbxloop import __version__
from sbxloop.api import errors, ws
from sbxloop.api.context import ApiContext
from sbxloop.api.routes import auth, catalog, events, health, items, meta, operations, runs, status

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(
        title="sbxloop",
        version=__version__,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.state.ctx = ctx
    max_body = int(ctx.api.max_body_bytes)

    @app.middleware("http")
    async def _request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # A client's own id is echoed so it can correlate; otherwise one is
        # minted. Either way it rides every problem body and log line.
        given = request.headers.get("x-request-id", "").strip()
        request.state.request_id = given[:64] if given else "req_" + uuid.uuid4().hex[:16]
        response = await call_next(request)
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
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id"],
            expose_headers=["X-Request-Id", "Location", "Retry-After"],
            allow_credentials=False,
            max_age=600,
        )

    errors.install(app)
    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(status.router)
    app.include_router(operations.router)
    app.include_router(items.router)
    app.include_router(runs.router)
    app.include_router(catalog.router)
    app.include_router(events.router)
    app.include_router(ws.router)
    app.include_router(auth.router)
    return app
