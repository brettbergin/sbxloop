"""Typed subprocess wrapper around the sbx binary.

Every invocation injects ``--app-name`` (unless disabled) so sbxloop's
sandboxes, policies, and secrets live in an isolated sbx application state,
invisible to the user's interactive sbx usage.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence

from sbxloop.errors import SbxError, SbxNotFoundError
from sbxloop.sbx.models import ExecResult, SandboxInfo, SandboxSpec
from sbxloop.sbx.parse import parse_ls, parse_version

_NOT_FOUND_MARKERS = ("not found", "no such sandbox", "does not exist", "unknown sandbox")

# Stderr shapes meaning sbx itself failed to run the command (daemon
# unreachable, transport dropped, VM stopped) rather than the command failing
# inside the sandbox. Field-collected (#63) — grow this list from observed
# failures, but keep every entry specific enough that stderr from a
# legitimately-failing inner command (curl to a blocked host, a flaky pip
# download) cannot plausibly match it.
_INFRA_MARKERS = (
    "is not running",  # exec refused: the sandbox exists but is stopped/crashed
    "cannot connect to the",  # daemon unreachable ("Cannot connect to the ... daemon at ...")
    "error during connect",  # docker-family transport failure
    "dial unix",  # daemon socket dial errors
)

# Flags whose values are secrets. ExecResult/SbxError carry argv into error
# messages, logs, and events — secret values must never travel with them.
_SECRET_FLAGS = frozenset({"--value"})
_REDACTED = "***"


def redacted_argv(argv: Sequence[str]) -> list[str]:
    """A copy of argv safe to embed in errors/logs: secret flag values masked."""
    safe = list(argv)
    for i, arg in enumerate(safe):
        if arg in _SECRET_FLAGS and i + 1 < len(safe):
            safe[i + 1] = _REDACTED
        else:
            flag = arg.split("=", 1)[0]
            if "=" in arg and flag in _SECRET_FLAGS:
                safe[i] = f"{flag}={_REDACTED}"
    return safe


class SbxCLI:
    """Blocking, typed access to the sbx CLI."""

    def __init__(
        self,
        binary: str = "sbx",
        app_name: str | None = None,
        default_timeout: float = 120.0,
    ) -> None:
        self.binary = binary
        self.app_name = app_name
        self.default_timeout = default_timeout

    def argv(self, *args: str) -> list[str]:
        prefix = [self.binary]
        if self.app_name:
            prefix += ["--app-name", self.app_name]
        return prefix + list(args)

    def run(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = True,
        stdin: str | None = None,
    ) -> ExecResult:
        argv = self.argv(*args)
        # The real argv reaches the subprocess only; everything observable
        # (ExecResult, exceptions, and therefore logs/events) gets the
        # redacted copy so secret values can never leak through error text.
        safe_argv = redacted_argv(argv)
        started = time.monotonic()
        try:
            # `sbx exec` attaches whatever stdin it inherits (see
            # exec_interactive) — never the caller's TTY here, or background
            # execs steal keystrokes from the run TUI's chat form.
            proc = subprocess.run(  # nosec B603 - list argv, sbx CLI, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.default_timeout,
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SbxNotFoundError(
                f"sbx binary {self.binary!r} not found on PATH — install Docker Sandboxes "
                "(https://docs.docker.com/ai/sandboxes/) and run `sbx login`",
                argv=safe_argv,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SbxError(
                f"sbx invocation timed out after {timeout or self.default_timeout:.0f}s",
                argv=safe_argv,
                stderr=(exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else "",
            ) from exc

        result = ExecResult(
            argv=safe_argv,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=time.monotonic() - started,
        )
        if check and not result.ok:
            raise self._error_for(result)
        return result

    def popen(self, *args: str) -> subprocess.Popen[str]:
        """Start a streaming sbx invocation (stdout piped, line-buffered)."""
        argv = self.argv(*args)
        try:
            return subprocess.Popen(  # nosec B603 - list argv, sbx CLI, no shell
                argv,
                stdin=subprocess.DEVNULL,  # long-lived: must not hold the TTY
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise SbxNotFoundError(
                f"sbx binary {self.binary!r} not found on PATH",
                argv=redacted_argv(argv),
            ) from exc

    def _error_for(self, result: ExecResult) -> SbxError:
        message = f"sbx command failed: {' '.join(result.argv[1:3])}"
        lowered = result.stderr.lower()
        cls = SbxNotFoundError if any(m in lowered for m in _NOT_FOUND_MARKERS) else SbxError
        return cls(
            message,
            argv=result.argv,
            returncode=result.returncode,
            stderr=result.stderr,
        )

    # -- lifecycle ---------------------------------------------------------

    def create(self, spec: SandboxSpec) -> None:
        args = ["create", f"--name={spec.name}"]
        if spec.template:
            args += ["--template", spec.template]
        args += [spec.agent, str(spec.workspace)]
        # Creating a microVM can take a while on first template pull.
        self.run(*args, timeout=600.0)

    def exec(
        self,
        name: str,
        cmd: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        """Run a command inside a sandbox; the inner exit code is returned,
        but recognizable sbx-level failures (missing sandbox, sandbox not
        running, daemon down) raise so callers never mistake infra trouble
        for the command's own answer. Unrecognized sbx failures still come
        back as a nonzero ExecResult — decision points that act on a nonzero
        result must stay conservative about that ambiguity (#63)."""
        result = self.run("exec", name, *cmd, timeout=timeout, check=False)
        if not result.ok:
            lowered = result.stderr.lower()
            if any(m in lowered for m in (*_NOT_FOUND_MARKERS, *_INFRA_MARKERS)):
                raise self._error_for(result)
        return result

    def exec_interactive(self, name: str, cmd: Sequence[str]) -> int:
        """Run a command in a sandbox with the caller's terminal attached
        (stdin/stdout/stderr inherited); returns the command's exit code.

        `sbx exec` documents no -it-style flags; terminal attachment is
        simply inheriting the caller's stdio.
        """
        argv = self.argv("exec", name, *cmd)
        try:
            return subprocess.run(argv, check=False).returncode  # nosec B603 - list argv, no shell
        except FileNotFoundError as exc:
            raise SbxNotFoundError(
                f"sbx binary {self.binary!r} not found on PATH",
                argv=redacted_argv(argv),
            ) from exc

    def cp(self, src: str, dst: str, *, timeout: float | None = None) -> None:
        self.run("cp", src, dst, timeout=timeout)

    def ls(self) -> list[SandboxInfo]:
        return parse_ls(self.run("ls").stdout)

    def stop(self, name: str) -> None:
        self.run("stop", name)

    def rm(self, name: str, *, force: bool = True) -> None:
        args = ["rm"]
        if force:
            args.append("--force")
        self.run(*args, name)

    def version(self) -> str | None:
        return parse_version(self.run("version", check=False).stdout)

    # -- templates ---------------------------------------------------------

    def template_save(self, name: str, ref: str, *, timeout: float = 600.0) -> None:
        """Persist a sandbox's current state as a reusable template image."""
        self.run("template", "save", name, ref, timeout=timeout)

    def template_ls(self) -> str:
        return self.run("template", "ls").stdout

    # -- network policy ----------------------------------------------------

    def policy_allow(self, domain: str, *, sandbox: str | None = None) -> None:
        args = ["policy", "allow", "network", domain]
        if sandbox:
            args += ["--sandbox", sandbox]
        self.run(*args)

    def policy_check(self, host: str, *, sandbox: str | None = None) -> bool:
        """Whether the network policy allows ``host``.

        Returns the policy's answer only: a failed invocation without a
        deny-shaped answer raises SbxError instead of reading as "blocked",
        so infra trouble is never reported as a policy verdict (#63).
        """
        args = ["policy", "check", "network", host]
        if sandbox:
            args += ["--sandbox", sandbox]
        result = self.run(*args, check=False)
        text = f"{result.stdout}\n{result.stderr}".lower()
        denied = any(marker in text for marker in ("deny", "denied", "block"))
        if not result.ok and not denied:
            raise self._error_for(result)
        return result.ok and not denied

    def policy_ls(self) -> str:
        return self.run("policy", "ls").stdout

    def policy_init(self, preset: str) -> None:
        self.run("policy", "init", preset)

    # -- secrets -----------------------------------------------------------

    def secret_set(
        self,
        service: str,
        *,
        sandbox: str | None = None,
        token: str | None = None,
    ) -> None:
        """Attach a built-in secret service (global with ``-g``, else scoped)."""
        scope = [sandbox] if sandbox else ["-g"]
        self.run("secret", "set", *scope, service, stdin=token)

    def secret_set_custom(
        self,
        *,
        host: str,
        env: str,
        value: str,
        sandbox: str | None = None,
    ) -> None:
        scope = [sandbox] if sandbox else ["-g"]
        self.run(
            "secret",
            "set-custom",
            *scope,
            "--host",
            host,
            "--env",
            env,
            "--value",
            value,
        )

    def secret_ls(self) -> ExecResult:
        """``sbx secret ls``, never raising on failure: whether (and how) a
        given sbx build enumerates secrets is unverified, so callers treat a
        non-ok result as "listing unsupported" and fall back to probing."""
        return self.run("secret", "ls", check=False)

    def secret_rm(
        self,
        *,
        service: str | None = None,
        host: str | None = None,
        env: str | None = None,
        sandbox: str | None = None,
    ) -> bool:
        """Best-effort secret removal; returns whether sbx accepted it.

        Used by the replace-on-exists flow: sbx refuses to overwrite an
        existing secret, so provisioning removes and re-sets. Removal syntax
        for custom secrets is not a stable documented API, hence best-effort
        (callers keep the existing value when removal is rejected).
        """
        scope = [sandbox] if sandbox else ["-g"]
        args = ["secret", "rm", *scope]
        if service:
            args.append(service)
        else:
            assert env is not None
            if host:
                args += ["--host", host]
            args += ["--env", env]
        return self.run(*args, check=False).ok
