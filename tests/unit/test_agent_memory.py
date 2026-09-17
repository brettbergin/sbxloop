"""Per-agent long-term memory over the daemon's real store.

The expectations are the shared memory contract: a memory is scoped by the
channel it was learned in (global memories and the current channel's are
visible, another channel's are not unless the platform says that channel is
workspace-visible), the prompt block is empty when nothing is visible and
bounded otherwise, an agent's store is capped by evicting its oldest
unpinned memory, updates are revision-checked, and forgetting is a soft
delete. Expected values are written out literally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from sbxloop.agents.memory import (
    AgentMemoryError,
    Memory,
    MemoryRevisionConflict,
    MemoryService,
    NoWorkspaceVisibility,
    WorkspaceChannelVisibility,
)
from sbxloop.config import Config, MemoryConfig
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import AgentMemoryRow, ChannelMemberRow, ChannelRow

USER = "user:usr_owner"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class FakeVisibility:
    def __init__(self, *visible: str) -> None:
        self.visible = set(visible)

    def workspace_visible(self, channel_id: str) -> bool:
        return channel_id in self.visible


@pytest.fixture
def store(tmp_path: Path) -> DaemonStore:
    return DaemonStore(tmp_path / "state.db")


def service(
    store: DaemonStore,
    *,
    visibility: object | None = None,
    **cfg: object,
) -> MemoryService:
    return MemoryService(
        store,
        visibility or NoWorkspaceVisibility(),  # type: ignore[arg-type]
        MemoryConfig.model_validate(cfg),
        Clock(),
    )


def contents(memories: list[Memory]) -> list[str]:
    return [memory.content for memory in memories]


class TestConfig:
    def test_defaults(self) -> None:
        memory = Config().memory
        assert memory.enabled is True
        assert memory.max_items_per_agent == 500
        assert memory.max_item_chars == 1000
        assert memory.prompt_budget_chars == 4000

    def test_a_config_without_the_section_still_loads(self) -> None:
        assert Config.model_validate({}).memory == MemoryConfig()


class TestRemember:
    def test_a_memory_carries_its_source_and_author(self, store: DaemonStore) -> None:
        memories = service(store)
        memory = memories.remember(
            "planner",
            "  The deploy window is Friday.  ",
            kind="fact",
            channel_id="chn_a",
            run_id="run_1",
            message_id="msg_1",
            author="agent:planner",
        )
        assert memory.id.startswith("mem_")
        assert memory.agent_slug == "planner"
        assert memory.kind == "fact"
        assert memory.content == "The deploy window is Friday."
        assert memory.source_channel_id == "chn_a"
        assert memory.source_run_id == "run_1"
        assert memory.source_message_id == "msg_1"
        assert memory.author == "agent:planner"
        assert memory.pinned is False
        assert memory.revision == 1
        assert memory.last_used_at is None
        assert memory.created_at == memory.updated_at

    def test_content_is_capped(self, store: DaemonStore) -> None:
        memory = service(store, max_item_chars=10).remember(
            "planner", "abcdefghijklmnop", channel_id=None, author=USER
        )
        assert memory.content == "abcdefghij"

    def test_blank_content_is_refused(self, store: DaemonStore) -> None:
        with pytest.raises(AgentMemoryError) as caught:
            service(store).remember("planner", "   ", channel_id=None, author=USER)
        assert caught.value.code == "invalid_memory"

    def test_an_unknown_kind_is_refused(self, store: DaemonStore) -> None:
        with pytest.raises(AgentMemoryError):
            service(store).remember(
                "planner",
                "x",
                kind="secret",  # type: ignore[arg-type]
                channel_id=None,
                author=USER,
            )

    def test_an_author_names_an_agent_or_a_user(self, store: DaemonStore) -> None:
        with pytest.raises(AgentMemoryError):
            service(store).remember("planner", "x", channel_id=None, author="someone")

    def test_disabled_memory_refuses_to_remember(self, store: DaemonStore) -> None:
        memories = service(store, enabled=False)
        with pytest.raises(AgentMemoryError) as caught:
            memories.remember("planner", "x", channel_id=None, author=USER)
        assert caught.value.code == "memory_disabled"

    def test_the_created_event_carries_no_content(self, store: DaemonStore) -> None:
        memory = service(store).remember(
            "planner", "a private detail", channel_id="chn_a", author=USER
        )
        with store.read() as session:
            events = list(
                session.scalars(select(ApiEventRow).where(ApiEventRow.type.like("agent.memory.%")))
            )
        assert [event.type for event in events] == ["agent.memory.created"]
        data = json.loads(events[0].data_json)
        assert data["memory_id"] == memory.id
        assert data["agent_slug"] == "planner"
        assert "a private detail" not in events[0].data_json
        assert "content" not in data


class TestItemCap:
    def test_the_oldest_unpinned_memory_is_evicted(self, store: DaemonStore) -> None:
        memories = service(store, max_items_per_agent=3)
        first = memories.remember("planner", "first", channel_id=None, author=USER, pinned=True)
        memories.remember("planner", "second", channel_id=None, author=USER)
        memories.remember("planner", "third", channel_id=None, author=USER)
        memories.remember("critic", "other agent", channel_id=None, author=USER)
        memories.remember("planner", "fourth", channel_id=None, author=USER)

        kept = memories.list("planner", channel_id=None, include_private=True)
        assert sorted(contents(kept)) == ["first", "fourth", "third"]
        assert first.id in {memory.id for memory in kept}
        # Another agent's store is its own.
        assert contents(memories.list("critic", channel_id=None, include_private=True)) == [
            "other agent"
        ]
        with store.read() as session:
            evicted = session.scalars(
                select(AgentMemoryRow).where(AgentMemoryRow.content == "second")
            ).one()
        assert evicted.deleted_at is not None

    def test_a_store_of_only_pinned_memories_is_full(self, store: DaemonStore) -> None:
        memories = service(store, max_items_per_agent=1)
        memories.remember("planner", "kept", channel_id=None, author=USER, pinned=True)
        with pytest.raises(AgentMemoryError) as caught:
            memories.remember("planner", "new", channel_id=None, author=USER)
        assert caught.value.code == "memory_full"
        assert contents(memories.list("planner", channel_id=None, include_private=True)) == ["kept"]


class TestScoping:
    @pytest.fixture
    def seeded(self, store: DaemonStore) -> MemoryService:
        memories = service(store, visibility=FakeVisibility("chn_shared"))
        for text, channel in (
            ("global note", None),
            ("same channel note", "chn_here"),
            ("private elsewhere note", "chn_private"),
            ("shared elsewhere note", "chn_shared"),
        ):
            memories.remember("planner", text, channel_id=channel, author=USER)
        return memories

    def test_recall_sees_global_same_channel_and_workspace_visible(
        self, seeded: MemoryService
    ) -> None:
        recalled = seeded.recall("planner", query="note", channel_id="chn_here")
        assert sorted(contents(recalled)) == [
            "global note",
            "same channel note",
            "shared elsewhere note",
        ]

    def test_recall_outside_any_channel_sees_global_and_workspace_visible(
        self, seeded: MemoryService
    ) -> None:
        recalled = seeded.recall("planner", query="note", channel_id=None)
        assert sorted(contents(recalled)) == ["global note", "shared elsewhere note"]

    def test_list_hides_other_private_channels_unless_asked(self, seeded: MemoryService) -> None:
        assert sorted(
            contents(seeded.list("planner", channel_id="chn_here", include_private=False))
        ) == ["global note", "same channel note", "shared elsewhere note"]
        assert sorted(
            contents(seeded.list("planner", channel_id="chn_here", include_private=True))
        ) == [
            "global note",
            "private elsewhere note",
            "same channel note",
            "shared elsewhere note",
        ]

    def test_list_leaves_out_channels_the_reader_cannot_read(self, seeded: MemoryService) -> None:
        def readable(channel_id: str) -> bool:
            return channel_id in {"chn_here", "chn_shared"}

        for include_private in (False, True):
            assert sorted(
                contents(
                    seeded.list(
                        "planner",
                        channel_id="chn_here",
                        include_private=include_private,
                        readable=readable,
                    )
                )
            ) == ["global note", "same channel note", "shared elsewhere note"]

    def test_the_default_visibility_shares_nothing(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "elsewhere", channel_id="chn_other", author=USER)
        assert memories.recall("planner", query="elsewhere", channel_id="chn_here") == []

    def test_another_agents_memories_are_never_recalled(self, seeded: MemoryService) -> None:
        assert seeded.recall("critic", query="note", channel_id="chn_here") == []


class TestRecall:
    def test_pinned_first_then_score_then_recency(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "release notes go in the wiki", channel_id=None, author=USER)
        memories.remember(
            "planner", "release checklist lives beside the notes", channel_id=None, author=USER
        )
        memories.remember("planner", "release happens on Friday", channel_id=None, author=USER)
        memories.remember(
            "planner", "the release owner signs off", channel_id=None, author=USER, pinned=True
        )
        memories.remember("planner", "unrelated reminder", channel_id=None, author=USER)

        recalled = memories.recall("planner", query="Release NOTES", channel_id=None)
        assert contents(recalled) == [
            "the release owner signs off",
            "release checklist lives beside the notes",
            "release notes go in the wiki",
            "release happens on Friday",
        ]

    def test_limit_bounds_the_answer(self, store: DaemonStore) -> None:
        memories = service(store)
        for index in range(5):
            memories.remember("planner", f"note {index}", channel_id=None, author=USER)
        assert len(memories.recall("planner", query="note", channel_id=None, limit=2)) == 2

    def test_recall_marks_what_it_returned_as_used(self, store: DaemonStore) -> None:
        memories = service(store)
        hit = memories.remember("planner", "alpha", channel_id=None, author=USER)
        miss = memories.remember("planner", "beta", channel_id=None, author=USER)
        memories.recall("planner", query="alpha", channel_id=None)
        listed = {m.id: m for m in memories.list("planner", channel_id=None, include_private=True)}
        assert listed[hit.id].last_used_at is not None
        assert listed[miss.id].last_used_at is None

    def test_disabled_memory_recalls_nothing(self, store: DaemonStore) -> None:
        service(store).remember("planner", "alpha", channel_id=None, author=USER)
        assert service(store, enabled=False).recall("planner", query="alpha", channel_id=None) == []


class TestUpdateAndForget:
    def test_update_bumps_the_revision(self, store: DaemonStore) -> None:
        memories = service(store)
        memory = memories.remember("planner", "old", channel_id=None, author=USER)
        updated = memories.update(
            memory.id, content=" new ", pinned=True, expected_revision=1, author=USER
        )
        assert updated.content == "new"
        assert updated.pinned is True
        assert updated.revision == 2
        assert updated.updated_at > memory.updated_at

    def test_a_stale_revision_is_a_conflict(self, store: DaemonStore) -> None:
        memories = service(store)
        memory = memories.remember("planner", "old", channel_id=None, author=USER)
        memories.update(memory.id, content="newer", pinned=None, expected_revision=1, author=USER)
        with pytest.raises(MemoryRevisionConflict):
            memories.update(
                memory.id, content="stale", pinned=None, expected_revision=1, author=USER
            )
        listed = memories.list("planner", channel_id=None, include_private=True)
        assert contents(listed) == ["newer"]

    def test_update_of_an_unknown_memory_is_not_found(self, store: DaemonStore) -> None:
        with pytest.raises(AgentMemoryError) as caught:
            service(store).update(
                "mem_missing", content="x", pinned=None, expected_revision=1, author=USER
            )
        assert caught.value.code == "memory_not_found"

    def test_forget_is_a_soft_delete(self, store: DaemonStore) -> None:
        memories = service(store)
        memory = memories.remember("planner", "forget me", channel_id=None, author=USER)
        memories.forget("planner", memory.id, author=USER)

        assert memories.list("planner", channel_id=None, include_private=True) == []
        assert memories.recall("planner", query="forget", channel_id=None) == []
        with store.read() as session:
            row = session.get(AgentMemoryRow, memory.id)
            assert row is not None and row.deleted_at is not None
        with pytest.raises(AgentMemoryError) as caught:
            memories.update(
                memory.id, content="back", pinned=None, expected_revision=1, author=USER
            )
        assert caught.value.code == "memory_not_found"

    def test_forget_names_the_agent_it_belongs_to(self, store: DaemonStore) -> None:
        memories = service(store)
        memory = memories.remember("planner", "mine", channel_id=None, author=USER)
        with pytest.raises(AgentMemoryError) as caught:
            memories.forget("critic", memory.id, author=USER)
        assert caught.value.code == "memory_not_found"
        assert contents(memories.list("planner", channel_id=None, include_private=True)) == ["mine"]


class TestPromptBlock:
    def test_nothing_visible_is_the_empty_string(self, store: DaemonStore) -> None:
        memories = service(store)
        assert memories.prompt_block("planner", channel_id="chn_a", budget_chars=4000) == ""
        memories.remember("planner", "elsewhere", channel_id="chn_b", author=USER)
        assert memories.prompt_block("planner", channel_id="chn_a", budget_chars=4000) == ""

    def test_pinned_first_then_most_recent(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "older", channel_id=None, author=USER)
        memories.remember("planner", "pinned", kind="procedure", channel_id=None, author=USER)
        memories.remember("planner", "newer", kind="preference", channel_id="chn_a", author=USER)
        pinned = memories.list("planner", channel_id=None, include_private=True)
        target = next(memory for memory in pinned if memory.content == "pinned")
        memories.update(target.id, content=None, pinned=True, expected_revision=1, author=USER)
        # The update made "pinned" the most recently changed; pin order wins anyway.
        memories.remember("planner", "newest", channel_id=None, author=USER)

        block = memories.prompt_block("planner", channel_id="chn_a", budget_chars=4000)
        assert block == (
            "## What you remember\n"
            "- (procedure) pinned\n"
            "- (fact) newest\n"
            "- (preference) newer\n"
            "- (fact) older\n"
        )

    def test_the_block_stays_within_its_budget(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "a" * 40, channel_id=None, author=USER)
        memories.remember("planner", "b" * 40, channel_id=None, author=USER)
        heading = "## What you remember\n"
        line = "- (fact) " + "b" * 40 + "\n"
        budget = len(heading) + len(line) + 10
        block = memories.prompt_block("planner", channel_id=None, budget_chars=budget)
        assert block == heading + line
        assert len(block) <= budget

    def test_a_budget_too_small_for_one_line_is_empty(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "a" * 40, channel_id=None, author=USER)
        assert memories.prompt_block("planner", channel_id=None, budget_chars=30) == ""

    def test_a_multiline_memory_is_one_bullet(self, store: DaemonStore) -> None:
        memories = service(store)
        memories.remember("planner", "first line\n\n  second line", channel_id=None, author=USER)
        assert memories.prompt_block("planner", channel_id=None, budget_chars=4000) == (
            "## What you remember\n- (fact) first line second line\n"
        )

    def test_the_configured_budget_is_the_default(self, store: DaemonStore) -> None:
        memories = service(store, prompt_budget_chars=30)
        memories.remember("planner", "a" * 40, channel_id=None, author=USER)
        assert memories.prompt_block("planner", channel_id=None) == ""

    def test_disabled_memory_is_the_empty_string(self, store: DaemonStore) -> None:
        service(store).remember("planner", "alpha", channel_id=None, author=USER)
        assert service(store, enabled=False).prompt_block("planner", channel_id=None) == ""


class TestWorkspaceChannelVisibility:
    @pytest.fixture
    def channels(self, store: DaemonStore) -> WorkspaceChannelVisibility:
        with store.transaction() as session:
            for channel_id, owner, visibility in (
                ("chn_open", "usr_owner", "workspace"),
                ("chn_owned", "usr_owner", "private"),
                ("chn_joined", "usr_owner", "private"),
                ("chn_mine", "usr_member", "private"),
            ):
                session.add(
                    ChannelRow(
                        id=channel_id,
                        user_id=owner,
                        title=channel_id,
                        visibility=visibility,
                        created_at=1.0,
                        updated_at=1.0,
                    )
                )
            session.add(
                ChannelMemberRow(
                    channel_id="chn_joined", user_id="usr_member", role="member", joined_at=1.0
                )
            )
        return WorkspaceChannelVisibility(store)

    def test_only_a_workspace_channel_is_workspace_visible(
        self, channels: WorkspaceChannelVisibility
    ) -> None:
        assert channels.workspace_visible("chn_open") is True
        assert channels.workspace_visible("chn_owned") is False
        assert channels.workspace_visible("chn_missing") is False

    def test_a_member_reads_workspace_created_and_joined_channels(
        self, channels: WorkspaceChannelVisibility
    ) -> None:
        readable = channels.readable_by("usr_member")
        assert [
            channel
            for channel in ("chn_open", "chn_owned", "chn_joined", "chn_mine", "chn_missing")
            if readable(channel)
        ] == ["chn_open", "chn_joined", "chn_mine"]

    def test_the_service_shares_what_a_workspace_channel_learned(
        self, store: DaemonStore, channels: WorkspaceChannelVisibility
    ) -> None:
        memories = service(store, visibility=channels)
        memories.remember("planner", "open note", channel_id="chn_open", author=USER)
        memories.remember("planner", "owned note", channel_id="chn_owned", author=USER)
        assert contents(memories.recall("planner", query="note", channel_id="chn_mine")) == [
            "open note"
        ]
