"""The GitLab backend's content role (#1020): GitHub's blob, tree, commit
and ref steps staged in the backend and written as one commits-API
changeset; a branch rewritten under force, never deleted."""

from __future__ import annotations

import base64

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitlab.content import blob_sha, commit_record, plan_actions, tree_handle
from sbxloop.vcs.gitlab.ops import GitlabOps
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"
COMMITS = "/projects/acme%2Fwidgets/repository/commits"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def deliver(fake: FakeGitlab, branch: str, files: dict[str, bytes], message: str) -> str:
    """The sequence ``deliver.py`` runs, on the fake: the commit's sha."""
    base = fake.ref_lookup(REPO, "heads/main")
    assert base
    base_tree = str(fake.commit_get(REPO, base)["tree"]["sha"])
    shas = fake.blobs_create_many(
        REPO, [{"path": path, "content_b64": b64(raw)} for path, raw in files.items()]
    )
    entries = [
        {"path": path, "mode": "100644", "type": "blob", "sha": shas[path]} for path in files
    ]
    tree = fake.tree_create(REPO, base_tree=base_tree, entries=entries)
    commit = fake.commit_create(REPO, message=message, tree=str(tree["sha"]), parents=[base])
    if fake.ref_lookup(REPO, f"heads/{branch}") is None:
        fake.ref_create(REPO, f"refs/heads/{branch}", str(commit["sha"]))
    else:
        fake.ref_force_update(REPO, branch, str(commit["sha"]))
    return str(commit["sha"])


class TestPureParts:
    def test_a_blob_sha_is_gits_own(self) -> None:
        assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

    def test_a_tree_handle_names_the_same_tree_twice(self) -> None:
        actions = [{"action": "create", "file_path": "a", "content": "YQ==", "encoding": "base64"}]
        assert tree_handle("base123", actions) == tree_handle("base123", list(actions))
        assert tree_handle("base123", actions) != tree_handle("other", actions)
        assert tree_handle("base123", actions).startswith("tree:")

    def test_the_commit_record_addresses_the_tree_by_the_commit(self) -> None:
        record = commit_record({"id": "abc", "parent_ids": ["p1"], "message": "m", "web_url": "u"})
        assert record == {
            "sha": "abc",
            "tree": {"sha": "abc"},
            "parents": [{"sha": "p1"}],
            "message": "m",
            "html_url": "u",
        }
        with pytest.raises(GithubOpsError, match="without an id"):
            commit_record({"message": "m"})

    def test_actions_say_create_update_delete_and_chmod(self) -> None:
        base = {
            "": {
                "README.md": ("blob", "100644"),
                "run.sh": ("blob", "100755"),
                "src": ("tree", "040000"),
            },
            "src": {"old.py": ("blob", "100644")},
        }
        blobs = {"s1": b"one", "s2": b"two", "s3": b"three"}
        entries = [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "s1"},
            {"path": "run.sh", "mode": "100644", "type": "blob", "sha": "s2"},
            {"path": "src/new.py", "mode": "100755", "type": "blob", "sha": "s3"},
            {"path": "src/old.py", "mode": "100644", "type": "blob", "sha": None},
            {"path": "src/gone.py", "mode": "100644", "type": "blob", "sha": None},
        ]
        actions, skipped = plan_actions(entries, lambda d: base.get(d, {}), blobs)
        assert [a["action"] for a in actions] == ["update", "update", "chmod", "create", "delete"]
        assert actions[0]["file_path"] == "README.md" and actions[0]["encoding"] == "base64"
        assert base64.b64decode(actions[0]["content"]) == b"one"
        assert actions[2] == {"action": "chmod", "file_path": "run.sh", "execute_filemode": False}
        assert actions[3]["execute_filemode"] is True
        assert actions[4] == {"action": "delete", "file_path": "src/old.py"}
        assert skipped == ["src/gone.py"]

    def test_a_submodule_a_symlink_and_an_unstaged_blob_are_refused_by_name(self) -> None:
        listing = {"": {}}
        with pytest.raises(GithubOpsError, match="submodule pointer at 'lib'"):
            plan_actions(
                [{"path": "lib", "mode": "160000", "type": "commit", "sha": "x"}],
                lambda d: listing.get(d, {}),
                {},
            )
        with pytest.raises(GithubOpsError, match="no symlink"):
            plan_actions(
                [{"path": "link", "mode": "120000", "type": "blob", "sha": "x"}],
                lambda d: listing.get(d, {}),
                {"x": b"target"},
            )
        with pytest.raises(GithubOpsError, match="not staged"):
            plan_actions(
                [{"path": "a", "mode": "100644", "type": "blob", "sha": "nope"}],
                lambda d: listing.get(d, {}),
                {},
            )
        with pytest.raises(GithubOpsError, match="no path"):
            plan_actions([{"mode": "100644"}], lambda d: {}, {})


