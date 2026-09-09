"""hostgit tests against real git repos built in tmp_path.

The helpers wrap GitPython (which drives the system git binary); tests
arrange fixtures with plain subprocess git so the arrangement is
independent of the code under test.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from git import GitCommandError, Repo

from sbxloop import hostgit
from sbxloop.errors import DeliveryError, ProvisionError
from sbxloop_worker.gitops import GitMergeError, merge_from_base
from tests.fakes.gitserver import PrivateGitServer, bare_from


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_SSL_CAINFO": os.environ.get("GIT_SSL_CAINFO", ""),
        },
    )


def make_repo(tmp_path: Path, name: str = "src") -> Path:
    root = tmp_path / name
    root.mkdir()
    git("init", "-b", "main", cwd=root)
    (root / "hello.txt").write_text("hi\n")
    git("add", ".", cwd=root)
    git("commit", "-m", "init", cwd=root)
    return root


class TestRepoToplevel:
    def test_root_returns_itself(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        assert hostgit.repo_toplevel(root) == root.resolve()

    def test_subdir_returns_root(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        sub = root / "sub"
        sub.mkdir()
        assert hostgit.repo_toplevel(sub) == root.resolve()

    def test_plain_dir_is_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert hostgit.repo_toplevel(plain) is None


class TestDirtyAndHead:
    def test_clean_repo_is_not_dirty(self, tmp_path: Path) -> None:
        assert hostgit.is_dirty(make_repo(tmp_path)) is False

    def test_modified_file_is_dirty(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / "hello.txt").write_text("changed\n")
        assert hostgit.is_dirty(root) is True

    def test_untracked_file_is_dirty(self, tmp_path: Path) -> None:
        # Untracked files would silently not travel into a clone; they must
        # trip the dirty refusal like modifications do.
        root = make_repo(tmp_path)
        (root / "new.txt").write_text("x\n")
        assert hostgit.is_dirty(root) is True

    def test_ignored_names_do_not_count(self, tmp_path: Path) -> None:
        """sbxloop's own state dir dropped inside a checkout is run state,
        not user content (field failure r5a1d9m9c)."""
        root = make_repo(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        assert hostgit.is_dirty(root) is True  # counted without ignore
        assert hostgit.is_dirty(root, ignore=[".sbxloop"]) is False

    def test_real_changes_still_count_alongside_ignored_names(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / ".sbxloop").mkdir()
        (root / ".sbxloop" / "state.db").write_text("db\n")
        (root / "hello.txt").write_text("changed\n")
        assert hostgit.is_dirty(root, ignore=[".sbxloop"]) is True

    def test_head_commit_sha(self, tmp_path: Path) -> None:
        sha = hostgit.head_commit(make_repo(tmp_path))
        assert sha is not None and len(sha) == 40

    def test_unborn_head_is_none(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        git("init", "-b", "main", cwd=root)
        assert hostgit.head_commit(root) is None


class TestCloneForRun:
    def test_clone_is_self_contained_on_branch(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        target = tmp_path / "state" / "runs" / "r1" / "workspace"
        target.parent.mkdir(parents=True)

        sha = hostgit.clone_for_run(source, target, "sbxloop/r1")

        assert sha == hostgit.head_commit(source)
        assert (target / "hello.txt").read_text() == "hi\n"
        # self-contained: a real .git directory, no alternates into the source
        assert (target / ".git").is_dir()
        assert not (target / ".git" / "objects" / "info" / "alternates").exists()
        # on the run branch
        head = (target / ".git" / "HEAD").read_text().strip()
        assert head.endswith("refs/heads/sbxloop/r1")
        # the source repo is untouched: no new branch, same HEAD
        assert not (source / ".git" / "refs" / "heads" / "sbxloop").exists()
        assert hostgit.head_commit(source) == sha

    def test_clone_pins_base_ref(self, tmp_path: Path) -> None:
        """The commit the clone was cut from is pinned so delivery can diff
        against it after the agent commits or moves the branch (#248)."""
        source = make_repo(tmp_path)
        target = tmp_path / "target"
        sha = hostgit.clone_for_run(source, target, "sbxloop/r1")
        pinned = subprocess.run(
            ["git", "rev-parse", hostgit.CLONE_BASE_REF],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert pinned == sha
        # the pin is not a branch: it must never show up as one to the agent
        assert (
            "sbxloop/base"
            not in subprocess.run(
                ["git", "branch", "--list"], cwd=target, check=True, capture_output=True, text=True
            ).stdout
        )

    def test_clone_failure_raises_provision_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProvisionError, match="cloning workspace"):
            hostgit.clone_for_run(tmp_path / "nope", tmp_path / "target", "sbxloop/r1")

    def test_find_git_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostgit.shutil, "which", lambda name: None)
        assert hostgit.find_git() is None


def touch(root: Path, *rels: str) -> None:
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")


