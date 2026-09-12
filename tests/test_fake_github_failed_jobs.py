"""The FakeGithub failed-worker-job ledger (#559).

A real non-2xx does two things: it raises host-side, and it fails the
worker job — a ``worker.error`` event and a red chronology panel. The fake
used to model only the first, so a defect whose only symptom was chronology
noise (a doomed call the code then handled correctly) was invisible to every
test built on it; that is how #518 and #556 both reached review.

These tests pin the ledger itself: a call that would have failed a job
records, an ``allow_missing``-style probe that resolves a miss as data does
not, and the two field defects' happy paths are asserted against the ledger
rather than against the absence of an exception.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from sbxloop.deliver import branch_name, deliver_workspace
from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import GithubOpsError
from tests.fakes.fake_github import FakeGithub

FOLLOWUP_LABEL = "sbxloop:follow-up"


def make_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "hello.txt").write_text("hi\n")
    return root


class TestRawLookupIsNotAFailedJob:
    """#558: the fake's ``raw_lookup`` keeps the real one's contract — a
    listed miss is an answer with no ledger entry, anything else records."""

    def test_a_listed_miss_answers_none_without_a_ledger_entry(self) -> None:
        fake = FakeGithub()
        fake.fail_once["raw"] = GithubOpsError("not found", http_status=404)
        assert fake.raw_lookup("GET", "/repos/o/r/compare/main...x") is None
        fake.assert_no_failed_jobs()

    def test_an_unlisted_status_still_records_and_raises(self) -> None:
        fake = FakeGithub()
        fake.fail_once["raw"] = GithubOpsError("forbidden", http_status=403)
        with pytest.raises(GithubOpsError):
            fake.raw_lookup("GET", "/repos/o/r/x")
        assert fake.failed_job_paths == ["raw"]

    def test_the_helper_falls_back_for_a_stand_in_without_the_method(self) -> None:
        from sbxloop.vcs.github.ops import raw_lookup

        class Bare:
            def raw(self, method: str, path: str, body: Any = None) -> Any:
                raise GithubOpsError("gone", http_status=404)

        assert raw_lookup(Bare(), "DELETE", "/x") is None
        with pytest.raises(GithubOpsError):
            raw_lookup(Bare(), "DELETE", "/x", missing=(422,))


class TestLookupsAreRawCalls:
    """#607: the repository and ref probes go through ``raw`` like the real
    facade's ops do, so they appear in the ledger, a miss costs no failed
    job, and the non-404 branches the real ``ref_lookup`` has — a refusal,
    an empty repository's 409, a malformed answer — can be exercised."""

    def test_repo_lookup_is_one_raw_get_and_a_miss_is_data(self) -> None:
        fake = FakeGithub()
        found = fake.repo_lookup("o/r")
        assert found is not None and found["default_branch"] == fake.repo_payload["default_branch"]
        assert found["has_issues"] is True  # the engine's up-front probe reads it (#631)
        assert fake.raw_calls == [("GET", "/repos/o/r", None)]
        fake.repo_missing = True
        assert fake.repo_lookup("o/r") is None
        fake.assert_no_failed_jobs()

    def test_ref_lookup_is_one_raw_get_with_the_real_facades_branches(self) -> None:
        fake = FakeGithub()
        assert fake.ref_lookup("o/r", "heads/main") == "base123"
        assert fake.raw_calls[-1] == ("GET", "/repos/o/r/git/ref/heads/main", None)
        # A delivery branch exists only once delivery created it.
        assert fake.ref_lookup("o/r", "heads/sbxloop/r1") is None
        fake.branches.add("sbxloop/r1")
        assert fake.ref_lookup("o/r", "heads/sbxloop/r1") == fake.head_sha
        # A base that is gone, or not visible to the token.
        fake.missing_refs.add("main")
        assert fake.ref_lookup("o/r", "heads/main") is None
        fake.assert_no_failed_jobs()  # every miss so far was an answer
        # The empty-repository answer is a 409, and still an answer.
        fake.empty_repo = True
        assert fake.ref_lookup("o/r", "heads/main") is None
        fake.assert_no_failed_jobs()
        fake.empty_repo = False
        # An answer without a sha raises the way the real facade does.
        fake.malformed_refs.add("develop")
        with pytest.raises(GithubOpsError, match="no sha"):
            fake.ref_lookup("o/r", "heads/develop")

    def test_a_refusal_on_a_lookup_records_and_raises(self) -> None:
        fake = FakeGithub()
        fake.fail_once["raw"] = GithubOpsError("forbidden", http_status=403)
        with pytest.raises(GithubOpsError):
            fake.ref_lookup("o/r", "heads/main")
        assert fake.failed_job_paths == ["raw"]


