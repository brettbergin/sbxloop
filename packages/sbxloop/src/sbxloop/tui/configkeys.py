"""The dotted paths the console manages configuration by.

Two jobs. The first is **flattening**: the resolved view is one row per
*leaf*, and everything above a leaf is walked, so the second
``[[github.repos]]`` entry is ``github.repos[1].repo`` — its own row, its
own edit — instead of a whole array of tables printed as one unreadable
blob. A list of scalars (``policy.allow``) stays a single leaf, because
the useful edit there is the list, not an element of it; an empty table
stays a leaf too, so there is a row to fill in.

The second is **what a key accepts**. A path is resolved against the
``Config`` model, so the editor can offer a bool as two choices, a
``Literal`` as its set, a number as a number and a list as one item per
line — and knows whether the key may be unset — before the loader is ever
asked. Resolution fails soft: an unknown path is edited as a raw TOML
value, and the loader still has the last word.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
import types
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel

from sbxloop.config import Config

#: One step of a path: a table/mapping key, or an index into an array.
PathPart = str | int

_SEGMENT = re.compile(r"^([^.\[\]]*)((?:\[\d+\])*)$")
_INDEX = re.compile(r"\[(\d+)\]")

#: How a value is edited. ``choice`` covers bools and ``Literal`` sets;
#: ``list`` is a list of scalars, one per line; ``raw`` is a TOML value
#: typed as written, for anything the model could not describe.
ValueKind = Literal["choice", "int", "float", "str", "list", "raw"]


#: Keys the loader resolves after every layer has had its say, so a file
#: that sets one changes nothing. The dialog refuses them by name rather
#: than writing a line that does nothing — the value comes from the
#: environment, and only the environment moves it.
ENV_ONLY_KEYS: dict[str, str] = {
    "home": "the sbxloop home comes from SBXLOOP_HOME (else HOME), never from a file",
}


class PathError(ValueError):
    """A dotted path that is not addressable."""


def parse_path(dotted: str) -> tuple[PathPart, ...]:
    """``github.repos[1].repo`` → ``("github", "repos", 1, "repo")``."""
    if not dotted.strip():
        raise PathError("empty key")
    parts: list[PathPart] = []
    for segment in dotted.split("."):
        match = _SEGMENT.match(segment)
        if match is None:
            raise PathError(f"{dotted!r}: {segment!r} is not a key or an index")
        name, indices = match.group(1), match.group(2)
        if name:
            parts.append(name)
        elif not parts:
            raise PathError(f"{dotted!r}: a key may not start with an index")
        parts.extend(int(n) for n in _INDEX.findall(indices))
    if not parts:
        raise PathError(f"{dotted!r}: nothing to address")
    return tuple(parts)


def format_path(parts: typing.Sequence[PathPart]) -> str:
    """The dotted form of ``parts`` — the inverse of :func:`parse_path`."""
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


def ancestors(dotted: str) -> Iterator[str]:
    """``dotted`` itself, then each shorter prefix: the order to look a
    dotted key up in a mapping keyed by whole containers."""
    try:
        parts = list(parse_path(dotted))
    except PathError:
        return
    while parts:
        yield format_path(parts)
        parts.pop()


def source_for(dotted: str, sources: Mapping[str, str]) -> str:
    """Which layer supplied ``dotted``. The loader attributes whole
    containers — a ``[[github.repos]]`` array is one key to it — so a leaf
    inherits the nearest attributed ancestor's layer."""
    for prefix in ancestors(dotted):
        source = sources.get(prefix)
        if source is not None:
            return source
    return "default"


def is_leaf(value: Any) -> bool:
    """Whether ``value`` is edited whole rather than walked into."""
    if isinstance(value, dict):
        return not value
    if isinstance(value, list):
        return not any(isinstance(item, dict | list) for item in value)
    return True


