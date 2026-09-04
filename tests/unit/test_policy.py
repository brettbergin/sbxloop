"""Plan-declared egress: pattern matching, bounds, and the grant-late granter."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.engine.model import EgressSpec
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.policy import (
    APT_MIRROR_DOMAINS,
    BASELINE_REGISTRY_DOMAINS,
    PROMPT_ADVERTISED_DOMAINS,
    WELL_KNOWN_REGISTRY_DOMAINS,
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

    def test_tiers_do_not_overlap(self) -> None:
        # A domain in both tiers would be a promotion that forgot to remove
        # the old entry: reachable, but still advertised as declare-me.
        assert not set(BASELINE_REGISTRY_DOMAINS) & set(WELL_KNOWN_REGISTRY_DOMAINS)

    def test_node_registry_is_baseline(self) -> None:
        # #148: npm/yarn joined PyPI's tier, so an npm build no longer fails
        # for a reason a pip build never encounters.
        for domain in ("registry.npmjs.org", "registry.yarnpkg.com", "codeload.github.com"):
            assert domain in BASELINE_REGISTRY_DOMAINS

    def test_go_module_proxy_and_checksum_db_are_both_baseline(self) -> None:
        # #154: the pair matters. A reachable proxy with an unreachable
        # checksum database fails `go mod download` at verification — a
        # confusing partial failure, not a graceful degradation.
        for domain in ("proxy.golang.org", "sum.golang.org"):
            assert domain in BASELINE_REGISTRY_DOMAINS
            assert domain not in WELL_KNOWN_REGISTRY_DOMAINS

    def test_ruby_registry_hosts_are_baseline(self) -> None:
        # #159: the case policy.py itself cited as motivating the declarable
        # tier — "write a Rails app" bundle-installing out of the box. It no
        # longer needs the plan to remember. Both hosts: bundler's compact
        # index is a different host from gem downloads.
        for domain in ("rubygems.org", "index.rubygems.org"):
            assert domain in BASELINE_REGISTRY_DOMAINS

    def test_java_needs_maven_central_and_gradle(self) -> None:
        # #162: Java was in neither tier — a plan could not even declare
        # Maven Central without operator configuration. Gradle needs the
        # plugin portal and the wrapper distribution host on top of the
        # registry; Central alone still fails the build.
        for domain in (
            "repo.maven.apache.org",
            "repo1.maven.org",
            "plugins.gradle.org",
            "services.gradle.org",
        ):
            assert domain in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        assert egress_rejection("repo.maven.apache.org", allow, deny) is None

    def test_dotnet_nuget_is_baseline(self) -> None:
        # #165: NuGet was in neither tier. It matters more than most because
        # `dotnet restore` runs implicitly inside `dotnet build` and
        # `dotnet test` — the failure surfaces at build time rather than at
        # an obvious install step.
        for domain in ("api.nuget.org", "nuget.org"):
            assert domain in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        assert egress_rejection("api.nuget.org", allow, deny) is None

    def test_php_packagist_is_baseline(self) -> None:
        # #168: Packagist was in neither tier. Composer also pulls many dist
        # zips from codeload.github.com, which #148 already made baseline
        # for npm's git dependencies — the same host, needed for the same
        # reason by a different ecosystem.
        for domain in ("repo.packagist.org", "packagist.org", "codeload.github.com"):
            assert domain in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        assert egress_rejection("repo.packagist.org", allow, deny) is None

    def test_cpp_builds_from_apt_without_any_registry(self) -> None:
        # #171: C/C++ is the one language whose default dependency source —
        # the distro mirrors — was already baseline. This asserts the
        # apt-only path end to end, which is also #141's evidence that the
        # baseline works when a language's dependencies come from it.
        allow, deny = effective_egress_bounds(Config())
        for domain in APT_MIRROR_DOMAINS:
            assert egress_rejection(domain, allow, deny) is None
            assert domain not in WELL_KNOWN_REGISTRY_DOMAINS

    def test_conan_is_declarable_not_baseline(self) -> None:
        # #171: Conan is a real registry, but C/C++ dependencies do not
        # normally arrive through it — so it is declare-if-you-need-it
        # rather than seeded into every sandbox. In bounds without operator
        # configuration; still granted late and event-logged.
        assert "center.conan.io" in WELL_KNOWN_REGISTRY_DOMAINS
        assert "center.conan.io" not in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        assert egress_rejection("center.conan.io", allow, deny) is None

    def test_uv_installer_host_is_declarable_not_baseline(self) -> None:
        # #250: astral.sh was in neither tier, so a plan naming uv's own
        # installer was refused outright. Provisioning installs uv from its
        # GitHub release, so the host is a declare-if-needed second line
        # (it serves a curl-into-shell installer, not a package registry)
        # rather than something every sandbox carries.
        assert "astral.sh" in WELL_KNOWN_REGISTRY_DOMAINS
        assert "astral.sh" not in BASELINE_REGISTRY_DOMAINS
        allow, deny = effective_egress_bounds(Config())
        assert egress_rejection("astral.sh", allow, deny) is None

    def test_vcpkg_is_not_in_either_tier(self) -> None:
        # #171: vcpkg clones ports from GitHub and then fetches source
        # tarballs from whatever upstream each port names — unbounded by
        # construction. No fixed host set could cover it, so it stays
        # operator [policy] allow rather than getting a partial grant that
        # works until it doesn't.
        both = (*BASELINE_REGISTRY_DOMAINS, *WELL_KNOWN_REGISTRY_DOMAINS)
        assert "vcpkg.io" not in both

    def test_declarable_tier_survives_being_empty(self) -> None:
        # The tier was empty between the Ruby promotion and Conan landing;
        # nothing may assume it is non-empty.
        assert isinstance(WELL_KNOWN_REGISTRY_DOMAINS, tuple)
        allow, deny = effective_egress_bounds(Config())
        for domain in WELL_KNOWN_REGISTRY_DOMAINS:
            assert egress_rejection(domain, allow, deny) is None

    def test_all_three_cargo_hosts_are_baseline(self) -> None:
        # #156: cargo splits resolution (index), download (static), and API
        # across three hosts. Two out of three fails mid-resolution, which
        # looks like a broken lockfile rather than a blocked host.
        for domain in ("crates.io", "static.crates.io", "index.crates.io"):
            assert domain in BASELINE_REGISTRY_DOMAINS
            assert domain not in WELL_KNOWN_REGISTRY_DOMAINS

    def test_typescript_toolchain_resolves_from_the_baseline(self) -> None:
        # #151: TypeScript reaches the registry for more than application
        # dependencies — the compiler and every `@types/*` package come from
        # there too. All of it is npm, so the #148 promotion covers it; this
        # asserts the coverage rather than assuming it, and fails loudly if
        # a later change moves npm back out of the baseline.
        allow, deny = effective_egress_bounds(Config())
        assert "registry.npmjs.org" in BASELINE_REGISTRY_DOMAINS
        # tsc, @types/*, ts-node, and friends are all plain npm packages:
        # one reachable host covers the whole toolchain.
        assert egress_rejection("registry.npmjs.org", allow, deny) is None
        assert "registry.npmjs.org" not in WELL_KNOWN_REGISTRY_DOMAINS

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
        # any registry in either tier — the "write a Rails app" case, and
        # (since #141) declaring a baseline registry redundantly.
        allow, _deny = effective_egress_bounds(Config())
        for domain in (*BASELINE_REGISTRY_DOMAINS, *WELL_KNOWN_REGISTRY_DOMAINS):
            assert egress_rejection(domain, allow, _deny) is None

    def test_deny_still_blocks_any_in_bounds_registry(self) -> None:
        # Whichever tier a registry sits in, [policy] deny outranks it.
        for domain in (*BASELINE_REGISTRY_DOMAINS, *WELL_KNOWN_REGISTRY_DOMAINS):
            config = Config.model_validate({"policy": {"deny": [domain]}})
            allow, deny = effective_egress_bounds(config)
            reason = egress_rejection(domain, allow, deny)
            assert reason is not None and "deny" in reason


class TestRegistryBounds:
    """A configured private registry's host is in bounds for a plan and
    already granted at provisioning (#680)."""

    CONFIG: ClassVar[dict[str, object]] = {
        "registries": [
            {
                "kind": "npm",
                "host": "artifactory.example.com",
                "url": "https://artifactory.example.com/api/npm/npm-virtual/",
            }
        ]
    }

    def test_registry_host_is_in_bounds_without_policy_allow(self) -> None:
        allow, _deny = effective_egress_bounds(Config.model_validate(self.CONFIG))
        assert egress_rejection("artifactory.example.com", allow, []) is None
        assert egress_rejection("artifactory.example.com", *effective_egress_bounds(Config()))

    def test_granter_treats_the_host_as_already_granted(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        granter = EgressGranter(
            SbxCLI(binary=str(fake_sbx.binary)),
            Config.model_validate(self.CONFIG),
            bus,
            "r1",
            "sbxloop-r1-agent",
        )
        granter.apply("t1", [("artifactory.example.com", "install deps")])
        assert fake_sbx.policies() == []
        assert not [e for e in events if e.type == HostEventTypes.POLICY_DENY]


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
        granter = self.make_granter(fake_sbx, events, policy={"allow": ["api.example-saas.com"]})
        granter.apply("t1", [("api.example-saas.com", "fetch the dataset")])
        assert [
            "allow",
            "network",
            "api.example-saas.com",
            "--sandbox",
            "sbxloop-r1-agent",
        ] in fake_sbx.policies()
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_ALLOW]
        assert event.data["domain"] == "api.example-saas.com"
        assert event.data["reason"] == "fetch the dataset"
        assert event.data["task_id"] == "t1"

    def test_grant_is_idempotent_per_domain(self, fake_sbx: FakeSbx) -> None:
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events, policy={"allow": ["*.example-saas.com"]})
        granter.apply("t1", [("api.example-saas.com", "fetch the dataset")])
        granter.apply("t2", [("api.example-saas.com", "fetch it again")])
        assert len(fake_sbx.policies()) == 1
        assert len([e for e in events if e.type == HostEventTypes.POLICY_ALLOW]) == 1

    def test_well_known_registry_granted_without_operator_config(self, fake_sbx: FakeSbx) -> None:
        # The declarable tier is in bounds with a default config, but still
        # granted via `sbx policy allow` (and event-logged) rather than
        # pre-seeded. #141 emptied the tier — every supported language's
        # registry is baseline now — so this exercises whatever is in it,
        # and comes back to life when something lands there again.
        if not WELL_KNOWN_REGISTRY_DOMAINS:
            pytest.skip("declarable tier is empty (#141 promoted every registry)")
        domain = WELL_KNOWN_REGISTRY_DOMAINS[0]
        events: list[Event] = []
        granter = self.make_granter(fake_sbx, events)
        granter.apply("t1", [(domain, "the toolchain needs it")])
        assert [
            "allow",
            "network",
            domain,
            "--sandbox",
            "sbxloop-r1-agent",
        ] in fake_sbx.policies()
        (event,) = [e for e in events if e.type == HostEventTypes.POLICY_ALLOW]
        assert event.data["domain"] == domain

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
