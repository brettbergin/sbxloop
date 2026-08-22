"""Per-phase system-message trimming.

Field measurement (run rews3ssdn, 272 turns across 25 jobs) put ~22k tokens
of fixed context on EVERY turn of every session — about 62% of the run's
input spend — while the phase prompt itself was under 2k of it. The rest is
the agent SDK's system message and tool schemas, and the whole thing is
re-sent on every turn. A section a phase cannot act on is therefore not paid
for once; it is paid for on every turn, for the life of the session.

The trim is deliberately narrow: only ``code_change_rules``, and only from
the phases that write no code. These tests pin that narrowness, because the
failure mode of over-trimming is a critic that cannot do its job (#123) and
that failure is silent.
"""

from __future__ import annotations

from sbxloop.config import Config
from sbxloop.engine.phases import AGENT_NAMES, PHASE_DROP_SECTIONS, PhaseRunner
from sbxloop_worker.backends.copilot import (
    SDK_SYSTEM_MESSAGE_SECTIONS,
    system_message_config,
)
from sbxloop_worker.protocol import JobRequest, JobResult


class TestPhaseDropSections:
    def test_every_phase_has_a_policy(self) -> None:
        assert set(PHASE_DROP_SECTIONS) == set(AGENT_NAMES)

    def test_only_sections_the_sdk_knows_are_dropped(self) -> None:
        for phase, sections in PHASE_DROP_SECTIONS.items():
            assert set(sections) <= SDK_SYSTEM_MESSAGE_SECTIONS, phase

    def test_execute_keeps_everything(self) -> None:
        """EXECUTE is the only phase with write permission, so it is the only
        one that can act on coding rules — and the one phase where being
        wrong about that is most expensive."""
        assert PHASE_DROP_SECTIONS["execute"] == ()

    def test_the_trim_stays_narrow(self) -> None:
        """Guardrail sections stay. ``tool_instructions``/``tool_efficiency``
        make sessions cheaper, not dearer; ``tone`` governs output format and
        a chattier reply costs a whole retry session; ``safety`` is not worth
        trimming for the bytes."""
        keep = {"tool_instructions", "tool_efficiency", "tone", "safety"}
        for phase, sections in PHASE_DROP_SECTIONS.items():
            assert not keep & set(sections), phase


class _StubAgent:
    def __init__(self) -> None:
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        self.jobs.append(job)
        return JobResult(job_id=job.job_id, status="ok", output_json={"verdict": "accept"})


def _runner(config: Config) -> tuple[PhaseRunner, _StubAgent]:
    agent = _StubAgent()
    return PhaseRunner(agent, config, "r1", "ship it", workdir="/work"), agent  # type: ignore[arg-type]


def _trimming(enabled: bool) -> Config:
    config = Config()
    config.budgets.trim_system_message = enabled
    return config


class TestPhaseRunnerDropSections:
    def test_trimming_is_off_by_default(self) -> None:
        """An unmeasured optimisation does not ship on. Whether that 22k/turn
        is billed or cached is answerable from the usage fields shipped
        alongside this, and whether the SDK accepts the `customize` shape is
        answerable only from a real run — which the deploy's health check
        does not start."""
        assert Config().budgets.trim_system_message is False
        runner, _ = _runner(Config())
        assert runner._drop_sections("scrutinize") == []

    def test_the_policy_reaches_the_job_when_enabled(self) -> None:
        runner, _ = _runner(_trimming(True))
        assert runner._drop_sections("scrutinize") == ["code_change_rules"]
        assert runner._drop_sections("execute") == []

    def test_an_unknown_phase_drops_nothing(self) -> None:
        runner, _ = _runner(_trimming(True))
        assert runner._drop_sections("nonesuch") == []


class TestSystemMessageConfig:
    def test_no_content_and_no_sections_leaves_the_sdk_alone(self) -> None:
        assert system_message_config(None, []) is None

    def test_content_alone_still_appends(self) -> None:
        """The concierge's existing behaviour must be untouched by this."""
        assert system_message_config("extra") == {"mode": "append", "content": "extra"}

    def test_sections_switch_to_customize(self) -> None:
        """``customize`` keeps the SDK-managed prompt structure — unlike
        ``replace``, which would discard the guardrails with it."""
        assert system_message_config(None, ["code_change_rules"]) == {
            "mode": "customize",
            "sections": {"code_change_rules": {"action": "remove"}},
        }

    def test_content_rides_along_with_sections(self) -> None:
        config = system_message_config("extra", ["guidelines"])
        assert config is not None
        assert config["mode"] == "customize"
        assert config["content"] == "extra"

    def test_sections_the_installed_sdk_does_not_know_are_dropped(self) -> None:
        """A host ahead of the sandbox's SDK degrades to today's behaviour
        rather than failing the job on a name the SDK would reject."""
        config = system_message_config(
            None, ["code_change_rules", "invented"], known_sections=frozenset({"code_change_rules"})
        )
        assert config == {
            "mode": "customize",
            "sections": {"code_change_rules": {"action": "remove"}},
        }

    def test_filtering_away_every_section_falls_back(self) -> None:
        assert system_message_config(None, ["invented"], known_sections=frozenset()) is None
        assert system_message_config("extra", ["invented"], known_sections=frozenset()) == {
            "mode": "append",
            "content": "extra",
        }

    def test_duplicate_sections_collapse(self) -> None:
        config = system_message_config(None, ["tone", "tone"])
        assert config is not None
        assert list(config["sections"]) == ["tone"]
