"""deliver_workspace tests against a stubbed GithubOps (no network, no sbx)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from sbxloop.deliver import branch_name, deliver_workspace
from sbxloop.errors import DeliveryError
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
        # dotfiles excluded
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
