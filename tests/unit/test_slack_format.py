"""Exact Slack mrkdwn of the re-dialecting layer (no slack_sdk needed)."""

from __future__ import annotations

from sbxloop.daemon.discord_format import EmbedSpec, status_embed
from sbxloop.daemon.slack_format import (
    EMOJI_NAMES,
    embed_attachment,
    escape,
    thread_permalink,
    to_mrkdwn,
)


class TestToMrkdwn:
    def test_bold_strike_and_headings(self) -> None:
        assert to_mrkdwn("**planner** · `claude`") == "*planner* · `claude`"
        assert to_mrkdwn("~~gone~~ and **kept**") == "~gone~ and *kept*"
        assert to_mrkdwn("## Plan\n- one") == "*Plan*\n- one"

    def test_links(self) -> None:
        assert to_mrkdwn("[issue #4](https://x/4)") == "<https://x/4|issue #4>"
        # already angle-bracketed by no_unfurl: kept, not double-wrapped
        assert to_mrkdwn("see <https://x/y?a=1&b=2>") == "see <https://x/y?a=1&b=2>"
        # a bare URL is bracketed so its & is not entity-mangled
        assert to_mrkdwn("see https://x/y?a=1&b=2 now") == "see <https://x/y?a=1&b=2> now"
        # a labelled Slack link (the thread pointer) survives verbatim
        link = "<https://slack.com/archives/C1/p1|thread>"
        assert to_mrkdwn(f"chronology: {link}") == f"chronology: {link}"

    def test_entities_in_prose(self) -> None:
        assert to_mrkdwn("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
        # a stray <!channel> in agent prose is text, never a broadcast
        assert to_mrkdwn("hey <!channel>") == "hey &lt;!channel&gt;"

    def test_user_mentions_are_escaped_unless_asked_for(self) -> None:
        assert to_mrkdwn("thanks <@U123>") == "thanks &lt;@U123&gt;"
        assert to_mrkdwn("<@U123> run `r1` done", mentions=True) == "<@U123> run `r1` done"
        # channel references are always kept
        assert to_mrkdwn("see <#C0123ABCDEF>") == "see <#C0123ABCDEF>"

    def test_code_keeps_its_body_and_drops_the_fence_language(self) -> None:
        fenced = "```py\nif a < b: print('**x**')\n```"
        assert to_mrkdwn(fenced) == "```\nif a &lt; b: print('**x**')\n```"
        assert to_mrkdwn("```\nplain\n```") == "```\nplain\n```"
        assert to_mrkdwn("run `a && b` **now**") == "run `a &amp;&amp; b` *now*"
        # a URL inside code is not turned into a link token
        assert to_mrkdwn("`curl https://x/y`") == "`curl https://x/y`"

    def test_escape_is_plain(self) -> None:
        assert escape("<&>") == "&lt;&amp;&gt;"


class TestEmbedAttachment:
    def test_card_shape(self) -> None:
        spec = EmbedSpec(
            title="run r1",
            description="**Fix login** · gh:issue:4",
            url="https://x/4",
            color=0x2ECC71,
            fields=(("state", "✅ merged", True), ("tasks", "2/2", True)),
            footer="sbxloop 1.0",
        )
        att = embed_attachment(spec)
        assert att["color"] == "#2ECC71"
        assert att["blocks"][0] == {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*<https://x/4|run r1>*\n*Fix login* · gh:issue:4",
            },
        }
        fields = att["blocks"][1]["fields"]
        assert fields == [
            {"type": "mrkdwn", "text": "*state*\n✅ merged"},
            {"type": "mrkdwn", "text": "*tasks*\n2/2"},
        ]
        assert att["blocks"][2]["type"] == "context"
        assert att["fallback"].startswith("**run r1**")

    def test_fields_split_ten_per_section_and_colour_optional(self) -> None:
        spec = EmbedSpec(fields=tuple((f"f{i}", str(i), True) for i in range(23)))
        att = embed_attachment(spec)
        assert "color" not in att
        sizes = [len(b["fields"]) for b in att["blocks"] if b["type"] == "section"]
        assert sizes == [10, 10, 3]

    def test_status_card_converts(self) -> None:
        att = embed_attachment(
            status_embed(
                {
                    "current": None,
                    "queued": 2,
                    "runs_today": 1,
                    "max_runs_per_day": 12,
                    "run_cap_timezone": "UTC",
                    "breaker_open": False,
                    "paused": False,
                    "holds": [],
                    "claiming": None,
                    "stopping": False,
                    "repos": [],
                }
            )
        )
        assert att["blocks"][0]["text"]["text"].startswith("*sbxloop daemon*")


def test_permalink_and_reaction_names() -> None:
    assert (
        thread_permalink("C0123ABCDEF", "1724968573.123456")
        == "https://slack.com/archives/C0123ABCDEF/p1724968573123456"
    )
    assert EMOJI_NAMES["⏳"] == "hourglass_flowing_sand"
    assert EMOJI_NAMES["✅"] == "white_check_mark"
    assert EMOJI_NAMES["⚠"] == "warning"
