"""A registered repository says whether it carries the labels sbxloop
applies, and an operator can create the missing ones without leaving the
console (#630).

Nothing but ``sbxloop init-repo`` on the host could create them, so a
repository registered through the API showed the loop's states as bare
text until somebody remembered to run a command — and nothing anywhere
said which repositories were set up and which were not. The daemon now
reads one repository's labels back per tick, through the forge sandbox it
already owns, and records what it saw; a label sync creates exactly the
ones that are missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sbxloop.config import Config
from sbxloop.errors import GithubOpsError
from sbxloop.vcs.github.labels import LabelSpec, audit_labels, lifecycle_specs, sync_labels
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_daemon_loop import Harness

SPECS = [
    LabelSpec("loop:run", "0e8a16", "queued", kind="trigger"),
    LabelSpec("loop:done", "6f42c1", "landed", kind="completed"),
]


class Box:
    """The daemon's forge sandbox as the loop uses it here: the surface
    the sources use — one ``ops``, and a failure noted."""

    def __init__(self, ops: Any, *, provisioned: bool = True) -> None:
        self.ops_obj = ops
        self.failures: list[str] = []
        self.provisioned = provisioned

    def ops(self) -> Any:
        return self.ops_obj

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(str(exc))
        return False


def harness(tmp_path: Path, **daemon: Any) -> Harness:
    return Harness(
        tmp_path,
        Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": daemon,
            }
        ),
    )


class TestReadingWhatARepositoryCarries:
    def test_an_audit_names_the_labels_the_repository_lacks(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"loop:run"}
        report = audit_labels(ops, "o/r", SPECS)
        assert report.expected == ("loop:run", "loop:done")
        assert report.missing == ("loop:done",)
        assert not report.compliant
        # One listing call, whatever the size of the set.
        assert ops.label_lists == ["o/r"]

    def test_a_repository_that_carries_every_label_is_compliant(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"loop:run", "loop:done"}
        assert audit_labels(ops, "o/r", SPECS).compliant

    def test_a_name_matches_case_insensitively_as_the_forge_does(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"Loop:Run", "LOOP:DONE"}
        assert audit_labels(ops, "o/r", SPECS).compliant

    def test_a_listing_the_forge_refused_raises_rather_than_reading_as_empty(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"loop:run", "loop:done"}
        ops.fail_always["labels_list"] = GithubOpsError("gh api failed", http_status=403)
        try:
            audit_labels(ops, "o/r", SPECS)
        except GithubOpsError:
            return
        raise AssertionError("a refused listing must not read as a repository with no labels")


class TestSyncing:
    def test_only_the_missing_labels_are_created(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"loop:run"}
        report = sync_labels(ops, "o/r", SPECS)
        assert report.created == ("loop:done",)
        assert report.missing == () and report.compliant
        assert ops.label_creates == ["loop:done"]

    def test_a_repository_already_set_up_is_read_and_left_alone(self) -> None:
        ops = FakeGithub()
        ops.labels_existing = {"loop:run", "loop:done"}
        report = sync_labels(ops, "o/r", SPECS)
        assert report.created == () and report.compliant
        assert ops.label_creates == []
        assert ops.label_lists == ["o/r"]

    def test_a_label_the_forge_will_not_create_stays_missing(self) -> None:
        ops = FakeGithub()
        ops.fail_always["raw"] = GithubOpsError("no write scope", http_status=403)
        try:
            report = sync_labels(ops, "o/r", SPECS)
        except GithubOpsError:
            return  # the listing itself was refused; the audit's contract
        assert report.missing == ("loop:run", "loop:done")
        assert not report.compliant


class TestTheDaemonReadsThemBack:
    def test_a_tick_records_what_the_repository_carries(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        ops.labels_existing = {
            spec.name
            for spec in lifecycle_specs(h.config.labels_for("o/r"), h.config.landing.followup_label)
        }
        h.loop.github = Box(ops)
        h.loop.tick()
        (row,) = h.dstore.repositories()
        assert row.labels_checked_at == h.clock()
        assert row.labels_missing == ()
        assert "sbxloop:run" in row.labels_expected
        # The follow-up label is one of the set the reading answers for.
        assert h.config.landing.followup_label in row.labels_expected

    def test_the_drift_is_recorded_by_name(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        ops.labels_existing = {"sbxloop:run"}
        h.loop.github = Box(ops)
        h.loop.tick()
        (row,) = h.dstore.repositories()
        assert "sbxloop:run" not in row.labels_missing
        assert "sbxloop:failed" in row.labels_missing

    def test_a_repository_is_read_once_per_interval(self, tmp_path: Path) -> None:
        h = harness(tmp_path, label_check_interval_s=3600.0)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops)
        h.loop.tick()
        h.loop.tick()
        assert len(ops.label_lists) == 1
        h.clock.t += 3601.0
        h.loop.tick()
        assert len(ops.label_lists) == 2

    def test_renaming_a_label_makes_the_old_reading_due_again(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops)
        h.loop.tick()
        assert len(ops.label_lists) == 1
        h.config.daemon.trigger_label = "loop:go"
        h.loop.tick()
        assert len(ops.label_lists) == 2
        (row,) = h.dstore.repositories()
        assert "loop:go" in row.labels_expected

    def test_every_repository_gets_its_turn_one_per_tick(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "o/one"}, {"repo": "o/two"}]},
            }
        )
        h = Harness(tmp_path, config)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops)
        h.loop.tick()
        assert ops.label_lists == ["o/one"]
        h.loop.tick()
        assert ops.label_lists == ["o/one", "o/two"]
        h.loop.tick()
        assert ops.label_lists == ["o/one", "o/two"]

    def test_a_disabled_repository_is_not_read(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "o/one", "enabled": False}]},
            }
        )
        h = Harness(tmp_path, config)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops)
        h.loop.tick()
        assert ops.label_lists == []

    def test_zero_turns_the_reading_off(self, tmp_path: Path) -> None:
        h = harness(tmp_path, label_check_interval_s=0)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops)
        h.loop.tick()
        assert ops.label_lists == []
        (row,) = h.dstore.repositories()
        assert row.labels_checked_at is None

    def test_a_forge_that_will_not_answer_records_nothing_and_backs_off(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path, label_check_interval_s=3600.0)
        h.loop.repositories.activate()
        ops = FakeGithub()
        ops.fail_always["labels_list"] = GithubOpsError("gh api failed", http_status=403)
        h.loop.github = Box(ops)
        h.loop.tick()
        (row,) = h.dstore.repositories()
        assert row.labels_checked_at is None
        # The sandbox is told, so a dead one is replaced rather than asked again.
        assert h.loop.github.failures
        # And not once per tick while the forge is unwell.
        h.loop.tick()
        assert len(ops.label_lists) == 1

    def test_a_reading_never_boots_the_sandbox_itself(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        h.loop.github = Box(ops, provisioned=False)
        h.loop.tick()
        assert ops.label_lists == []
        # The poll boots it; the next tick reads through the box it left.
        h.loop.github.provisioned = True
        h.loop.tick()
        assert ops.label_lists == ["o/r"]

    def test_a_daemon_without_a_forge_sandbox_reads_nothing(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        assert h.loop.github is None
        h.loop.tick()
        (row,) = h.dstore.repositories()
        assert row.labels_checked_at is None


class TestSyncingThroughTheLoop:
    def test_the_sync_creates_the_missing_ones_and_records_the_result(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        ops.labels_existing = {"sbxloop:run"}
        h.loop.github = Box(ops)
        result = h.loop.sync_repo_labels("o/r", by="tester")
        assert result["state"] == "compliant"
        assert "sbxloop:failed" in result["created"]
        assert result["missing"] == []
        assert result["checked_at"] == h.clock()
        (row,) = h.dstore.repositories()
        assert row.labels_missing == () and row.labels_checked_at == h.clock()

    def test_a_repository_already_set_up_creates_nothing(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        ops = FakeGithub()
        ops.labels_existing = {
            spec.name
            for spec in lifecycle_specs(h.config.labels_for("o/r"), h.config.landing.followup_label)
        }
        h.loop.github = Box(ops)
        result = h.loop.sync_repo_labels("o/r", by="tester")
        assert result["state"] == "compliant" and result["created"] == []
        assert ops.label_creates == []

    def test_an_unregistered_repository_is_refused(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        h.loop.github = Box(FakeGithub())
        try:
            h.loop.sync_repo_labels("o/elsewhere")
        except KeyError:
            return
        raise AssertionError("a repository this daemon does not know must be refused")

    def test_without_a_forge_sandbox_the_refusal_names_the_command_that_works(
        self, tmp_path: Path
    ) -> None:
        h = harness(tmp_path)
        h.loop.repositories.activate()
        try:
            h.loop.sync_repo_labels("o/r")
        except ValueError as exc:
            assert "init-repo o/r" in str(exc)
            return
        raise AssertionError("a daemon with no forge sandbox must refuse, named")
