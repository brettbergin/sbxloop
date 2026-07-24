"""Worker wheel resolution tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import sbxloop
from sbxloop.worker import wheel as wheel_mod


@pytest.fixture(autouse=True)
def clear_build_cache() -> None:
    wheel_mod._workspace_build.cache_clear()


def test_vendored_wheel_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = Path(sbxloop.__file__).parent / "_vendor"
    vendor.mkdir(exist_ok=True)
    name = f"sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
    target = vendor / name
    try:
        target.write_bytes(b"wheel")
        resolved = wheel_mod.resolve_worker_wheel()
        assert resolved is not None
        assert resolved.name == name
    finally:
        target.unlink(missing_ok=True)


def test_workspace_build_fallback() -> None:
    # No vendored wheel in a source checkout -> build from the sibling tree.
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
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
