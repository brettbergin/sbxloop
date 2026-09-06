"""``sbxloop init --migrate``: move a pre-home installation into the home.

Before the home, an installation was spread over a venv at
``~/.sbxloop-venv``, launchers in ``~/.local/bin`` sourcing an ``env.sh``
that exported every secret, a ``~/.config/sbxloop`` with the secrets and
the App key, a "runner directory" (the daemon unit's ``WorkingDirectory``)
holding ``sbxloop.toml``, and state under ``$XDG_STATE_HOME/sbxloop/<the
runner directory's name>`` — plus whatever stray state directories the
old rules had scattered (``~/.sbxloop`` itself, ``./.sbxloop`` in a
checkout). This module finds all of it, snapshots it, carries what the
home needs, lays the home out through :class:`sbxloop.homeinit.HomeInit`,
and — with ``--purge`` — removes the leftovers so ``sbxloop doctor``
stops failing on them.

What is carried: the daemon's ``state.db`` (the queue, the ledger, the
mailbox; copied through SQLite), the runner directory's ``sbxloop.toml``
(with the retired ``state_dir`` keys dropped and each ``workspace`` that
is a git checkout moved under ``workspaces/<owner>/<name>``), the secrets
file and the App key (the key's path rewritten in the secrets). What is
not: run directories — clones of repositories that exist on GitHub — and
the old venv, which the home rebuilds from the index.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sbxloop import hostgit
from sbxloop.backup import BackupInfo, _copy_db, create_backup
from sbxloop.errors import SbxloopError
from sbxloop.homeinit import RUNNER_UNIT, UNIT_NAMES, HomeInit, InitOptions, InitReport
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome

log = get_logger(__name__)

DAEMON_UNIT = "sbxloop-daemon.service"
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Printer = Callable[[str], None]

_WORKSPACE_LINE = re.compile(r'^(\s*workspace\s*=\s*")([^"]+)(".*)$')
_RETIRED_LINE = re.compile(r"^\s*state_dir\s*=")
_PEM_LINE = re.compile(r"^(\s*(?:export\s+)?GITHUB_APP_PRIVATE_KEY_PATH\s*=\s*)(.*)$")


class MigrateError(SbxloopError):
    pass


@dataclass
class LegacyInstall:
    """Everything the old layouts left on this host."""

    user_home: Path
    runner_dir: Path | None = None
    unit_files: list[Path] = field(default_factory=list)
    config_toml: Path | None = None  # the daemon's (runner dir)
    user_toml: Path | None = None  # ~/.config/sbxloop/sbxloop.toml
    secrets: Path | None = None
    other_secrets: list[Path] = field(default_factory=list)
    pem: Path | None = None
    env_sh: Path | None = None
    live_state_db: Path | None = None
    state_db_candidates: list[Path] = field(default_factory=list)
    legacy_state_roots: list[Path] = field(default_factory=list)
    flat_home_files: list[Path] = field(default_factory=list)
    venv: Path | None = None
    launchers: list[Path] = field(default_factory=list)
    runner_dir_files: list[Path] = field(default_factory=list)
    actions_runner: Path | None = None
    daemon_active: bool = False

    def summary(self) -> list[str]:
        lines: list[str] = []
        if self.runner_dir:
            lines.append(f"runner directory: {self.runner_dir}")
        if self.live_state_db:
            lines.append(f"live state.db: {self.live_state_db}")
        for db in self.state_db_candidates:
            if db != self.live_state_db:
                lines.append(f"other state.db: {db}")
        if self.config_toml:
            lines.append(f"config: {self.config_toml}")
        if self.user_toml:
            lines.append(f"user config: {self.user_toml}")
        if self.secrets:
            lines.append(f"secrets: {self.secrets}")
        for path in self.other_secrets:
            lines.append(f"other secrets file: {path}")
        if self.pem:
            lines.append(f"App key: {self.pem}")
        if self.venv:
            lines.append(f"old venv: {self.venv}")
        for path in self.launchers:
            lines.append(f"old launcher: {path}")
        for path in self.unit_files:
            lines.append(f"unit file: {path}")
        for path in self.flat_home_files:
            lines.append(f"flat layout in the home: {path}")
        for path in self.legacy_state_roots:
            lines.append(f"legacy state root: {path}")
        if self.actions_runner:
            lines.append(f"Actions runner: {self.actions_runner} (stays; its unit is re-rendered)")
        lines.append("daemon unit: " + ("active" if self.daemon_active else "not active"))
        return lines


@dataclass(frozen=True)
class MigrateOptions:
    purge: bool = False
    keep_runs: bool = False
    state_db: Path | None = None  # --from


@dataclass
class MigrateReport:
    legacy: LegacyInstall
    backup: BackupInfo | None = None
    init: InitReport | None = None
    carried: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    restarted: bool = False


def _unit_working_directory(unit_file: Path, user_home: Path) -> Path | None:
    try:
        text = unit_file.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "WorkingDirectory":
            value = value.strip().replace("%h", str(user_home))
            return Path(value).expanduser()
    return None


def _is_our_launcher(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return "sbxloop" in text or "env.sh" in text


def _daemon_active(run: Runner) -> bool:
    try:
        result = run(["systemctl", "--user", "is-active", DAEMON_UNIT])
    except (OSError, subprocess.CalledProcessError):
        return False
    return result.stdout.strip() == "active"


def discover(
    home: SbxloopHome,
    env: Mapping[str, str],
    *,
    cwd: Path,
    run: Runner,
    user_units: Path | None = None,
) -> LegacyInstall:
    """Find the old layouts on this host. Read-only."""
    user_home = Path(env.get("HOME", "").strip() or str(Path.home()))
    xdg_config = env.get("XDG_CONFIG_HOME", "").strip()
    xdg_state = env.get("XDG_STATE_HOME", "").strip()
    config_root = (Path(xdg_config) if xdg_config else user_home / ".config") / "sbxloop"
    state_root = (Path(xdg_state) if xdg_state else user_home / ".local" / "state") / "sbxloop"
    units_dir = user_units or user_home / ".config" / "systemd" / "user"
    legacy = LegacyInstall(user_home=user_home)

    for name in (*UNIT_NAMES, RUNNER_UNIT):
        unit = units_dir / name
        if unit.exists() and not unit.is_symlink():
            legacy.unit_files.append(unit)
    daemon_unit = units_dir / DAEMON_UNIT
    if daemon_unit.exists():
        legacy.runner_dir = _unit_working_directory(daemon_unit, user_home)
    legacy.daemon_active = _daemon_active(run)

    if legacy.runner_dir and legacy.runner_dir.is_dir():
        toml = legacy.runner_dir / "sbxloop.toml"
        if toml.is_file():
            legacy.config_toml = toml
        for path in sorted(legacy.runner_dir.iterdir()):
            if path.name == "sbxloop.toml" or path.name.startswith("sbxloop.toml."):
                legacy.runner_dir_files.append(path)
            elif path.name in (".env", "workload-profile.toml", ".workload-profile.toml"):
                legacy.runner_dir_files.append(path)
                if path.name == ".env":
                    legacy.other_secrets.append(path)
            elif path.name == ".sbxloop" and path.is_dir():
                legacy.runner_dir_files.append(path)
                if (path / "state.db").is_file():
                    legacy.state_db_candidates.append(path / "state.db")

    if config_root.is_dir():
        for name in ("secrets.env", ".env"):
            path = config_root / name
            if path.is_file():
                if legacy.secrets is None:
                    legacy.secrets = path
                else:
                    legacy.other_secrets.append(path)
        if (config_root / "sbxloop.toml").is_file():
            legacy.user_toml = config_root / "sbxloop.toml"
        if (config_root / "github-app.pem").is_file():
            legacy.pem = config_root / "github-app.pem"
        if (config_root / "env.sh").is_file():
            legacy.env_sh = config_root / "env.sh"
        legacy.legacy_state_roots.append(config_root)
    if legacy.secrets is None and legacy.other_secrets:
        legacy.secrets = legacy.other_secrets.pop(0)

    if state_root.is_dir():
        for path in sorted(state_root.iterdir()):
            db = path / "state.db"
            if db.is_file() and db not in legacy.state_db_candidates:
                legacy.state_db_candidates.append(db)
        legacy.legacy_state_roots.append(state_root)

    for name in ("state.db", "daemon", "conformance", "bake.json", "gc-pending"):
        path = home.root / name
        if path.exists():
            legacy.flat_home_files.append(path)
            if name == "state.db" and path not in legacy.state_db_candidates:
                legacy.state_db_candidates.append(path)
    if cwd.resolve() != home.root.resolve() and (cwd / ".sbxloop" / "state.db").is_file():
        legacy.state_db_candidates.append(cwd / ".sbxloop" / "state.db")
        legacy.legacy_state_roots.append(cwd / ".sbxloop")

    venv = user_home / ".sbxloop-venv"
    if venv.is_dir():
        legacy.venv = venv
    for name in ("sbxloop", "sbx"):
        launcher = user_home / ".local" / "bin" / name
        if launcher.is_file() and _is_our_launcher(launcher):
            legacy.launchers.append(launcher)
    runner = user_home / "actions-runner"
    if (runner / "run.sh").is_file():
        legacy.actions_runner = runner

    legacy.live_state_db = _pick_live_state_db(legacy, state_root)
    return legacy


def _pick_live_state_db(legacy: LegacyInstall, state_root: Path) -> Path | None:
    """The daemon's database, by the rule the old daemon applied from its
    working directory: ``[daemon] state_dir`` in its config, else a
    ``./.sbxloop/state.db`` there, else the XDG directory named after it."""
    if legacy.runner_dir is not None:
        if legacy.config_toml is not None:
            try:
                data = tomllib.loads(legacy.config_toml.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                data = {}
            daemon_table = data.get("daemon", {})
            pinned = daemon_table.get("state_dir") if isinstance(daemon_table, dict) else None
            if isinstance(pinned, str):
                candidate = (legacy.runner_dir / Path(pinned).expanduser()).resolve() / "state.db"
                if candidate.is_file():
                    return candidate
            top = data.get("state_dir")
            if isinstance(top, str):
                candidate = (legacy.runner_dir / Path(top).expanduser()).resolve() / "state.db"
                if candidate.is_file():
                    return candidate
        local = legacy.runner_dir / ".sbxloop" / "state.db"
        if local.is_file():
            return local
        xdg = state_root / legacy.runner_dir.name / "state.db"
        if xdg.is_file():
            return xdg
    if len(legacy.state_db_candidates) == 1:
        return legacy.state_db_candidates[0]
    return None


class HomeMigration:
    def __init__(
        self,
        home: SbxloopHome,
        legacy: LegacyInstall,
        options: MigrateOptions,
        *,
        init: HomeInit,
        run: Runner,
        say: Printer | None = None,
    ) -> None:
        self.home = home
        self.legacy = legacy
        self.options = options
        self.init = init
        self.run = run
        self.say = say or (lambda _line: None)
        self.report = MigrateReport(legacy)

    def execute(self) -> MigrateReport:
        legacy, home = self.legacy, self.home
        live_db = self.options.state_db or legacy.live_state_db
        if live_db is None and legacy.state_db_candidates:
            raise MigrateError(
                "several state databases and no daemon unit to say which is live: "
                + ", ".join(str(p) for p in legacy.state_db_candidates)
                + "; pass --from PATH"
            )
        if live_db is not None and not live_db.is_file():
            raise MigrateError(f"{live_db} is not a file")
        if legacy.daemon_active:
            self.say(f"stopping {DAEMON_UNIT}")
            self.run(["systemctl", "--user", "stop", DAEMON_UNIT])
        home.ensure_tree()
        self.report.backup = self._backup(live_db)
        self._clear_flat_home()
        self._carry_config()
        self._carry_secrets()
        if live_db is not None:
            self.say(f"carrying {live_db} → {home.state_db}")
            for suffix in ("-wal", "-shm"):
                home.state_db.with_name(home.state_db.name + suffix).unlink(missing_ok=True)
            _copy_db(live_db, home.state_db)
            self.report.carried.append(f"state.db from {live_db}")
        self.report.init = self.init.execute()
        if self.options.purge:
            self._purge()
        else:
            self.report.left.extend(self._purge_targets())
        if legacy.daemon_active:
            self.say(f"starting {DAEMON_UNIT}")
            self.run(["systemctl", "--user", "reset-failed", DAEMON_UNIT])
            self.run(["systemctl", "--user", "start", DAEMON_UNIT])
            self.report.restarted = True
        return self.report

    # -- steps --------------------------------------------------------------------

    def _backup(self, live_db: Path | None) -> BackupInfo:
        legacy = self.legacy
        extra: dict[str, Path] = {}

        def add(path: Path | None) -> None:
            if path is not None and path.is_file():
                rel = "legacy/" + str(path).lstrip("/").replace("/", "__")
                extra[rel] = path

        for path in (
            legacy.config_toml,
            legacy.user_toml,
            legacy.secrets,
            legacy.pem,
            legacy.env_sh,
        ):
            add(path)
        for path in (*legacy.other_secrets, *legacy.unit_files, *legacy.launchers):
            add(path)
        for path in legacy.runner_dir_files:
            if path.is_file():
                add(path)
        seen: set[Path] = set()
        for db in (live_db, *legacy.state_db_candidates):
            if db is not None and db.is_file() and db.resolve() not in seen:
                seen.add(db.resolve())
                add(db)
        info = create_backup(self.home, label="migrate", reason="init --migrate", extra=extra)
        self.say(f"backed up {info.files} file(s) to {info.path}")
        return info

    def _clear_flat_home(self) -> None:
        """The home used to be a flat state dir; its state, verdict cache and
        daemon scratch are in the backup (state.db) or regenerated, and the
        run directories are clones of repositories that exist elsewhere."""
        for path in self.legacy.flat_home_files:
            for suffix in ("", "-wal", "-shm") if path.name == "state.db" else ("",):
                target = path.with_name(path.name + suffix)
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            self.report.removed.append(str(path))
        if not self.options.keep_runs and self.home.runs.is_dir():
            removed = 0
            for run_dir in list(self.home.runs.iterdir()):
                if run_dir.is_dir():
                    shutil.rmtree(run_dir)
                    removed += 1
                else:
                    run_dir.unlink()
            if removed:
                self.report.removed.append(f"{removed} run directories under {self.home.runs}")

    def _carry_config(self) -> None:
        source = self.legacy.config_toml
        if self.home.config_toml.exists():
            self.report.notes.append(f"{self.home.config_toml} already exists; kept")
            return
        if source is None:
            if self.legacy.user_toml is not None:
                source = self.legacy.user_toml
            else:
                return
        text = source.read_text()
        lines: list[str] = []
        for line in text.splitlines():
            if _RETIRED_LINE.match(line):
                self.report.notes.append(f"dropped retired key from config: {line.strip()}")
                continue
            m = _WORKSPACE_LINE.match(line)
            if m:
                moved = self._relocate_workspace(Path(m.group(2)).expanduser())
                if moved is not None:
                    line = f"{m.group(1)}{moved}{m.group(3)}"
            lines.append(line)
        self.home.config_toml.write_text("\n".join(lines) + "\n")
        self.report.carried.append(f"config from {source}")
        if self.legacy.user_toml is not None and source is not self.legacy.user_toml:
            aside = self.home.config / "sbxloop.toml.user-legacy"
            shutil.copy2(self.legacy.user_toml, aside)
            self.report.notes.append(
                f"the user-level config {self.legacy.user_toml} was also in use; copied to "
                f"{aside} — merge what you still want into {self.home.config_toml}"
            )

    def _relocate_workspace(self, checkout: Path) -> Path | None:
        """Move a configured checkout under ``workspaces/<owner>/<name>``."""
        if not checkout.is_dir() or hostgit.repo_toplevel(checkout) != checkout.resolve():
            return None
        repo = hostgit.normalise_repo_url(hostgit.origin_url(checkout))
        if repo is None:
            self.report.notes.append(f"workspace {checkout} has no GitHub origin; left where it is")
            return None
        target = self.home.workspace_for(repo)
        if target.exists():
            if target.resolve() == checkout.resolve():
                return target
            self.report.notes.append(
                f"{target} already exists; workspace {checkout} left where it is"
            )
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(checkout), str(target))
        self.report.carried.append(f"workspace {checkout} → {target}")
        return target

    def _carry_secrets(self) -> None:
        legacy = self.legacy
        if legacy.pem is not None and not self.home.github_app_pem.exists():
            shutil.copy2(legacy.pem, self.home.github_app_pem)
            self.home.github_app_pem.chmod(0o600)
            self.report.carried.append(f"App key from {legacy.pem}")
        if self.home.secrets_env.exists():
            self.report.notes.append(f"{self.home.secrets_env} already exists; kept")
            return
        if legacy.secrets is None:
            return
        lines: list[str] = []
        for line in legacy.secrets.read_text().splitlines():
            m = _PEM_LINE.match(line)
            if m and self.home.github_app_pem.exists():
                line = f"{m.group(1)}{self.home.github_app_pem}"
            lines.append(line)
        self.home.secrets_env.touch(mode=0o600)
        self.home.secrets_env.write_text("\n".join(lines) + "\n")
        self.home.secrets_env.chmod(0o600)
        self.report.carried.append(f"secrets from {legacy.secrets}")
        for other in legacy.other_secrets:
            self.report.notes.append(
                f"another secrets file was in use, {other}; it is in the backup, "
                f"merge anything still needed into {self.home.secrets_env}"
            )

    def _purge_targets(self) -> list[str]:
        legacy = self.legacy
        targets: list[Path] = []
        if legacy.venv:
            targets.append(legacy.venv)
        targets.extend(legacy.launchers)
        targets.extend(legacy.legacy_state_roots)
        targets.extend(legacy.runner_dir_files)
        # Deduplicate and keep only what still exists.
        out: list[str] = []
        seen: set[Path] = set()
        for path in targets:
            if path.exists() and path.resolve() not in seen:
                seen.add(path.resolve())
                out.append(str(path))
        return out

    def _purge(self) -> None:
        for target in self._purge_targets():
            path = Path(target)
            if (
                path.resolve() == self.home.root.resolve()
                or self.home.root.resolve() in path.resolve().parents
            ):
                continue  # never inside the home
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            self.report.removed.append(target)
        if self.legacy.runner_dir and self.legacy.runner_dir.is_dir():
            leftover = [p.name for p in self.legacy.runner_dir.iterdir()]
            if leftover:
                self.report.notes.append(
                    f"{self.legacy.runner_dir} still holds {', '.join(sorted(leftover))}: "
                    "not sbxloop's to remove"
                )
            else:
                self.legacy.runner_dir.rmdir()
                self.report.removed.append(str(self.legacy.runner_dir))


def migrate_options_for(legacy: LegacyInstall, base: InitOptions) -> InitOptions:
    """The init options a migration uses: units on (Linux decides), the
    Actions runner's unit re-rendered when the runner is there."""
    return InitOptions(
        systemd=True,
        runner_dir=legacy.actions_runner,
        sbx=base.sbx,
        sbx_version=base.sbx_version,
        version=base.version,
        wheels=base.wheels,
        force=base.force,
        dry_run=base.dry_run,
        preset=base.preset,
        created_by="sbxloop init --migrate",
    )


def open_check(path: Path) -> None:
    """Fail closed on a database SQLite cannot open."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA schema_version").fetchone()
        conn.close()
    except sqlite3.Error as exc:
        raise MigrateError(f"{path} is not a usable SQLite database: {exc}") from exc


__all__ = [
    "DAEMON_UNIT",
    "HomeMigration",
    "LegacyInstall",
    "MigrateError",
    "MigrateOptions",
    "MigrateReport",
    "discover",
    "migrate_options_for",
    "open_check",
]
