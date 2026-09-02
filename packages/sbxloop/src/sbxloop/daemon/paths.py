"""Where the daemon keeps its state (#255, the practical half of #224).

The top-level ``state_dir`` defaults to the *relative* ``.sbxloop`` — fine
for a one-shot ``sbxloop run``, wrong for a daemon: when its cwd is the
checkout it works on, every per-run clone nests under
``<checkout>/.sbxloop/runs/*/workspace`` and the checkout grows without
bound (a full ``.git`` per run, nothing collects it — #233). The daemon
therefore anchors its state to an absolute path outside the workspace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sbxloop.config import Config

LEGACY_STATE_NAME = ".sbxloop"


@dataclass(frozen=True)
class StateDirChoice:
    path: Path
    # Which rule picked it — surfaced at startup so an operator can tell why
    # `sbxloop status` in the runner dir shows nothing.
    reason: str


def resolve_state_dir(
    config: Config,
    sources: Mapping[str, str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    home: Path,
) -> StateDirChoice:
    """Pick the daemon's absolute state directory.

    Precedence: ``[daemon] state_dir`` > an explicitly configured top-level
    ``state_dir`` (made absolute against cwd) > a legacy ``./.sbxloop`` that
    already holds a ``state.db`` (an upgraded deployment must keep its queue,
    ledger and resumable runs — moving out from under them would orphan
    every in-progress issue) > ``$XDG_STATE_HOME/sbxloop/<project>``, where
    ``<project>`` is the cwd's name: the runner directory that owns this
    daemon's config and tokens, one per deployment.
    """
    if config.daemon.state_dir is not None:
        path = Path(config.daemon.state_dir).expanduser()
        return StateDirChoice((cwd / path).resolve(), "[daemon] state_dir")
    if sources.get("state_dir", "default") != "default":
        return StateDirChoice((cwd / config.state_dir).resolve(), "state_dir")
    legacy = (cwd / LEGACY_STATE_NAME).resolve()
    if (legacy / "state.db").is_file():
        return StateDirChoice(legacy, "legacy ./.sbxloop state present")
    xdg = env.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else home / ".local" / "state"
    project = cwd.resolve().name or "default"
    return StateDirChoice((base / "sbxloop" / project).resolve(), "default (XDG state home)")


def resolve_cli_state_dir(
    config: Config,
    sources: Mapping[str, str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    home: Path,
) -> StateDirChoice:
    """Where the run-inspection commands (``status``, ``logs``,
    ``artifacts``, ``gc``) should look.

    The daemon anchors its state away from the top-level default, so on a
    daemon host those commands read one store while the daemon writes
    another: ``sbxloop status`` in the runner directory answers with an
    unrelated (usually stale, often empty) world, and there is no flag to
    talk it out of it. ``sbxloop daemon`` and its ``ctl`` subcommands
    already follow :func:`resolve_state_dir` for exactly this reason
    (``_daemon_state_dir``); the run commands were left behind.

    So: follow the daemon's rule whenever a daemon store actually exists for
    this directory, and otherwise keep the plain default. A single-user
    ``sbxloop run`` host has no such store and is unaffected — this only
    ever redirects a lookup that a daemon has already staked out, and it
    moves nothing on disk.
    """
    daemon_choice = resolve_state_dir(config, sources, cwd=cwd, env=env, home=home)
    configured = config.daemon.state_dir is not None or (
        sources.get("state_dir", "default") != "default"
    )
    if configured or (daemon_choice.path / "state.db").is_file():
        return daemon_choice
    return StateDirChoice(config.state_dir.expanduser().resolve(), "state_dir default")
