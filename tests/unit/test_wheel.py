"""Worker wheel resolution tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import sbxloop
from sbxloop.worker import wheel as wheel_mod


@pytest.fixture(autouse=True)
def clear_build_cache() -> None:
    wheel_mod._workspace_build.cache_clear()


def test_vendored_wheel_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point resource resolution at a tmp package dir rather than planting a
    # fake wheel in the real installed package: that path is shared global
    # state, and other tests (the build hook, real pip installs) resolve it
    # concurrently under pytest-xdist.
    vendor = tmp_path / "_vendor"
    vendor.mkdir()
    name = f"sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
    (vendor / name).write_bytes(b"wheel")
    monkeypatch.setattr(
        wheel_mod, "resources", SimpleNamespace(files=lambda package: tmp_path)
    )
    resolved = wheel_mod.resolve_worker_wheel()
    assert resolved is not None
    assert resolved.name == name


def test_workspace_build_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # No vendored wheel in a source checkout -> build from the sibling tree.
    # Force the no-vendor branch: editable installs may leave a real vendored
    # wheel in the source tree, which would short-circuit the build path.
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    monkeypatch.setattr(wheel_mod, "_vendored_wheel", lambda: None)
    resolved = wheel_mod.resolve_worker_wheel()
    assert resolved is not None
    assert resolved.name.startswith("sbxloop_worker-")
    assert resolved.name.endswith(".whl")
    assert sbxloop.__version__ in resolved.name


def test_pypi_fallback_when_nothing_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wheel_mod, "_vendored_wheel", lambda: None)
    monkeypatch.setattr(wheel_mod, "_workspace_worker_root", lambda: None)
    assert wheel_mod.resolve_worker_wheel() is None


def test_workspace_build_handles_uv_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(wheel_mod, "_vendored_wheel", lambda: None)

    def boom(*args: object, **kwargs: object) -> object:
        raise subprocess.CalledProcessError(1, ["uv"], stderr=b"nope")

    monkeypatch.setattr(wheel_mod.subprocess, "run", boom)
    assert wheel_mod.resolve_worker_wheel() is None