class TestStaging:
    def test_blobs_never_reach_gitlab(self) -> None:
        fake = FakeGitlab()
        shas = fake.blobs_create_many(REPO, [{"path": "hello.txt", "content_b64": b64(b"hello\n")}])
        assert shas == {"hello.txt": "ce013625030ba8dba906f756967f9e9ca394464a"}
        assert fake.raw_calls == []
        with pytest.raises(GithubOpsError, match="not base64"):
            fake.blobs_create_many(REPO, [{"path": "x", "content_b64": "not*base64"}])

    def test_the_base_commit_and_its_tree(self) -> None:
        fake = FakeGitlab()
        record = fake.commit_get(REPO, "base123")
        assert record["sha"] == "base123" and record["tree"] == {"sha": "base123"}
        assert record["parents"] == []
        with pytest.raises(GithubOpsError) as info:
            fake.commit_get(REPO, "nope")
        assert info.value.http_status == 404

    def test_a_tree_is_staged_against_the_base_listing(self) -> None:
        fake = FakeGitlab()
        fake.trees["base123"]["src/old.py"] = ("100644", b"old")
        shas = fake.blobs_create_many(
            REPO,
            [
                {"path": "README.md", "content_b64": b64(b"# new\n")},
                {"path": "src/new.py", "content_b64": b64(b"new")},
            ],
        )
        tree = fake.tree_create(
            REPO,
            base_tree="base123",
            entries=[
                {"path": "README.md", "mode": "100644", "type": "blob", "sha": shas["README.md"]},
                {"path": "src/new.py", "mode": "100755", "type": "blob", "sha": shas["src/new.py"]},
                {"path": "src/old.py", "mode": "100644", "type": "blob", "sha": None},
                {"path": "docs/x.md", "mode": "100644", "type": "blob", "sha": None},
            ],
        )
        staged = fake._trees[(REPO, str(tree["sha"]))]
        assert staged.base == "base123"
        assert [a["action"] for a in staged.actions] == ["update", "create", "delete"]
        listings = [c for c in fake.raw_calls if "/repository/tree" in c[1]]
        assert len(listings) == 3, "one listing per directory: the root, src and docs"
        with pytest.raises(GithubOpsError, match="needs the base commit"):
            fake.tree_create(REPO, base_tree="", entries=[])

    def test_a_commit_goes_on_a_pending_branch_from_its_parent(self) -> None:
        fake = FakeGitlab()
        shas = fake.blobs_create_many(REPO, [{"path": "hello.txt", "content_b64": b64(b"hi\n")}])
        tree = fake.tree_create(
            REPO,
            base_tree="base123",
            entries=[
                {"path": "hello.txt", "mode": "100644", "type": "blob", "sha": shas["hello.txt"]}
            ],
        )
        commit = fake.commit_create(
            REPO, message="deliver", tree=str(tree["sha"]), parents=["base123"]
        )
        assert commit["sha"] == "gl000001" and commit["tree"] == {"sha": "gl000001"}
        assert commit["parents"] == [{"sha": "base123"}]
        ((branch, posted),) = fake.commit_posts
        assert branch.startswith("sbxloop/pending/") and posted["start_sha"] == "base123"
        assert posted["commit_message"] == "deliver" and "force" not in posted
        assert fake.trees["gl000001"]["hello.txt"] == ("100644", b"hi\n")
        assert fake.commit_get(REPO, "gl000001")["parents"] == [{"sha": "base123"}]
        with pytest.raises(GithubOpsError, match="not staged"):
            fake.commit_create(REPO, message="m", tree="tree:nope", parents=["base123"])
        with pytest.raises(GithubOpsError, match="one parent"):
            fake.commit_create(REPO, message="m", tree=str(tree["sha"]), parents=["a", "b"])

    def test_an_empty_tree_is_an_empty_commit(self) -> None:
        fake = FakeGitlab()
        tree = fake.tree_create(REPO, base_tree="base123", entries=[])
        fake.commit_create(REPO, message="nothing", tree=str(tree["sha"]), parents=["base123"])
        assert fake.commit_posts[0][1]["allow_empty"] is True


