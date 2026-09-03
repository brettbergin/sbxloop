"""Conformance suite tests: probe verdicts, version-keyed cache, drift alarms."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sbxloop.sbx import conformance
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import (
    CATALOG,
    PROBE_CP_DIR_SEMANTICS,
    PROBE_EXEC_ERROR_CHANNEL,
    PROBE_LS_COLUMNS,
    PROBE_SECRET_ENV_VISIBILITY,
    PROBE_SECRET_EXISTS_ERROR,
    PROBE_SECRET_VALUE_STDIN,
    PROBE_WORKSPACE_MOUNT,
    ProbeRecord,
    cache_path,
    load_verdicts,
    record_field_verdict,
    run_conformance,
    save_verdicts,
)
from sbxloop.sbx.secretstate import parsed_scope
from tests.conftest import FakeSbx

FAKE_VERSION = "0.38.0"


def make_cli(fake_sbx: FakeSbx) -> SbxCLI:
    return SbxCLI(binary=str(fake_sbx.binary))


def by_id(report: conformance.ConformanceReport) -> dict[str, conformance.ProbeOutcome]:
    return {outcome.probe.id: outcome for outcome in report.outcomes}


class TestCatalog:
    def test_probe_ids_unique(self) -> None:
        ids = [probe.id for probe in CATALOG]
        assert len(ids) == len(set(ids))

    def test_every_probe_names_its_dependent_behavior(self) -> None:
        for probe in CATALOG:
            assert probe.depends, probe.id

    def test_scope_parser_matches_observed_error_shape(self) -> None:
        # the probe leans on secretstate's parser — the shared error-shape home
        stderr = 'ERROR: custom secret env "X" already exists in scope other-box with placeholder p'
        assert parsed_scope(stderr) == "other-box"
        assert parsed_scope("some unrelated error") is None


class TestDeepRun:
    def test_deep_run_matches_expected_verdicts(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        state = tmp_path / "state"
        report = run_conformance(make_cli(fake_sbx), state, deep=True)
        assert report.version == FAKE_VERSION
        outcomes = by_id(report)
        assert len(outcomes) == len(CATALOG)
        # The fake sbx models field-observed sbx behavior, so every probe with
        # an expected verdict must land on it — and thus report zero drift.
        for outcome in outcomes.values():
            assert outcome.source == "probe"
            assert outcome.matches_expected, (outcome.probe.id, outcome.verdict, outcome.detail)
        assert report.drifted == []
        assert report.deep_run_hint is None
        # ...and the CI gate (`doctor --fail-on-drift`) has nothing to say
        assert report.unverified == []

    def test_deep_run_removes_scratch_sandbox_and_secrets(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        cli = make_cli(fake_sbx)
        run_conformance(cli, tmp_path / "state", deep=True)
        assert cli.ls() == []
        secrets_state = fake_sbx.state / "secrets-state.json"
        state = json.loads(secrets_state.read_text())
        assert state["custom"] == {}

    def test_deep_run_writes_version_keyed_cache(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        state = tmp_path / "state"
        run_conformance(make_cli(fake_sbx), state, deep=True)
        cached = load_verdicts(state, FAKE_VERSION)
        assert set(cached) == {probe.id for probe in CATALOG}
        assert cached[PROBE_SECRET_ENV_VISIBILITY].verdict == "invisible-under-exec"
        assert cached[PROBE_CP_DIR_SEMANTICS].verdict == "contents-into-dst"
        assert cached[PROBE_EXEC_ERROR_CHANNEL].verdict == "stdout"
        assert cached[PROBE_WORKSPACE_MOUNT].verdict == "discoverable"
        assert cached[PROBE_SECRET_EXISTS_ERROR].verdict == "parseable-scope"

    def test_probe_error_does_not_abort_suite(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        fake_sbx.script("ls", returncode=1, stderr="daemon unreachable")
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=True)
        outcomes = by_id(report)
        assert outcomes[PROBE_LS_COLUMNS].is_error
        assert "daemon unreachable" in outcomes[PROBE_LS_COLUMNS].detail
        # errors are not drift, and the rest of the suite still ran
        assert outcomes[PROBE_LS_COLUMNS].drifts == []
        assert outcomes[PROBE_CP_DIR_SEMANTICS].verdict == "contents-into-dst"

    def test_mount_probe_reports_not_found_without_mount(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=True)
        outcome = by_id(report)[PROBE_WORKSPACE_MOUNT]
        assert outcome.verdict == "not-found"
        # flipped verdict vs what the codebase depends on -> loud drift
        assert outcome.drifts


class TestPageSizeProbe:
    """Verdict logic for the bundled-ripgrep page-size probe (issue #122),
    driven through a stub sandbox so every guest shape is coverable
    regardless of the host running the tests."""

    class _StubSandbox:
        def __init__(self, page_out: str, page_rc: int = 0, rg_rc: int = 1) -> None:
            self.page_out = page_out
            self.page_rc = page_rc
            self.rg_rc = rg_rc

        def exec(self, argv: list[str], **_: object) -> object:
            from sbxloop.sbx.models import ExecResult

            if argv[0] == "getconf":
                return ExecResult(
                    argv=argv,
                    returncode=self.page_rc,
                    stdout=self.page_out,
                    stderr="",
                    duration_s=0.0,
                )
            return ExecResult(
                argv=argv, returncode=self.rg_rc, stdout="", stderr="", duration_s=0.0
            )

    def _run(self, sandbox: object) -> tuple[str, str]:
        from sbxloop.sbx.conformance import ProbeContext, _probe_page_size

        ctx = ProbeContext(cli=None, sandbox=sandbox)  # type: ignore[arg-type]
        return _probe_page_size(ctx)

    def test_4k_guest(self) -> None:
        verdict, _ = self._run(self._StubSandbox("4096\n"))
        assert verdict == "4k-pages"

    def test_non_4k_with_system_rg(self) -> None:
        verdict, detail = self._run(self._StubSandbox("16384\n", rg_rc=0))
        assert verdict == "non-4k-rg-fallback"
        assert "16384" in detail

    def test_non_4k_without_system_rg(self) -> None:
        verdict, detail = self._run(self._StubSandbox("16384\n", rg_rc=1))
        assert verdict == "non-4k-degraded"
        assert "glob/grep" in detail

    def test_getconf_failure_is_unknown(self) -> None:
        verdict, _ = self._run(self._StubSandbox("", page_rc=1))
        assert verdict == "unknown"
        verdict, _ = self._run(self._StubSandbox("not-a-number\n"))
        assert verdict == "unknown"


class TestPythonVersionProbe:
    """Verdict logic for the template python3-vs-pin row (#250), through a
    stub sandbox so every template shape is coverable on any host."""

    class _StubSandbox:
        def __init__(self, out: str, rc: int = 0, err: str = "") -> None:
            self.out, self.rc, self.err = out, rc, err

        def exec(self, argv: list[str], **_: object) -> object:
            from sbxloop.sbx.models import ExecResult

            assert argv == ["python3", "--version"]
            return ExecResult(
                argv=argv, returncode=self.rc, stdout=self.out, stderr=self.err, duration_s=0.0
            )

    def _run(self, sandbox: object) -> tuple[str, str]:
        from sbxloop.sbx.conformance import ProbeContext, _probe_python_version

        ctx = ProbeContext(cli=None, sandbox=sandbox)  # type: ignore[arg-type]
        return _probe_python_version(ctx)

    def test_at_or_above_the_pin(self) -> None:
        from sbxloop.toolchains import PYTHON_SERIES

        verdict, detail = self._run(self._StubSandbox(f"Python {PYTHON_SERIES}.1\n"))
        assert verdict == "meets-pin"
        assert PYTHON_SERIES in detail
        verdict, _ = self._run(self._StubSandbox("Python 3.99.0\n"))
        assert verdict == "meets-pin"

    def test_below_the_pin_names_the_toolchain_guarantee(self) -> None:
        verdict, detail = self._run(self._StubSandbox("Python 3.12.3\n"))
        assert verdict == "below-pin"
        assert "3.12" in detail and "toolchain" in detail

    def test_detail_does_not_claim_the_interpreter_is_uv_managed(self) -> None:
        # A template that already ships uv + python3.13 passes the toolchain
        # probe and skips the install, so the versioned interpreter need not
        # be uv-managed; the row reports compatibility, not provenance.
        from sbxloop.toolchains import PYTHON_SERIES

        for out in (f"Python {PYTHON_SERIES}.1\n", "Python 3.99.0\n", "Python 3.12.3\n"):
            _, detail = self._run(self._StubSandbox(out))
            assert "uv-managed" not in detail
            assert "only if the template lacks it" in detail

    def test_python2_style_stderr_output_is_parsed(self) -> None:
        # Old interpreters print --version on stderr; the probe reads both.
        verdict, _ = self._run(self._StubSandbox("", err="Python 3.8.10\n"))
        assert verdict == "below-pin"

    def test_missing_python3(self) -> None:
        verdict, _ = self._run(self._StubSandbox("", rc=127, err="not found"))
        assert verdict == "no-python3"

    def test_catalog_row_is_informational(self) -> None:
        # Both answers are handled by the toolchain ensure; the row informs,
        # it never alarms as drift.
        from sbxloop.sbx.conformance import PROBE_PYTHON_VERSION

        probe = next(p for p in CATALOG if p.id == PROBE_PYTHON_VERSION)
        assert probe.tier == "sandbox"
        assert probe.expected is None


class TestShallowRun:
    def test_sandbox_probes_unprobed_without_cache(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=False)
        outcomes = by_id(report)
        assert outcomes[PROBE_LS_COLUMNS].verdict == "expected-columns"
        assert outcomes[PROBE_SECRET_ENV_VISIBILITY].source == "unprobed"
        assert report.deep_run_hint is not None
        assert "doctor --deep" in report.deep_run_hint
        # unprobed seams are exactly what the drift gate must refuse (#226)
        assert any(
            reason.startswith(f"{PROBE_SECRET_ENV_VISIBILITY}: unprobed")
            for reason in report.unverified
        )
        # no sandbox was ever created
        assert make_cli(fake_sbx).ls() == []

    def test_sandbox_probes_served_from_cache(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        state = tmp_path / "state"
        cli = make_cli(fake_sbx)
        run_conformance(cli, state, deep=True)
        report = run_conformance(cli, state, deep=False)
        outcomes = by_id(report)
        assert outcomes[PROBE_SECRET_ENV_VISIBILITY].source == "cache"
        assert outcomes[PROBE_SECRET_ENV_VISIBILITY].verdict == "invisible-under-exec"
        assert report.deep_run_hint is None

    def test_field_recorded_verdicts_render_as_field(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        record_field_verdict(
            state, FAKE_VERSION, PROBE_SECRET_ENV_VISIBILITY, "invisible-under-exec"
        )
        report = run_conformance(make_cli(fake_sbx), state, deep=False)
        assert by_id(report)[PROBE_SECRET_ENV_VISIBILITY].source == "provision"


class TestSecretValueStdinProbe:
    """The #57 ps-visibility watchdog: sbx set-custom offers no stdin path
    today, so the PAT must ride --value on argv; the probe alarms the moment
    an sbx upgrade makes stdin passing possible."""

    def test_argv_only_against_fake(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=False)
        outcome = by_id(report)[PROBE_SECRET_VALUE_STDIN]
        assert outcome.verdict == "argv-only"
        assert outcome.drifts == []

    def test_alarms_when_sbx_gains_a_stdin_path(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        fake_sbx.script(
            "secret set-custom --help",
            stdout="Flags:\n      --value-stdin   Read the secret value from stdin\n",
        )
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=False)
        outcome = by_id(report)[PROBE_SECRET_VALUE_STDIN]
        assert outcome.verdict == "stdin-available"
        assert outcome.drifts
        assert any("adopt it" in drift for drift in outcome.drifts)  # no bare #N (#635)

    def test_unrecognized_help_is_visible_not_fatal(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        fake_sbx.script("secret set-custom --help", returncode=2, stderr="unknown flag: --help\n")
        report = run_conformance(make_cli(fake_sbx), tmp_path / "state", deep=False)
        outcome = by_id(report)[PROBE_SECRET_VALUE_STDIN]
        assert outcome.verdict == "help-drifted"
        assert outcome.drifts  # flipped vs expected -> loud


class TestDrift:
    def seed_old_version(self, state: Path, probe_id: str, verdict: str) -> None:
        save_verdicts(
            state,
            "0.34.0",
            {probe_id: ProbeRecord(verdict=verdict, checked_at=time.time() - 100)},
        )

    def test_cross_version_flip_is_drift(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        state = tmp_path / "state"
        self.seed_old_version(state, PROBE_SECRET_ENV_VISIBILITY, "visible-under-exec")
        report = run_conformance(make_cli(fake_sbx), state, deep=True)
        outcome = by_id(report)[PROBE_SECRET_ENV_VISIBILITY]
        assert report.previous_version == "0.34.0"
        assert any("0.34.0" in drift for drift in outcome.drifts)
        assert any("visible-under-exec" in drift for drift in outcome.drifts)

    def test_expected_mismatch_names_dependent_behavior(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        # seed the CURRENT version's cache with a flipped verdict; a shallow
        # run must still alarm on the cached value. Any probe carrying an
        # `expected` will do — secret-env-visibility deliberately carries None
        # now, because provisioning auto-heals every answer it can give.
        save_verdicts(
            state,
            FAKE_VERSION,
            {PROBE_WORKSPACE_MOUNT: ProbeRecord(verdict="harvest-only", checked_at=time.time())},
        )
        report = run_conformance(make_cli(fake_sbx), state, deep=False)
        outcome = by_id(report)[PROBE_WORKSPACE_MOUNT]
        assert outcome.drifts
        assert any("mount discovery" in drift for drift in outcome.drifts)

    def test_same_verdict_across_versions_is_not_drift(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        state = tmp_path / "state"
        self.seed_old_version(state, PROBE_SECRET_ENV_VISIBILITY, "invisible-under-exec")
        report = run_conformance(make_cli(fake_sbx), state, deep=True)
        assert report.drifted == []


class TestCache:
    def test_save_merges_instead_of_replacing(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        save_verdicts(state, "0.35.0", {"a": ProbeRecord(verdict="x", checked_at=1.0)})
        save_verdicts(state, "0.35.0", {"b": ProbeRecord(verdict="y", checked_at=2.0)})
        cached = load_verdicts(state, "0.35.0")
        assert set(cached) == {"a", "b"}

    def test_versions_get_distinct_files(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        save_verdicts(state, "0.35.0", {"a": ProbeRecord(verdict="x", checked_at=1.0)})
        save_verdicts(state, "0.36.0", {"a": ProbeRecord(verdict="y", checked_at=2.0)})
        assert cache_path(state, "0.35.0") != cache_path(state, "0.36.0")
        assert load_verdicts(state, "0.35.0")["a"].verdict == "x"
        assert load_verdicts(state, "0.36.0")["a"].verdict == "y"

    def test_corrupt_cache_treated_as_empty(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        path = cache_path(state, "0.35.0")
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert load_verdicts(state, "0.35.0") == {}

    def test_record_field_verdict_swallows_unwritable_dir(self, tmp_path: Path) -> None:
        blocker = tmp_path / "state"
        blocker.write_text("a file where the state dir should be")
        record_field_verdict(blocker, "0.35.0", "a", "x")  # must not raise


class TestExecStdinEnvProbe:
    """Verdict logic for exec-stdin-env (#592) through a stub sandbox: does
    stdin piped through `sbx exec` reach the in-VM launch shell?"""

    class _StubSandbox:
        def __init__(self, *, echo_env: bool, rc: int = 0, noise: str = "") -> None:
            self.echo_env, self.rc, self.noise = echo_env, rc, noise

        def exec(self, argv: list[str], *, stdin: str = "", **_: object) -> object:
            from sbxloop.sbx.models import ExecResult

            # A forwarding sbx delivers the payload; the marker comes back,
            # possibly wrapped in login-profile chatter (self.noise).
            out = ""
            if self.echo_env and stdin:
                value = stdin.strip().split("=", 1)[1]
                out = f"{self.noise}{value}"
            return ExecResult(argv=argv, returncode=self.rc, stdout=out, stderr="", duration_s=0.0)

    def _run(self, sandbox: object) -> tuple[str, str]:
        from sbxloop.sbx.conformance import ProbeContext, _probe_exec_stdin_env

        ctx = ProbeContext(cli=None, sandbox=sandbox)  # type: ignore[arg-type]
        return _probe_exec_stdin_env(ctx)

    def test_forwarded_stdin_delivers(self) -> None:
        verdict, _ = self._run(self._StubSandbox(echo_env=True))
        assert verdict == "delivers"

    def test_profile_chatter_around_the_marker_still_delivers(self) -> None:
        verdict, _ = self._run(self._StubSandbox(echo_env=True, noise="nvm loaded\n"))
        assert verdict == "delivers"

    def test_swallowed_stdin_is_no_delivery(self) -> None:
        verdict, detail = self._run(self._StubSandbox(echo_env=False))
        assert verdict == "no-delivery"
        assert "env file" in detail

    def test_failing_launch_is_no_delivery(self) -> None:
        verdict, _ = self._run(self._StubSandbox(echo_env=True, rc=1))
        assert verdict == "no-delivery"
