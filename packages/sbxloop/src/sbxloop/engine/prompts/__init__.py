"""Prompt template loading and rendering.

Templates are Markdown files shipped in this package, rendered with
``string.Template`` ($-substitution) so JSON braces in the templates need no
escaping. Rendering fails loudly on missing variables — a silently empty
prompt section is a debugging nightmare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import cache
from importlib import resources
from string import Template

from sbxloop.policy import BASELINE_REGISTRY_DOMAINS, WELL_KNOWN_REGISTRY_DOMAINS
from sbxloop.toolchains import normalize_language


@cache
def _raw(name: str) -> str:
    return str((resources.files(__name__) / f"{name}.md").read_text())


def _domain_list(domains: Iterable[str]) -> str:
    return ", ".join(f"`{d}`" for d in domains) or "(none)"


# The ecosystem reference block in plan.md/execute.md, delimited so entries
# can be filtered without the prose being duplicated into Python.
_ECOSYSTEM_START = "<!-- ecosystems:start -->"
_ECOSYSTEM_END = "<!-- ecosystems:end -->"
# Entry headings are prose ("Java/JVM"); map them onto the canonical
# `[sandbox] languages` keys so one vocabulary drives provisioning, egress
# and the prompts.
_ECOSYSTEM_KEYS = {
    "python": "python",
    "javascript/node": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "ruby": "ruby",
    "java/jvm": "java",
    "c#/.net": "dotnet",
    "php": "php",
    "c/c++": "cpp",
}
_ENTRY_RE = re.compile(r"^(\s*)- \*\*(?P<name>[^*]+)\*\*", re.M)


def _select_ecosystems(text: str, languages: Sequence[str] | None) -> str:
    """Keep only the selected languages' entries in the ecosystem block.

    The block documents all ten ecosystems. Sending all ten on every request
    spends the prompt budget on nine the sandbox was never provisioned for,
    and — worse — advertises toolchains that are not installed: a planner
    told about `cargo test` in a Python-only sandbox can plan a build that
    cannot run. `[sandbox] languages` already says which ecosystems exist
    for this run, so the prompt is filtered to match the sandbox.

    ``languages=None`` keeps every entry, which is what a caller rendering a
    template outside a run context (docs, tests) wants.
    """
    start = text.find(_ECOSYSTEM_START)
    end = text.find(_ECOSYSTEM_END)
    if start == -1 or end == -1:
        return text
    block = text[start + len(_ECOSYSTEM_START) : end]
    head, tail = text[:start], text[end + len(_ECOSYSTEM_END) :]
    if languages is None:
        return head + block.strip("\n") + tail

    wanted = {normalize_language(name) for name in languages}
    matches = list(_ENTRY_RE.finditer(block))
    kept: list[str] = []
    for i, match in enumerate(matches):
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        key = _ECOSYSTEM_KEYS.get(match.group("name").strip().lower())
        if key in wanted:
            kept.append(block[match.start() : stop].rstrip("\n"))
    if not kept:
        # Every selected language is one this block has no entry for. Drop
        # the block rather than falling back to all ten: a planner given
        # ten irrelevant ecosystems is worse off than one given none, which
        # is the pre-Layer-3 state and still correct.
        return head.rstrip("\n") + "\n" + tail
    return head + "\n".join(kept) + tail


def render(name: str, *, languages: Sequence[str] | None = None, **context: str) -> str:
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
    text = _select_ecosystems(_raw(name), languages)
    return Template(text).substitute(context)


def bullet_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)