class TestRefs:
    def test_the_first_delivery_creates_the_branch_and_drops_the_pending_one(self) -> None:
        fake = FakeGitlab()
        sha = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n"}, "deliver")
        assert fake.ref_lookup(REPO, "heads/sbxloop/r7") == sha
        assert fake.branch_creates == [("sbxloop/r7", sha)]
        (pending,) = [b for b in fake.deleted_branches if b.startswith("sbxloop/pending/")]
        assert pending not in fake.branches
        with pytest.raises(GithubOpsError, match="refs/heads/<branch>"):
            fake.ref_create(REPO, "heads/x", sha)

    def test_a_collision_is_gitlabs_400_for_the_caller(self) -> None:
        fake = FakeGitlab()
        fake.branches["sbxloop/r7"] = "base123"
        with pytest.raises(GithubOpsError) as info:
            fake.ref_create(REPO, "refs/heads/sbxloop/r7", "base123")
        assert info.value.http_status == 400 and "already exists" in str(info.value)

    def test_a_fix_round_rewrites_the_branch_under_force(self) -> None:
        fake = FakeGitlab()
        first = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n"}, "deliver")
        fake.seed_mr(1, source_branch="sbxloop/r7", head_sha=first)
        second = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi again\n"}, "deliver again")
        head = fake.ref_lookup(REPO, "heads/sbxloop/r7")
        assert head and head != first and head != second, "a new commit of the same tree"
        assert fake.trees[head] == fake.trees[second]
        assert fake.commits[head]["parent_ids"] == ["base123"]
        forced = [p for b, p in fake.commit_posts if b == "sbxloop/r7"]
        assert forced == [
            {
                "branch": "sbxloop/r7",
                "commit_message": "deliver again",
                "actions": list(fake._pending[(REPO, second)].actions),
                "start_sha": "base123",
                "force": True,
            }
        ]
        assert "sbxloop/r7" not in fake.deleted_branches, "deleting the branch closes the request"
        assert fake.pr_get(REPO, 1)["state"] == "open"
        assert not [b for b in fake.branches if b.startswith("sbxloop/pending/")]

    def test_a_branch_already_at_the_commit_is_left_alone(self) -> None:
        fake = FakeGitlab()
        sha = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n"}, "deliver")
        before = len(fake.commit_posts)
        fake.ref_force_update(REPO, "sbxloop/r7", sha)
        assert len(fake.commit_posts) == before

    def test_a_missing_branch_is_created_and_a_foreign_commit_is_refused(self) -> None:
        fake = FakeGitlab()
        fake.ref_force_update(REPO, "sbxloop/r8", "base123")
        assert fake.branches["sbxloop/r8"] == "base123"
        fake.branches["sbxloop/r9"] = "base123"
        with pytest.raises(GithubOpsError, match="no call that moves branch 'sbxloop/r9'"):
            fake.ref_force_update(REPO, "sbxloop/r9", "someone-elses-commit")


class TestContentsPut:
    def test_create_then_replace_on_an_existing_branch(self) -> None:
        fake = FakeGitlab()
        fake.branches["sbxloop/r1"] = "base123"
        written = fake.contents_put(
            REPO, "notes.md", message="add", content_b64=b64(b"# n\n"), branch="sbxloop/r1"
        )
        assert written["commit"]["sha"] == "gl000001"
        assert written["content"] == {"path": "notes.md", "sha": blob_sha(b"# n\n")}
        assert fake.commit_posts[0][1]["actions"][0]["action"] == "create"
        replaced = fake.contents_put(
            REPO, "notes.md", message="again", content_b64=b64(b"# m\n"), branch="sbxloop/r1"
        )
        assert replaced["commit"]["sha"] == "gl000002"
        assert fake.commit_posts[1][1]["actions"][0]["action"] == "update"
        assert fake.trees["gl000002"]["notes.md"] == ("100644", b"# m\n")

    def test_a_new_branch_is_cut_from_the_default_and_an_empty_project_gets_its_first(
        self,
    ) -> None:
        fake = FakeGitlab()
        fake.contents_put(REPO, "a.md", message="m", content_b64=b64(b"a"), branch="feature")
        assert fake.commit_posts[0][1]["start_branch"] == "main"
        empty = FakeGitlab()
        empty.empty_repo = True
        empty.branches.clear()
        empty.contents_put(
            REPO, "README.md", message="init", content_b64=b64(b"# r"), branch="main"
        )
        assert "start_branch" not in empty.commit_posts[0][1]
        assert empty.branches["main"] == "gl000001" and empty.empty_repo is False

    def test_a_protected_base_refuses_the_developer(self) -> None:
        fake = FakeGitlab()
        fake.protected = {"name": "main", "push_access_levels": [{"access_level": 0}]}
        with pytest.raises(GithubOpsError) as info:
            fake.contents_put(REPO, "x", message="m", content_b64=b64(b"x"), branch="main")
        assert info.value.http_status == 403


class TestTheRole:
    def test_nothing_is_left_unimplemented(self) -> None:
        assert GitlabOps.UNIMPLEMENTED_OPERATIONS == ()
        assert FakeGitlab().capabilities()["remote_commit"].name == "SUPPORTED"
