"""A client as a remote client is: a name, a grant, a token, and the
verbs the contract offers — nothing reaches past the HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, Capability
from sbxloop.daemon.sources import ApiSource, CompositeSource, GitHubIssueSource, ScheduleSource
from tests.api.conftest import Api, build
from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

READER = frozenset({"runs:read"})
OPERATOR = frozenset(
    {"runs:read", "runs:control", "runs:steer", "items:create", "audit:read", "artifacts:read"}
)


@dataclass
class Client:
    """One remote client: a registered grant and the token it minted."""

    api: Api
    name: str
    client_id: str
    token: str
    keys: int = field(default=0)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **params: Any) -> httpx.Response:
        return self.api.client.get(path, params=params or None, headers=self.headers)

    def post(
        self, path: str, body: dict[str, Any] | None = None, *, key: str | None = None
    ) -> httpx.Response:
        headers = dict(self.headers)
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.api.client.post(path, json=body, headers=headers)

    def delete(self, path: str) -> httpx.Response:
        return self.api.client.delete(path, headers=self.headers)

    def fresh_key(self) -> str:
        self.keys += 1
        return f"{self.name}-{self.keys}"

    def events(self, *, after: str | None = None, type_prefix: str | None = None) -> list[Any]:
        params: dict[str, Any] = {"limit": 200}
        if after:
            params["after"] = after
        if type_prefix:
            params["type_prefix"] = type_prefix
        return list(self.get("/v1/events", **params).json()["data"])

    def event_types(self, **filters: Any) -> list[str]:
        return [e["type"] for e in self.events(**filters)]


def register(api: Api, name: str, capabilities: frozenset[Capability] = ALL_CAPABILITIES) -> Client:
    client, secret = api.auth.create_client(name, capabilities, created_by="test", now=api.clock())
    response = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "client_credentials", "client_id": client.id, "client_secret": secret},
    )
    assert response.status_code == 200, response.text
    return Client(api=api, name=name, client_id=client.id, token=response.json()["access_token"])


@pytest.fixture
def ops() -> RecordingOps:
    """The target forge: one open unlabelled issue, one closed."""
    return RecordingOps({"4": issue(4), "6": issue(6, state="closed")})


def sources(ops: RecordingOps) -> CompositeSource:
    github = GitHubIssueSource(
        lambda: ops,  # type: ignore[arg-type]
        "o/r",
        LABELS,
        host="db",
        clock=lambda: FIXTURE_NOW,
    )
    return CompositeSource(github, None, ScheduleSource(), ApiSource())


@pytest.fixture
def api(tmp_path: Path, ops: RecordingOps) -> Iterator[Api]:
    built = build(tmp_path, config={"workloads": [{"name": "brief", "sinks": ["artifact"]}]})
    built.loop.source = sources(ops)
    with built.client:
        yield built
    built.ctx.close()


def fake_source(api: Api) -> None:
    """Route the loop to the harness's scripted source: the tests that
    hold a run in flight dispatch through it."""
    api.loop.source = api.harness.source


def tick(api: Api, *outcomes: str) -> str:
    """One daemon tick with the scripted outcomes; the run id it dispatched."""
    api.harness.outcomes = list(outcomes)
    api.clock.t += 10
    api.loop.tick()
    return str(api.harness.runs[-1][0])


@pytest.fixture
def admin(api: Api) -> Client:
    return register(api, "admin")


@pytest.fixture
def operator(api: Api) -> Client:
    return register(api, "operator", OPERATOR)


@pytest.fixture
def reader(api: Api) -> Client:
    return register(api, "reader", READER)
