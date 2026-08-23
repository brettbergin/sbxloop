"""Display formatting for shell commands rendered into run threads and the TUI.

A bash tool call arrives as one long string whose most informative part —
the verb and its distinguishing arguments — sits in the middle, behind a
boilerplate ``cd <absolute run path> &&`` prefix that is identical on every
call in a run. Naive middle-elision therefore spends the whole visible
budget on the path and cuts away the verb.

This module renders a command for display:

* whitespace (including newlines) is collapsed to single spaces;
* a leading ``cd <run path> &&`` / ``cd <run path>;`` prefix is rewritten to
  ``cd $RUN &&`` (see :func:`collapse_run_prefix`);
* if the result is still too long, the *longest argument tokens* are elided
  in the middle one at a time, so the leading verb and the short flags stay
  intact; every token that lost characters carries a literal ``…`` marker,
  so a token in the output is never a silently truncated one.

``COMMAND_DISPLAY_CLIP`` is the default maximum rendered width, in
characters, of a single command line. 160 characters fits a wide terminal
line and a Discord thread line without wrapping more than once; it is only a
default — callers may pass a different ``limit``.

Stdlib-only by design, so both ``sbxloop.cli.tui`` and
``sbxloop.daemon.discord_format`` can import it without an import cycle.
"""

from __future__ import annotations

import re

__all__ = ["COMMAND_DISPLAY_CLIP", "collapse_run_prefix", "format_command"]

#: Default maximum rendered width (characters) of a displayed command.
COMMAND_DISPLAY_CLIP = 160

#: A token is only elided if it can keep this many characters on each side of
#: the ``…`` marker; shorter tokens are left alone (eliding them saves little
#: and destroys readability).
_TOKEN_KEEP = 6

_ELLIPSIS = "…"
_RUN_PREFIX = "cd $RUN && "

# ``cd <path> &&`` or ``cd <path>;`` with an optionally quoted path.
_CD_PREFIX_RE = re.compile(r"""^cd\s+(?P<q>['"]?)(?P<path>.+?)(?P=q)\s*(?:&&|;)\s*""")
_RUNS_SEGMENT_RE = re.compile(r"/runs/[^/]+")


def collapse_run_prefix(args: str, run_root: str | None = None) -> str:
    """Rewrite a leading ``cd <sandbox run path> &&`` prefix to ``cd $RUN &&``.

    The path qualifies when it starts with ``run_root`` (if one is given) or
    contains a ``/runs/<id>`` segment. Any other ``cd`` prefix — ``cd /tmp &&
    ls`` — is left exactly as it is, as is a string with no such prefix.
    """
    text = str(args)
    match = _CD_PREFIX_RE.match(text)
    if match is None:
        return text
    path = match.group("path")
    qualifies = bool(_RUNS_SEGMENT_RE.search(path))
    if run_root:
        qualifies = qualifies or path.startswith(str(run_root))
    if not qualifies:
        return text
    return _RUN_PREFIX + text[match.end() :]


def _elide_token(token: str) -> str | None:
    """Middle-elide a token, or None when it is too short to be worth it."""
    if _ELLIPSIS in token:
        return None
    if len(token) <= 2 * _TOKEN_KEEP + 1:
        return None
    return f"{token[:_TOKEN_KEEP]}{_ELLIPSIS}{token[-_TOKEN_KEEP:]}"


def _preserved_count(tokens: list[str]) -> int:
    """How many leading tokens must survive verbatim: the ``cd $RUN &&``
    prefix, when present, plus the command verb that follows it."""
    idx = 0
    if tokens[:3] == ["cd", "$RUN", "&&"]:
        idx = 3
    return min(idx + 1, len(tokens))


def format_command(
    args: str,
    limit: int = COMMAND_DISPLAY_CLIP,
    run_root: str | None = None,
) -> str:
    """Render ``args`` as a single display line of at most ``limit`` characters.

    The leading command verb is never dropped, and no token is shortened
    without gaining a ``…`` marker. Idempotent: formatting an already
    formatted command returns it unchanged.
    """
    flat = " ".join(str(args).split())
    flat = collapse_run_prefix(flat, run_root)
    if len(flat) <= limit or not flat:
        return flat

    tokens = flat.split(" ")
    keep = _preserved_count(tokens)
    # Elide the longest eligible argument first, then the next longest, until
    # the line fits or nothing else can usefully shrink.
    while len(" ".join(tokens)) > limit:
        best_i = -1
        best_len = 0
        for i in range(keep, len(tokens)):
            if len(tokens[i]) > best_len and _elide_token(tokens[i]) is not None:
                best_i, best_len = i, len(tokens[i])
        if best_i < 0:
            break
        elided = _elide_token(tokens[best_i])
        assert elided is not None
        tokens[best_i] = elided

    out = " ".join(tokens)
    if len(out) <= limit:
        return out

    # Verb-preserving elision could not fit: drop whole tokens from the middle,
    # keeping the preserved head and as much of the tail as fits.
    head = " ".join(tokens[:keep])
    marker = f" {_ELLIPSIS} "
    tail_tokens: list[str] = []
    for tok in reversed(tokens[keep:]):
        candidate = [tok, *tail_tokens]
        if len(head) + len(marker) + len(" ".join(candidate)) > limit:
            break
        tail_tokens = candidate
    if tail_tokens:
        return head + marker + " ".join(tail_tokens)
    if len(head) + len(marker.rstrip()) <= limit:
        return head + marker.rstrip()
    # Even the head alone overflows; elide inside it rather than lose the verb.
    if limit <= 1:
        return _ELLIPSIS
    cut = limit - 1
    return head[:cut] + _ELLIPSIS
