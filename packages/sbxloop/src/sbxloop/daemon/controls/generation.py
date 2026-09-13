"""The daemon generation: one id per process that owns execution.

A command is claimed under the generation of the daemon that took it. A
process that comes back after a crash cannot tell, from an operation row
alone, whether the effect it promised happened; what it can tell is that
the row was claimed by a generation that is not its own, and reconcile it
from the evidence the domain left behind. The id is stamped in
``daemon_state`` on recovery so a reader outside the process can see which
generation is answering.
"""

from __future__ import annotations

from sbxloop.ids import _token

#: ``daemon_state`` keys the generation is stamped under.
GENERATION_KEY = "daemon.generation"
GENERATION_STARTED_KEY = "daemon.generation_started_at"


def new_generation_id() -> str:
    return "g" + _token(10)
