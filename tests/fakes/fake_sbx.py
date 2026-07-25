"""A fake sbx CLI for tests.

Executed as its own process via a shim on PATH. Pure stdlib. State lives
under $SBX_FAKE_DIR:

- ``invocations.jsonl`` — every call: {"args": [...], "stdin": ..., "ts": ...}
  (args recorded after stripping the global ``--app-name`` flag)
- ``responses.json``    — scripted overrides: [{"prefix": "exec", "returncode":
  1, "stdout": "", "stderr": "", "once": true}]; first match wins
- ``sandboxes/<name>/fs`` — the sandbox "filesystem". ``cp`` really copies and
  ``exec`` really runs commands, with absolute path arguments rewritten into
  the fs root (chroot by convention) so real worker processes can run in tests
- ``policies.jsonl``, ``secrets.jsonl`` — recorded policy/secret mutations

Exit codes mimic sbx: 0 on success, 1 with "not found" stderr for unknown
sandboxes, 2 for usage errors.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION_OUTPUT = "sbx version 0.35.0\n"

LS_COLUMNS = ("NAME", "AGENT", "STATUS", "WORKSPACE")


def state_dir() -> Path:
    raw = os.environ.get("SBX_FAKE_DIR")
    if not raw:
        print("fake sbx: SBX_FAKE_DIR not set", file=sys.stderr)
        sys.exit(70)
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def strip_app_name(argv: list[str]) -> list[str]:
    args: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--app-name":
            i += 2
            continue
        if arg.startswith("--app-name="):
            i += 1
            continue
        args.append(arg)
        i += 1
    return args


def scripted_response(root: Path, args: list[str]) -> dict[str, object] | None:
    path = root / "responses.json"
    if not path.is_file():
        return None
    responses = json.loads(path.read_text())
    joined = " ".join(args)
    for i, response in enumerate(responses):
        if joined.startswith(str(response.get("prefix", ""))):
            if response.get("once"):
                del responses[i]
                path.write_text(json.dumps(responses))
            return dict(response)
    return None


def sandbox_dir(root: Path, name: str) -> Path:
    return root / "sandboxes" / name


def require_sandbox(root: Path, name: str) -> Path:
    path = sandbox_dir(root, name)
    if not (path / "meta.json").is_file():
        print(f'Error: sandbox "{name}" not found', file=sys.stderr)
        sys.exit(1)
    return path


# Sandbox-canonical path prefixes only. Deliberately narrow: on Linux CI the
# host checkout, interpreter, and pytest tmp dirs live under /home/runner and
# /tmp, so rewriting all of /home and /tmp would clobber legitimate host
# paths in worker argv (this broke CI once — keep it narrow). /workspace is
# the fake's model of the sbx workspace mount (a symlink to the real host
# workspace dir, created by cmd_create).
_SANDBOX_ROOTS = re.compile(
    r"(^|[\s='\"(:])(/(?:home/agent|etc/sandbox|tmp/sbxloop|workspace)(?=[/._\-\s]|$))"
)


def rewrite_abs(fs: Path, arg: str) -> str:
    """Map sandbox-canonical paths onto the fake fs root.

    Rewrites /home/agent, /etc/sandbox*, /tmp/sbxloop*, and /workspace — as
    whole args and embedded in shell strings, so both ``exec box
    /home/agent/x`` and ``exec box sh -c 'cat /home/agent/x'`` hit the fake
    filesystem. All other absolute paths (host binaries, host tmp files) are
    left alone. Args may mix already-host fs paths with canonical ones (a
    ``--cwd <fs>/workspace`` next to ``--job /home/agent/...``): the
    separator requirement in the regex keeps already-rewritten paths stable,
    so rewriting is idempotent without a whole-arg guard.
    """
    if arg.startswith("~/"):
        return str(fs / "home/agent" / arg[2:])
    return _SANDBOX_ROOTS.sub(lambda m: m.group(1) + str(fs) + m.group(2), arg)


def remote_to_host(fs: Path, path: str) -> Path:
    """Map a SANDBOX:PATH remote reference into the fake fs unconditionally
    (remote paths are always sandbox paths, whatever their prefix)."""
    if path.startswith("~/"):
        return fs / "home/agent" / path[2:]
    if str(fs) in path:
        return Path(path)
    return Path(str(fs) + (path if path.startswith("/") else f"/{path}"))


def template_dir(root: Path, ref: str) -> Path:
    return root / "templates" / re.sub(r"[^A-Za-z0-9._-]", "_", ref)


def cmd_template(root: Path, args: list[str]) -> int:
    """Model `sbx template save/ls`: save snapshots a sandbox's fs so a
    later `create --template <ref>` starts from that filesystem."""
    if args[:1] == ["save"]:
        if len(args) != 3:
            print("usage: sbx template save SANDBOX REF", file=sys.stderr)
            return 2
        _, name, ref = args
        path = require_sandbox(root, name)
        target = template_dir(root, ref)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(path / "fs", target / "fs", symlinks=True)
        (target / "ref").write_text(ref)
        return 0
    if args[:1] == ["ls"]:
        print("REPOSITORY  TAG")
        templates = root / "templates"
        if templates.is_dir():
            for path in sorted(templates.iterdir()):
                ref = (path / "ref").read_text()
                repo, _, tag = ref.partition(":")
                print(f"{repo}  {tag or 'latest'}")
        return 0
    print(f"fake sbx: unknown template subcommand {args!r}", file=sys.stderr)
    return 2


def cmd_create(root: Path, args: list[str]) -> int:
    name = None
    template = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--name="):
            name = arg.split("=", 1)[1]
        elif arg == "--name":
            i += 1
            name = args[i]
        elif arg == "--template":
            i += 1
            template = args[i]
        elif arg.startswith("--template="):
            template = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    if name is None or len(rest) != 2:
        print("usage: sbx create --name=NAME [--template T] AGENT WORKSPACE", file=sys.stderr)
        return 2
    agent, workspace = rest
    path = sandbox_dir(root, name)
    if (path / "meta.json").is_file():
        print(f'Error: sandbox "{name}" already exists', file=sys.stderr)
        return 1
    fs = path / "fs"
    saved = template_dir(root, template) if template else None
    if saved is not None and (saved / "fs").is_dir():
        # Seed the new sandbox from the saved template snapshot.
        shutil.copytree(saved / "fs", fs, symlinks=True)
    (fs / "home/agent").mkdir(parents=True, exist_ok=True)
    (fs / "etc").mkdir(parents=True, exist_ok=True)
    # Model the sbx workspace mount: /workspace inside the sandbox is the
    # host workspace directory (symlink — writes propagate live, exactly
    # like a real mount). SBX_FAKE_NO_MOUNT disables it so tests can force
    # discovery failure / harvest mode.
    if not os.environ.get("SBX_FAKE_NO_MOUNT") and Path(workspace).is_dir():
        # A template snapshot may carry the bake sandbox's stale mount link.
        if (fs / "workspace").is_symlink():
            (fs / "workspace").unlink()
        (fs / "workspace").symlink_to(workspace)
    meta = {"agent": agent, "workspace": workspace, "template": template, "status": "running"}
    (path / "meta.json").write_text(json.dumps(meta))
    return 0


def cmd_exec(root: Path, args: list[str]) -> int:
    args = [a for a in args if a not in ("-it", "-i", "-t")]
    if not args:
        print("usage: sbx exec SANDBOX CMD...", file=sys.stderr)
        return 2
    name, *cmd = args
    path = require_sandbox(root, name)
    meta = json.loads((path / "meta.json").read_text())
    if meta.get("status") != "running":
        print(f'Error: sandbox "{name}" is not running', file=sys.stderr)
        return 1
    if not cmd:
        print("usage: sbx exec SANDBOX CMD...", file=sys.stderr)
        return 2
    fs = path / "fs"
    rewritten = [rewrite_abs(fs, c) for c in cmd]
    home = fs / "home/agent"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SBX_FAKE_FS"] = str(fs)
    try:
        proc = subprocess.run(rewritten, cwd=home, env=env, check=False)
    except FileNotFoundError:
        # Model field-observed sbx behavior: exec launch errors (missing
        # binary) surface on STDOUT, not stderr.
        print(f'Error: exec failed: executable "{rewritten[0]}" was not located in the sandbox')
        return 1
    return proc.returncode


def parse_remote(root: Path, ref: str) -> Path | None:
    """SANDBOX:PATH -> host path inside the fake fs, else None."""
    if ":" not in ref:
        return None
    name, remote = ref.split(":", 1)
    path = require_sandbox(root, name)
    return remote_to_host(path / "fs", remote)


def cmd_cp(root: Path, args: list[str]) -> int:
    if len(args) != 2:
        print("usage: sbx cp SRC DST", file=sys.stderr)
        return 2
    src_ref, dst_ref = args
    src_remote = parse_remote(root, src_ref)
    dst_remote = parse_remote(root, dst_ref)
    if (src_remote is None) == (dst_remote is None):
        print("Error: exactly one side must be SANDBOX:PATH", file=sys.stderr)
        return 2
    src = src_remote or Path(src_ref)
    dst = dst_remote or Path(dst_ref)
    if not src.exists():
        print(f'Error: source path "{src_ref}" not found', file=sys.stderr)
        return 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return 0


def cmd_ls(root: Path) -> int:
    rows = []
    boxes = root / "sandboxes"
    if boxes.is_dir():
        for path in sorted(boxes.iterdir()):
            meta_path = path / "meta.json"
            if not meta_path.is_file():
                continue
            meta = json.loads(meta_path.read_text())
            rows.append(
                (
                    path.name,
                    meta.get("agent", ""),
                    meta.get("status", ""),
                    meta.get("workspace", ""),
                )
            )
    widths = [
        max([len(col)] + [len(str(row[i])) for row in rows]) + 2 for i, col in enumerate(LS_COLUMNS)
    ]
    print("".join(col.ljust(widths[i]) for i, col in enumerate(LS_COLUMNS)).rstrip())
    for row in rows:
        print("".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def cmd_stop(root: Path, args: list[str]) -> int:
    (name,) = args
    path = require_sandbox(root, name)
    meta = json.loads((path / "meta.json").read_text())
    meta["status"] = "stopped"
    (path / "meta.json").write_text(json.dumps(meta))
    return 0


def cmd_rm(root: Path, args: list[str]) -> int:
    names = [a for a in args if not a.startswith("-")]
    if len(names) != 1:
        print("usage: sbx rm [--force] SANDBOX", file=sys.stderr)
        return 2
    path = require_sandbox(root, names[0])
    shutil.rmtree(path)
    return 0


def cmd_policy(root: Path, args: list[str]) -> int:
    append_jsonl(root / "policies.jsonl", {"args": args, "ts": time.time()})
    if args[:1] == ["check"]:
        print("allowed")
    elif args[:1] == ["ls"]:
        print("RULE  DECISION\n(fake)  allow")
    return 0


def _parse_flags(args: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--"):
            flags[args[i][2:]] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        else:
            positional.append(args[i])
            i += 1
    return positional, flags


def cmd_secret(root: Path, args: list[str], stdin: str) -> int:
    """Stateful secrets, mimicking real sbx: refuses to overwrite an existing
    secret ("secret exists"), supports rm. Custom secrets are keyed globally
    by host+env (matching observed sbx behavior); service secrets per scope."""
    append_jsonl(root / "secrets.jsonl", {"args": args, "stdin": stdin, "ts": time.time()})
    state_path = root / "secrets-state.json"
    state: dict[str, dict[str, str]] = (
        json.loads(state_path.read_text())
        if state_path.is_file()
        else {"service": {}, "custom": {}}
    )
    sub, *rest = args
    positional, flags = _parse_flags(rest)
    code = 0
    if sub == "set":
        scope, service = positional[0], positional[1]
        key = f"{scope}|{service}"
        if key in state["service"]:
            print(f'Error: cannot set secret in "{scope}": secret exists', file=sys.stderr)
            code = 1
        else:
            state["service"][key] = stdin
    elif sub == "set-custom":
        scope = positional[0]
        env = flags["env"]
        if env in state["custom"]:
            owner = state["custom"][env]["scope"]
            print(
                f'ERROR: custom secret env "{env}" already exists in scope '
                f"{owner} with placeholder sbx-cs-abc123.",
                file=sys.stderr,
            )
            code = 1
        else:
            state["custom"][env] = {
                "scope": scope,
                "host": flags.get("host", ""),
                "value": flags.get("value", ""),
            }
    elif sub == "ls":
        # The real `sbx secret ls` output format is unverified; this shape is
        # speculative on purpose — the production parser is token-tolerant.
        print("SCOPE  TYPE  NAME  HOST")
        for env, entry in state["custom"].items():
            print(f"{entry['scope']}  custom  {env}  {entry['host']}")
        for key in state["service"]:
            svc_scope, service = key.split("|", 1)
            print(f"{svc_scope}  service  {service}  -")
    elif sub == "rm":
        scope = positional[0]
        if "env" in flags:
            env = flags["env"]
            entry = state["custom"].get(env)
            if entry is None or entry["scope"] != scope:
                print("Error: secret not found", file=sys.stderr)
                code = 1
            else:
                del state["custom"][env]
        else:
            key = f"{scope}|{positional[1]}"
            if state["service"].pop(key, None) is None:
                print("Error: secret not found", file=sys.stderr)
                code = 1
    state_path.write_text(json.dumps(state))
    return code


def main() -> int:
    root = state_dir()
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    args = strip_app_name(sys.argv[1:])
    append_jsonl(root / "invocations.jsonl", {"args": args, "stdin": stdin, "ts": time.time()})

    scripted = scripted_response(root, args)
    if scripted is not None:
        sys.stdout.write(str(scripted.get("stdout", "")))
        sys.stderr.write(str(scripted.get("stderr", "")))
        return int(scripted.get("returncode", 0))  # type: ignore[call-overload]

    if not args:
        print("usage: sbx COMMAND", file=sys.stderr)
        return 2
    command, *rest = args
    if command in ("--help", "help"):
        print(
            "Usage: sbx [OPTIONS] COMMAND\n\nCommands:\n"
            "  create  exec  cp  ls  stop  rm  policy  secret  version  login"
        )
        return 0
    if command == "create":
        return cmd_create(root, rest)
    if command == "exec":
        return cmd_exec(root, rest)
    if command == "cp":
        return cmd_cp(root, rest)
    if command == "ls":
        return cmd_ls(root)
    if command == "stop":
        return cmd_stop(root, rest)
    if command == "rm":
        return cmd_rm(root, rest)
    if command == "policy":
        return cmd_policy(root, rest)
    if command == "template":
        return cmd_template(root, rest)
    if command == "secret":
        return cmd_secret(root, rest, stdin)
    if command == "version":
        sys.stdout.write(VERSION_OUTPUT)
        return 0
    print(f"fake sbx: unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