def flatten(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every leaf under ``data``, by dotted path."""
    flat: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if prefix and is_leaf(value):
            flat[prefix] = value
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        else:  # pragma: no cover - a non-container root
            flat[prefix] = value

    walk("", data)
    return flat


def lookup(data: Mapping[str, Any], parts: typing.Sequence[PathPart]) -> Any:
    """The value at ``parts``, or ``None`` when nothing is there."""
    node: Any = data
    for part in parts:
        if isinstance(part, int):
            if not isinstance(node, list) or not 0 <= part < len(node):
                return None
            node = node[part]
        else:
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
    return node


# -- what a key accepts -----------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """What the ``Config`` model says about one path."""

    path: str
    kind: ValueKind = "raw"
    #: Whether the key may be unset (an optional field, or a path the
    #: model could not describe — the loader decides).
    optional: bool = True
    #: For ``kind="choice"``: every accepted value, as text.
    choices: tuple[str, ...] = ()
    #: For ``kind="list"``: how one item is parsed.
    item_kind: ValueKind = "str"
    #: Whether a ``choice`` is a bool — ``true``/``false`` the values, not
    #: the words.
    boolean: bool = False
    #: ``float``, ``str | None``, ``list[str]`` — for the operator to read.
    type_label: str = "unknown"
    #: ``> 0``, ``at most 200 items`` — the model's own bounds.
    constraints: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        text = self.type_label
        if self.choices:
            text += f" · one of {', '.join(self.choices)}"
        if self.constraints:
            text += f" · {', '.join(self.constraints)}"
        return text


_CONSTRAINT_WORDS: tuple[tuple[str, str], ...] = (
    ("gt", "> {}"),
    ("ge", "at least {}"),
    ("lt", "< {}"),
    ("le", "at most {}"),
    ("min_length", "at least {} long"),
    ("max_length", "at most {} long"),
    ("multiple_of", "a multiple of {}"),
)


def _constraints(metadata: typing.Sequence[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for item in metadata:
        for attr, template in _CONSTRAINT_WORDS:
            bound = getattr(item, attr, None)
            if bound is not None:
                out.append(template.format(bound))
    return tuple(out)


def _label(annotation: Any) -> str:
    """A type the way an operator reads it: ``str | none``, ``list[str]``.
    A ``Literal`` is named by what its members are — the set itself is the
    spec's ``choices``, printed once."""
    if annotation is None or annotation is type(None):
        return "none"
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Literal:
        return _label(type(args[0])) if args else "str"
    if origin in (types.UnionType, typing.Union):
        return " | ".join(_label(arg) for arg in args)
    if origin is not None:
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{', '.join(_label(arg) for arg in args)}]" if args else name
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "").replace("sbxloop.config.", "")


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    """Peel ``| None`` off a union, keeping the rest."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is Literal or not args:
        return annotation, False
    if type(None) not in args:
        return annotation, False
    rest = [arg for arg in args if arg is not type(None)]
    if len(rest) == 1:
        return rest[0], True
    return typing.Union[tuple(rest)], True  # noqa: UP007 - built from parts


def _scalar_kind(annotation: Any) -> ValueKind:
    if annotation is bool:
        return "choice"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str or (isinstance(annotation, type) and issubclass(annotation, Path)):
        return "str"
    return "raw"


def _step(annotation: Any, part: PathPart) -> tuple[Any, typing.Sequence[Any]] | None:
    """Descend one part; ``None`` when the model does not describe it."""
    annotation, _ = _split_optional(annotation)
    origin = typing.get_origin(annotation)
    if isinstance(part, int):
        args = typing.get_args(annotation)
        return (args[0], ()) if origin is list and args else None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        info = annotation.model_fields.get(part)
        return (info.annotation, info.metadata) if info is not None else None
    if origin is dict:
        args = typing.get_args(annotation)
        return (args[1], ()) if len(args) == 2 else None
    return None


def describe(dotted: str, *, model: type[BaseModel] = Config) -> FieldSpec:
    """What ``model`` says the key at ``dotted`` holds."""
    try:
        parts = parse_path(dotted)
    except PathError:
        return FieldSpec(dotted)
    annotation: Any = model
    metadata: typing.Sequence[Any] = ()
    for part in parts:
        step = _step(annotation, part)
        if step is None:
            return FieldSpec(dotted)
        annotation, metadata = step

    inner, optional = _split_optional(annotation)
    origin = typing.get_origin(inner)
    spec = FieldSpec(
        path=dotted,
        optional=optional,
        type_label=_label(annotation),
        constraints=_constraints(metadata),
    )
    if origin is Literal:
        choices = tuple(str(arg) for arg in typing.get_args(inner))
        return dataclasses.replace(spec, kind="choice", choices=choices)
    if inner is bool:
        return dataclasses.replace(spec, kind="choice", choices=("true", "false"), boolean=True)
    if origin is list:
        args = typing.get_args(inner)
        item = _split_optional(args[0])[0] if args else str
        if typing.get_origin(item) is Literal or (
            isinstance(item, type) and issubclass(item, BaseModel)
        ):
            # A list of tables is never a leaf; a list of choices is edited
            # as text, one per line, and the loader checks the set.
            return dataclasses.replace(spec, kind="list")
        return dataclasses.replace(spec, kind="list", item_kind=_scalar_kind(item))
    return dataclasses.replace(spec, kind=_scalar_kind(inner))


# -- values as text ---------------------------------------------------------


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_value(value: Any, spec: FieldSpec) -> str:
    """A value as the text an operator edits: no quotes around a string,
    one item per line for a list, a bare word for a choice — and a TOML
    literal only where the shape is not otherwise known."""
    if value is None:
        return ""
    if spec.kind == "list":
        items = value if isinstance(value, list) else [value]
        return "\n".join(_scalar_text(item) for item in items)
    if spec.kind in ("choice", "int", "float", "str"):
        return _scalar_text(value)
    return toml_literal(value)


def toml_literal(value: Any) -> str:
    """``value`` written the way TOML would write it — a table inline, so
    the whole value stays one line to edit."""
    if isinstance(value, Mapping):
        table = tomlkit.inline_table()
        table.update({key: toml_value(item) for key, item in value.items()})
        return table.as_string()
    literal: str = tomlkit.item(value).as_string()
    return literal


def toml_value(value: Any) -> Any:
    """``value`` as a tomlkit item, tables inline all the way down."""
    if isinstance(value, Mapping):
        table = tomlkit.inline_table()
        table.update({key: toml_value(item) for key, item in value.items()})
        return table
    return value


def _scalar_value(text: str, kind: ValueKind, what: str) -> Any:
    if kind == "int":
        try:
            return int(text, 10)
        except ValueError:
            raise ValueError(f"{what} is a whole number; {text!r} is not") from None
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{what} is a number; {text!r} is not") from None
    if kind == "raw":
        try:
            return tomllib.loads(f"v = {text}")["v"]
        except tomllib.TOMLDecodeError:
            raise ValueError(f"{what} takes a TOML value; {text!r} is not one") from None
    return text


def parse_value(text: str, spec: FieldSpec) -> Any:
    """The value ``text`` means for ``spec``. ``ValueError`` says why not,
    in the operator's terms; the loader still validates what comes back."""
    text = text.strip() if spec.kind != "list" else text
    if spec.kind == "choice":
        if text not in spec.choices:
            raise ValueError(f"{spec.path} is one of {', '.join(spec.choices)}; not {text!r}")
        if spec.boolean:
            return text == "true"
        return text
    if spec.kind == "list":
        return [
            _scalar_value(line.strip(), spec.item_kind, f"{spec.path}'s items")
            for line in text.splitlines()
            if line.strip()
        ]
    return _scalar_value(text, spec.kind, spec.path)


__all__ = [
    "ENV_ONLY_KEYS",
    "FieldSpec",
    "PathError",
    "PathPart",
    "ValueKind",
    "ancestors",
    "describe",
    "flatten",
    "format_path",
    "is_leaf",
    "lookup",
    "parse_path",
    "parse_value",
    "render_value",
    "source_for",
    "toml_literal",
]
