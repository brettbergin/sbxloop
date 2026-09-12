"""What each key is *for*, read from the example config.

The ``Config`` model documents its fields as comments, not
``Field(description=)``, so the one place every key has a sentence is
``sbxloop.toml.example`` — and ``tests/unit/test_examples.py`` holds that
file to mentioning every key the model knows. This module reads the
packaged copy once and pairs each key with its comment: the trailing one on
the key's own line when there is one, else the run of comment lines
directly above it. A key the parser cannot pair gets nothing — never an
invented sentence; the caller falls back to the type summary.

Keys are the example's dotted form (``github.repos.repo`` for a
``[[github.repos]]`` entry); :func:`doc_for` strips an editor path's indices
so ``github.repos[1].repo`` finds it.
"""

from __future__ import annotations

import re
from functools import cache
from importlib import resources

#: ``[table]`` / ``[[array]]``, live or commented out.
_HEADER = re.compile(r"^#?\s*\[\[?([A-Za-z0-9_.-]+)\]\]?\s*$")
#: ``key = value``, live or commented out, with an optional trailing comment.
_KEY = re.compile(r"^#?\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)(?:\s+#\s*(.*))?$")
#: A comment line that only continues the previous trailing comment: the
#: example aligns wrapped trailing comments under the ``#`` of the first.
_CONTINUATION = re.compile(r"^#\s{10,}#\s*(.*)$")
_COMMENT = re.compile(r"^#\s?(.*)$")
_INDEX = re.compile(r"\[\d+\]")


def _example_text() -> str:
    return resources.files("sbxloop.data").joinpath("sbxloop.toml.example").read_text("utf-8")


def parse(text: str) -> dict[str, str]:
    """``{dotted key: doc line}`` for every key ``text`` documents."""
    docs: dict[str, str] = {}
    prefix = ""
    block: list[str] = []
    last_key: str | None = None
    # The comment block straight under a header is the section's own doc
    # (kept under the section's name) and, when the first key carries no
    # comment of its own, that key's too: in this file the two usually
    # coincide — the paragraph under `[agent]` is about `backend`.
    section: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            block = []
            last_key = None
            section = None
            continue
        header = _HEADER.match(line)
        if header:
            prefix = header.group(1) + "."
            section = header.group(1)
            block = []
            last_key = None
            continue
        continuation = _CONTINUATION.match(line)
        if continuation and last_key is not None:
            docs[last_key] = f"{docs[last_key]} {continuation.group(1)}".strip()
            continue
        key = _KEY.match(line)
        # A commented key line reads as a comment too; the key match wins
        # only when the value looks like TOML rather than prose.
        if key and _looks_like_value(key.group(2)):
            dotted = prefix + key.group(1)
            trailing = (key.group(3) or "").strip()
            above = " ".join(block).strip()
            if section is not None and above and section not in docs:
                docs[section] = above
            section = None
            doc = trailing or above
            if doc and dotted not in docs:
                docs[dotted] = doc
            last_key = dotted if trailing else None
            block = []
            continue
        comment = _COMMENT.match(line)
        if comment:
            block.append(comment.group(1).strip())
            last_key = None
            continue
        block = []
        last_key = None
    return docs


def _looks_like_value(value: str) -> bool:
    """Whether the text after ``=`` is a TOML value, not a sentence that
    happened to contain ``=`` (``# Precedence: env > file = ...``)."""
    value = value.strip()
    if not value:
        return False
    return (
        value[0] in "\"'[{" or value in ("true", "false") or value[0].isdigit() or value[0] == "-"
    )


@cache
def doc_lines() -> dict[str, str]:
    """The packaged example's docs, parsed once per process."""
    return parse(_example_text())


def doc_for(dotted: str) -> str | None:
    """The doc line for an editor path; indices are not part of the key."""
    return doc_lines().get(_INDEX.sub("", dotted))


__all__ = ["doc_for", "doc_lines", "parse"]
