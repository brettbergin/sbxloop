"""The WebSocket: the same durable events and the same typed commands as
REST and SSE, multiplexed on one connection.

Authentication is a bearer token in the ``Authorization`` header or, for
a browser client that cannot set one, an ``auth`` frame within a few
seconds of connecting — never the query string, which proxies and logs
keep. Frames are JSON objects with a ``type``:

client → server: ``auth{token}``, ``subscribe{after?, run_id?,
type_prefix?}``, ``unsubscribe``, ``command{id, action, target?, params?,
idempotency_key?, expected_revision?}``, ``ping``.

server → client: ``hello{watermark, workspace_id, generation}``,
``subscribed{after}``, ``unsubscribed``, ``event{event}`` (the public
envelope under ``event``), ``reply{id, ok, result | problem}``,
``error{code, detail}``, ``pong``, ``closing{reason}``.

A subscription is a cursor: events are read from the store by ``seq``
whenever the hub says there may be more, so a slow client holds no
buffer and blocks nobody. Access is re-checked on a schedule and the
connection closes when the token no longer stands.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sbxloop.api.auth.deps import Authenticated, get_ctx, resolve_token
from sbxloop.api.commands import dispatch
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.projections import Views
from sbxloop.api.replay import cursor_after, read_after
from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.daemon.controls.results import ControlError

router = APIRouter(prefix="/v1", tags=["events"])

AUTH_TIMEOUT_S = 5.0
WAIT_S = 1.0
ACCESS_RECHECK_S = 60.0
BATCH = 200

#: Close codes: the 4xxx range is the application's.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_MANY = 4429
CLOSE_TOO_LARGE = 4413
CLOSE_GOING_AWAY = 1001


def _problem(exc: Problem) -> dict[str, Any]:
    return {"code": exc.code, "status": exc.status, "detail": exc.detail, **exc.extra}


class _Session:
    def __init__(self, ws: WebSocket, ctx: ApiContext, auth: Authenticated) -> None:
        self.ws = ws
        self.ctx = ctx
        self.auth = auth
        self.subscribed = False
        self.cursor = 0
        self.run_id: str | None = None
        self.type_prefix: str | None = None
        self.last_check = ctx.clock()

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(frame, separators=(",", ":"), default=str))

    async def error(self, code: str, detail: str, **extra: Any) -> None:
        await self.send({"type": "error", "code": code, "detail": detail, **extra})

    # -- frames ------------------------------------------------------------------

    async def handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "ping":
            await self.send({"type": "pong"})
        elif kind == "subscribe":
            await self.subscribe(frame)
        elif kind == "unsubscribe":
            self.subscribed = False
            await self.send({"type": "unsubscribed"})
        elif kind == "command":
            await self.command(frame)
        elif kind == "auth":
            await self.error("invalid_frame", "already authenticated")
        else:
            await self.error("invalid_frame", f"unknown frame type {kind!r}")

    async def subscribe(self, frame: dict[str, Any]) -> None:
        ctx = self.ctx
        try:
            after = cursor_after(frame.get("after"))
            run_id = frame.get("run_id")
            type_prefix = frame.get("type_prefix")
            if type_prefix is not None and (
                not isinstance(type_prefix, str) or len(type_prefix) > 64
            ):
                raise Problem(422, "invalid_request", "type_prefix must be a short string")

            def prepare() -> str | None:
                views = Views(ctx)
                internal = views.run_by_public_id(str(run_id)).run_id if run_id else None
                ctx.chronology.project(views.now)
                if ctx.chronology.expired(after):
                    raise Problem(
                        410,
                        "cursor_expired",
                        "events after the cursor were pruned; read a fresh snapshot",
                        snapshot="/v1/status",
                    )
                return internal

            self.run_id = await ctx.call(prepare)
        except Problem as exc:
            await self.error(exc.code, exc.detail, **exc.extra)
            return
        self.cursor = after
        self.type_prefix = type_prefix
        self.subscribed = True
        await self.send({"type": "subscribed", "after": f"evt_{after}"})

    async def command(self, frame: dict[str, Any]) -> None:
        command_id = frame.get("id")
        if not isinstance(command_id, str) or not command_id:
            await self.error("invalid_frame", "a command needs a string id")
            return
        params = frame.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            await self.error("invalid_frame", "params must be an object", id=command_id)
            return
        if self.ctx.stopping.is_set() or not self.ctx.ready.is_set():
            await self.send(
                {
                    "type": "reply",
                    "id": command_id,
                    "ok": False,
                    "problem": {
                        "code": "daemon_not_ready",
                        "status": 503,
                        "detail": "the daemon is not taking commands",
                    },
                }
            )
            return
        try:
            result, after = await dispatch(
                self.ctx,
                self.auth,
                action=str(frame.get("action") or ""),
                target=frame.get("target"),
                params=params,
                idempotency_key=frame.get("idempotency_key"),
                expected_revision=frame.get("expected_revision"),
            )
        except (Problem, ControlError) as exc:
            # A service refusal answers as the REST route would: the same
            # problem body, on the reply — never a closed socket.
            problem = exc if isinstance(exc, Problem) else Problem.from_control(exc)
            await self.send(
                {"type": "reply", "id": command_id, "ok": False, "problem": _problem(problem)}
            )
            return
        await self.send({"type": "reply", "id": command_id, "ok": True, "result": result})
        if after is not None:
            # A stop or restart takes effect once the reply is on its way.
            after()

    # -- the subscription --------------------------------------------------------

    async def pump(self) -> None:
        """Send everything after the cursor that matches the subscription."""
        while self.subscribed:
            ctx = self.ctx
            try:
                page = await ctx.call(self._read, self.cursor, self.run_id, self.type_prefix)
            except Problem as exc:
                self.subscribed = False
                await self.error(exc.code, exc.detail, **exc.extra)
                return
            for event in page.data:
                # The envelope rides under its own key: its `type` is the
                # event's, the frame's is the frame's.
                await self.send({"type": "event", "event": event.model_dump(mode="json")})
                self.cursor = int(event.id.removeprefix("evt_"))
            if not page.has_more:
                return

    def _read(self, cursor: int, run_id: str | None, prefix: str | None) -> Any:
        return read_after(Views(self.ctx), cursor, run_id=run_id, type_prefix=prefix, limit=BATCH)

    async def recheck_access(self) -> bool:
        now = self.ctx.clock()
        if now - self.last_check < ACCESS_RECHECK_S:
            return True
        self.last_check = now
        try:
            self.auth = await self.ctx.call(resolve_token, self.ctx, self.auth.token)
        except Problem as exc:
            await self.send({"type": "closing", "reason": exc.code})
            await self.ws.close(code=CLOSE_UNAUTHENTICATED)
            return False
        return True


async def _authenticate(ws: WebSocket, ctx: ApiContext) -> Authenticated | None:
    """The header, or the first frame within the timeout; a failure sends
    an error frame and closes."""
    header = ws.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        token = token.strip()
    else:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        except TimeoutError:
            await ws.send_text(
                json.dumps({"type": "error", "code": "unauthenticated", "detail": "no auth frame"})
            )
            await ws.close(code=CLOSE_UNAUTHENTICATED)
            return None
        except WebSocketDisconnect:
            return None
        frame = _parse(raw)
        token = str(frame.get("token") or "") if frame and frame.get("type") == "auth" else ""
        if not token:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "code": "unauthenticated",
                        "detail": "the first frame must be auth{token}",
                    }
                )
            )
            await ws.close(code=CLOSE_UNAUTHENTICATED)
            return None
    try:
        auth = await ctx.call(resolve_token, ctx, token)
    except Problem as exc:
        await ws.send_text(json.dumps({"type": "error", **_problem(exc)}))
        await ws.close(code=CLOSE_UNAUTHENTICATED)
        return None
    if not auth.principal.can("runs:read"):
        await ws.send_text(
            json.dumps(
                {"type": "error", "code": "forbidden", "detail": "the token lacks runs:read"}
            )
        )
        await ws.close(code=CLOSE_FORBIDDEN)
        return None
    return auth


def _parse(raw: str) -> dict[str, Any] | None:
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    ctx = get_ctx(ws)  # type: ignore[arg-type]
    await ws.accept()
    auth = await _authenticate(ws, ctx)
    if auth is None:
        return
    if not ctx.hub.admit(int(ctx.api.max_stream_clients)):
        await ws.send_text(
            json.dumps({"type": "error", "code": "too_many_streams", "detail": "try again later"})
        )
        await ws.close(code=CLOSE_TOO_MANY)
        return
    session = _Session(ws, ctx, auth)
    max_frame = int(ctx.api.max_body_bytes)
    try:
        watermark = await ctx.call(ctx.chronology.watermark)
        await session.send(
            {
                "type": "hello",
                "watermark": None if watermark is None else f"evt_{watermark}",
                "workspace_id": WORKSPACE_ID,
                "generation": ctx.generation(),
            }
        )
        receive: asyncio.Task[str] | None = None
        while not ctx.stopping.is_set():
            if receive is None:
                receive = asyncio.ensure_future(ws.receive_text())
            waiter = asyncio.ensure_future(ctx.hub.wait(WAIT_S))
            done, _ = await asyncio.wait({receive, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if receive in done:
                raw = receive.result()
                receive = None
                if len(raw) > max_frame:
                    await session.error(
                        "frame_too_large", f"frames are limited to {max_frame} bytes"
                    )
                    await ws.close(code=CLOSE_TOO_LARGE)
                    break
                frame = _parse(raw)
                if frame is None:
                    await session.error("invalid_frame", "frames are JSON objects")
                else:
                    await session.handle(frame)
            if waiter not in done:
                waiter.cancel()
            if not await session.recheck_access():
                break
            await session.pump()
        else:
            await session.send({"type": "closing", "reason": "daemon_stopping"})
            await ws.close(code=CLOSE_GOING_AWAY)
        if receive is not None and not receive.done():
            receive.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        ctx.hub.leave()
