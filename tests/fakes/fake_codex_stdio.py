"""A scripted app-server peer for the real Python SDK; never starts a model.

Launched through CodexConfig.launch_args_override by test_codex_sdk_contract.
Only synthetic protocol messages and a test-owned transcript path are used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def send(message: dict[str, Any]) -> None:
    print(json.dumps(message), flush=True)


def reply(message: dict[str, Any], result: dict[str, Any]) -> None:
    send({"id": message["id"], "result": result})


def notify(method: str, params: dict[str, Any]) -> None:
    send({"method": method, "params": params})


def thread_response(thread_id: str) -> dict[str, Any]:
    return {
        "thread": {
            "id": thread_id,
            "sessionId": "session-fixture",
            "cliVersion": "0.147.0",
            "createdAt": 1,
            "updatedAt": 1,
            "cwd": str(Path.cwd()),
            "ephemeral": False,
            "modelProvider": "openai",
            "preview": "fixture",
            "source": "appServer",
            "status": {"type": "idle"},
            "turns": [],
        },
        "model": "fixture-model",
        "modelProvider": "openai",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": str(Path.cwd()),
        "sandbox": {"type": "readOnly"},
    }


def complete(thread_id: str) -> None:
    location = {"threadId": thread_id, "turnId": "turn-fixture"}
    text = '{"verified":true}'
    item = {"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": text}
    notify("item/agentMessage/delta", {**location, "itemId": "answer", "delta": text})
    notify("item/completed", {**location, "item": item, "completedAtMs": 2})
    usage = {
        "inputTokens": 15,
        "cachedInputTokens": 4,
        "outputTokens": 6,
        "reasoningOutputTokens": 2,
        "totalTokens": 21,
    }
    notify("thread/tokenUsage/updated", {**location, "tokenUsage": {"last": usage, "total": usage}})
    notify(
        "turn/completed",
        {
            "threadId": thread_id,
            "turn": {"id": "turn-fixture", "items": [item], "status": "completed"},
        },
    )


def main() -> None:
    transcript, scenario = Path(sys.argv[1]), sys.argv[2]
    thread_id = "thread-fixture"
    for line in sys.stdin:
        message = json.loads(line)
        with transcript.open("a", encoding="utf-8") as output:
            output.write(json.dumps(message) + "\n")
        method = message.get("method")
        if method == "initialize":
            reply(
                message, {"userAgent": "fixture", "platformFamily": "unix", "platformOs": "linux"}
            )
        elif method == "initialized":
            continue
        elif method == "config/read":
            reply(message, {"config": {}, "origins": {}})
        elif method == "account/login/start":
            if scenario != "hang-login":
                reply(message, {"type": "apiKey"})
        elif method in ("thread/start", "thread/resume"):
            thread_id = message["params"].get("threadId", thread_id)
            reply(message, thread_response(thread_id))
        elif method == "turn/start":
            if scenario == "hang-turn":
                continue
            turn = {"id": "turn-fixture", "items": [], "status": "inProgress"}
            # Real app-server may notify before the response arrives. This
            # exercises the SDK's early queue registration as well.
            notify("turn/started", {"threadId": thread_id, "turn": turn})
            reply(message, {"turn": turn})
            send(
                {
                    "id": "fixture-tool-request",
                    "method": "item/tool/call",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-fixture",
                        "callId": "fixture-call",
                        "tool": "read_file",
                        "arguments": {"path": "input.txt"},
                    },
                }
            )
        elif message.get("id") == "fixture-tool-request":
            complete(thread_id)
        else:
            raise RuntimeError(f"unexpected fixture message: {method}")


if __name__ == "__main__":
    main()
