"""Spike #593: a credential-less workbench driven through host-brokered tools.

The proposed topology splits today's agent sandbox in two:

- a **brain** box that runs the model session and holds the inference
  credential, with no repo code and no built-in tools;
- a **workbench** box that holds the workspace and runs the repo's own
  toolchain, with **no credentials at all**.

Every operation the brain performs on the tree crosses the host: the session
calls a host tool (the concierge's existing ``host_tools`` mechanism), the
host executes it against the workbench via ``sbx exec``/``sbx cp``, and the
response file travels back. Both architecture invariants hold by
construction — no VM addresses the host or another VM, and everything
between the boxes passes a host policy checkpoint.

:class:`WorkbenchTools` is that checkpoint, spike-sized: path confinement to
the workbench workspace, a deny rule (``.git/`` writes), and a per-call
audit trail — the code-plane analogue of the ``policy.allow`` egress log.
"""

from __future__ import annotations

import posixpath
import shlex
import time
from dataclasses import dataclass, field

from sbxloop.sbx.sandbox import WORK_DIR, Sandbox
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec


@dataclass
class AuditEntry:
    """One brokered operation, as the host saw it."""

    tool: str
    detail: str
    ok: bool
    duration_s: float


@dataclass
class WorkbenchTools:
    """Host-side tools that let a brain-box session work a workbench box.

    The handler (:meth:`handle`) plugs straight into ``WorkerClient.submit``'s
    ``tool_handler`` seam; :meth:`specs` is the matching ``job.host_tools``
    payload. All paths are confined to ``root`` (the workbench workspace):
    absolute paths and ``..`` traversal are refused, as are writes under
    ``.git/`` — mechanical enforcement of rules today's builder observes only
    by prompt.
    """

    workbench: Sandbox
    root: str = WORK_DIR
    audit: list[AuditEntry] = field(default_factory=list)

    def specs(self) -> list[HostToolSpec]:
        path_param = {"type": "object", "properties": {"path": {"type": "string"}}}
        return [
            HostToolSpec(
                name="wb_read", description="read a workspace file", parameters=path_param
            ),
            HostToolSpec(
                name="wb_write",
                description="write a workspace file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                },
            ),
            HostToolSpec(name="wb_list", description="list workspace files", parameters=path_param),
            HostToolSpec(
                name="wb_shell",
                description="run a shell command in the workspace",
                parameters={"type": "object", "properties": {"cmd": {"type": "string"}}},
            ),
        ]

    # -- the HostToolHandler -----------------------------------------------

    def handle(self, call: HostToolCall) -> HostToolResponse:
        started = time.monotonic()
        try:
            text = self._dispatch(call)
            ok, error = True, None
        except _Refused as exc:
            text, ok, error = "", False, str(exc)
        entry = AuditEntry(
            tool=call.name,
            detail=str(call.arguments.get("path") or call.arguments.get("cmd") or "")[:120],
            ok=ok,
            duration_s=round(time.monotonic() - started, 4),
        )
        self.audit.append(entry)
        return HostToolResponse(call_id=call.call_id, ok=ok, text=text, error=error)

    def _dispatch(self, call: HostToolCall) -> str:
        if call.name == "wb_read":
            return self.workbench.read_text(self._resolve(call.arguments.get("path")))
        if call.name == "wb_write":
            path = self._resolve(call.arguments.get("path"), forbid_git=True)
            parent = posixpath.dirname(path)
            self.workbench.exec(["mkdir", "-p", parent])
            self.workbench.write_text(path, str(call.arguments.get("content", "")))
            return f"wrote {call.arguments.get('path')}"
        if call.name == "wb_list":
            path = self._resolve(call.arguments.get("path") or ".")
            result = self.workbench.exec(
                ["sh", "-c", f"cd {shlex.quote(path)} && find . -maxdepth 3 | sort | head -200"]
            )
            return result.stdout
        if call.name == "wb_shell":
            cmd = str(call.arguments.get("cmd", ""))
            result = self.workbench.exec(["sh", "-c", f"cd {shlex.quote(self.root)} && {cmd}"])
            out = f"{result.stdout}{result.stderr}"
            return f"exit={result.returncode}\n{out}"
        raise _Refused(f"unknown tool {call.name!r}")

    # -- confinement ---------------------------------------------------------

    def _resolve(self, raw: object, *, forbid_git: bool = False) -> str:
        path = str(raw or "")
        if not path:
            raise _Refused("path is required")
        if posixpath.isabs(path):
            raise _Refused(f"absolute paths are refused: {path!r}")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise _Refused(f"path traversal is refused: {path!r}")
        if forbid_git and parts and parts[0] == ".git":
            raise _Refused("writes under .git/ are refused")
        return posixpath.join(self.root, *parts) if parts else self.root


class _Refused(Exception):
    """A policy refusal — becomes an ok=False response the model can read."""
