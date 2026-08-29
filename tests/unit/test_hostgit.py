"""hostgit tests against real git repos built in tmp_path.

The helpers wrap GitPython (which drives the system git binary); tests
arrange fixtures with plain subprocess git so the arrangement is
independent of the code under test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from git import Repo

from sbxloop import hostgit
from sbxloop.errors import DeliveryError, ProvisionError


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

        result = hostgit.merge_from_base(clone, "main")

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

        result = hostgit.merge_from_base(clone, "main")

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

        result = hostgit.merge_from_base(clone, "main")

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
        assert hostgit.merge_from_base(clone, "main").merged is False

        (clone / "hello.txt").write_text("the run's line\nupstream's line\n")
        git("add", "-A", cwd=clone)
        git("commit", "--no-edit", cwd=clone)

        assert not (clone / ".git" / "MERGE_HEAD").exists()
        again = hostgit.merge_from_base(clone, "main")
        assert again.merged is True and again.conflicts == ()
        assert "already contains origin/main" in again.message
        with Repo(clone) as repo:
            assert repo.is_ancestor(repo.commit(new_sha), repo.head.commit)

    def test_base_already_contained_makes_no_commit(self, tmp_path: Path) -> None:
        _, clone = make_run_clone(tmp_path)
        (clone / "work.txt").write_text("the run's work\n")
        before = commit_all(clone, "run work")

        result = hostgit.merge_from_base(clone, "main")

        assert result.merged is True
        assert result.conflicts == ()
        assert "already contains origin/main" in result.message
        assert rev(clone) == before
        assert not (clone / ".git" / "MERGE_HEAD").exists()

    def test_no_origin_remote_reports_rather_than_raising(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = rev(repo)
        result = hostgit.merge_from_base(repo, "main")
        assert result.merged is False
        assert result.conflicts == ()
        assert "no origin remote" in result.message
        assert rev(repo) == before

    def test_fetch_failure_raises_provision_error(self, tmp_path: Path) -> None:
        _, clone = make_run_clone(tmp_path)
        git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=clone)
        before = rev(clone)
        with pytest.raises(ProvisionError, match="git fetch origin main failed"):
            hostgit.merge_from_base(clone, "main")
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

        with pytest.raises(ProvisionError, match="merging origin/main into"):
            hostgit.merge_from_base(clone, "main")

        assert rev(clone) == before
        assert not (clone / ".git" / "MERGE_HEAD").exists()
        assert not hostgit.is_dirty(clone)

    def test_not_a_repo_raises_provision_error(self, tmp_path: Path) -> None:
        with pytest.raises(ProvisionError, match="cannot merge into"):
            hostgit.merge_from_base(tmp_path / "nope", "main")
