"""Guards for the recorded GithubOps error shapes (#226).

The fixture is only worth having if (a) every entry is a string the worker
could really emit, with provenance, (b) each entry replays as a
``GithubOpsError`` carrying its status as structured data (#221), and (c) no unit test smuggles
in an inline literal for a status production branches on — that inline
literal is how #219's 404-for-empty-repo stub happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sbxloop.errors import GithubOpsError
from tests.fakes.github_errors import (
    FIXTURE_PATH,
    WORKER_SHAPES,
    field_error,
    field_errors,
    github_error,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_SRC = REPO_ROOT / "packages" / "sbxloop" / "src"
UNIT_TESTS = Path(__file__).resolve().parent

_STATUS_LITERAL = re.compile(r"HTTP (\d{3})")


def _load_bearing_statuses() -> set[str]:
    """HTTP statuses production code names in a string literal or comment."""
    statuses: set[str] = set()
    for path in PRODUCTION_SRC.rglob("*.py"):
        statuses.update(_STATUS_LITERAL.findall(path.read_text()))
    return statuses


class TestFixtureShape:
    def test_every_entry_is_worker_shaped_with_provenance(self) -> None:
        assert FIXTURE_PATH.is_file()
        errors = field_errors()
        assert errors, "fixture has no entries"
        for name, entry in errors.items():
            message = entry["message"]
            matches = [shape.match(message) for shape in WORKER_SHAPES]
            match = next((m for m in matches if m), None)
            assert match is not None, f"{name}: not a worker-shaped error line: {message!r}"
            assert int(match.group("status")) == entry["status"], f"{name}: status mismatch"
            assert entry["provenance"] in ("field", "synthetic"), name
            assert entry["observed"].strip(), f"{name}: needs provenance in 'observed'"
            if entry["provenance"] == "field":
                assert re.search(r"\brun \w+", entry["observed"]), (
                    f"{name}: a field capture must name the run it came from"
                )

    def test_entries_replay_as_worker_errors_with_structured_status(self) -> None:
        """Since #221 the host branches on ``http_status``, not on string
        matching (the predicates #226 first pinned are gone with #222); the
        replayed error must therefore carry the recorded status as data."""
        for name, entry in field_errors().items():
            exc = github_error(name)
            assert isinstance(exc, GithubOpsError)
            assert exc.http_status == entry["status"], name

    def test_field_shape_for_empty_repo_is_the_409(self) -> None:
        """The #219 lesson, pinned: the empty-repo answer is field-recorded and
        it is a 409, so a future 'simplification' back to 404 fails here."""
        entry = field_errors()["empty_repo_ref_409"]
        assert entry["provenance"] == "field"
        assert "HTTP 409" in field_error("empty_repo_ref_409")


class TestNoInventedShapesInUnitTests:
    def test_every_load_bearing_status_has_a_recorded_shape(self) -> None:
        recorded = {str(entry["status"]) for entry in field_errors().values()}
        missing = _load_bearing_statuses() - recorded
        assert not missing, f"production branches on HTTP {sorted(missing)} with no fixture entry"

    @pytest.mark.parametrize("path", sorted(UNIT_TESTS.glob("test_*.py")), ids=lambda p: p.name)
    def test_unit_tests_do_not_spell_load_bearing_statuses_inline(self, path: Path) -> None:
        if path.name in (Path(__file__).name, "test_worker_githubops.py"):
            # test_worker_githubops exercises the worker's PROSE parser: its
            # inline "HTTP nnn" strings are the subject under test, not a
            # shape production branches on.
            return
        statuses = _load_bearing_statuses()
        offenders = [
            f"{path.name}:{lineno}: {line.strip()}"
            for lineno, line in enumerate(path.read_text().splitlines(), 1)
            for status in _STATUS_LITERAL.findall(line)
            # only lines that BUILD an error count; docstrings/comments that
            # merely mention a status are commentary
            if status in statuses and "Error(" in line
        ]
        assert not offenders, (
            "load-bearing GitHub error shapes must come from tests/fixtures/"
            "github_field_errors.json via tests.fakes.github_errors:\n" + "\n".join(offenders)
        )
