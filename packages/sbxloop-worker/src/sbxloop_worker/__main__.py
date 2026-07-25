"""Worker entry point: ``python -m sbxloop_worker run --job J --events E --result R``.

Exit codes: 0 when a result file was written (including error/timeout
results — the result file is the outcome channel); 64 for usage errors;
70 when the result could not be produced at all.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.runner import JobRunner

DEFAULT_ENV_FILE = Path.home() / ".sbxloop" / "env.sh"
# sbx documents this file as the place persistent sandbox env lives; load
# it too so secrets reach the worker even outside login-shell contexts.
PERSISTENT_ENV_FILE = Path("/etc/sandbox-persistent.sh")

_EXPORT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_env_file(path: Path) -> dict[str, str]:
    """Parse ``export KEY=VALUE`` lines (shell-quoted values supported).

    Used by the plain-env secret strategy: the provisioner writes tokens to
    ``~/.sbxloop/env.sh`` and the worker loads them at startup. Existing
    process environment always wins.
    """
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXPORT_RE.match(line)
        if not match:
            continue
        key, raw = match.groups()
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        loaded[key] = parts[0] if parts else ""
    return loaded


def apply_env_file(path: Path) -> None:
    for key, value in load_env_file(path).items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sbxloop_worker")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one job")
    run.add_argument("--job", required=True, type=Path)
    run.add_argument("--events", required=True, type=Path)
    run.add_argument("--result", required=True, type=Path)
    run.add_argument("--heartbeat", type=float, default=15.0)
    run.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run.add_argument("--cwd", type=Path, default=None)
    # Resource guardrail thresholds (percent used; 0 disables). Sampling on
    # the heartbeat always happens — thresholds only add warn/abort levels.
    run.add_argument("--disk-warn", type=float, default=0.0)
    run.add_argument("--disk-abort", type=float, default=0.0)
    run.add_argument("--mem-warn", type=float, default=0.0)
    args = parser.parse_args(argv)

    apply_env_file(args.env_file)
    apply_env_file(PERSISTENT_ENV_FILE)

    try:
        job = JobRequest.model_validate_json(args.job.read_text())
    except (OSError, ValueError) as exc:
        print(f"sbxloop_worker: invalid job file {args.job}: {exc}", file=sys.stderr)
        return 64

    if args.cwd is not None:
        # Run the whole worker in the job's working directory so agent
        # sessions (which inherit the process cwd) and shell commands both
        # execute in the run workspace. Overwrite job.cwd with the resolved
        # value so shell.check keeps its single code path.
        try:
            os.chdir(args.cwd)
        except OSError as exc:
            print(f"sbxloop_worker: cannot chdir to {args.cwd}: {exc}", file=sys.stderr)
            return 64
        job = job.model_copy(update={"cwd": str(Path.cwd())})

    try:
        JobRunner(
            job,
            events_path=args.events,
            result_path=args.result,
            heartbeat_s=args.heartbeat,
            disk_warn=args.disk_warn,
            disk_abort=args.disk_abort,
            mem_warn=args.mem_warn,
        ).run()
    except BaseException as exc:
        print(f"sbxloop_worker: fatal: {exc}", file=sys.stderr)
        return 70
    return 0


if __name__ == "__main__":
    sys.exit(main())
