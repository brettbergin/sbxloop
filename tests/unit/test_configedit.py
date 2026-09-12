"""``sbxloop.configedit``: the per-key editor every surface goes through.

The move out of the console kept its behaviour (the console's own tests
still pass); what is new is the one object over it — a change the loader
has judged before anything is written, the note about which layer answers,
the doc line from the example, and the one place that says whether a
change applies live or at the next start."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from sbxloop.config import Config
from sbxloop.configedit import ConfigEditError, ConfigEditor, applies_for
from sbxloop.configedit.docs import doc_for, doc_lines, parse
from sbxloop.paths import SbxloopHome
from tests.unit.test_examples import INTERNAL_KEYS, LEGACY_KEYS


@pytest.fixture
def home(tmp_path: Path) -> SbxloopHome:
    home = SbxloopHome(tmp_path / ".sbxloop")
    home.ensure_tree()
    return home


def _editor(home: SbxloopHome, **env: str) -> ConfigEditor:
    # Only what the loader needs to find the home: nothing from the
    # developer's shell (an exported SBXLOOP_* would be another layer).
    return ConfigEditor(home, {"HOME": str(home.root.parent), **env})


def _seed(home: SbxloopHome, text: str) -> None:
    home.config_toml.write_text(text)


# -- changes ----------------------------------------------------------------


def test_a_value_the_loader_refuses_writes_nothing(home: SbxloopHome) -> None:
    """`[concierge] timeout_s` is bounded at 30; a draft past the bound is
    refused with the loader's reason and the file is byte-identical."""
    _seed(home, "# kept\n[concierge]\ntimeout_s = 60.0\n")
    before = home.config_toml.read_text()
    editor = _editor(home)
    change = editor.set("concierge.timeout_s", "5")
    assert not change.ok
    assert change.verdict.error is not None and "timeout_s" in change.verdict.error
    with pytest.raises(ConfigEditError, match="draft refused"):
        editor.commit(change)
    assert home.config_toml.read_text() == before


def test_a_key_the_environment_also_sets_is_flagged(home: SbxloopHome) -> None:
    _seed(home, "[daemon]\npoll_interval_s = 7.0\n")
    change = _editor(home, SBXLOOP_DAEMON__POLL_INTERVAL_S="5.0").set("daemon.poll_interval_s", "9")
    assert change.ok
    assert change.note_level == "warning"
    assert change.note is not None
    assert "env sets it too and wins" in change.note and "5.0" in change.note
    assert change.old == 7.0 and change.new == 9.0


def test_unsetting_names_what_answers_instead(home: SbxloopHome) -> None:
    _seed(home, '[landing]\nmerge_method = "squash"\n')
    change = _editor(home).unset("landing.merge_method")
    assert change.ok and change.unset
    assert change.note == "landing.merge_method unset here; it now comes from its default: 'auto'"
    assert change.note_level == "information"
    assert "merge_method" not in change.text


def test_commit_keeps_the_previous_file_and_every_comment(home: SbxloopHome) -> None:
    _seed(home, "# why the cap is low\n[daemon]\nmax_runs_per_day = 3  # trial\n")
    editor = _editor(home)
    change = editor.set("daemon.max_runs_per_day", "20")
    assert change.ok and change.note is None
    backup = editor.commit(change, now=0.0)
    assert backup is not None and backup.exists()
    assert backup.name.startswith("sbxloop.toml.bak-")
    assert backup.read_text() == "# why the cap is low\n[daemon]\nmax_runs_per_day = 3  # trial\n"
    text = home.config_toml.read_text()
    assert "# why the cap is low" in text and "max_runs_per_day = 20  # trial" in text


def test_the_first_write_has_no_backup_and_lands_in_the_home(home: SbxloopHome) -> None:
    editor = _editor(home)
    assert not home.config_toml.exists()
    change = editor.set("daemon.max_runs_per_day", "20")
    assert editor.commit(change) is None
    assert "max_runs_per_day = 20" in home.config_toml.read_text()


def test_refusals_are_named_in_the_operators_terms(home: SbxloopHome) -> None:
    _seed(home, "[daemon]\nmax_runs_per_day = 20\n")
    editor = _editor(home)
    with pytest.raises(ConfigEditError, match="whole number"):
        editor.set("daemon.max_runs_per_day", "soon")
    with pytest.raises(ConfigEditError, match="already says that"):
        editor.set("daemon.max_runs_per_day", "20")
    with pytest.raises(ConfigEditError, match="not a file setting"):
        editor.set("home", "/elsewhere")
    with pytest.raises(ConfigEditError, match="not a key or an index"):
        editor.describe("github.repos[x]")
    with pytest.raises(ConfigEditError, match="past the end"):
        editor.set("github.repos[3].repo", "o/r")


