"""The typed operator controls beneath every surface.

``daemon/control.py`` is the *prose* edge: it parses a line of chat or a
``ctl`` argument list and renders a sentence back. Everything it does to
the daemon goes through :class:`~sbxloop.daemon.controls.service.ControlService`,
which takes a :class:`~sbxloop.daemon.controls.principal.Principal` and
returns a typed outcome or raises a typed
:class:`~sbxloop.daemon.controls.results.ControlError`. A surface that
speaks JSON rather than prose (a remote API) calls the service directly
and never sees a sentence it would have to parse.

The split follows the remote API spike's first delivery stage: structured
command results, a principal distinct from a free-form attribution string,
eligibility that fails closed on "could not tell", and prose kept at the
CLI/chat boundary.
"""

from __future__ import annotations

from sbxloop.daemon.controls.principal import (
    ALL_CAPABILITIES,
    CAPABILITIES,
    WORKSPACE_ID,
    Capability,
    Principal,
)
from sbxloop.daemon.controls.results import ControlError, ErrorCode
from sbxloop.daemon.controls.service import ControlService

__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITIES",
    "WORKSPACE_ID",
    "Capability",
    "ControlError",
    "ControlService",
    "ErrorCode",
    "Principal",
]
