"""Workspace sanity: both distributions import and stay in version lockstep."""

import sdxloop
import sdxloop_worker


def test_versions_are_lockstep() -> None:
    assert sdxloop.__version__ == sdxloop_worker.__version__


def test_version_shape() -> None:
    parts = sdxloop.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
