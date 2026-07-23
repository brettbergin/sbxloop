"""TaskGraph and model validation tests."""

import pytest
from pydantic import ValidationError

from sdxloop.engine.model import TaskGraph, TaskSpec, Verdict


def spec(id: str, deps: list[str] | None = None) -> dict[str, object]:
    return {"id": id, "title": id.upper(), "depends_on": deps or []}


class TestTaskGraph:
    def test_valid_graph_topo_order(self) -> None:
        graph = TaskGraph.model_validate(
            {"tasks": [spec("t3", ["t1", "t2"]), spec("t1"), spec("t2", ["t1"])]}
        )
        assert [t.id for t in graph.topo_order()] == ["t1", "t2", "t3"]

    def test_topo_order_stable_for_independent_tasks(self) -> None:
        graph = TaskGraph.model_validate({"tasks": [spec("b"), spec("a"), spec("c")]})
        assert [t.id for t in graph.topo_order()] == ["b", "a", "c"]  # authored order

    def test_empty_graph_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one task"):
            TaskGraph.model_validate({"tasks": []})

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate task ids"):
            TaskGraph.model_validate({"tasks": [spec("t1"), spec("t1")]})

    def test_unknown_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown tasks"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["ghost"])]})

    def test_self_dependency_rejected(self) -> None:
        with pytest.raises(ValidationError, match="depends on itself"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t1"])]})

    def test_cycle_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            TaskGraph.model_validate({"tasks": [spec("t1", ["t2"]), spec("t2", ["t1"])]})


class TestVerdict:
    def test_verdict_literals(self) -> None:
        assert Verdict(verdict="pass").issues == []
        with pytest.raises(ValidationError):
            Verdict(verdict="maybe")  # type: ignore[arg-type]

    def test_task_spec_defaults(self) -> None:
        t = TaskSpec(id="t1", title="X")
        assert t.acceptance_criteria == []
        assert t.verify_commands == []
