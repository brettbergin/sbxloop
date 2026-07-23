"""sdxloop-worker — the in-sandbox runtime for sdxloop.

Runs inside a Docker Sandbox microVM. Hosts the shared host/worker protocol
models, the agent backends (GitHub Copilot SDK), and the job runner invoked
via ``python -m sdxloop_worker``.
"""

__version__ = "0.1.3"

__all__ = ["__version__"]
