"""Files are shared channel context, not run-scoped secrets (plan S-P15).

The contract:

- a work result attaches the files its run delivered to the message, and the
  message serves them;
- a turn's history lines name the sequence, the author, the kind and the
  files each message carried, and a trimmed history opens with the channel's
  latest summary;
- ``read_channel_artifact`` reads a file that the *current turn's channel*
  can see -- for every participant, including a read-only critic -- refuses
  one from another channel, truncates with a marker, and refuses binary;
- the channel artifact routes take channel read permission rather than
  ``artifacts:read``, so a workspace member reads the files of a channel
  they are in.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from sbxloop.api.channel_artifacts import READ_LIMIT_MAX, channel_artifact_tools
from sbxloop.api.channel_summary import ChannelSummarizer
from sbxloop.errors import ToolRejectedError
from tests.api.test_channel_access import _invite, _user_id
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_work_delivery import setup_work


@pytest.fixture(autouse=True)
def _directory_relative_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catalogued bytes on a platform without directory-relative opens.

    ``sbxloop.repofiles.open_file`` refuses every read where ``os.open``
    takes no ``dir_fd`` (Windows), so nothing is catalogued and nothing can
    be downloaded there. That opener's safety is
    ``tests/api/test_artifacts.py``'s subject on a platform that has it;
    here it is only a collaborator, so a plain opener bounded to the run's
    own directory stands in where the platform cannot provide one. On CI
    the real opener runs and this fixture does nothing.
    """
    if os.open in os.supports_dir_fd:
        return

    @contextmanager
    def opener(root: Path, relative: str | Path) -> Iterator[BinaryIO]:
        target = (Path(root) / relative).resolve()
        if not target.is_file() or Path(root).resolve() not in target.parents:
            raise OSError(errno.ENOENT, "not a regular file under the run's directory")
        with target.open("rb") as handle:
            yield handle

    monkeypatch.setattr("sbxloop.repofiles.open_file", opener)


def _deliver(
    api: Any, *, text: str = "# Report\n", name: str = "report.md"
) -> tuple[dict[str, str], str, str]:
    """A completed workload in a fresh channel, with one delivered file.

    Returns the owner's headers, the channel id and the run's public id.
    """
    headers, channel, item = setup_work(api, request={"intent": "workload"})
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    run_id = api.harness.runs[-1][0]
    home = api.ctx.config.paths
    api.harness.store.set_run_workspace(run_id, home.run_data(run_id), mounted=False)
    root = home.run_artifacts(run_id)
    root.mkdir(parents=True, exist_ok=True)
    # Bytes, not text: a platform newline translation would change the size.
    (root / name).write_bytes(text.encode("utf-8"))
    return headers, channel, f"run_{run_id}"


def _work_result(api: Any, headers: dict[str, str], channel: str) -> dict[str, Any]:
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    return next(message for message in messages if message["kind"] == "work_result")


