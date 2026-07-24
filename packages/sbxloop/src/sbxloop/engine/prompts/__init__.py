"""Prompt template loading and rendering.

Templates are Markdown files shipped in this package, rendered with
``string.Template`` ($-substitution) so JSON braces in the templates need no
escaping. Rendering fails loudly on missing variables — a silently empty
prompt section is a debugging nightmare.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from string import Template


@cache
def _template(name: str) -> Template:
    text = (resources.files(__name__) / f"{name}.md").read_text()
    return Template(text)


def render(name: str, **context: str) -> str:
    """Render prompt template ``name`` with strict substitution."""
    context.setdefault("retry_context", "")
    return _template(name).substitute(context)


def bullet_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)
