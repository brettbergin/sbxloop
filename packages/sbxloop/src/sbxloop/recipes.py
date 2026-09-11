"""The fixed recipes a `tool` run can be seeded with.

A recipe is trusted host code that is the whole plan: it pins the run's
config to the capabilities the work actually needs, stages the code the
run executes, and supplies the mechanical task graph — each task a command
the host chose, its checks and the files it leaves. Everything after that
is an ordinary run: the sandbox, the chronology, publication, resume — and
not one agent turn, because a recipe has nothing to ask a model.

The daemon knows recipes only through this registry, so adding one is a
row here rather than another persisted column and another branch in the
loop. A work item names its recipe and the one target that recipe was
queued for; the registry turns that pair into config, inputs and tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from sbxloop import entrygraph
from sbxloop.config import Config
from sbxloop.engine.model import TaskSpec
from sbxloop.errors import ConfigError


class Recipe(NamedTuple):
    """One seeded tool run, as the daemon needs it.

    ``config`` narrows a run's config to what the recipe may reach and
    publish to; ``stage`` writes the recipe's own code into the run's
    workspace *before* it is mounted; ``task`` is the seeded task graph.
    All three take the target the item was queued for, so none of them has
    to re-derive it from a config that may have been narrowed since.
    """

    name: str
    config: Callable[[Config, str], Config]
    stage: Callable[[Config, str, str], Path]
    task: Callable[[Config, str], TaskSpec]


RECIPES: dict[str, Recipe] = {
    "entrygraph": Recipe(
        name="entrygraph",
        config=entrygraph.tool_config,
        stage=entrygraph.stage_scanner,
        task=entrygraph.tool_task,
    ),
}


def get_recipe(name: str) -> Recipe:
    """The named recipe, or a configuration error naming the ones there are.

    A persisted item can outlive the recipe it names (a downgrade, a removed
    entry); failing here — inside the runner's exception boundary — reports
    and settles that item instead of starting a run with no task graph.
    """
    recipe = RECIPES.get(name)
    if recipe is None:
        known = ", ".join(sorted(RECIPES)) or "none"
        raise ConfigError(f"unknown workload recipe {name!r} (known: {known})")
    return recipe
