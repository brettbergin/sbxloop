"""Plan-declared egress: pattern matching, bounds, and the grant-late granter."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.engine.model import EgressSpec
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.policy import (
    APT_MIRROR_DOMAINS,
    BASELINE_REGISTRY_DOMAINS,
    PROMPT_ADVERTISED_DOMAINS,
    EgressGranter,
    baseline_allows,
    effective_egress_bounds,
    egress_rejection,
    pattern_covers,
    valid_pattern,
)
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx


class TestPatterns:
    def test_exact_match(self) -> None:
        assert pattern_covers("registry.npmjs.org", "registry.npmjs.org")
        assert not pattern_covers("registry.npmjs.org", "npmjs.org")

    def test_case_insensitive(self) -> None:
        assert pattern_covers("Example.COM", "example.com")

    def test_wildcard_covers_base_and_subdomains(self) -> None:
        assert pattern_covers("*.example.com", "example.com")
        assert pattern_covers("*.example.com", "api.example.com")
        assert pattern_covers("*.example.com", "deep.api.example.com")
        assert not pattern_covers("*.example.com", "example.org")
        assert not pattern_covers("*.example.com", "badexample.com")

    def test_wildcard_request_needs_covering_wildcard(self) -> None:
        assert pattern_covers("*.example.com", "*.example.com")
        assert pattern_covers("*.example.com", "*.api.example.com")
        assert not pattern_covers("api.example.com", "*.example.com")

    def test_star_covers_everything(self) -> None:
        assert pattern_covers("*", "anything.example")
        assert pattern_covers("*", "*.example.com")

    def test_valid_pattern(self) -> None:
        assert valid_pattern("pypi.org")
        assert valid_pattern("*.crates.io")
        assert not valid_pattern("*")
        assert valid_pattern("*", operator=True)
        assert not valid_pattern("https://pypi.org")
        assert not valid_pattern("pypi.org/simple")
        assert not valid_pattern("pypi.org:443")
        assert not valid_pattern("localhost")


class TestBaselineTiers:
    """The always-reachable tier #141 grows one language at a time."""

    def test_advertised_is_registries_plus_mirrors(self) -> None:
        # The prompts advertise exactly the two baseline tiers, so a domain
        # can never be promoted into one without the prompts covering it.
        assert (*BASELINE_REGISTRY_DOMAINS, *APT_MIRROR_DOMAINS) == PROMPT_ADVERTISED_DOMAINS

    def test_python_registry_is_baseline(self) -> None:
        # #145: PyPI keeps its baseline privilege — the level-up direction —
        # and the rest of Layer 2 joins it here rather than PyPI joining the
        # declarable tier.
        assert "pypi.org" in BASELINE_REGISTRY_DOMAINS
        assert "files.pythonhosted.org" in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        for domain in BASELINE_REGISTRY_DOMAINS:
            assert egress_rejection(domain, allow, deny) is None

    def test_baseline_allows_drops_denied_domains(self) -> None:
        # The baseline is seeded at provision time, before any plan exists,
        # so this filter is the only place [policy] deny can reach it.
        kept = baseline_allows(PROMPT_ADVERTISED_DOMAINS, ["pypi.org"])
        assert "pypi.org" not in kept
        assert "files.pythonhosted.org" in kept
        assert "archive.ubuntu.com" in kept

    def test_baseline_allows_honors_wildcards(self) -> None:
        assert baseline_allows(("pypi.org", "files.pythonhosted.org"), ["*.pythonhosted.org"]) == [
            "pypi.org"
        ]
        assert baseline_allows(PROMPT_ADVERTISED_DOMAINS, ["*"]) == []

    def test_baseline_allows_is_a_no_op_without_deny(self) -> None:
        assert baseline_allows(PROMPT_ADVERTISED_DOMAINS, []) == list(PROMPT_ADVERTISED_DOMAINS)


class TestBounds:
    def test_deny_wins_over_allow(self) -> None:
        reason = egress_rejection("evil.example.com", ["*.example.com"], ["evil.example.com"])
        assert reason is not None and "deny" in reason

    def test_unlisted_domain_rejected(self) -> None:
        reason = egress_rejection("registry.npmjs.org", [], [])
        assert reason is not None and "allow" in reason

    def test_allowed_domain_passes(self) -> None:
        assert egress_rejection("registry.npmjs.org", ["registry.npmjs.org"], []) is None

    def test_effective_bounds_include_baseline_and_advertised(self) -> None:
        config = Config.model_validate(
            {
                "sandbox": {"extra_allow_domains": ["internal.example.com"]},
                "policy": {"allow": ["registry.npmjs.org"]},
            }
        )
        allow, _deny = effective_egress_bounds(config)
        # declaring an already-reachable domain must never fail a plan
        assert "api.githubcopilot.com" in allow
        assert "internal.example.com" in allow
        assert "pypi.org" in allow
        assert "registry.npmjs.org" in allow

    def test_well_known_registries_in_bounds_by_default(self) -> None:
        # A default config (no [policy] allow) must accept plans declaring
        # the major package registries — the "write a Rails app" case.
        allow, _deny = effective_egress_bounds(Config())
        for domain in (
            "rubygems.org",
            "index.rubygems.org",
            "registry.npmjs.org",
            "registry.yarnpkg.com",
            "crates.io",
            "static.crates.io",
            "index.crates.io",
            "proxy.golang.org",
            "sum.golang.org",
        ):
            assert egress_rejection(domain, allow, _deny) is None

    def test_deny_still_blocks_well_known_registry(self) -> None:
        config = Config.model_validate({"policy": {"deny": ["rubygems.org"]}})
        allow, deny = effective_egress_bounds(config)
        reason = egress_rejection("rubygems.org", allow, deny)
        assert reason is not None and "deny" in reason


