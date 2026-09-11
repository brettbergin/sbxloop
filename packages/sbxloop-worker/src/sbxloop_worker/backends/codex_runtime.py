"""Pinned Codex SDK runtime, isolated from an operator's own Codex settings.

The SDK starts its bundled app-server over private stdio pipes. It opens no
listener. Inference credentials are supplied by a login RPC and retained in
memory; the dedicated home retains only the session state needed for resume.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

from sbxloop_worker.backends import BackendUnavailableError
from sbxloop_worker.secrets import is_sbx_sentinel, redact_secrets

SDK_VERSION = "0.147.0"
OPENAI_API_URL = "https://api.openai.com/v1"
ApprovalHandler = Callable[[str, dict[str, Any] | None], dict[str, Any]]

# Native tools do not all pass through an approval callback. Executable
# capabilities must instead be our governed dynamic tools. Model-required
# Code Mode remains pure composition: its V8 context has no filesystem,
# network, process or import API, and every nested tool uses our callback.
# Pinning the SDK/runtime makes this audited set a compatibility boundary.
DISABLED_FEATURES = (
    "apps",
    "plugins",
    "plugin_hooks",
    "hooks",
    "codex_hooks",
    "browser_use",
    "computer_use",
    "image_generation",
    "imagegenext",
    "workspace_dependencies",
    "memories",
    "memory_tool",
    "multi_agent",
    "multi_agent_v2",
    "multi_agent_mode",
    "code_mode",
    "code_mode_only",
    "js_repl",
    "js_repl_tools_only",
    "token_budget",
    "current_time_reminder",
    "deferred_executor",
    "deferred_tool_world_state",
    "default_mode_request_user_input",
    "request_permissions",
    "request_permissions_tool",
    "tool_search",
    "tool_suggest",
    "search_tool",
    "skill_search",
    "goals",
    "shell_tool",
    "view_image",
    "web_search",
    "web_search_cached",
    "web_search_request",
    "standalone_web_search",
)


def ensure_runtime() -> None:
    """Check both pinned packages without starting the runtime or a session."""
    try:
        from codex_cli_bin import bundled_codex_path
        from openai_codex import __version__
        from openai_codex.client import CodexClient  # noqa: F401
    except ImportError as exc:
        raise BackendUnavailableError(
            "the Codex SDK/runtime is not installed; install sbxloop-worker[codex]"
        ) from exc
    if __version__ != SDK_VERSION:
        raise BackendUnavailableError(
            f"Codex SDK {SDK_VERSION} is required for the audited tool contract; "
            "reinstall sbxloop-worker[codex]"
        )
    try:
        runtime_version = metadata.version("openai-codex-cli-bin")
    except metadata.PackageNotFoundError as exc:
        raise BackendUnavailableError("the pinned Codex runtime distribution is missing") from exc
    if runtime_version != SDK_VERSION:
        raise BackendUnavailableError(
            f"Codex runtime {SDK_VERSION} is required for the audited tool contract; "
            "reinstall sbxloop-worker[codex]"
        )
    try:
        binary = Path(bundled_codex_path())
    except (OSError, RuntimeError) as exc:
        raise BackendUnavailableError("the bundled Codex runtime is unavailable") from exc
    if not binary.is_file():
        raise BackendUnavailableError("the bundled Codex executable is missing; re-provision")
    suffix = ".exe" if binary.suffix.lower() == ".exe" else ""
    code_mode_host = binary.with_name(f"codex-code-mode-host{suffix}")
    if not code_mode_host.is_file():
        raise BackendUnavailableError(
            "the bundled codex-code-mode-host executable is missing; "
            "reinstall sbxloop-worker[codex]"
        )


def runtime_overrides(cwd: str) -> tuple[str, ...]:
    """Authoritative startup settings; no credential values occur here."""
    return (
        'model_provider="openai"',
        f"openai_base_url={json.dumps(OPENAI_API_URL)}",
        'forced_login_method="api"',
        'cli_auth_credentials_store="ephemeral"',
        'approval_policy="never"',
        'sandbox_mode="read-only"',
        'web_search="disabled"',
        "project_doc_max_bytes=0",
        "skills.include_instructions=false",
        "skills.bundled.enabled=false",
        "orchestrator.skills.enabled=false",
        "orchestrator.mcp.enabled=false",
        "tools.update_plan.enabled=false",
        "tools.experimental_request_user_input.enabled=false",
        "agents.enabled=false",
        # Model metadata can require Code Mode even when the feature's
        # optional preference is off. Its bundled local stdio host provides
        # pure JavaScript composition of our governed dynamic tools.
        "features.code_mode_host=true",
        "mcp_servers={}",
        f'projects.{json.dumps(cwd)}.trust_level="untrusted"',
        *(f"features.{feature}=false" for feature in DISABLED_FEATURES),
    )


def _unexpected_request(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    raise RuntimeError(f"unexpected Codex server request: {method}")


def _check_runtime_home(runtime_home: Path) -> None:
    """Persist session data, but never reuse executable/configuration sources.

    An earlier agent shell could have written these files. Rejecting them
    avoids relying on empty-table CLI overrides to erase recursive config
    merges. The operator's own Codex home is never used by this adapter.
    """
    forbidden = (
        "config.toml",
        "managed_config.toml",
        "requirements.toml",
        "hooks.json",
        "AGENTS.md",
        "AGENTS.override.md",
        "instructions.md",
        "rules",
        "skills",
        "plugins",
    )
    if runtime_home.is_symlink():
        raise BackendUnavailableError("the dedicated Codex runtime home must not be a symlink")
    for name in forbidden:
        candidate = runtime_home / name
        if candidate.exists() or candidate.is_symlink():
            raise BackendUnavailableError(
                f"the dedicated Codex runtime home contains an unexpected settings source: {name}; "
                "re-provision the agent sandbox"
            )


@contextlib.contextmanager
def authenticated_client(
    cwd: str | None = None,
    *,
    persistent: bool = True,
    timeout_s: float = 30.0,
    approval_handler: ApprovalHandler | None = None,
) -> Iterator[Any]:
    """Yield an authenticated SDK client under one bounded lifetime.

    Worker sessions use a dedicated persistent home inside the agent VM.
    Host-side model discovery passes ``persistent=False`` and leaves no state.
    This function never changes the calling process's environment.
    """
    token = os.environ.get("OPENAI_API_KEY", "")
    if not token or is_sbx_sentinel(token):
        raise BackendUnavailableError(
            "OPENAI_API_KEY is missing or is a secret-proxy placeholder; "
            "supply the inference key through the configured secret strategy"
        )
    ensure_runtime()
    if timeout_s <= 0:
        raise subprocess.TimeoutExpired("codex", timeout_s)
    from openai_codex.client import CodexClient, CodexConfig
    from openai_codex.generated.v2_all import ConfigReadResponse

    with contextlib.ExitStack() as stack:
        if persistent:
            runtime_home = Path.home() / ".sbxloop-codex"
            _check_runtime_home(runtime_home)
            runtime_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            runtime_home = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="sbxloop-codex-"))
            )
        workdir = str(Path(cwd).resolve()) if cwd else str(runtime_home)
        client = CodexClient(
            CodexConfig(
                cwd=workdir,
                env={
                    "CODEX_HOME": str(runtime_home),
                    "CODEX_API_KEY": "",
                    "CODEX_ACCESS_TOKEN": "",  # nosec B105: clear an inherited credential
                    "OPENAI_BASE_URL": OPENAI_API_URL,
                },
                config_overrides=runtime_overrides(workdir),
                client_name="sbxloop_worker",
                client_title="sbxloop worker",
                experimental_api=True,
            ),
            approval_handler=approval_handler or _unexpected_request,
        )
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            # Killing the stdio runtime releases SDK response/notification
            # waits, including startup and login, which have no SDK timeout.
            with contextlib.suppress(Exception):
                client.close()

        timer = threading.Timer(timeout_s, expire)
        timer.daemon = True
        timer.start()
        try:
            client.start()
            # SDK close() is a no-op until startup has created its process.
            # If the watchdog fired earlier, close the late process before
            # an RPC can wait without a watchdog still running.
            if expired.is_set():
                raise subprocess.TimeoutExpired("codex", timeout_s)
            client.initialize()
            # Empty-table overrides merge recursively, so they cannot erase
            # system MCP entries. Read the authoritative resolved settings
            # before a thread can initialize any configured MCP process.
            settings = client.request(
                "config/read",
                {"cwd": workdir, "includeLayers": False},
                response_model=ConfigReadResponse,
            ).config.model_dump(mode="json", by_alias=True)
            if settings.get("mcp_servers", {}) != {}:
                raise RuntimeError(
                    "unexpected MCP configuration in the Codex runtime; "
                    "remove inherited MCP settings or re-provision the agent sandbox"
                )
            client.account_login_start({"type": "apiKey", "apiKey": token})
            yield client
            if expired.is_set():
                raise subprocess.TimeoutExpired("codex", timeout_s)
        except Exception as exc:
            if expired.is_set() or isinstance(exc, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired("codex", timeout_s) from None
            # SDK diagnostics may echo a failed login request. Never allow
            # even a short/unusual key to survive in the worker traceback.
            message = redact_secrets(str(exc).replace(token, "[REDACTED]"))
            raise RuntimeError(message) from None
        finally:
            timer.cancel()
            with contextlib.suppress(Exception):
                client.close()
            timer.join(timeout=3)
