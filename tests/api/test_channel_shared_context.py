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
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from sqlalchemy import select

from sbxloop.api.channel_artifacts import (
    READ_LIMIT_MAX,
    TOOL_NAME,
    channel_artifact_tools,
)
from sbxloop.api.channel_summary import PROMPT, ChannelSummarizer
from sbxloop.daemon.model import WorkItem
from sbxloop.db.collaboration_models import ChannelSummaryRow
from sbxloop.errors import ToolRejectedError
from sbxloop.ghids import chat_item_id
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


def _code_run_checkout(api: Any) -> tuple[dict[str, str], str, str, Any]:
    """A code run the channel started, with one catalogued checkout file.

    A code run delivers a pull request, so the file is never attached to a
    message. Returns the owner's headers, the channel id, the run id and
    the catalogued artifact.
    """
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": "fix the bug"}
    ).json()
    assert api.ctx.turns.wait_idle(timeout=5)
    key = accepted["turn"]["input_message_id"]
    item = WorkItem(
        item_id=chat_item_id(key),
        source_key=key,
        title="Fix",
        body="fix the bug",
        kind="code",
        channel_id=channel,
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.harness.source.items = [item]
    api.harness.outcomes = ["merged"]
    api.clock.t += 10
    api.loop.tick()
    run_id = api.harness.runs[-1][0]
    home = api.ctx.config.paths
    api.harness.store.set_run_workspace(run_id, home.run_data(run_id), mounted=False)
    root = home.run_artifacts(run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.py").write_text("DATABASE_URL = 'postgres://localhost/app'", encoding="utf-8")
    assert api.ctx.artifacts.catalog_run(api.ctx.loop.store.get_run(run_id)) == 1
    (checkout,) = api.ctx.artifacts.for_run(run_id)
    return headers, channel, run_id, checkout


def _work_result(api: Any, headers: dict[str, str], channel: str) -> dict[str, Any]:
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    return next(message for message in messages if message["kind"] == "work_result")


def _chatter(api: Any, headers: dict[str, str], lines: list[str]) -> str:
    """A fresh channel where ``lines`` were each said and answered."""
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    for line in lines:
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns", headers=headers, json={"content": line}
        )
        assert accepted.status_code == 202, accepted.text
        assert api.ctx.turns.wait_idle(timeout=5)
    return channel


def _say(api: Any, headers: dict[str, str], channel: str, line: str) -> None:
    """``line`` is said in ``channel`` and answered."""
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": line}
    )
    assert accepted.status_code == 202, accepted.text
    assert api.ctx.turns.wait_idle(timeout=5)


def _settles(check: Any, timeout: float = 10.0) -> bool:
    """Whether ``check`` becomes true within ``timeout``: work that left the
    turn's lane finishes on another thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def _summary_calls(api: Any) -> list[dict[str, Any]]:
    """The compaction job's model calls, told apart from conversation by
    the standing instruction only it sends."""
    head = PROMPT.splitlines()[0]
    return [call for call in api.ctx.concierge.calls if head in call["text"]]


def _compactions_settled(api: Any) -> bool:
    """Whether the compactions the settled turns queued on their own thread
    have all finished: one still running would pick up whatever summariser
    a test installs next."""
    return _settles(lambda: not api.ctx._compactions)


class _Stuck:
    """A provider that accepts the turn and never answers, remembering what
    it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resets: list[str | None] = []
        self.futures: list[Future[Any]] = []

    def reset_session(self, session_key: str | None = None) -> None:
        self.resets.append(session_key)

    def submit_turn(self, text: str, **kwargs: Any) -> Future[Any]:
        self.calls.append({"text": text, **kwargs})
        future: Future[Any] = Future()
        self.futures.append(future)
        return future


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
        asked: list[tuple[str, str]] = []

        def summarize(summarised: str, prompt: str) -> str:
            asked.append((summarised, prompt))
            return "They counted to four."

        summarizer = ChannelSummarizer(store, summarize, api.clock, keep=2)
        assert summarizer.refresh(channel) is True
        assert asked[0][0] == channel
        assert "message 0" in asked[0][1]
        summary = store.latest_channel_summary(channel)
        assert summary is not None
        assert summary.content == "They counted to four."
        assert summary.through_sequence > 0
        # Nothing new fell out: the job does not ask again.
        assert summarizer.refresh(channel) is False
        assert len(asked) == 1


