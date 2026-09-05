"""Package data: the config template, the presets and the home's files.

``sbxloop.toml.example`` at the repository root is a symlink to the copy
here, as is ``.env.example`` to ``secrets.env.example``: one source of
truth each, so the shipped file and the committed example cannot drift.
"""

from __future__ import annotations

from importlib import resources

DEFAULT_CONFIG_TOML = (
    resources.files("sbxloop.data").joinpath("sbxloop.toml.example").read_text(encoding="utf-8")
)


def config_presets() -> dict[str, str]:
    """The packaged `init --preset` fragments by name, from `sbxloop/data/presets`.

    Package data, not a checkout path, so `sbxloop init --preset` works from a
    wheel (#636) and nothing `init` writes points outside the user's project.
    """
    folder = resources.files("sbxloop.data").joinpath("presets")
    return {
        entry.name.removesuffix(".toml"): entry.read_text(encoding="utf-8")
        for entry in folder.iterdir()
        if entry.name.endswith(".toml")
    }


def render_config_template(preset: str | None = None) -> str:
    """The template `sbxloop init` writes, with a preset's sections appended.

    Every table in the template is commented out, so appending a preset's
    live `[budgets]`/`[limits]` yields valid TOML. Raises KeyError for a
    preset name the package does not ship.
    """
    if preset is None:
        return DEFAULT_CONFIG_TOML
    fragment = config_presets()[preset]
    return DEFAULT_CONFIG_TOML.rstrip("\n") + "\n\n" + fragment


def secrets_env_template() -> str:
    """The shape of ``config/secrets.env``: the committed ``.env.example``."""
    return (
        resources.files("sbxloop.data").joinpath("secrets.env.example").read_text(encoding="utf-8")
    )


__all__ = [
    "DEFAULT_CONFIG_TOML",
    "config_presets",
    "render_config_template",
    "secrets_env_template",
]