# -- the resolved view -------------------------------------------------------


def test_rows_carry_source_doc_and_when_a_change_applies(home: SbxloopHome) -> None:
    _seed(home, "[daemon]\nmax_runs_per_day = 20\n")
    rows = {row.key: row for row in _editor(home).resolved()}
    cap = rows["daemon.max_runs_per_day"]
    assert cap.value == 20 and cap.source == "home config" and cap.in_file
    assert cap.applies == "restart"
    assert cap.doc is not None and "calendar-day cap" in cap.doc
    assert cap.spec.kind == "int" and cap.display == "20"
    build = rows["agent.models.build"]
    assert build.applies == "live" and build.source == "default" and not build.in_file
    # arrays of tables are walked, so a repo entry is addressable by key
    _seed(home, '[[github.repos]]\nrepo = "acme/app"\n')
    entry = _editor(home).describe("github.repos[0].repo")
    assert entry.value == "acme/app" and entry.in_file and entry.source == "home config"


def test_applies_for_is_the_one_rule() -> None:
    for key in (
        "model",
        "concierge.model",
        "agent.models.build",
        "github.repos[1].agent_models.review",
    ):
        assert applies_for(key) == "live", key
    for key in ("daemon.max_runs_per_day", "landing.merge_method", "concierge.timeout_s"):
        assert applies_for(key) == "restart", key


# -- doc lines from the example -------------------------------------------------


SAMPLE = """\
# Precedence: env > file = whatever, this is prose

# Model for agent sessions.
model = "auto"
keep = false  # kept for debugging

# [daemon]
# The always-on outer loop; polls every repository.
# poll_interval_s = 60.0
# trigger_label = "sbxloop:run"    # issue label that queues work
# blocked_label = "x"      # the loop could not land the PR;
#                                  # a human looks
# bare = 1

# [[github.repos]]
# repo = "you/your-repo"
# enabled = false              # registered but not polled
# [github.repos.agent_models]
# Sparse per-key overrides.
# build = "auto"
# [[github.repos]]
# enabled = true               # a second entry does not win
"""


def test_parse_pairs_each_key_with_its_comment() -> None:
    docs = parse(SAMPLE)
    assert docs["model"] == "Model for agent sessions."  # the block above
    assert docs["keep"] == "kept for debugging"  # the trailing comment
    assert docs["daemon"] == "The always-on outer loop; polls every repository."
    assert docs["daemon.poll_interval_s"] == docs["daemon"]  # first key, no comment of its own
    assert docs["daemon.trigger_label"] == "issue label that queues work"
    assert docs["daemon.blocked_label"] == "the loop could not land the PR; a human looks"
    assert "daemon.bare" not in docs  # nothing to pair with, nothing invented
    assert "github.repos.repo" not in docs
    assert docs["github.repos.enabled"] == "registered but not polled"  # first wins
    assert docs["github.repos.agent_models"] == "Sparse per-key overrides."
    assert docs["github.repos.agent_models.build"] == "Sparse per-key overrides."
    assert "Precedence" not in " ".join(docs.values())


def test_doc_for_reads_the_example_and_ignores_indices() -> None:
    assert doc_for("github.repos[1].enabled") == doc_for("github.repos.enabled")
    assert doc_for("concierge.timeout_s") == "one message's wall-clock budget"
    assert doc_for("nowhere.at.all") is None


def _model_keys() -> list[str]:
    keys: list[str] = []

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
            if f"{prefix}{name}" not in INTERNAL_KEYS | LEGACY_KEYS:
                keys.append(f"{prefix}{name}")

    walk(Config, "")
    return keys


# Keys the example lists with no comment on or above them. A new key lands
# here only by being added to the example without a sentence — add the
# comment instead of growing this number.
UNDOCUMENTED_KEYS = 99


def test_every_model_key_has_a_doc_line_or_is_counted() -> None:
    docs = doc_lines()
    keys = _model_keys()
    missing = sorted(key for key in keys if key not in docs)
    assert len(keys) > 200
    assert len(missing) == UNDOCUMENTED_KEYS, (
        f"{len(missing)} keys have no comment in sbxloop.toml.example (expected "
        f"{UNDOCUMENTED_KEYS}): {missing}"
    )
    # and no doc line is empty or a bare TOML assignment
    for key, doc in docs.items():
        assert doc and not re.fullmatch(r"[a-z_]+\s*=\s*\S+", doc), (key, doc)