class TestSummaryCompaction:
    """What compaction promises beyond writing a row.

    A summary is written from one channel's transcript, covers exactly the
    messages it was shown, fires whenever the history window actually
    trims, and never holds the channel's turn lane.
    """

    def test_each_channel_is_summarised_in_its_own_session(self, api: Any) -> None:
        # A resumed model session carries the last call's transcript into
        # the next one. One session for every channel would therefore show
        # a private channel's messages while summarising another's.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        first = _chatter(api, headers, [f"alpha {index}" for index in range(4)])
        second = _chatter(api, headers, [f"bravo {index}" for index in range(4)])
        # The turns that built the channels queued compactions of their
        # own; one still running would summarise with the narrower window
        # installed below and count a channel twice.
        assert _compactions_settled(api)
        api.ctx._summaries = ChannelSummarizer(
            api.ctx.collaboration, api.ctx._summarize, api.clock, keep=2
        )
        for channel in (first, second):
            api.ctx.compact_channel(channel)
        keys = [call["session_key"] for call in _summary_calls(api)]
        assert len(keys) == 2
        assert keys[0] != keys[1]
        assert first in keys[0] and second not in keys[0]
        assert second in keys[1] and first not in keys[1]

    def test_the_watermark_covers_only_what_the_transcript_carried(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        said = [f"note {word}" for word in ("alpha", "bravo", "charlie", "delta", "echo")]
        channel = _chatter(api, headers, said)
        store = api.ctx.collaboration
        # A budget far below the backlog: the transcript stops part way.
        backlog = store.summary_backlog(channel, keep=2, max_chars=80)
        assert backlog is not None
        through, previous, transcript = backlog
        assert previous is None
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        asked = [message for message in messages if message["role"] == "user"]
        # The budget really did bite: something the channel said is not here.
        assert any(message["content"] not in transcript for message in asked)
        # Everything the watermark now covers was shown to the model.
        covered = [message for message in asked if message["sequence"] <= through]
        assert covered
        assert [
            message["content"] for message in covered if message["content"] not in transcript
        ] == []
        # What did not fit is still backlog for the next compaction.
        store.put_channel_summary(channel, through, "Earlier notes.", api.clock())
        again = store.summary_backlog(channel, keep=2, max_chars=80)
        assert again is not None
        _, carried, rest = again
        assert carried == "Earlier notes."
        left = [message for message in asked if message["sequence"] > through]
        assert left
        assert left[0]["content"] in rest

    def test_a_history_trimmed_by_the_character_budget_is_summarised(self, api: Any) -> None:
        # Far fewer than HISTORY_MESSAGES messages, but more than the
        # character budget holds: the history is trimmed all the same, so
        # there has to be a summary for it to open with.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"paragraph {index}" for index in range(4)])
        store = api.ctx.collaboration
        turns = store.list_turns(None, channel)
        whole = store.turn_history(turns[-1]).splitlines()
        trimmed = store.turn_history(turns[-1], max_chars=200).splitlines()
        assert len(trimmed) < len(whole)
        assert store.summary_backlog(channel, max_chars=200) is not None

    def test_compaction_does_not_hold_the_channel_s_turn_lane(self, api: Any) -> None:
        # The turn pool is one lane wide per channel and as few as one
        # thread deep. A model call made inside the lane wedges the
        # channel -- and every other channel -- for its whole round trip.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        started = threading.Event()
        release = threading.Event()

        def summarize(*args: str) -> str:
            started.set()
            assert release.wait(10)
            return "They counted to four."

        api.ctx._summaries = ChannelSummarizer(api.ctx.collaboration, summarize, api.clock, keep=2)
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns", headers=headers, json={"content": "message 4"}
        )
        assert accepted.status_code == 202, accepted.text
        assert started.wait(10)
        assert api.ctx.turns.wait_idle(timeout=10) is True
        release.set()
        assert _settles(lambda: api.ctx.collaboration.latest_channel_summary(channel) is not None)

    def test_a_summary_is_refreshed_in_batches_not_on_every_turn(self, api: Any) -> None:
        # Past the cap every turn pushes a message or two out of the
        # window. Rewriting the summary for each of them would add a model
        # call per turn for the rest of the channel's life.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        asked: list[str] = []

        def summarize(summarised: str, prompt: str) -> str:
            asked.append(prompt)
            return f"Summary {len(asked)}."

        summarizer = ChannelSummarizer(
            api.ctx.collaboration, summarize, api.clock, keep=2, batch_messages=5
        )
        # The first compaction happens as soon as anything falls out: a
        # trimmed history has to open with a summary.
        assert summarizer.refresh(channel) is True
        # One more exchange falls out: not yet a batch.
        _say(api, headers, channel, "message 4")
        assert summarizer.refresh(channel) is False
        assert len(asked) == 1
        # Enough has fallen out since the last summary: now it is refreshed,
        # and what it was shown includes what waited.
        _say(api, headers, channel, "message 5")
        _say(api, headers, channel, "message 6")
        assert summarizer.refresh(channel) is True
        assert len(asked) == 2
        assert "message 4" in asked[1]

    def test_a_large_backlog_is_a_batch_whatever_its_count(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        asked: list[str] = []

        def summarize(summarised: str, prompt: str) -> str:
            asked.append(prompt)
            return "Summary."

        summarizer = ChannelSummarizer(
            api.ctx.collaboration, summarize, api.clock, keep=2, batch_messages=50, batch_chars=100
        )
        assert summarizer.refresh(channel) is True
        _say(api, headers, channel, "x" * 200)
        _say(api, headers, channel, "message 5")
        assert summarizer.refresh(channel) is True
        assert len(asked) == 2

    def test_only_the_newest_summary_is_kept(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        summarizer = ChannelSummarizer(
            api.ctx.collaboration,
            lambda summarised, prompt: "Summary.",
            api.clock,
            keep=2,
            batch_messages=1,
        )
        assert summarizer.refresh(channel) is True
        _say(api, headers, channel, "message 4")
        assert summarizer.refresh(channel) is True
        latest = api.ctx.collaboration.latest_channel_summary(channel)
        assert latest is not None
        with api.ctx.collaboration.dstore.read() as session:
            rows = list(
                session.scalars(
                    select(ChannelSummaryRow.through_sequence).where(
                        ChannelSummaryRow.channel_id == channel
                    )
                )
            )
        assert rows == [latest.through_sequence]

    def test_a_summary_s_model_call_is_charged_to_its_channel(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        api.ctx._summaries = ChannelSummarizer(
            api.ctx.collaboration, api.ctx._summarize, api.clock, keep=2
        )
        api.ctx.compact_channel(channel)
        # The messages that built the channel may already have queued a
        # background compaction, so count nothing: every summary call made
        # for this channel is charged to it and cannot act.
        calls = _summary_calls(api)
        assert calls
        for call in calls:
            assert call["channel_id"] == channel
            assert call["allow_actions"] is False

    def test_a_summary_is_one_stateless_call(self, api: Any) -> None:
        # A resumed session would carry an earlier summary's transcript
        # into the next compaction of the same channel, and a reset made
        # outside the lane races a call for that channel that was given up
        # on but is still running: the summary neither resumes nor stores
        # a session at all.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        assert _compactions_settled(api)
        api.ctx._summaries = ChannelSummarizer(
            api.ctx.collaboration, api.ctx._summarize, api.clock, keep=2
        )
        api.ctx.compact_channel(channel)
        calls = _summary_calls(api)
        assert calls
        for call in calls:
            assert call["stateless"] is True
        assert api.ctx.concierge.resets == []

    def test_a_summariser_that_never_answers_is_given_up_on(
        self, api: Any, monkeypatch: Any
    ) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        monkeypatch.setattr("sbxloop.api.context.SUMMARY_TIMEOUT_S", 0.5, raising=False)

        assert _compactions_settled(api)
        stuck = _Stuck()
        api.ctx.concierge = stuck
        api.ctx._summaries = ChannelSummarizer(
            api.ctx.collaboration, api.ctx._summarize, api.clock, keep=2
        )
        done = threading.Event()
        threading.Thread(
            target=lambda: (api.ctx.compact_channel(channel), done.set()), daemon=True
        ).start()
        assert done.wait(10) is True
        assert api.ctx.collaboration.latest_channel_summary(channel) is None
        # Giving up lets the call go: it no longer waits for a place in the
        # concierge's pool, so the next summary or chat turn is not queued
        # behind a summary nobody will read.
        (future,) = stuck.futures
        assert future.cancelled() is True
        (call,) = stuck.calls
        assert call["stateless"] is True
        assert stuck.resets == []

    def test_closing_waits_for_a_compaction_that_is_writing(self, api: Any) -> None:
        # The daemon closes its store right after the API context. A
        # compaction still reading or writing it on its own thread would
        # then touch a closed database.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])
        started = threading.Event()
        release = threading.Event()

        def summarize(*args: str) -> str:
            started.set()
            assert release.wait(10)
            return "They counted to four."

        api.ctx._summaries = ChannelSummarizer(api.ctx.collaboration, summarize, api.clock, keep=2)
        api.ctx.schedule_compaction(channel)
        assert started.wait(10)
        threading.Timer(0.3, release.set).start()
        api.ctx.close()
        # By the time close returns, the compaction has finished with the
        # store: its row is there and nothing else is in flight.
        assert api.ctx.collaboration.latest_channel_summary(channel) is not None

    def test_closing_abandons_a_summary_the_model_has_not_answered(self, api: Any) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, [f"message {index}" for index in range(4)])

        assert _compactions_settled(api)
        stuck = _Stuck()
        api.ctx.concierge = stuck
        api.ctx._summaries = ChannelSummarizer(
            api.ctx.collaboration, api.ctx._summarize, api.clock, keep=2
        )
        done = threading.Event()
        threading.Thread(
            target=lambda: (api.ctx.compact_channel(channel), done.set()), daemon=True
        ).start()
        time.sleep(0.2)
        api.ctx.close()
        # Well inside the model timeout: shutting down does not wait it out.
        assert done.wait(5) is True
        assert api.ctx.collaboration.latest_channel_summary(channel) is None
        # And the abandoned call is let go rather than left in the pool.
        (future,) = stuck.futures
        assert future.cancelled() is True


class TestReadChannelArtifact:
    def _tool(self, api: Any, channel: str) -> Any:
        (tool,) = channel_artifact_tools(api.ctx, channel)
        assert tool.spec.name == "read_channel_artifact"
        return tool

    def test_a_chat_turn_is_handed_the_channel_s_read_tool(self, api: Any) -> None:
        # The tool reaches a turn through the concierge seam, not only
        # through a direct call in a test.
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _chatter(api, headers, ["read the report"])
        del channel
        (call,) = [call for call in api.ctx.concierge.calls if call["text"] == "read the report"]
        assert [tool.spec.name for tool in call["channel_tools"]] == [TOOL_NAME]

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

    def test_a_code_run_s_checkout_is_refused_as_the_download_route_refuses_it(
        self, api: Any
    ) -> None:
        # An agent in the channel reads exactly what a member of the
        # channel can download. The checkout of a code run the channel
        # started is not a channel file: the route answers 404, so the
        # tool must refuse it too, whoever supplies the id.
        headers, channel, run_id, checkout = _code_run_checkout(api)
        assert api.ctx.collaboration.channel_owns_run(channel, run_id) is True
        route = api.client.get(
            f"/v1/channels/{channel}/artifacts/{checkout.id}/content", headers=headers
        )
        assert route.status_code == 404, route.text
        with pytest.raises(ToolRejectedError):
            self._tool(api, channel).impl({"artifact_id": checkout.id})

    def test_a_delivered_workload_file_is_readable_by_the_tool_and_the_route(
        self, api: Any
    ) -> None:
        headers, channel, _ = _deliver(api, text="Bread needs salt.\n")
        artifact = _work_result(api, headers, channel)["artifacts"][0]
        route = api.client.get(
            f"/v1/channels/{channel}/artifacts/{artifact['id']}/content", headers=headers
        )
        assert route.status_code == 200, route.text
        assert route.text == "Bread needs salt.\n"
        answer = self._tool(api, channel).impl({"artifact_id": artifact["id"]})
        assert "Bread needs salt." in answer


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

    def test_a_code_run_s_checkout_is_not_downloadable_through_the_channel(self, api: Any) -> None:
        # A code run delivers a pull request, so its files are never
        # attached to a message and never listed. The content route must
        # not hand them out either: a channel reader holds no
        # ``artifacts:read``, and the checkout is the target's source.
        headers, channel, run_id, checkout = _code_run_checkout(api)
        # The run is the channel's own work, and it is still not a channel file.
        assert api.ctx.collaboration.channel_owns_run(channel, run_id) is True
        listed = api.client.get(f"/v1/channels/{channel}/artifacts", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()["data"]] == []
        denied = api.client.get(
            f"/v1/channels/{channel}/artifacts/{checkout.id}/content", headers=headers
        )
        assert denied.status_code == 404, denied.text


def test_the_feature_is_advertised(api: Any, tmp_path: Path) -> None:
    del tmp_path
    served = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "collaboration.message_artifacts" in served
    assert "collaboration.channel_artifacts" in served
