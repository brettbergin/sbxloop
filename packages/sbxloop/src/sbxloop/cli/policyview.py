"""The effective per-phase egress policy, as data: what ``sbxloop config
policy`` prints and the console's Config screen shows, from one fold."""

from __future__ import annotations

from dataclasses import dataclass, field

from sbxloop.config import Config
from sbxloop.policy import (
    APT_MIRROR_DOMAINS,
    BASELINE_REGISTRY_DOMAINS,
    WELL_KNOWN_REGISTRY_DOMAINS,
    baseline_allows,
)
from sbxloop.sbx.provision import (
    AGENT_ALLOW_DOMAINS,
    github_policy_allows,
    service_policy_allows,
)
from sbxloop.sbx.registries import domains as registry_domains
from sbxloop.sbx.registries import languages as registry_languages

PHASES: tuple[tuple[str, str], ...] = (
    ("decompose", "baseline"),
    (
        "build",
        "baseline + task-declared grants (auto-granted just before build, "
        "within the [policy] bounds below; every grant/refusal is event-logged)",
    ),
    (
        "verify",
        "baseline + grants already made — sbx has no policy revocation, so "
        "grants persist for the sandbox's lifetime (sandboxes are removed at "
        "run end; grants never outlive a run)",
    ),
)


@dataclass(frozen=True)
class PolicyView:
    phases: tuple[tuple[str, str], ...]
    baseline: str
    registries: str
    mirrors: str
    well_known: str
    allow: str
    deny: str
    github: str | None
    service: str | None
    audit: str = "sbxloop logs RUN_ID --type policy."
    notes: tuple[str, ...] = field(default_factory=tuple)


def policy_view(config: Config) -> PolicyView:
    extra = [
        *registry_domains(config.open_registries_for()),
        *config.sandbox.extra_allow_domains,
    ]
    baseline = ", ".join(
        dict.fromkeys([*AGENT_ALLOW_DOMAINS, *config.github.allow_domains, *extra])
    )
    # What provisioning actually seeds, deny applied — an operator reading
    # this needs the effective set, not the constant.
    registries = ", ".join(baseline_allows(BASELINE_REGISTRY_DOMAINS, config.policy.deny))
    mirrors = ", ".join(baseline_allows(APT_MIRROR_DOMAINS, config.policy.deny))
    well_known = ", ".join(WELL_KNOWN_REGISTRY_DOMAINS) or (
        "(none — every supported language's registry is in the baseline above)"
    )
    allow = ", ".join(config.policy.allow) or (
        "(empty — tasks may only use the baseline and well-known registries)"
    )
    deny = ", ".join(config.policy.deny) or "(none)"
    github = ", ".join(github_policy_allows(config)) if config.github.enabled else None
    service: str | None = None
    credentialed = config.credentialed_registries_for()
    if credentialed:
        service = ", ".join(
            service_policy_allows(
                (), credentialed, registry_languages(credentialed), config.policy.deny
            )
        )
    return PolicyView(
        PHASES, baseline, registries, mirrors, well_known, allow, deny, github, service
    )


__all__ = ["PHASES", "PolicyView", "policy_view"]
