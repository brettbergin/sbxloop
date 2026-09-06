"""The workload profiles as data (#804): the bounds each profile sets,
spelled out for the console and the doctor row."""

from __future__ import annotations

from sbxloop.cli.workloadview import (
    NO_EGRESS,
    profile_summary,
    profile_view,
    profile_views,
)
from sbxloop.config import Config, ScheduleConfig


def config(**over: object) -> Config:
    raw: dict[str, object] = {
        "credentials": [{"name": "weather", "env": "WEATHER_KEY", "host": "api.weather.example"}],
        "workloads": [
            {
                "name": "research",
                "description": "reads the web",
                "egress": ["*.example.com", "api.weather.example"],
                "credentials": ["weather"],
                "sinks": ["artifact", "issue"],
                "repo": True,
                "publish": "hold",
                "budgets": {"max_tasks": 3},
            },
            {"name": "quiet"},
        ],
        "workload": {"default": "quiet"},
    }
    raw.update(over)
    return Config.model_validate(raw)


def test_a_profile_is_spelled_out_the_way_the_operator_is_told() -> None:
    cfg = config()
    view = profile_view(cfg, cfg.workloads[0])
    assert view.name == "research" and view.default is False
    assert view.title == "research" and view.description == "reads the web"
    rows = dict(view.rows)
    assert rows["egress"] == "*.example.com, api.weather.example"
    assert rows["credentials"] == "weather"  # the name, never the value
    assert rows["sinks"] == "chat, artifact, issue"  # chat is always allowed
    assert rows["repo"] == "a plan may ask for a checkout"
    assert rows["publish"] == "hold"
    assert rows["budgets"] == "max_tasks = 3"
    assert "WEATHER_KEY" not in str(view.rows)


def test_a_bare_profile_says_what_it_refuses() -> None:
    cfg = config()
    view = profile_view(cfg, cfg.workloads[1])
    assert view.title == "quiet (default)"
    rows = dict(view.rows)
    assert rows["egress"] == NO_EGRESS
    assert rows["credentials"] == "none" and rows["sinks"] == "chat"
    assert rows["repo"] == "no checkout" and rows["publish"] == "auto"
    assert rows["budgets"] == "[budgets] as configured"


def test_views_put_the_default_first_and_hang_schedules_under_their_profile() -> None:
    cfg = config()
    schedules = [
        ScheduleConfig(
            name="brief", profile="research", ask="x", cron="0 7 * * mon-fri", timezone="UTC"
        ),
        ScheduleConfig(name="tick", profile="quiet", ask="y", every="1h"),
    ]
    views = profile_views(cfg, schedules)
    assert [v.name for v in views] == ["quiet", "research"]
    assert views[0].schedules == ("tick · every 1h",)
    assert views[1].schedules == ("brief · cron 0 7 * * mon-fri (UTC)",)


def test_the_doctor_summary_counts_and_names_the_sinks() -> None:
    cfg = config()
    assert profile_summary(cfg, cfg.workloads[0]) == (
        "research: egress 2 patterns, credentials 1, sinks 3 (chat, artifact, issue), "
        "repo yes, publish hold"
    )
    assert profile_summary(cfg, cfg.workloads[1]) == (
        "quiet (default): egress 0 patterns, credentials 0, sinks 1 (chat), repo no, publish auto"
    )
