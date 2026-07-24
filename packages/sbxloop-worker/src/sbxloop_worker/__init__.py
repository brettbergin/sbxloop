"""sbxloop-worker — the in-sandbox runtime for sbxloop.

Runs inside a Docker Sandbox microVM. Hosts the shared host/worker protocol
models, the agent backends (GitHub Copilot SDK), and the job runner invoked
via ``python -m sbxloop_worker``.
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
