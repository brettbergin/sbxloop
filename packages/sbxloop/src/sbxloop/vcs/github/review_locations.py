"""Commentable RIGHT-side lines in a GitHub PR file's unified patch.

Only additions and context have RIGHT-side line numbers. Missing patches
(binary/oversized files), malformed hunks and truncated hunks establish no
inline locations; their findings can still be posted in the review body.
"""

from __future__ import annotations

import re

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)")


def right_side_ranges(patch: object) -> tuple[range, ...]:
    """Validate the patch before trusting its hunk ranges; never expand them."""
    if not isinstance(patch, str) or not patch:
        return ()
    ranges: list[range] = []
    old_left = new_left = 0
    old_end = new_end = 0
    for line in patch.rstrip("\n").split("\n"):
        header = _HUNK.fullmatch(line)
        if header:
            if old_left or new_left:
                return ()
            try:
                old_start, old_count, new_start, new_count = (
                    int(value) if value is not None else 1 for value in header.groups()
                )
            except ValueError:
                return ()
            if (
                (old_count and old_start == 0)
                or (new_count and new_start == 0)
                or old_start < old_end
                or new_start < new_end
            ):
                return ()
            old_end, new_end = old_start + old_count, new_start + new_count
            ranges.append(range(new_start, new_end))
            old_left, new_left = old_count, new_count
        elif line == "\\ No newline at end of file" and ranges:
            continue
        elif ranges and line[:1] in (" ", "+", "-"):
            old_left -= line[0] in (" ", "-")
            new_left -= line[0] in (" ", "+")
            if old_left < 0 or new_left < 0:
                return ()
        else:
            return ()
    return tuple(ranges) if not old_left and not new_left else ()
