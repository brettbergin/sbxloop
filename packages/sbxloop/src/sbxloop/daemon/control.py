"""One command dispatcher for every operator surface, plus a local control
queue so scripts can drive the daemon without Discord.

Field evidence (#232): during a spiraling run the only ways to stop the
daemon were a human typing ``!sbx cancel`` in Discord or a signal to the
process — a remote-control script posting from the bot's own token was
(correctly) ignored, because the bridge drops bot-authored messages to
prevent echo loops. Rather than weaken that filter, this module gives the
daemon a programmatic path: ``sbxloop daemon ctl <cmd>`` drops a request
into ``state_dir/daemon/ctl/`` and the running daemon answers it. Discord's
``!sbx`` and ``ctl`` both go through :func:`dispatch`, so the two surfaces
cannot drift.

The queue is files, not a socket: it needs no new dependency, works
identically under the test harness and systemd, survives the sandbox path
length limits a unix socket has, and its semantics are inspectable with
``ls``. Requests are only served by a *live* daemon — a request that
predates the daemon's start is answered with a refusal, never executed,
because a stale ``cancel``/``pause`` firing at boot is exactly the surprise
an operator does not want.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

from sbxloop.daemon.discord_format import code, items_lines, queue_lines

logger = logging.getLogger(__name__)

# Verbs both surfaces accept, in usage-line form. The item-level controls
# (#229) and `cancel --retry` (#246) live here too, so Discord and ctl gain
# them together instead of drifting.
COMMANDS: tuple[str, ...] = (
    "status",
    "pause",
    "resume",
    "cancel [--retry]",
    "queue",
    "items",
    "abandon <item> [reason]",
    "retry <item>",
    "requeue <item>",
)
# Verbs that may talk to the source (GitHub through the ops sandbox —
# seconds, not milliseconds); a surface with an event loop to protect runs
# these off it.
ITEM_COMMANDS: frozenset[str] = frozenset({"abandon", "retry", "requeue"})

CTL_SUBDIR = Path("daemon") / "ctl"
_REQUEST_SUFFIX = ".json"
# A request the server has taken but not yet answered. The rename is atomic,
# so a client that times out learns exactly which side owns the command:
# request still there -> withdraw it; gone -> the daemon is executing it and
# a withdrawal would be a lie (the abandon of a GitHub item still lands).
_CLAIMED_SUFFIX = ".claimed.json"
_REPLY_SUFFIX = ".reply.json"
# A reply nobody collected (client killed mid-wait) is swept after this.
_REPLY_TTL_S = 300.0


class CommandReply(NamedTuple):
    """What a command produced. ``text`` is Discord-flavoured markdown
    (bold/code spans) — the CLI strips it; ``status`` carries the raw
    status dict so Discord can render its embed card."""

    text: str
    ok: bool = True
    status: dict[str, Any] | None = None
    known: bool = True  # False: the verb was not recognised (text is the usage line)
    # True: the daemon took the request but had not answered when the client
    # stopped waiting — the command is executing, its outcome is unknown.
    pending: bool = False


# Item verbs talk to GitHub through the ops sandbox (#229): a live `abandon`
# was measured in seconds, so a one-digit wait made a healthy daemon look
# absent. 30s covers a cold ops-sandbox exec with margin.
DEFAULT_TIMEOUT_S = 30.0


def usage(prefix: str) -> str:
    return f"commands: `{prefix} {'|'.join(COMMANDS)}`"


def dispatch(loop: Any, cmd: str, *, prefix: str = "!sbx", by: str | None = None) -> CommandReply:
    """Run one operator command against the daemon loop.

    ``loop`` is a :class:`~sbxloop.daemon.loop.DaemonLoop` (or a fake with
    the same control surface); ``cmd`` is the text after the prefix;
    ``prefix`` is only echoed in the usage line; ``by`` names the operator
    for the source-facing attribution of cancel/retry (#246).
    """
    words = cmd.split()
    word = words[0].lower() if words else ""
    args = words[1:]
    if word == "status":
        s = loop.status()
        cur = s["current"]
        lines = [
            f"**current:** {cur['run_id']} — {cur['title']}" if cur else "**current:** idle",
            f"**queued:** {s['queued']} · **runs today:** "
            f"{s['runs_today']}/{s['max_runs_per_day']}"
            f" (resumes {s.get('resumes_today', 0)})",
            f"**breaker:** {'open' if s['breaker_open'] else 'closed'} · **paused:** {s['paused']}",
        ]
        return CommandReply("\n".join(lines), status=s)
    if word == "pause":
        loop.pause()
        return CommandReply("paused — the current run finishes; nothing new is claimed.")
    if word in ("resume", "unpause"):
        loop.unpause()
        return CommandReply("resumed.")
    if word == "cancel":
        # Attributed to the operator: the item is settled as cancelled (no
        # retry, no breaker count) unless --retry asks for a fresh run.
        unknown = [a for a in args if a != "--retry"]
        if unknown:
            # A typo (`--rety`) must not silently become a terminal no-retry
            # cancel: the two outcomes differ materially.
            return CommandReply(
                f"unknown cancel argument {code(' '.join(unknown))}; usage: "
                f"`{prefix} cancel [--retry]`",
                ok=False,
            )
        retry = "--retry" in args
        if not loop.cancel_current(by, retry=retry):
            return CommandReply("nothing is running.", ok=False)
        if retry:
            return CommandReply(
                "cancel requested — honored at the next task boundary; the item will be "
                "re-queued and run again fresh."
            )
        return CommandReply(
            "cancel requested — honored at the next task boundary; the item settles as "
            "cancelled (no retry) and the run stays resumable."
        )
    if word == "queue":
        return CommandReply(queue_lines(loop.dstore.queued()))
    if word == "items":
        return CommandReply(items_lines(loop.dstore.items()))
    if word in ITEM_COMMANDS:
        return _item_command(loop, word, args, by)
    return CommandReply(usage(prefix), ok=False, known=False)


def _item_command(loop: Any, word: str, args: list[str], by: str | None) -> CommandReply:
    """``abandon|retry|requeue <item_id> [reason…]``. Item ids are the
    daemon's own (``gh:12``, ``inbox:x.md``); the store rejects bad
    transitions with a message worth showing verbatim. A retry is
    attributed to the operator on the source."""
    if not args:
        return CommandReply(
            f"usage: {word} <item_id>" + (" [reason]" if word == "abandon" else ""), ok=False
        )
    item_id = args[0]
    try:
        if word == "abandon":
            item = loop.abandon_item(item_id, " ".join(args[1:]) or None)
            return CommandReply(
                f"{code(item_id)} abandoned"
                + (f" (its run {code(item.run_id)} will not resume)" if item.run_id else "")
                + "."
            )
        if word == "retry":
            loop.retry_item(item_id, by)
            return CommandReply(f"{code(item_id)} re-queued with attempts reset (fresh plan).")
        loop.requeue_item(item_id)
        return CommandReply(f"{code(item_id)} re-queued; its next dispatch starts a fresh run.")
    except (KeyError, ValueError) as exc:
        return CommandReply(f"{word} failed: {exc.args[0] if exc.args else exc}", ok=False)


def plain(text: str) -> str:
    """Strip the Discord markdown the dispatcher emits for terminal output."""
    return text.replace("**", "").replace("`", "")


# -- file-based control queue -----------------------------------------------------


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _operator() -> str:
    """Who to attribute a ctl cancel/retry to on the source (#246) — the
    login name when the OS knows it, so a GitHub comment reads
    "cancelled by brett via sbxloop daemon ctl" rather than "operator"."""
    try:
        return f"{getpass.getuser()} via sbxloop daemon ctl"
    except Exception:  # no passwd entry / no controlling identity (containers)
        return "sbxloop daemon ctl"


class ControlClient:
    """The ``sbxloop daemon ctl`` side: submit a request, wait for the reply."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir / CTL_SUBDIR

    def submit(self, cmd: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> CommandReply | None:
        """Returns None when no daemon *took* the request within
        ``timeout_s`` — it is withdrawn so a daemon started later does not
        act on it (module docstring). Returns a ``pending`` reply when the
        daemon claimed it but had not answered in time: the command is
        running (item verbs cross the ops sandbox) and cannot be withdrawn,
        so the caller must not report "not executed"."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # Time-prefixed so the server serves requests in submission order.
        req_id = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}"
        request = self.dir / f"{req_id}{_REQUEST_SUFFIX}"
        reply = self.dir / f"{req_id}{_REPLY_SUFFIX}"
        # The prefix only shapes the usage line, so `ctl bogus` names the
        # CLI, not Discord's `!sbx`.
        _write_atomic(
            request,
            {
                "cmd": cmd,
                "prefix": "sbxloop daemon ctl",
                "by": _operator(),
                "submitted_at": time.time(),
            },
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if reply.exists():
                try:
                    data = json.loads(reply.read_text())
                finally:
                    reply.unlink(missing_ok=True)
                return CommandReply(str(data.get("text", "")), bool(data.get("ok", True)))
            time.sleep(0.05)
        try:
            request.unlink()
        except FileNotFoundError:
            # Claimed (or already answered between our last poll and now).
            if reply.exists():
                data = json.loads(reply.read_text())
                reply.unlink(missing_ok=True)
                return CommandReply(str(data.get("text", "")), bool(data.get("ok", True)))
            return CommandReply(
                f"the daemon took `{cmd}` but has not answered within {timeout_s:g}s; "
                "it is still executing (item verbs go through the ops sandbox) — "
                "check `sbxloop daemon ctl items`.",
                ok=False,
                pending=True,
            )
        return None


class ControlServer:
    """The daemon side: a thread that polls the queue and answers requests.

    Runs on its own thread — not in the tick — because ``tick()`` blocks
    for the whole duration of a run, and ``cancel`` is precisely the
    command that must land mid-run.
    """

    def __init__(self, loop: Any, state_dir: Path, *, poll_s: float = 0.5) -> None:
        self.loop = loop
        self.dir = state_dir / CTL_SUBDIR
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._refuse_stale()
        self._thread = threading.Thread(target=self._main, name="sbxloop-daemon-ctl", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _requests(self) -> list[Path]:
        try:
            names = sorted(p for p in self.dir.iterdir())
        except FileNotFoundError:
            return []
        return [
            p
            for p in names
            if p.name.endswith(_REQUEST_SUFFIX)
            and not p.name.endswith((_REPLY_SUFFIX, _CLAIMED_SUFFIX))
        ]

    def _refuse_stale(self) -> None:
        # A claim left by a daemon that died mid-command: its client has long
        # since reported "pending"; nothing to answer, nothing to replay.
        for p in self.dir.glob(f"*{_CLAIMED_SUFFIX}"):
            p.unlink(missing_ok=True)
        for request in self._requests():
            self._answer(
                request,
                CommandReply(
                    "ignored: this request was submitted before the daemon started; resend it.",
                    ok=False,
                ),
            )

    def _main(self) -> None:
        while not self._stop.is_set():
            self.serve_once()
            self._stop.wait(self.poll_s)

    def serve_once(self) -> int:
        """Answer every pending request; returns how many were served."""
        served = 0
        for request in self._requests():
            claimed = request.with_name(request.name[: -len(_REQUEST_SUFFIX)] + _CLAIMED_SUFFIX)
            try:
                request.rename(claimed)
                payload = json.loads(claimed.read_text())
            except (OSError, ValueError):
                # Withdrawn between listing and claiming, or half-written.
                claimed.unlink(missing_ok=True)
                continue
            request = claimed
            cmd = str(payload.get("cmd", ""))
            try:
                reply = dispatch(
                    self.loop,
                    cmd,
                    prefix=str(payload.get("prefix", "!sbx")),
                    by=str(payload.get("by") or "sbxloop daemon ctl"),
                )
            except Exception as exc:  # a broken command must not kill the server
                logger.warning("ctl command %r failed", cmd, exc_info=True)
                reply = CommandReply(f"error: {exc}", ok=False)
            self._answer(request, reply)
            served += 1
        self._sweep_replies()
        return served

    def _answer(self, request: Path, reply: CommandReply) -> None:
        stem = request.name.removesuffix(_CLAIMED_SUFFIX).removesuffix(_REQUEST_SUFFIX)
        reply_path = request.with_name(stem + _REPLY_SUFFIX)
        _write_atomic(reply_path, {"ok": reply.ok, "text": reply.text, "answered_at": time.time()})
        request.unlink(missing_ok=True)

    def _sweep_replies(self) -> None:
        cutoff = time.time() - _REPLY_TTL_S
        try:
            for p in self.dir.iterdir():
                if p.name.endswith(_REPLY_SUFFIX) and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
