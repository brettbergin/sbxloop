"""WorkerClient: install the worker into a sandbox and run jobs through it.

Two transports:

- **stream** (default): one blocking ``sbx exec`` per job; the worker mirrors
  its JSONL events to stdout, which the host parses line-by-line and
  republishes on the EventBus. The result file is fetched afterwards with
  ``cp`` — stdout is telemetry, the result file is the outcome.
- **poll**: the worker is launched detached (``nohup ... &``); the host tails
  the in-sandbox events file by byte offset every ``poll_interval`` seconds.
  Fallback for environments where long-running exec streams are unreliable.

Host-side timeouts are ``job.timeout_s`` plus a grace period; on expiry the
worker process is killed inside the sandbox (pattern-scoped pkill) and
WorkerTimeoutError is raised.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import contextlib
import json
import queue
import shlex
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

import sbxloop
from sbxloop import toolchains
from sbxloop.config import Limits, WorkerTransport
from sbxloop.errors import SbxError, WorkerError, WorkerTimeoutError
from sbxloop.events import EventBus
from sbxloop.log import get_logger
from sbxloop.sbx.models import ExecResult
from sbxloop.sbx.sandbox import (
    BAKE_MANIFEST,
    ENV_FILE,
    EVENTS_DIR,
    JOBS_DIR,
    RESULTS_DIR,
    TOOLS_DIR,
    VENV_DIR,
    Sandbox,
)
from sbxloop.sbx.sandbox import VENV_PYTHON as DEFAULT_PYTHON
from sbxloop.worker.hosttools import HostToolBroker, HostToolHandler
from sbxloop.worker.wheel import resolve_worker_wheel
from sbxloop_worker.protocol import Event, EventTypes, JobRequest, JobResult

# Wheels must keep their canonical filename when staged: pip validates the
# name-version-python-abi-platform structure of the FILENAME itself and
# refuses to install a renamed wheel ("Invalid wheel filename").
STAGED_WHEEL_DIR = "/tmp"  # nosec B108 - path inside the sandbox VM, not host tmp

_SMOKE_BASE = "/tmp/sbxloop-smoke"  # nosec B108 - path inside the sandbox VM, not host tmp

# One in-sandbox orchestrator for the whole prebake verification: manifest
# read+parse, import/version check, and entrypoint smoke — each formerly its
# own `sbx exec` round trip (#127). Runs under the template's system python3
# (templates ship it; a template without it fails the probe and degrades to
# the install ladder, same as any other probe failure). argv:
# manifest_path expected_version default_python smoke_base. Emits exactly one
# JSON verdict line on stdout; the host maps stages onto the same decisions
# and log messages the serial probes produced. The "ok" verdict also reports
# whether the baseline tooling (git, #252) is on PATH: a template baked before
# git joined the baseline passes every worker check and would otherwise skip
# the ensure that installs it, so the host tops it up from this one answer
# instead of paying a separate probe round trip.
_PREBAKE_PROBE = """\
import json, shutil, subprocess, sys

manifest_path, expected, default_python, smoke_base = sys.argv[1:5]


def emit(stage, **extra):
    print(json.dumps({"stage": stage, **extra}))
    sys.exit(0)


def run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return None


def tail(proc):
    if proc is None:
        return "interpreter not found"
    parts = [p.strip() for p in (proc.stderr, proc.stdout) if p and p.strip()]
    return "\\n".join(parts)[-2000:] or "(no output)"


try:
    with open(manifest_path) as f:
        raw = f.read()
except OSError:
    emit("no-manifest")
try:
    manifest = json.loads(raw)
    baked = str(manifest["worker_version"])
    python = str(manifest.get("python") or default_python)
except (ValueError, KeyError, TypeError):
    emit("bad-manifest")
if baked != expected:
    emit("stale", baked=baked)
check = run([python, "-c", "import sbxloop_worker; print(sbxloop_worker.__version__)"])
if check is None or check.returncode != 0 or check.stdout.strip() != expected:
    emit("import-failed", rc=check.returncode if check else -1, output=tail(check))
smoke = run(
    [
        python,
        "-m",
        "sbxloop_worker",
        "run",
        "--job",
        smoke_base + "-missing.json",
        "--events",
        smoke_base + ".events.jsonl",
        "--result",
        smoke_base + ".result.json",
    ]
)
if smoke is None or smoke.returncode != 64:
    emit("smoke-failed", rc=smoke.returncode if smoke else -1, output=tail(smoke))
