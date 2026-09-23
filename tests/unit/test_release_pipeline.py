"""Release batches and deploy decisions against the repository's GitHub fake."""

from __future__ import annotations

import hashlib
import importlib.util
import io
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


def ci_run(run_id=1, **changes):
    return {
        "id": run_id,
        "workflow_id": 42,
        "path": ".github/workflows/ci.yml",
        "head_sha": "b" * 40,
        "head_branch": "main",
        "event": "push",
        "head_repository": {"full_name": "o/r"},
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        **changes,
    }


def green_ci(fake):
    fake.release_ci_runs = [ci_run()]
    fake.release_ci_jobs[1] = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in ("lint", "typecheck", "build", "test (3.13)", "test (3.14)", "verified")
    ]


def test_release_reuses_successful_ci_only_for_the_frozen_sha(api):
    client, fake = api
    green_ci(fake)
    assert pipeline.verification(client, "b" * 40) == 1
    assert pipeline.verification(client, "c" * 40) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"event": "pull_request"},
        {"head_branch": "feature"},
        {"head_repository": {"full_name": "fork/r"}},
        {"workflow_id": 99},
        {"path": ".github/workflows/impostor.yml"},
    ],
)
def test_release_rejects_ci_from_another_trust_context(api, changes):
    client, fake = api
    green_ci(fake)
    fake.release_ci_runs = [ci_run(**changes)]
    assert pipeline.verification(client, "b" * 40) is None


@pytest.mark.parametrize("result", ["failure", "timed_out", "action_required", "skipped"])
def test_release_never_substitutes_older_green_ci_for_newer_failed_run(api, result):
    client, fake = api
    green_ci(fake)
    fake.release_ci_runs.append(ci_run(2, conclusion=result))
    with pytest.raises(ValueError, match="CI"):
        pipeline.verification(client, "b" * 40)


def test_cancelled_and_legacy_ci_require_full_verification(api):
    client, fake = api
    green_ci(fake)
    fake.release_ci_runs[0]["conclusion"] = "cancelled"
    assert pipeline.verification(client, "b" * 40) is None
    fake.release_ci_runs[0]["conclusion"] = "success"
    fake.release_ci_jobs[1].pop()  # Pre-verdict CI cannot supply proof.
    assert pipeline.verification(client, "b" * 40) is None


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_successful_workflow_does_not_hide_an_unsuccessful_required_job(api, result):
    client, fake = api
    green_ci(fake)
    fake.release_ci_jobs[1][0]["conclusion"] = result
    with pytest.raises(ValueError, match="lint"):
        pipeline.verification(client, "b" * 40)


def test_ci_evidence_is_paginated(api):
    client, fake = api
    green_ci(fake)
    fake.release_ci_jobs[1] = [
        {"name": f"other-{i}", "status": "completed", "conclusion": "success"} for i in range(100)
    ] + fake.release_ci_jobs[1]
    assert pipeline.verification(client, "b" * 40) == 1


def test_release_waits_for_running_ci_but_never_assumes_a_timeout_is_success(api):
    client, fake = api
    green_ci(fake)
    fake.release_ci_runs[0].update(status="in_progress", conclusion=None)
    clock = Clock()
    with pytest.raises(TimeoutError, match="CI"):
        pipeline.verification(client, "b" * 40, clock=clock.time, sleep=clock.sleep, timeout=30)
    assert clock.now == 30

    def finish(seconds):
        clock.sleep(seconds)
        fake.release_ci_runs[0].update(status="completed", conclusion="success")

    assert pipeline.verification(client, "b" * 40, clock=clock.time, sleep=finish) == 1


def test_ci_api_failure_stops_release(api):
    client, fake = api
    fake.release_api_error = OSError("unavailable")
    with pytest.raises(OSError):
        pipeline.verification(client, "b" * 40)


@pytest.mark.parametrize("status", ["queued", "pending", "waiting", "requested"])
def test_automatic_wakeup_coalesces_a_pending_automatic_release(api, monkeypatch, status):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    fake.release_workflow_runs = [
        ci_run(
            path=".github/workflows/release.yml",
            event="workflow_dispatch",
            display_title="Automatic release",
            status=status,
            conclusion=None,
        )
    ]
    assert not pipeline.wake_release(client)
    assert not fake.release_commands


