"""Workspace sanity: both distributions import and stay in version lockstep."""

import sbxloop
import sbxloop_worker


def test_versions_are_lockstep() -> None:
    assert sbxloop.__version__ == sbxloop_worker.__version__


def test_version_shape() -> None:
    parts = sbxloop.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
