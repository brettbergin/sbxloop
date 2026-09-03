"""Host-side model listing for the configured agent backend.

`sbxloop list-models` asks the configured backend (#617) which models this
host's credential can use, so `model = "..."` in sbxloop.toml (or
`--model`) can be chosen from real ids instead of guesswork:

- copilot: the github-copilot-sdk — the same SDK agent sessions run on
  inside the sandbox — lists what the authenticated Copilot subscription
  can use. The SDK resolves auth from the environment (COPILOT_GITHUB_TOKEN
  → GH_TOKEN → GITHUB_TOKEN; ./.env is loaded by the CLI callback). It is
  optional host-side (the worker's `[copilot]` extra), so the import is
  deferred and its absence surfaces as an actionable error instead of a
  broken CLI.
- claude: the Anthropic Models API (`GET /v1/models`, paginated by
  `after_id`) with ANTHROPIC_API_KEY, over the stdlib — no SDK needed on
  the host. Response shape per the public API reference (2026-09-02):
  ``{"data": [{"id", "display_name", "created_at", "type"}], "has_more",
  "last_id"}``; the rows are read defensively so a field change degrades a
  column, never the command. FIELD-UNVERIFIED against a live key.

Runs on the host and needs no sandbox either way.

API shape verified against github-copilot-sdk 1.0.8 (2026-07-25):
``CopilotClient.list_models() -> list[ModelInfo]`` where ModelInfo carries
id, name, capabilities.supports.{vision,reasoning_effort},
capabilities.limits.{max_prompt_tokens,max_context_window_tokens},
policy.state, billing.multiplier, supported_reasoning_efforts and
default_reasoning_effort, plus a to_dict(). Attribute access below is
getattr-defensive anyway so an SDK bump degrades a column, never the
command.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sbxloop.backends import ANTHROPIC_TOKEN_ENV, AgentBackend
from sbxloop.errors import SbxloopError

# The SDK's documented auth resolution order.
SDK_TOKEN_ENVS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_PAGE_SIZE = 100
# Pages are ~100 short records; anything past this is not a model list.
ANTHROPIC_MAX_BYTES = 1 << 20

SDK_INSTALL_HINT = (
    "github-copilot-sdk is not installed on this host — install it with "
    "`pip install 'sbxloop[copilot]'` (or `pip install github-copilot-sdk`) "
    "to list models"
)


@dataclass(frozen=True)
class ModelRow:
    """One model, flattened for display (raw carries the SDK's full dict)."""

    id: str
    name: str
    multiplier: float | None
    context_window: int | None
    vision: bool
    reasoning_efforts: tuple[str, ...] | None
    default_reasoning_effort: str | None
    policy_state: str | None
    raw: dict[str, Any]
    created: str | None = None


def auth_hint(env: dict[str, str] | None = None) -> str:
    """Say which SDK auth env var (if any) this process can see."""
    env = dict(os.environ) if env is None else env
    for name in SDK_TOKEN_ENVS:
        if env.get(name):
            return (
                f"auth: {name} is set — if listing failed anyway, the token "
                'likely lacks the "Copilot Requests" permission or the '
                "subscription has no model access"
            )
    return (
        f"auth: none of {', '.join(SDK_TOKEN_ENVS)} is set — create a "
        'fine-grained PAT with the "Copilot Requests" permission and export '
        f"{SDK_TOKEN_ENVS[0]} (or put it in ./.env)"
    )


def fetch_models(timeout_s: float = 60.0) -> list[Any]:
    """The SDK's ModelInfo list, via a short-lived host-side client."""
    try:
        from copilot import CopilotClient
    except ImportError as exc:
        raise SbxloopError(SDK_INSTALL_HINT) from exc

    async def _session() -> list[Any]:
        async with CopilotClient() as client:
            models = await client.list_models()
            return list(models)

    try:
        return asyncio.run(asyncio.wait_for(_session(), timeout=timeout_s))
    except TimeoutError as exc:
        raise SbxloopError(
            f"listing models timed out after {timeout_s:.0f}s — the bundled "
            f"Copilot runtime may be unable to start or reach the API | {auth_hint()}"
        ) from exc
    except SbxloopError:
        raise
    except Exception as exc:
        # Auth failures surface as opaque SDK errors; append what the token
        # environment actually looks like (mirrors the worker backend).
        raise SbxloopError(f"listing models failed: {exc} | {auth_hint()}") from exc


def _raw_dict(info: Any) -> dict[str, Any]:
    to_dict = getattr(info, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
        except Exception:
            return {}
        if isinstance(raw, dict):
            return raw
    return {}


def model_row(info: Any) -> ModelRow:
    """Flatten one SDK ModelInfo (or anything shaped like it) for display."""
    capabilities = getattr(info, "capabilities", None)
    supports = getattr(capabilities, "supports", None)
    limits = getattr(capabilities, "limits", None)
    context = getattr(limits, "max_context_window_tokens", None)
    if not isinstance(context, int):
        context = None
    multiplier = getattr(getattr(info, "billing", None), "multiplier", None)
    multiplier = float(multiplier) if isinstance(multiplier, int | float) else None
    efforts = getattr(info, "supported_reasoning_efforts", None)
    policy_state = getattr(getattr(info, "policy", None), "state", None)
    default_effort = getattr(info, "default_reasoning_effort", None)
    return ModelRow(
        id=str(getattr(info, "id", "") or ""),
        name=str(getattr(info, "name", "") or ""),
        multiplier=multiplier,
        context_window=context,
        vision=bool(getattr(supports, "vision", False)),
        reasoning_efforts=(
            tuple(str(e) for e in efforts) if isinstance(efforts, list | tuple) else None
        ),
        default_reasoning_effort=str(default_effort) if isinstance(default_effort, str) else None,
        policy_state=str(policy_state) if isinstance(policy_state, str) else None,
        raw=_raw_dict(info),
    )


# -- the claude backend: Anthropic Models API over the stdlib ----------------

OpenUrl = Callable[[urllib.request.Request, float], bytes]


def _open_url(request: urllib.request.Request, timeout_s: float) -> bytes:
    # nosec B310 - ANTHROPIC_MODELS_URL is a constant https:// literal
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310
        return bytes(response.read(ANTHROPIC_MAX_BYTES + 1))


def fetch_anthropic_models(
    timeout_s: float = 60.0,
    env: dict[str, str] | None = None,
    *,
    open_url: OpenUrl | None = None,
) -> list[dict[str, Any]]:
    """Every model record the Anthropic Models API lists for the key in
    ``ANTHROPIC_API_KEY`` (all pages), as the API's own dicts."""
    env = dict(os.environ) if env is None else env
    key = env.get(ANTHROPIC_TOKEN_ENV, "")
    if not key:
        raise SbxloopError(
            f'{ANTHROPIC_TOKEN_ENV} is not set — [agent] backend = "claude" lists '
            "models with it; create an Anthropic API key and export it (or put "
            "it in ./.env)"
        )
    opener = _open_url if open_url is None else open_url
    records: list[dict[str, Any]] = []
    after_id: str | None = None
    for _ in range(50):  # pagination guard: never loop on a misbehaving server
        query = {"limit": str(ANTHROPIC_PAGE_SIZE)}
        if after_id:
            query["after_id"] = after_id
        request = urllib.request.Request(
            f"{ANTHROPIC_MODELS_URL}?{urllib.parse.urlencode(query)}",
            headers={
                "x-api-key": key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Accept": "application/json",
            },
        )
        page = _anthropic_page(opener, request, timeout_s)
        data = page.get("data")
        records.extend(
            item for item in (data if isinstance(data, list) else []) if isinstance(item, dict)
        )
        last_id = page.get("last_id")
        if not page.get("has_more") or not isinstance(last_id, str) or last_id == after_id:
            break
        after_id = last_id
    return records


def _anthropic_page(
    opener: OpenUrl, request: urllib.request.Request, timeout_s: float
) -> dict[str, Any]:
    try:
        raw = opener(request, timeout_s)
    except urllib.error.HTTPError as exc:
        hint = " — the key is invalid or revoked" if exc.code in (401, 403) else ""
        raise SbxloopError(f"listing models failed: HTTP {exc.code}{hint}") from exc
    except urllib.error.URLError as exc:
        raise SbxloopError(f"listing models failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SbxloopError(f"listing models timed out after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise SbxloopError(f"listing models failed: {exc}") from exc
    if len(raw) > ANTHROPIC_MAX_BYTES:
        raise SbxloopError("listing models failed: response is not a model list (too large)")
    try:
        page = json.loads(raw)
    except ValueError as exc:
        raise SbxloopError("listing models failed: response is not JSON") from exc
    if not isinstance(page, dict):
        raise SbxloopError("listing models failed: response is not a model list")
    return page


def anthropic_model_row(record: dict[str, Any]) -> ModelRow:
    """Flatten one Models API record; the Copilot-only columns stay blank."""
    created = record.get("created_at")
    return ModelRow(
        id=str(record.get("id") or ""),
        name=str(record.get("display_name") or ""),
        multiplier=None,
        context_window=None,
        vision=False,
        reasoning_efforts=None,
        default_reasoning_effort=None,
        policy_state=None,
        raw=dict(record),
        created=str(created)[:10] if isinstance(created, str) else None,
    )


def fetch_backend_rows(backend: AgentBackend, timeout_s: float = 60.0) -> list[ModelRow]:
    """The configured backend's models, flattened for display."""
    if backend.name == "claude":
        return [anthropic_model_row(record) for record in fetch_anthropic_models(timeout_s)]
    return [model_row(info) for info in fetch_models(timeout_s=timeout_s)]


def table_columns(backend: AgentBackend) -> tuple[str, ...]:
    """The columns `list-models` renders for ``backend``: the Models API
    carries no billing/context/reasoning metadata, so the claude table is
    id, name and release date."""
    if backend.name == "claude":
        return ("model", "name", "created")
    return ("model", "name", "billing", "context", "vision", "reasoning", "policy")


def format_context(tokens: int | None) -> str:
    if tokens is None:
        return ""
    if tokens >= 1000:
        return f"{tokens // 1000}k"
    return str(tokens)


def format_efforts(row: ModelRow) -> str:
    if not row.reasoning_efforts:
        return ""
    return ", ".join(
        f"{effort}*" if effort == row.default_reasoning_effort else effort
        for effort in row.reasoning_efforts
    )
