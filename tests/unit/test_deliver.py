"""deliver_workspace tests against a stubbed GithubOps (no network, no sbx)."""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sbxloop import hostgit
from sbxloop.deliver import (
    _plan_git_diff,
    _plan_snapshot,
    branch_name,
    deliver_workspace,
    ensure_repository,
)
from sbxloop.errors import DeliveryError, GithubOpsError
from sbxloop.gh.ops import PrRef
from tests.fakes.github_errors import github_error
from tests.fakes.gitserver import PrivateGitServer, bare_from


class StubOps:
    """Routes the git-data-API calls deliver_workspace makes; records all."""

    def __init__(self) -> None:
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.repo_get_calls: list[str] = []
        self.ref_lookups: list[tuple[str, str]] = []
        self.pr_kwargs: dict[str, Any] = {}
        self.blob_batches: list[list[dict[str, str]]] = []
        self.blob_count = 0
        # Where the run's delivery branch already sits, or None when a prior
        # delivery never created it (the first round).
        self.branch_sha: str | None = None

    def repo_get(self, repo: str) -> dict[str, Any]:
        self.repo_get_calls.append(repo)
        return {"default_branch": "main"}

    def default_branch(self, repo: str) -> str:
        # The real one's contract (#672): the reported name or an error.
        name = self.repo_get(repo).get("default_branch")
        if not name:
            raise GithubOpsError(f"GitHub did not report a default branch for {repo}")
        return str(name)

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        self.repo_get_calls.append(repo)
        return {"default_branch": "main"}

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        self.ref_lookups.append((repo, ref))
        if ref.startswith("heads/sbxloop/"):
            return self.branch_sha
        return "base123"

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        self.blob_batches.append(files)
        shas = {}
        for entry in files:
            self.blob_count += 1
            shas[entry["path"]] = f"blob{self.blob_count}"
        return shas

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "basetree"}}
        if path.endswith("/git/trees"):
            return {"sha": "tree456"}
        if path.endswith("/git/commits"):
            return {"sha": "commit789"}
        if path.endswith("/git/refs"):
            return {"ref": body["ref"] if body else ""}
        if method == "PATCH" and "/git/refs/heads/" in path:
            return {"ref": path}
        raise AssertionError(f"unexpected raw call {method} {path}")

    def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
        self.pr_kwargs = {"repo": repo, **kwargs}
        return PrRef(number=7, url="https://github.com/o/r/pull/7")


def make_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "hello.txt").write_text("hi\n")
    (root / "sub" / "logo.bin").write_bytes(b"\x00\x01\x02")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref")
    return root


