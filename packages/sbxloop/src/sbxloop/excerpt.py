"""Layer-neutral bounded output excerpting.

The line-selection half of the tool-output excerpt policy, shared by every
renderer (Discord threads, the ``sbxloop watch`` TUI) so there is exactly one
copy of the truncation rules. Presentation-specific caps -- Discord's
per-message limit, fencing, colours -- stay in the layer that needs them.

The policy, and why it is what it is:

* **Asymmetric budgets.** A tool call that succeeded is noise once you know
  it succeeded -- its outcome already appears on the call's status line --
  so success gets no block of its own by default
  (``TOOL_OUTPUT_LINES_DEFAULT`` is 0). A failure is the thing the reader
  opened the thread (or the TUI) for, so it gets
  ``TOOL_FAIL_OUTPUT_LINES_DEFAULT`` lines: enough stderr to act on without
  burying the surrounding transcript.
* **Head+tail, not tail-only.** Failures usually name the problem near the
  top (the command that could not be found, the first compiler error) and
  the summary near the bottom (``N tests failed``); a plain last-N tail
  loses the first half. So the budget is split evenly and the gap is
  marked ``… N lines elided …`` -- the reader can always tell that output
  was dropped, and how much.
* **Counted from the event's true line count.** ``output_lines`` on the
  event records how long the output really was, which may exceed what
  survived clipping upstream in the worker; the elision count uses it so
  the marker is not quietly wrong.
* **Per-line clip.** ``TOOL_EXCERPT_LINE_CLIP`` bounds each line so a
  single pathological line (minified JS, a one-line JSON blob) cannot eat
  a whole layer's character budget before that layer's own total-size cap
  is applied.

Deliberately dependency-light: it must not import from the daemon layer, so
daemon-side and CLI-side renderers can both depend on it without a cycle.
"""

from __future__ import annotations

from sbxloop.log import redact_text

# -- shared excerpt budget ------------------------------------------------------------------
# Tail lines shown for a *successful* call. 0 means a success renders no
# block of its own: its outcome already appears on the call's status line.
TOOL_OUTPUT_LINES_DEFAULT = 0
# Total head+tail lines shown for a *failed* call: enough stderr to act on.
TOOL_FAIL_OUTPUT_LINES_DEFAULT = 20
# Per-line character clip inside an excerpt, so one 1MB line cannot eat the
# whole budget before any total-size cap is applied.
TOOL_EXCERPT_LINE_CLIP = 300

__all__ = [
    "TOOL_EXCERPT_LINE_CLIP",
    "TOOL_FAIL_OUTPUT_LINES_DEFAULT",
    "TOOL_OUTPUT_LINES_DEFAULT",
    "clip_line",
    "excerpt_lines",
    "excerpt_output_lines",
]


def clip_line(text: str, limit: int) -> str:
    """Tail-truncate ``text`` to ``limit`` characters with an ellipsis."""
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 1 else "…"


def excerpt_lines(
    lines: list[str], max_lines: int, total: int, line_clip: int, *, tail_only: bool = False
) -> list[str]:
    """Head+tail selection of at most ``max_lines`` lines.

    Up to ``max_lines`` lines survive: everything, if the input already
    fits; otherwise the first ``max_lines // 2`` and the last
    ``max_lines - max_lines // 2``, with a ``… N lines elided …`` marker
    between them. ``tail_only`` (and a degenerate ``max_lines`` of 1)
    falls back to a plain tail, for callers whose output is a running log
    rather than an error report. ``total`` is the true line count of the
    original output -- which may exceed ``len(lines)`` when the text was
    clipped upstream -- so the marker reports what was really dropped.
    Every surviving line is right-stripped, clipped to ``line_clip``
    characters, and has any ``\\`\\`\\``` neutralised so an excerpt cannot
    break out of a caller's code fence.
    """
    if max_lines <= 0:
        return []
    if len(lines) <= max_lines:
        picked = list(lines)
        cut = len(picked)
    elif tail_only or max_lines == 1:
        picked = lines[-max_lines:]
        cut = 0
    else:
        head_n = max_lines // 2
        tail_n = max_lines - head_n
        picked = lines[:head_n] + lines[-tail_n:]
        cut = head_n
    omitted = max(0, total - len(picked))
    out = [clip_line(ln.rstrip(), line_clip).replace("```", "'''") for ln in picked]
    if omitted > 0:
        out = [*out[:cut], f"… {omitted} lines elided …", *out[cut:]]
    return out


def excerpt_output_lines(
    detail: str,
    *,
    max_lines: int,
    output_lines: int | None = None,
    line_clip: int = TOOL_EXCERPT_LINE_CLIP,
    tail_only: bool = False,
) -> list[str]:
    """Redact, split and excerpt raw tool output into display lines.

    ``detail`` is run through :func:`sbxloop.log.redact_text`, split into
    non-blank lines and reduced by :func:`excerpt_lines`. ``output_lines``
    is the event's true line count, used for the elided marker when the
    stored text was itself truncated upstream.
    """
    lines = [ln for ln in redact_text(str(detail or "")).strip().splitlines() if ln.strip()]
    total = max(len(lines), int(output_lines or 0))
    return excerpt_lines(lines, int(max_lines), total, line_clip, tail_only=tail_only)
