from __future__ import annotations

from pathlib import Path

import pytest
from worker_harness import WorkerHarness


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness(tmp_path)
