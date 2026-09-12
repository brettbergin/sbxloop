"""The API over a real daemon loop, in process.

``Harness`` (the loop tests' fixture) gives a ``DaemonLoop`` over real
stores with a scripted runner; the app is built over it and driven with
Starlette's client, so a request goes through the same executor, stores
and service a remote client's would. ``Clock`` is the daemon's and the
API's alike, so token expiry is a matter of moving it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sbxloop.api.app import create_app
from sbxloop.api.auth.keys import SigningKeys, load_or_create
from sbxloop.api.auth.store import ApiAuthStore, Client
from sbxloop.api.context import ApiContext
from sbxloop.config import Config
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, Capability
from tests.unit.test_daemon_loop import Harness


@dataclass
class Api:
    """Everything a test reaches for: the loop, the client, the auth store."""

    harness: Harness
    ctx: ApiContext
    client: TestClient
    keys: SigningKeys
    auth: ApiAuthStore

    @property
    def loop(self) -> Any:
        return self.harness.loop

    @property
    def clock(self) -> Any:
        return self.harness.clock

    def register(
        self, name: str = "tester", capabilities: frozenset[Capability] = ALL_CAPABILITIES
    ) -> tuple[Client, str]:
        return self.auth.create_client(
            name, capabilities, created_by="test", now=self.harness.clock()
        )

    def token(
        self, capabilities: frozenset[Capability] = ALL_CAPABILITIES, name: str = "tester"
    ) -> dict[str, Any]:
        client, secret = self.register(name, capabilities)
        response = self.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client.id,
                "client_secret": secret,
            },
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def bearer(self, capabilities: frozenset[Capability] = ALL_CAPABILITIES) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(capabilities)['access_token']}"}


def build(tmp_path: Path, *, ready: bool = True, **api_overrides: Any) -> Api:
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "api": {"enabled": True, **api_overrides},
        }
    )
    harness = Harness(tmp_path, config)
    keys = load_or_create(config.paths)
    auth = ApiAuthStore(harness.dstore)
    ctx = ApiContext(config, loop=harness.loop, auth=auth, keys=keys, clock=harness.clock)
    if ready:
        harness.loop.recover()
        ctx.ready.set()
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    return Api(harness=harness, ctx=ctx, client=client, keys=keys, auth=auth)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    built = build(tmp_path)
    with built.client:
        yield built
    built.ctx.close()
