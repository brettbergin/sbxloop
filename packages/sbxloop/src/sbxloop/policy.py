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

from sbxloop.config import Config
from sbxloop.errors import SbxError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.sbx.cli import SbxCLI

DOMAIN_PATTERN_RE = re.compile(r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}")

# Hosts the plan/execute prompts advertise as reachable. Granted to the
# agent sandbox at provision time (see sbx.provision) — the promise must
# hold even under an operator preset that lacks the host: the worker's pip
# installs and the dev-tools apt ensure both run before any plan exists, so
# they cannot rely on plan-declared egress. A plan declaring one of these
# is in-bounds without operator configuration, and needs no re-grant.
PROMPT_ADVERTISED_DOMAINS = (
    "pypi.org",
    "files.pythonhosted.org",
    "deb.debian.org",
    "security.debian.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ports.ubuntu.com",
)

# Well-known read-only package registries the prompts advertise as
# *declarable*: unlike the always-reachable baseline above, these are granted
# only when a plan names them in `egress` (grant-late, event-logged), but the
# declaration is in-bounds without any [policy] allow configuration. This is
# what lets "write a Rails app" bundle-install out of the box while keeping
# the audit trail. Registries beyond this set still need operator bounds;
# [policy] deny wins over this list like everything else.
WELL_KNOWN_REGISTRY_DOMAINS = (
    "rubygems.org",  # gem downloads and API
    "index.rubygems.org",  # bundler's compact index
    "registry.npmjs.org",  # npm
    "registry.yarnpkg.com",  # yarn classic (npm mirror)
    "crates.io",  # cargo API
    "static.crates.io",  # crate downloads
    "index.crates.io",  # cargo sparse index
    "proxy.golang.org",  # Go module proxy
    "sum.golang.org",  # Go checksum database
)


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
                *PROMPT_ADVERTISED_DOMAINS,
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
