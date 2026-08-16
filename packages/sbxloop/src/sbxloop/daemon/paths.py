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
