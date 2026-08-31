"""deliver_workspace tests against a stubbed GithubOps (no network, no sbx)."""

from __future__ import annotations

import base64
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sbxloop import hostgit
from sbxloop.deliver import branch_name, deliver_workspace, ensure_repository
from sbxloop.errors import DeliveryError, GithubOpsError
from sbxloop.gh.ops import PrRef
from tests.fakes.github_errors import github_error


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
        assert ensure_repository(ops, "o/r") is False  # type: ignore[arg-type]
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
        assert ensure_repository(ops, "me/proj", create=True) is True  # type: ignore[arg-type]
        posts = [call for call in ops.raw_calls if call[0] == "POST"]
        assert posts == [
            ("POST", "/user/repos", {"name": "proj", "private": True, "auto_init": True})
        ]

    def test_missing_repo_created_under_org(self) -> None:
        ops = MissingRepoOps(login="me")
        assert ensure_repository(ops, "acme/proj", create=True) is True  # type: ignore[arg-type]
        posts = [call for call in ops.raw_calls if call[0] == "POST"]
        assert posts and posts[0][1] == "/orgs/acme/repos"

    def test_creation_without_a_readable_identity_uses_the_org_route(self) -> None:
        """#581: a GitHub App installation token cannot call ``GET /user``.
        Creation then goes the organization route instead of failing on
        the identity lookup."""
        ops = MissingRepoOps()
        ops.user_forbidden = True
        assert ensure_repository(ops, "acme/proj", create=True) is True  # type: ignore[arg-type]
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
    that was the real operation; the ref is now looked up first."""

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
