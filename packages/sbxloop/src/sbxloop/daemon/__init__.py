"""``sbxloop daemon`` — the always-on outer loop around the run engine.

Discovers work (GitHub issues by label), runs each item through a fresh
:class:`~sbxloop.engine.engine.LoopEngine` that carries it all the way to
a merged pull request, settles the issue on how the run ended, mirrors the
chronology to chat (Discord or Slack), and keeps going. It never files work of its own.
"""

from sbxloop.daemon.model import DaemonNotice, RunReport, TickResult, WorkItem

__all__ = ["DaemonNotice", "RunReport", "TickResult", "WorkItem"]
