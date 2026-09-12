"""The `config_keys` rendering (#970): sections with counts, cards without
reprs, and a bound that cuts on a card and says how many it left out."""

from __future__ import annotations

from pathlib import Path

from sbxloop.configedit import ConfigEditor, Row
from sbxloop.daemon import configview
from sbxloop.daemon.configpolicy import never_from_chat
from sbxloop.paths import SbxloopHome


def _rows(tmp_path: Path, text: str) -> list[Row]:
    home = SbxloopHome(tmp_path / ".sbxloop")
    home.ensure_tree()
    home.config_toml.write_text(text)
    return ConfigEditor(home, {"HOME": str(tmp_path)}).resolved()


def test_sections_count_keys_and_what_the_file_sets(tmp_path: Path) -> None:
    rows = _rows(tmp_path, "[daemon]\nmax_runs_per_day = 20\npoll_interval_s = 30.0\n")
    text = configview.sections(rows)
    assert text.startswith(f"{len(rows)} keys in ")
    (daemon,) = [line for line in text.splitlines() if line.startswith("- `daemon`")]
    assert "2 set in the operator's file" in daemon
    assert "the always-on outer loop" in daemon  # the section's own doc line
    (top,) = [line for line in text.splitlines() if configview.TOP_LEVEL in line]
    assert "0 set in the operator's file" in top
    assert not any("`github.repos`" in line and "[" in line for line in text.splitlines())


def test_cards_read_as_an_operator_writes_them(tmp_path: Path) -> None:
    rows = {r.key: r for r in _rows(tmp_path, '[landing]\nmerge_method = "squash"\n')}
    text = configview.card(rows["landing.merge_method"], refusal=never_from_chat)
    head, doc = text.split("\n", 1)
    assert head.startswith("`landing.merge_method` = squash · set by home config · accepts str")
    assert "one of auto, squash, merge, rebase" in head and "applies restart" in head
    assert "'squash'" not in head  # no repr quotes (#835)
    assert doc.startswith("  auto | squash | merge | rebase")
    live = configview.card(rows["agent.models.build"], refusal=never_from_chat)
    assert "applies live" in live and "set by default" in live
    channel = configview.card(rows["discord.channel_id"], refusal=never_from_chat)
    assert "never from chat: it configures the chat channel" in channel


def test_matches_by_prefix_on_whole_segments_and_grep_on_key_or_doc(tmp_path: Path) -> None:
    rows = _rows(tmp_path, '[[github.repos]]\nrepo = "acme/app"\n')
    keys = {r.key for r in rows if configview.matches(r, prefix="daemon", grep=None)}
    assert "daemon.max_runs_per_day" in keys and not any(k.startswith("daemonx") for k in keys)
    entry = {r.key for r in rows if configview.matches(r, prefix="github.repos[0]", grep=None)}
    assert "github.repos[0].repo" in entry and "github.repo" not in entry
    one = [r for r in rows if configview.matches(r, prefix=None, grep="calendar-day")]
    assert [r.key for r in one] == ["daemon.max_runs_per_day"]
    assert not any(
        configview.matches(r, prefix="daemon", grep="calendar-day")
        for r in rows
        if r.key != "daemon.max_runs_per_day"
    )


def test_bounded_cuts_on_a_card_and_says_how_many_were_left() -> None:
    cards = [f"`k{i}` = {i} · set by default" for i in range(20)]
    text = configview.bounded(cards, 400)
    kept = [line for line in text.splitlines() if line.startswith("`k")]
    assert 0 < len(kept) < 20
    assert all(line in cards for line in kept)
    assert text.endswith(f"… {20 - len(kept)} more — narrow the prefix or add grep")
    assert configview.bounded(cards[:2], 10_000) == "\n".join(cards[:2])
    assert configview.bounded([], 100) == "no key matches"
    # one card always survives, however small the bound
    assert configview.bounded(cards[:1], 10).startswith("`k0`")
