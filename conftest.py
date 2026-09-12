"""Root conftest: the ``slow`` marker and ``--shard`` (#750).

Lives at the root rather than in ``tests/`` so the option exists for both
test trees (``testpaths``) and for a run that names only the worker tests.
"""

from __future__ import annotations

import zlib

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="run only the I-th of N deterministic slices of the collected tests "
        "(1-based); CI spreads the slow marker over runners this way",
    )


def _shard(spec: str) -> tuple[int, int]:
    try:
        index, count = (int(part) for part in spec.split("/", 1))
    except ValueError:
        raise pytest.UsageError(f"--shard expects I/N, got {spec!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise pytest.UsageError(f"--shard {spec}: need 1 <= I <= N")
    return index, count


# tryfirst: the marker must be on the item before pytest's own ``-m``
# filter (a later-registered builtin hook) looks for it.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Every test on the fake sbx is process-bound: each ``sbx`` call is
    ``sh`` → interpreter → the fake, and a pipeline test makes ~50 of them
    plus real worker launches. Those ~950 tests carry the ``slow`` marker
    automatically, so the commit gate (``make test-fast`` / ``-m "not slow"``)
    runs the other ~5700 without them — about three minutes on four cores.
    CI still runs everything, both halves spread over runners with ``--shard``
    (#750).

    This hook is ``tryfirst`` so the marker lands before pytest's own ``-m``
    filter sees the item. That also means ``--shard`` cuts the whole
    collection and a ``-m`` selection falls out of the slice afterwards, so
    the two compose in either order: ``-m slow --shard 1/3`` and
    ``-m "not slow" --shard 1/2`` each partition their half exactly.

    Sharding hashes the node id (crc32, not the salted ``hash``) so a slice
    is the same on every runner and every push; a class is not kept together
    on purpose — per-test slices balance better and nothing a test builds
    is shared across its class."""
    for item in items:
        if "fake_sbx" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.slow)
    spec = config.getoption("--shard")
    if spec is None:
        return
    index, count = _shard(spec)
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    for item in items:
        (keep if zlib.crc32(item.nodeid.encode()) % count == index - 1 else drop).append(item)
    items[:] = keep
    config.hook.pytest_deselected(items=drop)
