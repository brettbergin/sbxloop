"""Plan-declared network egress: bounds checking and grant-late application.

The PLAN phase may declare external domains a task needs during EXECUTE
(``PlanModel.egress``). Declarations are validated against operator-set
bounds (``[policy] allow`` / ``[policy] deny`` in sbxloop.toml) at plan time,
and granted to the agent sandbox — via ``sbx policy allow network <domain>
--sandbox <agent>`` — only just before EXECUTE runs ("grant late"). Every
grant and refusal is emitted as a ``policy.*`` run event, so the persisted
event log doubles as an egress audit trail (``sbxloop logs RUN --type
policy.``).

sbx 0.35 has no primitive for revoking or narrowing an allow on a live
sandbox, so grants are additive for the sandbox's lifetime — SCRUTINIZE and
VERIFY inherit whatever EXECUTE was granted. Grants never outlive a run:
sandboxes are removed at run end, and resume provisions fresh ones. If a
future sbx grows revocation, this module is where "revoke early" lands.

Pattern semantics (ours, not sbx's — bounds govern what may be *requested*;
the requested domain is passed to sbx verbatim): a pattern is an exact
domain, a wildcard ``*.example.com`` (covers ``example.com`` and every
subdomain), or the operator-only ``*`` (covers everything).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sbxloop.config import Config
from sbxloop.errors import SbxError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.sbx.cli import SbxCLI

DOMAIN_PATTERN_RE = re.compile(r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")

# Package registries in the always-reachable baseline: no plan declaration,
# no operator configuration. Issue #145 settled the direction for #141 —
# level *up* (every supported language's registry gets the treatment PyPI
# has always had) rather than level down (PyPI demoted to the declarable
# tier alongside everyone else).
#
# Level-down could not have produced real parity: the worker's own pip
# install and the dev-tools apt ensure run at provision time, before a plan
# exists to declare egress in, so Python would have kept a provision-time
# exception whatever the tiers said — while every plan that never declared
# `pypi.org` started failing mid-EXECUTE.
#
# The cost is audit granularity: a baseline registry emits no `policy.allow`
# event, because there is no grant to log. So the tier stays narrow — the
# read-only public registry hosts of supported languages, nothing else — and
# `[policy] deny` still overrides it, including the provision-time seeding
# (see ``baseline_allows``).
BASELINE_REGISTRY_DOMAINS = (
    "pypi.org",  # PyPI API and the simple index
    "files.pythonhosted.org",  # wheel and sdist downloads
    "registry.npmjs.org",  # npm
    "registry.yarnpkg.com",  # yarn classic (npm mirror)
    # Tarballs for git-hosted dependencies. github.com is already reachable
    # for the clone, but npm and yarn fetch `github:user/repo` deps as
    # tarballs from codeload, which is a separate host — a partial grant
    # fails only for projects that happen to use a git dep.
    "codeload.github.com",
    "proxy.golang.org",  # Go module proxy
    # A reachable proxy with an unreachable checksum database is worse than
    # neither: `go mod download` fails verification rather than falling back,
    # and the fix (GONOSUMDB/GOFLAGS) is not something a plan can discover
    # from the error. Both hosts or nothing.
    "sum.golang.org",
    # Cargo splits its work across three hosts and needs all of them: the
    # sparse index resolves versions, static serves the .crate files, and
    # the API backs `cargo publish`/`cargo search`. A partial grant fails
    # mid-resolution, which reads as a broken lockfile rather than a policy.
    "crates.io",  # cargo API
    "static.crates.io",  # crate downloads
    "index.crates.io",  # cargo sparse index
    "rubygems.org",  # gem downloads and API
    "index.rubygems.org",  # bundler's compact index — a separate host
    "repo.maven.apache.org",  # Maven Central
    "repo1.maven.org",  # Maven Central (the older canonical host)
    # Gradle needs more than the registry: the plugin portal resolves
    # `plugins { }` blocks and services.gradle.org serves the wrapper
    # distribution `gradlew` downloads on first run. A Gradle build fails
    # without either even when Central is reachable.
    "plugins.gradle.org",
    "services.gradle.org",
    "api.nuget.org",  # NuGet v3 API and package downloads
    "nuget.org",  # the gallery host clients still resolve through
    "repo.packagist.org",  # Composer metadata and dist
    "packagist.org",  # the canonical host Composer redirects from
)

# Distro package mirrors: language-neutral infrastructure rather than any one
# ecosystem's registry — the dev-tools apt ensure needs them before a plan
# exists — so they are baseline whatever the registry tiers do.
APT_MIRROR_DOMAINS = (
    "deb.debian.org",
    "security.debian.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ports.ubuntu.com",
)

# Hosts the plan/execute prompts advertise as reachable. Granted to the
# agent sandbox at provision time (see sbx.provision) — the promise must
# hold even under an operator preset that lacks the host: the worker's pip
# installs and the dev-tools apt ensure both run before any plan exists, so
# they cannot rely on plan-declared egress. A plan declaring one of these
# is in-bounds without operator configuration, and needs no re-grant.
PROMPT_ADVERTISED_DOMAINS = (*BASELINE_REGISTRY_DOMAINS, *APT_MIRROR_DOMAINS)

# Package registries the prompts advertise as *declarable*: unlike the
# always-reachable baseline above, these are granted only when a plan names
# them in `egress` (grant-late, event-logged), but the declaration is
# in-bounds without any [policy] allow configuration — the audit trail #141
# weighs the baseline against.
#
# #141 drained this tier into BASELINE_REGISTRY_DOMAINS one language at a
# time, and it is currently empty: every registry of the ten supported
# languages earned unconditional reachability. The tier itself stays — it is
# the right home for a registry that is legitimate but not something every
# build should reach by default (a plan may name it, with a justification,
# and the grant is event-logged). Registries beyond both tiers still need
# operator bounds, and [policy] deny wins over this list like everything
# else.
WELL_KNOWN_REGISTRY_DOMAINS: tuple[str, ...] = ()


def valid_pattern(pattern: str, *, operator: bool = False) -> bool:
    """Whether ``pattern`` is a well-formed domain pattern.

    ``operator=True`` additionally permits the bare ``"*"`` — meaningful as
    an operator bound ("grant anything the plan asks for") but rejected in
    plan declarations, which must name what they need.
    """
    if pattern == "*":
        return operator
    return DOMAIN_PATTERN_RE.fullmatch(pattern) is not None


def pattern_covers(pattern: str, domain: str) -> bool:
    """Whether a bounds pattern covers a requested domain (or wildcard)."""
    pattern = pattern.lower()
    domain = domain.lower()
    if pattern == "*" or pattern == domain:
        return True
    if not pattern.startswith("*."):
        return False
    base = pattern[2:]
    requested = domain[2:] if domain.startswith("*.") else domain
    return requested == base or requested.endswith(f".{base}")


def baseline_allows(domains: Iterable[str], deny: Iterable[str]) -> list[str]:
    """``domains`` minus everything ``[policy] deny`` covers.

    The always-reachable tier is seeded into the sandbox at provision time,
    before any plan exists — the one place a deny pattern cannot be enforced
    by refusing a grant later, because there is no grant. Filtering here is
    what keeps ``[policy] deny`` authoritative as #141 moves registries into
    the baseline: an operator who denies one gets a sandbox that never had
    it, not a sandbox that was seeded with it and then refused a redundant
    re-grant.

    Deliberately not applied to ``provision.AGENT_ALLOW_DOMAINS`` /
    ``GITHUB_ALLOW_DOMAINS``: those are sbxloop's own control plane (the
    Copilot and GitHub APIs the loop itself speaks), not task egress, and a
    run cannot function without them.
    """
    patterns = list(deny)
    return [d for d in domains if not any(pattern_covers(p, d) for p in patterns)]


def egress_rejection(domain: str, allow: list[str], deny: list[str]) -> str | None:
    """Why ``domain`` may not be granted, or None when it is in bounds."""
    for pattern in deny:
        if pattern_covers(pattern, domain):
            return f"matches [policy] deny pattern {pattern!r}"
    if not any(pattern_covers(pattern, domain) for pattern in allow):
        return "not covered by [policy] allow in sbxloop.toml"
    return None


def effective_egress_bounds(config: Config) -> tuple[list[str], list[str]]:
    """The (allow, deny) bounds plan-declared egress is checked against.

    The allow side is the operator's ``[policy] allow`` plus everything the
    agent sandbox can already reach (its provision-time baseline and the
    prompt-advertised hosts) — declaring an already-reachable domain must
    never fail a plan. ``[policy] deny`` still wins over all of it.
    """
    from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS

    allow = [
        *AGENT_ALLOW_DOMAINS,
        *config.sandbox.extra_allow_domains,
        *PROMPT_ADVERTISED_DOMAINS,
        *WELL_KNOWN_REGISTRY_DOMAINS,
        *config.policy.allow,
    ]
    return allow, list(config.policy.deny)


class EgressGranter:
    """Applies one run's plan-declared egress grants to the agent sandbox.

    Grants are applied at EXECUTE entry, not at plan time — the tightest
    point sbx's grant-only policy model permits. Idempotent per domain: the
    provision-time baseline is pre-seeded, and revision loops re-entering
    EXECUTE do not re-grant. Out-of-bounds declarations are refused with a
    ``policy.deny`` event; plan validation normally rejects them, so a deny
    here means the operator tightened bounds after the plan was persisted
    (e.g. across a resume).
    """

    def __init__(self, cli: SbxCLI, config: Config, bus: EventBus, run_id: str, sandbox: str):
        from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS

        self.cli = cli
        self.bus = bus
        self.run_id = run_id
        self.sandbox = sandbox
        self.allow, self.deny = effective_egress_bounds(config)
        self._granted = {
            d.lower()
            for d in (
                *AGENT_ALLOW_DOMAINS,
                # what provisioning actually seeded — a denied baseline
                # domain is not on the sandbox, so it is not "already
                # granted" (``apply`` refuses it on the deny check first
                # either way, but the set should describe reality).
                *baseline_allows(PROMPT_ADVERTISED_DOMAINS, self.deny),
                *config.sandbox.extra_allow_domains,
            )
        }

    def apply(self, task_id: str, egress: list[tuple[str, str]]) -> None:
        """Grant each declared (domain, justification), event-logging both
        grants and refusals. sbx failures propagate (infrastructure error)."""
        for domain, reason in egress:
            domain = domain.lower()
            rejection = egress_rejection(domain, self.allow, self.deny)
            if rejection is not None:
                self.bus.emit(
                    HostEventTypes.POLICY_DENY,
                    self.run_id,
                    task_id=task_id,
                    domain=domain,
                    sandbox=self.sandbox,
                    message=f"egress refused: {domain} — {rejection}",
                )
                continue
            if domain in self._granted:
                continue
            try:
                self.cli.policy_allow(domain, sandbox=self.sandbox)
            except SbxError:
                self.bus.emit(
                    HostEventTypes.POLICY_DENY,
                    self.run_id,
                    task_id=task_id,
                    domain=domain,
                    sandbox=self.sandbox,
                    message=f"egress grant failed: {domain} — sbx policy allow errored",
                )
                raise
            self._granted.add(domain)
            self.bus.emit(
                HostEventTypes.POLICY_ALLOW,
                self.run_id,
                task_id=task_id,
                domain=domain,
                reason=reason,
                sandbox=self.sandbox,
                message=f"egress granted: {domain} — {reason or 'no justification given'}",
            )
