"""Host-side Copilot model listing.

`sbxloop list-models` asks the github-copilot-sdk — the same SDK agent
sessions run on inside the sandbox — which models the authenticated Copilot
subscription can use, so `model = "..."` in sbxloop.toml (or `--model`) can
be chosen from real ids instead of guesswork.

Runs on the host: the SDK resolves auth from the environment
(COPILOT_GITHUB_TOKEN → GH_TOKEN → GITHUB_TOKEN; ./.env is loaded by the
CLI callback) and needs no sandbox. The SDK is optional host-side (it is
the worker's `[copilot]` extra), so the import is deferred and its absence
surfaces as an actionable error instead of a broken CLI.

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
import os
from dataclasses import dataclass
from typing import Any

from sbxloop.errors import SdxloopError

# The SDK's documented auth resolution order.
SDK_TOKEN_ENVS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

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
        raise SdxloopError(SDK_INSTALL_HINT) from exc

    async def _session() -> list[Any]:
        async with CopilotClient() as client:
            models = await client.list_models()
            return list(models)

    try:
        return asyncio.run(asyncio.wait_for(_session(), timeout=timeout_s))
    except TimeoutError as exc:
        raise SdxloopError(
            f"listing models timed out after {timeout_s:.0f}s — the bundled "
            f"Copilot runtime may be unable to start or reach the API | {auth_hint()}"
        ) from exc
    except SdxloopError:
        raise
    except Exception as exc:
        # Auth failures surface as opaque SDK errors; append what the token
        # environment actually looks like (mirrors the worker backend).
        raise SdxloopError(f"listing models failed: {exc} | {auth_hint()}") from exc


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
