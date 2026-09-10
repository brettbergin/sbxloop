"""Model selection and model-only refresh at agent phase boundaries."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sbxloop.config import AgentModels, Config, load_config
from sbxloop.errors import ConfigError


@dataclass
class ModelSelection:
    model: str
    source: str


def run_model_repo(config: Config) -> str | None:
    """Keep repo-less workloads separate from the daemon's default delivery repo."""
    return config.github.repo if config.run_model_repo is None else config.run_model_repo or None


def model_for_phase(config: Config, phase: str, *, repo: str | None = None) -> ModelSelection:
    """Resolve one requested model; None never selects an implicit repo."""
    if phase == "concierge":
        return ModelSelection(
            config.concierge.model or config.model,
            "concierge.model" if config.concierge.model else "model",
        )
    if phase not in AgentModels.model_fields:
        raise ValueError(f"unknown agent phase {phase!r}")
    if config.run_model_override is not None:
        return ModelSelection(config.run_model_override, "--model")
    if repo is not None:
        for entry in config.github.repos:
            if entry.repo.casefold() == repo.casefold():
                value = getattr(entry.agent_models, phase)
                if value is not None:
                    return ModelSelection(value, f"github.repos[{entry.repo}].agent_models.{phase}")
                break
    value = getattr(config.agent.models, phase)
    if value is not None:
        return ModelSelection(value, f"agent.models.{phase}")
    return ModelSelection(config.model, "model")


def refreshed_models(config: Config) -> Config:
    """Read the original operator layers, replacing only model settings.

    Directly constructed configs are static (the embedding caller owns
    them). Loaded configs remember their source directory and environment;
    after restart the environment is current, while the home stays pinned.
    A parse/validation error stops dispatch rather than using stale policy.
    """
    if config.model_source_dir is None:
        return config
    env = dict(os.environ if config._model_env is None else config._model_env)
    env["SBXLOOP_HOME"] = str(config.home)
    live = load_config(config.model_source_dir, env=env)
    if live.agent.backend != config.agent.backend:
        raise ConfigError(
            "the agent backend changed; live refresh supports models only. "
            "Restore the run's backend or start a new run with the new backend"
        )
    overrides = {entry.repo.casefold(): entry.agent_models for entry in live.github.repos}
    repos = [
        entry.model_copy(
            update={"agent_models": overrides.get(entry.repo.casefold(), AgentModels())}
        )
        for entry in config.github.repos
    ]
    return config.model_copy(
        update={
            "model": live.model,
            "agent": config.agent.model_copy(update={"models": live.agent.models}),
            "github": config.github.model_copy(update={"repos": repos}),
            "concierge": config.concierge.model_copy(update={"model": live.concierge.model}),
        }
    )


def model_plan(config: Config, *, repo: str | None = None) -> dict[str, ModelSelection]:
    return {
        phase: model_for_phase(config, phase, repo=repo)
        for phase in (*AgentModels.model_fields, "concierge")
    }