class TestLedgerRecordsFailures:
    """A raw() call GitHub refuses is a failed worker job, and says so."""

    def test_a_404_label_get_records_and_trips_the_assert(self) -> None:
        fake = FakeGithub()
        with pytest.raises(GithubOpsError) as excinfo:
            fake.raw("GET", "/repos/o/r/labels/absent")
        assert excinfo.value.http_status == 404
        assert fake.failed_jobs == [("raw.api", "GET", "/repos/o/r/labels/absent", 404)]
        assert fake.failed_job_paths == ["/repos/o/r/labels/absent"]
        with pytest.raises(AssertionError, match=r"failed worker jobs recorded:.*labels/absent"):
            fake.assert_no_failed_jobs()

    def test_a_422_ref_create_records(self) -> None:
        fake = FakeGithub()
        body = {"ref": "refs/heads/sbxloop/r42", "sha": "commit1"}
        fake.raw("POST", "/repos/o/r/git/refs", body)  # the first create is fine
        fake.assert_no_failed_jobs()
        with pytest.raises(GithubOpsError):
            fake.raw("POST", "/repos/o/r/git/refs", body)
        assert fake.failed_jobs == [("raw.api", "POST", "/repos/o/r/git/refs", 422)]

    def test_a_422_label_create_records(self) -> None:
        fake = FakeGithub()
        fake.labels_existing.add(FOLLOWUP_LABEL)
        with pytest.raises(GithubOpsError):
            fake.raw("POST", "/repos/o/r/labels", {"name": FOLLOWUP_LABEL})
        assert fake.failed_jobs == [("raw.api", "POST", "/repos/o/r/labels", 422)]


class TestLedgerIgnoresResolvedMisses:
    """An ``allow_missing`` probe resolves the miss as data: the worker job
    succeeds, so nothing is ledgered."""

    def test_label_lookup_miss_is_not_a_failed_job(self) -> None:
        fake = FakeGithub()
        assert fake.label_lookup("o/r", FOLLOWUP_LABEL) is None
        assert fake.failed_jobs == []
        fake.assert_no_failed_jobs()

    def test_label_lookup_hit_is_not_a_failed_job(self) -> None:
        fake = FakeGithub()
        fake.labels_existing.add(FOLLOWUP_LABEL)
        assert fake.label_lookup("o/r", FOLLOWUP_LABEL) == {"name": FOLLOWUP_LABEL}
        fake.assert_no_failed_jobs()

    def test_ref_lookup_miss_is_not_a_failed_job(self) -> None:
        fake = FakeGithub()
        assert fake.ref_lookup("o/r", "heads/sbxloop/r42") is None
        assert fake.failed_jobs == []
        fake.assert_no_failed_jobs()

    def test_a_failed_job_survives_a_later_clean_probe(self) -> None:
        """The ledger accumulates: one doomed call is not erased by the
        healthy calls around it."""
        fake = FakeGithub()
        with pytest.raises(GithubOpsError):
            fake.raw("GET", "/repos/o/r/labels/absent")
        assert fake.label_lookup("o/r", "absent") is None
        assert len(fake.failed_jobs) == 1


class TestRedeliveryLeavesNoFailedJob:
    """#518: the delivery branch is a pure function of the run id, so every
    round after the first met an existing branch. The blind refs POST could
    only 422 — one failed worker job per healthy re-delivery — which the
    force-move then papered over. The ledger is what makes that visible."""

    def _deliver(self, fake: FakeGithub, tmp_path: Path, round_no: int) -> Any:
        return deliver_workspace(
            fake,
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            round_no=round_no,
            # Round 2 lands on the PR round 1 opened; passing it keeps this
            # test about the ref path rather than the PR-collision path.
            pr_number=fake.number if round_no > 1 else None,
        )

    def test_second_round_records_no_failed_worker_job(self, tmp_path: Path) -> None:
        fake = FakeGithub()
        self._deliver(fake, tmp_path, round_no=1)
        fake.assert_no_failed_jobs()
        before = len(fake.raw_calls)

        self._deliver(fake, tmp_path, round_no=2)

        assert branch_name("r42") in fake.branches
        second_round = fake.raw_calls[before:]
        methods = {(method, path.rsplit("/git/", 1)[-1]) for method, path, _ in second_round}
        assert ("POST", "refs") not in methods, "a doomed create was made"
        assert ("PATCH", f"refs/heads/{branch_name('r42')}") in methods
        # The check #518 lacked: not "it did not raise", but "the run's
        # chronology carries no failed worker job".
        assert fake.failed_jobs == []
        fake.assert_no_failed_jobs()


class TestEnsureLabelLeavesNoFailedJob:
    """#556: the follow-up label already exists on most repositories, and
    the blind creation POST 422-ed — handled correctly, still a failed
    worker job in the run's chronology."""

    def test_existing_label_records_no_failed_worker_job(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake = FakeGithub()
        fake.labels_existing.add(FOLLOWUP_LABEL)
        with caplog.at_level(logging.DEBUG):
            LoopEngine._ensure_label(fake, "o/r", FOLLOWUP_LABEL)
        assert [c for c in fake.raw_calls if c[0] == "POST" and c[1].endswith("/labels")] == []
        assert fake.failed_jobs == []
        fake.assert_no_failed_jobs()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_absent_label_is_created_without_a_failed_worker_job(self) -> None:
        fake = FakeGithub()
        LoopEngine._ensure_label(fake, "o/r", FOLLOWUP_LABEL)
        assert fake.labels_created == [FOLLOWUP_LABEL]
        # The lookup missed, but a resolved miss is data, not a failed job.
        assert fake.failed_jobs == []
        fake.assert_no_failed_jobs()
