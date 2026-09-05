"""The plan prompt tells the operator what this run may ask for (#758):
the profile's hosts, credentials by name (never a value), sinks and repo
allowance — or that there is no profile and nothing can be granted."""

from __future__ import annotations

import json

from sbxloop.config import Config
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult

PLAN = {"title": "t", "tasks": [{"id": "t1", "title": "look"}]}


class PlanningAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        assert job.prompt is not None
        self.prompts.append(job.prompt)
        return JobResult(
            job_id=job.job_id, status="ok", output_json=PLAN, output_text=json.dumps(PLAN)
        )


def _config(**overrides: object) -> Config:
    return Config.model_validate(
        {
            "credentials": [
                {
                    "name": "weather",
                    "env": "WEATHER_API_KEY",
                    "host": "api.weather.example.com",
                    "description": "forecasts",
                },
                {"name": "keyed", "env": "KEYED_TOKEN", "host": "keyed.example.com"},
            ],
            **overrides,
        }
    )


def _plan_prompt(config: Config) -> str:
    agent = PlanningAgent()
    PhaseRunner(agent, config, "r1", "find out", workdir="/data").plan_workload()  # type: ignore[arg-type]
    return agent.prompts[0]


def test_the_profile_bounds_reach_the_planner() -> None:
    config = _config(
        workloads=[
            {
                "name": "research",
                "description": "reads the web",
                "egress": ["*.example.com"],
                "credentials": ["weather", "keyed"],
                "sinks": ["chat", "artifact"],
                "repo": True,
            }
        ],
        workload={"default": "research"},
    )
    text = _plan_prompt(config)
    assert "## What this run may ask for" in text
    assert "Profile `research` — reads the web:" in text
    assert "- hosts: `*.example.com`" in text
    assert (
        "- credentials, by name: `weather` (api.weather.example.com: forecasts), "
        "`keyed` (keyed.example.com)" in text
    )
    assert "- sinks: `chat` (always; the default when a task names none), `artifact`" in text
    assert "(`repo`, as `owner/name`, one configured for this host): allowed" in text
    assert "WEATHER_API_KEY" not in text and "KEYED_TOKEN" not in text


def test_an_empty_profile_says_so_line_by_line() -> None:
    config = _config(workloads=[{"name": "bare"}], workload={"default": "bare"})
    text = _plan_prompt(config)
    assert "Profile `bare`:" in text
    assert "- hosts: none beyond the always-reachable package registries" in text
    assert "- credentials: none" in text
    # Even an empty profile can answer: chat needs no granting (#759).
    assert "- sinks: `chat` (always; the default when a task names none)\n" in text
    assert "one configured for this host): not allowed" in text


def test_without_a_profile_the_planner_is_told_to_declare_no_needs() -> None:
    text = _plan_prompt(_config())
    assert "This run has no workload profile" in text
    assert "Declare no needs" in text
    assert "Profile `" not in text
