"""The sbxloop home: one root for everything sbxloop puts on a host.

Every path sbxloop reads or writes on the host hangs off one directory,
``$SBXLOOP_HOME`` (``~/.sbxloop`` unless the variable says otherwise)::

    ~/.sbxloop/
    ├── bin/         sbxloop, sbx      launchers; no secrets in them
    ├── venv/                          the interpreter and the packages
    ├── config/      sbxloop.toml, secrets.env (0600), github-app.pem (0600)
    ├── state/       state.db, bake.json, conformance/, daemon/{ctl,…}, gc-pending/
    ├── runs/        <run_id>/{workspace,artifacts,data}
    ├── workspaces/  <owner>/<repo>    dedicated clones the daemon refreshes
    ├── logs/        daemon.log, console/, deploy/
    ├── cache/       pip/, worker-wheels/, sdk/
    ├── tmp/         every temporary file
    ├── systemd/     the unit files (linked into ~/.config/systemd/user)
    ├── backups/     <timestamp>/ snapshots
    └── home.json    which layout this is and who installed it

There is deliberately no second rule. The former ``state_dir`` setting,
the daemon's XDG state directory keyed by the working directory's name,
the ``~/.config/sbxloop`` user config, the working-directory ``.env`` and
the ``~/.sbxloop-venv`` + ``~/.local/bin/sbxloop`` install each answered
the same question a different way, and one host ended up with three state
directories and a launcher that exported every secret to every child
process. A path that is not derived from :class:`SbxloopHome` is a bug.

``SBXLOOP_HOME`` is the only override, and it moves the whole tree — a
second daemon on the same host is a second home. Nothing here resolves
the working directory: the daemon's ``WorkingDirectory`` is the home, and
the run commands answer the same from any directory.
"""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

#: The one environment variable that relocates the home.
HOME_ENV = "SBXLOOP_HOME"
#: The default home, under the user's home directory.
HOME_DIRNAME = ".sbxloop"
#: Bumped when the on-disk layout changes shape; ``home.json`` records the
#: version a home was laid out with so a later ``sbxloop init`` can migrate.
LAYOUT_VERSION = 1
HOME_RECORD = "home.json"

#: Directory modes. ``config/`` holds secrets and is private; the rest is
#: ordinary. ``secrets.env`` and the App key are files, chmod 0600 by the
#: code that writes them.
PRIVATE_DIR_MODE = 0o700
DIR_MODE = 0o755


def home_root_from_env(env: Mapping[str, str]) -> Path | None:
    """The home ``env`` names: ``$SBXLOOP_HOME``, else ``$HOME/.sbxloop``,
    else None — the hermetic case, for a caller that passed an environment
    naming neither (tests, embedders) and must not touch the real home."""
    raw = env.get(HOME_ENV, "").strip()
    if raw:
        return _expand(raw, env)
    home = env.get("HOME", "").strip()
    if home:
        return Path(home) / HOME_DIRNAME
    return None


def resolve_home_root(env: Mapping[str, str] | None = None) -> Path:
    """The home for this process: :func:`home_root_from_env`, falling back
    to the process home directory."""
    root = home_root_from_env(os.environ if env is None else env)
    return root if root is not None else Path.home() / HOME_DIRNAME


def _expand(raw: str, env: Mapping[str, str]) -> Path:
    """``~`` against the mapped HOME (never the process one); a relative
    value is anchored at the current directory once, here, so it cannot
    drift with a later chdir."""
    if raw == "~" or raw.startswith("~/"):
        home = env.get("HOME", "").strip() or str(Path.home())
        raw = home + raw[1:]
    return Path(raw).resolve()


class HomeRecord(BaseModel):
    """``home.json``: what laid this home out."""

    model_config = ConfigDict(extra="forbid")

    layout_version: int = LAYOUT_VERSION
    sbxloop_version: str
    python: str
    created_at: str
    updated_at: str
    created_by: str  # the command: "sbxloop init", "install.sh", …


