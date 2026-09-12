"""Errors as ``application/problem+json`` (RFC 9457).

Every refusal carries ``type``, ``title``, ``status``, ``detail`` and
``instance``, plus ``code`` — the stable machine-readable name a client
branches on — and ``request_id``. Nothing here echoes a credential.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sbxloop.daemon.controls.results import ControlError
from sbxloop.log import get_logger

log = get_logger(__name__)

PROBLEM_TYPE = "urn:sbxloop:problem:"
MEDIA_TYPE = "application/problem+json"

#: The HTTP status each control refusal code maps to.
CONTROL_STATUS: dict[str, int] = {
    "unknown_target": 404,
    "not_eligible": 409,
    "invalid_argument": 422,
    "already_terminal": 409,
    "already_in_progress": 409,
    "stale_revision": 409,
    "unsupported_for_kind": 409,
    "capability_unknown": 409,
    "capability_unsupported": 409,
    "forbidden": 403,
    "unsupervised": 409,
    "daemon_not_ready": 503,
    "source_unavailable": 503,
}

_TITLES: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    413: "Content Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class Problem(Exception):
    """A refusal to render as problem+json."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra

    @classmethod
    def from_control(cls, exc: ControlError) -> Problem:
        extra = {k: v for k, v in exc.detail.items() if k != "capability"}
        return cls(CONTROL_STATUS.get(exc.code, 409), exc.code, exc.message, **extra)


def render(request: Request, problem: Problem) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE + problem.code,
        "title": _TITLES.get(problem.status, "Error"),
        "status": problem.status,
        "detail": problem.detail,
        "instance": str(request.url.path),
        "code": problem.code,
        "request_id": getattr(request.state, "request_id", None),
        **problem.extra,
    }
    headers = dict(problem.headers)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        # The middleware that stamps it is outside the server-error layer:
        # a crash's response would otherwise carry the id in its body only.
        headers["X-Request-Id"] = str(request_id)
    return JSONResponse(body, status_code=problem.status, media_type=MEDIA_TYPE, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return render(request, exc)

    @app.exception_handler(ControlError)
    async def _control(request: Request, exc: ControlError) -> JSONResponse:
        return render(request, Problem.from_control(exc))

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            413: "body_too_large",
            415: "unsupported_media_type",
        }.get(exc.status_code, "http_error")
        detail = str(exc.detail) if exc.detail else "request refused"
        headers = dict(exc.headers or {})
        return render(request, Problem(exc.status_code, code, detail, headers=headers))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return render(
            request, Problem(422, "invalid_request", "the request did not validate", errors=errors)
        )

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        # The record says what broke; the client gets the id to quote.
        log.error(
            "api.request_crashed",
            path=str(request.url.path),
            request_id=getattr(request.state, "request_id", None),
            exc_info=True,
        )
        return render(
            request, Problem(500, "internal_error", "the request failed; quote the request id")
        )
