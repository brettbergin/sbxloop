"""sdxloop — agentic loop orchestration on Docker Sandboxes (sbx).

Every sdxloop operation runs as a pair of isolated microVM sandboxes: an agent
sandbox holding only ``COPILOT_GITHUB_TOKEN`` for the GitHub Copilot SDK
agentic layer, and a GitHub-ops sandbox holding only ``GH_TOKEN`` for
user-facing GitHub interactions. The balanced network policy is the default.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
