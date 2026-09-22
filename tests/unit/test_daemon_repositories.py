"""The repository registry: the file's entries imported once into the
daemon's database, registrations folded over the file's settings, and the
doctor reading the same registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.cli.doctor import apply_registry, stored_repositories
from sbxloop.config import Config, RepoConfig
from sbxloop.daemon.repositories import RepositoryRegistry, merge
from sbxloop.daemon.store import DaemonStore, StoredRepository

T0 = 1_000_000.0


def _config(tmp_path: Path, *repos: dict[str, Any]) -> Config:
    return Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repos": list(repos)}}
    )


def _row(repo: str, **fields: Any) -> StoredRepository:
    values: dict[str, Any] = {
        "kind": None,
        "enabled": True,
        "deliver_base": None,
        "source": "api",
        "created_by": None,
        "created_at": T0,
        "updated_at": T0,
        "removed_at": None,
    }
    values.update(fields)
    return StoredRepository(repo=repo, **values)


class TestMerge:
    def test_a_registration_folds_over_the_files_entry_of_that_name(self) -> None:
        declared = [RepoConfig(repo="o/one", trigger_label="go", labels=["x"], deliver_base="dev")]
        rows = [_row("O/One", enabled=False, deliver_base="release", kind="gitlab")]
        (entry,) = merge(declared, rows)
        # The file's settings stay; the registration decides the rest.
        assert entry.trigger_label == "go" and entry.labels == ["x"]
        assert entry.enabled is False and entry.deliver_base == "release"
        assert entry.kind == "gitlab"
        assert entry.repo == "o/one"

    def test_a_registration_without_a_file_entry_is_a_bare_entry(self) -> None:
        (entry,) = merge([], [_row("o/new", deliver_base="main")])
        assert entry == RepoConfig(repo="o/new", enabled=True, deliver_base="main")

    def test_registration_order_is_the_list_order(self) -> None:
        declared = [RepoConfig(repo="o/b"), RepoConfig(repo="o/a")]
        merged = merge(declared, [_row("o/a"), _row("o/c"), _row("o/b")])
        assert [e.repo for e in merged] == ["o/a", "o/c", "o/b"]

    def test_a_file_entry_with_no_registration_is_not_in_the_list(self) -> None:
        assert merge([RepoConfig(repo="o/gone")], []) == []


@pytest.fixture
def clock() -> Any:
    class Clock:
        t = T0

        def __call__(self) -> float:
            return self.t

    return Clock()


class TestActivate:
    def test_the_files_entries_are_imported_once_with_their_provenance(
        self, tmp_path: Path, clock: Any
    ) -> None:
        config = _config(tmp_path, {"repo": "o/one", "deliver_base": "dev"}, {"repo": "o/two"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        assert registry.activate() == ["o/one", "o/two"]
        rows = store.repositories()
        assert [(r.repo, r.source, r.enabled, r.deliver_base, r.created_at) for r in rows] == [
            ("o/one", "config", True, "dev", T0),
            ("o/two", "config", True, None, T0),
        ]
        # Idempotent within the process, and nothing new for the next one.
        assert registry.activate() == ["o/one", "o/two"]
        assert RepositoryRegistry(config, store, clock=clock).activate() == []
        assert [e.repo for e in config.repo_list()] == ["o/one", "o/two"]
        assert [e.repo for e in config.declared_repos()] == ["o/one", "o/two"]

    def test_the_registration_wins_over_the_file_at_the_next_start(
        self, tmp_path: Path, clock: Any
    ) -> None:
        config = _config(tmp_path, {"repo": "o/one", "trigger_label": "go"})
        store = DaemonStore(config.paths.state_db)
        RepositoryRegistry(config, store, clock=clock).activate()
        store.update_repository("o/one", now=T0 + 1, enabled=False, deliver_base="release")
        # A fresh load of the same file, as the next daemon start makes.
        fresh = _config(tmp_path, {"repo": "o/one", "trigger_label": "go"})
        assert fresh.enabled_repos() != []
        RepositoryRegistry(fresh, store, clock=clock).activate()
        assert fresh.enabled_repos() == []
        entry = fresh.find_repo("o/one")
        assert entry is not None
        assert entry.deliver_base == "release" and entry.trigger_label == "go"

    def test_a_removed_registration_blocks_the_files_copy(self, tmp_path: Path, clock: Any) -> None:
        config = _config(tmp_path, {"repo": "o/one"}, {"repo": "o/two"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        registry.activate()
        registry.remove("o/two", by="tester")
        assert [e.repo for e in config.repo_list()] == ["o/one"]
        assert [e.repo for e in config.declared_repos()] == ["o/one", "o/two"]
        fresh = _config(tmp_path, {"repo": "o/one"}, {"repo": "o/two"})
        assert RepositoryRegistry(fresh, store, clock=clock).activate() == []
        assert [e.repo for e in fresh.repo_list()] == ["o/one"]
        assert [r.repo for r in store.repositories(include_removed=True)] == ["o/one", "o/two"]


class TestMutations:
    def test_add_validates_the_name_for_its_forge_and_refuses_a_taken_one(
        self, tmp_path: Path, clock: Any
    ) -> None:
        config = _config(tmp_path, {"repo": "o/one"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        registry.activate()
        with pytest.raises(ValueError, match="owner/name"):
            registry.add("nope", kind=None, enabled=True, deliver_base=None, by=None, source="api")
        with pytest.raises(ValueError, match="owner/name"):
            registry.add(
                "a/b/c", kind="github", enabled=True, deliver_base=None, by=None, source="api"
            )
        with pytest.raises(ValueError, match="o/one is already registered"):
            registry.add("O/ONE", kind=None, enabled=True, deliver_base=None, by=None, source="api")
        added = registry.add(
            "group/sub/project",
            kind="gitlab",
            enabled=False,
            deliver_base=" ",
            by="me",
            source="api",
        )
        assert added.kind == "gitlab" and added.enabled is False and added.deliver_base is None
        assert added.created_by == "me" and added.source == "api"
        assert [e.repo for e in config.repo_list()] == ["o/one", "group/sub/project"]
        assert config.vcs_kind_for("group/sub/project") == "gitlab"

    def test_update_changes_only_what_a_registration_owns(self, tmp_path: Path, clock: Any) -> None:
        config = _config(tmp_path, {"repo": "o/one"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        registry.activate()
        with pytest.raises(KeyError):
            registry.update("o/none", {"enabled": False}, by=None)
        with pytest.raises(ValueError, match="not trigger_label"):
            registry.update("o/one", {"trigger_label": "x"}, by=None)
        clock.t = T0 + 5
        row = registry.update("O/One", {"deliver_base": "main"}, by=None)
        assert row.repo == "o/one" and row.deliver_base == "main" and row.updated_at == T0 + 5
        assert config.effective_repo("o/one").deliver_base == "main"  # type: ignore[union-attr]
        row = registry.update("o/one", {"deliver_base": None, "enabled": False}, by=None)
        assert row.deliver_base is None and row.enabled is False
        assert config.enabled_repos() == []

    def test_a_removed_name_is_registered_again_as_new(self, tmp_path: Path, clock: Any) -> None:
        config = _config(tmp_path, {"repo": "o/one"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        registry.activate()
        registry.remove("o/one", by=None)
        with pytest.raises(KeyError):
            registry.remove("o/one", by=None)
        clock.t = T0 + 9
        row = registry.add(
            "o/one", kind=None, enabled=True, deliver_base="v2", by="me", source="api"
        )
        assert row.source == "api" and row.created_at == T0 + 9 and row.removed_at is None
        assert [(e.repo, e.deliver_base) for e in config.repo_list()] == [("o/one", "v2")]


class TestDoctor:
    def test_reads_the_registry_and_names_the_files_drift(self, tmp_path: Path, clock: Any) -> None:
        config = _config(tmp_path, {"repo": "o/one"}, {"repo": "o/two", "deliver_base": "dev"})
        store = DaemonStore(config.paths.state_db)
        registry = RepositoryRegistry(config, store, clock=clock)
        registry.activate()
        registry.update("o/one", {"enabled": False}, by=None)
        registry.add("o/three", kind=None, enabled=True, deliver_base=None, by="me", source="api")
        store.close()
        # Doctor loads the file afresh and reads the daemon's registry.
        fresh = _config(tmp_path, {"repo": "o/one"}, {"repo": "o/two", "deliver_base": "dev"})
        stored = stored_repositories(fresh)
        assert [r.repo for r in stored] == ["o/one", "o/two", "o/three"]
        checks = apply_registry(fresh, stored)
        assert [e.repo for e in fresh.enabled_repos()] == ["o/two", "o/three"]
        listed, drift = checks
        assert (
            listed.ok
            and "o/three (api)" in listed.detail
            and "o/one (config, disabled)" in listed.detail
        )
        assert not drift.ok and not drift.hard
        assert "o/one: enabled = True in the file, False registered" in drift.detail

    def test_no_database_is_no_registry(self, tmp_path: Path) -> None:
        config = _config(tmp_path, {"repo": "o/one"})
        assert stored_repositories(config) == []
        assert apply_registry(config, []) == []
        assert [e.repo for e in config.enabled_repos()] == ["o/one"]
