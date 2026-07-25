"""Packaging integration: the built sbxloop wheel embeds the worker wheel."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import sbxloop

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_built_wheel_embeds_worker_wheel(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    # Build in a private copy of the workspace, version pinned to the
    # host's. Building the live repo in place rewrites both packages'
    # hatch-vcs _version.py files and deletes/rebuilds the wheels in
    # src/sbxloop/_vendor/ mid-test — shared state that every other xdist
    # worker imports (worker subprocesses) and resolves
    # (resolve_worker_wheel) concurrently. _vendor is excluded from the
    # copy so the hook's build-the-worker path is always exercised.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(REPO_ROOT / name, workspace / name)
    ignore = shutil.ignore_patterns("__pycache__", ".*", "dist", "_vendor")
    for package in ("sbxloop", "sbxloop-worker"):
        shutil.copytree(
            REPO_ROOT / "packages" / package, workspace / "packages" / package, ignore=ignore
        )
    out_dir = tmp_path / "dist"
    env = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": sbxloop.__version__}
    subprocess.run(
        ["uv", "build", "--package", "sbxloop", "-o", str(out_dir)],
        cwd=workspace,
        check=True,
        capture_output=True,
        timeout=300,
        env=env,
    )
    wheels = list(out_dir.glob("sbxloop-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    expected = f"sbxloop/_vendor/sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
    assert expected in names
