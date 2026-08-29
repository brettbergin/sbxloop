"""Per-repo workspace resolution (``Config.workspace_for_repo``).

The multi-repo failure this guards: with one ``[sandbox] workspace`` and
several enabled repos, a run must never be built from another repository's
checkout.
"""

from __future__ import annotations

from pathlib import Path

from git import Repo

from sbxloop.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "sbxloop.toml").write_text(body)
    return tmp_path


def _checkout(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(path)
    repo.create_remote("origin", f"https://github.com/{origin}.git")
    return path


def test_repo_workspace_parses_from_repos_toml(tmp_path: Path) -> None:
    ws = tmp_path / "one-tree"
    _write(
        tmp_path,
        f'[[github.repos]]\nrepo = "o/one"\nworkspace = "{ws}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.github.repo_list()[0].workspace == ws


def test_legacy_single_repo_workspace_unchanged(tmp_path: Path) -> None:
    ws = tmp_path / "tree"
    ws.mkdir()
    _write(
        tmp_path,
        f'[github]\nrepo = "o/r"\n\n[sandbox]\nworkspace = "{ws}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.sandbox.workspace == ws
    assert cfg.workspace_for_repo("o/r") == ws
    assert cfg.workspace_for_repo(None) == ws


def test_per_repo_workspace_overrides_legacy(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    own = tmp_path / "own"
    _write(
        tmp_path,
        "[[github.repos]]\n"
        f'repo = "o/one"\nworkspace = "{own}"\n\n'
        "[[github.repos]]\n"
        'repo = "o/two"\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo("o/one") == own


def test_multi_repo_legacy_workspace_only_for_matching_origin(tmp_path: Path) -> None:
    legacy = _checkout(tmp_path / "one-checkout", "o/one")
    _write(
        tmp_path,
        "[[github.repos]]\n"
        'repo = "o/one"\n\n'
        "[[github.repos]]\n"
        'repo = "o/two"\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo("o/one") == legacy
    # The other repo must not be handed someone else's tree.
    assert cfg.workspace_for_repo("o/two") is None


def test_multi_repo_non_git_legacy_workspace_resolves_for_nobody(tmp_path: Path) -> None:
    legacy = tmp_path / "plain"
    legacy.mkdir()
    _write(
        tmp_path,
        "[[github.repos]]\n"
        'repo = "o/one"\n\n'
        "[[github.repos]]\n"
        'repo = "o/two"\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo("o/one") is None
    assert cfg.workspace_for_repo("o/two") is None


def test_disabled_second_repo_keeps_single_repo_behaviour(tmp_path: Path) -> None:
    legacy = tmp_path / "tree"
    legacy.mkdir()
    _write(
        tmp_path,
        "[[github.repos]]\n"
        'repo = "o/one"\n\n'
        "[[github.repos]]\n"
        'repo = "o/two"\nenabled = false\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo("o/one") == legacy
    assert cfg.workspace_for_repo("o/two") is None


def test_no_github_config_still_uses_sandbox_workspace(tmp_path: Path) -> None:
    legacy = tmp_path / "tree"
    legacy.mkdir()
    _write(tmp_path, f'[sandbox]\nworkspace = "{legacy}"\n')
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo(None) == legacy


def test_narrowed_run_config_carries_resolved_workspace(tmp_path: Path) -> None:
    legacy = _checkout(tmp_path / "one-checkout", "o/one")
    _write(
        tmp_path,
        "[[github.repos]]\n"
        'repo = "o/one"\n\n'
        "[[github.repos]]\n"
        'repo = "o/two"\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    one = cfg.github.for_repo("o/one", workspace=cfg.workspace_for_repo("o/one"))
    assert one.repos[0].workspace == legacy
    two = cfg.github.for_repo("o/two", workspace=cfg.workspace_for_repo("o/two"))
    assert two.repos[0].workspace is None


def test_narrowed_config_keeps_multi_repo_workspace_refusal(tmp_path: Path) -> None:
    """The run's narrowed config must resolve the same workspace as the daemon.

    ``GithubConfig.for_repo`` cuts ``repos`` to one entry; resolution must not
    then read that single-entry list as a single-repo deployment and hand the
    run the legacy checkout of the *other* repo (#526).
    """
    legacy = _checkout(tmp_path / "a-checkout", "o/a")
    _write(
        tmp_path,
        '[[github.repos]]\nrepo = "o/a"\n\n'
        '[[github.repos]]\nrepo = "o/b"\n\n'
        f'[sandbox]\nworkspace = "{legacy}"\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.workspace_for_repo("o/b") is None
    narrowed = cfg.model_copy(
        update={"github": cfg.github.for_repo("o/b", workspace=cfg.workspace_for_repo("o/b"))}
    )
    assert narrowed.github.multi_repo is True
    assert narrowed.workspace_for_repo("o/b") is None
    # The matching repo still resolves to the legacy checkout after narrowing.
    narrowed_a = cfg.model_copy(
        update={"github": cfg.github.for_repo("o/a", workspace=cfg.workspace_for_repo("o/a"))}
    )
    assert narrowed_a.workspace_for_repo("o/a") == legacy
