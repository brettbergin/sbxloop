"""Locate (or build) the sbxloop-worker wheel to install into sandboxes.

Resolution order:

1. **Vendored** — the wheel embedded in the installed sbxloop package at
   ``sbxloop/_vendor/`` (placed there by the hatch build hook). This is the
   production path and works with zero network access to PyPI for sbxloop
   itself.
2. **Workspace build** — when running from a source checkout (no vendor dir),
   build the wheel from a private temp copy of the sibling
   ``packages/sbxloop-worker`` tree with ``uv build``, pinned to the host's
   version. Cached per process.
3. **None** — the caller falls back to installing ``sbxloop-worker`` from
   PyPI inside the sandbox at the exact lockstep version.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from importlib import resources
from pathlib import Path

import sbxloop

logger = logging.getLogger(__name__)


def _vendored_wheel() -> Path | None:
    expected = f"sbxloop_worker-{sbxloop.__version__}-py3-none-any.whl"
    try:
        vendor = resources.files("sbxloop") / "_vendor"
        candidate = Path(str(vendor)) / expected
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None
    return candidate if candidate.is_file() else None


def _workspace_worker_root() -> Path | None:
    # <repo>/packages/sbxloop/src/sbxloop/__init__.py -> <repo>/packages/sbxloop-worker
    package_dir = Path(sbxloop.__file__).resolve().parent
    candidate = package_dir.parent.parent.parent / "sbxloop-worker"
    return candidate if (candidate / "pyproject.toml").is_file() else None


@lru_cache(maxsize=1)
def _workspace_build() -> Path | None:
    worker_root = _workspace_worker_root()
    uv = shutil.which("uv")
    if worker_root is None or uv is None:
        return None
    # Build from a private copy with the version pinned to the host's.
    # Building the live tree in place would rewrite its hatch-vcs
    # _version.py mid-build — shared state every concurrently spawned
    # worker subprocess imports (pytest-xdist runs many at once) — and a
    # git HEAD that moved since the environment was synced would produce a
    # wheel whose version fails the install ladder's lockstep check.
    build_root = Path(tempfile.mkdtemp(prefix="sbxloop-worker-wheel-"))
    src_copy = build_root / "sbxloop-worker"
    out_dir = build_root / "dist"
    try:
        shutil.copytree(
            worker_root,
            src_copy,
            ignore=shutil.ignore_patterns("__pycache__", ".*", "dist", "tests"),
        )
        env = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": sbxloop.__version__}
        subprocess.run(  # nosec B603 - fixed argv, absolute uv path
            [uv, "build", "--wheel", "-o", str(out_dir), str(src_copy)],
            check=True,
            capture_output=True,
            timeout=300,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("workspace worker wheel build failed: %s", exc)
        return None
    wheels = sorted(out_dir.glob("sbxloop_worker-*.whl"))
    return wheels[-1] if wheels else None


def resolve_worker_wheel() -> Path | None:
    """Best available worker wheel on this host, or None to use PyPI."""
    wheel = _vendored_wheel()
    if wheel is not None:
        return wheel
    return _workspace_build()
