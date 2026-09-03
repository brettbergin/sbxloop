"""The ecosystem fixture matrix (#644): one row per project shape sbxloop
must not mistake for a Python repo, walked through every generalization
surface that has landed.

Today's columns: language detection (#624), the installer allowlist
(#616), the project gate (#625/#626) and the config-override lint (#628).
Later work adds its column here rather than a test of its own, so "does a
Go repo work?" stays one table, and a regression names the decision that
changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from sbxloop import toolchains
from sbxloop.config import Config
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.verifylint import config_override_problems, project_gate
from tests.conftest import FakeSbx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ecosystems"


class Expectation(NamedTuple):
    languages: tuple[str, ...]
    source: str
    # installer hosts the agent sandbox must be created with, and one it
    # must NOT be — a language nobody selected opens nothing (#616)
    allowed: tuple[str, ...]
    not_allowed: tuple[str, ...]
    # the project's own gate under the resolved toolchains (#625/#626);
    # None = the project declares none and nothing is invented for it
    gate: str | None
    # verify commands against this project's own config files, and whether
    # the config-override lint flags each (#628); False rows pin the
    # narrowing and bare forms the lint must keep accepting
    lint: tuple[tuple[str, bool], ...]


EXPECTATIONS: dict[str, Expectation] = {
    "python-uv": Expectation(
        ("python",),
        "detected",
        allowed=("release-assets.githubusercontent.com",),
        not_allowed=("nodejs.org", "go.dev"),
        gate=None,
        lint=(
            ("uv run pytest -q packages", True),
            ("uv run pytest -q tests/unit", False),
            ("uv run pytest -q", False),
        ),
    ),
    # A pnpm monorepo: the root package.json fires javascript; the
    # workspace package's tsconfig.json, two levels down, fires typescript.
    "node-pnpm": Expectation(
        ("javascript", "typescript"),
        "detected",
        allowed=("nodejs.org", "registry.npmjs.org"),
        not_allowed=("go.dev", "static.rust-lang.org"),
        gate="pnpm run check",
        # the root tsconfig.json is a solution file: any input file handed
        # to tsc drops it, `-b` walks its references
        lint=(
            ("pnpm exec tsc --noEmit src/index.ts", True),
            ("pnpm exec tsc --noEmit", False),
            ("pnpm exec tsc -b packages/web", False),
        ),
    ),
    "go": Expectation(
        ("go",),
        "detected",
        allowed=("go.dev", "dl.google.com"),
        not_allowed=("nodejs.org", "static.rust-lang.org"),
        gate="go vet ./... && go test ./...",
        # no entry reads Go config: golangci-lint honours its exclusions
        # for explicit paths and `go` overrides by build tag, not by path
        lint=(
            ("go vet ./cmd/...", False),
            ("golangci-lint run ./cmd/...", False),
        ),
    ),
    "rust": Expectation(
        ("rust",),
        "detected",
        allowed=("static.rust-lang.org",),
        not_allowed=("nodejs.org", "go.dev"),
        gate="cargo test",
        lint=(("cargo test -p fixture", False),),
    ),
    # apt-only toolchain: nothing beyond the always-reachable mirrors
    "java-gradle": Expectation(
        ("java",),
        "detected",
        allowed=(),
        not_allowed=("nodejs.org", "go.dev", "static.rust-lang.org"),
        gate="./gradlew check",
        lint=(("./gradlew check", False),),
    ),
    "ruby": Expectation(
        ("ruby",),
        "detected",
        allowed=(),
        not_allowed=("nodejs.org", "go.dev"),
        gate="bundle exec rake default",
        lint=(
            ("bundle exec rubocop db/schema.rb", True),
            ("bundle exec rubocop app", False),
            ("bundle exec rubocop", False),
        ),
    ),
    # both, in registry order — union, never "best guess"
    "polyglot": Expectation(
        ("python", "javascript"),
        "detected",
        allowed=("release-assets.githubusercontent.com", "nodejs.org"),
        not_allowed=("go.dev",),
        gate=None,
        # neither manifest scopes anything: nothing to override
        lint=(
            ("npx tsc --noEmit src/index.ts", False),
            ("uv run pytest tests", False),
        ),
    ),
    # No manifest at the root or one level down (the node_modules
    # package.json is skipped): the historical default applies.
    "none": Expectation(
        ("python",),
        "default",
        allowed=("release-assets.githubusercontent.com",),
        not_allowed=("nodejs.org",),
        gate=None,
        lint=(("pytest tests", False),),
    ),
}


def test_every_fixture_has_an_expectation() -> None:
    present = {p.name for p in FIXTURES.iterdir() if p.is_dir()}
    assert present == set(EXPECTATIONS), "add a row to EXPECTATIONS for each fixture dir"


@pytest.mark.parametrize("fixture", sorted(EXPECTATIONS))
def test_detection(fixture: str) -> None:
    expected = EXPECTATIONS[fixture]
    resolved = toolchains.resolve_languages((), FIXTURES / fixture)
    assert resolved.languages == expected.languages
    assert resolved.source == expected.source
    if expected.source == "detected":
        # every detected language names the manifest that fired
        assert set(resolved.signals) == set(expected.languages)
        assert all(resolved.signals[lang] for lang in expected.languages)
    else:
        assert resolved.signals == {}


@pytest.mark.parametrize("fixture", sorted(EXPECTATIONS))
def test_agent_allowlist(fixture: str, fake_sbx: FakeSbx, tmp_path: Path) -> None:
    expected = EXPECTATIONS[fixture]
    config = Config.model_validate({"state_dir": str(tmp_path / "state")})
    provisioner = Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env={})
    workspace = FIXTURES / fixture
    resolved = provisioner.resolve_languages(workspace)
    agent, _github = provisioner.build_specs("r1", workspace, languages=resolved.languages)
    for host in expected.allowed:
        assert host in agent.policy_allows, host
    for host in expected.not_allowed:
        assert host not in agent.policy_allows, host


@pytest.mark.parametrize("fixture", sorted(EXPECTATIONS))
def test_project_gate(fixture: str) -> None:
    """The gate is detected under the toolchains the same fixture resolved
    to — a gate the sandbox could not run is not a gate (#625)."""
    expected = EXPECTATIONS[fixture]
    resolved = toolchains.resolve_languages((), FIXTURES / fixture)
    assert project_gate(FIXTURES / fixture, languages=resolved.languages) == expected.gate


@pytest.mark.parametrize("fixture", sorted(EXPECTATIONS))
def test_config_override_lint(fixture: str) -> None:
    """The config-override lint reads this project's own config files: a
    path that escapes what they declare is flagged, a narrowing or bare
    form is not (#628)."""
    expected = EXPECTATIONS[fixture]
    for command, flagged in expected.lint:
        problems = config_override_problems(command, FIXTURES / fixture)
        assert bool(problems) is flagged, (command, problems)
