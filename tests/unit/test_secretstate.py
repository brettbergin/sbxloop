"""Tests for the shared custom-secret registration state module."""

from __future__ import annotations

import pytest

from sbxloop.errors import SecretStateError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.secretstate import (
    COPILOT_TOKEN_ENV,
    COPILOT_TOKEN_HOST,
    Assessment,
    CustomSecretState,
    assess,
    inspect_custom_secret,
    parse_secret_ls_entry,
    parsed_scope,
    probe_custom_secret,
    removal_ladder,
    replace_registration,
)
from tests.conftest import FakeSbx

EXISTS_STDERR = (
    'ERROR: custom secret env "COPILOT_GITHUB_TOKEN" already exists in scope '
    "sbxloop-r1-agent with placeholder sbx-cs-abc123."
)


class TestParsing:
    def test_parsed_scope_variants(self) -> None:
        assert parsed_scope(EXISTS_STDERR) == "sbxloop-r1-agent"
        assert parsed_scope('already exists in scope "quoted-scope" with x') == "quoted-scope"
        assert parsed_scope("some unrelated error") is None

    def test_ls_entry_sandbox_scope_and_host(self) -> None:
        raw = (
            "SCOPE  TYPE  NAME  HOST\n"
            "sbxloop-r1-agent  custom  COPILOT_GITHUB_TOKEN  api.github.com\n"
        )
        entry = parse_secret_ls_entry(raw, COPILOT_TOKEN_ENV)
        assert entry == ("sbxloop-r1-agent", ["api.github.com"])

    def test_ls_entry_global_spellings(self) -> None:
        for spelling in ("global", "-g"):
            raw = f"{spelling}  custom  COPILOT_GITHUB_TOKEN  api.github.com\n"
            entry = parse_secret_ls_entry(raw, COPILOT_TOKEN_ENV)
            assert entry is not None
            assert entry[0] == "global"

    def test_ls_entry_absent_env_returns_none(self) -> None:
        raw = "SCOPE  TYPE  NAME  HOST\nglobal  service  github  -\n"
        assert parse_secret_ls_entry(raw, COPILOT_TOKEN_ENV) is None

    def test_ls_entry_env_name_is_not_substring_matched(self) -> None:
        # A different var embedding the tracked name must not match.
        raw = "global  custom  MY_COPILOT_GITHUB_TOKEN_2  api.github.com\n"
        assert parse_secret_ls_entry(raw, COPILOT_TOKEN_ENV) is None


