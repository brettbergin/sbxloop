"""sbxloop-worker — the in-sandbox runtime for sbxloop.

Runs inside a Docker Sandbox microVM. Hosts the shared host/worker protocol
models, the agent backends (GitHub Copilot SDK), and the job runner invoked
via ``python -m sbxloop_worker``.
"""

try:
    # written at build time by hatch-vcs (see pyproject [tool.hatch.build.hooks.vcs])
    from sbxloop_worker._version import __version__
except ImportError:  # pragma: no cover - raw source tree that was never built
    try:
        from importlib.metadata import version as _pkg_version

        __version__ = _pkg_version("sbxloop-worker")
    except Exception:
        __version__ = "0.0.0"

__all__ = ["__version__"]
