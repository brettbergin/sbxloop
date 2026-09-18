"""``read_channel_artifact``: a file the channel can see, as text.

A run's files used to be reachable only by whoever could read the run. In a
channel with several people and several agents that is the wrong boundary:
the file a workload delivered *into the conversation* is part of what the
conversation knows, so anyone -- and any agent -- who can read the channel
can read it, and nothing else.

The tool resolves an artifact id only when it is attached to a message in
the turn's own channel, or produced by a run that channel admitted. It never
takes a path, so no traversal is possible; the bytes are opened through the
same directory-relative reader the download route uses, relative to the
run's own directory and never following a link out of it. Text comes back
UTF-8 with a truncation marker naming the next offset; anything that is not
text comes back as one metadata line, never as bytes the model would have to
guess at.

It is offered to every participant in a chat turn, read-only roles included
(a critic reviewing a file has to be able to read it), through the
:class:`~sbxloop.daemon.concierge.TurnContext` seam the memory tools use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sbxloop.agents.tools import AgentTool
from sbxloop.api.artifacts import Artifact
from sbxloop.errors import ToolRejectedError
from sbxloop.log import get_logger
from sbxloop_worker.protocol import HostToolSpec

if TYPE_CHECKING:
    from sbxloop.api.context import ApiContext

log = get_logger(__name__)

TOOL_NAME = "read_channel_artifact"
#: The most one call returns, and the default when the caller names no limit.
READ_LIMIT_MAX = 64_000
#: A file bigger than the window says where to carry on from.
_TRUNCATED = "\n\n[truncated at {read} of {size} bytes: call again with offset={offset}]"
#: Bytes past a decode failure that could be one split UTF-8 character.
_BOUNDARY = 3


def _artifact_id(args: Mapping[str, Any]) -> str:
    value = args.get("artifact_id")
    if not isinstance(value, str) or not value.strip():
        raise ToolRejectedError("artifact_id is required: take it from the file list on a message")
    return value.strip()[:64]


def _whole(args: Mapping[str, Any], name: str, default: int, *, low: int, high: int) -> int:
    # A backend that fills every optional parameter sends an omitted number
    # as an explicit null; that is the default, not a bad call.
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolRejectedError(f"{name} must be a whole number")
    return max(low, min(value, high))


def _as_text(chunk: bytes) -> str | None:
    """``chunk`` as UTF-8 text, or ``None`` when it is not text at all.

    A window cut mid-character is not binary, so up to three trailing bytes
    are given back to the next call rather than refused.
    """
    if b"\x00" in chunk:
        return None
    for trim in range(_BOUNDARY + 1):
        try:
            return chunk[: len(chunk) - trim].decode("utf-8") if trim else chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def attached(ctx: ApiContext, channel_id: str, artifact_id: str) -> Artifact | None:
    """The catalogued file, when a message in this channel carries it.

    What the channel's own routes serve: exactly the files its file list
    names, so ``GET .../artifacts`` and ``GET .../artifacts/{id}/content``
    cannot disagree. A run linked to the channel is deliberately not enough
    here -- a code run's whole checkout is catalogued and none of it is the
    channel's, so a channel reader holding no ``artifacts:read`` would
    otherwise reach it.
    """
    artifact = ctx.artifacts.get(artifact_id)
    if artifact is None:
        return None
    return artifact if ctx.collaboration.artifact_attached(channel_id, artifact.id) else None


def resolve(ctx: ApiContext, channel_id: str, artifact_id: str) -> Artifact | None:
    """The catalogued file, when this channel is allowed to see it.

    Wider than :func:`attached` by one case, and only for the in-turn tool:
    a run the channel itself admitted is part of its shared context before
    its result message lands, so an agent answering there can read what it
    produced. The file still has to be named by an id the conversation
    already carries.
    """
    artifact = ctx.artifacts.get(artifact_id)
    if artifact is None:
        return None
    store = ctx.collaboration
    if store.artifact_attached(channel_id, artifact.id):
        return artifact
    if store.channel_owns_run(channel_id, artifact.run_id):
        return artifact
    return None


def read_channel_artifact(ctx: ApiContext, channel_id: str, args: Mapping[str, Any]) -> str:
    """Answer one ``read_channel_artifact`` call for ``channel_id``."""
    from sbxloop.api.projections import Views

    artifact_id = _artifact_id(args)
    offset = _whole(args, "offset", 0, low=0, high=1 << 40)
    limit = _whole(args, "limit", READ_LIMIT_MAX, low=1, high=READ_LIMIT_MAX)
    artifact = resolve(ctx, channel_id, artifact_id)
    if artifact is None:
        raise ToolRejectedError(
            f"no file {artifact_id} in this conversation: "
            "only files delivered here can be read, by the id shown on the message"
        )
    if not artifact.available:
        raise ToolRejectedError(f"{artifact.relpath} is no longer on the host")
    record = Views(ctx).run_record(artifact.run_id)
    if record is None:
        raise ToolRejectedError(f"{artifact.relpath} is no longer on the host")
    try:
        with ctx.artifacts.open(record, artifact) as handle:
            handle.seek(offset)
            chunk = handle.read(limit)
    except OSError:
        log.info("api.channel_artifact_unreadable", artifact=artifact.id)
        raise ToolRejectedError(f"{artifact.relpath} could not be read") from None
    text = _as_text(chunk)
    if text is None:
        return (
            f"{artifact.relpath} is not text ({artifact.media_type}, {artifact.size} bytes). "
            "Ask the person for it, or work from the file list instead."
        )
    read = offset + len(text.encode("utf-8"))
    if read < artifact.size:
        text += _TRUNCATED.format(read=read, size=artifact.size, offset=read)
    return f"{artifact.relpath} ({artifact.media_type}, {artifact.size} bytes):\n{text}"


def channel_artifact_tools(ctx: ApiContext, channel_id: str) -> list[AgentTool]:
    """The channel's own read tool for a turn answering in ``channel_id``."""

    def read(args: Mapping[str, Any]) -> str:
        return read_channel_artifact(ctx, channel_id, args)

    return [
        AgentTool(
            HostToolSpec(
                name=TOOL_NAME,
                description=(
                    "Read a file that was delivered into this conversation, by the id shown "
                    "with the message that carried it. Text comes back as text; a long file "
                    "is cut and says the offset to carry on from. Files from other "
                    "conversations are not readable."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "artifact_id": {
                            "type": "string",
                            "description": "The file's id, as the message listed it.",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Byte to start at; 0 is the top of the file.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": READ_LIMIT_MAX,
                        },
                    },
                    "additionalProperties": False,
                    "required": ["artifact_id"],
                },
            ),
            read,
        )
    ]


__all__ = [
    "READ_LIMIT_MAX",
    "TOOL_NAME",
    "attached",
    "channel_artifact_tools",
    "read_channel_artifact",
    "resolve",
]
