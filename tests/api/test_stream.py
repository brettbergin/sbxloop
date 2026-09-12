"""Server-sent events from the durable cursor space: resume by id, pings
while idle, closed when the token or the daemon goes.

The stream body is an endless generator, which Starlette's test client
cannot consume (it buffers a whole response), so the behaviours are
driven on the generator itself and the transport is checked once over
the real listener.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from sbxloop.api.app import create_app
from sbxloop.api.auth.deps import resolve_token
from sbxloop.api.routes import events as events_route
from sbxloop.api.routes.events import sse_frames
from sbxloop.api.server import ApiServer
from tests.api.conftest import Api, build


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_route, "WAIT_S", 0.05)


def _parse(text: str) -> list[dict[str, Any]]:
    """SSE frames from a body: comments as ``{"comment": …}``."""
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            out.append({"comment": block})
            continue
        frame: dict[str, Any] = {}
        for line in block.splitlines():
            field, _, value = line.partition(":")
            frame[field] = value.strip()
        out.append(frame)
    return out


def _collect(
    api: Api, headers: dict[str, str], *, after: int = 0, until: int
) -> list[dict[str, Any]]:
    """Run the generator until ``until`` frames were produced."""
    auth = resolve_token(api.ctx, headers["Authorization"].removeprefix("Bearer "))

    async def go() -> str:
        text = ""
        count = 0
        gen: AsyncIterator[str] = sse_frames(
            api.ctx, auth, after=after, run_id=None, type_prefix=None
        )
        async for frame in gen:
            text += frame
            count += 1
            if count >= until:
                break
        return text

    return _parse(asyncio.run(go()))


class TestFrames:
    def test_frames_carry_ids_and_resume_from_a_cursor(self, api: Api) -> None:
        headers = api.bearer()
        first = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 1})
        second = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 2})
        frames = _collect(api, headers, until=2)
        assert [f["id"] for f in frames] == [f"evt_{first}", f"evt_{second}"]
        assert frames[0]["event"] == "daemon.notice"
        assert json.loads(frames[0]["data"])["data"] == {"n": 1}
        # Resuming from the first id yields only what came after it.
        assert [f["id"] for f in _collect(api, headers, after=first, until=1)] == [f"evt_{second}"]

    def test_idle_streams_ping_and_a_write_wakes_them(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(events_route, "PING_EVERY_S", 0.0)
        headers = api.bearer()
        auth = resolve_token(api.ctx, headers["Authorization"].removeprefix("Bearer "))

        async def go() -> list[str]:
            seen: list[str] = []
            gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
            seen.append(await gen.__anext__())
            seq = api.ctx.chronology.record("daemon.notice", api.clock(), data={})
            api.ctx.hub.notify()
            while True:
                frame = await gen.__anext__()
                seen.append(frame)
                if f"id: evt_{seq}" in frame:
                    return seen

        seen = asyncio.run(go())
        assert seen[0] == ": ping\n\n" and seen[-1].startswith("id: evt_")

    def test_a_revoked_token_closes_the_stream(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(events_route, "ACCESS_RECHECK_S", 0.0)
        pair = api.token()
        api.ctx.chronology.record("daemon.notice", api.clock(), data={})
        auth = resolve_token(api.ctx, pair["access_token"])

        async def go() -> list[str]:
            seen: list[str] = []
            gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
            seen.append(await gen.__anext__())
            api.auth.revoke_client(pair["client_id"], api.clock())
            async for frame in gen:
                seen.append(frame)
            return seen

        seen = asyncio.run(go())
        assert seen[0].startswith("id: evt_")
        assert seen[-1] == 'event: stream.closed\ndata: {"reason":"access_revoked"}\n\n'

    def test_stopping_ends_every_stream(self, api: Api) -> None:
        headers = api.bearer()
        auth = resolve_token(api.ctx, headers["Authorization"].removeprefix("Bearer "))

        async def go() -> list[str]:
            gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
            api.ctx.stopping.set()
            api.ctx.hub.notify()
            return [frame async for frame in gen]

        assert asyncio.run(go()) == ['event: stream.closed\ndata: {"reason":"daemon_stopping"}\n\n']


class TestTransport:
    def test_the_route_refuses_what_it_must_before_streaming(self, api: Api) -> None:
        headers = api.bearer()
        assert api.client.get("/v1/events/stream").status_code == 401
        reader = api.bearer(frozenset({"audit:read"}))
        assert api.client.get("/v1/events/stream", headers=reader).status_code == 403
        bad = api.client.get("/v1/events/stream", params={"after": "nope"}, headers=headers)
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"
        api.ctx.chronology.record("daemon.notice", api.clock(), data={})
        api.clock.t += 604800 + 10
        api.ctx.chronology.prune(api.clock() - 604800)
        headers = api.bearer()
        gone = api.client.get("/v1/events/stream", headers=headers)
        assert gone.status_code == 410 and gone.json()["code"] == "cursor_expired"
        missing = api.client.get("/v1/events/stream", params={"run_id": "run_x"}, headers=headers)
        assert missing.status_code == 404

    def test_over_the_real_listener(self, tmp_path: Path) -> None:
        """One end-to-end pass: the frames over HTTP, a resume by
        ``Last-Event-ID``, and the client bound."""
        api = build(tmp_path, max_stream_clients=1)
        ephemeral = api.ctx.config.api.model_copy(update={"port": 0})
        server = ApiServer(create_app(api.ctx), ephemeral, ctx=api.ctx)
        server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/v1/events/stream"
            headers = api.bearer()
            first = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 1})
            second = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 2})

            def read(extra: dict[str, str], until: int) -> Iterator[str]:
                with httpx.stream("GET", url, headers={**headers, **extra}, timeout=10) as r:
                    assert r.status_code == 200
                    assert r.headers["content-type"].startswith("text/event-stream")
                    got = 0
                    for line in r.iter_lines():
                        yield line
                        if line.startswith("data:"):
                            got += 1
                            if got >= until:
                                return

            lines = list(read({}, 2))
            assert f"id: evt_{first}" in lines and f"id: evt_{second}" in lines
            resumed = list(read({"Last-Event-ID": f"evt_{first}"}, 1))
            assert f"id: evt_{first}" not in resumed and f"id: evt_{second}" in resumed
            # A second concurrent stream is refused by the bound.
            with httpx.stream("GET", url, headers=headers, timeout=10) as held:
                assert held.status_code == 200
                refused = httpx.get(url, headers=headers, timeout=10)
                assert refused.status_code == 503
                assert refused.json()["code"] == "too_many_streams"
        finally:
            server.close()
        api.ctx.close()
