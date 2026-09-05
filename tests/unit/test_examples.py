"""The committed example files cannot rot: `sbxloop.toml.example` is the
single source `sbxloop init` writes from, it covers the config model, and
neither example nor template may carry anything that looks like a real
credential, host path or repository."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sbxloop.cli.app import DEFAULT_CONFIG_TOML, config_presets, render_config_template
from sbxloop.config import Config, load_dotenv_file

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "sbxloop.toml.example"


def _key_paths(data: dict[str, Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for key, value in data.items():
        if isinstance(value, dict):
            paths |= _key_paths(value, f"{prefix}{key}.")
        else:
            paths.add(f"{prefix}{key}")
    return paths


def test_example_exists_and_parses() -> None:
    assert EXAMPLE.is_file()
    assert isinstance(tomllib.loads(EXAMPLE.read_text()), dict)


def test_init_template_is_the_example_file() -> None:
    """One source of truth: `sbxloop init` writes this exact file."""
    assert EXAMPLE.read_text() == DEFAULT_CONFIG_TOML
    assert _key_paths(tomllib.loads(DEFAULT_CONFIG_TOML)) == _key_paths(
        tomllib.loads(EXAMPLE.read_text())
    )


def test_packaged_copy_matches_the_root_example() -> None:
    """`sbxloop init` reads the packaged copy; it must be byte-identical to
    the file published at the repository root."""
    packaged = (
        REPO_ROOT / "packages" / "sbxloop" / "src" / "sbxloop" / "data" / "sbxloop.toml.example"
    )
    assert packaged.is_file()
    assert packaged.read_text() == EXAMPLE.read_text()


def test_example_is_a_valid_config() -> None:
    Config.model_validate(tomllib.loads(EXAMPLE.read_text()))


def test_sbxloop_init_renders_the_example_file(tmp_path: Path, monkeypatch: Any) -> None:
    """End-to-end drift check: what `sbxloop init` actually writes into a
    temp dir has the same dotted key paths as the committed example."""
    from typer.testing import CliRunner

    from sbxloop.cli.app import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    written = (tmp_path / "sbxloop.toml").read_text()
    assert _key_paths(tomllib.loads(written)) == _key_paths(tomllib.loads(EXAMPLE.read_text()))
    assert written == EXAMPLE.read_text()


PRESETS_DIR = REPO_ROOT / "packages" / "sbxloop" / "src" / "sbxloop" / "data" / "presets"


class TestPresets:
    """#636: presets are package data, applied by `sbxloop init --preset`, so
    they work from a wheel and nothing `init` writes points at a checkout."""

    def test_presets_ship_as_package_data_and_the_contrib_path_is_an_alias(self) -> None:
        assert (PRESETS_DIR / "large-repo.toml").is_file()
        assert config_presets().keys() == {"large-repo"}
        alias = REPO_ROOT / "contrib" / "presets" / "large-repo.toml"
        assert alias.is_symlink()
        assert alias.resolve() == (PRESETS_DIR / "large-repo.toml").resolve()

    def test_every_preset_is_a_valid_config_on_its_own_and_appended(self) -> None:
        for name, fragment in config_presets().items():
            Config.model_validate(tomllib.loads(fragment))
            merged = tomllib.loads(render_config_template(name))
            Config.model_validate(merged)
            # the template's own tables are all commented, so the preset's
            # live sections are the only ones and survive the merge intact
            for table, values in tomllib.loads(fragment).items():
                assert merged[table] == values, (name, table)

    def test_large_repo_preset_sizes_for_a_slow_gate(self) -> None:
        config = Config.model_validate(tomllib.loads(render_config_template("large-repo")))
        assert config.budgets.max_wall_clock_s == 14400.0
        assert config.budgets.max_tool_calls_per_phase == 80
        assert config.limits.mem_abort == 97.0
        header = config_presets()["large-repo"]
        assert "two minutes or more" in header  # framed by measured gate duration
        assert "sbxloop init --preset large-repo" in header

    def test_nothing_init_writes_references_a_path_outside_the_project(self) -> None:
        for name in (None, *config_presets()):
            text = render_config_template(name)
            for needle in ("contrib/", "packages/sbxloop", "#25"):
                assert needle not in text, (name, needle)

    def test_unknown_preset_is_a_key_error(self) -> None:
        with pytest.raises(KeyError):
            render_config_template("not-a-preset")

    def test_sbxloop_init_preset_writes_one_self_contained_file(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from typer.testing import CliRunner

        from sbxloop.cli.app import app

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, ["init", "--preset", "large-repo"])
        assert result.exit_code == 0, result.output
        written = (tmp_path / "sbxloop.toml").read_text()
        assert written == render_config_template("large-repo")
        assert written.startswith(EXAMPLE.read_text().rstrip("\n"))
        assert Config.model_validate(tomllib.loads(written)).budgets.max_wall_clock_s == 14400.0
        streamed = CliRunner().invoke(app, ["init", "--stdout", "--preset", "large-repo"])
        assert streamed.exit_code == 0, streamed.output
        assert streamed.output == written

    def test_sbxloop_init_rejects_an_unknown_preset_before_writing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from typer.testing import CliRunner

        from sbxloop.cli.app import app

        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, ["init", "--preset", "huge-repo"])
        assert result.exit_code == 2, result.output
        assert "unknown preset 'huge-repo'" in result.output
        assert "large-repo" in result.output  # names what does exist
        assert not (tmp_path / "sbxloop.toml").exists()


# Derived internals the engine sets on a narrowed config; never configured.
INTERNAL_KEYS = {"github.enabled_repo_count", "workload.result_issue"}


def test_example_mentions_every_key_the_config_model_knows() -> None:
    """Live or commented, every field of the model appears in the example."""
    text = EXAMPLE.read_text()
    missing: list[str] = []

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, field in model.model_fields.items():
            annotation = field.annotation
            nested = [
                arg
                for arg in (annotation, *getattr(annotation, "__args__", ()))
                if isinstance(arg, type) and issubclass(arg, BaseModel)
            ]
            if nested:
                for sub in nested:
                    walk(sub, f"{prefix}{name}.")
                continue
            if f"{prefix}{name}" in INTERNAL_KEYS:
                continue
            if not re.search(rf"^#?\s*{re.escape(name)}\s*=", text, re.MULTILINE):
                missing.append(f"{prefix}{name}")

    walk(Config, "")
    assert not missing, f"keys absent from sbxloop.toml.example: {missing}"


def test_example_documents_both_github_forms() -> None:
    text = EXAMPLE.read_text()
    assert "[[github.repos]]" in text
    assert re.search(r"^# repo = \"you/your-repo\"", text, re.MULTILINE)
    for key in ("deliver_base", "enabled", "token_env", "trigger_label", "labels", "workspace"):
        assert re.search(rf"^#\s*{key} = ", text, re.MULTILINE), key


ENV_EXAMPLE = REPO_ROOT / ".env.example"
SOURCE_ROOT = REPO_ROOT / "packages" / "sbxloop" / "src" / "sbxloop"

# Credentials the code reads by name; each must be documented in .env.example.
CREDENTIAL_ENVS = {
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "DISCORD_BOT_TOKEN",
}


def _env_example_names() -> set[str]:
    """Every variable named in .env.example, live or commented out."""
    return {
        m.group(1)
        for m in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", ENV_EXAMPLE.read_text(), re.MULTILINE)
    }


def test_env_example_documents_every_credential_the_source_reads() -> None:
    documented = _env_example_names()
    missing = sorted(CREDENTIAL_ENVS - documented)
    assert not missing, f"credentials absent from .env.example: {missing}"


def test_credential_env_names_are_still_read_by_the_source() -> None:
    """The other direction: the documented credentials are names the code
    actually uses, so a rename in the source fails this test too."""
    text = "\n".join(path.read_text() for path in sorted(SOURCE_ROOT.rglob("*.py")))
    for name in sorted(CREDENTIAL_ENVS):
        assert f'"{name}"' in text or f"``{name}``" in text, name


def test_env_example_documents_the_per_repo_token_pattern() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "token_env" in text
    assert re.search(r"^#?\s*GH_TOKEN_TWO\s*=", text, re.MULTILINE)


def test_env_example_documents_the_daemon_host_layout() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "~/.config/sbxloop/secrets.env" in text
    assert "0600" in text


def test_env_example_sbxloop_overrides_name_real_config_keys() -> None:
    """Each SBXLOOP_<SECTION>__<KEY> mentioned maps onto a model field."""
    known = _config_key_paths()
    seen = 0
    for name in _env_example_names():
        if not name.startswith("SBXLOOP_"):
            continue
        remainder = name[len("SBXLOOP_") :].lower()
        dotted = remainder.replace("__", ".")
        assert dotted in known, f"{name} is not a config key ({dotted})"
        seen += 1
    assert seen, "expected .env.example to document some SBXLOOP_* overrides"


def _config_key_paths() -> set[str]:
    paths: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, field in model.model_fields.items():
            annotation = field.annotation
            nested = [
                arg
                for arg in (annotation, *getattr(annotation, "__args__", ()))
                if isinstance(arg, type) and issubclass(arg, BaseModel)
            ]
            paths.add(f"{prefix}{name}")
            for sub in nested:
                walk(sub, f"{prefix}{name}.")

    walk(Config, "")
    return paths


def test_env_example_loads_with_the_cli_dotenv_loader(tmp_path: Path, monkeypatch: Any) -> None:
    """The file parses with the same loader `sbxloop` uses, and — since every
    credential ships blank or commented — sets nothing that could shadow a
    real export."""
    (tmp_path / ".env").write_text(ENV_EXAMPLE.read_text())
    for name in CREDENTIAL_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert load_dotenv_file(tmp_path) == tmp_path / ".env"
    for name in CREDENTIAL_ENVS:
        assert not os.environ.get(name), f"{name} got a value from .env.example"


def test_env_example_has_no_legacy_single_repo_override_uncommented() -> None:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "SBXLOOP_GITHUB__REPO" not in line


PLACEHOLDER_REPOS = {"you/your-repo", "you/other-repo"}
PLACEHOLDER_SNOWFLAKE = "123456789012345678"

REAL_LOOKING = [
    ("classic PAT", re.compile(r"ghp_[A-Za-z0-9]{10,}")),
    ("fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{10,}")),
    ("gh server/oauth token", re.compile(r"gh[opsu]_[A-Za-z0-9]{10,}")),
    ("discord token", re.compile(r"[\w-]{24}\.[\w-]{6}\.[\w-]{27,}")),
    ("home path", re.compile(r"/home/(?!agent/\.sbxloop)[A-Za-z0-9._-]+")),
    ("users path", re.compile(r"/Users/[A-Za-z0-9._-]+")),
]

EXAMPLE_FILES = ("sbxloop.toml.example", ".env.example")


def _texts() -> dict[str, str]:
    files = {name: (REPO_ROOT / name).read_text() for name in EXAMPLE_FILES}
    files["sbxloop init template"] = DEFAULT_CONFIG_TOML
    return files


def test_examples_contain_no_real_looking_secrets_or_paths() -> None:
    for name, text in _texts().items():
        for label, pattern in REAL_LOOKING:
            found = pattern.search(text)
            assert found is None, f"{name}: {label} lookalike {found.group(0)!r}"


def test_examples_use_only_placeholder_snowflakes() -> None:
    for name, text in _texts().items():
        ids = {
            m.group(0)
            for m in re.finditer(r"(?<![\w.])\d{17,19}(?![\w.])", text)
            if m.group(0) != PLACEHOLDER_SNOWFLAKE
        }
        assert not ids, f"{name}: non-placeholder snowflake(s) {sorted(ids)}"


def test_examples_use_only_placeholder_repositories() -> None:
    # owner/name shapes only; skip paths, URLs and label values.
    pattern = re.compile(r"(?<![\w./-])([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)(?![\w./-])")
    for name, text in _texts().items():
        for line in text.splitlines():
            for match in pattern.finditer(line):
                candidate = match.group(1)
                if not re.search(rf"repo\w*\s*=\s*\"{re.escape(candidate)}\"", line):
                    continue
                assert candidate in PLACEHOLDER_REPOS, f"{name}: real repository {candidate!r}"


def test_example_ships_sections_commented_out() -> None:
    """A fresh copy is exactly the built-in defaults: only the top-level keys
    are live, every `[section]` and its keys ship commented out. This also
    keeps the parsed document flat, which the drift check relies on."""
    parsed = tomllib.loads(EXAMPLE.read_text())
    assert not [k for k, v in parsed.items() if isinstance(v, dict)]
    assert set(parsed) == {
        "model",
        "app_name",
        "state_dir",
        "keep_sandboxes",
        "keep_on_failure",
        "worker_transport",
        "secret_strategy",
    }
    assert Config.model_validate(parsed).model_dump() == Config().model_dump()


def test_every_commented_key_is_a_real_config_key() -> None:
    """Each commented `key = value` under a commented `[section]` header is a
    key the model knows, with a value the model accepts — so uncommenting any
    single line loads."""
    section = ""
    checked = 0
    for line in EXAMPLE.read_text().splitlines():
        stripped = re.sub(r"^#\s?", "", line)
        header = re.match(r"^\[\[?([a-z.]+)\]\]?$", stripped)
        if header:
            section = header.group(1)
            continue
        assignment = re.match(r"^([a-z_]+) = (.+?)(?:\s{2,}#.*)?$", stripped)
        if not section or not assignment or not line.lstrip().startswith("#"):
            continue
        key, value = assignment.groups()
        try:
            parsed = tomllib.loads(f"{key} = {value}")
        except tomllib.TOMLDecodeError:
            continue  # a multi-line value (the exclude list); covered below
        if section in ("registries", "credentials", "workloads", "schedules"):
            continue  # array-of-tables entries load as whole blocks, below
        if section == "github.repos":
            doc: dict[str, Any] = {"github": {"repos": [{"repo": "you/your-repo", **parsed}]}}
        elif section == "github":
            # `[github]` needs a repository before any other key is meaningful.
            doc = {"github": {"repo": "you/your-repo", **parsed}}
        elif section == "workload":
            # `[workload] default` names a profile that must exist.
            doc = {"workload": parsed, "workloads": [{"name": parsed.get("default", "p")}]}
        elif section == "chat":
            # `[chat] backend` names a section that must carry a channel_id.
            doc = {
                "chat": parsed,
                "discord": {"channel_id": 1},
                "slack": {"channel_id": "C0123ABCDEF"},
            }
        else:
            doc = {section: parsed}
        Config.model_validate(doc)
        checked += 1
    assert checked > 50, f"expected the example to document many keys, saw {checked}"


def test_example_registry_entries_load_together() -> None:
    """A `[[registries]]` entry's keys are coupled by `kind` (a go entry takes
    no url, a pypi entry needs auth_user), so the commented entries are
    validated as whole blocks — uncommenting all of them loads as one list."""
    blocks: list[str] = []
    for line in EXAMPLE.read_text().splitlines():
        stripped = re.sub(r"^#\s?", "", line)
        if stripped == "[[registries]]":
            blocks.append("")
        elif blocks and line.startswith("#") and re.match(r"^[a-z_]+ = ", stripped):
            blocks[-1] += stripped + "\n"
        elif blocks and not line.strip():
            break
    entries = [tomllib.loads(block) for block in blocks]
    assert len(entries) >= 4
    assert {entry["kind"] for entry in entries} >= {"npm", "pypi", "cargo", "go"}
    config = Config.model_validate({"registries": entries})
    assert [r.kind for r in config.registries] == [entry["kind"] for entry in entries]


def test_example_workload_profile_loads_with_its_credential() -> None:
    """The commented `[[workloads]]` entry loads as one block beside the
    `[[credentials]]` entry it names, and `[workload] default` finds it —
    uncommenting the three together is a working configuration."""

    def block_after(header: str) -> dict[str, Any]:
        text = ""
        in_block = False
        for line in EXAMPLE.read_text().splitlines():
            stripped = re.sub(r"^#\s?", "", line)
            if stripped == header:
                in_block = True
            elif in_block and line.startswith("#") and re.match(r"^[a-z_]+ = ", stripped):
                text += stripped + "\n"
            elif in_block and stripped.startswith("["):
                break
        return tomllib.loads(text)

    profile = block_after("[[workloads]]")
    credential = block_after("[[credentials]]")
    section = block_after("[workload]")
    config = Config.model_validate(
        {"credentials": [credential], "workloads": [profile], "workload": section}
    )
    (entry,) = config.workloads
    assert entry.name == profile["name"] == section["default"]
    assert entry.credentials == [credential["name"]]
    assert entry.budgets.set_keys == sorted(profile["budgets"])
    assert config.workload_profile() is entry
    # The commented `[[schedules]]` entry shows both cadences; either one
    # alone, with the profile above, is a working schedule (#761).
    schedule = block_after("[[schedules]]")
    assert schedule["profile"] == profile["name"]
    for drop in ("every", "cron"):
        one = {k: v for k, v in schedule.items() if k != drop}
        scheduled = Config.model_validate(
            {"credentials": [credential], "workloads": [profile], "schedules": [one]}
        )
        (spec,) = scheduled.schedules
        assert spec.name == schedule["name"] and spec.cadence_text
    with pytest.raises(ValueError, match="exactly one of every / cron"):
        Config.model_validate(
            {"credentials": [credential], "workloads": [profile], "schedules": [schedule]}
        )


def test_example_credential_entry_loads() -> None:
    """The commented `[[credentials]]` entry loads as one block: its keys
    are coupled (a bare `X-Api-Key` header wants `scheme = ""`), so it is
    validated whole rather than key by key."""
    block = ""
    in_block = False
    for line in EXAMPLE.read_text().splitlines():
        stripped = re.sub(r"^#\s?", "", line)
        if stripped == "[[credentials]]":
            in_block = True
        elif in_block and line.startswith("#") and re.match(r"^[a-z_]+ = ", stripped):
            block += stripped + "\n"
        elif in_block and not line.strip():
            break
    entry = tomllib.loads(block)
    config = Config.model_validate({"credentials": [entry]})
    (cred,) = config.credentials
    assert cred.name == entry["name"]
    assert cred.host == entry["host"]
    assert cred.header == entry["header"]
    assert cred.scheme == entry["scheme"]
