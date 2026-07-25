"""sbxloop — agentic loop orchestration on Docker Sandboxes (sbx).

Every sbxloop operation runs as a pair of isolated microVM sandboxes: an agent
sandbox holding only ``COPILOT_GITHUB_TOKEN`` for the GitHub Copilot SDK
agentic layer, and a GitHub-ops sandbox holding only ``GH_TOKEN`` for
user-facing GitHub interactions. The balanced network policy is the default.
"""

__version__ = "0.4.0"

from sbxloop.config import Budgets, Config, load_config
from sbxloop.engine import LoopEngine, RunResult, run_outcome
from sbxloop.events import Event, EventBus, Hook

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