emit("ok", python=python, git=shutil.which("git") is not None)
"""

log = get_logger(__name__)


def _output_tail(result: ExecResult, limit: int = 2000) -> str:
    """Combined stderr+stdout tail: sbx exec surfaces some in-sandbox errors
    on stdout, so stderr alone can be empty exactly when it matters."""
    combined = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip())
    return combined[-limit:] if combined else "(no output)"


class WorkerClient:
    def __init__(
        self,
        sandbox: Sandbox,
        bus: EventBus | None = None,
        *,
        transport: WorkerTransport = "stream",
        python: str = DEFAULT_PYTHON,
        poll_interval: float = 2.0,
        grace_s: float = 60.0,
        role: str | None = None,
        limits: Limits | None = None,
        credential_refresh: Callable[[], None] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.bus = bus or EventBus()
        self.transport = transport
        self.python = python
        self.poll_interval = poll_interval
        self.grace_s = grace_s
        # Set by install(): True when a prebaked template carried a working
        # worker and the install ladder was skipped entirely.
        self.prebaked = False
        # Baseline tools the prebake probe found absent (see _PREBAKE_PROBE);
        # install() tops them up after a successful verification.
        self._prebake_missing: list[toolchains.Toolchain] = []
        # Sandbox role for enriching resource telemetry (the worker doesn't
        # know which sandbox it lives in), and guardrail thresholds to pass
        # through to the worker's heartbeat sampler.
        self.role = role
        self.limits = limits
        # Invoked before each submit when set (github role under GitHub App
        # auth): re-mints and rewrites the in-VM env file when the
        # installation token nears expiry, so this job's worker process
        # starts with a live credential. See Provisioner.gh_refresher.
        self.credential_refresh = credential_refresh
        # job_id -> agent persona (planner, executor, ...) supplied at
        # submit(); stamped onto that job's agent.* events so the transcript
        # can say who is speaking (the worker doesn't know which phase it
        # serves).
        self._job_agents: dict[str, str] = {}
        # job_id -> the broker answering that job's host-tool requests
        # (see sbxloop.worker.hosttools); registered for the life of submit().
        self._brokers: dict[str, HostToolBroker] = {}

    # -- install -----------------------------------------------------------

    def install(
        self,
        *,
        extras: str = "copilot",
        wheel: Path | None = None,
        timeout: float = 600.0,
        no_deps: bool = False,
        system_site_packages: bool = False,
        ensure_dev_tools: bool = False,
        languages: Sequence[str] = (),
        expect_prebaked: bool = False,
    ) -> None:
        """Install sbxloop-worker into the sandbox, venv-first with fallbacks.

        ``expect_prebaked`` (set when ``[sandbox].template`` is configured)
        first probes for a template baked by ``sbxloop bake``: a bake
        manifest whose worker version matches this host, verified with the
        same entrypoint smoke check the ladder ends with. On success the
        whole ladder is skipped; any verification failure degrades to the
        ladder below, so a stale template costs one probe, never a run.

        Sandbox templates ship python3 but often lack python3-venv
        (Debian/Ubuntu split ensurepip out). The ladder:

        1. ``python3 -m venv`` — the clean path.
        2. On a venv/ensurepip failure: ``sudo -n apt-get install
           python3-venv python3-pip`` (the template's agent user has sudo;
           apt hosts are on the balanced allowlist), then retry the venv.
        3. Still no venv: **user-site fallback** — ``python3 -m pip install
           --user`` (adding ``--break-system-packages`` when pip reports an
           externally-managed environment), and the worker runs under the
           system ``python3``. ``self.python`` is updated so submit() uses
           the right interpreter either way.

        ``no_deps``/``system_site_packages`` are test seams for hermetic
        installs; production uses full dependency resolution (PyPI is
        reachable under the balanced network policy).

        ``ensure_dev_tools`` additionally makes the sandbox dev-ready for
        the AGENT's own work (see _ensure_dev_tools) — the engine sets it
        for the agent sandbox only, passing the configured ``languages``.
        """
        started = time.monotonic()
        log.info(
            "worker.install_start",
            sandbox=self.sandbox.name,
            role=self.role,
            expect_prebaked=expect_prebaked,
            ensure_dev_tools=ensure_dev_tools,
            languages=list(languages) or None,
        )
        if expect_prebaked and self._verify_prebaked():
            self.prebaked = True
            if ensure_dev_tools and self._prebake_missing:
                self._provision_toolchains(self._prebake_missing, timeout)
            log.info(
                "worker.installed",
                sandbox=self.sandbox.name,
                role=self.role,
                prebaked=True,
                duration_s=round(time.monotonic() - started, 1),
            )
            return
        if ensure_dev_tools:
            self._ensure_dev_tools(timeout, languages)
            self._ensure_search_fallback(timeout)
        wheel = wheel if wheel is not None else resolve_worker_wheel()
        if wheel is not None:
            staged = f"{STAGED_WHEEL_DIR}/{wheel.name}"
            self.sandbox.cp_in(wheel, staged)
            base_target = staged
        else:
            base_target = f"sbxloop-worker=={sbxloop.__version__}"
        target = f"{base_target}[{extras}]" if extras else base_target

        if self._create_venv(timeout, system_site_packages):
            self.python = DEFAULT_PYTHON
            pip = [f"{VENV_DIR}/bin/pip", "install", "--quiet"]
            if no_deps:
                pip.append("--no-deps")
            self._check(self.sandbox.exec([*pip, target], timeout=timeout), "worker install")
        else:
            self.python = "python3"
            self._pip_user_install(target, timeout=timeout, no_deps=no_deps)

        verify = self.sandbox.exec(
            [self.python, "-c", "import sbxloop_worker; print(sbxloop_worker.__version__)"]
        )
        self._check(verify, "worker import check")
        installed = verify.stdout.strip()
        if installed != sbxloop.__version__:
            raise WorkerError(
                f"worker version {installed!r} does not match host {sbxloop.__version__!r}"
            )
        log.info(
            "worker.installed",
            sandbox=self.sandbox.name,
            role=self.role,
            prebaked=False,
            python=self.python,
            version=installed,
            duration_s=round(time.monotonic() - started, 1),
        )

        # Entrypoint smoke check: importing the package proves nothing about
        # `python -m sbxloop_worker` actually executing under sbx exec. A run
        # against a missing job file must exit 64 (the worker's usage-error
        # code) — anything else means jobs would die with no result file,
        # so fail HERE with full output instead of at the first real job.
        smoke = self._entrypoint_smoke(self.python)
        if smoke.returncode != 64:
            raise WorkerError(
                "worker entrypoint check failed "
                f"(rc={smoke.returncode}, expected 64): {_output_tail(smoke)}"
            )

    def verify_installed(self) -> bool:
        """Is a matching worker already installed under ``self.python``?

        The two checks the install ladder ends with — version equals the
        host's, entrypoint exits 64 — as a cheap probe for reusing a
        long-lived sandbox (the daemon's concierge box survives restarts;
        a host upgrade must re-install rather than trust it).
        """
        try:
            verify = self.sandbox.exec(
                [self.python, "-c", "import sbxloop_worker; print(sbxloop_worker.__version__)"]
            )
        except SbxError:
            return False
        if not verify.ok or verify.stdout.strip() != sbxloop.__version__:
            return False
        try:
            return self._entrypoint_smoke(self.python).returncode == 64
        except SbxError:
            return False

    def _entrypoint_smoke(self, python: str) -> ExecResult:
        """Run the worker entrypoint against a missing job file; a healthy
        install exits 64 (the worker's usage-error code)."""
        return self.sandbox.exec(
            [
                python,
                "-m",
                "sbxloop_worker",
                "run",
                "--job",
                f"{_SMOKE_BASE}-missing.json",
                "--events",
                f"{_SMOKE_BASE}.events.jsonl",
                "--result",
                f"{_SMOKE_BASE}.result.json",
            ]
        )

    def _verify_prebaked(self) -> bool:
        """Fast prerequisite probe against a prebaked template.

        One ``sbx exec`` runs the whole chain in-sandbox (_PREBAKE_PROBE):
        read the bake manifest ``sbxloop bake`` left in the template, then
        re-run the two checks the install ladder ends with (version match,
        entrypoint exits 64) under the interpreter the bake recorded —
        formerly three exec round trips (#127). Any failure returns False —
        the caller falls back to the install ladder, so a stale or foreign
        template degrades to today's behavior instead of failing the run.
        """
        probe = self.sandbox.exec(
            [
                "python3",
                "-c",
                _PREBAKE_PROBE,
                BAKE_MANIFEST,
                sbxloop.__version__,
                DEFAULT_PYTHON,
                _SMOKE_BASE,
            ]
        )
        verdict: dict[str, object] = {}
        if probe.ok and probe.stdout.strip():
            try:
                parsed = json.loads(probe.stdout.strip().splitlines()[-1])
                if isinstance(parsed, dict):
                    verdict = parsed
            except ValueError:
                pass
        stage = verdict.get("stage")
        fallback = "running the install ladder"
        if stage is None:
            log.warning(
                "worker.prebake_no_verdict",
                sandbox=self.sandbox.name,
                rc=probe.returncode,
                output=_output_tail(probe),
                action=fallback,
            )
            return False
        if stage == "no-manifest":
            log.info(
                "worker.prebake_no_manifest",
                sandbox=self.sandbox.name,
                manifest=BAKE_MANIFEST,
                action=fallback,
            )
            return False
        if stage == "bad-manifest":
            log.warning(
                "worker.prebake_bad_manifest",
                sandbox=self.sandbox.name,
                manifest=BAKE_MANIFEST,
                action=fallback,
            )
            return False
        if stage == "stale":
            log.warning(
                "worker.prebake_stale_template",
                sandbox=self.sandbox.name,
                baked=verdict.get("baked"),
                host=sbxloop.__version__,
                action=fallback,
                hint="re-run `sbxloop bake` to refresh the template",
            )
            return False
        if stage == "import-failed":
            log.warning(
                "worker.prebake_import_failed",
                sandbox=self.sandbox.name,
                rc=verdict.get("rc"),
                output=verdict.get("output") or "(no output)",
                action=fallback,
            )
            return False
        if stage == "smoke-failed":
            log.warning(
                "worker.prebake_smoke_failed",
                sandbox=self.sandbox.name,
                rc=verdict.get("rc"),
                expected_rc=64,
                output=verdict.get("output") or "(no output)",
                action=fallback,
            )
            return False
        python = verdict.get("python")
        if stage != "ok" or not isinstance(python, str) or not python:
            log.warning(
                "worker.prebake_unrecognized_verdict",
                sandbox=self.sandbox.name,
                stage=stage,
                action=fallback,
            )
            return False
        self.python = python
        # Fail closed: anything but an explicit True costs one best-effort
        # apt call, whereas trusting a malformed answer leaves the agent
        # without git for the whole run (#252).
        self._prebake_missing = [] if verdict.get("git") is True else [toolchains.GIT]
        log.info(
            "worker.prebake_verified",
            sandbox=self.sandbox.name,
            version=sbxloop.__version__,
            python=python,
            git=verdict.get("git") is True,
        )
        return True

    def _ensure_dev_tools(self, timeout: float, languages: Sequence[str] = ()) -> None:
        """Best-effort: make the sandbox dev-ready for the agent's own work.

        This provisions the toolchains for ``languages`` (see
        ``sbxloop.toolchains``) before the agent's first turn, so it does not
        burn revision budget bootstrapping its own compiler. Empty selects
        the default, which is Python — the case this ensure was born for.
        ``toolchains.BASELINE_TOOLS`` (git, #252) is provisioned on top of
        whatever was selected: a project's tests shell out to git whatever
        its language, so it is not an opt-in.

        Field failure (0.4.0): templates ship a system python without
        ensurepip. The worker self-heals its OWN venv (the ladder below),
        but when that apt heal silently fails the worker still succeeds via
        the user-site fallback — leaving python3-venv missing, so the
        AGENT's `python3 -m venv` for the project it is building dies with
        "ensurepip is not available" on every revision until the budget
        exhausts. Since #140 that is one entry in a registry rather than the
        only ecosystem with a head start, but the semantics are the ones
        that failure taught us:

        - **Probe first.** A template that already ships a toolchain needs
          no apt and no network at all.
        - **Batch the apt path.** All still-missing apt packages go into one
          ``update && install``, so N selected languages is one round trip.
        - **Never fatal, but loud.** Warn with the toolchain named; worker
          installation has its own ladder and the agent retains
          ``sudo apt-get`` as an escape hatch.
        """
        selected = (
            *toolchains.BASELINE_TOOLS,
            *toolchains.resolve(languages or toolchains.DEFAULT_LANGUAGES),
        )
        missing = [tc for tc in selected if not self.sandbox.exec(["sh", "-c", tc.probe]).ok]
        if not missing:
            log.debug("worker.dev_tools_present", sandbox=self.sandbox.name)
            return
        self._provision_toolchains(missing, timeout)

    def _provision_toolchains(
        self, missing: Sequence[toolchains.Toolchain], timeout: float
    ) -> None:
        """Install already-probed-missing toolchains: one pooled apt call,
        then each entry's install script. Best-effort and loud, see
        ``_ensure_dev_tools``."""
        log.info(
            "worker.toolchains_provisioning",
            sandbox=self.sandbox.name,
            toolchains=[tc.name for tc in missing],
        )
        packages = toolchains.apt_packages(missing)
        if packages:
            apt_for = [tc for tc in missing if tc.apt_packages]
            result = self.sandbox.exec(
                [
                    "sh",
                    "-c",
                    "sudo -n apt-get update -q && "
                    f"sudo -n apt-get install -y -q {' '.join(packages)}",
                ],
                timeout=timeout,
            )
            if not result.ok:
                log.warning(
                    "worker.dev_tools_ensure_failed",
                    sandbox=self.sandbox.name,
                    toolchains=[tc.name for tc in apt_for],
                    rc=result.returncode,
                    wanted="; ".join(tc.wanted for tc in apt_for),
                    output=_output_tail(result),
                    hint="the agent has to bootstrap these itself; rc=100 usually means apt "
                    "could not reach its mirrors — check the sandbox network policy allows "
                    "the Ubuntu/Debian apt hosts",
                )
        for toolchain in missing:
            if toolchain.install_script is None:
                continue
            result = self.sandbox.exec(
                ["sh", "-c", toolchain.install_script],
                timeout=timeout,
            )
            if not result.ok:
                log.warning(
                    "worker.dev_tools_ensure_failed",
                    sandbox=self.sandbox.name,
                    toolchains=[toolchain.name],
                    rc=result.returncode,
                    wanted=toolchain.wanted,
                    hint="the agent has to bootstrap this itself; a blocked installer "
                    "domain is the usual cause — check the sandbox network policy",
                    output=_output_tail(result),
                )

    # The worker reroutes the Copilot CLI's glob/grep tools to a PATH ripgrep
    # on non-4-KiB-page guests (USE_BUILTIN_RIPGREP=false, issue #122); this
    # probe answers whether that reroute would have a binary to land on.
    _SEARCH_FALLBACK_PROBE = 'test "$(getconf PAGESIZE)" = 4096 || command -v rg >/dev/null'

    def _ensure_search_fallback(self, timeout: float) -> None:
        """Best-effort: a PATH ripgrep for non-4-KiB-page guests (issue #122).

        The Copilot CLI's bundled ripgrep is a jemalloc build compiled for
        4 KiB pages; on guests with a larger page size (16 KiB is common
        for Apple-silicon microVMs) it aborts at startup ("<jemalloc>:
        Unsupported system page size") and the agent silently loses its
        search tools. The worker reroutes glob/grep to the system ripgrep
        via ``USE_BUILTIN_RIPGREP=false`` on such guests — this ensure
        installs that ripgrep. Probe first: a 4 KiB guest, or one that
        already ships ``rg``, needs no apt and no network at all. Never
        fatal: the worker also warns in the transcript when the reroute
        has no binary to land on.
        """
        probe = self.sandbox.exec(["sh", "-c", self._SEARCH_FALLBACK_PROBE])
        if probe.ok:
            return
        result = self.sandbox.exec(
            [
                "sh",
                "-c",
                "sudo -n apt-get update -q && sudo -n apt-get install -y -q ripgrep",
            ],
            timeout=timeout,
        )
        if not result.ok:
            log.warning(
                "worker.search_fallback_ensure_failed",
                sandbox=self.sandbox.name,
                rc=result.returncode,
                output=_output_tail(result),
                hint="this guest's page size is not 4096 and no system ripgrep could be "
                "installed, so the agent's glob/grep tools will abort (jemalloc "
                "'Unsupported system page size')",
            )

    def _create_venv(self, timeout: float, system_site_packages: bool) -> bool:
        venv_cmd = ["python3", "-m", "venv"]
        if system_site_packages:
            venv_cmd.append("--system-site-packages")
        venv_cmd.append(VENV_DIR)

        result = self.sandbox.exec(venv_cmd, timeout=timeout)
        if result.ok:
            return True
        output = f"{result.stdout} {result.stderr}".lower()
        if "ensurepip" in output or "venv" in output:
            # Self-heal: the official templates run Ubuntu with a sudo-capable
            # agent user, and apt hosts are on the balanced allowlist.
            self.sandbox.exec(
                [
                    "sh",
                    "-c",
                    "sudo -n apt-get update -q && "
                    "sudo -n apt-get install -y -q python3-venv python3-pip",
                ],
                timeout=timeout,
            )
            result = self.sandbox.exec(venv_cmd, timeout=timeout)
            if result.ok:
                return True
        log.warning(
            "worker.venv_failed",
            sandbox=self.sandbox.name,
            rc=result.returncode,
            output=_output_tail(result),
            action="falling back to a user-site install with the system python3",
        )
        return False

    def _pip_user_install(self, target: str, *, timeout: float, no_deps: bool) -> None:
        pip = ["python3", "-m", "pip", "install", "--quiet", "--user"]
        if no_deps:
            pip.append("--no-deps")
        result = self.sandbox.exec([*pip, target], timeout=timeout)
        if not result.ok and "externally-managed" in f"{result.stdout} {result.stderr}".lower():
            # PEP 668 (Ubuntu 24.04+): system pip refuses --user without an
            # explicit opt-out.
            result = self.sandbox.exec([*pip, "--break-system-packages", target], timeout=timeout)
        self._check(result, "worker install (user-site fallback)")

    @staticmethod
    def _check(result: ExecResult, step: str) -> None:
        if not result.ok:
            raise WorkerError(f"{step} failed (rc={result.returncode}): {_output_tail(result)}")

    # -- submit ------------------------------------------------------------

    def submit(
        self,
        job: JobRequest,
        *,
        agent: str | None = None,
        tool_handler: HostToolHandler | None = None,
    ) -> JobResult:
        """Run one job to completion.

        ``tool_handler`` answers the job's host-tool calls (``job.host_tools``)
        from a host thread pool while the session runs; the two must travel
        together — a tool the model can call but nobody answers would only
        time out. ``host_tools_dir`` is filled in here (per-job directory
        under TOOLS_DIR) unless the caller set it.
        """
        if self.credential_refresh is not None:
            self.credential_refresh()
        if bool(job.host_tools) != (tool_handler is not None):
            raise WorkerError(
                "job.host_tools and tool_handler must be given together "
                f"(host_tools={len(job.host_tools)}, handler={tool_handler is not None})"
            )
        if tool_handler is not None:
            if job.host_tools_dir is None:
                job = job.model_copy(update={"host_tools_dir": f"{TOOLS_DIR}/{job.job_id}"})
            broker = HostToolBroker(self.sandbox, job, tool_handler)
            self._brokers[job.job_id] = broker
            try:
                return self._submit_as(job, agent)
            finally:
                self._brokers.pop(job.job_id, None)
                broker.close()
        return self._submit_as(job, agent)

    def _submit_as(self, job: JobRequest, agent: str | None) -> JobResult:
        if agent is not None:
            self._job_agents[job.job_id] = agent
        try:
            return self._submit(job)
        finally:
            self._job_agents.pop(job.job_id, None)

    def _submit(self, job: JobRequest) -> JobResult:
        job_path = f"{JOBS_DIR}/{job.job_id}.json"
        events_path = f"{EVENTS_DIR}/{job.job_id}.jsonl"
        result_path = f"{RESULTS_DIR}/{job.job_id}.json"
        self.sandbox.write_text(job_path, job.model_dump_json())

        argv = [
            self.python,
            "-m",
            "sbxloop_worker",
            "run",
            "--job",
            job_path,
            "--events",
            events_path,
            "--result",
            result_path,
            "--env-file",
            ENV_FILE,
        ]
        # cwd travels on argv (not only in the job JSON) so the worker
        # process itself chdirs there — agent SDK sessions inherit it.
        if job.cwd:
            argv += ["--cwd", job.cwd]
        if job.host_tools_dir:
            argv += ["--tools-dir", job.host_tools_dir]
        if self.limits is not None:
            argv += [
                "--disk-warn",
                str(self.limits.disk_warn),
                "--disk-abort",
                str(self.limits.disk_abort),
                "--mem-warn",
                str(self.limits.mem_warn),
                "--mem-abort",
                str(self.limits.mem_abort),
            ]
        # sbx injects secrets through the sandbox session/profile machinery;
        # a bare exec'd process may not see them. Run the worker under a
        # login shell so the sandbox environment is fully loaded.
        wrapped = ["sh", "-lc", shlex.join(argv)]
        deadline = time.monotonic() + job.timeout_s + self.grace_s
        started = time.monotonic()
        log.info(
            "worker.job_submit",
            job=job.job_id,
            kind=job.kind,
            sandbox=self.sandbox.name,
            role=self.role,
            transport=self.transport,
            timeout_s=job.timeout_s,
            cwd=job.cwd,
        )
        if self.transport == "poll":
            self._run_poll(job, wrapped, events_path, result_path, deadline)
            diagnostics = ""
        else:
            diagnostics = self._run_stream(job, wrapped, deadline)
        result = self._fetch_result(job, result_path, events_path, diagnostics)
        log.info(
            "worker.job_done",
            job=job.job_id,
            kind=job.kind,
            sandbox=self.sandbox.name,
            status=result.status,
            error=result.error.message[:200] if result.error is not None else None,
            exit_code=result.exit_code,
            duration_s=round(time.monotonic() - started, 1),
        )
        return result

    # -- stream transport --------------------------------------------------

    def _run_stream(self, job: JobRequest, argv: list[str], deadline: float) -> str:
        """Run the worker via a blocking exec; returns diagnostics (exit code
        + stderr tail) for the no-result failure path."""
        proc = self.sandbox.exec_stream(argv)
        lines: queue.Queue[str | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=50)

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def err_reader() -> None:
            # stderr must be drained: an unread PIPE deadlocks a chatty
            # worker once the 64KB buffer fills — and its content is the
            # only clue when the process dies before writing a result.
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())

        threading.Thread(target=reader, name="sbxloop-stream-reader", daemon=True).start()
        threading.Thread(target=err_reader, name="sbxloop-stderr-reader", daemon=True).start()

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "worker.job_timeout",
                        job=job.job_id,
                        sandbox=self.sandbox.name,
                        transport="stream",
                        timeout_s=job.timeout_s,
                        grace_s=self.grace_s,
                    )
                    self._kill(job, proc)
                    raise WorkerTimeoutError(
                        f"job {job.job_id} exceeded {job.timeout_s}s (+{self.grace_s}s grace)"
                    )
                try:
                    line = lines.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    break
                self._handle_line(job, line)
        finally:
            with contextlib.suppress(Exception):
                proc.wait(timeout=self.grace_s)
        parts = [f"exec rc={proc.returncode}"]
        if stderr_tail:
            parts.append("stderr: " + " | ".join(stderr_tail)[-1500:])
        return "; ".join(parts)

    def _handle_line(self, job: JobRequest, line: str) -> Event | None:
        """Publish one stdout/events line; returns the parsed Event (None for
        blank or non-event lines) so callers can act on event types without
        re-parsing or substring-matching the raw line."""
        line = line.strip()
        if not line:
            return None
        try:
            event = Event.from_json_line(line)
        except ValueError:
            self.bus.publish(
                Event.now(EventTypes.WORKER_STDOUT, job.run_id, job_id=job.job_id, line=line)
            )
            return None
        if self.role is not None and event.type in (
            EventTypes.SANDBOX_RESOURCES,
            EventTypes.SANDBOX_RESOURCES_WARNING,
        ):
            event.data.setdefault("role", self.role)
        agent = self._job_agents.get(job.job_id)
        if agent is not None and event.type.startswith("agent."):
            event.data.setdefault("agent", agent)
        self.bus.publish(event)
        if event.type == EventTypes.AGENT_TOOL_REQUEST:
            broker = self._brokers.get(job.job_id)
            if broker is not None:
                broker.dispatch(event)
        return event

    # -- poll transport ----------------------------------------------------

    def _run_poll(
        self,
        job: JobRequest,
        argv: list[str],
        events_path: str,
        result_path: str,
        deadline: float,
    ) -> None:
        quoted = shlex.join(argv)
        launch = self.sandbox.exec(["sh", "-c", f"nohup {quoted} >/dev/null 2>&1 & echo $!"])
        if not launch.ok:
            log.warning(
                "worker.launch_failed",
                job=job.job_id,
                sandbox=self.sandbox.name,
                rc=launch.returncode,
                stderr=launch.stderr.strip()[:500],
            )
            raise WorkerError(f"failed to launch worker: {launch.stderr.strip()[:2000]}")
        pid = launch.stdout.strip().splitlines()[-1] if launch.stdout.strip() else ""
        log.debug(
            "worker.launched",
            job=job.job_id,
            sandbox=self.sandbox.name,
            pid=pid or None,
            poll_interval_s=self.poll_interval,
        )

        drain = _PollDrain(self, job, events_path).drain

        while True:
            if time.monotonic() > deadline:
                log.warning(
                    "worker.job_timeout",
                    job=job.job_id,
                    sandbox=self.sandbox.name,
                    transport="poll",
                    timeout_s=job.timeout_s,
                    grace_s=self.grace_s,
                )
                self._kill(job, None)
                raise WorkerTimeoutError(
                    f"job {job.job_id} exceeded {job.timeout_s}s (+{self.grace_s}s grace)"
                )
            time.sleep(self.poll_interval)
            if drain():
                break
            if pid:
                alive = self.sandbox.exec(
                    ["sh", "-c", f"kill -0 {pid} 2>/dev/null && echo alive || echo dead"]
                )
                if "dead" in alive.stdout:
                    # Worker exited between polls: drain whatever remains.
                    log.debug("worker.exited", job=job.job_id, pid=pid)
                    drain()
                    break

    # -- helpers -----------------------------------------------------------

    def _kill(self, job: JobRequest, proc: object) -> None:
        # Pattern is job-id scoped so concurrent workers are never collateral.
        log.warning("worker.kill", job=job.job_id, sandbox=self.sandbox.name)
        try:
            self.sandbox.exec(["pkill", "-f", f"sbxloop_worker.*{job.job_id}"])
        except Exception:
            log.warning("worker.kill_failed", job=job.job_id, exc_info=True)
        if proc is not None:
            try:
                proc.kill()  # type: ignore[attr-defined]
            except Exception:
                log.debug("worker.kill_proc_failed", job=job.job_id, exc_info=True)

    def _events_tail(self, events_path: str, lines: int = 5) -> str:
        if not events_path:
            return ""
        with contextlib.suppress(Exception):
            result = self.sandbox.exec(
                ["sh", "-c", f"tail -n {lines} {events_path} 2>/dev/null || true"]
            )
            return result.stdout.strip().replace("\n", " | ")[-1500:]
        return ""

    def _fetch_result(
        self,
        job: JobRequest,
        result_path: str,
        events_path: str = "",
        diagnostics: str = "",
    ) -> JobResult:
        try:
            raw = self.sandbox.read_text(result_path)
        except SbxError as exc:
            detail = [f"worker for job {job.job_id} produced no result file ({result_path})"]
            if diagnostics:
                detail.append(diagnostics)
            tail = self._events_tail(events_path)
            if tail:
                detail.append(f"last events: {tail}")
            raise WorkerError("; ".join(detail)) from exc
        try:
            result = JobResult.model_validate_json(raw)
        except ValueError as exc:
            raise WorkerError(f"invalid result file for job {job.job_id}: {exc}") from exc
        if result.job_id != job.job_id:
            raise WorkerError(f"result job_id mismatch: expected {job.job_id}, got {result.job_id}")
        return result


