"""The capability gate a scenario runs behind.

A scenario names what it relies on; the backend's report decides whether
the scenario runs, skips, or fails — and the distinction between the last
two is the whole point of the three-state capability."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pytest

from sbxloop.vcs.protocol import CAPABILITIES, Capability


def check_needs(kind: str, report: Mapping[str, Capability], needs: Iterable[str]) -> None:
    """Skip when ``kind`` reports a needed capability ``UNSUPPORTED``
    (naming it), fail when it reports ``UNKNOWN`` or nothing at all; a
    need no backend could ever report is a mistake in the scenario."""
    wanted = sorted(set(needs))
    unknown_names = [name for name in wanted if name not in CAPABILITIES]
    if unknown_names:
        pytest.fail(f"scenario needs capabilities no backend reports: {unknown_names}")
    for name in wanted:
        state = report.get(name)
        if state is Capability.UNSUPPORTED:
            pytest.skip(f"{kind}: {name} is unsupported")
        if state is not Capability.SUPPORTED:
            pytest.fail(f"{kind}: {name} is {state}; the backend could not decide")
