"""Packaging integration: the built sbxloop wheel embeds the worker wheel."""

from __future__ import annotations

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
    subprocess.run(
        ["uv", "build", "--package", "sbxloop", "-o", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=300,
    )
    wheels = list(tmp_path.glob("sbxloop-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    expected = f"sbxloop/_vendor/sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
    assert expected in names
