"""Resource allocations are mandatory, including on non-run creation paths."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner


def test_default_allocations_cover_every_provisioned_role(tmp_path: Path) -> None:
    config = Config(home=tmp_path)
    provisioner = Provisioner(SbxCLI(), config)
    agent, github = provisioner.build_specs("r1", tmp_path)
    concierge = provisioner.agent_only_spec("concierge", tmp_path)
    poller = provisioner.github_only_spec("poller", tmp_path)
    service = provisioner.service_spec("r1", tmp_path, [])
    assert [
        (s.resources.cpus, s.resources.memory) for s in (agent, concierge, github, poller, service)
    ] == [(6, "12g"), (2, "4g"), (1, "2g"), (1, "2g"), (1, "2g")]


def test_repo_overrides_only_change_the_run_agent(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "home": tmp_path,
            "sandbox": {"cpus": 4, "memory": "8g", "concierge_cpus": 3},
            "github": {"repos": [{"repo": "org/project", "cpus": 8, "memory": "16g"}]},
        }
    )
    provisioner = Provisioner(SbxCLI(), config)
    agent, github = provisioner.build_specs("r1", tmp_path, "org/project")
    assert (agent.resources.cpus, agent.resources.memory) == (8, "16g")
    assert github.resources.cpus == 1
    assert provisioner.agent_only_spec("concierge", tmp_path).resources.cpus == 3
    assert config.sandbox_resources_for("agent", "org/other").cpus == 4


@pytest.mark.parametrize("key", ["cpus", "concierge_cpus", "github_cpus", "service_cpus"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_cpu_limits_cannot_be_unbounded_or_coerced(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"sandbox": {key: value}})


@pytest.mark.parametrize("key", ["memory", "concierge_memory", "github_memory", "service_memory"])
@pytest.mark.parametrize("value", ["", "0g", "-1g", "auto", "50%", None, 8])
def test_memory_limits_must_be_explicit_positive_sizes(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"sandbox": {key: value}})


def test_equivalent_memory_limits_have_the_same_allocation() -> None:
    a = Config.model_validate({"sandbox": {"memory": "8192M"}})
    b = Config.model_validate({"sandbox": {"memory": "8g"}})
    assert a.sandbox_resources_for("agent") == b.sandbox_resources_for("agent")


def test_create_always_sends_limits_and_never_retries_without_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sbxloop.errors import SbxError
    from sbxloop.sbx.models import SandboxSpec

    calls = []

    def reject(self, *args, **kwargs):
        calls.append(args)
        raise SbxError("unknown flag: --cpus", returncode=1)

    monkeypatch.setattr(SbxCLI, "run", reject)
    with pytest.raises(SbxError, match="will not retry without resource limits"):
        SbxCLI().create(SandboxSpec(name="test", role="agent", workspace=tmp_path))
    assert calls == [
        ("create", "--name=test", "--cpus", "6", "--memory", "12g", "shell", str(tmp_path))
    ]


def test_reuse_requires_a_host_record_and_matching_vm_identity(tmp_path: Path) -> None:
    from sbxloop.errors import ProvisionError
    from sbxloop.resources import SandboxResources
    from sbxloop.sbx.allocations import record_allocation, require_allocation

    class Box:
        name = "test"
        cli = SbxCLI()

        def __init__(self):
            self.files = {}

        def mkdirs(self, *paths):
            pass

        def write_text(self, path, content):
            self.files[path] = content

        def read_text(self, path):
            return self.files[path]

    box = Box()
    home = Config(home=tmp_path).paths
    resources = SandboxResources()
    with pytest.raises(ProvisionError, match="recreat"):
        require_allocation(home, box, resources)
    record_allocation(home, box, resources)
    require_allocation(home, box, resources)
    # A damaged receipt must not acquire today's defaults during parsing.
    import json

    receipt = next(home.sandbox_allocations.glob("*.json"))
    original = receipt.read_text()
    damaged = json.loads(original)
    damaged["resources"] = {}
    receipt.write_text(json.dumps(damaged))
    with pytest.raises(ProvisionError, match="recreat"):
        require_allocation(home, box, resources)
    receipt.write_text(original)
    with pytest.raises(ProvisionError, match="recreat"):
        require_allocation(home, box, SandboxResources(cpus=2))
    for path in box.files:
        box.files[path] = "another-vm"
    with pytest.raises(ProvisionError, match="recreat"):
        require_allocation(home, box, resources)


def test_packaged_toml_and_all_presets_keep_live_resource_defaults() -> None:
    import tomllib

    from sbxloop.data import config_presets, render_config_template

    for preset in (None, *config_presets()):
        parsed = tomllib.loads(render_config_template(preset))
        assert parsed["sandbox"]["cpus"] == 6
        assert parsed["sandbox"]["memory"] == "12g"
        assert parsed["sandbox"]["concierge_cpus"] == 2
        assert parsed["sandbox"]["github_memory"] == "2g"
        Config.model_validate(parsed)
        if preset is not None:
            assert config_presets()[preset].splitlines()[0] in render_config_template(preset)


def test_doctor_reports_requested_allocations_and_repo_overrides() -> None:
    from sbxloop.cli.doctor import sandbox_resource_checks

    config = Config.model_validate({"github": {"repos": [{"repo": "org/project", "cpus": 8}]}})
    rows = sandbox_resource_checks(config)
    assert len(rows) == 5
    assert "6 CPUs, 12g" in rows[0].detail
    assert "2 CPUs, 4g" in rows[1].detail
    assert "8 CPUs, 12g" in rows[-1].detail


@pytest.mark.parametrize("repo_override", [None, 3])
def test_resume_uses_current_resource_limits_but_keeps_run_rules(tmp_path, repo_override):
    from sbxloop.engine.engine import LoopEngine

    saved = Config.model_validate(
        {
            "home": tmp_path,
            "sandbox": {"cpus": 8, "memory": "16g"},
            "github": {"repos": [{"repo": "org/project", "cpus": 10, "memory": "20g"}]},
            "budgets": {"max_tasks": 7},
        }
    )
    current = Config.model_validate(
        {
            "home": tmp_path,
            "sandbox": {"cpus": 2, "memory": "4g", "service_memory": "3g"},
            "github": {"repos": [{"repo": "org/project", "cpus": repo_override}]},
        }
    )
    engine = LoopEngine(current)
    try:
        engine.store.create_run("r1", "task", saved.model_dump_json())
        engine._rehydrate_config("r1")
        allocation = engine.config.sandbox_resources_for("agent", "org/project")
        assert allocation.cpus == (repo_override or 2)
        assert allocation.memory == "4g"
        assert engine.config.sandbox_resources_for("service").memory == "3g"
        assert engine.config.budgets.max_tasks == 7
    finally:
        engine.store.close()
