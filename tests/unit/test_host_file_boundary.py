"""Host prompt reads must not turn repository links into access to host secrets."""

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.deliver import pr_template
from sbxloop.engine.phases import PhaseRunner
from sbxloop.paths import SbxloopHome
from tests.unit.test_repocontext import GRAPH, PromptAgent


@pytest.mark.parametrize("location", ["dedicated", "run"])
def test_host_secret_does_not_enter_decompose_prompt(tmp_path: Path, location: str) -> None:
    home = SbxloopHome(tmp_path / "home")
    workspace = (
        home.workspace_for("customer/project")
        if location == "dedicated"
        else home.run_workspace("audit")
    )
    workspace.mkdir(parents=True)
    home.secrets_env.parent.mkdir(parents=True)
    marker = "AUDIT_SYNTHETIC_HOST_SECRET=never-a-real-key"
    home.secrets_env.write_text(marker)
    (workspace / "AGENTS.md").symlink_to("../../../config/secrets.env")
    agent = PromptAgent([GRAPH])
    PhaseRunner(agent, Config(), "audit", "do the task", workspace=workspace).decompose()  # type: ignore[arg-type]
    assert marker not in agent.prompts[0][1]


def test_pr_template_does_not_read_host_secret(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "home")
    workspace = home.run_workspace("audit")
    (workspace / ".github").mkdir(parents=True)
    home.secrets_env.parent.mkdir(parents=True)
    home.secrets_env.write_text("AUDIT_SYNTHETIC_HOST_SECRET=template-reproduction")
    (workspace / ".github/pull_request_template.md").symlink_to("../../../../config/secrets.env")
    assert pr_template(workspace) is None


def test_regular_pr_upload_cannot_follow_a_replacement_link(tmp_path: Path) -> None:
    from sbxloop.deliver import _blob_upload
    from sbxloop.errors import DeliveryError
    from sbxloop.hostgit import WorkspaceChange

    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("synthetic host secret")
    # The file was regular when the change list was taken, then replaced.
    change = WorkspaceChange(path="result.txt", status="added", mode="100644")
    (root / "result.txt").symlink_to(secret)
    with pytest.raises(DeliveryError, match="cannot safely read"):
        _blob_upload(root, change)


def test_artifact_copy_refuses_a_link_to_host_files(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from sbxloop.engine.engine import LoopEngine
    from sbxloop.engine.model import TaskOutput, TaskRecord, TaskSpec

    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("synthetic host secret")
    (root / "result.txt").symlink_to(secret)
    config = Config.model_validate({"home": str(tmp_path / "home")})
    engine = SimpleNamespace(config=config)
    pipeline = SimpleNamespace(pair=SimpleNamespace(mounted=True, workspace=root))
    run = SimpleNamespace(run_id="audit", kind="workload", workspace=root)
    task = TaskRecord(
        spec=TaskSpec(id="t1", title="report"), output=TaskOutput(files=["result.txt"])
    )
    with pytest.raises(OSError):
        LoopEngine._stage_files(engine, pipeline, run, [task])
    assert not (config.paths.run_artifacts("audit") / "result.txt").exists()
