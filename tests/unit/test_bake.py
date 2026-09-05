"""`sbxloop bake` tests: scratch sandbox, install ladder, template save.

The install ladder execs are scripted (the real-pip path is covered in
test_worker_client); the template save/seed round trip runs against the
fake sbx's real template snapshot model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sbxloop
from sbxloop import toolchains
from sbxloop.config import Config
from sbxloop.errors import BakeError
from sbxloop.sbx.bake import (
    DEFAULT_TEMPLATE_REF,
    bake_record_path,
    bake_template,
    load_bake_record,
)
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from tests.conftest import FakeSbx

BOX = "bakebox"
VENV_PY = "/home/agent/.sbxloop/venv/bin/python"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(home=tmp_path / "state")


@pytest.fixture
def cli(fake_sbx: FakeSbx) -> SbxCLI:
    return SbxCLI(binary=str(fake_sbx.binary))


def script_install(
    fake_sbx: FakeSbx,
    *,
    runtime_rc: int = 0,
    missing: list[str] | None = None,
    languages: list[str] = ["python"],  # noqa: B006 - read only
    git_rc: int = 0,
    probe_rc: int = 0,
) -> None:
    # Page-size/ripgrep probe: scripted, else it runs on the host where the
    # answer varies by machine (16 KiB pages on Apple silicon).
    fake_sbx.script(f'exec {BOX} sh -c test "$(getconf PAGESIZE)"', returncode=0)
    # The ladder's per-tool probes (git #252, the configured languages):
    # likewise host-dependent when unscripted — and an unscripted miss
    # would run the toolchain's installer on the host.
    fake_sbx.script(f"exec {BOX} sh -c {toolchains.GIT.probe}", returncode=git_rc)
    for toolchain in toolchains.resolve(languages):
        fake_sbx.script(f"exec {BOX} sh -c {toolchain.probe}", returncode=0)
    # The batched probe the bake records from (#615): what landed.
    fake_sbx.script(
        f"exec {BOX} sh -c : sbxloop-toolchain-probe",
        returncode=probe_rc,
        stdout="".join(f"{name}\n" for name in missing or []),
    )
    fake_sbx.script(f"exec {BOX} sh -c sudo -n apt-get", returncode=0)
    fake_sbx.script(f"exec {BOX} python3 -m venv", returncode=0)
    fake_sbx.script(f"exec {BOX} /home/agent/.sbxloop/venv/bin/pip", returncode=0)
    fake_sbx.script(f"exec {BOX} {VENV_PY} -c", stdout=f"{sbxloop.__version__}\n")
    fake_sbx.script(f"exec {BOX} {VENV_PY} -m sbxloop_worker", returncode=64)
    fake_sbx.script(f"exec {BOX} {VENV_PY} -m copilot", returncode=runtime_rc)


class TestBakeHappyPath:
    def test_bake_saves_template_and_records(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx)
        record = bake_template(cli, config, name=BOX)

        assert record.ref == DEFAULT_TEMPLATE_REF
        assert record.worker_version == sbxloop.__version__
        assert record.python == VENV_PY
        assert record.runtime_cached

        # template saved AFTER the manifest was written into the VM
        assert ["template", "save", BOX, DEFAULT_TEMPLATE_REF] in fake_sbx.invocations("template")
        saved = fake_sbx.state / "templates" / "sbxloop-baked_latest" / "fs"
        manifest = json.loads((saved / "home/agent/.sbxloop/bake.json").read_text())
        assert manifest["worker_version"] == sbxloop.__version__
        assert manifest["python"] == VENV_PY
        assert manifest["runtime_cached"] is True
        assert manifest["languages"] == ["python"]
        assert record.languages == ("python",)

        # scratch sandbox removed; host record persisted for doctor
        assert not (fake_sbx.state / "sandboxes" / BOX).exists()
        assert bake_record_path(config).is_file()
        loaded = load_bake_record(config)
        assert loaded is not None and loaded.ref == record.ref

        # copilot runtime pre-cache ran under the installed interpreter
        assert [c for c in fake_sbx.invocations("exec") if "copilot" in c]

    def test_bake_applies_agent_network_allows_and_no_secrets(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx)
        bake_template(cli, config, name=BOX)
        allows = [p for p in fake_sbx.policies() if p[:2] == ["allow", "network"]]
        assert any("api.githubcopilot.com" in p for p in allows)
        # apt mirrors granted so the dev-tools ensure resolves during bake
        assert any("archive.ubuntu.com" in p for p in allows)
        # templates carry software, never secrets
        assert fake_sbx.secrets() == []

    def test_bake_allows_the_configured_toolchains_installer_hosts(
        self, cli: SbxCLI, tmp_path: Path, fake_sbx: FakeSbx
    ) -> None:
        # #616: the bake installs the configured toolchains, so their
        # installer hosts must be reachable in the scratch sandbox too.
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "sandbox": {"languages": ["go"]}}
        )
        script_install(fake_sbx, languages=["go"])
        bake_template(cli, config, name=BOX)
        allows = [p[2] for p in fake_sbx.policies() if p[:2] == ["allow", "network"]]
        assert "go.dev" in allows and "dl.google.com" in allows
        assert "nodejs.org" not in allows

    def test_bake_installs_and_records_the_configured_languages(
        self, cli: SbxCLI, tmp_path: Path, fake_sbx: FakeSbx
    ) -> None:
        """#615: the bake used to install the default (Python) whatever the
        config said, so a `languages = ["go"]` template shipped without
        Go. The configured set is provisioned and recorded — in the host
        record for doctor and in the in-VM manifest for provisioning."""
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "sandbox": {"languages": ["go"]}}
        )
        script_install(fake_sbx, languages=["go"])
        record = bake_template(cli, config, name=BOX)
        probes = [c[-1] for c in fake_sbx.invocations("exec") if c[2:4] == ["sh", "-c"]]
        assert toolchains.GO.probe in probes
        assert toolchains.PYTHON.probe not in probes
        assert record.languages == ("go",)
        saved = fake_sbx.state / "templates" / "sbxloop-baked_latest" / "fs"
        manifest = json.loads((saved / "home/agent/.sbxloop/bake.json").read_text())
        assert manifest["languages"] == ["go"]

    def test_bake_records_only_what_landed(
        self, cli: SbxCLI, tmp_path: Path, fake_sbx: FakeSbx
    ) -> None:
        # The ensure is best-effort: a toolchain whose installer failed is
        # not in the record, so doctor says the template lacks it rather
        # than trusting the attempt.
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "sandbox": {"languages": ["python", "go"]}}
        )
        script_install(fake_sbx, languages=["python", "go"], missing=["go"])
        reports: list[str] = []
        record = bake_template(cli, config, name=BOX, progress=reports.append)
        assert record.languages == ("python",)
        assert record.git is True
        assert any("not on PATH after the install: go" in r for r in reports)

    def test_bake_fails_when_the_toolchain_probe_cannot_answer(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        # Fail closed: a record that guessed would have doctor vouch for a
        # template nobody verified.
        script_install(fake_sbx, probe_rc=127)
        with pytest.raises(BakeError, match="could not probe"):
            bake_template(cli, config, name=BOX)

    def test_baked_template_seeds_new_sandboxes(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        """Round trip through the fake's template model: a sandbox created
        from the baked ref starts with the bake manifest in its fs."""
        script_install(fake_sbx)
        record = bake_template(cli, config, name=BOX)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        cli.create(
            SandboxSpec(name="fromtpl", role="agent", workspace=workspace, template=record.ref)
        )
        fs = fake_sbx.sandbox_fs("fromtpl")
        assert (fs / "home/agent/.sbxloop/bake.json").is_file()
        # the mount model still points at the NEW sandbox's workspace
        assert (fs / "workspace").resolve() == workspace.resolve()


class TestBakeOptions:
    def test_runtime_cache_failure_is_nonfatal(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx, runtime_rc=1)
        record = bake_template(cli, config, name=BOX)
        assert not record.runtime_cached
        saved = fake_sbx.state / "templates" / "sbxloop-baked_latest" / "fs"
        manifest = json.loads((saved / "home/agent/.sbxloop/bake.json").read_text())
        assert manifest["runtime_cached"] is False

    def test_no_runtime_cache_skips_download(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx)
        bake_template(cli, config, name=BOX, cache_runtime=False)
        assert not [c for c in fake_sbx.invocations("exec") if "copilot" in c]

    def test_custom_ref_and_base_template(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx)
        record = bake_template(cli, config, name=BOX, ref="me/mine:v2", base_template="base:v1")
        assert record.ref == "me/mine:v2"
        creates = fake_sbx.invocations("create")
        assert any("base:v1" in arg for c in creates for arg in c)
        assert ["template", "save", BOX, "me/mine:v2"] in fake_sbx.invocations("template")

    def test_keep_retains_scratch_sandbox(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        script_install(fake_sbx)
        bake_template(cli, config, name=BOX, keep=True)
        assert (fake_sbx.state / "sandboxes" / BOX).exists()


class TestBakeFailure:
    def test_install_failure_cleans_up_and_raises(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        # ladder fully fails: venv, apt heal, and user-site pip all refuse
        fake_sbx.script(f"exec {BOX} sh -c sudo -n apt-get", returncode=1, stderr="no apt")
        fake_sbx.script(f"exec {BOX} python3 -m venv", returncode=1, stderr="no venv")
        fake_sbx.script(f"exec {BOX} python3 -m pip install", returncode=1, stderr="pip broken")
        with pytest.raises(BakeError, match="bake failed"):
            bake_template(cli, config, name=BOX)
        assert not (fake_sbx.state / "sandboxes" / BOX).exists()
        assert not bake_record_path(config).is_file()
        assert fake_sbx.invocations("template") == []

    def test_bake_records_git_present(self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx) -> None:
        # #252: doctor's "git in template" row reads this — a bake that
        # captured git means runs skip the per-provision apt top-up.
        script_install(fake_sbx)
        record = bake_template(cli, config, name=BOX)
        assert record.git is True
        loaded = load_bake_record(config)
        assert loaded is not None and loaded.git is True

    def test_bake_records_git_missing_after_failed_ensure(
        self, cli: SbxCLI, config: Config, fake_sbx: FakeSbx
    ) -> None:
        # The ensure is best-effort; the record reports what actually landed
        # in the template, not what was attempted.
        script_install(fake_sbx, missing=["git"], git_rc=1)
        record = bake_template(cli, config, name=BOX)
        assert record.git is False
        apt = [" ".join(c) for c in fake_sbx.invocations("exec") if "apt-get" in " ".join(c)]
        assert any("git" in cmd.split() for cmd in apt), apt

    def test_load_bake_record_without_git_field(self, config: Config) -> None:
        # Records from before the field existed must still load: doctor
        # renders "not recorded", it does not refuse the whole record.
        path = bake_record_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ref": DEFAULT_TEMPLATE_REF,
                    "worker_version": sbxloop.__version__,
                    "python": VENV_PY,
                    "runtime_cached": True,
                    "baked_at": 0.0,
                }
            )
        )
        loaded = load_bake_record(config)
        assert loaded is not None and loaded.git is None and loaded.languages is None

    def test_load_bake_record_tolerates_garbage(self, config: Config) -> None:
        path = bake_record_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert load_bake_record(config) is None