class TestEgressSpec:
    def test_normalizes_domain(self) -> None:
        assert EgressSpec(domain="  Registry.NPMJS.org ").domain == "registry.npmjs.org"

    @pytest.mark.parametrize(
        "bad", ["https://pypi.org", "pypi.org/simple", "*", "not a domain", ""]
    )
    def test_rejects_non_domains(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            EgressSpec(domain=bad)


class TestEgressGranter:
    def make_granter(
        self, fake_sbx: FakeSbx, events: list[Event], **config_overrides: object
    ) -> EgressGranter:
        config = Config.model_validate(config_overrides)
        bus = EventBus()
        bus.subscribe(events.append)
        return EgressGranter(
            SbxCLI(binary=str(fake_sbx.binary)), config, bus, "r1", "sbxloop-r1-agent"
        )

    def test_grants_in_bounds_domain_and_emits_event(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events, policy={"allow": ["registry.npmjs.org"]})
        granter.apply("t1", [("registry.npmjs.org", "npm install")])
        assert [
            "allow",
            "network",
            "registry.npmjs.org",
            "--sandbox",
            "sbxloop-r1-agent",
        ] in fake_sbx.policies()
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_ALLOW]
        assert event.data["domain"] == "registry.npmjs.org"
        assert event.data["reason"] == "npm install"
        assert event.data["task_id"] == "t1"

    def test_grant_is_idempotent_per_domain(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events, policy={"allow": ["*.crates.io"]})
        granter.apply("t1", [("static.crates.io", "cargo build")])
        granter.apply("t2", [("static.crates.io", "cargo build again")])
        assert len(fake_sbx.policies()) == 1
        assert len([e for e in events if e.type == HostEventTypes.POLICY_ALLOW]) == 1

    def test_well_known_registry_granted_without_operator_config(self, fake_sbx: FakeSbx) -> None:
        # Registries are declarable-not-baseline: in bounds with a default
        # config, but still granted via `sbx policy allow` (and event-logged)
        # rather than pre-seeded.
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [("rubygems.org", "bundle install for the Rails app")])
        assert [
            "allow",
            "network",
            "rubygems.org",
            "--sandbox",
            "sbxloop-r1-agent",
        ] in fake_sbx.policies()
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_ALLOW]
        assert event.data["domain"] == "rubygems.org"

    def test_baseline_domain_needs_no_grant(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [("api.github.com", "call the API")])
        assert fake_sbx.policies() == []
        assert [e for e in events if e.type.startswith("policy.")] == []

    def test_prompt_advertised_domain_needs_no_grant(self, fake_sbx: FakeSbx) -> None:
        # PyPI/apt mirrors are granted at provision time, so a plan
        # declaring one is in-bounds AND needs no re-grant.
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [("archive.ubuntu.com", "apt-get for build deps")])
        assert fake_sbx.policies() == []
        assert [e for e in events if e.type.startswith("policy.")] == []

    def test_baseline_registry_needs_no_grant(self, fake_sbx: FakeSbx) -> None:
        # The parity promise: a plan may name the language registry, and it
        # costs nothing — no grant, no event, no failure when it forgets.
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [("pypi.org", "pip install the deps")])
        assert fake_sbx.policies() == []
        assert [e for e in events if e.type.startswith("policy.")] == []

    def test_denied_baseline_registry_is_refused(self, fake_sbx: FakeSbx) -> None:
        # [policy] deny wins over the always-reachable tier: provisioning
        # never seeded it (baseline_allows), so a declaration is refused
        # rather than silently treated as already granted.
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events, policy={"deny": ["pypi.org"]})
        granter.apply("t1", [("pypi.org", "pip install the deps")])
        assert fake_sbx.policies() == []
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_DENY]
        assert event.data["domain"] == "pypi.org"

    def test_out_of_bounds_refused_with_deny_event(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [("exfil.example.com", "totally legit")])
        assert fake_sbx.policies() == []
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_DENY]
        assert event.data["domain"] == "exfil.example.com"

    def test_deny_pattern_blocks_allowed_domain(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(
            fake_sbx,
            events,
            policy={"allow": ["*.example.com"], "deny": ["secrets.example.com"]},
        )
        granter.apply("t1", [("secrets.example.com", "read the secrets")])
        assert fake_sbx.policies() == []
        assert [e.type for e in events if e.type.startswith("policy.")] == [
            HostEventTypes.POLICY_DENY
        ]