@dataclass(frozen=True)
class SbxloopHome:
    """Every host path, derived from the one root."""

    root: Path

    # -- the tree -------------------------------------------------------------

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def systemd(self) -> Path:
        return self.root / "systemd"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def record(self) -> Path:
        return self.root / HOME_RECORD

    # -- files and subdirectories the code addresses ----------------------------

    @property
    def launcher(self) -> Path:
        return self.bin / "sbxloop"

    @property
    def sbx_launcher(self) -> Path:
        return self.bin / "sbx"

    @property
    def venv_python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def venv_sbxloop(self) -> Path:
        return self.venv / "bin" / "sbxloop"

    @property
    def config_toml(self) -> Path:
        return self.config / "sbxloop.toml"

    @property
    def secrets_env(self) -> Path:
        return self.config / "secrets.env"

    @property
    def github_app_pem(self) -> Path:
        return self.config / "github-app.pem"

    @property
    def state_db(self) -> Path:
        return self.state / "state.db"

    @property
    def bake_json(self) -> Path:
        return self.state / "bake.json"

    @property
    def conformance(self) -> Path:
        return self.state / "conformance"

    @property
    def daemon(self) -> Path:
        return self.state / "daemon"

    @property
    def ctl(self) -> Path:
        return self.daemon / "ctl"

    @property
    def github_workspace(self) -> Path:
        return self.daemon / "github-workspace"

    @property
    def concierge_workspace(self) -> Path:
        return self.daemon / "concierge-workspace"

    @property
    def gc_pending(self) -> Path:
        return self.state / "gc-pending"

    @property
    def daemon_log(self) -> Path:
        return self.logs / "daemon.log"

    @property
    def console(self) -> Path:
        return self.logs / "console"

    @property
    def deploy_logs(self) -> Path:
        return self.logs / "deploy"

    @property
    def worker_wheels(self) -> Path:
        return self.cache / "worker-wheels"

    @property
    def uv(self) -> Path:
        """The uv the home installs and updates itself with."""
        return self.bin / "uv"

    @property
    def python(self) -> Path:
        """Where uv keeps the interpreters it installs for this home."""
        return self.root / "python"

    @property
    def sbx_prefix(self) -> Path:
        """Docker's sbx, installed by its own installer with this PREFIX."""
        return self.root / "sbx"

    @property
    def sbx_binary(self) -> Path:
        return self.sbx_prefix / "bin" / "sbx"

    @property
    def sbx_version_file(self) -> Path:
        return self.sbx_prefix / "VERSION"

    def unit(self, name: str) -> Path:
        return self.systemd / name

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def run_workspace(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def run_artifacts(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def run_data(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "data"

    def workspace_for(self, repo: str) -> Path:
        """The dedicated clone of ``owner/name``: ``workspaces/owner/name``."""
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name or ".." in (owner, name):
            raise ValueError(f"not an owner/name repository: {repo!r}")
        return self.workspaces / owner / name

    # -- laying it out --------------------------------------------------------

    @property
    def directories(self) -> tuple[tuple[Path, int], ...]:
        """Every directory ``init`` creates, with its mode."""
        return (
            (self.root, DIR_MODE),
            (self.bin, DIR_MODE),
            (self.config, PRIVATE_DIR_MODE),
            (self.state, DIR_MODE),
            (self.daemon, DIR_MODE),
            (self.ctl, DIR_MODE),
            (self.conformance, DIR_MODE),
            (self.runs, DIR_MODE),
            (self.workspaces, DIR_MODE),
            (self.logs, DIR_MODE),
            (self.console, DIR_MODE),
            (self.deploy_logs, DIR_MODE),
            (self.cache, DIR_MODE),
            (self.worker_wheels, DIR_MODE),
            (self.tmp, DIR_MODE),
            (self.systemd, DIR_MODE),
            (self.backups, DIR_MODE),
        )

    def ensure_tree(self) -> None:
        """Create every directory that is missing (idempotent). The venv is
        not a plain directory and is left to the installer."""
        for path, mode in self.directories:
            path.mkdir(parents=True, exist_ok=True)
            if mode == PRIVATE_DIR_MODE:
                path.chmod(mode)

    def missing_directories(self) -> list[Path]:
        return [path for path, _mode in self.directories if not path.is_dir()]

    @property
    def initialised(self) -> bool:
        return self.record.is_file()

    def read_record(self) -> HomeRecord | None:
        try:
            return HomeRecord.model_validate(json.loads(self.record.read_text()))
        except (OSError, ValueError):
            return None

    def write_record(self, *, sbxloop_version: str, created_by: str) -> HomeRecord:
        """Stamp ``home.json``; an existing record keeps its ``created_*``."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        previous = self.read_record()
        record = HomeRecord(
            sbxloop_version=sbxloop_version,
            python=platform.python_version(),
            created_at=previous.created_at if previous else now,
            updated_at=now,
            created_by=previous.created_by if previous else created_by,
        )
        self.record.write_text(json.dumps(record.model_dump(), indent=2) + "\n")
        return record

    def as_env(self) -> dict[str, str]:
        """What a child process needs to land in this home."""
        return {HOME_ENV: str(self.root)}

    def __str__(self) -> str:
        return str(self.root)


# -- the layouts this one replaces ----------------------------------------------


@dataclass(frozen=True)
class LegacyPath:
    path: Path
    what: str


def legacy_paths(
    home: SbxloopHome, env: Mapping[str, str], *, cwd: Path | None = None
) -> list[LegacyPath]:
    """Where an installation from before the home existed left things.

    Reported by ``sbxloop doctor`` as a hard failure and moved by
    ``sbxloop init --migrate``. A host with two layouts is misconfigured:
    which one a command reads would depend on which rule it applied, which
    is exactly what the home ends.
    """
    found: list[LegacyPath] = []
    user_home = Path(env.get("HOME", "").strip() or str(Path.home()))
    xdg_config = env.get("XDG_CONFIG_HOME", "").strip()
    xdg_state = env.get("XDG_STATE_HOME", "").strip()
    config_root = Path(xdg_config) if xdg_config else user_home / ".config"
    state_root = Path(xdg_state) if xdg_state else user_home / ".local" / "state"
    candidates: list[tuple[Path, str]] = [
        (config_root / "sbxloop", "user config and secrets (~/.config/sbxloop)"),
        (state_root / "sbxloop", "daemon state keyed by working directory (XDG state home)"),
        (user_home / ".sbxloop-venv", "the pre-home virtualenv"),
        (user_home / ".local" / "bin" / "sbxloop", "the pre-home launcher"),
        # The flat layout that lived at ~/.sbxloop before state/ existed.
        (home.root / "state.db", "state.db at the root of the home (flat layout)"),
        (home.root / "daemon", "daemon/ at the root of the home (flat layout)"),
        (home.root / "conformance", "conformance/ at the root of the home (flat layout)"),
        (home.root / "bake.json", "bake.json at the root of the home (flat layout)"),
        (home.root / "gc-pending", "gc-pending/ at the root of the home (flat layout)"),
    ]
    if cwd is not None and cwd.resolve() != home.root.resolve():
        candidates.append(
            (cwd / ".sbxloop" / "state.db", "project-scoped state in the working directory")
        )
    for path, what in candidates:
        if path.exists() or path.is_symlink():
            found.append(LegacyPath(path, what))
    return found


def describe(paths: list[LegacyPath]) -> str:
    return "; ".join(f"{p.path} ({p.what})" for p in paths)


__all__ = [
    "DIR_MODE",
    "HOME_DIRNAME",
    "HOME_ENV",
    "HOME_RECORD",
    "LAYOUT_VERSION",
    "PRIVATE_DIR_MODE",
    "HomeRecord",
    "LegacyPath",
    "SbxloopHome",
    "describe",
    "home_root_from_env",
    "legacy_paths",
    "resolve_home_root",
]