@pytest.mark.parametrize(
    "title,status",
    [
        ("Manual release", "pending"),
        ("Automatic release", "in_progress"),
        ("Automatic release", "completed"),
    ],
)
def test_automatic_wakeup_preserves_manual_requests_and_can_queue_after_active_run(
    api, monkeypatch, title, status
):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    fake.release_workflow_runs = [
        ci_run(
            path=".github/workflows/release.yml",
            event="workflow_dispatch",
            display_title=title,
            status=status,
        )
    ]
    assert pipeline.wake_release(client)
    assert fake.release_commands == [
        (
            "gh",
            "workflow",
            "run",
            "release.yml",
            "--repo",
            "o/r",
            "--ref",
            "main",
            "-f",
            "automatic=true",
        )
    ]


def test_automatic_wakeup_does_not_dispatch_when_github_is_unavailable(api, monkeypatch):
    client, fake = api
    monkeypatch.setattr(pipeline, "command", fake.release_command)
    fake.release_api_error = OSError("unavailable")
    with pytest.raises(OSError):
        pipeline.wake_release(client)
    assert not fake.release_commands


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


def test_release_uses_dedicated_assets_when_summary_omits_them(api):
    client, fake = api
    item = fake.release_payloads[0]
    complete_assets = item["assets"]
    item["assets"] = []
    fake.release_asset_payloads[item["id"]] = complete_assets

    assert client.latest()["assets"] == complete_assets
    assert pipeline.release_plan(client, "b" * 40) == {
        "action": "new",
        "version": "1.0.2",
        "sha": "b" * 40,
    }


def test_dedicated_assets_remain_authoritative_when_summary_looks_complete(api):
    client, fake = api
    item = fake.release_payloads[0]
    fake.release_asset_payloads[item["id"]] = item["assets"][:-1]

    with pytest.raises(ValueError, match="assets"):
        client.latest()


@pytest.mark.parametrize("bad_checksum", [False, True])
def test_direct_release_download_checks_the_dedicated_asset_digest(
    api, tmp_path, monkeypatch, bad_checksum
):
    client, fake = api
    item = fake.release_payloads[0]
    names = [name for name in pipeline.distribution_names("1.0.1") if name.endswith(".whl")]
    files = {name: f"original {name}".encode() for name in [*names, pipeline.MANIFEST]}
    assets = []
    for name, content in files.items():
        checksum = hashlib.sha256(content).hexdigest()
        if bad_checksum and name == names[0]:
            checksum = "0" * 64
        assets.append(
            {
                "name": name,
                "state": "uploaded",
                "size": len(content),
                "digest": f"sha256:{checksum}",
                "browser_download_url": (f"https://github.com/o/r/releases/download/v1.0.1/{name}"),
            }
        )
    for name in pipeline.distribution_names("1.0.1"):
        if name.endswith(".tar.gz"):
            assets.append({"name": name, "state": "uploaded", "size": 1})
    fake.release_asset_payloads[item["id"]] = assets
    item["assets"] = []  # the stale release-summary response
    monkeypatch.setattr(
        pipeline.urllib.request,
        "urlopen",
        lambda url, timeout: io.BytesIO(files[url.rsplit("/", 1)[1]]),
    )

    if bad_checksum:
        with pytest.raises(ValueError, match="checksum"):
            client.download_assets("1.0.1", tmp_path, names)
        assert not (tmp_path / names[0]).exists()
        assert not (tmp_path / f".{names[0]}.partial").exists()
    else:
        client.download_assets("1.0.1", tmp_path, [*names, pipeline.MANIFEST])
        for name, content in files.items():
            assert (tmp_path / name).read_bytes() == content


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
    intake = (ROOT / ".github/workflows/release-wakeup.yml").read_text()
    assert "schedule:" in intake  # finish changes after an older reservation
    assert "branches: [main]" in intake  # isolated pushes do not wait for the schedule


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
