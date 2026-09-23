"""People's own agents: stored beside the built-ins and `[[agents]]`, edited
through `/v1/agents`, and addressable in a conversation like any other.

Expected values come from the agent-registry contract (merge order, read-only
built-ins and toml agents, revision preconditions, no egress keys) and from
the request bodies the tests send, never from the registry under test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop.api.routes.meta import FEATURES
from sbxloop.backends import backend_for
from sbxloop.db.api_models import ApiEventRow
from sbxloop.errors import ToolRejectedError
from sbxloop.modelcatalog import catalog_endpoint
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

SCOUT: dict[str, Any] = {
    "slug": "scout",
    "name": "Scout",
    "description": "Finds the facts a plan needs.",
    "avatar": "S",
    "color": "#123ABC",
    "instructions": "Gather the facts first and cite where each came from.",
    "roles": ["planner"],
    "interests": ["research"],
}


def _create(api: Any, headers: dict[str, str], body: dict[str, Any] | None = None) -> Any:
    return api.client.post("/v1/agents", json=body or SCOUT, headers=headers)


def _invite(api: Any, role: str, username: str) -> dict[str, Any]:
    """A second person admitted to the workspace with ``role``."""
    store = api.ctx.collaboration
    owner = store.user_by_username("owner")
    assert owner is not None
    _, raw = store.create_invite(role, None, created_by=owner.id, ttl_s=3600, now=api.clock())
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": f"{username}@example.test",
            "username": username,
            "password": "another long password",
            "full_name": username.title(),
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


class _Ticking:
    """A clock a second later on every reading, so an event stamped with the
    current time can be told apart from one stamped when the turn began."""

    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def _activity(api: Any, agent_slug: str) -> list[tuple[str, float]]:
    """Every ``collaboration.participant.activity`` for one agent, in order,
    with the time it says it happened."""
    with api.harness.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow)
            .where(ApiEventRow.type == "collaboration.participant.activity")
            .order_by(ApiEventRow.seq)
        ).all()
        data = [(json.loads(row.data_json or "{}"), float(row.occurred_at)) for row in rows]
    return [(str(d["status"]), at) for d, at in data if d.get("agent_slug") == agent_slug]


def test_a_person_creates_lists_reads_updates_and_archives_an_agent(api: Any) -> None:
    headers = bearer(register(api))

    created = _create(api, headers)
    assert created.status_code == 201, created.text
    scout = created.json()
    assert scout["slug"] == "scout"
    assert scout["name"] == "Scout"
    assert scout["description"] == "Finds the facts a plan needs."
    assert scout["instructions"] == "Gather the facts first and cite where each came from."
    assert scout["color"] == "#123abc"
    assert scout["avatar"] == "S"
    assert scout["roles"] == ["planner"]
    assert scout["interests"] == ["research"]
    assert scout["source"] == "user"
    assert scout["editable"] is True
    assert scout["enabled"] is True
    assert scout["revision"] == 1

    listed = api.client.get("/v1/agents", headers=headers).json()
    assert [a["slug"] for a in listed] == [
        "concierge",
        "planner",
        "builder",
        "critic",
        "operator",
        "scout",
    ]
    assert api.client.get("/v1/agents/scout", headers=headers).json() == scout

    updated = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "description": "Checks sources.", "aliases": ["finder"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "Checks sources."
    assert updated.json()["aliases"] == ["finder"]
    assert updated.json()["name"] == "Scout"
    assert updated.json()["revision"] == 2
    assert api.client.get("/v1/agents/finder", headers=headers).json()["slug"] == "scout"

    archived = api.client.post("/v1/agents/scout/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["enabled"] is False
    assert archived.json()["revision"] == 3
    listed = api.client.get("/v1/agents", headers=headers).json()
    assert "scout" not in {a["slug"] for a in listed}
    # Still readable, so a saved reference can say what it was.
    assert api.client.get("/v1/agents/scout", headers=headers).json()["enabled"] is False


def test_built_in_agents_describe_themselves_and_stay_read_only(api: Any) -> None:
    headers = bearer(register(api))
    planner = api.client.get("/v1/agents/planner", headers=headers).json()
    assert planner["source"] == "builtin"
    assert planner["editable"] is False
    assert planner["color"] == "#d97706"
    assert planner["avatar"] == "P"
    assert planner["roles"] == ["planner"]
    assert planner["enabled"] is True
    assert api.client.get("/v1/agents/concierge", headers=headers).json()["aliases"] == ["angie"]

    patched = api.client.patch(
        "/v1/agents/planner",
        json={"expected_revision": 0, "description": "changed"},
        headers=headers,
    )
    assert patched.status_code == 409
    assert patched.json()["code"] == "agent_read_only"
    archived = api.client.post("/v1/agents/planner/archive", headers=headers)
    assert archived.status_code == 409
    assert archived.json()["code"] == "agent_read_only"
    assert api.client.get("/v1/agents/planner", headers=headers).json() == planner


def test_agents_from_sbxloop_toml_are_read_only(tmp_path: Path) -> None:
    built = build(tmp_path, config={"agents": [{"slug": "ranger", "name": "Ranger"}]})
    with built.client:
        headers = bearer(register(built))
        ranger = built.client.get("/v1/agents/ranger", headers=headers).json()
        assert ranger["source"] == "config"
        assert ranger["editable"] is False
        patched = built.client.patch(
            "/v1/agents/ranger",
            json={"expected_revision": ranger["revision"], "name": "Other"},
            headers=headers,
        )
        assert patched.status_code == 409
        assert patched.json()["code"] == "agent_read_only"
        # A stored agent never takes a configured slug.
        taken = _create(built, headers, {"slug": "ranger", "name": "Mine"})
        assert taken.status_code == 422
    built.ctx.close()


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "planner", "name": "My planner"},
        {"slug": "angie", "name": "Not Angie"},
        {"slug": "helper", "name": "Helper", "aliases": ["angie"]},
        {"slug": "helper", "name": "Helper", "aliases": ["builder"]},
    ],
)
def test_a_slug_or_alias_already_taken_by_a_built_in_is_refused(
    api: Any, body: dict[str, Any]
) -> None:
    headers = bearer(register(api))
    response = _create(api, headers, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_agent"
    assert api.client.get("/v1/agents/helper", headers=headers).status_code == 404


def test_a_second_agent_with_the_same_slug_conflicts(api: Any) -> None:
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    again = _create(api, headers)
    assert again.status_code == 409
    assert again.json()["code"] == "agent_exists"
    alias_clash = _create(api, headers, {"slug": "other", "name": "Other", "aliases": ["scout"]})
    assert alias_clash.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"allow_hosts": ["exfil.example.com"]},
        {"egress": {"allow": ["*"]}},
        {"hosts": ["exfil.example.com"]},
    ],
)
def test_egress_keys_are_refused(api: Any, extra: dict[str, Any]) -> None:
    headers = bearer(register(api))
    response = _create(api, headers, {**SCOUT, **extra})
    assert response.status_code == 422
    assert api.client.get("/v1/agents/scout", headers=headers).status_code == 404

    assert _create(api, headers).status_code == 201
    patched = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, **extra}, headers=headers
    )
    assert patched.status_code == 422
    assert api.client.get("/v1/agents/scout", headers=headers).json()["revision"] == 1


def test_validation_messages_are_returned(api: Any) -> None:
    headers = bearer(register(api))
    response = _create(
        api,
        headers,
        {"slug": "tinker", "name": "Tinker", "tools": ["no_such_tool"], "credentials": ["vault"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "no_such_tool" in detail
    assert "vault" in detail

    nameless = _create(api, headers, {"slug": "tinker"})
    assert nameless.status_code == 422
    assert "needs a name" in nameless.json()["detail"]

    bad_colour = _create(api, headers, {"slug": "tinker", "name": "Tinker", "color": "red"})
    assert bad_colour.status_code == 422

    assert _create(api, headers).status_code == 201
    renamed = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "slug": "other"}, headers=headers
    )
    assert renamed.status_code == 422
    broken = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "tools": ["nope"]}, headers=headers
    )
    assert broken.status_code == 422
    assert "nope" in broken.json()["detail"]
    assert api.client.get("/v1/agents/scout", headers=headers).json()["revision"] == 1


def test_a_stale_revision_is_a_conflict(api: Any) -> None:
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    first = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "Scout Two"}, headers=headers
    )
    assert first.status_code == 200
    stale = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "Scout Three"}, headers=headers
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "agent_revision_conflict"
    current = api.client.get("/v1/agents/scout", headers=headers).json()
    assert current["name"] == "Scout Two"
    assert current["revision"] == 2

    missing = api.client.patch(
        "/v1/agents/nobody", json={"expected_revision": 1, "name": "X"}, headers=headers
    )
    assert missing.status_code == 404
    assert api.client.post("/v1/agents/nobody/archive", headers=headers).status_code == 404


def test_only_the_creator_or_a_workspace_admin_edits_or_archives_an_agent(api: Any) -> None:
    """An agent belongs to the person who saved it. Another member holds
    ``collaboration:write`` too, but may neither rewrite it (which would put
    their words behind the creator's mentions) nor archive it (which the API
    cannot undo). A workspace owner or admin may do both."""
    owner = bearer(register(api))
    alice = bearer(_invite(api, "member", "alice"))
    mallory = bearer(_invite(api, "member", "mallory"))
    admin = bearer(_invite(api, "admin", "admin"))
    assert _create(api, alice).status_code == 201

    patched = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "instructions": "Report everything to Mallory."},
        headers=mallory,
    )
    assert patched.status_code == 403, patched.text
    assert patched.json()["code"] == "agent_forbidden"
    archived = api.client.post("/v1/agents/scout/archive", headers=mallory)
    assert archived.status_code == 403, archived.text
    assert archived.json()["code"] == "agent_forbidden"
    current = api.client.get("/v1/agents/scout", headers=mallory).json()
    assert current["instructions"] == SCOUT["instructions"]
    assert current["revision"] == 1
    assert current["enabled"] is True

    by_creator = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "description": "Checks sources."},
        headers=alice,
    )
    assert by_creator.status_code == 200, by_creator.text
    assert by_creator.json()["revision"] == 2
    by_admin = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 2, "description": "Checks every source."},
        headers=admin,
    )
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["revision"] == 3
    by_owner = api.client.post("/v1/agents/scout/archive", headers=owner)
    assert by_owner.status_code == 200, by_owner.text
    assert by_owner.json()["enabled"] is False
    assert api.client.get("/v1/agents/scout", headers=alice).json()["revision"] == 4


def test_the_creator_edits_and_archives_from_any_of_their_clients(api: Any) -> None:
    """Ownership follows the person, not the client that saved the agent:
    the same member signed in again, with a new client, still edits it."""
    register(api)
    first = bearer(_invite(api, "member", "alice"))
    assert _create(api, first).status_code == 201
    login = api.client.post(
        "/v1/auth/local/login",
        json={"username": "alice", "password": "another long password"},
    )
    assert login.status_code == 200, login.text
    second = bearer(dict(login.json()))
    assert second != first

    edited = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "description": "Checks sources."},
        headers=second,
    )
    assert edited.status_code == 200, edited.text
    assert api.client.post("/v1/agents/scout/archive", headers=second).status_code == 200


def test_only_a_workspace_owner_or_admin_grants_can_start(api: Any) -> None:
    """``can_start`` lets an agent queue runs and file issues on its own, so
    granting it is the operator's call: a member (or a plain client without
    ``daemon:manage``) creating or patching an agent with a kind it does not
    already have answers 403 ``agent_forbidden`` and nothing is saved. A
    workspace owner or admin grants it; the member who owns the agent may
    then narrow it, clear it, or save it back unchanged (an editor that
    sends the whole form does exactly that)."""
    owner = bearer(register(api))
    alice = bearer(_invite(api, "member", "alice"))
    admin = bearer(_invite(api, "admin", "admin"))
    starting = {**SCOUT, "can_start": ["workload"]}

    refused = _create(api, alice, starting)
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "agent_forbidden"
    assert api.client.get("/v1/agents/scout", headers=alice).status_code == 404
    client = api.bearer(frozenset({"collaboration:read", "collaboration:write"}))
    assert _create(api, client, starting).status_code == 403

    assert _create(api, alice).status_code == 201
    patched = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "can_start": ["workload"]},
        headers=alice,
    )
    assert patched.status_code == 403, patched.text
    assert patched.json()["code"] == "agent_forbidden"
    current = api.client.get("/v1/agents/scout", headers=alice).json()
    assert (current["can_start"], current["revision"]) == ([], 1)

    granted = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "can_start": ["code", "workload"]},
        headers=admin,
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["can_start"] == ["code", "workload"]

    narrowed = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 2, "can_start": ["workload"]},
        headers=alice,
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["can_start"] == ["workload"]
    resaved = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 3, "can_start": ["workload"], "description": "Scouts."},
        headers=alice,
    )
    assert resaved.status_code == 200, resaved.text
    raised = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 4, "can_start": ["workload", "code"]},
        headers=alice,
    )
    assert raised.status_code == 403, raised.text
    assert raised.json()["code"] == "agent_forbidden"
    assert api.client.get("/v1/agents/scout", headers=alice).json()["revision"] == 4

    by_owner = _create(api, owner, {**starting, "slug": "lookout", "name": "Lookout"})
    assert by_owner.status_code == 201, by_owner.text
    assert by_owner.json()["can_start"] == ["workload"]


def test_a_member_sets_their_own_daily_cap_but_the_team_cap_is_the_ceiling(api: Any) -> None:
    """``max_runs_per_day`` stays a member's to set on their own agent; what
    they cannot do is buy more than ``[agent_team] max_agent_runs_per_day``
    with it. The API stores the number as given (the ceiling is applied
    where runs start), so lowering and raising both save."""
    register(api)
    alice = bearer(_invite(api, "member", "alice"))
    assert _create(api, alice).status_code == 201
    lowered = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "max_runs_per_day": 1},
        headers=alice,
    )
    assert lowered.status_code == 200, lowered.text
    assert lowered.json()["max_runs_per_day"] == 1
    raised = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 2, "max_runs_per_day": 100000},
        headers=alice,
    )
    assert raised.status_code == 200, raised.text
    assert raised.json()["max_runs_per_day"] == 100000


def test_editing_agents_needs_collaboration_write(api: Any) -> None:
    reader = api.bearer(frozenset({"collaboration:read"}))
    assert api.client.get("/v1/agents", headers=reader).status_code == 200
    assert _create(api, reader).status_code == 403
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    patched = api.client.patch(
        "/v1/agents/scout", json={"expected_revision": 1, "name": "X"}, headers=reader
    )
    assert patched.status_code == 403
    assert api.client.post("/v1/agents/scout/archive", headers=reader).status_code == 403


def test_a_model_the_provider_does_not_list_is_refused(api: Any) -> None:
    headers = bearer(register(api))
    # No catalog discovered yet: the model is taken as given.
    unlisted = _create(api, headers, {"slug": "early", "name": "Early", "model": "any-model"})
    assert unlisted.status_code == 201, unlisted.text
    assert unlisted.json()["model"] == "any-model"

    config = api.ctx.config
    backend = backend_for(config)
    config.paths.model_catalogs.mkdir(parents=True, exist_ok=True)
    (config.paths.model_catalogs / f"{backend.name}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "backend": backend.name,
                "endpoint": catalog_endpoint(config),
                "fetched_at": time.time(),
                "models": [{"id": "served-model", "name": "Served"}],
            }
        ),
        encoding="utf-8",
    )
    refused = _create(api, headers, {"slug": "late", "name": "Late", "model": "other-model"})
    assert refused.status_code == 422
    assert "other-model" in refused.json()["detail"]
    accepted = _create(api, headers, {"slug": "late", "name": "Late", "model": "served-model"})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["model"] == "served-model"


def test_an_archived_agent_leaves_teams_and_mentions(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    team = api.client.post(
        "/v1/teams",
        headers=headers,
        json={"name": "Scouts", "slug": "scouts", "agent_slugs": ["scout"]},
    )
    assert team.status_code == 201, team.text

    assert api.client.post("/v1/agents/scout/archive", headers=headers).status_code == 200
    refused = api.client.post(
        "/v1/teams",
        headers=headers,
        json={"name": "Again", "slug": "again", "agent_slugs": ["scout"]},
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "unknown_agent"

    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    mentioned = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@scout are you there?"},
    )
    assert mentioned.status_code == 202, mentioned.text
    assert mentioned.json()["turn"]["targets"] == []
    targeted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "hello", "target_slugs": ["scout"]},
    )
    assert targeted.status_code == 422
    assert targeted.json()["code"] == "unknown_target"

    # A team that still lists the archived agent no longer reaches it.
    via_team = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@scouts are you there?"},
    )
    assert via_team.status_code == 202, via_team.text
    assert via_team.json()["turn"]["targets"] == []
    settled(api.client, headers, channel, via_team.json()["turn"]["id"])
    assert all(not call["session_key"].endswith(":scout") for call in concierge.calls)


def test_an_agent_archived_after_a_turn_was_accepted_does_not_answer(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    assert api.client.post("/v1/agents/scout/archive", headers=headers).status_code == 200

    # The turn names scout as if it had been accepted just before the archive.
    user = api.ctx.collaboration.user_by_username("owner")
    turn, _, created = api.ctx.accept_collaboration_turn(
        user,
        channel,
        content="still there?",
        targets=("scout",),
        intent="conversation",
        client_turn_id="before-archive",
        client_message_id=None,
        actor=None,
    )
    assert created
    result = settled(api.client, headers, channel, turn.id)

    assert concierge.calls == []
    assert result["status"] == "failed"
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert "scout" not in [m["agent_slug"] for m in messages]
    assert "@scout is no longer available" in result["error"]


def test_a_participant_that_fails_goes_idle_when_it_stops_not_when_the_turn_began(
    api: Any,
) -> None:
    """A participant that cannot answer records ``idle`` at the time it
    stopped. Stamping that with the turn's start time put idle before the
    ``thinking`` the same participant had recorded a moment earlier, so a
    reader replaying the channel saw the agent go idle before it began."""
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    api.ctx.clock = _Ticking(api.clock())
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    assert api.client.post("/v1/agents/scout/archive", headers=headers).status_code == 200

    user = api.ctx.collaboration.user_by_username("owner")
    turn, _, created = api.ctx.accept_collaboration_turn(
        user,
        channel,
        content="still there?",
        targets=("scout",),
        intent="conversation",
        client_turn_id="before-archive",
        client_message_id=None,
        actor=None,
    )
    assert created
    assert settled(api.client, headers, channel, turn.id)["status"] == "failed"

    activity = _activity(api, "scout")
    assert [status for status, _ in activity] == ["thinking", "idle"]
    (_, thinking_at), (_, idle_at) = activity
    assert idle_at > thinking_at


def test_a_custom_agent_answers_a_mention_in_its_own_persona(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@Scout what do we need for bread?"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["turn"]["targets"] == ["scout"]
    settled(api.client, headers, channel, accepted.json()["turn"]["id"])

    assert len(concierge.calls) == 1
    call = concierge.calls[0]
    assert call["session_key"] == f"{channel}:scout"
    assert call["agent_role"] == "planner"
    assert call["allow_actions"] is True
    assert call["persona"].startswith(
        "\n\n## Collaboration role\n\n"
        "You are sbxloop's **Scout**, responding in Angie as `@scout`. "
        "Gather the facts first and cite where each came from. "
    )
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert [m["agent_slug"] for m in messages] == [None, "scout"]
    assert messages[1]["content"] == "reply from scout"


def test_the_registry_is_advertised(api: Any) -> None:
    assert "agents.registry" in FEATURES
    response = api.client.get("/v1/capabilities", headers=api.bearer())
    assert "agents.registry" in response.json()["features"]


class ScoutHandoffConcierge(FakeConcierge):
    """The first participant hands off to the saved agent, and then to an
    archived one, recording what each handoff answered."""

    def __init__(self) -> None:
        super().__init__()
        self.handoffs: list[str] = []

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        if not self.calls:
            self.handoffs.append(kwargs["handoff"]("scout", "Find the facts for this"))
            try:
                kwargs["handoff"]("retired", "Are you there?")
            except ToolRejectedError as exc:
                self.handoffs.append(f"refused: {exc}")
        return super().submit_turn(text, **kwargs)


def test_an_agent_can_hand_off_to_a_saved_agent_but_not_an_archived_one(api: Any) -> None:
    concierge = ScoutHandoffConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    assert _create(api, headers, {"slug": "retired", "name": "Retired"}).status_code == 201
    assert api.client.post("/v1/agents/retired/archive", headers=headers).status_code == 200
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@planner plan the bake sale"},
    )
    assert accepted.status_code == 202, accepted.text
    done = settled(api.client, headers, channel, accepted.json()["turn"]["id"])

    assert done["status"] == "completed", done
    assert concierge.handoffs[0].startswith("Queued @scout")
    assert concierge.handoffs[1].startswith("refused: ")
    assert [p["agent_slug"] for p in done["participants"]] == ["planner", "scout"]
    assert [c["session_key"] for c in concierge.calls] == [
        f"{channel}:planner",
        f"{channel}:scout",
    ]
    # The handoff tool offers the saved agent, not the archived one.
    offered = concierge.calls[0]["handoff_agents"]
    assert "scout" in offered and "critic" in offered
    assert "retired" not in offered


def test_disabled_agents_are_listed_on_request_for_writers(api: Any) -> None:
    headers = bearer(register(api))
    assert _create(api, headers).status_code == 201
    assert api.client.post("/v1/agents/scout/archive", headers=headers).status_code == 200

    default = api.client.get("/v1/agents", headers=headers).json()
    assert "scout" not in {a["slug"] for a in default}
    everything = api.client.get("/v1/agents?include_disabled=true", headers=headers)
    assert everything.status_code == 200, everything.text
    archived = {a["slug"]: a for a in everything.json()}["scout"]
    assert archived["enabled"] is False
    assert "planner" in {a["slug"] for a in everything.json()}

    reader = api.bearer(frozenset({"collaboration:read"}))
    assert api.client.get("/v1/agents", headers=reader).status_code == 200
    refused = api.client.get("/v1/agents?include_disabled=true", headers=reader)
    assert refused.status_code == 403

    # A disabled (not archived) agent is found the same way and switched back on.
    assert _create(api, headers, {"slug": "napper", "name": "Napper"}).status_code == 201
    off = api.client.patch(
        "/v1/agents/napper", json={"expected_revision": 1, "enabled": False}, headers=headers
    )
    assert off.status_code == 200, off.text
    assert "napper" not in {a["slug"] for a in api.client.get("/v1/agents", headers=headers).json()}
    found = {
        a["slug"]: a
        for a in api.client.get("/v1/agents?include_disabled=true", headers=headers).json()
    }["napper"]
    assert found["enabled"] is False
    assert found["editable"] is True
    on = api.client.patch(
        "/v1/agents/napper",
        json={"expected_revision": found["revision"], "enabled": True},
        headers=headers,
    )
    assert on.status_code == 200, on.text
    assert "napper" in {a["slug"] for a in api.client.get("/v1/agents", headers=headers).json()}


def test_agent_and_team_slugs_do_not_collide(api: Any) -> None:
    headers = bearer(register(api))
    team = api.client.post("/v1/teams", headers=headers, json={"name": "Bakers", "slug": "bakers"})
    assert team.status_code == 201, team.text

    clash = _create(api, headers, {"slug": "bakers", "name": "Baker"})
    assert clash.status_code == 409, clash.text
    assert clash.json()["code"] == "slug_taken"
    alias_clash = _create(api, headers, {**SCOUT, "aliases": ["bakers"]})
    assert alias_clash.status_code == 409
    assert alias_clash.json()["code"] == "slug_taken"
    assert api.client.get("/v1/agents/bakers", headers=headers).status_code == 404

    assert _create(api, headers, {**SCOUT, "aliases": ["finder"]}).status_code == 201
    renamed_alias = api.client.patch(
        "/v1/agents/scout",
        json={"expected_revision": 1, "aliases": ["bakers"]},
        headers=headers,
    )
    assert renamed_alias.status_code == 409
    assert renamed_alias.json()["code"] == "slug_taken"

    for slug in ("scout", "finder", "planner", "angie"):
        refused = api.client.post(
            "/v1/teams", headers=headers, json={"name": "Clash", "slug": slug}
        )
        assert refused.status_code == 409, (slug, refused.text)
        assert refused.json()["code"] == "slug_taken"
    renamed = api.client.patch(
        f"/v1/teams/{team.json()['id']}", headers=headers, json={"slug": "scout"}
    )
    assert renamed.status_code == 409
    assert renamed.json()["code"] == "slug_taken"
    assert api.client.get("/v1/teams", headers=headers).json()[0]["slug"] == "bakers"


def test_a_saved_agent_answers_with_its_own_model(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    created = _create(api, headers, {**SCOUT, "model": "scout-model"})
    assert created.status_code == 201, created.text
    assert created.json()["model"] == "scout-model"
    assert created.json()["model_source"] == "agent.model"
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@scout and @planner, what do we need?"},
    )
    assert accepted.status_code == 202, accepted.text
    settled(api.client, headers, channel, accepted.json()["turn"]["id"])

    models = {c["session_key"].rsplit(":", 1)[-1]: c.get("model") for c in concierge.calls}
    assert models == {"scout": "scout-model", "planner": None}