class TestMessageArtifacts:
    def test_a_work_result_attaches_and_serves_the_files_its_run_delivered(self, api: Any) -> None:
        headers, channel, run_id = _deliver(api)
        result = _work_result(api, headers, channel)
        assert [a["relpath"] for a in result["artifacts"]] == ["report.md"]
        attached = result["artifacts"][0]
        assert attached["id"].startswith("art_")
        assert attached["run_id"] == run_id
        assert attached["media_type"] == "text/markdown"
        assert attached["size"] == len("# Report\n")
        # The same rows are what the channel's file list serves.
        listed = api.client.get(f"/v1/channels/{channel}/artifacts", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()["data"]] == [attached["id"]]

    def test_an_ordinary_message_carries_no_artifacts(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        api.client.post(f"/v1/channels/{channel}/turns", headers=headers, json={"content": "hi"})
        assert api.ctx.turns.wait_idle(timeout=5)
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert all(message["artifacts"] == [] for message in messages)


class TestHistoryLines:
    def test_history_lines_carry_the_author_the_kind_and_the_files(self, api: Any) -> None:
        headers, channel, _ = _deliver(api)
        result = _work_result(api, headers, channel)
        store = api.ctx.collaboration
        (turn,) = store.list_turns(None, channel)
        # A later turn reads the delivered result as history.
        later = api.client.post(
            f"/v1/channels/{channel}/turns", headers=headers, json={"content": "and now?"}
        )
        assert later.status_code == 202, later.text
        assert api.ctx.turns.wait_idle(timeout=5)
        (_, second) = store.list_turns(None, channel)
        lines = [json.loads(line) for line in store.turn_history(second).splitlines()]
        delivered = next(line for line in lines if line["kind"] == "work_result")
        assert delivered["seq"] == result["sequence"]
        assert delivered["author_kind"] == "agent"
        assert delivered["author"] == "concierge"
        assert delivered["role"] == "assistant"
        assert [a["name"] for a in delivered["artifacts"]] == ["report.md"]
        assert delivered["artifacts"][0]["id"] == result["artifacts"][0]["id"]
        asked = next(line for line in lines if line["role"] == "user")
        assert asked["author_kind"] == "human"
        assert asked["author"] == _user_id(api, headers)
        assert "artifacts" not in asked
        del turn

    def test_a_trimmed_history_opens_with_the_channel_s_latest_summary(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        store = api.ctx.collaboration
        for index in range(4):
            api.client.post(
                f"/v1/channels/{channel}/turns",
                headers=headers,
                json={"content": f"message {index}"},
            )
            assert api.ctx.turns.wait_idle(timeout=5)
        turns = store.list_turns(None, channel)
        store.put_channel_summary(channel, 2, "Earlier: they discussed bread.", api.clock())
        # A budget small enough to drop the oldest lines.
        trimmed = store.turn_history(turns[-1], max_chars=200).splitlines()
        first = json.loads(trimmed[0])
        assert first["kind"] == "channel_summary"
        assert first["content"] == "Earlier: they discussed bread."
        # An untrimmed history does not repeat the summary.
        whole = store.turn_history(turns[-1]).splitlines()
        assert not any(json.loads(line)["kind"] == "channel_summary" for line in whole)

    def test_the_summarizer_writes_a_row_for_what_fell_out_of_the_window(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        store = api.ctx.collaboration
        for index in range(4):
            api.client.post(
                f"/v1/channels/{channel}/turns",
                headers=headers,
                json={"content": f"message {index}"},
            )
            assert api.ctx.turns.wait_idle(timeout=5)
        asked: list[str] = []

        def summarize(prompt: str) -> str:
            asked.append(prompt)
            return "They counted to four."

        summarizer = ChannelSummarizer(store, summarize, api.clock, keep=2)
        assert summarizer.refresh(channel) is True
        assert "message 0" in asked[0]
        summary = store.latest_channel_summary(channel)
        assert summary is not None
        assert summary.content == "They counted to four."
        assert summary.through_sequence > 0
        # Nothing new fell out: the job does not ask again.
        assert summarizer.refresh(channel) is False
        assert len(asked) == 1


class TestReadChannelArtifact:
    def _tool(self, api: Any, channel: str) -> Any:
        (tool,) = channel_artifact_tools(api.ctx, channel)
        assert tool.spec.name == "read_channel_artifact"
        return tool

    def test_a_critic_in_the_channel_reads_the_file_s_text(self, api: Any) -> None:
        headers, channel, _ = _deliver(api, text="Bread needs salt.\n")
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        answer = self._tool(api, channel).impl({"artifact_id": artifact["id"]})
        assert "Bread needs salt." in answer

    def test_a_file_from_another_channel_is_refused(self, api: Any) -> None:
        headers, channel, _ = _deliver(api)
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        with pytest.raises(ToolRejectedError):
            self._tool(api, other).impl({"artifact_id": artifact["id"]})

    def test_a_long_file_is_truncated_with_a_marker(self, api: Any) -> None:
        body = "line\n" * 40_000
        assert len(body) > READ_LIMIT_MAX
        headers, channel, _ = _deliver(api, text=body, name="long.txt")
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        answer = self._tool(api, channel).impl({"artifact_id": artifact["id"]})
        assert len(answer) < len(body)
        assert "truncated" in answer
        assert "offset=" in answer
        # Reading on from the marker's offset continues the same file.
        rest = self._tool(api, channel).impl(
            {"artifact_id": artifact["id"], "offset": READ_LIMIT_MAX, "limit": 100}
        )
        assert "line" in rest

    def test_a_binary_file_is_refused_with_a_metadata_line(self, api: Any) -> None:
        headers, channel, _ = _deliver(api, name="logo.png")
        run_id = api.harness.runs[-1][0]
        (api.ctx.config.paths.run_artifacts(run_id) / "logo.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00binary"
        )
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        answer = self._tool(api, channel).impl({"artifact_id": artifact["id"]})
        assert answer.startswith("logo.png is not text (image/png, 18 bytes).")
        # The bytes themselves never reach the model.
        assert "PNG" not in answer.replace("logo.png", "")
        assert "\x89" not in answer


class TestChannelArtifactRoutes:
    def test_a_member_without_artifacts_read_reads_the_channel_s_files(self, api: Any) -> None:
        headers, channel, run_id = _deliver(api)
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        guest = bearer(_invite(api, "member", "guest"))
        # The run route is closed to them: they hold no ``artifacts:read``.
        assert api.client.get(f"/v1/runs/{run_id}/artifacts", headers=guest).status_code == 403
        # Not in the private channel yet: the channel's files do not exist.
        assert api.client.get(f"/v1/channels/{channel}/artifacts", headers=guest).status_code == 404
        added = api.client.post(
            f"/v1/channels/{channel}/members",
            headers=headers,
            json={"user_id": _user_id(api, guest)},
        )
        assert added.status_code in (200, 201), added.text
        listed = api.client.get(f"/v1/channels/{channel}/artifacts", headers=guest)
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()["data"]] == [artifact["id"]]
        content = api.client.get(
            f"/v1/channels/{channel}/artifacts/{artifact['id']}/content", headers=guest
        )
        assert content.status_code == 200, content.text
        assert content.text == "# Report\n"

    def test_a_file_outside_the_channel_is_not_found(self, api: Any) -> None:
        headers, channel, _ = _deliver(api)
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        missing = api.client.get(
            f"/v1/channels/{other}/artifacts/{artifact['id']}/content", headers=headers
        )
        assert missing.status_code == 404, missing.text


def test_the_feature_is_advertised(api: Any, tmp_path: Path) -> None:
    del tmp_path
    served = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "collaboration.message_artifacts" in served
    assert "collaboration.channel_artifacts" in served
