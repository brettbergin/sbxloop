"""The console's rendering helpers: the bridges' dialect into what a
terminal shows, cards, ages and labels."""

from __future__ import annotations

import time

from sbxloop.daemon.discord_format import EmbedSpec
from sbxloop.tui.format import (
    SPEND_NOT_REPORTED,
    age,
    card,
    duration,
    state_label,
    to_commonmark,
    to_rich,
    tokens,
)


def test_to_commonmark_unmasks_links_mentions_and_threads() -> None:
    text = "see <https://x/pull/3> and <@123> in <#thread:9>, keep `<@5>` and ```<https://c>```"
    out = to_commonmark(text, names={"123": "brett"})
    assert "https://x/pull/3" in out and "<https://x/pull/3>" not in out
    assert "@brett" in out and "<@123>" not in out
    assert "`thread:9`" in out
    assert "`<@5>`" in out and "```<https://c>```" in out, "code spans are left alone"


def test_to_rich_styles_bold_code_links_and_mentions() -> None:
    rich = to_rich("**bold** `code` [pr](https://x/pull/3) <@1> https://y")
    assert rich.plain == "bold code pr @1 https://y"
    styles = {span.style for span in rich.spans}
    assert "bold" in styles and "cyan" in styles
    assert any("link https://x/pull/3" in str(s) for s in styles)
    assert "dim" in styles


def test_card_renders_fields_and_colour() -> None:
    spec = EmbedSpec(
        title="run `r1`",
        description="**Add retries**",
        color=0x2ECC71,
        fields=(("State", "merged", True), ("PR", "[#3](https://x/pull/3)", True)),
        footer="sbxloop · host",
    )
    panel = card(spec)
    assert panel.border_style == "green"
    from rich.console import Console

    console = Console(record=True, width=80)
    console.print(panel)
    text = console.export_text()
    assert "State" in text and "merged" in text and "PR" in text and "sbxloop · host" in text


def test_age_duration_tokens_and_labels() -> None:
    now = time.time()
    assert age(None) == "—" and age(now - 5, now) == "5s ago"
    assert age(now - 120, now) == "2m ago" and age(now - 7200, now) == "2h ago"
    assert age(now - 3 * 86400, now) == "3d ago"
    assert duration(None) == "—" and duration(5) == "5s" and duration(125) == "2m 05s"
    assert duration(3700) == "1h 01m"
    assert tokens(None) == "—" and tokens(999) == "999" and tokens(4200) == "4k"
    assert tokens(2_500_000) == "2.5M"
    label = state_label("failed", "orphaned")
    assert label.plain.endswith("failed — orphaned") and "❌" in label.plain
    assert "❌" not in state_label("failed", emoji=False).plain
    assert "currency" not in SPEND_NOT_REPORTED and "not reported" in SPEND_NOT_REPORTED
