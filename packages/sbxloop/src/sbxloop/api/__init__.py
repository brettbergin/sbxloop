"""The remote operations API: REST under ``/v1``, served by the daemon.

The one package that imports FastAPI, and it does so lazily: an install
without the ``sbxloop[api]`` extra imports this module fine and learns by
name what it lacks when ``[api] enabled = true`` asks for the listener.
Everything the routes do goes through
:class:`~sbxloop.daemon.controls.service.ControlService` with a principal
authenticated here; no route composes a sentence a client would parse.
"""

from __future__ import annotations

import importlib.util

MISSING_EXTRA = (
    "the remote API needs the `sbxloop[api]` extra (fastapi, uvicorn, pyjwt); "
    "install it, or set `[api] enabled = false`"
)


def api_available() -> bool:
    """Whether the ``sbxloop[api]`` extra is installed."""
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("fastapi", "uvicorn", "jwt", "cryptography")
    )


def require_available() -> None:
    from sbxloop.errors import ConfigError

    if not api_available():
        raise ConfigError(MISSING_EXTRA)