class _PollDrain:
    """Byte-offset tail reader over the in-sandbox events file.

    ``tail -c`` offsets are raw bytes with no character alignment, so each
    chunk is fetched base64-encoded — binary-safe through the text-mode exec
    (no newline translation or decode errors can perturb the byte count) —
    and the offset advances by decoded byte length. A split multibyte UTF-8
    character is held by the incremental decoder until the next chunk
    completes it; a split line is held in the partial-line buffer until its
    newline arrives.

    Completion is signalled only by a *parsed* event of type worker.end:
    substring-matching the raw line would false-trigger on an agent message
    that merely mentions the protocol literal.
    """

    def __init__(self, client: WorkerClient, job: JobRequest, events_path: str) -> None:
        self.client = client
        self.job = job
        self.events_path = events_path
        self.offset = 0
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def drain(self) -> bool:
        """Publish any newly completed event lines; True once worker.end is seen."""
        chunk = self.client.sandbox.exec(
            ["sh", "-c", f"tail -c +{self.offset + 1} {self.events_path} 2>/dev/null | base64"]
        )
        try:
            raw = base64.b64decode(chunk.stdout) if chunk.stdout else b""
        except binascii.Error:
            log.warning(
                "worker.poll_chunk_undecodable", events=self.events_path, action="retry next poll"
            )
            return False
        if not raw:
            return False
        self.offset += len(raw)
        finished = False
        *complete, self.buffer = (self.buffer + self.decoder.decode(raw)).split("\n")
        for line in complete:
            event = self.client._handle_line(self.job, line)
            if event is not None and event.type == EventTypes.WORKER_END:
                finished = True
        return finished
