"""Crashes at the command boundaries: a process that dies after accepting
a command and before acting, and one that dies after the effect and
before closing the record. The next generation settles each from
evidence, and a client reading the record sees the truth, never a
guess."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.api.conformance.conftest import Client, register
from tests.api.conftest import build


def _crash_at(client: Client, seam: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every service the listener builds from here on dies at ``seam``."""
    api = client.api
    original = api.ctx.service

    def service() -> object:
        built = original()
        runner = getattr(built, "runner", None)
        if runner is not None:

            def boom(op: object) -> None:
                raise RuntimeError("process died")

            setattr(runner, seam, boom)
        return built

    monkeypatch.setattr(api.ctx, "service", service)


class TestBoundaries:
    def test_a_crash_before_the_effect_expires_the_intent(
        self, admin: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _crash_at(admin, "after_accept", monkeypatch)
        crashed = admin.post("/v1/daemon/holds", {"name": "deploy-1"})
        assert crashed.status_code == 500 and crashed.json()["code"] == "internal_error"
        # The command was accepted, durably, and never acted on.
        (op,) = admin.get("/v1/operations", target_kind="hold", target_id="deploy-1").json()["data"]
        assert op["state"] == "accepted"
        assert admin.get("/v1/daemon/holds").json()["data"] == []
        # The next generation refuses to apply stale intent: expired, said so.
        again = build(tmp_path)
        with again.client:
            client = register(again, "after")
            settled = client.get(f"/v1/operations/{op['id']}").json()
            assert settled["state"] == "expired"
            assert client.get("/v1/daemon/holds").json()["data"] == []
        again.ctx.close()

    def test_a_crash_after_the_effect_is_settled_from_evidence(
        self, admin: Client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _crash_at(admin, "after_effect", monkeypatch)
        crashed = admin.post("/v1/daemon/holds", {"name": "deploy-2"})
        assert crashed.status_code == 500
        (op,) = admin.get("/v1/operations", target_kind="hold", target_id="deploy-2").json()["data"]
        assert op["state"] == "running"
        # The effect happened: the hold stands, whoever asks.
        assert [h["name"] for h in admin.get("/v1/daemon/holds").json()["data"]] == ["deploy-2"]
        again = build(tmp_path)
        with again.client:
            client = register(again, "after")
            settled = client.get(f"/v1/operations/{op['id']}").json()
            assert settled["state"] == "succeeded"
            assert [h["name"] for h in client.get("/v1/daemon/holds").json()["data"]] == [
                "deploy-2"
            ]
        again.ctx.close()

    def test_the_listener_stays_up_and_the_next_command_is_taken(
        self, admin: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _crash_at(admin, "after_claim", monkeypatch)
        assert admin.post("/v1/daemon/holds", {"name": "h1"}).status_code == 500
        monkeypatch.undo()
        taken = admin.post("/v1/daemon/holds", {"name": "h2"})
        assert taken.status_code == 201, taken.text
        assert admin.get("/health/ready").json()["ready"] is True
