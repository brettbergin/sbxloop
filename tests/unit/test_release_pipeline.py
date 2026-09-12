"""Release batches and deploy decisions against the repository's GitHub fake."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from sbxloop.config import Config
from tests.fakes.fake_github import FakeGithub

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "release_pipeline", ROOT / "scripts/release_pipeline.py"
)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def release(version: str, sha: str, *, draft: bool = False) -> dict:
    return {
        "id": int(version.split(".")[-1]) + 1,
        "tag_name": f"v{version}",
        "draft": draft,
        "prerelease": False,
        "published_at": None if draft else "2026-09-12T00:00:00Z",
        "assets": [
            {"name": name, "state": "uploaded", "size": 10}
            for name in pipeline.distribution_names(version)
        ],
    }


@pytest.fixture
def api():
    fake = FakeGithub()
    fake.release_heads = ["b" * 40]
    fake.release_tags = [{"name": "v1.0.1", "commit": {"sha": "a" * 40}}]
    fake.release_payloads = [release("1.0.1", "a" * 40)]
    return pipeline.Github("o/r", request=fake.release_request), fake


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_isolated_merge_waits_three_minutes(api):
    client, _ = api
    clock = Clock()
    plan = pipeline.batch(client, manual=False, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 180
    assert plan == {"action": "new", "version": "1.0.2", "sha": "b" * 40}


def test_new_merges_reset_quiet_time_and_use_latest_sha(api):
    client, fake = api
    fake.release_heads = ["b" * 40] * 5 + ["c" * 40]
    clock = Clock()
    plan = pipeline.batch(client, manual=False, clock=clock.time, sleep=clock.sleep)
    assert clock.now > 180
    assert plan["sha"] == "c" * 40
    assert plan["version"] == "1.0.2"


def test_continuous_merges_do_not_reset_maximum_wait(api):
    client, fake = api
    fake.release_heads = [f"{n:040x}" for n in range(200)]
    clock = Clock()
    pipeline.batch(client, manual=False, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 1800


def test_manual_release_bypasses_wait(api):
    client, _ = api
    clock = Clock()
    assert (
        pipeline.batch(client, manual=True, clock=clock.time, sleep=clock.sleep)["action"] == "new"
    )
    assert clock.now == 0


def test_published_head_is_a_noop_before_wait(api):
    client, fake = api
    fake.release_heads = ["a" * 40]
    clock = Clock()
    assert (
        pipeline.batch(client, manual=False, clock=clock.time, sleep=clock.sleep)["action"]
        == "noop"
    )
    assert clock.now == 0


@pytest.mark.parametrize("has_draft", [False, True])
def test_partial_publication_reuses_reserved_version_even_after_main_moves(api, has_draft):
    client, fake = api
    fake.release_tags.append({"name": "v1.0.2", "commit": {"sha": "b" * 40}})
    if has_draft:
        fake.release_payloads.append(release("1.0.2", "b" * 40, draft=True))
    fake.release_heads = ["c" * 40]
    clock = Clock()
    assert pipeline.batch(client, manual=False, clock=clock.time, sleep=clock.sleep) == {
        "action": "resume",
        "version": "1.0.2",
        "sha": "b" * 40,
    }
    assert clock.now == 0


def test_release_selection_is_numeric_and_ignores_drafts(api):
    client, fake = api
    fake.release_payloads += [release("1.0.9", "b" * 40), release("1.0.10", "c" * 40)]
    fake.release_payloads.append(release("1.0.11", "d" * 40, draft=True))
    fake.release_tags.append({"name": "v1.0.10", "commit": {"sha": "c" * 40}})
    assert client.latest()["tag_name"] == "v1.0.10"


def test_incomplete_published_release_fails_closed(api):
    client, fake = api
    fake.release_payloads[0]["assets"].pop()
    with pytest.raises(ValueError, match="assets"):
        client.latest()


@pytest.mark.parametrize("bad", ["../1.0.2", "1.0.2\nchanged=true", "1.0.2rc1", "01.0.2"])
def test_rejects_invalid_versions(bad):
    with pytest.raises(ValueError):
        pipeline.version_key(bad)


def test_deploy_cooldown_and_manual_override():
    state = {"finished_at": 1000, "blocked_through": "", "in_progress": False}
    assert pipeline.deploy_decision("1.0.1", "1.0.2", state, now=1100, manual=False) == "cooldown"
    assert pipeline.deploy_decision("1.0.1", "1.0.2", state, now=2800, manual=False) == "deploy"
    assert pipeline.deploy_decision("1.0.1", "1.0.2", state, now=1100, manual=True) == "deploy"


def test_installed_release_and_automatic_downgrades_do_not_restart():
    assert pipeline.deploy_decision("1.0.2", "1.0.2", {}, now=0, manual=True) == "current"
    assert pipeline.deploy_decision("1.0.3", "1.0.2", {}, now=0, manual=False) == "older"
    assert pipeline.deploy_decision("1.0.3", "1.0.2", {}, now=0, manual=True) == "deploy"


def test_failed_release_is_not_retried_automatically():
    state = {"blocked_through": "1.0.3", "finished_at": 0, "in_progress": False}
    assert pipeline.deploy_decision("1.0.1", "1.0.3", state, now=4000, manual=False) == "blocked"
    assert pipeline.deploy_decision("1.0.1", "1.0.4", state, now=4000, manual=False) == "deploy"
    assert pipeline.deploy_decision("1.0.1", "1.0.3", state, now=4000, manual=True) == "deploy"


def test_interrupted_mutation_requires_manual_recovery():
    with pytest.raises(ValueError, match="interrupted"):
        pipeline.deploy_decision("1.0.1", "1.0.2", {"in_progress": True}, now=4000, manual=False)
    assert (
        pipeline.deploy_decision("1.0.2", "1.0.2", {"in_progress": True}, now=4000, manual=True)
        == "deploy"
    )


def test_partial_pypi_retry_restores_original_bytes(api, tmp_path, monkeypatch):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    plan = {"version": "1.0.2", "sha": "b" * 40, "action": "new"}
    dist = tmp_path / "original"
    dist.mkdir()
    for name in pipeline.distribution_names("1.0.2"):
        (dist / name).write_bytes(f"original {name}".encode())
    pipeline.stage(client, plan, dist)
    assert fake.release_commands[-1][-1].endswith(pipeline.MANIFEST)
    assert fake.release_payloads[-1]["draft"] is True
    retry = tmp_path / "retry"
    assert pipeline.restore_staged(client, plan, retry)
    for name in pipeline.distribution_names("1.0.2"):
        assert (retry / name).read_bytes() == (dist / name).read_bytes()
    commands_before = len(fake.release_commands)
    pipeline.stage(client, plan, retry)
    assert len(fake.release_commands) == commands_before  # no replacement uploads


def test_corrupt_staged_bytes_stop_publication(api, tmp_path, monkeypatch):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    plan = {"version": "1.0.2", "sha": "b" * 40}
    dist = tmp_path / "original"
    dist.mkdir()
    for name in pipeline.distribution_names("1.0.2"):
        (dist / name).write_bytes(b"original")
    pipeline.stage(client, plan, dist)
    fake.release_files["v1.0.2", pipeline.distribution_names("1.0.2")[0]] = b"corrupt"
    with pytest.raises(ValueError, match="manifest"):
        pipeline.restore_staged(client, plan, tmp_path / "retry")


def test_partial_staging_can_resume_but_published_assets_cannot_be_replaced(
    api, tmp_path, monkeypatch
):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    plan = {"version": "1.0.2", "sha": "b" * 40}
    fake.release_payloads.append({"tag_name": "v1.0.2", "draft": True, "assets": []})
    assert not pipeline.restore_staged(client, plan, tmp_path)
    fake.release_payloads[-1]["draft"] = False
    with pytest.raises(ValueError, match="published"):
        pipeline.stage(client, plan, tmp_path)


def test_api_outage_and_divergent_release_fail_closed(api):
    client, fake = api
    fake.release_api_error = OSError("unavailable")
    with pytest.raises(OSError):
        pipeline.batch(client, manual=True)
    fake.release_api_error = None
    fake.release_compare_status = "diverged"
    with pytest.raises(ValueError, match="ancestor"):
        pipeline.batch(client, manual=True)
    with pytest.raises(ValueError, match="main"):
        client.latest()


@pytest.mark.parametrize("published", [False, True])
def test_receipt_distinguishes_noop_from_publication(published):
    assert (
        pipeline.receipt_valid(
            {"schema": 1, "published": published, "version": "1.0.2", "sha": "b" * 40}
        )
        is published
    )


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"schema": 1, "published": "false"},
        {"schema": 1, "published": True, "version": "bad", "sha": "b" * 40},
    ],
)
def test_malformed_receipt_is_not_assumed_to_be_a_noop(data):
    with pytest.raises(ValueError):
        pipeline.receipt_valid(data)


def test_pagination_does_not_hide_newest_version(api):
    client, fake = api
    fake.release_payloads = [release(f"1.0.{n}", "a" * 40) for n in range(120)]
    fake.release_tags = [{"name": "v1.0.119", "commit": {"sha": "a" * 40}}]
    assert client.latest()["tag_name"] == "v1.0.119"


def test_corrupt_state_is_not_treated_as_first_install(tmp_path):
    path = tmp_path / "deploy.json"
    path.write_text("broken")
    with pytest.raises(ValueError):
        pipeline.read_state(path)
    path.write_text(json.dumps({"finished_at": "yesterday"}))
    with pytest.raises(ValueError):
        pipeline.read_state(path)


def test_release_workflow_has_quiet_batch_and_noop_receipt():
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert "release_pipeline.py batch" in text
    assert "release-result" in text
    assert "queue: max" in text
    assert "schedule:" in text  # finish later main changes after an older reservation
    assert "branches: [main]" in text  # isolated PRs do not wait for that schedule


def test_deploy_refreshes_after_drain_before_fetch_and_mutation():
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    names = [
        "Wait for the daemon to go idle",
        "Refresh the target after draining",
        "Fetch the release wheels",
        "Upgrade",
    ]
    positions = [text.index(f"- name: {name}\n") for name in names]
    assert positions == sorted(positions)
    assert "github.workflow_sha" in text
    assert "queue: max" in text
    assert "schedule:" in text  # deferred work is retried even without another merge


def test_deploy_notice_overrides_the_channel_through_the_old_notifier_signature(tmp_path):
    config = Config.model_validate(
        {
            "home": str(tmp_path),
            "mattermost": {
                "url": "https://mm.example.test",
                "channel_id": "c" * 26,
            },
        }
    )
    calls = []

    # This is deliberately the pre-channel-override post_notice signature.
    def old_post(config, text):
        calls.append((config, text))
        return ("mattermost", config.mattermost.channel_ref)

    posted = pipeline.deploy_notice(config, "deploying", "d" * 26, post=old_post)

    assert posted == ("mattermost", "d" * 26)
    assert len(calls) == 1
    sent_config, sent_text = calls[0]
    assert sent_text == "deploying"
    assert sent_config.mattermost.channel_ref == "d" * 26
    assert config.mattermost.channel_ref == "c" * 26


def test_deploy_notify_command_uses_the_installed_notifier_without_github(
    tmp_path, monkeypatch, capsys
):
    import sbxloop.config as config_module
    from sbxloop.daemon import notify

    config = Config.model_validate(
        {
            "home": str(tmp_path),
            "mattermost": {
                "url": "https://mm.example.test",
                "channel_id": "c" * 26,
            },
        }
    )
    sent = []

    def old_post(config, text):
        sent.append((config.mattermost.channel_ref, text))
        return notify.Posted("mattermost", config.mattermost.channel_ref)

    monkeypatch.setattr(config_module, "load_secrets_env", lambda: None)
    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(notify, "post_notice", old_post)
    monkeypatch.setattr(sys, "argv", ["release_pipeline.py", "deploy-notify"])
    monkeypatch.setenv("DEPLOY_NOTICE", "deploying")
    monkeypatch.setenv("DEPLOY_CHANNEL", "d" * 26)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    pipeline.main()

    assert sent == [("d" * 26, "deploying")]
    assert capsys.readouterr().out == f"posted to mattermost channel {'d' * 26}\n"


@pytest.fixture
def cli(api, tmp_path, monkeypatch):
    client, fake = api
    current = ["1.0.1"]
    fake.release_tags.append({"name": "v1.0.2", "commit": {"sha": "b" * 40}})
    fake.release_payloads.append(release("1.0.2", "b" * 40))
    monkeypatch.setattr(pipeline, "Github", lambda repo: client)
    monkeypatch.setattr(pipeline, "command", lambda *args: f"sbxloop {current[0]}")
    monkeypatch.setattr(pipeline.time, "time", lambda: 10000)
    for key, value in {
        "GITHUB_REPOSITORY": "o/r",
        "SBXLOOP_HOME": str(tmp_path),
        "VENV_SBXLOOP": "unused",
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "MANUAL": "false",
        "INPUT_VERSION": "",
        "HEALTH": "",
        "RESTORED": "",
    }.items():
        monkeypatch.setenv(key, value)

    def run(mode, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        (tmp_path / "outputs").write_text("")
        monkeypatch.setattr(sys, "argv", ["release_pipeline.py", mode])
        pipeline.main()
        return dict(line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines())

    return run, fake, current, tmp_path


def test_deploy_cli_coalesces_during_drain_then_next_wakeup_does_nothing(cli):
    run, fake, current, home = cli
    assert run("deploy-check")["version"] == "1.0.2"
    fake.release_payloads.append(release("1.0.3", "c" * 40))
    fake.release_tags.append({"name": "v1.0.3", "commit": {"sha": "c" * 40}})
    final = run("deploy-check")  # the second selection is after the drain
    assert final["version"] == "1.0.3"
    run("deploy-start", VERSION=final["version"])
    assert pipeline.read_state(home / "state/deploy.json")["in_progress"]
    current[0] = final["version"]
    run("deploy-finish", HEALTH="success")
    assert run("deploy-check")["changed"] == "false"


def test_deploy_cli_persists_failure_suppression_after_verified_rollback(cli):
    run, _, _, home = cli
    run("deploy-start", VERSION="1.0.2")
    run("deploy-finish", HEALTH="failure", RESTORED="true")
    state = pipeline.read_state(home / "state/deploy.json")
    assert state == {"finished_at": 10000, "blocked_through": "1.0.2", "in_progress": False}
    assert run("deploy-check")["reason"] == "blocked"


def test_deploy_cli_manual_pin_survives_new_release_and_rollback_is_not_undone(cli):
    run, fake, current, home = cli
    assert run("deploy-check", MANUAL="true", INPUT_VERSION="v1.0.2")["version"] == "1.0.2"
    fake.release_payloads.append(release("1.0.3", "c" * 40))
    fake.release_tags.append({"name": "v1.0.3", "commit": {"sha": "c" * 40}})
    current[0] = "1.0.3"
    assert run("deploy-check")["version"] == "1.0.2"
    run("deploy-start", VERSION="1.0.2")
    current[0] = "1.0.2"
    run("deploy-finish", HEALTH="success")
    assert pipeline.read_state(home / "state/deploy.json")["blocked_through"] == "1.0.3"
    assert run("deploy-check", MANUAL="false", INPUT_VERSION="")["reason"] == "blocked"


def test_workflow_receipts_and_mutations_use_the_frozen_result():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["deploy"]["steps"]}
    for name in ("Fetch the release wheels", "Upgrade", "Health check"):
        assert steps[name]["env"]["VERSION"] == "${{ steps.final.outputs.version }}"
    assert (
        steps["Compare against what is installed"]["if"]
        == "steps.receipt.outputs.eligible == 'true'"
    )
    assert "steps.installed.outputs.changed" in steps["Report"]["if"]
    assert (
        "steps.final.outputs.version || steps.installed.outputs.version"
        in steps["Report"]["env"]["VERSION"]
    )
    helper = steps["Load the trusted workflow helper"]
    assert helper["env"]["WORKFLOW_SHA"] == "${{ github.workflow_sha }}"
    assert "head_sha" not in helper["run"]
