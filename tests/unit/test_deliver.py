"""deliver_workspace tests against a stubbed GithubOps (no network, no sbx)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from sbxloop.deliver import branch_name, deliver_workspace, ensure_repository
from sbxloop.errors import DeliveryError, GithubOpsError
from sbxloop.gh.ops import PrRef


class StubOps:
    """Routes the git-data-API calls deliver_workspace makes; records all."""

    def __init__(self) -> None:
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.repo_get_calls: list[str] = []
        self.pr_kwargs: dict[str, Any] = {}
        self.blob_batches: list[list[dict[str, str]]] = []
        self.blob_count = 0

    def repo_get(self, repo: str) -> dict[str, Any]:
        self.repo_get_calls.append(repo)
        return {"default_branch": "main"}

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        self.blob_batches.append(files)
        shas = {}
        for entry in files:
            self.blob_count += 1
            shas[entry["path"]] = f"blob{self.blob_count}"
        return shas

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "base123"}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "basetree"}}
        if path.endswith("/git/trees"):
            return {"sha": "tree456"}
        if path.endswith("/git/commits"):
            return {"sha": "commit789"}
        if path.endswith("/git/refs"):
            return {"ref": body["ref"] if body else ""}
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

        # base resolved from the repo's default branch
        assert ops.repo_get_calls == ["o/r"]
        assert ("GET", "/repos/o/r/git/ref/heads/main", None) in ops.raw_calls

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
        # ...and the exclusion is surfaced, not silent (#67)
        assert "1 file(s) excluded (.git)" in ops.pr_kwargs["body"]

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
        assert ("GET", "/repos/o/r/git/ref/heads/develop", None) in ops.raw_calls
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

    def test_unresolvable_base_branch(self, tmp_path: Path) -> None:
        class NoRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if "/git/ref/heads/" in path:
                    return {"message": "Not Found"}
                return super().raw(method, path, body)

        with pytest.raises(DeliveryError, match="cannot resolve base branch"):
            deliver_workspace(
                NoRefOps(),  # type: ignore[arg-type]
                "o/r",
                run_id="r1",
                outcome="x",
                source_dir=make_workspace(tmp_path),
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
    """repo_get 404s until a creation POST lands."""

    def __init__(self, login: str = "me") -> None:
        super().__init__()
        self.exists = False
        self.login = login

    def repo_get(self, repo: str) -> dict[str, Any]:
        self.repo_get_calls.append(repo)
        if not self.exists:
            raise GithubOpsError("github op repo.get failed: GET /repos -> HTTP 404: Not Found")
        return {"default_branch": "main"}

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "GET" and path == "/user":
            self.raw_calls.append((method, path, body))
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

    def test_create_public_flips_private(self) -> None:
        ops = MissingRepoOps()
        ensure_repository(ops, "me/proj", create=True, public=True)  # type: ignore[arg-type]
        body = next(call[2] for call in ops.raw_calls if call[0] == "POST")
        assert body is not None and body["private"] is False

    def test_non_404_probe_errors_propagate(self) -> None:
        class ForbiddenOps(StubOps):
            def repo_get(self, repo: str) -> dict[str, Any]:
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

            # The exact worker-shaped error real GitHub produces for an
            # empty repo — field-verified on run rgwp5z40x (it is a 409,
            # NOT a 404; the stub used 404 and the field disagreed).
            error = (
                "github op raw.api failed: GithubOpError: gh api GET "
                "/repos/o/r/git/ref/heads/main failed (rc=1): gh: Git "
                "Repository is empty. (HTTP 409)"
            )

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/git/ref/heads/" in path and not self.bootstrapped:
                    self.raw_calls.append((method, path, body))
                    raise GithubOpsError(self.error)
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

    def test_404_missing_ref_also_bootstraps(self, tmp_path: Path) -> None:
        """An explicit `base` naming a branch that does not exist yet gets
        the 404 shape; it means the same thing (no base to build on)."""

        class MissingRefOps(StubOps):
            def __init__(self) -> None:
                super().__init__()
                self.bootstrapped = False

            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/git/ref/heads/" in path and not self.bootstrapped:
                    raise GithubOpsError("GET ref -> HTTP 404: Not Found")
                if method == "PUT" and path.endswith("/contents/README.md"):
                    self.bootstrapped = True
                    return {"content": {"path": "README.md"}}
                return super().raw(method, path, body)

        pr = deliver_workspace(
            MissingRefOps(),  # type: ignore[arg-type]
            "o/r",
            run_id="r9",
            outcome="x",
            source_dir=make_workspace(tmp_path),
        )
        assert pr.number == 7

    def test_unrelated_ref_errors_still_raise(self, tmp_path: Path) -> None:
        class ForbiddenRefOps(StubOps):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/git/ref/heads/" in path:
                    raise GithubOpsError("GET ref -> HTTP 403: rate limited")
                return super().raw(method, path, body)

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
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "GET" and "/git/ref/heads/" in path:
                    raise GithubOpsError("gh: Git Repository is empty. (HTTP 409)")
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
