"""The workload profiles as data (#804): what bounds each `[[workloads]]`
entry sets, spelled out the way the operator persona is told them — for
the console's Config screen, the doctor row and anything else that must
say what a workload may do without reading TOML.

Names and patterns only, never a credential's value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sbxloop.config import Config, ScheduleConfig, WorkloadProfile

NO_EGRESS = "none — no host may be granted"
NO_PROFILE_NOTE = (
    "no [[workloads]] profile declared: a workload runs with no profile and every need "
    "but the chat sink is refused; [workload] default is unset"
)


@dataclass(frozen=True)
class ProfileView:
    """One profile's bounds, rendered as label/text rows."""

    name: str
    default: bool
    description: str
    rows: tuple[tuple[str, str], ...]
    #: The stored schedules that run under this profile, one line each.
    schedules: tuple[str, ...]

    @property
    def title(self) -> str:
        return f"{self.name} (default)" if self.default else self.name


def _budget_text(profile: WorkloadProfile) -> str:
    keys = profile.budgets.set_keys
    if not keys:
        return "[budgets] as configured"
    return ", ".join(f"{key} = {getattr(profile.budgets, key)}" for key in keys)


def profile_view(
    config: Config, profile: WorkloadProfile, schedules: Sequence[ScheduleConfig] = ()
) -> ProfileView:
    # The loader already refused a name no [[credentials]] entry carries,
    # so these are names only, each backed by an entry.
    credentials = ", ".join(profile.credentials) or "none"
    sinks = ", ".join(dict.fromkeys(["chat", *profile.sinks]))
    rows = (
        ("egress", ", ".join(profile.egress) or NO_EGRESS),
        ("credentials", credentials),
        ("sinks", sinks),
        ("repo", "a plan may ask for a checkout" if profile.repo else "no checkout"),
        ("publish", profile.publish),
        ("budgets", _budget_text(profile)),
    )
    lines = tuple(
        f"{s.name} · {s.cadence_text}" + (f" ({s.timezone})" if s.timezone else "")
        for s in schedules
        if s.profile == profile.name
    )
    return ProfileView(
        name=profile.name,
        default=profile.name == config.workload.default,
        description=profile.description,
        rows=rows,
        schedules=lines,
    )


def profile_views(config: Config, schedules: Sequence[ScheduleConfig] = ()) -> list[ProfileView]:
    """Every declared profile, the default first."""
    views = [profile_view(config, p, schedules) for p in config.workloads]
    return sorted(views, key=lambda v: (not v.default, v.name))


def profile_summary(config: Config, profile: WorkloadProfile) -> str:
    """One profile as a doctor row says it: counts, with the sinks named."""
    sinks = list(dict.fromkeys(["chat", *profile.sinks]))
    return (
        f"{profile.name}"
        + (" (default)" if profile.name == config.workload.default else "")
        + f": egress {len(profile.egress)} pattern{'s' if len(profile.egress) != 1 else ''}, "
        + f"credentials {len(profile.credentials)}"
        + f", sinks {len(sinks)} ({', '.join(sinks)}), repo {'yes' if profile.repo else 'no'}, "
        + f"publish {profile.publish}"
    )


__all__ = [
    "NO_EGRESS",
    "NO_PROFILE_NOTE",
    "ProfileView",
    "profile_summary",
    "profile_view",
    "profile_views",
]
