"""Package data: the config template, the presets and the home's files.

``sbxloop.toml.example`` at the repository root is a symlink to the copy
here, as is ``.env.example`` to ``secrets.env.example``: one source of
truth each, so the shipped file and the committed example cannot drift.
"""

from __future__ import annotations

from importlib import resources

import tomlkit
from tomlkit.items import Table

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
    """The template `sbxloop init` writes, with a preset's sections merged.

    Resource limits are live in the template. Merge preset sandbox keys
    into that table, retaining limits the preset did not override and the
    template's operator guidance. Unknown preset names raise KeyError.
    """
    if preset is None:
        return DEFAULT_CONFIG_TOML
    fragment = config_presets()[preset]
    document = tomlkit.parse(DEFAULT_CONFIG_TOML)
    for key, value in tomlkit.parse(fragment).body:
        if key is None:
            document.append(None, value)
            continue
        current = document.get(key.key)
        if isinstance(current, Table) and isinstance(value, Table):
            for child_key, child_value in value.value.body:
                if child_key is None:
                    current.append(None, child_value)
                else:
                    current[child_key.key] = child_value
        else:
            document[key.key] = value
    return tomlkit.dumps(document)


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
