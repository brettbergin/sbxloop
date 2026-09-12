"""The intake rules beneath the remote API's `POST /items` (#1036): what
an inline workload or a recipe must satisfy before it becomes an item,
and how the service records an admission."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.controls import ControlError, ControlService, Principal
from sbxloop.daemon.controls.intake import (
    IssueAdmission,
    ToolAdmission,
    WorkloadAdmission,
    build_item,
    target_key,
)
from sbxloop.daemon.controls.operations import IdempotencyConflict, OperationReplay
from tests.unit.test_daemon_loop import Harness

CLIENT = Principal(
    kind="client",
    id="cli_a",
    display="reporter",
    via="api",
    capabilities=frozenset({"items:create", "runs:read"}),
)


def config(**sections: object) -> Config:
    return Config.model_validate(
        {
            "home": "/tmp/x",
            "github": {"repo": "o/r"},
            "workloads": [{"name": "research", "sinks": ["chat", "issue"]}],
            **sections,
        }
    )


class TestBuildItem:
    def test_a_workload_is_the_concierges_shape_under_an_api_id(self) -> None:
        request = WorkloadAdmission(ask="Summarise\nthe week", profile="research", sink="issue")
        key = target_key(request)
        assert key.startswith("api:")
        item = build_item(config(), request, item_id=key, requested_by=None)
        assert item.item_id == key and item.source_key == key.removeprefix("api:")
        assert item.kind == "workload" and item.profile == "research"
        assert item.title == "Summarise" and item.body.endswith("through the `issue` sink.")
        assert item.requested_by is None and item.repo is None
        # A caller's key names the item; a request without one mints a fresh one.
        assert target_key(WorkloadAdmission(ask="x", key="m1")) == "api:m1"
        assert target_key(WorkloadAdmission(ask="x")) != target_key(WorkloadAdmission(ask="x"))

    def test_workload_refusals(self) -> None:
        cfg = config()
        with pytest.raises(ControlError, match="ask is required") as blank:
            build_item(cfg, WorkloadAdmission(ask="  "), item_id="api:k", requested_by=None)
        assert blank.value.code == "invalid_argument"
        with pytest.raises(ControlError, match="not declared") as unknown:
            build_item(
                cfg, WorkloadAdmission(ask="x", profile="nope"), item_id="api:k", requested_by=None
            )
        assert unknown.value.code == "invalid_argument"
        with pytest.raises(ControlError, match="unknown sink"):
            build_item(
                cfg,
                WorkloadAdmission(ask="x", profile="research", sink="fax"),
                item_id="api:k",
                requested_by=None,
            )
        with pytest.raises(ControlError, match="is not one profile `research` allows") as sink:
            build_item(
                cfg,
                WorkloadAdmission(ask="x", profile="research", sink="pr"),
                item_id="api:k",
                requested_by=None,
            )
        assert sink.value.code == "not_eligible"
        # No profile at all is a run with no profile, which is allowed.
        assert (
            build_item(cfg, WorkloadAdmission(ask="x"), item_id="api:k", requested_by=None).profile
            is None
        )

    def test_a_recipe_is_the_registry_entry_with_its_validated_target(self) -> None:
        request = ToolAdmission(recipe="entrygraph", parameters={"repository": "o/r"})
        key = target_key(request)
        assert key.startswith("api:") and ":entrygraph:" in key
        item = build_item(config(), request, item_id=key, requested_by=None)
        assert item.kind == "tool" and item.recipe == "entrygraph"
        assert item.recipe_target == "o/r" and item.repo == "o/r" and item.profile is None
        # The same parameters under the same key are the same target.
        assert target_key(
            ToolAdmission(recipe="entrygraph", parameters={"repository": "o/r"}, key="m")
        ) == target_key(
            ToolAdmission(recipe="entrygraph", parameters={"repository": "o/r"}, key="m")
        )

    def test_recipe_refusals_never_reach_the_registry_with_a_free_command(self) -> None:
        cfg = config()

        def refuse(request: ToolAdmission, code: str, match: str) -> None:
            with pytest.raises(ControlError, match=match) as excinfo:
                build_item(cfg, request, item_id="api:k", requested_by=None)
            assert excinfo.value.code == code

        refuse(
            ToolAdmission(recipe="shell", parameters={"command": "ls"}),
            "unknown_target",
            "unknown recipe",
        )
        refuse(
            ToolAdmission(recipe="entrygraph", parameters={"repository": "o/r", "command": "ls"}),
            "invalid_argument",
            "takes no parameter command",
        )
        refuse(
            ToolAdmission(recipe="entrygraph", parameters={"repository": ""}),
            "invalid_argument",
            "non-empty",
        )
        refuse(ToolAdmission(recipe="entrygraph"), "invalid_argument", "exactly one")
        refuse(
            ToolAdmission(
                recipe="entrygraph", parameters={"repository": "o/r", "url": "https://x/y"}
            ),
            "invalid_argument",
            "exactly one",
        )
        refuse(
            ToolAdmission(recipe="entrygraph", parameters={"repository": "o/else"}),
            "invalid_argument",
            "not an enabled configured repository",
        )
        refuse(
            ToolAdmission(recipe="entrygraph", parameters={"url": "http://x/y"}),
            "invalid_argument",
            "HTTPS",
        )
        disabled = config(entrygraph={"enabled": False})
        with pytest.raises(ControlError, match="disabled") as excinfo:
            build_item(
                disabled,
                ToolAdmission(recipe="entrygraph", parameters={"repository": "o/r"}),
                item_id="api:k",
                requested_by=None,
            )
        assert excinfo.value.code == "not_eligible"

    def test_an_issue_is_not_built_here(self) -> None:
        with pytest.raises(ControlError, match="built by its source"):
            build_item(config(), IssueAdmission("o/r", 4), item_id="x", requested_by=None)
        assert target_key(IssueAdmission("o/r", 4)) == "o/r#4"


class TestServiceAdmit:
    def test_an_admission_is_one_recorded_operation(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, config(home=str(tmp_path / "state")))
        h.loop.recover()
        service = ControlService(h.loop)
        outcome = service.admit(CLIENT, WorkloadAdmission(ask="x", profile="research"))
        assert outcome.fresh and outcome.item.state == "queued"
        assert outcome.operation_id is not None
        op = h.loop.operations.get(outcome.operation_id)
        assert op is not None and op.action == "item.admit" and op.state == "succeeded"
        assert op.target_key == outcome.item.item_id and op.actor["via"] == "api"
        assert op.request == {"form": "workload", "ask": "x", "profile": "research", "sink": None}
        assert h.dstore.get(outcome.item.item_id) is not None

    def test_idempotency_replays_and_conflicts(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, config(home=str(tmp_path / "state")))
        h.loop.recover()
        service = ControlService(h.loop)
        pair = ("local:cli_a:POST:/v1/items", "k")
        first = service.admit(CLIENT, WorkloadAdmission(ask="x"), idempotency=pair)
        with pytest.raises(OperationReplay) as replay:
            service.admit(CLIENT, WorkloadAdmission(ask="x"), idempotency=pair)
        assert replay.value.existing.id == first.operation_id
        with pytest.raises(IdempotencyConflict):
            service.admit(CLIENT, WorkloadAdmission(ask="y"), idempotency=pair)
        assert len(h.dstore.items()) == 1

    def test_the_capability_is_checked_first(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, config(home=str(tmp_path / "state")))
        reader = Principal(
            kind="client", id="cli_r", display="r", via="api", capabilities=frozenset({"runs:read"})
        )
        with pytest.raises(ControlError) as excinfo:
            ControlService(h.loop).admit(reader, WorkloadAdmission(ask="x"))
        assert excinfo.value.code == "forbidden" and h.dstore.items() == []

    def test_an_issue_needs_a_source_that_can_admit(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, config(home=str(tmp_path / "state")))
        h.loop.recover()
        service = ControlService(h.loop)
        with pytest.raises(ControlError) as excinfo:
            service.admit(CLIENT, IssueAdmission("o/r", 4))
        assert excinfo.value.code == "source_unavailable"
        with pytest.raises(ControlError) as unknown:
            service.admit(CLIENT, IssueAdmission("o/other", 4))
        assert unknown.value.code == "unknown_target"
