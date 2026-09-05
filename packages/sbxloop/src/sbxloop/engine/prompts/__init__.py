"""Prompt template loading and rendering.

Templates are Markdown files shipped in this package, rendered with
``string.Template`` ($-substitution) so JSON braces in the templates need no
escaping. Rendering fails loudly on missing variables — a silently empty
prompt section is a debugging nightmare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache
from importlib import resources
from string import Template

from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS

# The header is followed by a blank line (mdformat insists on one before the
# title); swallow it too so the rendered prompt still opens with "# ...".
_CONTRACT_HEADER = re.compile(r"\A<!--.*?-->\n*", re.DOTALL)


def _strip_contract_header(text: str) -> str:
    """Drop the leading ``<!-- ... -->`` block that documents a template's
    contract (#225). It is written for the humans editing the file — the
    variables it lists, the ``$``-escaping rule, which test guards which
    section — and must never cost the model tokens or, worse, be read by
    it as instructions. Stripping happens before ``Template`` sees the text
    so the header can name ``$variables`` freely."""
    return _CONTRACT_HEADER.sub("", text, count=1)


@cache
def _template(name: str) -> Template:
    text = (resources.files(__name__) / f"{name}.md").read_text()
    return Template(_strip_contract_header(text))


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
    # The repository's PR conventions (#678) are a paragraph only when the
    # workspace declares some; most render calls have none to give.
    context.setdefault("pr_conventions", "")
    # The repository's own instruction files (#688): a heading and their
    # text when the workspace has any, else nothing — a render call with
    # no workspace (a unit test, the concierge) gives none.
    context.setdefault("repo_conventions", "")
    # Where the builder is and what it has (#689): the checkout path and
    # the resolved toolchain set, rendered by the phase from the run.
    context.setdefault("work_dir", "the current working directory")
    context.setdefault("toolchains", "(none)")
    # What the sandbox's verification did not decide (#682): advisory
    # failures, or that nothing ran under `ci-only`. Empty under `full`.
    context.setdefault("verification", "")
    # The run's service credentials (#765): a section naming the
    # `call_service` tool and the granted names when the run has any,
    # rendered by the build phase; nothing — not even a blank line — for
    # the runs that have none, which is every `code` run.
    context.setdefault("service_tools", "")
    context.setdefault("baseline_registries", _domain_list(BASELINE_REGISTRY_DOMAINS))
    context.setdefault("declarable_registries", _domain_list(WELL_KNOWN_REGISTRY_DOMAINS))
    return _template(name).substitute(context)


def bullet_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)
