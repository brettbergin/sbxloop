"""Writing one key back into ``sbxloop.toml`` without disturbing the rest.

An operator's config is mostly comments — why a domain is allowed, what a
budget was raised for, which knob was tried and reverted. Editing a single
value must keep every one of them, so the draft is round-tripped with
``tomlkit`` (``tomllib`` reads but cannot write) and only the addressed key
is touched: the file that comes back differs from the file that went in by
that assignment alone.

Paths are :mod:`sbxloop.configedit.keys`', so ``vcs.repos[1].repo``
reaches into the second array-of-tables entry. Containers on the way are
created when missing — a table, or one new array entry appended at the
end — and anything else that does not fit (an index past the end, a key
whose parent is a scalar) is refused by name rather than guessed at.
"""

from __future__ import annotations

from typing import Any

import tomlkit

from sbxloop.configedit.keys import PathError, PathPart, format_path


class ConfigWriteError(ValueError):
    """A key that cannot be written where it was addressed."""


def parse(text: str) -> tomlkit.TOMLDocument:
    """The draft as an editable document; a syntax error is named."""
    try:
        return tomlkit.parse(text)
    except Exception as exc:  # tomlkit raises a family of parse errors
        raise ConfigWriteError(f"invalid TOML: {exc}") from exc


def _mapping(node: Any) -> bool:
    return isinstance(node, dict)


def _sequence(node: Any) -> bool:
    return isinstance(node, list)


def _new_container(next_part: PathPart) -> Any:
    """What a missing step must become to hold ``next_part``."""
    if isinstance(next_part, int):
        return tomlkit.aot()
    return tomlkit.table()


def _descend(node: Any, part: PathPart, rest: tuple[PathPart, ...], where: str) -> Any:
    """One step down, creating the container when it is missing."""
    if isinstance(part, int):
        if not _sequence(node):
            raise ConfigWriteError(
                f"{where}: {format_path([part])} indexes a {type(node).__name__}"
            )
        if part < len(node):
            return node[part]
        if part > len(node):
            raise ConfigWriteError(
                f"{where}: index {part} is past the end — the array holds {len(node)}"
            )
        if not rest or isinstance(rest[0], int):
            raise ConfigWriteError(f"{where}: only a table can be appended to an array of tables")
        fresh = tomlkit.table()
        # A new [[table]] reads as its own stanza, not as a continuation of
        # the one above it.
        fresh.trivia.indent = "\n"
        node.append(fresh)
        return node[part]
    if not _mapping(node):
        raise ConfigWriteError(f"{where}: {part!r} is a key of a {type(node).__name__}")
    if part not in node:
        if not rest:
            raise ConfigWriteError(f"{where}: nothing to descend into")  # pragma: no cover
        node[part] = _new_container(rest[0])
    return node[part]


def _walk(doc: tomlkit.TOMLDocument, parts: tuple[PathPart, ...], where: str) -> Any:
    node: Any = doc
    for index, part in enumerate(parts[:-1]):
        node = _descend(node, part, parts[index + 1 :], where)
    return node


def set_value(text: str, parts: tuple[PathPart, ...], value: Any) -> str:
    """``text`` with the key at ``parts`` set to ``value``."""
    if not parts:
        raise PathError("no key to set")
    where = format_path(parts)
    doc = parse(text)
    node = _walk(doc, parts, where)
    last = parts[-1]
    if isinstance(last, int):
        if not _sequence(node):
            raise ConfigWriteError(f"{where}: [{last}] indexes a {type(node).__name__}")
        if last > len(node):
            raise ConfigWriteError(
                f"{where}: index {last} is past the end — the array holds {len(node)}"
            )
        if last == len(node):
            node.append(value)
        else:
            node[last] = value
    else:
        if not _mapping(node):
            raise ConfigWriteError(f"{where}: {last!r} is a key of a {type(node).__name__}")
        node[last] = value
    return tomlkit.dumps(doc)


