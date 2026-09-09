"""The ``load_skill`` host tool: the door every backend gets to the skills.

Backends differ in what they can load from a filesystem, and one of the
sessions that most needs a procedure — the concierge — runs with no built-in
tools at all (``available_tools=[]``), so it has no reader to open a file
with. A host tool is therefore the floor rather than the fallback: it is the
one mechanism that works identically on every backend and in every session
shape, and because the host answers each call, every load is auditable.

The catalogue in the prompt lists what a role may load; this tool returns
one body. A name that is not in the caller's catalogue is refused rather
than silently answered, so the tool cannot be used to read a skill written
for a different role.
"""

from __future__ import annotations

from sbxloop.engine.harness import Role
from sbxloop.log import get_logger
from sbxloop.skills import skill_body, skills_for
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec

__all__ = ["SKILL_TOOL_NAME", "answer_skill_call", "skill_tool_spec"]

log = get_logger(__name__)

SKILL_TOOL_NAME = "load_skill"


def skill_tool_spec(role: Role) -> HostToolSpec | None:
    """The tool as a session running as ``role`` sees it, or None when the
    role has no skills (the tool would only be a dead end)."""
    skills = skills_for(role)
    if not skills:
        return None
    names = [skill.name for skill in skills]
    listing = "; ".join(f"{skill.name}: {skill.description}" for skill in skills)
    return HostToolSpec(
        name=SKILL_TOOL_NAME,
        description=(
            "Load one of this loop's skills: a short procedure written for the "
            "situation you are in. Call it when you are about to do something the "
            "catalogue names and you want the procedure rather than your own guess "
            "at it. Returns the skill's full text. Available: " + listing + "."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "enum": names}},
            "required": ["name"],
        },
    )


def answer_skill_call(call: HostToolCall, role: Role) -> HostToolResponse:
    """Answer one ``load_skill`` call for a session running as ``role``.

    Every outcome is an answer, never an exception: a model that asked for
    a skill must always learn something it can act on, and the names it may
    ask for are already an enum on the tool.
    """
    name = str(call.arguments.get("name") or "").strip()
    allowed = {skill.name for skill in skills_for(role)}
    if name not in allowed:
        offered = ", ".join(sorted(allowed)) or "(none)"
        return HostToolResponse(
            call_id=call.call_id,
            ok=False,
            error=f"no skill {name!r} for this session; available: {offered}",
        )
    body = skill_body(name)
    if body is None:  # pragma: no cover - defended, unreachable via the enum
        return HostToolResponse(call_id=call.call_id, ok=False, error=f"skill {name!r} is empty")
    log.info("skill.loaded", skill=name, role=role)
    return HostToolResponse(call_id=call.call_id, ok=True, text=body)
