"""``sbxloop tui``: the operator console.

A Textual application run on the daemon host. It reads the daemon's
``state.db`` through the read-only :class:`~sbxloop.daemon.mailbox.MailboxClient`,
drives the daemon through the same ``ctl`` file queue every other operator
surface uses, and (from the chat screens) speaks to the daemon's local chat
bridge. Nothing here imports Textual at ``import sbxloop`` time: the CLI
imports :mod:`sbxloop.tui.app` only when the command runs.
"""

from __future__ import annotations

__all__: list[str] = []