def unset_value(text: str, parts: tuple[PathPart, ...]) -> str:
    """``text`` with the key at ``parts`` removed — the file stops saying
    anything about it, so the layer beneath (or the built-in default) is
    what the loader sees. Removing what is not there is not an error."""
    if not parts:
        raise PathError("no key to unset")
    doc = parse(text)
    node: Any = doc
    for part in parts[:-1]:
        if isinstance(part, int):
            if not _sequence(node) or not 0 <= part < len(node):
                return text
        elif not _mapping(node) or part not in node:
            return text
        node = node[part]
    last = parts[-1]
    if isinstance(last, int):
        if not _sequence(node) or not 0 <= last < len(node):
            return text
        del node[last]
    else:
        if not _mapping(node) or last not in node:
            return text
        del node[last]
    return tomlkit.dumps(doc)


def file_value(text: str, parts: tuple[PathPart, ...]) -> tuple[Any, bool]:
    """What *this file* says at ``parts``, and whether it says anything —
    distinct from the effective value, which other layers may supply."""
    try:
        node: Any = parse(text)
    except ConfigWriteError:
        return None, False
    for part in parts:
        if isinstance(part, int):
            if not _sequence(node) or not 0 <= part < len(node):
                return None, False
        elif not _mapping(node) or part not in node:
            return None, False
        node = node[part]
    return node, True


#: The delivery settings a legacy ``[github] repo`` carried beside it; they
#: move into the entry it becomes, where the same keys mean the same.
_LEGACY_REPO_KEYS = ("deliver_base", "create_repo", "create_public")


def migrate_repos(text: str) -> tuple[str, list[str]]:
    """``text`` with its repositories declared under ``[[vcs.repos]]``, and
    the repositories that moved (#2255).

    ``[[github.repos]]`` entries move as they are, sub-tables and comments
    included; a single ``[github] repo`` becomes one entry carrying the
    ``deliver_base`` / ``create_repo`` / ``create_public`` beside it. The
    rest of ``[github]`` stays. A file already on the current spelling comes
    back byte-identical with nothing moved. A file that declares
    repositories under both is refused by name: the loader refuses it too,
    and merging two lists is not something to guess at.
    """
    doc = parse(text)
    github: Any = doc.get("github")
    if not _mapping(github):
        return text, []
    has_entries = "repos" in github
    has_single = "repo" in github
    if not has_entries and not has_single:
        return text, []
    vcs: Any = doc.get("vcs")
    if _mapping(vcs) and vcs.get("repos"):
        raise ConfigWriteError(
            "[[vcs.repos]] and [[github.repos]] (or [github] repo) both declare "
            "repositories: declare each repository once, under [[vcs.repos]], then "
            "run this again"
        )
    if not _mapping(vcs):
        vcs = tomlkit.table()
        doc["vcs"] = vcs
    moved: list[str] = []
    if has_entries:
        entries = github.pop("repos")
        if not _sequence(entries):
            raise ConfigWriteError("github.repos is not an array of tables")
        moved.extend(str(entry.get("repo", "")) for entry in entries)
        vcs["repos"] = entries
    else:
        entry = tomlkit.table()
        entry["repo"] = github.pop("repo")
        for key in _LEGACY_REPO_KEYS:
            if key in github:
                entry[key] = github.pop(key)
        entries = tomlkit.aot()
        entries.append(entry)
        vcs["repos"] = entries
        moved.append(str(entry["repo"]))
    return tomlkit.dumps(doc), moved


def current_spelling(text: str, parts: tuple[PathPart, ...]) -> tuple[str, list[str]]:
    """``text`` ready for a write at ``parts``, and the repositories moved.

    A key under ``vcs.repos`` on a file still spelt ``[[github.repos]]``
    would otherwise open an empty second list beside the old one, so the
    file is migrated first (:func:`migrate_repos`), every entry moved with
    its comments; every surface that writes a key (the editor, the console)
    goes through this. Any other key, a file already on the current
    spelling, or a file the migration refuses is returned untouched — the
    write then fails or succeeds on its own terms."""
    if tuple(parts[:2]) != ("vcs", "repos"):
        return text, []
    try:
        return migrate_repos(text)
    except ConfigWriteError:
        return text, []


__all__ = [
    "ConfigWriteError",
    "current_spelling",
    "file_value",
    "migrate_repos",
    "parse",
    "set_value",
    "unset_value",
]
