"""Hatch build hook: embed the sbxloop-worker wheel into the host package.

Provisioning must be able to install the worker into a sandbox even when
sbxloop-worker is not (yet) on PyPI, so the host wheel ships the worker wheel
as package data at ``sbxloop/_vendor/``.

Runs for both sdist and wheel targets. When building the wheel from an sdist
(as ``uv build`` does), the worker source tree is not available — but the
sdist already contains the vendored wheel built during the sdist pass, so the
hook detects it and skips rebuilding.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class VendorWorkerWheelHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "vendor-worker-wheel"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        vendor_dir = root / "src" / "sbxloop" / "_vendor"
        host_version = self.metadata.version
        expected = f"sbxloop_worker-{host_version}-py3-none-any.whl"

        # The static dependency list can't carry an exact pin now that versions
        # come from git tags (hatch-vcs), so add the lockstep pin to the wheel
        # metadata here.
        if self.target_name == "wheel":
            build_data.setdefault("dependencies", []).append(f"sbxloop-worker=={host_version}")

        if (vendor_dir / expected).is_file():
            return

        worker_root = root.parent / "sbxloop-worker"
        if not (worker_root / "pyproject.toml").is_file():
            raise RuntimeError(
                f"cannot vendor worker wheel: {expected} not present and worker source "
                f"not found at {worker_root}"
            )

        vendor_dir.mkdir(parents=True, exist_ok=True)
        for stale in vendor_dir.glob("*.whl"):
            stale.unlink()

        subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(vendor_dir)],
            cwd=worker_root,
            check=True,
            capture_output=True,
        )

        if not (vendor_dir / expected).is_file():
            built = [p.name for p in vendor_dir.glob("*.whl")]
            raise RuntimeError(
                f"worker wheel version mismatch: expected {expected}, built {built}. "
                "sbxloop and sbxloop-worker versions must stay in lockstep."
            )
