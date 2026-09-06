"""External MCP servers, from the neutral spec to an SDK's dialect.

The host resolves an operator's ``[[mcp]]`` entry into a
:class:`~sbxloop_worker.protocol.McpServerSpec` and sends it in the job. It
must not send credentials: a job's contents reach events, logs and the
worker's own stderr. So a spec carries ``${NAME}`` references instead, and
this module expands them **inside the sandbox**, against the process
environment the provisioner set up — where, under the default secret
strategy, the value is a proxy placeholder that only becomes real in flight
to the credential's own host.

The two SDKs agree on the shape (a name -> config mapping, the same fields)
and disagree on exactly one token: stdio is ``"stdio"`` to the Claude Agent
SDK and ``"local"`` to the Copilot SDK. That single difference is why the
protocol carries a neutral transport and the backends pass ``stdio_type``
here rather than each writing their own mapping.

Field-verified 2026-09-06 against github-copilot-sdk 1.0.8
(``CopilotClient.create_session(mcp_servers=...)``, ``MCPStdioServerConfig``
/ ``MCPHTTPServerConfig``) and claude-agent-sdk 0.2.149
(``ClaudeAgentOptions.mcp_servers``, ``McpStdioServerConfig`` /
``McpSSEServerConfig`` / ``McpHttpServerConfig``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from sbxloop_worker.protocol import McpServerSpec

__all__ = ["ENV_REF", "expand_refs", "server_configs"]

#: ``${NAME}`` — the only substitution a spec may ask for. Deliberately not
#: shell expansion: no ``$NAME``, no ``$(...)``, no defaults, so a value
#: that merely contains a dollar sign cannot become a command.
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_refs(value: str, environ: Mapping[str, str] | None = None) -> str:
    """``value`` with every ``${NAME}`` replaced from the environment.

    A name the environment does not carry expands to the empty string
    rather than raising: the server then fails its own authentication with
    a message naming the service, which is a better diagnosis than the
    worker refusing to start a session at all. Provisioning is what
    guarantees the name is set, and doctor is what reports it missing.
    """
    env = os.environ if environ is None else environ
    return ENV_REF.sub(lambda m: env.get(m.group(1), ""), value)


def server_configs(
    servers: list[McpServerSpec],
    *,
    stdio_type: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """The operator's MCP servers as the SDK's ``mcp_servers`` mapping.

    ``stdio_type`` is the caller's spelling of the stdio transport. Pure
    apart from the environment lookup, so both backends' mappings are
    testable without either SDK installed.
    """
    configs: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.transport == "stdio":
            config: dict[str, Any] = {"type": stdio_type, "command": server.command}
            if server.args:
                config["args"] = list(server.args)
            if server.env:
                config["env"] = {k: expand_refs(v, environ) for k, v in server.env.items()}
        else:
            config = {"type": server.transport, "url": server.url}
            if server.headers:
                config["headers"] = {k: expand_refs(v, environ) for k, v in server.headers.items()}
        configs[server.name] = config
    return configs
