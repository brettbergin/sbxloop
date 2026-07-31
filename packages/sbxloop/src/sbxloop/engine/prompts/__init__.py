"""Prompt template loading and rendering.

Templates are Markdown files shipped in this package, rendered with
``string.Template`` ($-substitution) so JSON braces in the templates need no
escaping. Rendering fails loudly on missing variables — a silently empty
prompt section is a debugging nightmare.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib import resources
from string import Template

from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS


@cache
def _template(name: str) -> Template:
    text = (resources.files(__name__) / f"{name}.md").read_text()
    return Template(text)


def _domain_list(domains: Iterable[str]) -> str:
    return ", ".join(f"`{d}`" for d in domains) or "(none)"


def render(name: str, **context: str) -> str:
    """Render prompt template ``name`` with strict substitution.

    The registry tiers are injected rather than written into the templates:
    what a planner may assume reachable is a policy fact, and #141 moves
    registries between the tiers one language at a time. A hardcoded list
    would drift, and a drifted list is a failed run — the planner either
    declares a domain that needs no declaration or omits one that does.
    """
    context.setdefault("retry_context", "")
    context.setdefault("baseline_registries", _domain_list(BASELINE_REGISTRY_DOMAINS))
    context.setdefault("declarable_registries", _domain_list(WELL_KNOWN_REGISTRY_DOMAINS))
    return _template(name).substitute(context)


def bullet_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)
