"""Opaque channel originals over the real API and collaboration store."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from sbxloop.api.channel_file_tools import (
    read_channel_input,
    search_channel_input,
    strings_channel_input,
)
from sbxloop.backup import create_backup
from sbxloop.daemon.concierge import ConciergeReply


def _owner(api: Any) -> tuple[dict[str, str], str]:
    registered = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": "file-owner@example.test",
            "username": "file-owner",
            "password": "correct horse battery staple",
        },
    )
    assert registered.status_code == 201, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    channel = api.client.post("/v1/channels", headers=headers, json={})
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"]


def _reserve(api: Any, headers: dict[str, str], channel: str, name: str, size: int) -> str:
    response = api.client.post(
        f"/v1/channels/{channel}/files",
        headers=headers,
        json={"client_upload_id": "upload-1", "name": name, "size": size},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_arbitrary_bytes_round_trip_and_backup(api: Any) -> None:
    headers, channel = _owner(api)
    original = b"MZ\x00\xff\x80unknown-binary\n" + bytes(range(256))
    file_id = _reserve(api, headers, channel, "../sa\u202emple.exe", len(original))
    base = f"/v1/channels/{channel}/files/{file_id}"

    staged = api.client.get(base, headers=headers)
    assert staged.status_code == 200
    assert staged.json()["name"] == "sample.exe"
    assert staged.json()["status"] == "reserved"
    assert api.client.get(base + "/content", headers=headers).status_code == 409

    sent = api.client.put(
        base + "/content",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=original,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["sha256"] == hashlib.sha256(original).hexdigest()
    assert sent.json()["size"] == len(original)
    downloaded = api.client.get(base + "/content", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == original
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert api.client.get(f"/v1/channels/{channel}/files", headers=headers).json() == {"data": []}

    snapshot = create_backup(api.ctx.config.paths, label="file-input")
    assert (snapshot.path / "channel-files" / file_id).read_bytes() == original


def test_upload_replay_conflict_size_and_channel_scope(api: Any) -> None:
    headers, channel = _owner(api)
    other = api.client.post("/v1/channels", headers=headers, json={}).json()["id"]
    file_id = _reserve(api, headers, channel, "notes.txt", 5)
    reserve = f"/v1/channels/{channel}/files"
    base = f"{reserve}/{file_id}"

    replay = api.client.post(
        reserve,
        headers=headers,
        json={"client_upload_id": "upload-1", "name": "notes.txt", "size": 5},
    )
    assert replay.status_code == 201 and replay.json()["id"] == file_id
    conflict = api.client.post(
        reserve,
        headers=headers,
        json={"client_upload_id": "upload-1", "name": "different.txt", "size": 5},
    )
    assert conflict.status_code == 409
    mismatch = api.client.put(base + "/content", headers=headers, content=b"abc")
    assert mismatch.status_code == 409
    assert api.client.get(base, headers=headers).json()["status"] == "reserved"

    uploaded = api.client.put(base + "/content", headers=headers, content=b"hello")
    assert uploaded.status_code == 200, uploaded.text
    assert api.client.put(base + "/content", headers=headers, content=b"hello").status_code == 200
    assert api.client.put(base + "/content", headers=headers, content=b"other").status_code == 409
    assert (
        api.client.get(f"/v1/channels/{other}/files/{file_id}", headers=headers).status_code == 404
    )
    assert (
        api.client.get(f"/v1/channels/{other}/files/{file_id}/content", headers=headers).status_code
        == 404
    )

    cancelled = api.client.delete(base, headers=headers)
    assert cancelled.status_code == 204, cancelled.text
    assert api.client.get(base, headers=headers).status_code == 404
    assert not (api.ctx.config.paths.channel_files / file_id).exists()
    cancelled_replay = api.client.post(
        reserve,
        headers=headers,
        json={"client_upload_id": "upload-1", "name": "notes.txt", "size": 5},
    )
    assert cancelled_replay.status_code == 409


def test_stream_limit_counts_actual_bytes(api: Any, monkeypatch: Any) -> None:
    from sbxloop.api import channel_files as service
    from sbxloop.api.routes import channel_files as route

    headers, channel = _owner(api)
    file_id = _reserve(api, headers, channel, "tiny.bin", 3)
    monkeypatch.setattr(route, "MAX_FILE_BYTES", 3)
    monkeypatch.setattr(service, "MAX_FILE_BYTES", 3)
    too_large = api.client.put(
        f"/v1/channels/{channel}/files/{file_id}/content",
        headers=headers,
        content=b"1234",
    )
    assert too_large.status_code == 413
    assert (
        api.client.get(f"/v1/channels/{channel}/files/{file_id}", headers=headers).json()["status"]
        == "reserved"
    )
    assert not (api.ctx.config.paths.channel_files / file_id).exists()


def test_turn_attaches_files_and_agent_reads_only_its_snapshot(api: Any) -> None:
    class CapturingConcierge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def submit_turn(self, prompt: str, **kwargs: Any) -> Future[ConciergeReply]:
            self.calls.append((prompt, kwargs))
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("I can inspect the file."))
            return result

    concierge = CapturingConcierge()
    api.ctx.concierge = concierge
    headers, channel = _owner(api)
    first = _reserve(api, headers, channel, "source.py", 10)
    content = b"print(42)\n"
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{first}/content", headers=headers, content=content
        ).status_code
        == 200
    )
    turn_body = {
        "content": "Review the uploaded source",
        "file_ids": [first],
        "client_turn_id": "with-file",
        "client_message_id": "message-with-file",
    }
    sent = api.client.post(f"/v1/channels/{channel}/turns", headers=headers, json=turn_body)
    assert sent.status_code == 202, sent.text
    message = sent.json()["message"]
    assert message["client_message_id"] == "message-with-file"
    assert [file["id"] for file in message["input_files"]] == [first]
    assert (
        api.client.get(f"/v1/channels/{channel}/files", headers=headers).json()["data"][0]["id"]
        == first
    )
    turn_id = sent.json()["turn"]["id"]
    deadline = time.monotonic() + 2
    while not concierge.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert concierge.calls
    prompt, call = concierge.calls[0]
    assert first in prompt and "source.py" in prompt
    assert {tool.spec.name for tool in call["channel_tools"]} >= {
        "list_channel_inputs",
        "read_channel_input",
        "search_channel_input",
        "strings_channel_input",
    }
    file, chunk = api.ctx.channel_files.read_for_turn(turn_id, first, offset=0, limit=100)
    assert file.id == first and chunk == content
    inspected = json.loads(read_channel_input(api.ctx, turn_id, {"file_id": first}))
    assert inspected["representation"] == "utf-8"
    assert inspected["content"] == "print(42)\n"
    replay = api.client.post(f"/v1/channels/{channel}/turns", headers=headers, json=turn_body)
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    altered = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={**turn_body, "file_ids": []},
    )
    assert altered.status_code == 409

    second_reserve = api.client.post(
        f"/v1/channels/{channel}/files",
        headers=headers,
        json={"client_upload_id": "upload-2", "name": "later.bin", "size": 3},
    )
    second = second_reserve.json()["id"]
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{second}/content", headers=headers, content=b"xyz"
        ).status_code
        == 200
    )
    later = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "", "file_ids": [second], "client_turn_id": "file-only"},
    )
    assert later.status_code == 202, later.text
    assert [
        file.id for file in api.ctx.channel_files.list_for_turn(turn_id, offset=0, limit=10)[0]
    ] == [first]
    assert [
        file.id
        for file in api.ctx.channel_files.list_for_turn(
            later.json()["turn"]["id"], offset=0, limit=10
        )[0]
    ] == [second, first]
    try:
        api.ctx.channel_files.read_for_turn(turn_id, second, offset=0, limit=10)
    except Exception as exc:
        assert getattr(exc, "code", None) == "input_file_not_found"
    else:
        raise AssertionError("an earlier turn read a later file")

    assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
    try:
        api.ctx.channel_files.read_for_turn(turn_id, first, offset=0, limit=10)
    except Exception as exc:
        assert getattr(exc, "code", None) == "channel_not_found"
    else:
        raise AssertionError("a deleted channel kept agent file access")


def test_search_channel_input_is_bounded_and_snapshot_scoped(api: Any) -> None:
    class CapturingConcierge:
        def submit_turn(self, prompt: str, **kwargs: Any) -> Future[ConciergeReply]:
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("I can inspect the file."))
            return result

    api.ctx.concierge = CapturingConcierge()
    headers, channel = _owner(api)
    boundary = 1_000_000
    content = b"x" * (boundary - 3) + b"need" + b"le\n" + b"z" * 100
    first = _reserve(api, headers, channel, "large.txt", len(content))
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{first}/content", headers=headers, content=content
        ).status_code
        == 200
    )
    sent = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Find the needle", "file_ids": [first], "client_turn_id": "search-1"},
    )
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["turn"]["id"]

    first_page = json.loads(
        search_channel_input(
            api.ctx, turn_id, {"file_id": first, "query": "needle", "max_bytes": boundary - 3}
        )
    )
    assert first_page["matches"] == []
    assert first_page["next_offset"] == boundary - 3
    assert first_page["truncated"] is True
    second_page = json.loads(
        search_channel_input(
            api.ctx, turn_id, {"file_id": first, "query": "needle", "offset": boundary - 3}
        )
    )
    assert [match["offset"] for match in second_page["matches"]] == [boundary - 3]
    assert "needle" in second_page["matches"][0]["excerpt"]
    assert second_page["truncated"] is False

    later = api.client.post(
        f"/v1/channels/{channel}/files",
        headers=headers,
        json={"client_upload_id": "upload-later", "name": "later.txt", "size": 4},
    ).json()["id"]
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{later}/content", headers=headers, content=b"test"
        ).status_code
        == 200
    )
    assert (
        api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": "Another file", "file_ids": [later], "client_turn_id": "search-2"},
        ).status_code
        == 202
    )
    from sbxloop.errors import ToolRejectedError

    with pytest.raises(ToolRejectedError, match="unavailable"):
        search_channel_input(api.ctx, turn_id, {"file_id": later, "query": "test"})
    with pytest.raises(ToolRejectedError, match="unavailable"):
        strings_channel_input(api.ctx, turn_id, {"file_id": later})
    with pytest.raises(ToolRejectedError, match="nonempty"):
        search_channel_input(api.ctx, turn_id, {"file_id": first, "query": ""})
    with pytest.raises(ToolRejectedError, match="too long"):
        search_channel_input(api.ctx, turn_id, {"file_id": first, "query": "x" * 257})
    with pytest.raises(ToolRejectedError, match="valid Unicode"):
        search_channel_input(api.ctx, turn_id, {"file_id": first, "query": "\ud800"})
    assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
    with pytest.raises(ToolRejectedError, match="unavailable"):
        search_channel_input(api.ctx, turn_id, {"file_id": first, "query": "needle"})


def test_search_channel_input_limits_results_and_escapes_binary(api: Any) -> None:
    class CapturingConcierge:
        def submit_turn(self, prompt: str, **kwargs: Any) -> Future[ConciergeReply]:
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("I can inspect the file."))
            return result

    api.ctx.concierge = CapturingConcierge()
    headers, channel = _owner(api)
    content = b"\x00" + b"hit" * 25
    first = _reserve(api, headers, channel, "binary.bin", len(content))
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{first}/content", headers=headers, content=content
        ).status_code
        == 200
    )
    sent = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Find hits", "file_ids": [first], "client_turn_id": "search-binary"},
    )
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["turn"]["id"]
    result = json.loads(search_channel_input(api.ctx, turn_id, {"file_id": first, "query": "hit"}))
    assert len(result["matches"]) == 20
    assert result["matches"][0]["offset"] == 1
    assert result["matches"][0]["representation"] == "hex"
    assert result["truncated"] is True
    continuation = json.loads(
        search_channel_input(
            api.ctx, turn_id, {"file_id": first, "query": "hit", "offset": result["next_offset"]}
        )
    )
    assert [match["offset"] for match in continuation["matches"]] == [61, 64, 67, 70, 73]


def test_strings_channel_input_discovers_inert_binary_indicators(api: Any) -> None:
    class CapturingConcierge:
        def submit_turn(self, prompt: str, **kwargs: Any) -> Future[ConciergeReply]:
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("I can inspect the file."))
            return result

    api.ctx.concierge = CapturingConcierge()
    headers, channel = _owner(api)
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/channel_analysis/inert_keylogger_indicators.elf"
    ).read_bytes()
    first = _reserve(api, headers, channel, "sample-01.bin", len(fixture))
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{first}/content", headers=headers, content=fixture
        ).status_code
        == 200
    )
    sent = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Inspect this binary", "file_ids": [first], "client_turn_id": "strings-1"},
    )
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["turn"]["id"]

    result = json.loads(strings_channel_input(api.ctx, turn_id, {"file_id": first}))
    values = {item["value"] for item in result["strings"]}
    assert {"GetAsyncKeyState", "SetWindowsHookExA", "/dev/input/event0"} <= values
    assert result["truncated"] is False
    for item in result["strings"]:
        assert fixture[item["offset"] : item["offset"] + len(item["value"])] == item[
            "value"
        ].encode("ascii")

    from sbxloop.errors import ToolRejectedError

    assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
    with pytest.raises(ToolRejectedError, match="unavailable"):
        strings_channel_input(api.ctx, turn_id, {"file_id": first})


def test_strings_channel_input_continues_after_scan_and_result_limits(api: Any) -> None:
    class CapturingConcierge:
        def submit_turn(self, prompt: str, **kwargs: Any) -> Future[ConciergeReply]:
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("I can inspect the file."))
            return result

    api.ctx.concierge = CapturingConcierge()
    headers, channel = _owner(api)
    content = b"\x00ABCD\x00" + b"".join(f"S{n:03d}".encode() + b"\x00" for n in range(105))
    first = _reserve(api, headers, channel, "strings.bin", len(content))
    assert (
        api.client.put(
            f"/v1/channels/{channel}/files/{first}/content", headers=headers, content=content
        ).status_code
        == 200
    )
    sent = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Find strings", "file_ids": [first], "client_turn_id": "strings-2"},
    )
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["turn"]["id"]

    boundary = json.loads(
        strings_channel_input(api.ctx, turn_id, {"file_id": first, "max_bytes": 3})
    )
    assert [(item["offset"], item["value"]) for item in boundary["strings"]] == [(1, "ABCD")]
    assert boundary["next_offset"] == 5

    first_page = json.loads(strings_channel_input(api.ctx, turn_id, {"file_id": first}))
    assert len(first_page["strings"]) == 100
    assert first_page["truncated"] is True
    second_page = json.loads(
        strings_channel_input(
            api.ctx, turn_id, {"file_id": first, "offset": first_page["next_offset"]}
        )
    )
    assert [item["value"] for item in second_page["strings"]] == [
        f"S{n:03d}" for n in range(99, 105)
    ]
    assert second_page["truncated"] is False