class TestGitignoredFiles:
    """Build byproducts a project gitignores (wheels, dist/, generated
    _version.py) must not reach a delivered PR (#249)."""

    def test_checkout_uses_its_index(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / ".gitignore").write_text("dist/\n_vendor/\n_version.py\n")
        touch(root, "dist/a.whl", "pkg/_vendor/w.whl", "pkg/_version.py", "src/m.py")
        # A force-added ignored file is tracked, hence a deliverable.
        git("add", "-f", "pkg/_version.py", cwd=root)
        assert hostgit.gitignored_files(root) == {"dist/a.whl", "pkg/_vendor/w.whl"}

    def test_plain_tree_honours_in_tree_gitignore(self, tmp_path: Path) -> None:
        """Harvested copies carry .gitignore but never .git."""
        root = tmp_path / "harvest"
        root.mkdir()
        (root / ".gitignore").write_text("dist/\n")
        (root / "pkg").mkdir()
        (root / "pkg" / ".gitignore").write_text("_version.py\n")
        touch(root, "dist/a.whl", "pkg/_version.py", "pkg/m.py", "_version.py")
        assert hostgit.gitignored_files(root) == {"dist/a.whl", "pkg/_version.py"}

    def test_broken_dot_git_falls_back_to_plain_probe(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref\n")
        (root / ".gitignore").write_text("dist/\n")
        touch(root, "dist/a.whl", "m.py")
        assert hostgit.gitignored_files(root) == {"dist/a.whl"}

    def test_parent_checkout_rules_do_not_leak(self, tmp_path: Path) -> None:
        """A harvest dir under a checkout's .sbxloop/ must not see itself as
        ignored through the enclosing repo's rules."""
        outer = make_repo(tmp_path)
        (outer / ".gitignore").write_text(".sbxloop/\n")
        root = outer / ".sbxloop" / "runs" / "r1" / "artifacts"
        touch(root, "app.py", "dist/a.whl")
        assert hostgit.gitignored_files(root) == frozenset()

    def test_relative_root_resolves_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unmounted artifact roots come from the relative default state_dir;
        GIT_WORK_TREE must not be resolved beneath root itself."""
        root = tmp_path / ".sbxloop" / "runs" / "r1" / "artifacts"
        root.mkdir(parents=True)
        (root / ".gitignore").write_text("dist/\n")
        touch(root, "dist/a.whl", "m.py")
        monkeypatch.chdir(tmp_path)
        rel = Path(".sbxloop") / "runs" / "r1" / "artifacts"
        assert hostgit.gitignored_files(rel) == {"dist/a.whl"}

    def test_checkout_config_cannot_run_an_fsmonitor_hook(self, tmp_path: Path) -> None:
        """The agent can write the clone's .git/config; a core.fsmonitor hook
        there must not execute on the host during the scan."""
        root = make_repo(tmp_path)
        marker = tmp_path / "pwned"
        hook = tmp_path / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        git("config", "core.fsmonitor", str(hook), cwd=root)
        (root / ".gitignore").write_text("dist/\n")
        touch(root, "dist/a.whl", "m.py")
        assert hostgit.gitignored_files(root) == {"dist/a.whl"}
        assert not marker.exists()

    def test_no_git_binary_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostgit, "find_git", lambda: None)
        assert hostgit.gitignored_files(tmp_path) is None


def rev(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A source repo with a committed executable script, and its run clone."""
    source = make_repo(tmp_path)
    (source / "scripts").mkdir()
    script = source / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    (source / "old.txt").write_text("old\n")
    git("add", ".", cwd=source)
    git("commit", "-m", "more", cwd=source)
    clone = tmp_path / "clone"
    hostgit.clone_for_run(source, clone, "sbxloop/r1")
    return source, clone


class TestChangesSince:
    def test_lists_adds_mods_deletes_with_modes(self, tmp_path: Path) -> None:
        """Working-tree edits (uncommitted), a deletion, an untracked file and
        an executable all surface — the shapes a snapshot overlay got wrong."""
        _, clone = make_clone(tmp_path)
        (clone / "hello.txt").write_text("changed\n")
        (clone / "old.txt").unlink()
        (clone / "new.txt").write_text("new\n")
        (clone / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
        (clone / "tool.py").write_text("print()\n")
        (clone / "tool.py").chmod(0o755)

        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))

        assert [(c.path, c.status, c.mode) for c in changes] == [
            ("hello.txt", "modified", "100644"),
            ("new.txt", "added", "100644"),
            ("old.txt", "deleted", ""),
            ("scripts/run.sh", "modified", "100755"),
            ("tool.py", "added", "100755"),
        ]

    def test_committed_changes_and_renames(self, tmp_path: Path) -> None:
        """Committed work counts the same as uncommitted, and a rename comes
        back as delete + add — a git tree has no rename entry."""
        _, clone = make_clone(tmp_path)
        git("mv", "old.txt", "renamed.txt", cwd=clone)
        git("commit", "-m", "rename", cwd=clone)
        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert [(c.path, c.status) for c in changes] == [
            ("old.txt", "deleted"),
            ("renamed.txt", "added"),
        ]

    def test_ignored_files_do_not_count(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        (source / ".gitignore").write_text("*.log\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "ignore", cwd=source)
        clone = tmp_path / "clone"
        hostgit.clone_for_run(source, clone, "sbxloop/r1")
        (clone / "debug.log").write_text("noise\n")
        (clone / "kept.txt").write_text("x\n")
        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert [c.path for c in changes] == ["kept.txt"]

    def test_symlink_mode(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        (clone / "link").symlink_to("hello.txt")
        (change,) = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert (change.path, change.mode) == ("link", "120000")

    def test_unchanged_clone_is_empty(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        assert hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF)) == []

    def test_bad_base_raises_delivery_error(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        with pytest.raises(DeliveryError, match="git diff failed"):
            hostgit.changes_since(clone, "0" * 40)


class TestResolveDiffBase:
    def test_prefers_merge_base_with_known_remote_tip(self, tmp_path: Path) -> None:
        """When the PR target commit is known locally, the diff base is the
        merge base — local commits the source was ahead by travel too."""
        source, clone = make_clone(tmp_path)
        remote_tip = rev(source, "HEAD~1")
        assert hostgit.resolve_diff_base(clone, remote_tip) == remote_tip

    def test_falls_back_to_clone_pin(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        pinned = rev(clone, hostgit.CLONE_BASE_REF)
        # remote tip unknown locally (a sha the clone never saw)
        assert hostgit.resolve_diff_base(clone, "f" * 40) == pinned
        assert hostgit.resolve_diff_base(clone, None) == pinned

    def test_falls_back_to_origin_head_without_pin(self, tmp_path: Path) -> None:
        """Clones made before the pin existed still resolve via origin/HEAD."""
        _, clone = make_clone(tmp_path)
        git("update-ref", "-d", hostgit.CLONE_BASE_REF, cwd=clone)
        assert hostgit.resolve_diff_base(clone, None) == rev(clone, "origin/HEAD")

    def test_no_anchor_is_none(self, tmp_path: Path) -> None:
        """A repo the agent git-init-ed itself has nothing to diff against."""
        root = make_repo(tmp_path)
        assert hostgit.resolve_diff_base(root, None) is None
        assert hostgit.resolve_diff_base(root, "f" * 40) is None

    def test_plain_directory_is_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert hostgit.resolve_diff_base(plain, None) is None


class TestDiffText:
    """The reviewer's view of the run's changes: tracked edits (committed or
    not), deletions, and untracked files it never `git add`-ed."""

    def test_includes_tracked_modifications_and_untracked_files(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        (clone / "hello.txt").write_text("changed\n")
        (clone / "old.txt").unlink()
        (clone / "new.txt").write_text("brand new\n")

        text = hostgit.diff_text(clone, None)

        assert text is not None
        # --stat header opens the text
        assert text.splitlines()[0].strip().startswith("hello.txt")
        assert "-hi\n+changed" in text
        assert "diff --git a/old.txt b/old.txt" in text and "-old" in text
        assert "diff --git a/new.txt b/new.txt" in text
        assert "+brand new" in text
        assert "\x1b[" not in text  # --no-color

    def test_committed_work_counts_the_same(self, tmp_path: Path) -> None:
        _, clone = make_clone(tmp_path)
        (clone / "hello.txt").write_text("committed change\n")
        git("commit", "-am", "edit", cwd=clone)
        text = hostgit.diff_text(clone, None)
        assert text is not None and "+committed change" in text

    def test_ignored_untracked_files_are_left_out(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        (source / ".gitignore").write_text("*.log\n")
        git("add", ".", cwd=source)
        git("commit", "-m", "ignore", cwd=source)
        clone = tmp_path / "clone"
        hostgit.clone_for_run(source, clone, "sbxloop/r1")
        (clone / "debug.log").write_text("noise\n")
        (clone / "kept.txt").write_text("x\n")
        text = hostgit.diff_text(clone, None)
        assert text is not None
        assert "kept.txt" in text and "debug.log" not in text

    def test_prefers_the_remote_base_when_known(self, tmp_path: Path) -> None:
        source, clone = make_clone(tmp_path)
        remote_tip = rev(source, "HEAD~1")
        # Against HEAD~1 the second commit's files (old.txt, the script) are
        # part of the diff; against the clone pin they are not.
        against_tip = hostgit.diff_text(clone, remote_tip)
        against_pin = hostgit.diff_text(clone, None)
        assert against_tip is not None and "old.txt" in against_tip
        assert against_pin == ""

    def test_no_base_is_none(self, tmp_path: Path) -> None:
        """A repo the agent git-init-ed itself has nothing to diff against;
        the reviewer then reads the tree."""
        assert hostgit.diff_text(make_repo(tmp_path), None) is None
        plain = tmp_path / "plain"
        plain.mkdir()
        assert hostgit.diff_text(plain, None) is None

    def test_no_git_binary_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _, clone = make_clone(tmp_path)
        monkeypatch.setattr(hostgit, "find_git", lambda: None)
        assert hostgit.diff_text(clone, None) is None


def make_upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' plus a checkout cloned from it — the daemon's
    dedicated-clone layout — so refresh has a real remote to fetch from."""
    seed = make_repo(tmp_path, "seed")
    upstream = tmp_path / "upstream.git"
    git("clone", "--bare", "-q", str(seed), str(upstream), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    git("clone", "-q", str(upstream), str(checkout), cwd=tmp_path)
    return upstream, checkout


def push_upstream_commit(tmp_path: Path, upstream: Path, name: str = "pusher") -> str:
    """Advance origin/main from a *different* clone, like a teammate merging."""
    other = tmp_path / name
    git("clone", "-q", str(upstream), str(other), cwd=tmp_path)
    (other / f"{name}.txt").write_text("more\n")
    git("add", ".", cwd=other)
    git("commit", "-q", "-m", f"{name} change", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    return hostgit.head_commit(other) or ""


class TestOriginUrl:
    def test_clone_reports_its_origin(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        assert hostgit.origin_url(checkout) == str(upstream)

    def test_repo_without_origin_is_none(self, tmp_path: Path) -> None:
        assert hostgit.origin_url(make_repo(tmp_path)) is None

    def test_not_a_repo_is_none(self, tmp_path: Path) -> None:
        assert hostgit.origin_url(tmp_path / "nope") is None


class TestClonePointsOriginAtSourceOrigin:
    def test_run_clone_origin_is_the_upstream_url_not_the_host_path(self, tmp_path: Path) -> None:
        """#255: `git remote -v` in a run workspace should name the real
        remote, not a host path that is meaningless inside the VM."""
        upstream, checkout = make_upstream_and_clone(tmp_path)
        target = tmp_path / "state" / "runs" / "r1" / "workspace"
        target.parent.mkdir(parents=True)
        hostgit.clone_for_run(checkout, target, "sbxloop/r1")
        assert hostgit.origin_url(target) == str(upstream)
        assert (target / "hello.txt").read_text() == "hi\n"

    def test_source_without_origin_keeps_default_path_origin(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        target = tmp_path / "ws"
        hostgit.clone_for_run(source, target, "sbxloop/r1")
        assert hostgit.origin_url(target) == str(source)


class TestPublicRemoteUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://x-access-token:ghp_secret@github.com/o/r.git", "https://github.com/o/r.git"),
            ("https://token@github.com/o/r", "https://github.com/o/r"),
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("ssh://git@github.com/o/r.git", "ssh://github.com/o/r.git"),
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
            ("/srv/git/upstream.git", "/srv/git/upstream.git"),
        ],
    )
    def test_strips_userinfo_from_scheme_urls_only(self, url: str, expected: str) -> None:
        assert hostgit.public_remote_url(url) == expected

    def test_run_clone_never_carries_a_token_from_the_source_origin(self, tmp_path: Path) -> None:
        """The source checkout's origin holds a PAT in the URL (common for
        bots); the per-run clone the agent can read must not."""
        source = make_repo(tmp_path)
        git("remote", "add", "origin", "https://x:ghp_secret@github.com/o/r.git", cwd=source)
        target = tmp_path / "ws"
        hostgit.clone_for_run(source, target, "sbxloop/r1")
        assert hostgit.origin_url(target) == "https://github.com/o/r.git"
        assert "ghp_secret" not in (target / ".git" / "config").read_text()


class TestRefreshFromOrigin:
    def test_up_to_date_is_a_no_op(self, tmp_path: Path) -> None:
        _upstream, checkout = make_upstream_and_clone(tmp_path)
        before = hostgit.head_commit(checkout)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is False
        assert result.before == result.after == before
        assert "up to date" in result.message

    def test_fast_forwards_to_new_upstream_commit(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        stale = hostgit.head_commit(checkout)
        new_sha = push_upstream_commit(tmp_path, upstream)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is True
        assert (result.before, result.after) == (stale, new_sha)
        assert hostgit.head_commit(checkout) == new_sha
        assert (checkout / "pusher.txt").exists()
        assert "fast-forwarded main" in result.message

    def test_diverged_local_branch_is_left_alone(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        (checkout / "local.txt").write_text("mine\n")
        git("add", ".", cwd=checkout)
        git("commit", "-q", "-m", "local", cwd=checkout)
        local = hostgit.head_commit(checkout)
        push_upstream_commit(tmp_path, upstream)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is False
        assert hostgit.head_commit(checkout) == local
        assert "diverged" in result.message

    def test_colliding_local_edit_blocks_ff_without_damage(self, tmp_path: Path) -> None:
        """A dirty tree whose edits overlap the update: git refuses the
        fast-forward; the edit survives and the run proceeds from old HEAD."""
        upstream, checkout = make_upstream_and_clone(tmp_path)
        other = tmp_path / "editor"
        git("clone", "-q", str(upstream), str(other), cwd=tmp_path)
        (other / "hello.txt").write_text("upstream edit\n")
        git("commit", "-q", "-am", "edit hello", cwd=other)
        git("push", "-q", "origin", "main", cwd=other)
        (checkout / "hello.txt").write_text("local uncommitted edit\n")
        stale = hostgit.head_commit(checkout)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is False
        assert hostgit.head_commit(checkout) == stale
        assert (checkout / "hello.txt").read_text() == "local uncommitted edit\n"
        assert "could not fast-forward" in result.message

    def test_unrelated_dirty_edit_does_not_block_ff(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        (checkout / "scratch.txt").write_text("wip\n")
        new_sha = push_upstream_commit(tmp_path, upstream)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is True
        assert hostgit.head_commit(checkout) == new_sha
        assert (checkout / "scratch.txt").read_text() == "wip\n"

    def test_no_origin_remote_reports_and_leaves_repo(self, tmp_path: Path) -> None:
        source = make_repo(tmp_path)
        result = hostgit.refresh_from_origin(source)
        assert result.advanced is False
        assert "no origin remote" in result.message

    def test_branch_without_upstream_falls_back_to_same_name(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        git("branch", "--unset-upstream", cwd=checkout)
        new_sha = push_upstream_commit(tmp_path, upstream)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is True
        assert hostgit.head_commit(checkout) == new_sha

    def test_branch_with_no_remote_counterpart_is_left(self, tmp_path: Path) -> None:
        _upstream, checkout = make_upstream_and_clone(tmp_path)
        git("checkout", "-q", "-b", "feature", cwd=checkout)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is False
        assert "no origin branch" in result.message

    def test_branch_tracking_another_remote_fetches_that_remote(self, tmp_path: Path) -> None:
        """Fork layout: `origin` is the fork, `main` tracks `upstream/main`.
        Fetching only origin would leave upstream/main stale and report the
        checkout as up to date; the tracked remote must be the one fetched."""
        fork, checkout = make_upstream_and_clone(tmp_path)
        seed = tmp_path / "seed"
        upstream = tmp_path / "real-upstream.git"
        git("clone", "--bare", "-q", str(seed), str(upstream), cwd=tmp_path)
        git("remote", "add", "upstream", str(upstream), cwd=checkout)
        git("fetch", "-q", "upstream", cwd=checkout)
        git("branch", "-u", "upstream/main", "main", cwd=checkout)
        stale = hostgit.head_commit(checkout)
        new_sha = push_upstream_commit(tmp_path, upstream, name="upstream-pusher")
        assert push_upstream_commit(tmp_path, fork, name="fork-pusher") != new_sha
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is True
        assert (result.before, result.after) == (stale, new_sha)
        assert hostgit.head_commit(checkout) == new_sha
        assert "upstream/main" in result.message

    def test_detached_head_fetches_only(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        sha = hostgit.head_commit(checkout) or ""
        git("checkout", "-q", "--detach", sha, cwd=checkout)
        push_upstream_commit(tmp_path, upstream)
        result = hostgit.refresh_from_origin(checkout)
        assert result.advanced is False
        assert hostgit.head_commit(checkout) == sha
        assert "detached HEAD" in result.message

    def test_unreachable_origin_raises_provision_error(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        shutil.rmtree(upstream)
        with pytest.raises(ProvisionError, match="git fetch origin failed"):
            hostgit.refresh_from_origin(checkout)

    def test_not_a_repo_raises_provision_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProvisionError, match="cannot refresh"):
            hostgit.refresh_from_origin(tmp_path / "nope")


class TestCloneExistingBranch:
    """A fix round continues its own pull request, so its clone must start
    from what that branch actually has.

    The refusal is the safety property: a round that silently fell back to
    the default branch would deliver a tree that never contained the PR's
    work, and force-updating the branch with it destroys that work.
    """

    def _with_pr_branch(self, tmp_path: Path) -> tuple[Path, Path, str]:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        other = tmp_path / "author"
        git("clone", "-q", str(upstream), str(other), cwd=tmp_path)
        git("checkout", "-q", "-b", "sbxloop/r1", cwd=other)
        (other / "pr-work.txt").write_text("the PR's work\n")
        git("add", ".", cwd=other)
        git("commit", "-q", "-m", "pr work", cwd=other)
        git("push", "-q", "origin", "sbxloop/r1", cwd=other)
        sha = hostgit.head_commit(other) or ""
        git("fetch", "-q", "origin", cwd=checkout)
        return upstream, checkout, sha

    def test_it_starts_from_the_branchs_own_commit(self, tmp_path: Path) -> None:
        _, checkout, sha = self._with_pr_branch(tmp_path)
        target = tmp_path / "run"
        got = hostgit.clone_existing_branch(checkout, target, "sbxloop/r1")
        assert got == sha
        # The PR's work is present — this is the whole point.
        assert (target / "pr-work.txt").read_text() == "the PR's work\n"

    def test_it_checks_out_that_branch_by_name(self, tmp_path: Path) -> None:
        """Same name in, same name out: the delivery force-updates this
        branch, which is what updates the PR rather than opening a new one."""
        _, checkout, _ = self._with_pr_branch(tmp_path)
        target = tmp_path / "run"
        hostgit.clone_existing_branch(checkout, target, "sbxloop/r1")
        with Repo(target) as clone:
            assert clone.active_branch.name == "sbxloop/r1"

    def test_the_base_ref_is_pinned_for_the_delivery_diff(self, tmp_path: Path) -> None:
        _, checkout, sha = self._with_pr_branch(tmp_path)
        target = tmp_path / "run"
        hostgit.clone_existing_branch(checkout, target, "sbxloop/r1")
        with Repo(target) as clone:
            assert clone.git.rev_parse(hostgit.CLONE_BASE_REF).strip() == sha

    def test_a_missing_branch_refuses_rather_than_falling_back(self, tmp_path: Path) -> None:
        """The failure that matters. Provisioning failing is recoverable;
        delivering a tree that never had the PR's work is not."""
        _, checkout = make_upstream_and_clone(tmp_path)
        with pytest.raises(ProvisionError, match="is not on"):
            hostgit.clone_existing_branch(checkout, tmp_path / "run", "sbxloop/nope")

    def test_a_refusal_leaves_no_half_clone(self, tmp_path: Path) -> None:
        _, checkout = make_upstream_and_clone(tmp_path)
        target = tmp_path / "run"
        with pytest.raises(ProvisionError):
            hostgit.clone_existing_branch(checkout, target, "sbxloop/nope")
        assert not target.exists() or not any(target.iterdir())


def make_run_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A run clone whose ``origin`` is a bare upstream that can move on.

    ``clone_for_run`` re-points the clone's origin at the *source's* origin,
    so cutting the clone from a checkout of the bare repo leaves the clone
    fetching from the bare repo — the shape merge_from_base runs against.
    """
    upstream, checkout = make_upstream_and_clone(tmp_path)
    clone = tmp_path / "run"
    hostgit.clone_for_run(checkout, clone, "sbxloop/r1")
    assert hostgit.origin_url(clone) == str(upstream)
    return upstream, clone


def push_upstream_edit(
    tmp_path: Path, upstream: Path, rel: str, content: str, name: str = "editor"
) -> str:
    """Advance origin/main with an edit to an existing file, from another clone."""
    other = tmp_path / name
    git("clone", "-q", str(upstream), str(other), cwd=tmp_path)
    (other / rel).write_text(content)
    git("commit", "-q", "-am", f"{name} edits {rel}", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    return rev(other)


def commit_all(cwd: Path, message: str) -> str:
    git("add", "-A", cwd=cwd)
    git("commit", "-q", "-m", message, cwd=cwd)
    return rev(cwd)


class TestMergeFromBase:
    """Before a conflict fix round the current base is merged into the run's
    clone, so the conflict is real in the fixer's working tree rather than
    something delivery silently overwrites."""

    def test_clean_merge_brings_the_new_base_commit_in(self, tmp_path: Path) -> None:
        upstream, clone = make_run_clone(tmp_path)
        (clone / "work.txt").write_text("the run's work\n")
        commit_all(clone, "run work")
        new_sha = push_upstream_commit(tmp_path, upstream)

        result = merge_from_base(clone, "main")

        assert result.merged is True
        assert result.conflicts == ()
        assert "origin/main" in result.message
        with Repo(clone) as repo:
            assert repo.is_ancestor(repo.commit(new_sha), repo.head.commit)
            assert repo.active_branch.name == "sbxloop/r1"
            assert not repo.is_dirty(untracked_files=True)
        # Both sides' files are present: the run's work and the base's.
        assert (clone / "work.txt").read_text() == "the run's work\n"
        assert (clone / "pusher.txt").read_text() == "more\n"
        assert not (clone / ".git" / "MERGE_HEAD").exists()

    def test_uncommitted_work_is_checkpointed_first(self, tmp_path: Path) -> None:
        """git refuses to merge over local edits, and the agent may not have
        committed: the tree is committed as-is (host identity) and the merge
        then proceeds cleanly."""
        upstream, clone = make_run_clone(tmp_path)
        (clone / "wip.txt").write_text("not yet committed\n")
        (clone / "hello.txt").write_text("edited but not committed\n")
        new_sha = push_upstream_commit(tmp_path, upstream)

        result = merge_from_base(clone, "main")

        assert result.merged is True
        with Repo(clone) as repo:
            checkpoints = [
                c for c in repo.iter_commits() if c.message.startswith("sbxloop: checkpoint")
            ]
            assert len(checkpoints) == 1
            (checkpoint,) = checkpoints
            assert (checkpoint.author.name, checkpoint.author.email) == (
                "sbxloop",
                "sbxloop@localhost",
            )
            assert (checkpoint.committer.name, checkpoint.committer.email) == (
                "sbxloop",
                "sbxloop@localhost",
            )
            assert set(checkpoint.stats.files) == {"wip.txt", "hello.txt"}
            assert repo.is_ancestor(checkpoint, repo.head.commit)
            assert repo.is_ancestor(repo.commit(new_sha), repo.head.commit)
            assert not repo.is_dirty(untracked_files=True)
        # Nothing of the agent's work was lost in the checkpoint.
        assert (clone / "wip.txt").read_text() == "not yet committed\n"
        assert (clone / "hello.txt").read_text() == "edited but not committed\n"
        assert (clone / "pusher.txt").exists()

    def test_conflict_is_left_in_progress_with_the_paths(self, tmp_path: Path) -> None:
        """Both sides edit the same line: the merge stays in progress, the
        markers are in the file, and the conflicted paths are reported for
        the fixer's brief."""
        upstream, clone = make_run_clone(tmp_path)
        (clone / "hello.txt").write_text("the run's line\n")
        before = commit_all(clone, "run edit")
        push_upstream_edit(tmp_path, upstream, "hello.txt", "upstream's line\n")

        result = merge_from_base(clone, "main")

        assert result.merged is False
        assert result.conflicts == ("hello.txt",)
        assert "1 conflicted file" in result.message and "origin/main" in result.message
        text = (clone / "hello.txt").read_text()
        assert "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text
        assert "the run's line" in text and "upstream's line" in text
        assert (clone / ".git" / "MERGE_HEAD").is_file()
        assert rev(clone) == before, "no merge commit until the fixer resolves it"

    def test_the_fixers_finish_completes_the_merge(self, tmp_path: Path) -> None:
        """The brief tells the fixer `git add -A && git commit --no-edit`;
        after that the base is contained and a second merge is a no-op."""
        upstream, clone = make_run_clone(tmp_path)
        (clone / "hello.txt").write_text("the run's line\n")
        commit_all(clone, "run edit")
        new_sha = push_upstream_edit(tmp_path, upstream, "hello.txt", "upstream's line\n")
        assert merge_from_base(clone, "main").merged is False

        (clone / "hello.txt").write_text("the run's line\nupstream's line\n")
        git("add", "-A", cwd=clone)
        git("commit", "--no-edit", cwd=clone)

        assert not (clone / ".git" / "MERGE_HEAD").exists()
        again = merge_from_base(clone, "main")
        assert again.merged is True and again.conflicts == ()
        assert "already contains origin/main" in again.message
        with Repo(clone) as repo:
            assert repo.is_ancestor(repo.commit(new_sha), repo.head.commit)

    def test_base_already_contained_makes_no_commit(self, tmp_path: Path) -> None:
        _, clone = make_run_clone(tmp_path)
        (clone / "work.txt").write_text("the run's work\n")
        before = commit_all(clone, "run work")

        result = merge_from_base(clone, "main")

        assert result.merged is True
        assert result.conflicts == ()
        assert "already contains origin/main" in result.message
        assert rev(clone) == before
        assert not (clone / ".git" / "MERGE_HEAD").exists()

    def test_no_origin_remote_reports_rather_than_raising(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = rev(repo)
        result = merge_from_base(repo, "main")
        assert result.merged is False
        assert result.conflicts == ()
        assert "no origin remote" in result.message
        assert rev(repo) == before

    def test_fetch_failure_raises_provision_error(self, tmp_path: Path) -> None:
        _, clone = make_run_clone(tmp_path)
        git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=clone)
        before = rev(clone)
        with pytest.raises(GitMergeError, match="git fetch origin main failed"):
            merge_from_base(clone, "main")
        assert rev(clone) == before

    def test_non_content_merge_failure_aborts_and_raises(self, tmp_path: Path) -> None:
        """A merge git refuses outright (here: unrelated histories) has no
        conflicted paths to hand the fixer; the tree is left as it was."""
        _, clone = make_run_clone(tmp_path)
        before = rev(clone)
        # Different content, so the stranger's root commit can never collide
        # with the seed's (same tree + message + identity in the same second
        # would be the same SHA, and the histories would then be related).
        stranger = tmp_path / "stranger"
        stranger.mkdir()
        git("init", "-b", "main", cwd=stranger)
        (stranger / "stranger.txt").write_text("nothing in common\n")
        commit_all(stranger, "a stranger's root")
        stranger_bare = tmp_path / "stranger.git"
        git("clone", "--bare", "-q", str(stranger), str(stranger_bare), cwd=tmp_path)
        git("remote", "set-url", "origin", str(stranger_bare), cwd=clone)

        with pytest.raises(GitMergeError, match="merging origin/main into"):
            merge_from_base(clone, "main")

        assert rev(clone) == before
        assert not (clone / ".git" / "MERGE_HEAD").exists()
        assert not hostgit.is_dirty(clone)

    def test_not_a_repo_raises_provision_error(self, tmp_path: Path) -> None:
        with pytest.raises(GitMergeError, match="cannot merge into"):
            merge_from_base(tmp_path / "nope", "main")


def git_out(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def clone_config(clone: Path, key: str) -> str:
    return git_out("config", "--get", key, cwd=clone)


def remote_branches(clone: Path) -> list[str]:
    """Remote-tracking branches, without the ``origin/HEAD -> …`` pointer."""
    return [
        line.strip() for line in git_out("branch", "-r", cwd=clone).splitlines() if "->" not in line
    ]


class TestCloneSize:
    """Every run clone is single-branch and tag-free (#632): a run works on
    one branch, and nothing downstream reads another branch out of the
    clone — merge_from_base fetches the base explicitly."""

    def _upstream_with_baggage(self, tmp_path: Path) -> tuple[Path, Path]:
        """A bare upstream carrying a second branch and a tag, plus the
        daemon's checkout of it (which has both)."""
        seed = make_repo(tmp_path, "seed")
        git("tag", "v1", cwd=seed)
        git("checkout", "-q", "-b", "other", cwd=seed)
        (seed / "other.txt").write_text("other\n")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "other work", cwd=seed)
        git("checkout", "-q", "main", cwd=seed)
        upstream = tmp_path / "upstream.git"
        git("clone", "--bare", "-q", str(seed), str(upstream), cwd=tmp_path)
        git("config", "uploadpack.allowFilter", "true", cwd=upstream)
        checkout = tmp_path / "checkout"
        git("clone", "-q", str(upstream), str(checkout), cwd=tmp_path)
        assert "v1" in git_out("tag", cwd=checkout)
        assert "origin/other" in git_out("branch", "-r", cwd=checkout)
        return upstream, checkout

    def test_run_clone_of_a_checkout_carries_one_branch_and_no_tags(self, tmp_path: Path) -> None:
        _, checkout = self._upstream_with_baggage(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(checkout, clone, "sbxloop/r1")
        assert git_out("tag", cwd=clone) == ""
        assert remote_branches(clone) == ["origin/main"]
        assert (
            clone_config(clone, "remote.origin.fetch")
            == "+refs/heads/main:refs/remotes/origin/main"
        )
        assert clone_config(clone, "remote.origin.tagopt") == "--no-tags"
        assert (clone / "hello.txt").read_text() == "hi\n"

    def test_remote_clone_carries_one_branch_and_no_tags(self, tmp_path: Path) -> None:
        upstream, _ = self._upstream_with_baggage(tmp_path)
        clone = tmp_path / "run"
        sha = hostgit.clone_from_remote(f"file://{upstream}", clone, "sbxloop/r1")
        assert sha == rev(upstream, "main")
        assert git_out("tag", cwd=clone) == ""
        assert remote_branches(clone) == ["origin/main"]
        assert clone_config(clone, "remote.origin.tagopt") == "--no-tags"
        assert "partialclonefilter" not in git_out("config", "--list", cwd=clone)

    def test_existing_branch_remote_clone_is_cut_on_that_branch(self, tmp_path: Path) -> None:
        """A fix round with no local checkout: --branch is the only way
        origin/<branch> exists in a single-branch clone at all."""
        upstream, _ = self._upstream_with_baggage(tmp_path)
        clone = tmp_path / "run"
        sha = hostgit.clone_from_remote(f"file://{upstream}", clone, "other", existing=True)
        assert sha == rev(upstream, "other")
        assert remote_branches(clone) == ["origin/other"]
        with Repo(clone) as repo:
            assert repo.active_branch.name == "other"
        assert (clone / "other.txt").read_text() == "other\n"

    def test_existing_branch_the_remote_lacks_fails_the_clone(self, tmp_path: Path) -> None:
        upstream, _ = self._upstream_with_baggage(tmp_path)
        target = tmp_path / "run"
        with pytest.raises(ProvisionError, match="cloning"):
            hostgit.clone_from_remote(f"file://{upstream}", target, "sbxloop/nope", existing=True)
        assert not target.exists() or not (target / ".git").is_dir()

    def test_base_that_is_not_the_clone_branch_still_merges_and_anchors(
        self, tmp_path: Path
    ) -> None:
        """The safety argument for --single-branch, end to end: a fix round on
        a PR against `other` merges from a base the clone never fetched at
        clone time, and the delivery diff then anchors on that base."""
        upstream, _ = self._upstream_with_baggage(tmp_path)
        author = tmp_path / "author"
        git("clone", "-q", str(upstream), str(author), cwd=tmp_path)
        git("checkout", "-q", "-b", "sbxloop/r1", "origin/other", cwd=author)
        (author / "pr.txt").write_text("pr\n")
        git("add", ".", cwd=author)
        git("commit", "-q", "-m", "pr", cwd=author)
        git("push", "-q", "origin", "sbxloop/r1", cwd=author)
        # The base moves on after the PR branched.
        git("checkout", "-q", "other", cwd=author)
        (author / "base-moved.txt").write_text("moved\n")
        git("add", ".", cwd=author)
        git("commit", "-q", "-m", "base moves", cwd=author)
        git("push", "-q", "origin", "other", cwd=author)
        new_base = rev(author)

        clone = tmp_path / "run"
        hostgit.clone_from_remote(f"file://{upstream}", clone, "sbxloop/r1", existing=True)
        assert remote_branches(clone) == ["origin/sbxloop/r1"]
        assert hostgit.resolve_diff_base(clone, new_base) != new_base  # not fetched yet

        result = merge_from_base(clone, "other")
        assert result.merged, result.message
        assert "origin/other" in remote_branches(clone)
        assert (clone / "base-moved.txt").read_text() == "moved\n"
        assert hostgit.resolve_diff_base(clone, new_base) == new_base
        # The PR's own work is what the diff against the base shows.
        assert {c.path for c in hostgit.changes_since(clone, new_base)} == {"pr.txt"}

    def test_clone_filter_is_opt_in_and_recorded(self, tmp_path: Path) -> None:
        upstream, _ = self._upstream_with_baggage(tmp_path)
        clone = tmp_path / "run"
        sha = hostgit.clone_from_remote(
            f"file://{upstream}", clone, "sbxloop/r1", clone_filter="blob:none"
        )
        assert sha == rev(upstream, "main")
        assert clone_config(clone, "remote.origin.partialclonefilter") == "blob:none"
        assert clone_config(clone, "remote.origin.promisor") == "true"
        assert clone_config(clone, "remote.origin.tagopt") == "--no-tags"
        assert (clone / "hello.txt").read_text() == "hi\n"

    def test_a_git_without_filter_support_gets_a_full_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An old client refuses the option outright; the run gets an
        unfiltered clone and a log line, never an error."""
        upstream, _ = self._upstream_with_baggage(tmp_path)
        real = Repo.clone_from
        calls: list[list[str]] = []

        def old_git(url: str, path: str, **kwargs: object) -> Repo:
            options = list(kwargs.get("multi_options") or [])  # type: ignore[call-overload]
            calls.append(options)
            if any(o.startswith("--filter=") for o in options):
                raise GitCommandError(
                    ["git", "clone"], 129, stderr="error: unknown option `filter=blob:none'"
                )
            return real(url, path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(hostgit.Repo, "clone_from", staticmethod(old_git))
        clone = tmp_path / "run"
        sha = hostgit.clone_from_remote(
            f"file://{upstream}", clone, "sbxloop/r1", clone_filter="blob:none"
        )
        assert sha == rev(upstream, "main")
        assert len(calls) == 2
        assert "partialclonefilter" not in git_out("config", "--list", cwd=clone)
        assert "--single-branch" in calls[1] and "--no-tags" in calls[1]

    def test_another_clone_failure_with_a_filter_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ProvisionError, match="cloning"):
            hostgit.clone_from_remote(
                f"file://{tmp_path / 'gone.git'}", tmp_path / "run", "r", clone_filter="blob:none"
            )
        assert not (tmp_path / "run").exists()

    def test_existing_branch_found_on_the_sources_local_branches(self, tmp_path: Path) -> None:
        """A single-branch clone of a checkout no longer copies the source's
        other local branches as origin/*; the branch is fetched from the
        source's heads instead."""
        _, checkout = self._upstream_with_baggage(tmp_path)
        git("branch", "sbxloop/r1", "origin/other", cwd=checkout)
        clone = tmp_path / "run"
        sha = hostgit.clone_existing_branch(checkout, clone, "sbxloop/r1")
        assert sha == rev(checkout, "sbxloop/r1")
        assert (clone / "other.txt").read_text() == "other\n"
        with Repo(clone) as repo:
            assert repo.active_branch.name == "sbxloop/r1"


class TestPrivateRemoteClone:
    """#683: the run's token — and only it — authenticates a private remote,
    through a helper that leaves no trace in the clone."""

    TOKEN = "ghs_test-installation-token"

    @pytest.fixture
    def remote(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        seed = make_repo(tmp_path)
        bare_from(seed, tmp_path / "srv", "o/private.git")
        with PrivateGitServer(
            tmp_path / "srv", username="x-access-token", token=self.TOKEN
        ) as server:
            yield server

    def test_clones_with_the_token_and_leaves_no_trace(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        clone = tmp_path / "run"
        url = f"{remote.url}/o/private.git"
        sha = hostgit.clone_from_remote(url, clone, "sbxloop/r1", token=self.TOKEN)
        assert sha == rev(tmp_path / "src", "main")
        assert (clone / "hello.txt").read_text() == "hi\n"
        # The first request is unauthenticated (git only sends a credential
        # once challenged); the retry carried the run's token.
        assert remote.requests[0] is None
        assert any(r is not None for r in remote.requests)
        # git remote -v shows the bare URL; the token is nowhere in the clone.
        assert git_out("remote", "-v", cwd=clone).count(url) == 2
        assert self.TOKEN not in (clone / ".git" / "config").read_text()
        assert "credential" not in git_out("config", "--list", "--local", cwd=clone)

    def test_without_a_token_the_private_remote_refuses(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        target = tmp_path / "run"
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.clone_from_remote(f"{remote.url}/o/private.git", target, "sbxloop/r1")
        assert "cloning" in str(excinfo.value)
        assert not (target / ".git").is_dir()

    def test_a_wrong_token_fails_without_leaking_it(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        target = tmp_path / "run"
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.clone_from_remote(
                f"{remote.url}/o/private.git", target, "sbxloop/r1", token="ghs_wrong"
            )
        message = str(excinfo.value)
        assert "Authentication failed" in message
        assert "ghs_wrong" not in message
        assert not (target / ".git").is_dir()

    def test_the_helper_lives_only_in_the_clone_environment(self) -> None:
        env = hostgit._clone_env("s3cr3t-value")
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env[hostgit.CLONE_TOKEN_ENV] == "s3cr3t-value"
        # The host user's own helpers are cleared before ours is added, so a
        # keychain never answers for the run.
        assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("credential.helper", "")
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert "x-access-token" in env["GIT_CONFIG_VALUE_1"]
        assert "s3cr3t-value" not in env["GIT_CONFIG_VALUE_1"]
        assert hostgit._clone_env(None)["GIT_CONFIG_COUNT"] == "1"


class TestIsTracked:
    def test_tracked_untracked_and_outside(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        assert hostgit.is_tracked(root, root / "hello.txt") is True
        (root / "local.toml").write_text("x = 1\n")
        assert hostgit.is_tracked(root, root / "local.toml") is False
        assert hostgit.is_tracked(root, root / "missing.toml") is False
        assert hostgit.is_tracked(root, tmp_path / "elsewhere.toml") is False

    def test_nested_path_and_ignored_file(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        (root / ".gitignore").write_text("sbxloop.toml\n")
        (root / "sbxloop.toml").write_text("model = 'x'\n")
        git("add", ".gitignore", cwd=root)
        git("commit", "-m", "ignore", cwd=root)
        assert hostgit.is_tracked(root, root / "sbxloop.toml") is False
        (root / "pkg").mkdir()
        (root / "pkg" / "pyproject.toml").write_text("[tool.sbxloop]\n")
        git("add", "pkg/pyproject.toml", cwd=root)
        git("commit", "-m", "pkg", cwd=root)
        assert hostgit.is_tracked(root, root / "pkg" / "pyproject.toml") is True

    def test_without_git_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(hostgit, "find_git", lambda: None)
        assert hostgit.is_tracked(root, root / "hello.txt") is None


def make_submodule_setup(tmp_path: Path, remote_root: str) -> tuple[Path, Path, Path]:
    """A library repo, its bare copy under ``tmp_path/remotes`` (served at
    ``remote_root``), and a superproject checkout that vendors the library
    as the submodule ``vendor/lib`` — populated, at the library's first
    commit. Returns (superproject, lib checkout, lib bare)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    git("init", "-b", "main", cwd=lib)
    (lib / "lib.txt").write_text("v1\n")
    git("add", ".", cwd=lib)
    git("commit", "-m", "lib v1", cwd=lib)
    lib_bare = bare_from(lib, tmp_path / "remotes", "lib.git")
    app = make_repo(tmp_path, "app")
    git("submodule", "add", "-q", f"{remote_root}/lib.git", "vendor/lib", cwd=app)
    git("commit", "-q", "-m", "vendor lib", cwd=app)
    return app, lib, lib_bare


def lib_v2(lib: Path, lib_bare: Path) -> str:
    """A second library commit, pushed to the bare remote; returns its sha."""
    (lib / "lib.txt").write_text("v2\n")
    git("commit", "-q", "-am", "lib v2", cwd=lib)
    git("push", "-q", str(lib_bare), "main", cwd=lib)
    return rev(lib)


@pytest.mark.slow
class TestSubmodules:
    """#692: a run clone's submodules are populated, from the host checkout
    when it can, and a moved gitlink surfaces as a ``160000`` change with
    the commit it points at — never as the deletion of a directory."""

    @pytest.fixture
    def remote(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        (tmp_path / "remotes").mkdir()
        with PrivateGitServer(tmp_path / "remotes", username="x", token="y", public=True) as srv:
            yield srv

    def test_lists_gitmodules_entries(self, tmp_path: Path, remote: PrivateGitServer) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        (sub,) = hostgit.list_submodules(app)
        assert (sub.name, sub.path, sub.url) == (
            "vendor/lib",
            "vendor/lib",
            f"{remote.url}/lib.git",
        )
        assert hostgit.list_submodules(make_repo(tmp_path, "plain")) == []

    def test_fresh_clone_populates_from_the_host_checkout(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert not (clone / "vendor" / "lib" / "lib.txt").exists()  # a bare gitlink
        before = len(remote.requests)
        populated = hostgit.populate_submodules(clone, source=app, token=None)
        assert populated == [("vendor/lib", "local")]
        assert (clone / "vendor" / "lib" / "lib.txt").read_text() == "v1\n"
        assert len(remote.requests) == before  # nothing fetched over the network
        # origin points at the URL .gitmodules names, not at the host path
        sub = clone / "vendor" / "lib"
        assert git_out("remote", "get-url", "origin", cwd=sub) == f"{remote.url}/lib.git"
        assert clone_config(clone, "submodule.vendor/lib.url") == f"{remote.url}/lib.git"
        assert str(app) not in git_out("config", "--list", cwd=sub)

    def test_a_stale_host_copy_falls_back_to_the_remote(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        """The host checkout's submodule lacks the commit the superproject
        records (someone bumped the gitlink without `submodule update`)."""
        app, lib, lib_bare = make_submodule_setup(tmp_path, remote.url)
        v2 = lib_v2(lib, lib_bare)
        git("update-index", "--cacheinfo", f"160000,{v2},vendor/lib", cwd=app)
        git("commit", "-q", "-m", "bump lib without updating", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        populated = hostgit.populate_submodules(clone, source=app, token=None)
        assert populated == [("vendor/lib", "remote")]
        assert (clone / "vendor" / "lib" / "lib.txt").read_text() == "v2\n"
        assert rev(clone / "vendor" / "lib") == v2
        assert remote.requests  # the fallback went to the remote

    def test_without_a_host_checkout_the_remote_is_the_source(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert hostgit.populate_submodules(clone, source=None, token=None) == [
            ("vendor/lib", "remote")
        ]
        assert (clone / "vendor" / "lib" / "lib.txt").read_text() == "v1\n"

    def test_a_private_submodule_takes_the_runs_token(self, tmp_path: Path) -> None:
        token = "ghs_sub-token"
        (tmp_path / "remotes").mkdir()
        with PrivateGitServer(
            tmp_path / "remotes", username="x-access-token", token=token
        ) as private:
            # The superproject is built while the server is public, then
            # its .gitmodules is pointed at the same server made private.
            private.public = True
            app, _, _ = make_submodule_setup(tmp_path, private.url)
            private.public = False
            clone = tmp_path / "run"
            hostgit.clone_for_run(app, clone, "sbxloop/r1")
            with pytest.raises(ProvisionError) as excinfo:
                hostgit.populate_submodules(clone, source=None, token=None)
            assert "vendor/lib" in str(excinfo.value)
            populated = hostgit.populate_submodules(
                clone, source=None, token=token, credential_url=private.url
            )
            assert populated == [("vendor/lib", "remote")]
            assert (clone / "vendor" / "lib" / "lib.txt").read_text() == "v1\n"
            assert token not in (clone / ".git" / "config").read_text()
            assert (
                token not in (clone / ".git" / "modules" / "vendor" / "lib" / "config").read_text()
            )

    def test_a_local_path_submodule_url_is_refused(self, tmp_path: Path) -> None:
        """A `.gitmodules` URL naming a host path must not be followed: the
        clone is read by the sandbox, and such a URL would copy any git
        repository on the host into it."""
        lib = make_repo(tmp_path, "lib")
        app = make_repo(tmp_path, "app")
        git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(lib),
            "vendor/lib",
            cwd=app,
        )
        git("commit", "-q", "-m", "vendor lib", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.populate_submodules(clone, source=None, token=None)
        assert "vendor/lib" in str(excinfo.value)
        assert "clone_submodules = false" in str(excinfo.value)

    def test_nested_submodules_populate_level_by_level(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        """The library itself vendors a submodule: the host checkout's copy
        of it is the source one level down, the remote when that copy is
        absent."""
        core = tmp_path / "core"
        core.mkdir()
        git("init", "-b", "main", cwd=core)
        (core / "core.txt").write_text("core\n")
        git("add", ".", cwd=core)
        git("commit", "-m", "core", cwd=core)
        bare_from(core, tmp_path / "remotes", "core.git")
        app, lib, lib_bare = make_submodule_setup(tmp_path, remote.url)
        git("submodule", "add", "-q", f"{remote.url}/core.git", "deps/core", cwd=lib)
        git("commit", "-q", "-m", "vendor core", cwd=lib)
        git("push", "-q", str(lib_bare), "main", cwd=lib)
        git("-C", "vendor/lib", "pull", "-q", "origin", "main", cwd=app)
        git("-C", "vendor/lib", "submodule", "update", "--init", "-q", cwd=app)
        git("commit", "-q", "-am", "bump lib", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert hostgit.populate_submodules(clone, source=app, token=None) == [
            ("vendor/lib", "local"),
            ("vendor/lib/deps/core", "local"),
        ]
        assert (clone / "vendor" / "lib" / "deps" / "core" / "core.txt").read_text() == "core\n"
        clone2 = tmp_path / "run2"
        hostgit.clone_for_run(app, clone2, "sbxloop/r2")
        assert hostgit.populate_submodules(clone2, source=None, token=None) == [
            ("vendor/lib", "remote"),
            ("vendor/lib/deps/core", "remote"),
        ]
        assert (clone2 / "vendor" / "lib" / "deps" / "core" / "core.txt").read_text() == "core\n"

    def test_no_submodules_is_a_no_op(self, tmp_path: Path) -> None:
        source, clone = make_clone(tmp_path)
        assert hostgit.populate_submodules(clone, source=source, token=None) == []

    def test_a_gitmodules_entry_without_a_gitlink_is_skipped(
        self, tmp_path: Path, remote: PrivateGitServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        # a half-finished ``git rm``: the gitlink is gone, the .gitmodules
        # entry stayed behind — nothing to check out, not a failed run
        app, _lib, _lib_bare = make_submodule_setup(tmp_path, remote.url)
        git("rm", "--cached", "vendor/lib", cwd=app)
        git("commit", "-m", "drop the gitlink, keep the stanza", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        with caplog.at_level(logging.INFO):
            assert hostgit.populate_submodules(clone, source=app, token=None) == []
        assert not (clone / "vendor" / "lib").exists()
        assert any("workspace.submodule_not_in_tree" in r.getMessage() for r in caplog.records)

    def test_a_bumped_gitlink_is_a_160000_change_with_its_commit(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, lib, lib_bare = make_submodule_setup(tmp_path, remote.url)
        v2 = lib_v2(lib, lib_bare)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_submodules(clone, source=app, token=None)
        # the agent fetches and checks the new library commit out, staging
        # nothing in the superproject
        git("fetch", "-q", "origin", cwd=clone / "vendor" / "lib")
        git("checkout", "-q", v2, cwd=clone / "vendor" / "lib")
        notes: list[str] = []
        (change,) = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF), notes=notes)
        assert change == hostgit.WorkspaceChange(
            path="vendor/lib", status="modified", mode="160000", sha=v2
        )
        assert change.is_gitlink
        assert notes == []
        # staged, the diff carries the sha itself: same answer
        git("add", "vendor/lib", cwd=clone)
        (change,) = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert change.sha == v2

    def test_changes_inside_a_submodule_are_skipped_with_a_note(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_submodules(clone, source=app, token=None)
        (clone / "vendor" / "lib" / "lib.txt").write_text("edited in place\n")
        (clone / "README").write_text("real work\n")
        notes: list[str] = []
        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF), notes=notes)
        assert [c.path for c in changes] == ["README"]
        assert notes == ["changes inside submodule `vendor/lib` are not delivered"]

    def test_a_commit_the_remote_lacks_is_not_delivered(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_submodules(clone, source=app, token=None)
        sub = clone / "vendor" / "lib"
        (sub / "lib.txt").write_text("local\n")
        git("commit", "-q", "-am", "local only", cwd=sub)
        local = rev(sub)
        notes: list[str] = []
        assert hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF), notes=notes) == []
        (note,) = notes
        assert local[:12] in note and "vendor/lib" in note and "remote does not have" in note

    def test_a_removed_submodule_is_a_gitlink_deletion(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_submodules(clone, source=app, token=None)
        git("rm", "-q", "vendor/lib", cwd=clone)  # also drops its .gitmodules entry
        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert [(c.path, c.status, c.mode) for c in changes] == [
            (".gitmodules", "modified", "100644"),
            ("vendor/lib", "deleted", "160000"),
        ]

    def test_an_untouched_submodule_is_not_a_change(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, _, _ = make_submodule_setup(tmp_path, remote.url)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_submodules(clone, source=app, token=None)
        (clone / "README").write_text("real work\n")
        changes = hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF))
        assert [c.path for c in changes] == ["README"]


class TestSubmoduleHosts:
    def test_hosts_come_from_gitmodules_urls(self, tmp_path: Path) -> None:
        app = make_repo(tmp_path, "app")
        (app / ".gitmodules").write_text(
            '[submodule "a"]\n\tpath = a\n\turl = https://gitlab.example.com/o/a.git\n'
            '[submodule "b"]\n\tpath = vendor/b\n\turl = git@github.com:o/b.git\n'
            '[submodule "c"]\n\tpath = c\n\turl = ssh://git@git.corp.example:2222/o/c\n'
            '[submodule "d"]\n\tpath = d\n\turl = ../d.git\n'
            '[submodule "e"]\n\tpath = e\n\turl = /srv/git/e.git\n'
            '[submodule "f"]\n\tpath = f\n\turl = https://github.com/o/f\n'
        )
        git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
        assert hostgit.submodule_hosts(app) == [
            "gitlab.example.com",
            "github.com",
            "git.corp.example",
        ]

    def test_relative_urls_need_an_origin(self, tmp_path: Path) -> None:
        app = make_repo(tmp_path, "app")
        (app / ".gitmodules").write_text('[submodule "d"]\n\tpath = d\n\turl = ../d.git\n')
        assert hostgit.submodule_hosts(app) == []

    def test_no_gitmodules_is_empty(self, tmp_path: Path) -> None:
        assert hostgit.submodule_hosts(make_repo(tmp_path)) == []
        assert hostgit.submodule_hosts(tmp_path / "missing") == []

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://github.com/o/r.git", "github.com"),
            ("https://user:tok@ghe.example.com:8443/o/r", "ghe.example.com"),
            ("ssh://git@[::1]:22/o/r", "::1"),
            ("git@github.com:o/r.git", "github.com"),
            ("/srv/git/r.git", None),
            ("../r.git", None),
            ("C:/git/r.git", None),
        ],
    )
    def test_url_host(self, url: str, host: str | None) -> None:
        assert hostgit.url_host(url) == host


# -- Git LFS (#693) ----------------------------------------------------------


needs_git_lfs = pytest.mark.skipif(
    hostgit.lfs_version() is None, reason="git-lfs is not installed on this host"
)


def make_lfs_repo(tmp_path: Path, name: str = "app") -> tuple[Path, bytes]:
    """A checkout whose ``*.bin`` files live in Git LFS, one committed
    asset in its store. Returns (checkout, the asset's real bytes)."""
    root = tmp_path / name
    root.mkdir()
    git("init", "-b", "main", cwd=root)
    git("lfs", "install", "--local", cwd=root)
    (root / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    payload = bytes(range(256)) * 4
    (root / "asset.bin").write_bytes(payload)
    (root / "README").write_text("hi\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root, payload


def is_pointer(path: Path) -> bool:
    return path.read_bytes().startswith(b"version https://git-lfs.github.com/spec/v1")


@needs_git_lfs
@pytest.mark.slow
class TestLfs:
    """#693: a run clone is cut with pointer files and populated from the
    host checkout's store first, the repository's LFS endpoint second —
    with the run's token, never the host's global git-lfs setup."""

    @pytest.fixture
    def remote(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        (tmp_path / "remotes").mkdir()
        with PrivateGitServer(
            tmp_path / "remotes", username="x-access-token", token="ghs_lfs"
        ) as srv:
            yield srv

    def test_endpoint_is_the_dot_git_info_lfs_of_the_clone_url(self) -> None:
        assert (
            hostgit.lfs_endpoint("https://github.com/o/r") == "https://github.com/o/r.git/info/lfs"
        )
        assert (
            hostgit.lfs_endpoint("https://github.com/o/r.git/")
            == "https://github.com/o/r.git/info/lfs"
        )

    def test_a_run_clone_starts_on_pointer_files(self, tmp_path: Path) -> None:
        app, _ = make_lfs_repo(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert is_pointer(clone / "asset.bin")

    def test_populates_from_the_host_checkout_without_the_network(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, payload = make_lfs_repo(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        population = hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        assert population == hostgit.LfsPopulation(files=1, linked=1, fetched=0)
        assert (clone / "asset.bin").read_bytes() == payload
        assert remote.lfs_requests == []
        # the clone's own config carries the filters; the source is untouched
        assert 'filter "lfs"' in (clone / ".git" / "config").read_text()
        assert not hostgit.is_dirty(app)

    def test_fetches_what_the_host_store_lacks_with_the_runs_token(
        self, tmp_path: Path, remote: PrivateGitServer
    ) -> None:
        app, payload = make_lfs_repo(tmp_path)
        bare_from(app, tmp_path / "remotes", "o/app.git")
        remote.seed_lfs(app)
        url = f"{remote.url}/o/app.git"
        clone = tmp_path / "run"
        hostgit.clone_from_remote(url, clone, "sbxloop/r1", token="ghs_lfs")
        assert is_pointer(clone / "asset.bin")
        population = hostgit.populate_lfs(
            clone, source=None, lfs_url=hostgit.lfs_endpoint(url), token="ghs_lfs"
        )
        assert population == hostgit.LfsPopulation(files=1, linked=0, fetched=1)
        assert (clone / "asset.bin").read_bytes() == payload
        assert [p.rsplit("/", 1)[-1] for p in remote.lfs_requests][:1] == ["batch"]
        assert "ghs_lfs" not in (clone / ".git" / "config").read_text()

    def test_a_wrong_token_fails_closed(self, tmp_path: Path, remote: PrivateGitServer) -> None:
        app, _ = make_lfs_repo(tmp_path)
        bare_from(app, tmp_path / "remotes", "o/app.git")
        remote.seed_lfs(app)
        url = f"{remote.url}/o/app.git"
        clone = tmp_path / "run"
        hostgit.clone_from_remote(url, clone, "sbxloop/r1", token="ghs_lfs")
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.populate_lfs(
                clone, source=None, lfs_url=hostgit.lfs_endpoint(url), token="wrong"
            )
        assert "1 Git LFS object(s)" in str(excinfo.value)
        assert "clone_lfs = false" in str(excinfo.value)

    def test_missing_objects_with_no_endpoint_fail_closed(self, tmp_path: Path) -> None:
        app, _ = make_lfs_repo(tmp_path)
        shutil.rmtree(app / ".git" / "lfs")  # a host checkout that never pulled
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        assert "asset.bin" in str(excinfo.value)
        assert "no GitHub repository" in str(excinfo.value)

    def test_without_git_lfs_on_the_host_it_names_the_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _ = make_lfs_repo(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        monkeypatch.setattr(hostgit, "lfs_version", lambda: None)
        with pytest.raises(ProvisionError) as excinfo:
            hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        assert "apt install git-lfs" in str(excinfo.value)
        assert "clone_lfs = false" in str(excinfo.value)

    def test_a_populated_asset_is_not_a_change(self, tmp_path: Path) -> None:
        app, _ = make_lfs_repo(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        assert hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF)) == []
        # a build that touches the file's mtime re-runs the clean filter,
        # which hashes the bytes back to the committed pointer
        os.utime(clone / "asset.bin")
        assert hostgit.changes_since(clone, rev(clone, hostgit.CLONE_BASE_REF)) == []

    def test_lfs_tracked_names_the_paths_gitattributes_routes(self, tmp_path: Path) -> None:
        app, _ = make_lfs_repo(tmp_path)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        hostgit.populate_lfs(clone, source=app, lfs_url=None, token=None)
        (clone / "new.bin").write_bytes(b"\x00" * 16)
        (clone / "README").write_text("more\n")
        assert hostgit.lfs_tracked(clone, ["new.bin", "README", "asset.bin"]) == [
            "new.bin",
            "asset.bin",
        ]
        assert hostgit.lfs_tracked(clone, []) == []


# -- tags (#694) ------------------------------------------------------------


def describe(cwd: Path) -> str:
    return git_out("describe", "--tags", "--always", cwd=cwd)


class TestFetchTags:
    """#694: a ``--no-tags`` run clone can be given the repository's tags —
    from the host checkout when it has them, from origin under the run's
    credential otherwise — so a build that reads ``git describe`` sees the
    version the repository actually has."""

    def test_a_run_clone_starts_without_tags(self, tmp_path: Path) -> None:
        app = make_repo(tmp_path, "app")
        git("tag", "-a", "v1.2.3", "-m", "release", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert hostgit.tag_count(clone) == 0
        assert hostgit.tag_count(app) == 1

    def test_tags_come_from_the_host_checkout_without_the_network(self, tmp_path: Path) -> None:
        (tmp_path / "remotes").mkdir()
        with PrivateGitServer(tmp_path / "remotes", username="x", token="y") as private:
            app = make_repo(tmp_path, "app")
            git("tag", "-a", "v1.2.3", "-m", "release", cwd=app)
            git("remote", "add", "origin", f"{private.url}/o/app.git", cwd=app)
            clone = tmp_path / "run"
            hostgit.clone_for_run(app, clone, "sbxloop/r1")
            fetched = hostgit.fetch_tags(clone, source=app, token=None)
            assert fetched == hostgit.TagFetch(tags=1, source="local")
            assert describe(clone) == "v1.2.3"
            assert private.requests == []

    def test_a_host_checkout_without_tags_falls_back_to_origin(self, tmp_path: Path) -> None:
        upstream = make_repo(tmp_path, "upstream")
        git("tag", "-a", "v2.0.0", "-m", "release", cwd=upstream)
        (tmp_path / "remotes").mkdir()
        bare_from(upstream, tmp_path / "remotes", "o/app.git")
        with PrivateGitServer(
            tmp_path / "remotes", username="x-access-token", token="ghs_tags"
        ) as private:
            url = f"{private.url}/o/app.git"
            # the host checkout is itself a --no-tags clone
            host = tmp_path / "host"
            hostgit.clone_from_remote(url, host, "main", existing=True, token="ghs_tags")
            assert hostgit.tag_count(host) == 0
            clone = tmp_path / "run"
            hostgit.clone_for_run(host, clone, "sbxloop/r1")
            fetched = hostgit.fetch_tags(
                clone, source=host, token="ghs_tags", credential_url=private.url
            )
            assert fetched == hostgit.TagFetch(tags=1, source="remote")
            assert describe(clone) == "v2.0.0"
            assert private.requests
            assert "ghs_tags" not in (clone / ".git" / "config").read_text()

    def test_a_remote_clone_fetches_under_the_runs_token(self, tmp_path: Path) -> None:
        upstream = make_repo(tmp_path, "upstream")
        git("tag", "v0.9", cwd=upstream)  # lightweight tags count too
        (tmp_path / "remotes").mkdir()
        bare_from(upstream, tmp_path / "remotes", "o/app.git")
        with PrivateGitServer(
            tmp_path / "remotes", username="x-access-token", token="ghs_tags"
        ) as private:
            url = f"{private.url}/o/app.git"
            clone = tmp_path / "run"
            hostgit.clone_from_remote(url, clone, "sbxloop/r1", token="ghs_tags")
            with pytest.raises(ProvisionError) as excinfo:
                hostgit.fetch_tags(clone, source=None, token="wrong", credential_url=private.url)
            assert 'fetch_tags = "never"' in str(excinfo.value)
            assert hostgit.tag_count(clone) == 0
            fetched = hostgit.fetch_tags(
                clone, source=None, token="ghs_tags", credential_url=private.url
            )
            assert fetched == hostgit.TagFetch(tags=1, source="remote")
            assert describe(clone) == "v0.9"

    def test_a_repository_without_tags_is_not_an_error(self, tmp_path: Path) -> None:
        app = make_repo(tmp_path, "app")
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert hostgit.fetch_tags(clone, source=app, token=None) == hostgit.TagFetch(0, "remote")

    def test_a_tag_off_the_branch_brings_its_commit(self, tmp_path: Path) -> None:
        app = make_repo(tmp_path, "app")
        git("checkout", "-q", "-b", "release", cwd=app)
        (app / "rel.txt").write_text("r\n")
        git("add", ".", cwd=app)
        git("commit", "-q", "-m", "release only", cwd=app)
        git("tag", "v3.0.0", cwd=app)
        git("checkout", "-q", "main", cwd=app)
        clone = tmp_path / "run"
        hostgit.clone_for_run(app, clone, "sbxloop/r1")
        assert hostgit.fetch_tags(clone, source=app, token=None) == hostgit.TagFetch(1, "local")
        assert git_out("cat-file", "-t", "v3.0.0^{commit}", cwd=clone) == "commit"
