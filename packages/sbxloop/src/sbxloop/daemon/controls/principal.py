"""Who is asking.

Every operator surface used to hand the loop a free-form ``by`` string —
"brett via sbxloop daemon ctl", a chat author's display name — and the
loop passed it on to the source as attribution. That string is not an
identity: nothing checked it, and a remote client could send any text.
A :class:`Principal` separates the two. The attribution string is derived
*from* the principal for the source-facing sentence the loop already
writes; a principal is never built from a caller-supplied attribution.

The surfaces that exist today (the ``ctl`` queue on the host, a chat
channel the operator restricted, the local console, the concierge) are
trusted completely, as they always were: :meth:`Principal.trusted` gives
them every capability and keeps their attribution byte-for-byte. A
principal with fewer capabilities can only come from an authenticated
surface that knows what it granted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

Capability = Literal[
    "runs:read",
    "artifacts:read",
    "items:create",
    "runs:steer",
    "runs:control",
    "gates:approve",
    "budgets:grant",
    "daemon:manage",
    "credentials:manage",
    "audit:read",
    "diagnostics:read",
    "collaboration:read",
    "collaboration:write",
    "collaboration:delegate",
]

#: Every capability, in the order the spike lists them.
CAPABILITIES: tuple[Capability, ...] = get_args(Capability)
ALL_CAPABILITIES: frozenset[Capability] = frozenset(CAPABILITIES)

#: The one workspace a single installation has. A hosted deployment varies
#: this; nothing here may assume the value.
WORKSPACE_ID = "local"

PrincipalKind = Literal["operator", "client", "system"]


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated (or host-trusted) actor and what it may do.

    ``display`` is the attribution the loop and the source hear — for a
    trusted surface, exactly the ``by`` string it used to pass. ``via``
    names the surface for the audit line. ``id`` is stable across requests
    from the same actor where the surface can tell (a client id, a login
    name); a trusted surface without one falls back to its ``via``.
    """

    kind: PrincipalKind
    id: str
    display: str | None
    via: str
    capabilities: frozenset[Capability] = field(default=ALL_CAPABILITIES)
    workspace_id: str = WORKSPACE_ID

    @classmethod
    def trusted(cls, by: str | None, via: str) -> Principal:
        """A host-trusted operator: every capability, the legacy attribution
        kept as it is (``None`` stays ``None`` so the loop's own
        ``by or "operator"`` fallbacks render the same words)."""
        return cls(kind="operator", id=by or via, display=by, via=via)

    @classmethod
    def system(cls, via: str) -> Principal:
        """The daemon acting for itself (a scheduled tick, recovery)."""
        return cls(kind="system", id="daemon", display=None, via=via)

    def attribution(self) -> str | None:
        """The ``by`` string the loop passes to the source."""
        return self.display

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def audit(self) -> dict[str, object]:
        """The structured fields an audit record carries; never secrets."""
        return {
            "kind": self.kind,
            "id": self.id,
            "display": self.display,
            "via": self.via,
            "workspace_id": self.workspace_id,
        }