class TestDeliverWorkspace:
    def test_happy_path_commits_and_opens_pr(self, tmp_path: Path) -> None:
        ops = StubOps()
        pr = deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="write hello",
            source_dir=make_workspace(tmp_path),
        )
        assert pr == PrRef(number=7, url="https://github.com/o/r/pull/7")

        # base resolved from the repo's default branch; the delivery branch
        # looked up (and found missing) before it is created
        assert ops.repo_get_calls == ["o/r"]
        assert ops.ref_lookups == [("o/r", "heads/main"), ("o/r", f"heads/{branch_name('r42')}")]

        # all blobs ride ONE batched worker job (base64, binary-safe);
        # .git excluded
        (batch,) = ops.blob_batches
        assert [e["path"] for e in batch] == ["hello.txt", "sub/logo.bin"]
        contents = {base64.b64decode(e["content_b64"]) for e in batch}
        assert contents == {b"hi\n", b"\x00\x01\x02"}

        # one tree on top of the base tree, posix relative paths, mode 100644
        (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
        assert tree_body is not None
        assert tree_body["base_tree"] == "basetree"
        assert [e["path"] for e in tree_body["tree"]] == ["hello.txt", "sub/logo.bin"]
        assert {e["mode"] for e in tree_body["tree"]} == {"100644"}

        # one commit parented on base, one branch ref for the run
        (commit_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/commits")]
        assert commit_body is not None
        assert commit_body["parents"] == ["base123"]
        assert "r42" in commit_body["message"]
        (ref_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/refs")]
        assert ref_body == {"ref": f"refs/heads/{branch_name('r42')}", "sha": "commit789"}

        # PR from the new branch onto base, artifacts listed in the body
        assert ops.pr_kwargs["base"] == "main"
        assert ops.pr_kwargs["head"] == "sbxloop/r42"
        assert ops.pr_kwargs["draft"] is False
        assert "hello.txt" in ops.pr_kwargs["body"]
        assert "sub/logo.bin" in ops.pr_kwargs["body"]
        assert "Closes #" not in ops.pr_kwargs["body"]  # no issue was named
        # ...and the exclusion is surfaced, not silent (#67)
        assert "1 file(s) excluded (.git)" in ops.pr_kwargs["body"]

    def test_closes_puts_the_closing_keyword_in_the_body(self, tmp_path: Path) -> None:
        """`Closes #N` is how GitHub links issue and PR and closes the issue
        on merge even when the daemon is not running to do it."""
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="write hello",
            source_dir=make_workspace(tmp_path),
            closes=42,
        )
        assert "Closes #42" in ops.pr_kwargs["body"]

    def test_dot_path_artifacts_are_delivered(self, tmp_path: Path) -> None:
        """.github/ and .gitignore are the point of many outcomes — they must
        ship; only the denylist (.git) stays out (#67)."""
        root = make_workspace(tmp_path)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        (root / ".gitignore").write_text("*.pyc\n")
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="add CI",
            source_dir=root,
        )
        (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
        assert tree_body is not None
        assert [e["path"] for e in tree_body["tree"]] == [
            ".github/workflows/ci.yml",
            ".gitignore",
            "hello.txt",
            "sub/logo.bin",
        ]
        assert ".github/workflows/ci.yml" in ops.pr_kwargs["body"]
        assert "1 file(s) excluded (.git)" in ops.pr_kwargs["body"]

    def test_custom_exclude_and_no_note_when_nothing_excluded(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path)
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="x",
            source_dir=root,
            exclude=[".git", "sub"],
        )
        (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
        assert tree_body is not None
        assert [e["path"] for e in tree_body["tree"]] == ["hello.txt"]
        assert "excluded (.git, sub)" in ops.pr_kwargs["body"]

        clean = tmp_path / "clean"
        clean.mkdir()
        (clean / "only.txt").write_text("x")
        ops2 = StubOps()
        deliver_workspace(
            ops2,  # type: ignore[arg-type]
            "o/r",
            run_id="r2",
            outcome="x",
            source_dir=clean,
        )
        assert "excluded" not in ops2.pr_kwargs["body"]

    def test_base_is_the_repositorys_default_branch(self, tmp_path: Path) -> None:
        """#672: a `develop`/`master` repository is delivered against that
        branch — the base commit, the merge parent and the PR all name it."""

        class DevelopOps(StubOps):
            def repo_get(self, repo: str) -> dict[str, Any]:
                self.repo_get_calls.append(repo)
                return {"default_branch": "develop"}

        ops = DevelopOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="x",
            source_dir=make_workspace(tmp_path),
        )
        assert ops.ref_lookups[0] == ("o/r", "heads/develop")
        assert ops.pr_kwargs["base"] == "develop"

    def test_no_default_branch_reported_stops_the_delivery(self, tmp_path: Path) -> None:
        """#672: no guess of `main` — the delivery fails before a single
        ref lookup or blob upload."""

        class BranchlessOps(StubOps):
            def repo_get(self, repo: str) -> dict[str, Any]:
                self.repo_get_calls.append(repo)
                return {"full_name": repo}

        ops = BranchlessOps()
        with pytest.raises(GithubOpsError, match="did not report a default branch"):
            deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        assert ops.ref_lookups == [] and ops.blob_batches == [] and ops.pr_kwargs == {}

    def test_explicit_base_skips_repo_get(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            base="develop",
            draft=True,
        )
        assert ops.repo_get_calls == []
        assert ops.ref_lookups == [
            ("o/r", "heads/develop"),
            ("o/r", f"heads/{branch_name('r1')}"),
        ]
        assert ops.pr_kwargs["base"] == "develop"
        assert ops.pr_kwargs["draft"] is True

    def test_a_repository_without_drafts_gets_a_ready_pr_on_one_retry(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#677: private repositories on GitHub Free and GHE instances
        without drafts 422 a draft PR; `deliver_draft` was a preference."""

        class NoDraftsOps(StubOps):
            def __init__(self) -> None:
                super().__init__()
                self.creates: list[dict[str, Any]] = []

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                self.creates.append(kwargs)
                if kwargs.get("draft"):
                    raise github_error("pr_draft_unsupported_422")
                return super().pr_create(repo, **kwargs)

        ops = NoDraftsOps()
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            pr = deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                draft=True,
            )
        assert pr.number == 7
        assert [c["draft"] for c in ops.creates] == [True, False]
        assert not [c for c in ops.raw_calls if "/pulls?" in c[1]], "not a collision: no lookup"
        assert any("deliver.draft_unsupported" in r.getMessage() for r in caplog.records)

    def test_a_draft_refusal_that_says_something_else_is_not_retried(self, tmp_path: Path) -> None:
        class RefusingOps(StubOps):
            def __init__(self) -> None:
                super().__init__()
                self.creates = 0

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                self.creates += 1
                raise github_error("pr_no_commits_422")

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/pulls?" in path:
                    return []  # the collision lookup finds no open PR
                return super().raw(method, path, body)

        ops = RefusingOps()
        with pytest.raises(GithubOpsError):
            deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                draft=True,
            )
        assert ops.creates == 1, "a bare 422 is a possible collision, never a draft retry"

    def test_empty_workspace_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(DeliveryError, match="nothing to deliver"):
            deliver_workspace(
                StubOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=empty,
            )

    def test_missing_blob_sha_names_file(self, tmp_path: Path) -> None:
        class DroppingOps(StubOps):
            def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
                shas = super().blobs_create_many(repo, files)
                shas.pop("sub/logo.bin")
                return shas

        with pytest.raises(DeliveryError, match=r"no blob sha for: sub/logo\.bin"):
            deliver_workspace(
                DroppingOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )

    def test_large_manifest_chunks_by_payload_not_file_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """50 files stay O(1) jobs; chunk splits happen only on the byte cap,
        and a single file over the cap still ships in its own chunk."""
        import sbxloop.deliver as deliver_mod

        root = tmp_path / "big"
        root.mkdir()
        for i in range(50):
            (root / f"f{i:02}.txt").write_text("x" * 10)
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="x",
            source_dir=root,
        )
        assert len(ops.blob_batches) == 1
        assert sum(len(batch) for batch in ops.blob_batches) == 50

        # Cap of 40 base64 bytes: ~2 ten-byte files (16 b64 chars each) per
        # chunk, and the 100-byte file exceeds the cap alone yet still ships.
        (root / "huge.bin").write_bytes(b"y" * 100)
        monkeypatch.setattr(deliver_mod, "BLOB_BATCH_MAX_B64_BYTES", 40)
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r2",
            outcome="x",
            source_dir=root,
        )
        assert len(ops.blob_batches) > 1
        delivered = [e["path"] for batch in ops.blob_batches for e in batch]
        assert len(delivered) == 51
        assert "huge.bin" in delivered
        assert all(len(batch) >= 1 for batch in ops.blob_batches)

    def test_long_outcome_clipped_in_title(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r1",
            outcome="x" * 200,
            source_dir=make_workspace(tmp_path),
        )
        assert len(ops.pr_kwargs["title"]) <= 72


class MissingRepoOps(StubOps):
    """The repository probe answers "missing" (as data, not an error — #222)
    until a creation POST lands."""

    def __init__(self, login: str = "me") -> None:
        super().__init__()
        self.exists = False
        self.login = login

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        self.repo_get_calls.append(repo)
        if not self.exists:
            return None
        return {"default_branch": "main"}

    # Models a GitHub App installation token when set: ``GET /user`` is
    # user-scoped and answers 403 "Resource not accessible by integration"
    # (#581).
    user_forbidden = False

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "GET" and path == "/user":
            self.raw_calls.append((method, path, body))
            if self.user_forbidden:
                raise GithubOpsError(
                    "github op raw.api failed: GithubOpError: gh api GET /user "
                    "failed (rc=1): gh: Resource not accessible by integration "
                    "(HTTP 403)",
                    http_status=403,
                )
            return {"login": self.login}
        if method == "POST" and (path == "/user/repos" or path.startswith("/orgs/")):
            self.raw_calls.append((method, path, body))
            self.exists = True
            return {"full_name": path}
        return super().raw(method, path, body)


class TestEnsureRepository:
    def test_existing_repo_is_left_alone(self) -> None:
        ops = StubOps()
        assert ensure_repository(ops, "o/r").created is False  # type: ignore[arg-type]
        assert ops.raw_calls == []

    def test_missing_repo_without_create_refuses(self) -> None:
        ops = MissingRepoOps()
        with pytest.raises(DeliveryError, match="--create-repo"):
            ensure_repository(ops, "me/proj")  # type: ignore[arg-type]
        # refusal happens before any mutating call
        assert all(method != "POST" for method, _, _ in ops.raw_calls)

    def test_missing_repo_created_under_user(self) -> None:
        # owner match is case-insensitive (GitHub logins are)
        ops = MissingRepoOps(login="Me")
        assert ensure_repository(ops, "me/proj", create=True).created is True  # type: ignore[arg-type]
        posts = [call for call in ops.raw_calls if call[0] == "POST"]
        assert posts == [
            ("POST", "/user/repos", {"name": "proj", "private": True, "auto_init": True})
        ]

    def test_missing_repo_created_under_org(self) -> None:
        ops = MissingRepoOps(login="me")
        assert ensure_repository(ops, "acme/proj", create=True).created is True  # type: ignore[arg-type]
        posts = [call for call in ops.raw_calls if call[0] == "POST"]
        assert posts and posts[0][1] == "/orgs/acme/repos"

    def test_creation_without_a_readable_identity_uses_the_org_route(self) -> None:
        """#581: a GitHub App installation token cannot call ``GET /user``.
        Creation then goes the organization route instead of failing on
        the identity lookup."""
        ops = MissingRepoOps()
        ops.user_forbidden = True
        assert ensure_repository(ops, "acme/proj", create=True).created is True  # type: ignore[arg-type]
        posts = [call for call in ops.raw_calls if call[0] == "POST"]
        assert posts and posts[0][1] == "/orgs/acme/repos"

    def test_create_public_flips_private(self) -> None:
        ops = MissingRepoOps()
        ensure_repository(ops, "me/proj", create=True, public=True)  # type: ignore[arg-type]
        body = next(call[2] for call in ops.raw_calls if call[0] == "POST")
        assert body is not None and body["private"] is False

    def test_non_404_probe_errors_propagate(self) -> None:
        class ForbiddenOps(StubOps):
            def repo_lookup(self, repo: str) -> dict[str, Any] | None:
                raise GithubOpsError("GET /repos -> HTTP 403: rate limited")

        with pytest.raises(GithubOpsError, match="403"):
            ensure_repository(ForbiddenOps(), "o/r", create=True)  # type: ignore[arg-type]


class TestEmptyRepoBootstrap:
    def test_missing_base_ref_bootstraps_then_delivers(self, tmp_path: Path) -> None:
        """An existing-but-empty repo (default branch named, no ref behind
        it) gets one contents-API commit, then the normal PR path runs."""

        class EmptyRepoOps(StubOps):
            def __init__(self) -> None:
                super().__init__()
                self.bootstrapped = False

            # The worker answers "missing" for both the 409 an empty repo
            # gets and the 404 an absent branch gets (#222); the host sees
            # None either way and bootstraps.
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                if ref != "heads/main":
                    return super().ref_lookup(repo, ref)
                self.ref_lookups.append((repo, ref))
                return "base123" if self.bootstrapped else None

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PUT" and path.endswith("/contents/README.md"):
                    self.raw_calls.append((method, path, body))
                    self.bootstrapped = True
                    return {"content": {"path": "README.md"}}
                return super().raw(method, path, body)

        ops = EmptyRepoOps()
        pr = deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r9",
            outcome="fresh project",
            source_dir=make_workspace(tmp_path),
        )
        assert pr.number == 7
        puts = [call for call in ops.raw_calls if call[0] == "PUT"]
        assert len(puts) == 1
        assert puts[0][2] is not None and puts[0][2]["branch"] == "main"
        # the bootstrap README round-trips as valid base64
        base64.b64decode(puts[0][2]["content"])
        # base looked up twice (the miss, then the bootstrapped base), then
        # the delivery branch once before it is created
        assert ops.ref_lookups == [("o/r", "heads/main")] * 2 + [
            ("o/r", f"heads/{branch_name('r9')}")
        ]

    def test_still_missing_after_bootstrap_is_loud(self, tmp_path: Path) -> None:
        class NeverThereOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                return None

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PUT" and path.endswith("/contents/README.md"):
                    return {"content": {"path": "README.md"}}
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError, match="still missing after bootstrap"):
            deliver_workspace(
                NeverThereOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r9",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )

    def test_unrelated_ref_errors_still_raise(self, tmp_path: Path) -> None:
        class ForbiddenRefOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                raise GithubOpsError("GET ref -> HTTP 403: rate limited")

        with pytest.raises(GithubOpsError, match="403"):
            deliver_workspace(
                ForbiddenRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r9",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )

    def test_missing_base_ref_error_still_loud_when_bootstrap_fails(self, tmp_path: Path) -> None:
        class BrokenOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                return None

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PUT" and path.endswith("/contents/README.md"):
                    raise GithubOpsError("PUT contents -> HTTP 403: token lacks contents:write")
                return super().raw(method, path, body)

        with pytest.raises(GithubOpsError, match="403"):
            deliver_workspace(
                BrokenOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r9",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )


def git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    ).stdout.strip()


def make_clone_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A source checkout with an executable script and a file to delete,
    plus the run clone hostgit would make of it. Returns (clone, base_sha)."""
    source = tmp_path / "src"
    source.mkdir()
    git("init", "-b", "main", cwd=source)
    (source / "keep.txt").write_text("keep\n")
    (source / "old.txt").write_text("old\n")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.sh").write_text("#!/bin/sh\n")
    (source / "scripts" / "run.sh").chmod(0o755)
    git("add", ".", cwd=source)
    git("commit", "-m", "init", cwd=source)
    clone = tmp_path / "clone"
    hostgit.clone_for_run(source, clone, "sbxloop/r1")
    return clone, git("rev-parse", "HEAD", cwd=source)


def deliver(ops: StubOps, source_dir: Path) -> PrRef:
    return deliver_workspace(
        ops,  # type: ignore[arg-type]
        "o/r",
        run_id="r1",
        outcome="refactor",
        source_dir=source_dir,
    )


def tree_entries(ops: StubOps) -> list[dict[str, Any]]:
    (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
    assert tree_body is not None
    return list(tree_body["tree"])


class TestGitDiffDelivery:
    """A git-checkout workspace delivers its diff, not a snapshot (#248):
    deletions propagate, exec bits survive, unchanged files stay home."""

    def test_clone_delivers_only_changes(self, tmp_path: Path) -> None:
        clone, base_sha = make_clone_workspace(tmp_path)
        (clone / "old.txt").unlink()
        (clone / "new.txt").write_text("new\n")
        (clone / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
        # a rename: delete + add in tree terms
        (clone / "keep.txt").rename(clone / "kept.txt")

        class KnownBaseOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                self.ref_lookups.append((repo, ref))
                return base_sha

        ops = KnownBaseOps()
        assert deliver(ops, clone).number == 7

        # only changed content is uploaded — keep.txt's bytes travel as kept.txt
        uploaded = {e["path"] for batch in ops.blob_batches for e in batch}
        assert uploaded == {"kept.txt", "new.txt", "scripts/run.sh"}

        by_path = {e["path"]: e for e in tree_entries(ops)}
        assert set(by_path) == {"keep.txt", "kept.txt", "new.txt", "old.txt", "scripts/run.sh"}
        # deletions are sha: null entries; the exec bit is preserved
        assert by_path["old.txt"] == {
            "path": "old.txt",
            "mode": "100644",
            "type": "blob",
            "sha": None,
        }
        assert by_path["keep.txt"]["sha"] is None
        assert by_path["scripts/run.sh"]["mode"] == "100755"
        assert by_path["new.txt"]["mode"] == "100644"
        assert by_path["new.txt"]["sha"].startswith("blob")

        # the tree still overlays the GitHub base tree and the commit
        # parents on the GitHub base commit
        (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
        assert tree_body is not None and tree_body["base_tree"] == "basetree"
        (commit_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/commits")]
        assert commit_body is not None and commit_body["parents"] == [base_sha]

        # the PR body says what happened, per path, and how it was derived
        body = ops.pr_kwargs["body"]
        assert "**Changes (5):**" in body
        assert "- D `old.txt`" in body
        assert "- A `new.txt`" in body
        assert "- M `scripts/run.sh`" in body
        assert f"git diff against `{base_sha[:12]}`" in body

    def test_unknown_remote_tip_diffs_against_clone_pin(self, tmp_path: Path) -> None:
        """The GitHub base commit is not necessarily in the clone (source
        checkout behind origin); the clone's own pin then anchors the diff
        and only the run's changes overlay the remote tip."""
        clone, base_sha = make_clone_workspace(tmp_path)
        (clone / "new.txt").write_text("new\n")
        ops = StubOps()  # base ref resolves to "base123", unknown locally
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["new.txt"]
        assert f"git diff against `{base_sha[:12]}`" in ops.pr_kwargs["body"]

    def test_committed_work_counts(self, tmp_path: Path) -> None:
        clone, _ = make_clone_workspace(tmp_path)
        (clone / "old.txt").unlink()
        (clone / "new.txt").write_text("new\n")
        git("add", "-A", cwd=clone)
        git("commit", "-m", "agent work", cwd=clone)
        ops = StubOps()
        deliver(ops, clone)
        assert {(e["path"], e["sha"] is None) for e in tree_entries(ops)} == {
            ("new.txt", False),
            ("old.txt", True),
        }

    def test_excludes_still_apply_to_diff(self, tmp_path: Path) -> None:
        """An un-ignored .venv the agent built must not ride the diff any
        more than it rides a snapshot; the exclusion is surfaced (#67)."""
        clone, _ = make_clone_workspace(tmp_path)
        (clone / ".venv" / "bin").mkdir(parents=True)
        (clone / ".venv" / "bin" / "python").write_text("bin\n")
        (clone / "new.txt").write_text("new\n")
        ops = StubOps()
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["new.txt"]
        assert "1 file(s) excluded (.venv)" in ops.pr_kwargs["body"]

    def test_no_changes_refused(self, tmp_path: Path) -> None:
        clone, _ = make_clone_workspace(tmp_path)
        ops = StubOps()
        with pytest.raises(DeliveryError, match="no changes relative to"):
            deliver(ops, clone)
        # nothing was written to GitHub
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)

    def test_agent_initialized_repo_falls_back_to_snapshot(self, tmp_path: Path) -> None:
        """A checkout with no base to diff against (git init'ed by the agent)
        still delivers — as a snapshot, and the PR body says so."""
        root = tmp_path / "ws"
        root.mkdir()
        git("init", "-b", "main", cwd=root)
        (root / "hello.txt").write_text("hi\n")
        git("add", ".", cwd=root)
        git("commit", "-m", "init", cwd=root)
        ops = StubOps()
        deliver(ops, root)
        assert [e["path"] for e in tree_entries(ops)] == ["hello.txt"]
        assert "**Files (1):**" in ops.pr_kwargs["body"]
        assert "workspace snapshot: no base commit" in ops.pr_kwargs["body"]

    def test_no_git_binary_keeps_snapshot_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone, _ = make_clone_workspace(tmp_path)
        (clone / "new.txt").write_text("new\n")
        monkeypatch.setattr(hostgit, "find_git", lambda: None)
        ops = StubOps()
        deliver(ops, clone)
        assert "keep.txt" in [e["path"] for e in tree_entries(ops)]
        assert "**Files (" in ops.pr_kwargs["body"]


class TestSnapshotModes:
    """#695: a snapshot delivery records what is on disk — exec bits and
    symlinks — the same way the git-diff plan does, instead of writing
    ``100644`` for everything and de-linking every symlink."""

    def make_workspace(self, tmp_path: Path) -> Path:
        root = tmp_path / "ws"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "run").write_text("#!/bin/sh\necho hi\n")
        (root / "bin" / "run").chmod(0o755)
        (root / "config.yml").write_text("a: 1\n")
        (root / "shared").mkdir()
        (root / "shared" / "settings.yml").write_text("b: 2\n")
        (root / "config.link.yml").symlink_to("config.yml")
        (root / "settings").symlink_to("shared")  # a directory symlink
        (root / "dangling").symlink_to("nowhere")
        return root

    def test_snapshot_keeps_exec_bits_and_symlinks(self, tmp_path: Path) -> None:
        root = self.make_workspace(tmp_path)
        ops = StubOps()
        deliver(ops, root)
        by_path = {e["path"]: e for e in tree_entries(ops)}
        assert set(by_path) == {
            "bin/run",
            "config.link.yml",
            "config.yml",
            "dangling",
            "settings",
            "shared/settings.yml",
        }
        assert by_path["bin/run"]["mode"] == "100755"
        assert by_path["config.yml"]["mode"] == "100644"
        assert by_path["config.link.yml"]["mode"] == "120000"
        assert by_path["settings"]["mode"] == "120000"
        assert by_path["dangling"]["mode"] == "120000"
        # a symlink uploads its target string, not what it points at
        uploaded = {
            e["path"]: base64.b64decode(e["content_b64"])
            for batch in ops.blob_batches
            for e in batch
        }
        assert uploaded["config.link.yml"] == b"config.yml"
        assert uploaded["settings"] == b"shared"
        assert uploaded["dangling"] == b"nowhere"
        assert uploaded["config.yml"] == b"a: 1\n"
        assert "**Files (6):**" in ops.pr_kwargs["body"]

    def test_the_two_plans_agree_on_modes(self, tmp_path: Path) -> None:
        """The same tree delivered as a snapshot and as a git diff produces
        the same modes and contents — one builder, not two."""
        root = self.make_workspace(tmp_path)
        snapshot = _plan_snapshot(root, [])
        clone, base_sha = make_clone_workspace(tmp_path)
        for name in ("bin", "shared"):
            shutil.copytree(root / name, clone / name)
        for name in ("config.yml", "config.link.yml", "settings", "dangling"):
            shutil.copy(root / name, clone / name, follow_symlinks=False)
        diff = _plan_git_diff(clone, base_sha, [])
        assert diff is not None
        snap = {e["path"]: e["mode"] for e in snapshot.entries}
        assert {e["path"]: e["mode"] for e in diff.entries if e["path"] in snap} == snap
        assert {p: diff.uploads[p] for p in snap} == snapshot.uploads


class TestSubmoduleDelivery:
    """#692: a submodule is a gitlink, delivered as the commit it points at
    — never as the deletion of the directory ``stat`` sees — and work
    inside one is named as not delivered rather than dropped."""

    @pytest.fixture
    def workspace(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        """(clone, base_sha, bump): a run clone of a superproject vendoring
        ``vendor/lib``, and a function that moves the library to a new
        commit its remote has and returns that sha."""
        lib = tmp_path / "lib"
        lib.mkdir()
        git("init", "-b", "main", cwd=lib)
        (lib / "lib.txt").write_text("v1\n")
        git("add", ".", cwd=lib)
        git("commit", "-m", "lib v1", cwd=lib)
        lib_bare = bare_from(lib, tmp_path / "remotes", "lib.git")
        with PrivateGitServer(tmp_path / "remotes", username="x", token="y", public=True) as srv:
            app = tmp_path / "app"
            app.mkdir()
            git("init", "-b", "main", cwd=app)
            (app / "README").write_text("app\n")
            git("add", ".", cwd=app)
            git("submodule", "add", "-q", f"{srv.url}/lib.git", "vendor/lib", cwd=app)
            git("commit", "-q", "-m", "init", cwd=app)
            clone = tmp_path / "clone"
            hostgit.clone_for_run(app, clone, "sbxloop/r1")
            hostgit.populate_submodules(clone, source=app, token=None)

            def bump() -> str:
                (lib / "lib.txt").write_text("v2\n")
                git("commit", "-q", "-am", "lib v2", cwd=lib)
                git("push", "-q", str(lib_bare), "main", cwd=lib)
                sha = git("rev-parse", "HEAD", cwd=lib)
                git("fetch", "-q", "origin", cwd=clone / "vendor" / "lib")
                git("checkout", "-q", sha, cwd=clone / "vendor" / "lib")
                return sha

            yield clone, git("rev-parse", "HEAD", cwd=app), bump

    def _ops(self, base_sha: str) -> StubOps:
        class KnownBaseOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                self.ref_lookups.append((repo, ref))
                return base_sha

        return KnownBaseOps()

    def test_a_bumped_gitlink_is_a_commit_entry_with_no_blob(
        self, workspace: tuple[Path, str, Any]
    ) -> None:
        clone, base_sha, bump = workspace
        sha = bump()
        (clone / "README").write_text("uses v2\n")
        ops = self._ops(base_sha)
        deliver(ops, clone)
        uploaded = {e["path"] for batch in ops.blob_batches for e in batch}
        assert uploaded == {"README"}
        by_path = {e["path"]: e for e in tree_entries(ops)}
        assert by_path["vendor/lib"] == {
            "path": "vendor/lib",
            "mode": "160000",
            "type": "commit",
            "sha": sha,
        }
        body = ops.pr_kwargs["body"]
        assert f"- M `vendor/lib` (submodule → {sha[:12]})" in body
        assert "Not delivered" not in body

    def test_work_inside_a_submodule_is_named_not_delivered(
        self, workspace: tuple[Path, str, Any]
    ) -> None:
        clone, base_sha, _ = workspace
        (clone / "vendor" / "lib" / "lib.txt").write_text("patched in place\n")
        (clone / "README").write_text("real work\n")
        ops = self._ops(base_sha)
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["README"]
        body = ops.pr_kwargs["body"]
        assert "**Not delivered:** changes inside submodule `vendor/lib` are not delivered" in body

    def test_only_work_inside_a_submodule_is_nothing_to_deliver(
        self, workspace: tuple[Path, str, Any]
    ) -> None:
        clone, base_sha, _ = workspace
        (clone / "vendor" / "lib" / "lib.txt").write_text("patched in place\n")
        with pytest.raises(DeliveryError, match="inside submodule `vendor/lib`"):
            deliver(self._ops(base_sha), clone)

    def test_an_untouched_submodule_stays_out_of_the_tree(
        self, workspace: tuple[Path, str, Any]
    ) -> None:
        clone, base_sha, _ = workspace
        (clone / "README").write_text("real work\n")
        ops = self._ops(base_sha)
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["README"]

    def test_a_removed_submodule_drops_its_path(self, workspace: tuple[Path, str, Any]) -> None:
        clone, base_sha, _ = workspace
        git("rm", "-q", "vendor/lib", cwd=clone)
        ops = self._ops(base_sha)
        deliver(ops, clone)
        by_path = {e["path"]: e for e in tree_entries(ops)}
        assert by_path["vendor/lib"] == {
            "path": "vendor/lib",
            "mode": "160000",
            "type": "commit",
            "sha": None,
        }
        assert by_path[".gitmodules"]["type"] == "blob"
        assert "- D `vendor/lib` (submodule)" in ops.pr_kwargs["body"]


@pytest.mark.skipif(hostgit.lfs_version() is None, reason="git-lfs is not installed on this host")
@pytest.mark.slow
class TestLfsDelivery:
    """#693: a file ``.gitattributes`` routes through Git LFS is refused
    rather than committed as a blob where the repository expects a pointer
    — named in the PR body, with the rest of the work still delivered."""

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> tuple[Path, str]:
        """(clone, base_sha): a populated run clone of a checkout whose
        ``*.bin`` files live in LFS."""
        app = tmp_path / "app"
        app.mkdir()
        git("init", "-b", "main", cwd=app)
        git("lfs", "install", "--local", cwd=app)
        (app / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        (app / "asset.bin").write_bytes(bytes(range(256)))
        (app / "README").write_text("app\n")
        git("add", "-A", cwd=app)
        git("commit", "-q", "-m", "init", cwd=app)
        clone = tmp_path / "clone"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        return clone, git("rev-parse", "HEAD", cwd=app)

    def _ops(self, base_sha: str) -> StubOps:
        class KnownBaseOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                self.ref_lookups.append((repo, ref))
                return base_sha

        return KnownBaseOps()

    def test_an_added_lfs_file_is_named_not_delivered(
        self, workspace: tuple[Path, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        clone, base_sha = workspace
        (clone / "new.bin").write_bytes(b"\x00" * 32)
        (clone / "README").write_text("real work\n")
        ops = self._ops(base_sha)
        with caplog.at_level(logging.WARNING):
            deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["README"]
        body = ops.pr_kwargs["body"]
        assert "**Not delivered:** LFS-tracked file `new.bin` is not delivered" in body
        assert "does not push objects to the repository's LFS store" in body
        assert any("deliver.lfs_change_skipped" in r.getMessage() for r in caplog.records)

    def test_a_modified_lfs_file_is_refused_the_same_way(self, workspace: tuple[Path, str]) -> None:
        clone, base_sha = workspace
        (clone / "asset.bin").write_bytes(b"\xff" * 32)
        (clone / "README").write_text("real work\n")
        ops = self._ops(base_sha)
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["README"]
        assert "LFS-tracked file `asset.bin` is not delivered" in ops.pr_kwargs["body"]

    def test_only_lfs_changes_is_nothing_to_deliver(self, workspace: tuple[Path, str]) -> None:
        clone, base_sha = workspace
        (clone / "new.bin").write_bytes(b"\x00" * 32)
        with pytest.raises(DeliveryError, match=r"LFS-tracked file `new\.bin`"):
            deliver(self._ops(base_sha), clone)

    def test_deleting_an_lfs_file_delivers(self, workspace: tuple[Path, str]) -> None:
        clone, base_sha = workspace
        (clone / "asset.bin").unlink()
        ops = self._ops(base_sha)
        deliver(ops, clone)
        by_path = {e["path"]: e for e in tree_entries(ops)}
        assert by_path["asset.bin"]["sha"] is None
        assert "Not delivered" not in ops.pr_kwargs["body"]

    def test_an_untouched_lfs_file_stays_out_of_the_tree(self, workspace: tuple[Path, str]) -> None:
        clone, base_sha = workspace
        (clone / "README").write_text("real work\n")
        ops = self._ops(base_sha)
        deliver(ops, clone)
        assert [e["path"] for e in tree_entries(ops)] == ["README"]
        assert "Not delivered" not in ops.pr_kwargs["body"]


def _refs_calls(ops: StubOps) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in ops.raw_calls if "/git/refs" in p]


def _force_moves(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if "deliver.branch_force_moved" in r.getMessage()
    ]


class TestRedeliveryCollisions:
    """The delivery branch is a pure function of the run id, so it already
    exists for every fix round after the first and for a manual
    `sbxloop deliver <run>` after a failed attempt (#223). Field run
    `rfxja288b` (#518) showed each such round paying a doomed refs POST —
    a `worker.error` panel per healthy re-delivery — before the force-move
    that was the real operation; the ref is now looked up first.

    StubOps records calls but not failed worker jobs; the ledger assertion
    that a healthy re-delivery leaves the chronology clean lives in
    tests/test_fake_github_failed_jobs.py (#559)."""

    def test_fresh_branch_is_created_with_one_call(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ops = StubOps()  # branch_sha None: no prior delivery
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                round_no=1,
            )
        assert _refs_calls(ops) == [("POST", "/repos/o/r/git/refs")]
        assert _force_moves(caplog) == []

    def test_existing_branch_is_force_moved_without_a_doomed_create(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Round >= 2: the lookup finds the branch, so the POST that could
        only 422 is never made — no `worker.error`, no `job_done error=`."""
        ops = StubOps()
        ops.branch_sha = "e31ae110407f0000deadbeef"
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            pr = deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                round_no=3,
            )
        assert pr.number == 7
        assert _refs_calls(ops) == [("PATCH", f"/repos/o/r/git/refs/heads/{branch_name('r42')}")]
        (patch,) = [call for call in ops.raw_calls if call[0] == "PATCH"]
        assert patch[2] == {"sha": "commit789", "force": True}
        # a fresh PR was still opened from the (moved) branch
        assert ops.pr_kwargs["head"] == branch_name("r42")
        # the event says which round superseded which commit, not "a prior
        # attempt"
        (event,) = _force_moves(caplog)
        assert "'from': 'e31ae110407f'" in event
        assert "'to': 'commit789'" in event
        assert "'round': 3" in event
        assert "'run': 'r42'" in event
        assert "prior attempt" not in event

    def test_manual_redelivery_has_no_round_to_report(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`sbxloop deliver <run>` (#223) lands on the same branch; it does
        not count rounds, and the event does not invent one."""
        ops = StubOps()
        ops.branch_sha = "abc123abc123abc123"
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        assert _refs_calls(ops) == [("PATCH", f"/repos/o/r/git/refs/heads/{branch_name('r42')}")]
        (event,) = _force_moves(caplog)
        assert "'from': 'abc123abc123'" in event and "'round'" not in event

    def test_branch_appearing_between_lookup_and_create_is_still_force_moved(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The race the 422 catch is kept for: the lookup misses, then the
        create collides anyway. Delivery still lands rather than failing."""

        class RacedOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    self.raw_calls.append((method, path, body))
                    raise github_error("ref_exists_422")
                return super().raw(method, path, body)

        ops = RacedOps()
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            pr = deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                round_no=1,
            )
        assert pr.number == 7
        assert _refs_calls(ops) == [
            ("POST", "/repos/o/r/git/refs"),
            ("PATCH", f"/repos/o/r/git/refs/heads/{branch_name('r42')}"),
        ]
        (event,) = _force_moves(caplog)
        assert "'to': 'commit789'" in event and "'from'" not in event
        assert "between the lookup and the create" in event

    def test_existing_open_pr_is_reused(self, tmp_path: Path) -> None:
        class PrExistsOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/pulls?" in path:
                    self.raw_calls.append((method, path, body))
                    return [{"number": 12, "html_url": "https://github.com/o/r/pull/12"}]
                return super().raw(method, path, body)

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                raise github_error("pr_exists_422")

        ops = PrExistsOps()
        pr = deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
        )
        assert pr == PrRef(number=12, url="https://github.com/o/r/pull/12")
        lookups = [call for call in ops.raw_calls if call[0] == "GET" and "/pulls?" in call[1]]
        assert lookups == [
            ("GET", f"/repos/o/r/pulls?state=open&head=o:{branch_name('r42')}", None)
        ]

    def test_pr_collision_without_open_pr_stays_loud(self, tmp_path: Path) -> None:
        class GhostPrOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/pulls?" in path:
                    return []
                return super().raw(method, path, body)

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                raise github_error("pr_exists_422")

        with pytest.raises(GithubOpsError, match="already exists"):
            deliver_workspace(
                GhostPrOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )

    def test_unrelated_ref_post_errors_still_raise(self, tmp_path: Path) -> None:
        class ForbiddenRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    raise GithubOpsError("POST refs -> HTTP 403: token lacks contents:write")
                return super().raw(method, path, body)

        with pytest.raises(GithubOpsError, match="403"):
            deliver_workspace(
                ForbiddenRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )


class TestRedeliveryOntoAKnownPr:
    """Field run r8tzse1qa (#387): round two moved the branch, blind-POSTed a
    new PR, got gh's bare "Validation Failed (HTTP 422)" and died — with the
    PR number in hand all along."""

    def test_known_pr_number_skips_the_create(self, tmp_path: Path) -> None:
        class KnownPrOps(StubOps):
            def pr_get(self, repo: str, number: int) -> dict[str, Any]:
                return {"number": number, "html_url": f"https://github.com/o/r/pull/{number}"}

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                raise AssertionError("a known PR must not be re-created")

        ops = KnownPrOps()
        pr = deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            pr_number=12,
        )
        assert pr == PrRef(number=12, url="https://github.com/o/r/pull/12")

    def test_a_bare_422_is_confirmed_by_the_open_pr_lookup(self, tmp_path: Path) -> None:
        class BareGhOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/pulls?" in path:
                    return [{"number": 12, "html_url": "https://github.com/o/r/pull/12"}]
                return super().raw(method, path, body)

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                raise GithubOpsError(
                    "github op pr.create failed: GithubOpError: gh api POST /repos/o/r/pulls "
                    "failed (rc=1): gh: Validation Failed (HTTP 422)",
                    http_status=422,
                )

        pr = deliver_workspace(
            BareGhOps(),  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
        )
        assert pr == PrRef(number=12, url="https://github.com/o/r/pull/12")


class TestContinuedHistory:
    """A restart that adopts a previous attempt's branch (#600) parents on
    that branch's head — but its tree is still the run's own workspace,
    diffed against base. Building the tree on the prior head's tree instead
    would deliver the union of the previous attempt's whole tree and this
    run's base-relative changes: a tree neither the agent built nor the
    reviewer diffed, keeping every file the previous attempt got wrong."""

    def test_parent_keeps_history_without_inheriting_the_prior_tree(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        source.mkdir()
        git("init", "-b", "main", cwd=source)
        git("config", "user.email", "t@e.st", cwd=source)
        git("config", "user.name", "t", cwd=source)
        (source / "keep.txt").write_text("keep\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "init", cwd=source)
        base_sha = git("rev-parse", "HEAD", cwd=source)
        clone = tmp_path / "clone"
        hostgit.clone_for_run(source, clone, "sbxloop/rnew")
        # What a restarted run's fresh base-cut clone looks like.
        (clone / "new.txt").write_text("new\n")

        class KnownBaseOps(StubOps):
            def ref_lookup(self, repo: str, ref: str) -> str | None:
                self.ref_lookups.append((repo, ref))
                if ref.startswith("heads/sbxloop/"):
                    return "PRIORHEAD"
                return base_sha

            def raw(self, method: str, path: str, body: Any = None) -> Any:
                if method == "GET" and "/git/commits/" in path:
                    self.raw_calls.append((method, path, body))
                    sha = path.rsplit("/", 1)[-1]
                    return {"tree": {"sha": f"TREE_OF_{sha}"}}
                return super().raw(method, path, body)

        ops = KnownBaseOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="rnew",
            outcome="o",
            source_dir=clone,
            base="main",
            branch="sbxloop/prev",
            parent="PRIORHEAD",
        )
        (tree_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/trees")]
        assert tree_body is not None
        # The tree is layered on BASE's tree, not the previous attempt's.
        assert tree_body["base_tree"] == f"TREE_OF_{base_sha}"
        assert [e["path"] for e in tree_body["tree"]] == ["new.txt"]
        # The prior commit is still the parent, so its history survives.
        (commit_body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/commits")]
        assert commit_body is not None and commit_body["parents"] == ["PRIORHEAD"]


class TestPrConventions:
    """#678: the repository's pull request template opens the body, the
    agent's own `.sbxloop/pr-body` wins over it, and a title lint in the
    tree is detected — each from the delivered tree itself."""

    def test_the_template_opens_the_body_verbatim(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path)
        (root / ".github").mkdir()
        (root / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(
            "## Summary\n\n## Checklist\n- [ ] tests\n"
        )
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=root,
            closes=5,
        )
        body = ops.pr_kwargs["body"]
        assert body.startswith("## Summary\n\n## Checklist\n- [ ] tests\n\n---\n\n")
        assert "Artifacts produced by sbxloop run `r42`." in body
        assert body.endswith("\nCloses #5\n")

    def test_the_agents_body_wins_over_the_template(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path)
        (root / "PULL_REQUEST_TEMPLATE.md").write_text("## Summary\n")
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=root,
            closes=5,
            authored_body="  ## Summary\n\nDid the thing.\n\n- [x] tests\n\n",
        )
        body = ops.pr_kwargs["body"]
        assert body.startswith("## Summary\n\nDid the thing.\n\n- [x] tests\n\n---\n\n")
        assert "Files (" not in body, "the authored body replaces the summary"
        assert "sbxloop run `r42`" in body and body.endswith("\nCloses #5\n")

    def test_without_either_the_body_reads_as_before(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
        )
        body = ops.pr_kwargs["body"]
        assert body.startswith("Artifacts produced by sbxloop run `r42`.\n\n**Outcome:** x\n")
        assert "---" not in body

    def test_a_redelivery_with_an_authored_body_rewrites_the_open_pr(self, tmp_path: Path) -> None:
        class KnownPrOps(StubOps):
            def pr_get(self, repo: str, number: int) -> dict[str, Any]:
                return {"number": number, "html_url": "https://github.com/o/r/pull/12"}

            def pr_create(self, repo: str, **kwargs: Any) -> PrRef:
                raise AssertionError("a known PR must not be re-created")

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PATCH" and path.endswith("/pulls/12"):
                    self.raw_calls.append((method, path, body))
                    return {}
                return super().raw(method, path, body)

        ops = KnownPrOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            pr_number=12,
            authored_body="## Fixed\n",
        )
        (patch,) = [b for m, p, b in ops.raw_calls if m == "PATCH" and p.endswith("/pulls/12")]
        assert patch is not None and patch["body"].startswith("## Fixed\n\n---\n\n")

    def test_a_redelivery_without_one_leaves_the_body_alone(self, tmp_path: Path) -> None:
        class KnownPrOps(StubOps):
            def pr_get(self, repo: str, number: int) -> dict[str, Any]:
                return {"number": number, "html_url": "https://github.com/o/r/pull/12"}

        ops = KnownPrOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            pr_number=12,
        )
        assert not [b for m, p, b in ops.raw_calls if m == "PATCH" and p.endswith("/pulls/12")]

    def test_pr_template_takes_githubs_first_choice_and_skips_empty_ones(
        self, tmp_path: Path
    ) -> None:
        from sbxloop.deliver import pr_template

        # One spelling per directory: the checkout may sit on a
        # case-insensitive filesystem, where the two spellings are one file.
        root = make_workspace(tmp_path)
        assert pr_template(root) is None
        (root / "docs").mkdir()
        (root / "docs" / "pull_request_template.md").write_text("docs one\n")
        assert pr_template(root)[1] == "docs one"  # type: ignore[index]
        (root / "PULL_REQUEST_TEMPLATE.md").write_text("root one\n")
        assert pr_template(root) == ("PULL_REQUEST_TEMPLATE.md", "root one")
        (root / ".github").mkdir()
        (root / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("  \n")
        assert pr_template(root) == ("PULL_REQUEST_TEMPLATE.md", "root one"), (
            "an empty template is no template"
        )
        (root / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("gh one\n")
        assert pr_template(root) == (".github/PULL_REQUEST_TEMPLATE.md", "gh one")

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ({}, None),
            ({"commitlint.config.js": "module.exports = {}"}, "commitlint.config.js"),
            ({".commitlintrc.yml": "extends: []"}, ".commitlintrc.yml"),
            ({"package.json": '{"commitlint": {}}'}, "package.json (commitlint)"),
            ({"package.json": '{"name": "x"}'}, None),
            ({"package.json": "not json"}, None),
            (
                {".github/workflows/pr.yml": "uses: amannn/action-semantic-pull-request@v5\n"},
                ".github/workflows/pr.yml (amannn/action-semantic-pull-request)",
            ),
            (
                {".github/workflows/lint.yaml": "uses: wagoid/commitlint-github-action@v6\n"},
                ".github/workflows/lint.yaml (wagoid/commitlint-github-action)",
            ),
            ({".github/workflows/ci.yml": "uses: actions/checkout@v4\n"}, None),
            ({"src/commitlint.config.js": "x"}, None),
        ],
    )
    def test_conventional_titles_names_the_evidence(
        self, tmp_path: Path, files: dict[str, str], expected: str | None
    ) -> None:
        from sbxloop.deliver import conventional_titles

        root = make_workspace(tmp_path)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        assert conventional_titles(root) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("feat(api): add the endpoint", "feat(api): add the endpoint"),
            ("fix!: drop  the\n thing", "fix!: drop the thing"),
            ("Add the endpoint", "chore: add the endpoint"),
            ("sbxloop: Add the endpoint", "chore: sbxloop: Add the endpoint"),
            ("Feat: shouting type", "chore: feat: shouting type"),
            ("revert: the last change", "revert: the last change"),
            ("", "chore: deliver artifacts"),
        ],
    )
    def test_conventional_title_keeps_one_and_guesses_otherwise(
        self, title: str, expected: str
    ) -> None:
        from sbxloop.deliver import conventional_title

        assert conventional_title(title) == expected

    def test_pr_conventions_is_a_paragraph_only_when_the_workspace_says_so(
        self, tmp_path: Path
    ) -> None:
        from sbxloop.deliver import pr_conventions

        root = make_workspace(tmp_path)
        assert pr_conventions(None) == "" and pr_conventions(root) == ""
        (root / "commitlint.config.js").write_text("x")
        text = pr_conventions(root)
        assert "`commitlint.config.js`" in text and "`type(scope): summary`" in text
        assert "pr-body" not in text
        (root / "PULL_REQUEST_TEMPLATE.md").write_text("## Why\n")
        text = pr_conventions(root)
        assert "`PULL_REQUEST_TEMPLATE.md`" in text and "`.sbxloop/pr-body`" in text
        assert text.count("\n- ") == 1 and text.startswith("- ")


class TestNaming:
    """#621: the PR title and commit message are the operator's templates,
    rendered with the plan's title when the model gave one; unset, they
    are byte-for-byte what the loop always wrote."""

    def test_render_naming(self) -> None:
        from sbxloop.deliver import render_naming

        out = render_naming(
            "{repo} · {title} · {outcome} · {run_id}",
            title="  Add   the\nthing ",
            outcome="o",
            run_id="r1",
            repo="o/r",
        )
        assert out == "o/r · Add the thing · o · r1"
        assert render_naming("{title}", title=None, outcome="fallback", run_id="r", repo="x") == (
            "fallback"
        )

    def test_defaults_render_identically_with_no_title(self, tmp_path: Path) -> None:
        from sbxloop.config import DEFAULT_COMMIT_MESSAGE_TEMPLATE, DEFAULT_PR_TITLE_TEMPLATE
        from sbxloop.deliver import render_naming

        plain, templated = StubOps(), StubOps()
        kwargs: dict[str, Any] = {"run_id": "r42", "outcome": "write hello"}
        deliver_workspace(plain, "o/r", source_dir=make_workspace(tmp_path), **kwargs)  # type: ignore[arg-type]
        deliver_workspace(
            templated,  # type: ignore[arg-type]
            "o/r",
            source_dir=make_workspace(tmp_path / "b"),
            title=render_naming(DEFAULT_PR_TITLE_TEMPLATE, title=None, repo="o/r", **kwargs),
            commit_message=render_naming(
                DEFAULT_COMMIT_MESSAGE_TEMPLATE, title=None, repo="o/r", **kwargs
            ),
            **kwargs,
        )
        assert plain.pr_kwargs["title"] == templated.pr_kwargs["title"] == "sbxloop: write hello"
        commits = [
            b["message"]
            for ops in (plain, templated)
            for _, p, b in ops.raw_calls
            if p.endswith("/git/commits") and b
        ]
        assert commits == ["sbxloop run r42: deliver artifacts\n\nOutcome: write hello"] * 2

    def test_a_given_title_and_message_are_used_and_the_title_clipped(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            title="feat: " + "y" * 100,
            commit_message="feat: the thing\n\nBody.",
        )
        assert ops.pr_kwargs["title"].startswith("feat: yyy") and len(ops.pr_kwargs["title"]) == 72
        (body,) = [b for _, p, b in ops.raw_calls if p.endswith("/git/commits")]
        assert body is not None and body["message"] == "feat: the thing\n\nBody."

    def test_a_redelivery_renames_the_pr_when_the_title_changed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        class TitledOps(StubOps):
            def pr_get(self, repo: str, number: int) -> dict[str, Any]:
                return {
                    "number": number,
                    "html_url": "https://github.com/o/r/pull/12",
                    "title": "old",
                }

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PATCH" and path.endswith("/pulls/12"):
                    self.raw_calls.append((method, path, body))
                    return {"title": body["title"] if body else ""}
                return super().raw(method, path, body)

        ops = TitledOps()
        with caplog.at_level(logging.INFO, logger="sbxloop.deliver"):
            deliver_workspace(
                ops,  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
                pr_number=12,
                title="new",
            )
        assert ("PATCH", "/repos/o/r/pulls/12", {"title": "new"}) in ops.raw_calls
        assert any("deliver.title_changed" in r.getMessage() for r in caplog.records)

        # Same title: nothing to say to GitHub.
        ops = TitledOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path / "b"),
            pr_number=12,
            title="old",
        )
        assert not any(m == "PATCH" and p.endswith("/pulls/12") for m, p, _ in ops.raw_calls)

    def test_a_refused_rename_does_not_fail_the_delivery(self, tmp_path: Path) -> None:
        class RefusingOps(StubOps):
            def pr_get(self, repo: str, number: int) -> dict[str, Any]:
                return {
                    "number": number,
                    "html_url": "https://github.com/o/r/pull/12",
                    "title": "old",
                }

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "PATCH" and path.endswith("/pulls/12"):
                    raise GithubOpsError("locked", http_status=403)
                return super().raw(method, path, body)

        pr = deliver_workspace(
            RefusingOps(),  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            pr_number=12,
            title="new",
        )
        assert pr.number == 12

    def test_a_ref_422_naming_signatures_names_signing_not_the_prefix(self, tmp_path: Path) -> None:
        """#677: every non-collision 422 used to be "the branch name"."""

        class RefusedRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    raise github_error("ref_signature_required_422")
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError) as info:
            deliver_workspace(
                RefusedRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        text = str(info.value)
        assert "verified signatures" in text and "signed commits" in text
        assert "GitHub App" in text and "branch_prefix" not in text

    def test_a_ref_422_naming_a_lock_says_so(self, tmp_path: Path) -> None:
        class RefusedRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    raise github_error("ref_locked_422")
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError, match="locked") as info:
            deliver_workspace(
                RefusedRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        assert "branch_prefix" not in str(info.value)

    def test_an_unrecognised_ref_422_quotes_github_and_names_no_knob(self, tmp_path: Path) -> None:
        class RefusedRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    raise github_error("ref_refused_unclassified_422")
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError) as info:
            deliver_workspace(
                RefusedRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        text = str(info.value)
        assert "Something about the size limit" in text
        assert "branch_prefix" not in text and "signed" not in text and "locked" not in text

    def test_a_ruleset_refusing_the_branch_names_the_knob(self, tmp_path: Path) -> None:
        class RefusedRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "POST" and path.endswith("/git/refs"):
                    raise github_error("ref_creation_restricted_422")
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError, match=r"\[github\] branch_prefix") as info:
            deliver_workspace(
                RefusedRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r42",
                outcome="x",
                source_dir=make_workspace(tmp_path),
            )
        assert "'sbxloop/r42'" in str(info.value)
        assert "creations being restricted" in str(info.value)


class TestVerificationSection:
    """#682: what the sandbox's checks did not decide closes the body as its
    own section, before `Closes`, whichever way the body was written."""

    NOTE = 'The operator set `verify_mode = "advisory"`: these checks failed\n- task t1: x'

    def test_the_section_precedes_closes_in_the_summary_body(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            closes=5,
            verification=self.NOTE,
        )
        body = ops.pr_kwargs["body"]
        assert body.startswith("Artifacts produced by sbxloop run `r42`.")
        assert f"\n**Verification:** {self.NOTE}\n\nCloses #5\n" in body
        assert body.endswith("\nCloses #5\n")

    def test_the_section_follows_an_authored_body(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            authored_body="## Summary\n\nDid it.\n",
            verification=self.NOTE,
        )
        body = ops.pr_kwargs["body"]
        assert body.startswith(
            "## Summary\n\nDid it.\n\n---\n\nArtifacts produced by sbxloop run `r42`."
        )
        assert body.endswith(f"\n**Verification:** {self.NOTE}\n")

    def test_no_note_no_section(self, tmp_path: Path) -> None:
        ops = StubOps()
        deliver_workspace(
            ops,  # type: ignore[arg-type]
            "o/r",
            run_id="r42",
            outcome="x",
            source_dir=make_workspace(tmp_path),
            verification="  ",
        )
        assert "Verification" not in ops.pr_kwargs["body"]
