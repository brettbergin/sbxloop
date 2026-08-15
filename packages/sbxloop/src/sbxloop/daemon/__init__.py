"""``sbxloop daemon`` — the always-on outer loop around the run engine.

Discovers work (GitHub issues by label, inbox ``.md`` files), runs each item
through a fresh :class:`~sbxloop.engine.engine.LoopEngine`, reports back to
the source, mirrors the chronology to Discord, and keeps going.
"""

from sbxloop.daemon.model import RunReport, TickResult, WorkItem

__all__ = ["RunReport", "TickResult", "WorkItem"]