class TestInspect:
    def cli(self) -> SbxCLI:
        return SbxCLI()

    def custom_state(self, fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        import json

        path = fake_sbx.state / "secrets-state.json"
        data = json.loads(path.read_text()) if path.is_file() else {"custom": {}}
        return data["custom"]

    def test_registered_secret_found_via_ls(self, fake_sbx: FakeSbx) -> None:
        cli = self.cli()
        cli.secret_set_custom(
            host=COPILOT_TOKEN_HOST,
            env=COPILOT_TOKEN_ENV,
            value="tok",
            sandbox="sbxloop-r1-agent",
        )
        state = inspect_custom_secret(cli, COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST)
        assert state.exists is True
        assert state.source == "ls"
        assert state.scope == "sbxloop-r1-agent"
        assert state.hosts == [COPILOT_TOKEN_HOST]

    def test_absent_secret_probed_and_no_sentinel_left(self, fake_sbx: FakeSbx) -> None:
        state = inspect_custom_secret(self.cli(), COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST)
        assert state.exists is False
        assert state.source == "probe"
        # the probe's transient sentinel registration was removed again
        assert self.custom_state(fake_sbx) == {}

    def test_ls_unsupported_falls_back_to_collision_probe(self, fake_sbx: FakeSbx) -> None:
        cli = self.cli()
        cli.secret_set_custom(
            host=COPILOT_TOKEN_HOST,
            env=COPILOT_TOKEN_ENV,
            value="tok",
            sandbox="sbxloop-r1-agent",
        )
        fake_sbx.script("secret ls", returncode=1, stderr="unknown command")
        state = inspect_custom_secret(cli, COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST)
        assert state.exists is True
        assert state.source == "probe"
        assert state.scope == "sbxloop-r1-agent"  # parsed from the exists-error
        # probing never clobbered the real registration
        assert self.custom_state(fake_sbx)[COPILOT_TOKEN_ENV]["value"] == "tok"

    def test_probe_disabled_and_ls_unsupported_is_undetermined(self, fake_sbx: FakeSbx) -> None:
        fake_sbx.script("secret ls", returncode=1, stderr="unknown command")
        state = inspect_custom_secret(
            self.cli(), COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST, probe=False
        )
        assert state.exists is None

    def test_probe_reports_global_owner(self, fake_sbx: FakeSbx) -> None:
        cli = self.cli()
        cli.secret_set_custom(host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="tok")
        state = probe_custom_secret(cli, COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST)
        assert state.exists is True
        assert state.scope == "global"  # fake stores the -g spelling; normalized


class TestAssess:
    def state(self, **kwargs: object) -> CustomSecretState:
        return CustomSecretState.model_validate({"env": COPILOT_TOKEN_ENV, **kwargs})

    def judge(self, state: CustomSecretState, live: set[str] | None = None) -> Assessment:
        return assess(state, canonical_host=COPILOT_TOKEN_HOST, live_sandboxes=live or set())

    def test_absent_is_ok(self) -> None:
        judgement = self.judge(self.state(exists=False))
        assert judgement.status == "ok" and not judgement.stale

    def test_dead_sandbox_scope_is_stale(self) -> None:
        judgement = self.judge(self.state(exists=True, scope="sbxloop-old-agent"))
        assert judgement.status == "warn" and judgement.stale and judgement.owned

    def test_live_sandbox_scope_is_ok_but_owned(self) -> None:
        judgement = self.judge(
            self.state(exists=True, scope="sbxloop-r1-agent"), live={"sbxloop-r1-agent"}
        )
        assert judgement.status == "ok" and not judgement.stale and judgement.owned

    def test_global_canonical_is_ok(self) -> None:
        judgement = self.judge(self.state(exists=True, scope="global", hosts=["api.github.com"]))
        assert judgement.status == "ok" and not judgement.stale and judgement.owned

    def test_global_wrong_host_is_stale(self) -> None:
        judgement = self.judge(
            self.state(exists=True, scope="global", hosts=["api.githubcopilot.com"])
        )
        assert judgement.status == "warn" and judgement.stale

    def test_foreign_scope_is_never_ours(self) -> None:
        judgement = self.judge(self.state(exists=True, scope="someones-app"))
        assert judgement.status == "warn"
        assert not judgement.stale and not judgement.owned

    def test_unknown_owner_is_untouchable(self) -> None:
        judgement = self.judge(self.state(exists=True, scope=None))
        assert judgement.status == "warn"
        assert not judgement.stale and not judgement.owned


class TestRemovalAndRotation:
    def custom_state(self, fake_sbx: FakeSbx) -> dict[str, dict[str, str]]:
        import json

        return json.loads((fake_sbx.state / "secrets-state.json").read_text())["custom"]

    def test_removal_ladder_leads_with_probed_scope(self, fake_sbx: FakeSbx) -> None:
        cli = SbxCLI()
        cli.secret_set_custom(
            host=COPILOT_TOKEN_HOST,
            env=COPILOT_TOKEN_ENV,
            value="tok",
            sandbox="sbxloop-old-agent",
        )
        state = CustomSecretState(
            env=COPILOT_TOKEN_ENV, exists=True, scope="sbxloop-old-agent", detail=EXISTS_STDERR
        )
        assert any(rm() for rm in removal_ladder(cli, state, host=COPILOT_TOKEN_HOST))
        assert self.custom_state(fake_sbx) == {}

    def test_replace_registration_replaces_stale_scope(self, fake_sbx: FakeSbx) -> None:
        cli = SbxCLI()
        cli.secret_set_custom(
            host="wrong.example.com",
            env=COPILOT_TOKEN_ENV,
            value="old",
            sandbox="sbxloop-old-agent",
        )
        replace_registration(cli, env=COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST, token="new")
        entry = self.custom_state(fake_sbx)[COPILOT_TOKEN_ENV]
        assert entry == {"scope": "-g", "host": COPILOT_TOKEN_HOST, "value": "new"}

    def test_replace_registration_strict_raises_when_unremovable(self, fake_sbx: FakeSbx) -> None:
        cli = SbxCLI()
        cli.secret_set_custom(host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, value="old")
        fake_sbx.script("secret rm", returncode=1, stderr="unknown command")
        with pytest.raises(SecretStateError):
            replace_registration(cli, env=COPILOT_TOKEN_ENV, host=COPILOT_TOKEN_HOST, token="new")
