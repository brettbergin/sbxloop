"""sdxloop — agentic loop orchestration on Docker Sandboxes (sbx).

Every sdxloop operation runs as a pair of isolated microVM sandboxes: an agent
sandbox holding only ``COPILOT_GITHUB_TOKEN`` for the GitHub Copilot SDK
agentic layer, and a GitHub-ops sandbox holding only ``GH_TOKEN`` for
user-facing GitHub interactions. The balanced network policy is the default.
"""

__version__ = "0.1.1"

from sdxloop.config import Budgets, Config, load_config
from sdxloop.engine import LoopEngine, RunResult, run_outcome
from sdxloop.events import Event, EventBus, Hook

__all__ = [
    "Budgets",
    "Config",
    "Event",
    "EventBus",
    "Hook",
    "LoopEngine",
    "RunResult",
    "__version__",
    "load_config",
    "run_outcome",
]
