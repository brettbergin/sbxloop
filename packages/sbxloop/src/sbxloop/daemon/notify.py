"""One message into the daemon's control channel, from the host, without
the daemon (#639).

The deploy pipeline used to post its notices by sourcing the daemon's
``secrets.env`` in a CI step, ``sed``-parsing ``sbxloop.toml`` for a
``channel_id`` and curling Discord's REST API by hand — Discord-only, and
CI reading the daemon's secrets file. ``sbxloop daemon notify "<text>"``
replaces that: it reads the *configured* ``[chat] backend`` and its
section, takes the bot token from the environment exactly as the bridge
does, and posts once over the service's plain HTTPS API. No gateway, no
socket, no SDK — stdlib ``urllib`` only, so it works on a host that has
neither chat extra installed and, above all, while the daemon is down:
the notice that matters most ("rollback also failed") is sent when
nothing else is running.

Text is written in the house dialect (Discord-flavoured Markdown, the
same one every bridge message is shaped in) and re-dialected for Slack
by :func:`~sbxloop.daemon.slack_format.to_mrkdwn`, so a caller never
knows which service is behind the channel. Links never unfurl and
mentions never ping: Discord gets ``SUPPRESS_EMBEDS`` and
``allowed_mentions: {parse: []}``, Slack ``unfurl_links: false`` with user
mentions escaped — the same rules the bridges apply to agent prose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple
from urllib.request import Request

from sbxloop.config import Config
from sbxloop.daemon.slack_format import to_mrkdwn
from sbxloop.errors import SbxloopError

# The bridges' token names, restated here so this module never imports a
# bridge module (each pulls its SDK in).
DISCORD_TOKEN_ENV = "DISCORD_BOT_TOKEN"  # nosec B105 - env var name, not a secret
SLACK_TOKEN_ENV = "SLACK_BOT_TOKEN"  # nosec B105 - env var name, not a secret

DISCORD_MESSAGES_URL = "https://discord.com/api/v10/channels/{channel_id}/messages"
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
# Both services cap a message well under this; a longer notice is the
# caller's mistake, reported before any request goes out.
MAX_CHARS = 2000
_MAX_REPLY_BYTES = 1 << 16

OpenUrl = Callable[[Request, float], bytes]


class Posted(NamedTuple):
    """Where a notice landed."""

    backend: str
    channel_id: str


def _open_url(request: Request, timeout_s: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310 - https only
        return bytes(response.read(_MAX_REPLY_BYTES + 1))


def post_notice(
    config: Config,
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
    open_url: OpenUrl | None = None,
) -> Posted:
    """Post ``text`` to the configured control channel; returns where it
    went. Raises :class:`SbxloopError` when the daemon is headless, the
    backend's bot token is unset, the text is too long, or the service
    refuses — every case names what to fix."""
    env = os.environ if env is None else env
    opener = open_url or _open_url
    backend = config.chat_backend
    if backend is None:
        raise SbxloopError(
            "no chat backend is configured — set [chat] backend (or a [discord] / [slack] "
            "channel_id) to post notices"
        )
    text = text.strip()
    if not text:
        raise SbxloopError("nothing to post: the notice text is empty")
    if len(text) > MAX_CHARS:
        raise SbxloopError(f"notice is {len(text)} characters; the limit is {MAX_CHARS}")
    section = config.chat_section(backend)
    channel_id = section.channel_ref
    token_env = DISCORD_TOKEN_ENV if backend == "discord" else SLACK_TOKEN_ENV
    token = env.get(token_env, "")
    if not token:
        raise SbxloopError(
            f'[chat] backend = "{backend}" but {token_env} is not set; export it (or put it '
            "in the project .env) — never in sbxloop.toml"
        )
    if backend == "discord":
        request = Request(
            DISCORD_MESSAGES_URL.format(channel_id=channel_id),
            # flags 4 = SUPPRESS_EMBEDS: no link preview, as the bridge sends.
            data=json.dumps(
                {"content": text, "allowed_mentions": {"parse": []}, "flags": 4}
            ).encode(),
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            method="POST",
        )
        _send(opener, request, timeout_s, backend)
    else:
        request = Request(
            SLACK_POST_MESSAGE_URL,
            data=json.dumps(
                {
                    "channel": channel_id,
                    "text": to_mrkdwn(text),
                    "unfurl_links": False,
                    "unfurl_media": False,
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        body = _send(opener, request, timeout_s, backend)
        # Slack answers 200 to a refused post; the verdict is in the body.
        _check_slack_reply(body)
    return Posted(backend, channel_id)


def _send(opener: OpenUrl, request: Request, timeout_s: float, backend: str) -> bytes:
    try:
        body = opener(request, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        if exc.code in (401, 403):
            detail += " — the bot token is invalid, or the bot is not in the channel"
        elif exc.code == 404:
            detail += " — no such channel for this bot"
        raise SbxloopError(f"posting to {backend} failed: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SbxloopError(f"posting to {backend} failed: {exc}") from exc
    if len(body) > _MAX_REPLY_BYTES:
        raise SbxloopError(f"posting to {backend} failed: reply exceeds {_MAX_REPLY_BYTES} bytes")
    return body


def _check_slack_reply(body: bytes) -> None:
    try:
        data: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SbxloopError("posting to slack failed: the reply is not JSON") from exc
    if not isinstance(data, dict) or not data.get("ok", False):
        error = data.get("error", "unknown error") if isinstance(data, dict) else "unknown error"
        hint = {
            "not_in_channel": " — invite the app to the channel",
            "channel_not_found": " — check [slack] channel_id",
            "invalid_auth": " — check SLACK_BOT_TOKEN",
        }.get(str(error), "")
        raise SbxloopError(f"posting to slack failed: {error}{hint}")


__all__ = ["Posted", "post_notice"]
